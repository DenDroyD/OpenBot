from datetime import datetime, timedelta
from typing import List, Optional
import calendar

import dateparser
import pytz

tz = pytz.timezone("Europe/Moscow")


def get_calendar_emoji_for_date(target_date) -> str:
    """
    Возвращает эмодзи календаря. Для единообразия используем 📅.
    (В будущем можно добавить поддержку эмодзи с числами, если появятся в Unicode.)
    """
    return '📅'


def parse_natural_date(text: str):
    """Парсит дату из естественного языка."""
    text_lower = text.strip().lower()
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
    tz_local = pytz.timezone("Europe/Moscow")
    start = event.start.astimezone(tz_local).strftime("%H:%M")
    end = event.end.astimezone(tz_local).strftime("%H:%M")
    subject = event.subject or "Без темы"
    
    date_str = ""
    if include_date:
        event_date = event.start.astimezone(tz_local).date()
        calendar_emoji = get_calendar_emoji_for_date(event_date)
        date_str = f"{calendar_emoji} {event_date.strftime('%d.%m.%Y')} | "
    
    location = f" 📍 {event.location}" if getattr(event, "location", None) else ""
    organizer = ""
    if getattr(event, "organizer", None):
        name = event.organizer.name or event.organizer.email_address
        if "@" in name and len(name) > 20:
            name = name.split("@")[0] + "@..."
        organizer = f" 👤 {name}"
    
    if show_header:
        return f"{date_str}`{start}-{end}` | {subject}{location}{organizer}"
    else:
        return f"`{start}-{end}` | {subject}{location}{organizer}"


def format_room_event(event) -> str:
    tz_local = pytz.timezone("Europe/Moscow")
    start = event.start.astimezone(tz_local).strftime("%H:%M")
    end = event.end.astimezone(tz_local).strftime("%H:%M")
    subject = event.subject or "Без темы"
    organizer = ""
    if getattr(event, "organizer", None):
        name = event.organizer.name or event.organizer.email_address
        if "@" in name and len(name) > 20:
            name = name.split("@")[0] + "@..."
        organizer = f" 👤 {name}"
    return f"`{start}-{end}` | {subject}{organizer}".strip()


def format_time_line(free_periods: list, busy_periods: list, day_start: str = "09:00", day_end: str = "20:00") -> str:
    """
    Формирует текстовую визуальную временную шкалу для расписания комнаты.
    """
    if not free_periods and not busy_periods:
        return ""
    all_periods = []
    for start_dt, end_dt in free_periods:
        all_periods.append((start_dt.time(), end_dt.time(), '🟢'))
    for start_dt, end_dt in busy_periods:
        all_periods.append((start_dt.time(), end_dt.time(), '🔴'))
    all_periods.sort(key=lambda x: x[0])
    timeline_parts = []
    for start_t, end_t, status in all_periods:
        timeline_parts.append(f"{status} `{start_t.strftime('%H:%M')}-{end_t.strftime('%H:%M')}`")
    return " | ".join(timeline_parts)


def create_progress_bar(current: int, total: int, prefix: str = "", suffix: str = "") -> str:
    filled = int(10 * current / total)
    bar = "█" * filled + "░" * (10 - filled)
    percent = int(100 * current / total)
    return f"{prefix} [{bar}] {percent}% {suffix}".strip()