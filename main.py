import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List

import pytz
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

from app_storage import init_db, is_registered, upsert_user, get_user
from employees import load_employees, build_fio_index, find_employee_by_fio, normalize_phone_to_e164_ru
from ews_api import CalendarAPI, EwsConfig
from bot_utils import (
    parse_natural_date, format_event, format_room_event, filter_future_events,
    get_current_datetime_msk, get_calendar_emoji_for_date, format_time_line, create_progress_bar
)


# ========== Состояния FSM ==========
class RegisterStates(StatesGroup):
    waiting_for_contact = State()
    waiting_for_ews_username = State()
    waiting_for_ews_password = State()


class CreateMeeting(StatesGroup):
    subject = State()
    date = State()
    time = State()
    duration = State()
    attendees = State()
    room = State()
    confirm = State()
    change_attendee = State()


class ScheduleStates(StatesGroup):
    waiting_for_date = State()


class RescheduleMeeting(StatesGroup):
    choosing_event = State()
    new_date = State()
    new_time = State()
    new_duration = State()
    confirming = State()
    page = State()
    events_list = State()


class RoomScheduleStates(StatesGroup):
    waiting_for_date = State()


class BookingStates(StatesGroup):
    waiting_for_subject = State()


# ========== Вспомогательные функции ==========
def _env(name: str, *, required: bool = True, default: str = "") -> str:
    v = os.environ.get(name, default).strip()
    if required and not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


BOT_TOKEN = _env("BOT_TOKEN")
EWS_URL = _env("EWS_URL")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

ROOMS = {
    "Москва": "room_msk@promotion-lc.ru",
    "СПб": "room_spb@promotion-lc.ru",
}

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("calendar-bot")

tz = pytz.timezone("Europe/Moscow")


@dataclass
class AppContext:
    employees_by_phone: Dict[str, object]
    fio_index: Dict[str, object]
    ews_cfg: EwsConfig


CTX: Optional[AppContext] = None


# ========== Клавиатуры ==========
def main_menu() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📅 Расписание"))
    builder.add(KeyboardButton(text="➕ Создать встречу"))
    builder.add(KeyboardButton(text="🏢 Переговорки"))
    builder.add(KeyboardButton(text="✏️ Изменить встречи"))
    builder.add(KeyboardButton(text="👤 Мой профиль"))
    builder.add(KeyboardButton(text="❌ Отмена"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


def schedule_submenu() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="Сегодня"))
    builder.add(KeyboardButton(text="Завтра"))
    builder.add(KeyboardButton(text="Выбрать день"))
    builder.add(KeyboardButton(text="📆 Эта неделя"))
    builder.add(KeyboardButton(text="Назад"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


def registration_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📱 Подтвердить номер телефона", request_contact=True))
    builder.add(KeyboardButton(text="❌ Отмена"))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="❌ Отмена"))
    return builder.as_markup(resize_keyboard=True)


