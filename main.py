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
from bot_utils import parse_natural_date, format_event, format_room_event, filter_future_events, get_current_datetime_msk


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
    change_attendee = State()  # Состояние для смены участника


class ScheduleStates(StatesGroup):
    waiting_for_date = State()


class RescheduleMeeting(StatesGroup):
    choosing_event = State()
    new_date = State()
    new_time = State()
    new_duration = State()
    confirming = State()


class RoomScheduleStates(StatesGroup):
    waiting_for_date = State()


class RoomAction(StatesGroup):
    """Состояния для меню переговорок: выбор действия (расписание/бронь)"""
    pass


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


def main_menu() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📅 Расписание"))
    builder.add(KeyboardButton(text="➕ Создать встречу"))
    builder.add(KeyboardButton(text="🏢 Переговорки"))
    builder.add(KeyboardButton(text="🗑 Удалить/перенести"))
    builder.add(KeyboardButton(text="❓ Помощь"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


def schedule_submenu() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="Сегодня"))
    builder.add(KeyboardButton(text="Выбрать день"))
    builder.add(KeyboardButton(text="Назад"))
    builder.adjust(3)
    return builder.as_markup(resize_keyboard=True)


def registration_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📱 Подтвердить номер телефона", request_contact=True))
    builder.add(KeyboardButton(text="Отмена"))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


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
        "🔐 Для работы нужно зарегистрироваться.\n\n"
        "1) Подтвердите номер телефона (кнопка ниже)\n"
        "2) Затем введите логин (например: a.leonov)\n"
        "3) Затем пароль от Outlook/Exchange\n\n"
        "Я сверю телефон по списку сотрудников.",
        reply_markup=registration_keyboard(),
    )
    return False


def _resolve_attendees_to_emails(raw: str) -> Tuple[List[str], List[str]]:
    """
    Возвращает (emails, warnings).
    Ввод: 'Иванов Иван, Петров Петр'
    """
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
            # fallback: наивная генерация (может быть неверной)
            bits = q.lower().split()
            if len(bits) >= 2:
                emails.append(f"{bits[0]}.{bits[1]}@promotion-lc.ru")
            else:
                emails.append(f"{q.lower().replace(' ', '.')}@promotion-lc.ru")
            warnings.append(f"⚠️ Не нашёл '{q}' в базе. Использую e-mail по шаблону.")
    return emails, warnings


async def startup() -> None:
    global CTX
    init_db()
    employees_by_phone = load_employees()
    fio_index = build_fio_index(employees_by_phone)
    CTX = AppContext(
        employees_by_phone=employees_by_phone,
        fio_index=fio_index,
        ews_cfg=EwsConfig(ews_url=EWS_URL, rooms=ROOMS),
    )
    logger.info("Loaded employees: %s", len(employees_by_phone))


