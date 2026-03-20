from datetime import datetime, timedelta
from typing import List, Optional
import calendar

import dateparser
import pytz

tz = pytz.timezone("Europe/Moscow")


def get_calendar_emoji_for_date(target_date) -> str:
    """
    Возвращает эмодзи календаря с правильной датой.
    Поддерживаемые эмодзи: 📅 (общий), 📆 (перекидной)
    Для дней 1-31 используем соответствующие эмодзи если доступны.
    """
    day = target_date.day
    
    # Эмодзи календарей с числами (Unicode 15.0+)
    # Поддерживаются дни 1-31
    calendar_emojis = {
        1: '📅', 2: '📅', 3: '📅', 4: '📅', 5: '📅',
        6: '📅', 7: '📅', 8: '📅', 9: '📅', 10: '📅',
        11: '📅', 12: '📅', 13: '📅', 14: '📅', 15: '📅',
        16: '📅', 17: '📅', 18: '📅', 19: '📅', 20: '📅',
        21: '📅', 22: '📅', 23: '📅', 24: '📅', 25: '📅',
        26: '📅', 27: '📅', 28: '📅', 29: '📅', 30: '📅',
        31: '📅',
    }
    
    # Возвращаем общий эмодзи календаря
    # В будущем можно добавить поддержку конкретных дат через Unicode 15.0+
    return calendar_emojis.get(day, '📅')


def parse_natural_date(text: str):
    """
    Парсит дату из естественного языка.
    Для слова 'сегодня' возвращаем текущую дату независимо от TIMEZONE.
    """
    text_lower = text.strip().lower()
    
    # Обработка специальных случаев
    if text_lower == "сегодня":
        return datetime.now(tz).date()
    elif text_lower == "завтра":
        return (datetime.now(tz) + timedelta(days=1)).date()
    
    settings = {
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": "Europe/Moscow",
        "RETURN_AS_TIMEZONE_AWARE": False,
        "DATE_ORDER": "DMY",
        "PREFER_DAY_OF_MONTH": "current",
        "LANGUAGE_DETECTION_CONFIDENCE_THRESHOLD": 0.0,
    }
    dt = dateparser.parse(text, settings=settings)
    if dt:
        return dt.date()
    return None


def get_current_datetime_msk() -> datetime:
    return datetime.now(tz)


def filter_future_events(events, current_dt: datetime):
    return [ev for ev in events if ev.end > current_dt]


def format_event(event, show_header: bool = True, include_date: bool = False) -> str:
    tz = pytz.timezone("Europe/Moscow")
    start = event.start.astimezone(tz).strftime("%H:%M")
    end = event.end.astimezone(tz).strftime("%H:%M")
    subject = event.subject or "Без темы"
    
    # Форматируем дату если нужно
    date_str = ""
    if include_date:
        event_date = event.start.astimezone(tz).date()
        calendar_emoji = get_calendar_emoji_for_date(event_date)
        date_str = f"{calendar_emoji} {event_date.strftime('%d.%m.%Y')} | "
    
    location = f" 📍 {event.location}" if getattr(event, "location", None) else ""
    organizer = ""
    if getattr(event, "organizer", None):
        name = event.organizer.name or event.organizer.email_address
        # Сокращаем email для красоты
        if "@" in name and len(name) > 20:
            name = name.split("@")[0] + "@..."
        organizer = f" 👤 {name}"
    
    lines = []
    if show_header:
        lines.append(f"{date_str}`{start}-{end}` | {subject}{location}{organizer}")
    else:
        lines.append(f"`{start}-{end}` | {subject}{location}{organizer}")
    
    return "\n".join(lines).strip()


def format_room_event(event) -> str:
    tz = pytz.timezone("Europe/Moscow")
    start = event.start.astimezone(tz).strftime("%H:%M")
    end = event.end.astimezone(tz).strftime("%H:%M")
    subject = event.subject or "Без темы"
    organizer = ""
    if getattr(event, "organizer", None):
        name = event.organizer.name or event.organizer.email_address
        # Сокращаем email для красоты
        if "@" in name and len(name) > 20:
            name = name.split("@")[0] + "@..."
        organizer = f" 👤 {name}"
    return f"`{start}-{end}` | {subject}{organizer}".strip()


def format_time_line(free_periods: list, busy_periods: list, day_start: str = "09:00", day_end: str = "20:00") -> str:
    """
    Формирует текстовую визуальную временную шкалу для расписания комнаты.
    🟢 — свободно, 🔴 — занято
    """
    if not free_periods and not busy_periods:
        return ""
    
    # Собираем все периоды
    all_periods = []
    for start_dt, end_dt in free_periods:
        all_periods.append((start_dt.time(), end_dt.time(), '🟢'))
    for start_dt, end_dt in busy_periods:
        all_periods.append((start_dt.time(), end_dt.time(), '🔴'))
    
    # Сортируем по времени начала
    all_periods.sort(key=lambda x: x[0])
    
    if not all_periods:
        return ""
    
    # Формируем строку временной шкалы
    timeline_parts = []
    for start_t, end_t, status in all_periods:
        timeline_parts.append(f"{status} `{start_t.strftime('%H:%M')}-{end_t.strftime('%H:%M')}`")
    
    return " | ".join(timeline_parts)


def create_progress_bar(current: int, total: int, prefix: str = "", suffix: str = "") -> str:
    """
    Создаёт текстовый прогресс-бар.
    Пример: Шаг 2 из 6 [████░░░░░░] 33%
    """
    filled = int(10 * current / total)
    bar = "█" * filled + "░" * (10 - filled)
    percent = int(100 * current / total)
    return f"{prefix} [{bar}] {percent}% {suffix}".strip()