def create_meeting_keyboard(step: int = 1, total: int = 6) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    if step > 1:
        builder.add(KeyboardButton(text="◀️ Назад"))
    builder.add(KeyboardButton(text="❌ Отмена"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


def back_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="◀️ Назад"))
    builder.add(KeyboardButton(text="❌ Отмена"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


def _get_user_calendar_api(tg_user_id: int) -> CalendarAPI:
    u = get_user(tg_user_id)
    if not u:
        raise RuntimeError("User not registered")
    assert CTX is not None
    return CalendarAPI(
        email=u.email,
        username=u.ews_username,
        password=u.ews_password,
        cfg=CTX.ews_cfg,
    )


async def _ensure_registered(message: types.Message, state: FSMContext) -> bool:
    if is_registered(message.from_user.id):
        return True
    await state.set_state(RegisterStates.waiting_for_contact)
    await message.answer(
        "🔐 **Регистрация** (шаг 1 из 3)\n\n"
        "1) Подтвердите номер телефона (кнопка ниже)\n"
        "2) Введите логин (например: a.leonov)\n"
        "3) Введите пароль (⚠️ пароль будет виден в истории чата, удалите сообщение после)\n\n"
        "Я сверю телефон по списку сотрудников.",
        parse_mode="Markdown",
        reply_markup=registration_keyboard(),
    )
    return False


def _resolve_attendees_to_emails(raw: str) -> Tuple[List[str], List[str]]:
    assert CTX is not None
    raw = (raw or "").strip()
    if not raw or raw == "-":
        return [], []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    emails: List[str] = []
    warnings: List[str] = []
    for q in parts:
        found = find_employee_by_fio(q, CTX.fio_index, score_cutoff=78)
        if found:
            emp, score = found
            emails.append(emp.email)
            if score < 95:
                warnings.append(f"⚠️ Уточнение: '{q}' → '{emp.fio}' ({score}%)")
        else:
            bits = q.lower().split()
            if len(bits) >= 2:
                emails.append(f"{bits[0]}.{bits[1]}@promotion-lc.ru")
            else:
                emails.append(f"{q.lower().replace(' ', '.')}@promotion-lc.ru")
            warnings.append(f"⚠️ Не нашёл '{q}' в базе. Использую e-mail по шаблону.")
    return emails, warnings


async def startup() -> None:
    global CTX
    try:
        init_db()
        employees_by_phone = load_employees()
        fio_index = build_fio_index(employees_by_phone)
        CTX = AppContext(
            employees_by_phone=employees_by_phone,
            fio_index=fio_index,
            ews_cfg=EwsConfig(ews_url=EWS_URL, rooms=ROOMS),
        )
        logger.info("Loaded employees: %s", len(employees_by_phone))
    except Exception as e:
        logger.exception("Ошибка при старте бота")
        raise


# ========== Основной код бота ==========
async def main() -> None:
    await startup()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # --- Команда /start ---
    @dp.message(Command("start"))
    async def cmd_start(message: types.Message, state: FSMContext):
        # Сброс состояния при старте
        await state.clear()
        if not await _ensure_registered(message, state):
            return
        await message.answer(
            "👋 Привет! Я персональный ассистент календаря.\n"
            "Работаю с Outlook/Exchange через EWS.\n\n"
            "📌 **Основные команды:**\n"
            "• /help – подробная справка\n\n"
            "Выберите действие в меню 👇",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )

    # --- Команда /help ---
    @dp.message(Command("help"))
    async def cmd_help(message: types.Message, state: FSMContext):
        await state.clear()
        text = (
            "📌 **Помощь по боту**\n\n"
            "**Основные возможности:**\n"
            "• 📅 **Расписание** – просмотр встреч на сегодня, завтра, выбранный день или неделю.\n"
            "• ➕ **Создать встречу** – пошаговый мастер с указанием темы, даты, времени, участников и комнаты.\n"
            "• 🏢 **Переговорки** – расписание комнат и бронирование.\n"
            "• ✏️ **Изменить встречи** – удаление или перенос существующих встреч.\n"
            "• 👤 **Мой профиль** – ваши данные (ФИО, email, логин).\n"
            "• ❌ **Отмена** – прервать текущее действие.\n\n"
            "**Советы:**\n"
            "• Даты можно вводить в свободной форме: *завтра*, *15 марта*, *следующий понедельник*.\n"
            "• Время пишите в формате *14:30*.\n"
            "• Участников указывайте по ФИО через запятую (например: *Иванов Иван, Петров Пётр*).\n\n"
            "Если что-то пошло не так, нажмите кнопку «❌ Отмена» и попробуйте снова."
        )
        await message.answer(text, parse_mode="Markdown", reply_markup=main_menu())

    # --- Отмена любого действия ---
    @dp.message(F.text == "❌ Отмена")
    async def cancel_any(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer("✅ Действие отменено. Главное меню:", reply_markup=main_menu())

    # ========== Регистрация ==========
    @dp.message(RegisterStates.waiting_for_contact)
    async def reg_contact(message: types.Message, state: FSMContext):
        if message.contact is None or message.contact.phone_number is None:
            await message.answer("Нужно нажать кнопку «📱 Подтвердить номер телефона».", reply_markup=registration_keyboard())
            return
        phone = normalize_phone_to_e164_ru(message.contact.phone_number)
        if not phone:
            await message.answer("Не смог распознать номер. Попробуйте ещё раз.", reply_markup=registration_keyboard())
            return
        assert CTX is not None
        emp = CTX.employees_by_phone.get(phone)
        if not emp:
            await message.answer(
                "❌ Ваш номер телефона не найден в списке сотрудников.\n"
                "Если номер в Excel отличается — поправьте Excel и перезапустите бота.",
                reply_markup=main_menu(),
            )
            await state.clear()
            return
        await state.update_data(phone_e164=phone, fio=emp.fio, email=emp.email)
        await state.set_state(RegisterStates.waiting_for_ews_username)
        await message.answer(
            f"✅ Телефон подтверждён: {emp.fio}\n\n"
            f"📝 **Регистрация (шаг 2 из 3)**\n"
            f"Введите логин Exchange (например: `a.leonov`):",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )

    @dp.message(RegisterStates.waiting_for_ews_username)
    async def reg_username(message: types.Message, state: FSMContext):
        username = (message.text or "").strip()
        if not username:
            await message.answer("Введите логин (например: `a.leonov`).", parse_mode="Markdown", reply_markup=cancel_keyboard())
            return
        await state.update_data(ews_username=username)
        await state.set_state(RegisterStates.waiting_for_ews_password)
        await message.answer(
            f"📝 **Регистрация (шаг 3 из 3)**\n\n"
            f"Введите пароль Exchange/Outlook.\n\n"
            f"⚠️ **Внимание:** пароль будет виден в истории чата. После регистрации удалите это сообщение.\n"
            f"Для удобства можете скопировать и сразу удалить.",
            reply_markup=cancel_keyboard()
        )

    @dp.message(RegisterStates.waiting_for_ews_password)
    async def reg_password(message: types.Message, state: FSMContext):
        password = (message.text or "").strip()
        if not password:
            await message.answer("Пароль пустой. Введите пароль ещё раз.", reply_markup=cancel_keyboard())
            return
        data = await state.get_data()
        phone_e164 = data["phone_e164"]
        fio = data["fio"]
        email = data["email"]
        username = data["ews_username"]

        assert CTX is not None
        try:
            api = CalendarAPI(email=email, username=username, password=password, cfg=CTX.ews_cfg)
            _ = api.get_my_events(get_current_datetime_msk().date())
        except Exception as e:
            await message.answer(
                f"❌ Не удалось войти в Exchange: {e}\n\n"
                f"Попробуйте снова: введите логин (шаг 2 из 3).",
                reply_markup=cancel_keyboard()
            )
            await state.set_state(RegisterStates.waiting_for_ews_username)
            return

        upsert_user(
            tg_user_id=message.from_user.id,
            phone_e164=phone_e164,
            fio=fio,
            email=email,
            ews_username=username,
            ews_password=password,
        )
        await state.clear()
        await message.answer(
            "✅ Регистрация завершена!\n\n"
            "Теперь вы можете:\n"
            "• Смотреть расписание\n"
            "• Создавать встречи\n"
            "• Бронировать переговорки\n\n"
            "Выберите действие в меню:",
            reply_markup=main_menu()
        )

    # ========== Расписание ==========
    @dp.message(F.text == "📅 Расписание")
    async def schedule_menu(message: types.Message, state: FSMContext):
        # Автоматический сброс состояния
        if await state.get_state() is not None:
            await state.clear()
            await message.answer("🔄 Предыдущее действие отменено. Начинаю расписание.")
        if not await _ensure_registered(message, state):
            return
        await state.set_state(ScheduleStates.waiting_for_date)
        await message.answer("📅 Выберите день:", reply_markup=schedule_submenu())

    @dp.message(ScheduleStates.waiting_for_date, F.text == "Сегодня")
    async def show_today(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        api = _get_user_calendar_api(message.from_user.id)
        current_dt = get_current_datetime_msk()
        today = current_dt.date()
        events = api.get_my_events(today)
        events = filter_future_events(events, current_dt)

        morning, day, evening = [], [], []
        for ev in events:
            hour = ev.start.astimezone(tz).hour
            if hour < 12:
                morning.append(ev)
            elif hour < 18:
                day.append(ev)
            else:
                evening.append(ev)

        text = f"📅 **Сегодня, {today.strftime('%d.%m.%Y')}**\n"
        text += f"(показаны встречи после {current_dt.strftime('%H:%M')})\n\n"

        if morning:
            text += "🌅 **Утро** (до 12:00)\n"
            for ev in morning:
                text += f"{format_event(ev, show_header=False)}\n"
            text += "\n"
        if day:
            text += "🌞 **День** (12:00-18:00)\n"
            for ev in day:
                text += f"{format_event(ev, show_header=False)}\n"
            text += "\n"
        if evening:
            text += "🌙 **Вечер** (после 18:00)\n"
            for ev in evening:
                text += f"{format_event(ev, show_header=False)}\n"
            text += "\n"

        if not events:
            text += "📭 На сегодня предстоящих встреч нет.\n\nОтдыхайте! 🎉"

        await message.answer(text, parse_mode="Markdown", reply_markup=main_menu())

    @dp.message(ScheduleStates.waiting_for_date, F.text == "Завтра")
    async def show_tomorrow(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        api = _get_user_calendar_api(message.from_user.id)
        tomorrow = (get_current_datetime_msk() + timedelta(days=1)).date()
        events = api.get_my_events(tomorrow)
        calendar_emoji = get_calendar_emoji_for_date(tomorrow)

        if events:
            text = f"{calendar_emoji} **Завтра, {tomorrow.strftime('%d.%m.%Y')}**\n\n"
            text += "\n".join([format_event(e) for e in events])
        else:
            text = f"{calendar_emoji} **Завтра, {tomorrow.strftime('%d.%m.%Y')}**\n\n📭 На завтра встреч нет.\n\nПланируйте свой день! ✨"
        await message.answer(text, parse_mode="Markdown", reply_markup=main_menu())

    @dp.message(ScheduleStates.waiting_for_date, F.text == "📆 Эта неделя")
    async def show_week(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        api = _get_user_calendar_api(message.from_user.id)
        current_dt = get_current_datetime_msk()
        start_of_week = current_dt - timedelta(days=current_dt.weekday())
        end_of_week = start_of_week + timedelta(days=6)

        text = f"📅 **Неделя {start_of_week.strftime('%d.%m')} – {end_of_week.strftime('%d.%m.%Y')}**\n\n"
        for i in range(7):
            day = (start_of_week + timedelta(days=i)).date()
            events = api.get_my_events(day)
            if day == current_dt.date():
                events = filter_future_events(events, current_dt)
                day_str = "Сегодня"
            elif day == (current_dt + timedelta(days=1)).date():
                day_str = "Завтра"
            else:
                day_str = day.strftime("%d.%m")
            calendar_emoji = get_calendar_emoji_for_date(day)
            text += f"{calendar_emoji} **{day_str}**\n"
            if events:
                for ev in events:
                    text += f"{format_event(ev, show_header=False)}\n"
            else:
                text += "   📭 Нет встреч\n"
            text += "\n"
        await message.answer(text, parse_mode="Markdown", reply_markup=main_menu())

    @dp.message(ScheduleStates.waiting_for_date, F.text == "Выбрать день")
    async def ask_day(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        await message.answer(
            "📅 Введите дату:\n\n"
            "💡 Примеры: завтра, 15 марта, следующий четверг",
            reply_markup=back_keyboard()
        )

    @dp.message(F.text == "Назад")
    async def back_to_main(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer("✅ Возврат в главное меню:", reply_markup=main_menu())

    @dp.message(ScheduleStates.waiting_for_date)
    async def show_day(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        d = parse_natural_date((message.text or "").strip().lower())
        if not d:
            await message.answer(
                "🤔 Я не распознал дату.\n\n"
                "Попробуйте так:\n"
                "• завтра\n"
                "• 15 марта\n"
                "• следующий понедельник\n\n"
                "Или нажмите «◀️ Назад»:",
                reply_markup=back_keyboard()
            )
            return
        api = _get_user_calendar_api(message.from_user.id)
        events = api.get_my_events(d)
        current_dt = get_current_datetime_msk()
        if d == current_dt.date():
            events = filter_future_events(events, current_dt)
            date_desc = f"сегодня (после {current_dt.strftime('%H:%M')})"
        else:
            date_desc = d.strftime("%d.%m.%Y")
        calendar_emoji = get_calendar_emoji_for_date(d)
        if events:
            text = f"{calendar_emoji} **{date_desc}**\n\n" + "\n".join([format_event(e) for e in events])
        else:
            text = f"{calendar_emoji} **{date_desc}**\n\n📭 На этот день встреч нет."
        await message.answer(text, parse_mode="Markdown", reply_markup=main_menu())

    # ========== Создание встречи ==========
    @dp.message(F.text == "➕ Создать встречу")
    async def create_start(message: types.Message, state: FSMContext):
        # Автоматический сброс состояния
        if await state.get_state() is not None:
            await state.clear()
            await message.answer("🔄 Предыдущее действие отменено. Начинаю создание встречи.")
        if not await _ensure_registered(message, state):
            return
        await state.set_state(CreateMeeting.subject)
        await state.update_data(step=1)
        await message.answer(
            "📝 **Создание встречи**\n\n"
            f"{create_progress_bar(1, 6, 'Шаг', 'из 6')}\n\n"
            "Введите тему встречи:\n"
            "━━━━━━━━━━━━━━━━\n"
            "💡 *Пример: «Планерка проекта»*",
            parse_mode="Markdown",
            reply_markup=create_meeting_keyboard(1, 6)
        )

    @dp.message(CreateMeeting.subject)
    async def create_subject(message: types.Message, state: FSMContext):
        data = await state.get_data()
        if data.get("book_room"):
            subject = (message.text or "").strip()
            if not subject:
                await message.answer("⚠️ Тема не может быть пустой. Введите тему:", reply_markup=create_meeting_keyboard(1, 6))
                return
            await state.update_data(subject=subject, step=2)
            await state.set_state(CreateMeeting.date)
            await message.answer(
                "📝 **Создание встречи**\n\n"
                f"{create_progress_bar(2, 6, 'Шаг', 'из 6')}\n\n"
                "Когда встреча?\n\n"
                "💡 *Примеры: завтра, 15 марта*",
                parse_mode="Markdown",
                reply_markup=create_meeting_keyboard(2, 6)
            )
            return

        subject = (message.text or "").strip()
        if not subject:
            await message.answer("⚠️ Тема не может быть пустой. Введите тему:", reply_markup=create_meeting_keyboard(1, 6))
            return
        await state.update_data(subject=subject, step=2)
        await state.set_state(CreateMeeting.date)
        await message.answer(
            "📝 **Создание встречи**\n\n"
            f"{create_progress_bar(2, 6, 'Шаг', 'из 6')}\n\n"
            "Когда встреча?\n\n"
            "💡 *Примеры: завтра, 15 марта, в понедельник*",
            parse_mode="Markdown",
            reply_markup=create_meeting_keyboard(2, 6)
        )

    @dp.message(CreateMeeting.date)
    async def create_date(message: types.Message, state: FSMContext):
        d = parse_natural_date(message.text or "")
        if not d:
            await message.answer(
                "🤔 Я не распознал дату.\n\n"
                "Попробуйте так:\n"
                "• завтра\n"
                "• 15 марта\n\n"
                "Или нажмите «◀️ Назад»:",
                reply_markup=back_keyboard()
            )
            return
        await state.update_data(date=d, step=3)
        await state.set_state(CreateMeeting.time)
        await message.answer(
            "📝 **Создание встречи**\n\n"
            f"{create_progress_bar(3, 6, 'Шаг', 'из 6')}\n\n"
            "Во сколько начало?\n\n"
            "💡 *Пример: 14:00*",
            parse_mode="Markdown",
            reply_markup=create_meeting_keyboard(3, 6)
        )

    @dp.message(CreateMeeting.time)
    async def create_time(message: types.Message, state: FSMContext):
        try:
            t = datetime.strptime((message.text or "").strip(), "%H:%M").time()
        except Exception:
            await message.answer(
                "🤔 Неверный формат времени.\n\n"
                "Используйте ЧЧ:ММ, например 14:30\n\n"
                "Или нажмите «◀️ Назад»:",
                reply_markup=back_keyboard()
            )
            return
        await state.update_data(time=t, step=4)
        await state.set_state(CreateMeeting.duration)

        kb = InlineKeyboardBuilder()
        kb.button(text="⏱ 15 мин", callback_data="duration_15")
        kb.button(text="⏱ 30 мин", callback_data="duration_30")
        kb.button(text="⏱ 1 час", callback_data="duration_60")
        kb.button(text="⏱ 1.5 часа", callback_data="duration_90")
        kb.adjust(2)
        await message.answer(
            "📝 **Создание встречи**\n\n"
            f"{create_progress_bar(4, 6, 'Шаг', 'из 6')}\n\n"
            "Длительность в минутах?\n\n"
            "💡 *Примеры: 30, 45, 60, 90*\n"
            "*По умолчанию: 60 минут*",
            parse_mode="Markdown",
            reply_markup=create_meeting_keyboard(4, 6)
        )
        await message.answer("Или выберите быстро:", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("duration_"))
    async def duration_preset(callback: types.CallbackQuery, state: FSMContext):
        duration_map = {"duration_15": 15, "duration_30": 30, "duration_60": 60, "duration_90": 90}
        duration = duration_map.get(callback.data, 60)
        await state.update_data(duration=duration, step=5)
        await state.set_state(CreateMeeting.attendees)

        data = await state.get_data()
        if data.get("book_room"):
            await handle_booking_after_duration(callback.message, state, duration)
        else:
            await callback.message.answer(
                "📝 **Создание встречи**\n\n"
                f"{create_progress_bar(5, 6, 'Шаг', 'из 6')}\n\n"
                "Укажите участников через запятую (ФИО):\n\n"
                "💡 *Пример: Иванов Иван, Петров Пётр*\n"
                "или «-» если никого не добавлять",
                parse_mode="Markdown",
                reply_markup=create_meeting_keyboard(5, 6)
            )
        await callback.answer()

    async def handle_booking_after_duration(message: types.Message, state: FSMContext, duration: int):
        data = await state.get_data()
        room = data.get("book_room")
        start_dt = tz.localize(datetime.combine(data["date"], data["time"]))
        end_dt = start_dt + timedelta(minutes=duration)

        api = _get_user_calendar_api(message.from_user.id)
        available, msg = api.is_room_available(room, start_dt, end_dt)
        if not available:
            await message.answer(
                f"⚠️ {msg}\n\n"
                f"Попробуйте другое время или создайте без комнаты.",
                reply_markup=back_keyboard()
            )
            await state.clear()
            return

        await state.update_data(attendees=[], room=room)
        text = (
            f"📝 **Подтверждение встречи**\n\n"
            f"📌 **Тема:** {data['subject']}\n"
            f"📅 **Дата:** {start_dt.strftime('%d.%m.%Y')}\n"
            f"⏰ **Время:** {start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}\n"
            f"🏢 **Комната:** {room}\n"
            f"\nПодтвердить создание?"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Да", callback_data="confirm_booking_yes")
        kb.button(text="❌ Нет", callback_data="confirm_no")
        kb.button(text="👥 Добавить участников", callback_data="add_attendees_to_booking")
        kb.adjust(2)
        await state.set_state(CreateMeeting.confirm)
        await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")

    @dp.message(CreateMeeting.duration)
    async def create_duration(message: types.Message, state: FSMContext):
        data = await state.get_data()
        txt = (message.text or "").strip()
        try:
            duration = int(txt) if txt else 60
        except Exception:
            duration = 60
        await state.update_data(duration=duration, step=5)

        if data.get("book_room"):
            room = data.get("book_room")
            start_dt = tz.localize(datetime.combine(data["date"], data["time"]))
            end_dt = start_dt + timedelta(minutes=duration)

            api = _get_user_calendar_api(message.from_user.id)
            available, msg = api.is_room_available(room, start_dt, end_dt)
            if not available:
                await message.answer(f"⚠️ {msg}\nПопробуйте другое время или создайте без комнаты.", reply_markup=back_keyboard())
                await state.clear()
                return

            await state.update_data(attendees=[], room=room)
            text = (
                f"📝 **Подтверждение встречи**\n\n"
                f"📌 **Тема:** {data['subject']}\n"
                f"📅 **Дата:** {start_dt.strftime('%d.%m.%Y')}\n"
                f"⏰ **Время:** {start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}\n"
                f"🏢 **Комната:** {room}\n"
                f"\nПодтвердить создание?"
            )
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Да", callback_data="confirm_booking_yes")
            kb.button(text="❌ Нет", callback_data="confirm_no")
            kb.button(text="👥 Добавить участников", callback_data="add_attendees_to_booking")
            kb.adjust(2)
            await state.set_state(CreateMeeting.confirm)
            await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")
            return

        await state.set_state(CreateMeeting.attendees)
        await message.answer(
            "📝 **Создание встречи**\n\n"
            f"{create_progress_bar(5, 6, 'Шаг', 'из 6')}\n\n"
            "Укажите участников через запятую (ФИО):\n\n"
            "💡 *Пример: Иванов Иван, Петров Пётр*\n"
            "или «-» если никого не добавлять",
            parse_mode="Markdown",
            reply_markup=create_meeting_keyboard(5, 6)
        )

    @dp.message(CreateMeeting.attendees)
    async def create_attendees(message: types.Message, state: FSMContext):
        data = await state.get_data()
        if data.get("book_room"):
            emails, warnings = _resolve_attendees_to_emails(message.text or "")
            await state.update_data(attendees=emails)

            room = data.get("book_room")
            start_dt = tz.localize(datetime.combine(data["date"], data["time"]))
            end_dt = start_dt + timedelta(minutes=int(data["duration"]))

            text = (
                f"📝 **Подтверждение встречи**\n\n"
                f"📌 **Тема:** {data['subject']}\n"
                f"📅 **Дата:** {start_dt.strftime('%d.%m.%Y')}\n"
                f"⏰ **Время:** {start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}\n"
                f"🏢 **Комната:** {room}\n"
            )
            if emails:
                text += f"👥 **Участники:** {', '.join(emails)}\n"
            text += "\nПодтвердить создание?"

            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Да", callback_data="confirm_booking_yes")
            kb.button(text="❌ Нет", callback_data="confirm_no")
            kb.adjust(2)
            await state.set_state(CreateMeeting.confirm)
            await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")
            return

        emails, warnings = _resolve_attendees_to_emails(message.text or "")
        await state.update_data(attendees=emails)

        if emails:
            found = [e for e in emails if e.endswith("@promotion-lc.ru")]
            if found:
                await message.answer(f"✅ Найдено: {', '.join(found)}")
        if warnings:
            await message.answer("\n".join(warnings))

        builder = InlineKeyboardBuilder()
        builder.button(text="Москва", callback_data="room_msk")
        builder.button(text="СПб", callback_data="room_spb")
        builder.button(text="Без комнаты", callback_data="room_none")
        builder.adjust(2)

        await state.set_state(CreateMeeting.room)
        await message.answer("Нужна переговорка?", reply_markup=builder.as_markup())

    @dp.callback_query(CreateMeeting.room)
    async def create_room(callback: types.CallbackQuery, state: FSMContext):
        room = None
        if callback.data == "room_msk":
            room = "Москва"
        elif callback.data == "room_spb":
            room = "СПб"

        await state.update_data(room=room)
        data = await state.get_data()

        start_dt = tz.localize(datetime.combine(data["date"], data["time"]))
        end_dt = start_dt + timedelta(minutes=int(data["duration"]))

        api = _get_user_calendar_api(callback.from_user.id)
        if room:
            available, msg = api.is_room_available(room, start_dt, end_dt)
            if not available:
                await callback.message.edit_text(f"⚠️ {msg}\nПопробуйте другое время или создайте без комнаты.")
                await state.clear()
                await callback.answer()
                return

        text = (
            f"📌 **Тема:** {data['subject']}\n"
            f"📅 **Дата:** {start_dt.strftime('%d.%m.%Y')}\n"
            f"⏰ **Время:** {start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}\n"
        )
        if data.get("attendees"):
            text += f"👥 **Участники:** {', '.join(data['attendees'])}\n"
        if room:
            text += f"🏢 **Комната:** {room}\n"
        text += "\nПодтвердить создание?"

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Да", callback_data="confirm_yes")
        kb.button(text="❌ Нет", callback_data="confirm_no")
        kb.button(text="🔄 Сменить участника", callback_data="change_attendee")
        kb.adjust(2)

        await state.set_state(CreateMeeting.confirm)
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")
        await callback.answer()

    @dp.callback_query(CreateMeeting.confirm, F.data == "confirm_yes")
    async def confirm_create(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        start_dt = tz.localize(datetime.combine(data["date"], data["time"]))
        end_dt = start_dt + timedelta(minutes=int(data["duration"]))

        api = _get_user_calendar_api(callback.from_user.id)
        ok, msg, _ = api.create_event(
            subject=data["subject"],
            start=start_dt,
            end=end_dt,
            attendees_emails=data.get("attendees", []),
            room_name=data.get("room"),
        )
        await callback.message.answer(msg, reply_markup=main_menu())
        await state.clear()
        await callback.answer()

    @dp.callback_query(CreateMeeting.confirm, F.data == "confirm_booking_yes")
    async def confirm_booking(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        start_dt = tz.localize(datetime.combine(data["date"], data["time"]))
        end_dt = start_dt + timedelta(minutes=int(data["duration"]))

        api = _get_user_calendar_api(callback.from_user.id)
        room_name = data.get("book_room") or data.get("room")
        attendees = data.get("attendees", [])

        ok, msg, _ = api.create_event(
            subject=data["subject"],
            start=start_dt,
            end=end_dt,
            attendees_emails=attendees,
            room_name=room_name,
        )
        await callback.message.answer(msg, reply_markup=main_menu())
        await state.clear()
        await callback.answer()

    @dp.callback_query(CreateMeeting.confirm, F.data == "confirm_no")
    async def confirm_no(callback: types.CallbackQuery, state: FSMContext):
        await callback.message.edit_text("Создание встречи отменено.", reply_markup=main_menu())
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "change_attendee")
    async def change_attendee_handler(callback: types.CallbackQuery, state: FSMContext):
        await state.set_state(CreateMeeting.attendees)
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад", callback_data="back_to_room")
        builder.adjust(1)
        await callback.message.edit_text("Введите участников через запятую (ФИО) или '-' если никого не добавлять:", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data == "back_to_room")
    async def back_to_room_handler(callback: types.CallbackQuery, state: FSMContext):
        builder = InlineKeyboardBuilder()
        builder.button(text="Москва", callback_data="room_msk")
        builder.button(text="СПб", callback_data="room_spb")
        builder.button(text="Без комнаты", callback_data="room_none")
        builder.adjust(2)
        await callback.message.edit_text("Нужна переговорка?", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data == "add_attendees_to_booking")
    async def add_attendees_to_booking_handler(callback: types.CallbackQuery, state: FSMContext):
        await state.set_state(CreateMeeting.attendees)
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад к подтверждению", callback_data="back_to_booking_confirm")
        builder.adjust(1)
        await callback.message.edit_text("Введите участников через запятую (ФИО) или '-' если никого не добавлять:", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data == "back_to_booking_confirm")
    async def back_to_booking_confirm_handler(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        room = data.get("book_room")
        start_dt = tz.localize(datetime.combine(data["date"], data["time"]))
        end_dt = start_dt + timedelta(minutes=int(data["duration"]))

        text = (
            f"📌 **Тема:** {data['subject']}\n"
            f"📅 **Дата:** {start_dt.strftime('%d.%m.%Y')}\n"
            f"⏰ **Время:** {start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}\n"
            f"🏢 **Комната:** {room}\n"
        )
        if data.get("attendees"):
            text += f"👥 **Участники:** {', '.join(data['attendees'])}\n"
        text += "\nПодтвердить создание?"

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Да", callback_data="confirm_booking_yes")
        kb.button(text="❌ Нет", callback_data="confirm_no")
        kb.adjust(2)

        await state.set_state(CreateMeeting.confirm)
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")
        await callback.answer()

    # ========== Переговорки ==========
    @dp.message(F.text == "🏢 Переговорки")
    async def rooms_menu(message: types.Message, state: FSMContext):
        if await state.get_state() is not None:
            await state.clear()
            await message.answer("🔄 Предыдущее действие отменено. Начинаю работу с переговорками.")
        if not await _ensure_registered(message, state):
            return
        builder = InlineKeyboardBuilder()
        builder.button(text="Москва", callback_data="room_msk_info")
        builder.button(text="СПб", callback_data="room_spb_info")
        builder.adjust(2)
        await message.answer("🏢 Выберите переговорную:", reply_markup=builder.as_markup())

    @dp.callback_query(F.data.endswith("_info"))
    async def room_info(callback: types.CallbackQuery, state: FSMContext):
        room = "Москва" if callback.data == "room_msk_info" else "СПб"
        await state.update_data(current_room=room)

        api = _get_user_calendar_api(callback.from_user.id)
        today = get_current_datetime_msk().date()
        busy, free = api.get_room_freebusy_periods(room, today)

        text = f"🏢 **{room}**\n\n"
        if free:
            text += "🟢 **Свободно сегодня:**\n"
            for start, end in free:
                text += f"   {start.strftime('%H:%M')}–{end.strftime('%H:%M')}\n"
        else:
            text += "🟢 Сегодня свободных слотов нет.\n"

        if busy:
            text += "\n🔴 **Занято:**\n"
            for start, end in busy:
                text += f"   {start.strftime('%H:%M')}–{end.strftime('%H:%M')}\n"

        timeline = format_time_line(free, busy)
        if timeline:
            text += f"\n📊 **Таймлайн:**\n{timeline}\n"

        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Забронировать", callback_data=f"room_book_{room}")
        builder.button(text="📅 Другой день", callback_data=f"room_other_day_{room}")
        builder.button(text="🔙 К комнатам", callback_data="room_back_to_rooms")
        builder.adjust(1)

        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()

    @dp.callback_query(F.data == "room_back_to_rooms")
    async def room_back_to_rooms(callback: types.CallbackQuery, state: FSMContext):
        builder = InlineKeyboardBuilder()
        builder.button(text="🏢 Москва", callback_data="room_msk_info")
        builder.button(text="🏢 СПб", callback_data="room_spb_info")
        builder.adjust(2)
        await callback.message.edit_text("🏢 Выберите переговорную:", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("room_other_day_"))
    async def room_other_day(callback: types.CallbackQuery, state: FSMContext):
        room = callback.data.replace("room_other_day_", "")
        await state.update_data(current_room=room)
        await state.set_state(RoomScheduleStates.waiting_for_date)
        await callback.message.edit_text("Напишите интересующий день (например: завтра, 15 марта).")
        await callback.answer()

    @dp.callback_query(F.data.startswith("room_book_"))
    async def room_book(callback: types.CallbackQuery, state: FSMContext):
        room = callback.data.replace("room_book_", "")
        await state.update_data(book_room=room)
        await state.set_state(BookingStates.waiting_for_subject)
        await callback.message.edit_text(f"📝 Введите тему встречи для брони {room}:")
        await callback.answer()

    @dp.message(RoomScheduleStates.waiting_for_date)
    async def room_show_date(message: types.Message, state: FSMContext):
        data = await state.get_data()
        room = data.get("current_room")
        if not room:
            await state.clear()
            await message.answer("Что-то пошло не так. Начните заново.", reply_markup=main_menu())
            return

        d = parse_natural_date(message.text or "")
        if not d:
            await message.answer(
                "🤔 Я не распознал дату.\n\n"
                "Попробуйте: завтра, 15 марта, следующий четверг\n"
                "Или нажмите «❌ Отмена».",
                reply_markup=back_keyboard()
            )
            return

        api = _get_user_calendar_api(message.from_user.id)
        busy, free = api.get_room_freebusy_periods(room, d)

        text = f"🏢 **{room}** – {d.strftime('%d.%m.%Y')}\n\n"
        if free:
            text += "🟢 **Свободно:**\n"
            for start, end in free:
                text += f"   {start.strftime('%H:%M')}–{end.strftime('%H:%M')}\n"
        else:
            text += "🟢 Свободных слотов нет.\n"

        if busy:
            text += "\n🔴 **Занято:**\n"
            for start, end in busy:
                text += f"   {start.strftime('%H:%M')}–{end.strftime('%H:%M')}\n"

        timeline = format_time_line(free, busy)
        if timeline:
            text += f"\n📊 **Таймлайн:**\n{timeline}"

        await message.answer(text, parse_mode="Markdown")
        await state.clear()

    @dp.message(BookingStates.waiting_for_subject)
    async def booking_subject(message: types.Message, state: FSMContext):
        subject = message.text.strip()
        if not subject:
            await message.answer("Тема не может быть пустой. Введите тему:")
            return
        await state.update_data(subject=subject)
        await state.set_state(CreateMeeting.date)
        await message.answer("📅 Когда забронировать? (например: завтра, 15 марта)", reply_markup=back_keyboard())

    # ========== Изменение встреч ==========
    @dp.message(F.text == "✏️ Изменить встречи")
    async def modify_meetings(message: types.Message, state: FSMContext):
        if await state.get_state() is not None:
            await state.clear()
            await message.answer("🔄 Предыдущее действие отменено. Начинаю редактирование встреч.")
        if not await _ensure_registered(message, state):
            return
        await state.set_state(RescheduleMeeting.choosing_event)
        await state.update_data(page=0, events_list=[])
        await show_events_page(message, state, 0)

    async def show_events_page(message: types.Message, state: FSMContext, page: int):
        api = _get_user_calendar_api(message.from_user.id)
        events = api.get_upcoming_events(days=7)
        if not events:
            await message.answer("Нет предстоящих встреч.", reply_markup=main_menu())
            await state.clear()
            return

        page_size = 5
        total_pages = (len(events) + page_size - 1) // page_size
        start = page * page_size
        end = start + page_size
        page_events = events[start:end]

        if not page_events and page > 0:
            page = total_pages - 1
            start = page * page_size
            end = start + page_size
            page_events = events[start:end]

        builder = InlineKeyboardBuilder()
        events_map = []
        for idx, ev in enumerate(page_events):
            events_map.append({"event_id": str(ev.id), "subject": ev.subject, "start": ev.start})
            start_msk = ev.start.astimezone(tz).strftime("%d.%m %H:%M")
            subj = (ev.subject or "Без темы")[:25]
            builder.button(text=f"{start_msk} – {subj}", callback_data=f"modify_event_{idx}")
        builder.adjust(1)

        nav_buttons = []
        if page > 0:
            nav_buttons.append(types.InlineKeyboardButton(text="◀️ Назад", callback_data=f"events_page_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(types.InlineKeyboardButton(text="▶️ Вперед", callback_data=f"events_page_{page+1}"))
        if nav_buttons:
            builder.row(*nav_buttons)
        builder.row(types.InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_modify"))

        await state.update_data(events_list=events, page=page, total_pages=total_pages, events_map=events_map)
        await message.answer(f"📅 Выберите встречу (страница {page+1} из {total_pages}):", reply_markup=builder.as_markup())

    @dp.callback_query(RescheduleMeeting.choosing_event, F.data.startswith("modify_event_"))
    async def choose_event(callback: types.CallbackQuery, state: FSMContext):
        idx = int(callback.data.split("_")[-1])
        data = await state.get_data()
        events = data.get("events_list", [])
        page = data.get("page", 0)
        page_size = 5
        start = page * page_size
        actual_idx = start + idx
        if actual_idx >= len(events):
            await callback.answer("Ошибка выбора", show_alert=True)
            return
        event = events[actual_idx]
        await state.update_data(event_id=event.id, event_subject=event.subject)
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 Удалить", callback_data="act_delete")
        kb.button(text="🔁 Перенести", callback_data="act_reschedule")
        kb.adjust(2)
        await callback.message.edit_text(f"Что сделать с встречей «{event.subject or 'Без темы'}»?", reply_markup=kb.as_markup())
        await callback.answer()

    @dp.callback_query(F.data == "act_delete")
    async def do_delete(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        api = _get_user_calendar_api(callback.from_user.id)
        ok = api.cancel_event(data.get("event_id", ""))
        await callback.message.edit_text("✅ Встреча удалена." if ok else "❌ Не удалось удалить встречу.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "act_reschedule")
    async def start_reschedule(callback: types.CallbackQuery, state: FSMContext):
        await state.set_state(RescheduleMeeting.new_date)
        await callback.message.edit_text("На какую дату перенести? (например: завтра, 20 марта)")
        await callback.answer()

    @dp.message(RescheduleMeeting.new_date)
    async def resched_date(message: types.Message, state: FSMContext):
        d = parse_natural_date(message.text or "")
        if not d:
            await message.answer("🤔 Не могу распознать дату. Попробуйте: завтра, 15 марта", reply_markup=back_keyboard())
            return
        await state.update_data(new_date=d)
        await state.set_state(RescheduleMeeting.new_time)
        await message.answer("Во сколько начало? (ЧЧ:ММ)", reply_markup=back_keyboard())

    @dp.message(RescheduleMeeting.new_time)
    async def resched_time(message: types.Message, state: FSMContext):
        try:
            t = datetime.strptime((message.text or "").strip(), "%H:%M").time()
        except Exception:
            await message.answer("Неверный формат времени. Например 14:30.", reply_markup=back_keyboard())
            return
        await state.update_data(new_time=t)
        await state.set_state(RescheduleMeeting.new_duration)
        await message.answer("Новая длительность в минутах? (по умолчанию 60)", reply_markup=back_keyboard())

    @dp.message(RescheduleMeeting.new_duration)
    async def resched_duration(message: types.Message, state: FSMContext):
        txt = (message.text or "").strip()
        try:
            duration = int(txt) if txt else 60
        except Exception:
            duration = 60
        await state.update_data(new_duration=duration)
        data = await state.get_data()
        start_dt = tz.localize(datetime.combine(data["new_date"], data["new_time"]))
        end_dt = start_dt + timedelta(minutes=duration)

        # Проверка доступности участников (упрощённо)
        api = _get_user_calendar_api(message.from_user.id)
        event = api.account.calendar.get(id=data["event_id"])
        attendees = [att.mailbox.email_address for att in (event.required_attendees or [])]
        # Здесь можно добавить проверку занятости, но для простоты оставим

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Да", callback_data="resched_yes")
        kb.button(text="❌ Нет", callback_data="resched_no")
        kb.adjust(2)
        await state.set_state(RescheduleMeeting.confirming)
        await message.answer(
            f"Перенести встречу на:\n📅 {start_dt.strftime('%d.%m.%Y')}\n⏰ {start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}\n\nПодтвердить?",
            reply_markup=kb.as_markup(),
        )

    @dp.callback_query(RescheduleMeeting.confirming, F.data == "resched_yes")
    async def resched_yes(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        api = _get_user_calendar_api(callback.from_user.id)
        start_dt = tz.localize(datetime.combine(data["new_date"], data["new_time"]))
        end_dt = start_dt + timedelta(minutes=int(data["new_duration"]))
        ok, msg = api.reschedule_event(data["event_id"], start_dt, end_dt)
        await callback.message.edit_text(msg)
        await state.clear()
        await callback.answer()

    @dp.callback_query(RescheduleMeeting.confirming, F.data == "resched_no")
    async def resched_no(callback: types.CallbackQuery, state: FSMContext):
        await callback.message.edit_text("Перенос отменён.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data.startswith("events_page_"))
    async def events_page(callback: types.CallbackQuery, state: FSMContext):
        page = int(callback.data.split("_")[-1])
        await state.update_data(page=page)
        await show_events_page(callback.message, state, page)
        await callback.answer()

    @dp.callback_query(F.data == "cancel_modify")
    async def cancel_modify(callback: types.CallbackQuery, state: FSMContext):
        await callback.message.edit_text("Отмена.")
        await state.clear()
        await callback.answer()

    # ========== Мой профиль ==========
    @dp.message(F.text == "👤 Мой профиль")
    async def my_profile(message: types.Message, state: FSMContext):
        if await state.get_state() is not None:
            await state.clear()
            await message.answer("🔄 Предыдущее действие отменено. Показываю профиль.")
        if not await _ensure_registered(message, state):
            return
        user = get_user(message.from_user.id)
        if not user:
            await message.answer("❌ Вы не зарегистрированы.", reply_markup=main_menu())
            return
        text = (
            "👤 **Ваш профиль**\n\n"
            f"📝 **ФИО:** {user.fio}\n"
            f"📧 **Email:** {user.email}\n"
            f"🔑 **Логин Exchange:** {user.ews_username}\n"
            f"📱 **Телефон:** {user.phone_e164}\n"
            f"✅ Статус: зарегистрирован"
        )
        await message.answer(text, parse_mode="Markdown", reply_markup=main_menu())

    # ========== Fallback ==========
    @dp.message()
    async def fallback(message: types.Message, state: FSMContext):
        # Игнорируем, если пользователь в активном диалоге
        current_state = await state.get_state()
        if current_state is not None:
            return
        if not await _ensure_registered(message, state):
            return
        text = (message.text or "").lower()
        if any(w in text for w in ["расписание", "встречи", "что у меня"]):
            api = _get_user_calendar_api(message.from_user.id)
            d = parse_natural_date(text) or get_current_datetime_msk().date()
            events = api.get_my_events(d)
            current_dt = get_current_datetime_msk()
            if d == current_dt.date():
                events = filter_future_events(events, current_dt)
                date_desc = f"сегодня (после {current_dt.strftime('%H:%M')})"
            else:
                date_desc = d.strftime("%d.%m.%Y")
            if events:
                msg = f"📅 **{date_desc}**\n\n" + "\n".join([format_event(e) for e in events])
            else:
                msg = f"📭 На {date_desc} встреч нет."
            await message.answer(msg, parse_mode="Markdown")
            return
        await message.answer("Не совсем понял. Используйте меню.", reply_markup=main_menu())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())