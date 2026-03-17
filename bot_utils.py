from datetime import datetime
from typing import List

import dateparser
import pytz

tz = pytz.timezone("Europe/Moscow")


def parse_natural_date(text: str):
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


def format_event(event) -> str:
    start = event.start.strftime("%H:%M")
    end = event.end.strftime("%H:%M")
    subject = event.subject or "Без темы"
    location = f"📍 {event.location}" if getattr(event, "location", None) else ""
    organizer = ""
    if getattr(event, "organizer", None):
        name = event.organizer.name or event.organizer.email_address
        organizer = f"👤 {name}"
    return f"• {start}-{end} | {subject}\n{location} {organizer}".strip()


def format_room_event(event) -> str:
    start = event.start.strftime("%H:%M")
    end = event.end.strftime("%H:%M")
    subject = event.subject or "Без темы"
    organizer = ""
    if getattr(event, "organizer", None):
        name = event.organizer.name or event.organizer.email_address
        organizer = f"👤 {name}"
    return f"• {start}-{end} | {subject} {organizer}".strip()