async def main() -> None:
    await startup()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(Command("start"))
    async def cmd_start(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        await state.clear()
        await message.answer(
            "👋 Привет! Я персональный ассистент календаря.\n"
            "Работаю с Outlook/Exchange через EWS.\n\n"
            "Выберите действие в меню.",
            reply_markup=main_menu(),
        )

    @dp.message(F.text == "Отмена")
    async def cancel_any(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer("Ок. Главное меню:", reply_markup=main_menu())

    # ===== Регистрация =====
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
            f"✅ Телефон подтверждён: {emp.fio}\n\nВведите логин Exchange (например: `a.leonov`):",
            parse_mode="Markdown",
            reply_markup=types.ReplyKeyboardRemove(),
        )

    @dp.message(RegisterStates.waiting_for_ews_username)
    async def reg_username(message: types.Message, state: FSMContext):
        username = (message.text or "").strip()
        if not username:
            await message.answer("Введите логин (например: `a.leonov`).", parse_mode="Markdown")
            return
        await state.update_data(ews_username=username)
        await state.set_state(RegisterStates.waiting_for_ews_password)
        await message.answer("Введите пароль Exchange/Outlook (сообщение не скрывается в Telegram).")

    @dp.message(RegisterStates.waiting_for_ews_password)
    async def reg_password(message: types.Message, state: FSMContext):
        password = (message.text or "").strip()
        if not password:
            await message.answer("Пароль пустой. Введите пароль ещё раз.")
            return
        data = await state.get_data()
        phone_e164 = data["phone_e164"]
        fio = data["fio"]
        email = data["email"]
        username = data["ews_username"]

        # Проверяем креды: пробуем создать API и получить события на сегодня (лёгкий запрос)
        assert CTX is not None
        try:
            api = CalendarAPI(email=email, username=username, password=password, cfg=CTX.ews_cfg)
            _ = api.get_my_events(get_current_datetime_msk().date())
        except Exception as e:
            await message.answer(f"❌ Не удалось войти в Exchange: {e}\nПопробуйте снова: введите логин.")
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
        await message.answer("✅ Регистрация завершена. Открываю меню.", reply_markup=main_menu())

    # ===== Расписание =====
    @dp.message(F.text == "📅 Расписание")
    async def schedule_menu(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        await state.clear()
        await message.answer("Выберите действие:", reply_markup=schedule_submenu())

    @dp.message(ScheduleStates.waiting_for_date, F.text != "Назад")
    async def show_today(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        api = _get_user_calendar_api(message.from_user.id)
        current_dt = get_current_datetime_msk()
        today = current_dt.date()
        events = api.get_my_events(today)
        events = filter_future_events(events, current_dt)
        if events:
            text = f"📅 **Сегодня, {today.strftime('%d.%m.%Y')}** (предстоящие)\n\n" + "\n".join([format_event(e) for e in events])
        else:
            text = "📭 На сегодня предстоящих встреч нет."
        await message.answer(text, parse_mode="Markdown", reply_markup=schedule_submenu())

    @dp.message(ScheduleStates.waiting_for_date, F.text == "Выбрать день")
    async def ask_day(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        await state.set_state(ScheduleStates.waiting_for_date)
        await message.answer("Напишите интересующий день (например: завтра, 15 марта, следующий четверг).")

    @dp.message(F.text == "Назад")
    async def back_to_main(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer("Главное меню:", reply_markup=main_menu())

    @dp.message(ScheduleStates.waiting_for_date)
    async def show_day(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        d = parse_natural_date((message.text or "").strip().lower())
        if not d:
            await message.answer("Не удалось распознать дату. Попробуйте ещё раз или нажмите «Назад».")
            return
        api = _get_user_calendar_api(message.from_user.id)
        events = api.get_my_events(d)
        current_dt = get_current_datetime_msk()
        if d == current_dt.date():
            events = filter_future_events(events, current_dt)
            date_desc = "сегодня (предстоящие)"
        else:
            date_desc = d.strftime("%d.%m.%Y")
        if events:
            text = f"📅 **{date_desc}**\n\n" + "\n".join([format_event(e) for e in events])
        else:
            text = f"📭 На {date_desc} встреч нет."
        await state.clear()
        await message.answer(text, parse_mode="Markdown", reply_markup=schedule_submenu())

    # ===== Создание встречи =====
    @dp.message(F.text == "➕ Создать встречу")
    async def create_start(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        await state.set_state(CreateMeeting.subject)
        await message.answer("Введите тему встречи:", reply_markup=types.ReplyKeyboardRemove())

    @dp.message(CreateMeeting.subject)
    async def create_subject(message: types.Message, state: FSMContext):
        data = await state.get_data()
        # Если это бронирование из меню переговорок - тема уже установлена, сразу запрашиваем дату
        if data.get("book_room"):
            d = parse_natural_date((message.text or "").strip().lower())
            if not d:
                await message.answer("Не могу распознать дату. Попробуйте ещё раз (например: завтра, 15 марта).")
                return
            await state.update_data(date=d)
            await state.set_state(CreateMeeting.time)
            await message.answer("Во сколько начало? (например: 14:00, формат ЧЧ:ММ)")
            return
        
        # Обычный поток создания встречи
        await state.update_data(subject=(message.text or "").strip())
        await state.set_state(CreateMeeting.date)
        await message.answer("Когда встреча? (например: завтра, 15 марта)")

    @dp.message(CreateMeeting.date)
    async def create_date(message: types.Message, state: FSMContext):
        data = await state.get_data()
        # Если это бронирование из меню переговорок - дата еще не установлена, запрашиваем её
        if data.get("book_room"):
            d = parse_natural_date((message.text or "").strip().lower())
            if not d:
                await message.answer("Не могу распознать дату. Попробуйте ещё раз (например: завтра, 15 марта).")
                return
            await state.update_data(date=d)
            await state.set_state(CreateMeeting.time)
            await message.answer("Во сколько начало? (например: 14:00, формат ЧЧ:ММ)")
            return
            
        d = parse_natural_date(message.text or "")
        if not d:
            await message.answer("Не могу распознать дату. Попробуйте ещё раз.")
            return
        await state.update_data(date=d)
        await state.set_state(CreateMeeting.time)
        await message.answer("Во сколько начало? (например: 14:00, формат ЧЧ:ММ)")

    @dp.message(CreateMeeting.time)
    async def create_time(message: types.Message, state: FSMContext):
        try:
            t = datetime.strptime((message.text or "").strip(), "%H:%M").time()
        except Exception:
            await message.answer("Неверный формат времени. Используйте ЧЧ:ММ, например 14:30.")
            return
        await state.update_data(time=t)
        await state.set_state(CreateMeeting.duration)
        await message.answer("Длительность в минутах? (по умолчанию 60)")

    @dp.message(CreateMeeting.duration)
    async def create_duration(message: types.Message, state: FSMContext):
        data = await state.get_data()
        txt = (message.text or "").strip()
        try:
            duration = int(txt) if txt else 60
        except Exception:
            duration = 60
        await state.update_data(duration=duration)
        
        # Если это бронирование из меню переговорок - сразу переходим к подтверждению
        if data.get("book_room"):
            # Комната уже установлена в book_room
            room = data.get("book_room")
            
            # Проверяем доступность комнаты
            start_dt = tz.localize(datetime.combine(data["date"], data["time"]))
            end_dt = start_dt + timedelta(minutes=int(duration))
            
            api = _get_user_calendar_api(message.from_user.id)
            available, msg = api.is_room_available(room, start_dt, end_dt)
            if not available:
                await message.answer(f"⚠️ {msg}\nПопробуйте другое время или создайте без комнаты.")
                await state.clear()
                return
            
            # Участники не требуются для простого бронирования (пока пустой список)
            await state.update_data(attendees=[], room=room)
            
            # Формируем текст подтверждения
            text = (
                f"📌 **Тема:** {data['subject']}\n"
                f"📅 **Дата:** {start_dt.strftime('%d.%m.%Y')}\n"
                f"⏰ **Время:** {start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}\n"
                f"🏢 **Комната:** {room}\n"
                f"\nПодтвердить создание?"
            )
            
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Да", callback_data="confirm_yes")
            kb.button(text="❌ Нет", callback_data="confirm_no")
            kb.button(text="👥 Добавить участников", callback_data="add_attendees_to_booking")
            kb.adjust(2)
            
            await state.set_state(CreateMeeting.confirm)
            await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")
            return
        
        # Обычный поток создания встречи
        await state.set_state(CreateMeeting.attendees)
        await message.answer("Укажите участников через запятую (ФИО) или '-' если никого не добавлять.")

    @dp.message(CreateMeeting.attendees)
    async def create_attendees(message: types.Message, state: FSMContext):
        data = await state.get_data()
        
        # Если это бронирование переговорки - после ввода участников сразу показываем подтверждение
        if data.get("book_room"):
            emails, warnings = _resolve_attendees_to_emails(message.text or "")
            await state.update_data(attendees=emails)
            
            room = data.get("book_room")
            start_dt = tz.localize(datetime.combine(data["date"], data["time"]))
            end_dt = start_dt + timedelta(minutes=int(data["duration"]))
            
            text = (
                f"📌 **Тема:** {data['subject']}\n"
                f"📅 **Дата:** {start_dt.strftime('%d.%m.%Y')}\n"
                f"⏰ **Время:** {start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}\n"
                f"🏢 **Комната:** {room}\n"
            )
            if emails:
                text += f"👥 **Участники:** {', '.join(emails)}\n"
            text += "\nПодтвердить создание?"
            
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Да", callback_data="confirm_yes_booking")
            kb.button(text="❌ Нет", callback_data="confirm_no")
            kb.adjust(2)
            
            await state.set_state(CreateMeeting.confirm)
            await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")
            return
        
        # Обычный поток создания встречи
        emails, warnings = _resolve_attendees_to_emails(message.text or "")
        await state.update_data(attendees=emails)

        builder = InlineKeyboardBuilder()
        builder.button(text="Москва", callback_data="room_msk")
        builder.button(text="СПб", callback_data="room_spb")
        builder.button(text="Без комнаты", callback_data="room_none")
        builder.adjust(2)

        note = "\n".join(warnings)
        if note:
            await message.answer(note)

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
        await callback.message.edit_text(msg, reply_markup=main_menu())
        await state.clear()
        await callback.answer()

    @dp.callback_query(CreateMeeting.confirm, F.data == "confirm_no")
    async def confirm_no(callback: types.CallbackQuery, state: FSMContext):
        await callback.message.edit_text("Создание встречи отменено.", reply_markup=main_menu())
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "change_attendee")
    async def change_attendee_handler(callback: types.CallbackQuery, state: FSMContext):
        """Обработчик кнопки 'Сменить участника' - возвращает к вводу участников"""
        await state.set_state(CreateMeeting.attendees)
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад", callback_data="back_to_room")
        builder.adjust(1)
        await callback.message.edit_text("Введите участников через запятую (ФИО) или '-' если никого не добавлять:", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data == "back_to_room")
    async def back_to_room_handler(callback: types.CallbackQuery, state: FSMContext):
        """Возврат к выбору комнаты после ввода участников"""
        data = await state.get_data()
        builder = InlineKeyboardBuilder()
        builder.button(text="Москва", callback_data="room_msk")
        builder.button(text="СПб", callback_data="room_spb")
        builder.button(text="Без комнаты", callback_data="room_none")
        builder.adjust(2)
        await callback.message.edit_text("Нужна переговорка?", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data == "add_attendees_to_booking")
    async def add_attendees_to_booking_handler(callback: types.CallbackQuery, state: FSMContext):
        """Обработчик кнопки 'Добавить участников' при бронировании переговорки"""
        await state.set_state(CreateMeeting.attendees)
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад к подтверждению", callback_data="back_to_booking_confirm")
        builder.adjust(1)
        await callback.message.edit_text("Введите участников через запятую (ФИО) или '-' если никого не добавлять:", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data == "back_to_booking_confirm")
    async def back_to_booking_confirm_handler(callback: types.CallbackQuery, state: FSMContext):
        """Возврат к подтверждению бронирования после ввода участников"""
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
        kb.button(text="✅ Да", callback_data="confirm_yes_booking")
        kb.button(text="❌ Нет", callback_data="confirm_no")
        kb.adjust(2)
        
        await state.set_state(CreateMeeting.confirm)
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")
        await callback.answer()

    # ===== Переговорки =====
    @dp.message(F.text == "🏢 Переговорки")
    async def rooms_menu(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        builder = InlineKeyboardBuilder()
        builder.button(text="Москва", callback_data="info_msk")
        builder.button(text="СПб", callback_data="info_spb")
        builder.adjust(2)
        await message.answer("Выбери переговорку:", reply_markup=builder.as_markup())

    @dp.callback_query(F.data.startswith("info_"))
    async def room_info(callback: types.CallbackQuery, state: FSMContext):
        # Проверяем регистрацию по ID пользователя
        if not is_registered(callback.from_user.id):
            await callback.message.answer(
                "🔐 Для работы нужно зарегистрироваться.\n\n"
                "1) Подтвердите номер телефона (кнопка ниже)\n"
                "2) Затем введите логин (например: a.leonov)\n"
                "3) Затем пароль от Outlook/Exchange\n\n"
                "Я сверю телефон по списку сотрудников.",
                reply_markup=registration_keyboard(),
            )
            await state.set_state(RegisterStates.waiting_for_contact)
            await callback.answer()
            return

        room = "Москва" if callback.data == "info_msk" else "СПб"
        await state.update_data(current_room=room)

        # Меню выбора действия: Забронировать / Узнать расписание
        builder = InlineKeyboardBuilder()
        builder.button(text="📅 Узнать расписание", callback_data="room_schedule")
        builder.button(text="➕ Забронировать", callback_data="room_book")
        builder.button(text="🔙 Назад в меню", callback_data="room_back_main")
        builder.adjust(1)

        await callback.message.edit_text(
            f"🏢 **{room}**\n\nВыберите действие:",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()

    @dp.callback_query(F.data == "room_back_main")
    async def room_back_to_main(callback: types.CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.message.edit_text("Главное меню:", reply_markup=main_menu())
        await callback.answer()

    @dp.callback_query(F.data == "room_schedule")
    async def room_schedule_menu(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        room = data.get("current_room")
        if not room:
            await callback.message.answer("Сначала выберите переговорку.")
            await callback.answer()
            return

        builder = InlineKeyboardBuilder()
        builder.button(text="Сегодня", callback_data="room_today_btn")
        builder.button(text="Выбрать день", callback_data="room_choose_day_btn")
        builder.button(text="🔙 Назад в меню", callback_data="room_back_main")
        builder.adjust(1)

        await callback.message.edit_text(
            f"🏢 **{room}** — Расписание\n\nВыберите действие:",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()

    @dp.callback_query(F.data == "room_today_btn")
    async def room_today_callback(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        room = data.get("current_room")
        if not room:
            await callback.message.answer("Сначала выберите переговорку.")
            await callback.answer()
            return

        if not is_registered(callback.from_user.id):
            await callback.message.answer(
                "🔐 Для работы нужно зарегистрироваться.",
                reply_markup=registration_keyboard(),
            )
            await callback.answer()
            return

        api = _get_user_calendar_api(callback.from_user.id)
        current_dt = get_current_datetime_msk()
        today = current_dt.date()

        events = api.get_room_events(room, today)
        if events:
            future_events = [ev for ev in events if ev.end > current_dt]
            text = f"🏢 **{room}** – сегодня, {today.strftime('%d.%m.%Y')} (предстоящие)\n\n"
            if future_events:
                text += "\n".join([format_room_event(e) for e in future_events])
            else:
                text += "📭 На сегодня предстоящих бронирований нет."
        else:
            text = f"🏢 **{room}** – сегодня, {today.strftime('%d.%m.%Y')}\n\n✅ Свободна."

        # Возвращаем меню расписания
        builder = InlineKeyboardBuilder()
        builder.button(text="Сегодня", callback_data="room_today_btn")
        builder.button(text="Выбрать день", callback_data="room_choose_day_btn")
        builder.button(text="🔙 Назад в меню", callback_data="room_back_main")
        builder.adjust(1)
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()

    @dp.callback_query(F.data == "room_choose_day_btn")
    async def room_choose_day_callback(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not data.get("current_room"):
            await callback.message.answer("Сначала выберите переговорку через меню.")
            await callback.answer()
            return

        if not is_registered(callback.from_user.id):
            await callback.message.answer(
                "🔐 Для работы нужно зарегистрироваться.",
                reply_markup=registration_keyboard(),
            )
            await callback.answer()
            return

        # Запрашиваем ввод даты через модальное окно или сообщение
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад в меню", callback_data="room_back_main")
        builder.adjust(1)
        
        await callback.message.edit_text(
            "Напишите интересующий день (например: завтра, 15 марта, следующий четверг).\n\nИли нажмите «Назад».",
            reply_markup=builder.as_markup()
        )
        await state.set_state(RoomScheduleStates.waiting_for_date)
        await callback.answer()

    @dp.message(RoomScheduleStates.waiting_for_date)
    async def room_show_date_message(message: types.Message, state: FSMContext):
        if not is_registered(message.from_user.id):
            await message.answer(
                "🔐 Для работы нужно зарегистрироваться.",
                reply_markup=registration_keyboard(),
            )
            await state.clear()
            return

        data = await state.get_data()
        room = data.get("current_room")
        if not room:
            await state.clear()
            await message.answer("Что-то пошло не так. Начните заново.", reply_markup=main_menu())
            return

        d = parse_natural_date((message.text or "").strip().lower())
        if not d:
            await message.answer("Не удалось распознать дату. Попробуйте ещё раз или выберите переговорку заново.", reply_markup=main_menu())
            await state.clear()
            return

        api = _get_user_calendar_api(message.from_user.id)
        events = api.get_room_events(room, d)

        if events:
            text = f"🏢 **{room}** – {d.strftime('%d.%m.%Y')}\n\n"
            text += "\n".join([format_room_event(e) for e in events])
        else:
            text = f"🏢 **{room}** – {d.strftime('%d.%m.%Y')}\n\n✅ Свободна."

        await state.clear()
        await message.answer(text, parse_mode="Markdown", reply_markup=main_menu())

    @dp.callback_query(F.data == "room_book")
    async def room_booking_start(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        room = data.get("current_room")
        if not room:
            await callback.message.answer("Сначала выберите переговорку.")
            await callback.answer()
            return

        # Устанавливаем тему по умолчанию для быстрого бронирования
        await state.update_data(book_room=room, subject=f"Бронь {room}")
        await state.set_state(CreateMeeting.date)
        await callback.message.edit_text(f"📝 Когда забронировать {room}? (например: завтра, 15 марта)")
        await callback.answer()

    @dp.callback_query(F.data.startswith("confirm_yes"))
    async def confirm_create_from_booking(callback: types.CallbackQuery, state: FSMContext):
        """Обработчик подтверждения для бронирования переговорки"""
        data = await state.get_data()
        start_dt = tz.localize(datetime.combine(data["date"], data["time"]))
        end_dt = start_dt + timedelta(minutes=int(data["duration"]))

        api = _get_user_calendar_api(callback.from_user.id)
        
        # Добавляем комнату как участника и как место
        attendees = data.get("attendees", [])
        room_name = data.get("book_room") or data.get("room")
        
        ok, msg, _ = api.create_event(
            subject=data["subject"],
            start=start_dt,
            end=end_dt,
            attendees_emails=attendees,
            room_name=room_name,
        )
        
        await callback.message.edit_text(msg, reply_markup=main_menu())
        await state.clear()
        await callback.answer()

    # ===== Удалить/перенести =====
    @dp.message(F.text == "🗑 Удалить/перенести")
    async def delete_or_reschedule(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        api = _get_user_calendar_api(message.from_user.id)
        events = api.get_upcoming_events(days=3)
        if not events:
            await message.answer("Нет предстоящих встреч.")
            return
        builder = InlineKeyboardBuilder()
        events_map = []
        for idx, ev in enumerate(events[:5]):
            events_map.append({"index": idx, "event_id": str(ev.id)})
            start = ev.start.strftime("%d.%m %H:%M")
            subj = (ev.subject or "Без темы")[:20]
            builder.button(text=f"{start} – {subj}", callback_data=f"event_{idx}")
        builder.adjust(1)
        await state.update_data(events_map=events_map)
        await state.set_state(RescheduleMeeting.choosing_event)
        await message.answer("Выберите встречу:", reply_markup=builder.as_markup())

    @dp.callback_query(RescheduleMeeting.choosing_event, F.data.startswith("event_"))
    async def choose_event(callback: types.CallbackQuery, state: FSMContext):
        idx_str = callback.data[len("event_") :]
        data = await state.get_data()
        events_map = data.get("events_map", [])
        event_id = None
        try:
            idx = int(idx_str)
            if 0 <= idx < len(events_map):
                event_id = events_map[idx]["event_id"]
        except Exception:
            event_id = None
        if not event_id:
            await callback.message.edit_text("Не удалось определить встречу. Повторите.")
            await state.clear()
            await callback.answer()
            return
        await state.update_data(event_id=event_id)
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 Удалить", callback_data="act_delete")
        kb.button(text="🔁 Перенести", callback_data="act_reschedule")
        kb.adjust(2)
        await callback.message.edit_text("Что сделать?", reply_markup=kb.as_markup())
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
            await message.answer("Не могу распознать дату. Попробуйте ещё раз.")
            return
        await state.update_data(new_date=d)
        await state.set_state(RescheduleMeeting.new_time)
        await message.answer("Во сколько начало? (ЧЧ:ММ)")

    @dp.message(RescheduleMeeting.new_time)
    async def resched_time(message: types.Message, state: FSMContext):
        try:
            t = datetime.strptime((message.text or "").strip(), "%H:%M").time()
        except Exception:
            await message.answer("Неверный формат времени. Например 14:30.")
            return
        await state.update_data(new_time=t)
        await state.set_state(RescheduleMeeting.new_duration)
        await message.answer("Новая длительность в минутах? (по умолчанию 60)")

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
        end_dt = start_dt + timedelta(minutes=int(duration))
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

    # ===== Помощь =====
    @dp.message(F.text == "❓ Помощь")
    async def help_message(message: types.Message, state: FSMContext):
        if not await _ensure_registered(message, state):
            return
        await message.answer(
            "Команды:\n"
            "- 📅 Расписание: сегодня/выбор дня\n"
            "- ➕ Создать встречу: пошагово\n"
            "- 🏢 Переговорки: Москва/СПб\n"
            "- 🗑 Удалить/перенести: управление встречами\n\n"
            "Регистрация: телефон должен быть в Excel + логин/пароль Exchange.",
            reply_markup=main_menu(),
        )

    # ===== Естественный язык (минимально) =====
    @dp.message()
    async def fallback(message: types.Message, state: FSMContext):
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
                date_desc = "сегодня (предстоящие)"
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

