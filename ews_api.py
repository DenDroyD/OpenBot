from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time, date as date_cls
from typing import Dict, List, Optional, Tuple

from exchangelib import Account, Configuration, Credentials, DELEGATE, EWSTimeZone, NTLM
from exchangelib.items import CalendarItem
from exchangelib.properties import Attendee, Mailbox
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

import warnings
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter


@dataclass(frozen=True)
class EwsConfig:
    ews_url: str
    rooms: Dict[str, str]
    tz_name: str = "Europe/Moscow"


class CalendarAPI:
    def __init__(self, *, email: str, username: str, password: str, cfg: EwsConfig):
        self.email = email
        self.username = username
        self.password = password
        self.cfg = cfg

        try:
            self.TZ = EWSTimeZone(cfg.tz_name)
        except Exception:
            self.TZ = ZoneInfo(cfg.tz_name)

        credentials = Credentials(username=self.username, password=self.password)
        config = Configuration(
            service_endpoint=cfg.ews_url,
            credentials=credentials,
            auth_type=NTLM,
        )
        self.account = Account(
            primary_smtp_address=self.email,
            config=config,
            autodiscover=False,
            access_type=DELEGATE,
        )

    def _make_tz_aware(self, dt: datetime) -> datetime:
        if hasattr(self.TZ, "localize"):
            return self.TZ.localize(dt)
        return dt.replace(tzinfo=self.TZ)

    def get_my_events(self, date: date_cls) -> List[CalendarItem]:
        start = self._make_tz_aware(datetime.combine(date, time.min))
        end = self._make_tz_aware(datetime.combine(date, time.max))
        events = list(self.account.calendar.view(start, end))
        return sorted(events, key=lambda x: x.start)

    def get_room_events(self, room_name: str, date: date_cls) -> Optional[List[CalendarItem]]:
        if room_name not in self.cfg.rooms:
            return None
        room_email = self.cfg.rooms[room_name]
        try:
            room_account = Account(
                primary_smtp_address=room_email,
                config=self.account.config,
                autodiscover=False,
                access_type=DELEGATE,
            )
            start = self._make_tz_aware(datetime.combine(date, time.min))
            end = self._make_tz_aware(datetime.combine(date, time.max))
            events = list(room_account.calendar.view(start, end))
            return sorted(events, key=lambda x: x.start)
        except Exception:
            return None

    def is_room_available(self, room_name: str, start: datetime, end: datetime) -> Tuple[bool, str]:
        if room_name not in self.cfg.rooms:
            return False, f"⚠️ Комната '{room_name}' не найдена в конфигурации."
        
        events = self.get_room_events(room_name, start.date())
        if events is None:
            return False, "Не удалось проверить занятость (нет доступа к календарю)."
        
        for ev in events:
            if not (ev.end <= start or ev.start >= end):
                organizer = getattr(ev, 'organizer', None)
                organizer_text = ""
                if organizer and hasattr(organizer, 'name') and organizer.name:
                    organizer_text = f" — {organizer.name}"
                elif hasattr(ev, 'required_attendees') and ev.required_attendees:
                    # Пытаемся получить первого участника как организатора
                    for att in ev.required_attendees[:1]:
                        if hasattr(att, 'mailbox') and hasattr(att.mailbox, 'name') and att.mailbox.name:
                            organizer_text = f" — {att.mailbox.name}"
                            break
                
                return (
                    False,
                    f"Комната занята: {ev.start.strftime('%H:%M')}-{ev.end.strftime('%H:%M')}{organizer_text} ({ev.subject or 'Без темы'})",
                )
        return True, "Слот свободен."

    def create_event(
        self,
        *,
        subject: str,
        start: datetime,
        end: datetime,
        attendees_emails: Optional[List[str]] = None,
        room_name: Optional[str] = None,
        location_text: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        from exchangelib.items import CalendarItem as EwsCalendarItem
        
        attendees: List[Attendee] = []
        for email in (attendees_emails or []):
            attendees.append(Attendee(mailbox=Mailbox(email_address=email)))

        location = location_text
        if room_name and room_name in self.cfg.rooms:
            room_email = self.cfg.rooms[room_name]
            # Добавляем комнату как обязательного участника для бронирования
            attendees.append(Attendee(mailbox=Mailbox(email_address=room_email)))
            if not location:
                location = f"Переговорка {room_name}"

        event = CalendarItem(
            folder=self.account.calendar,
            account=self.account,
            subject=subject,
            start=self._make_tz_aware(start),
            end=self._make_tz_aware(end),
            required_attendees=attendees or None,
            location=location,
        )
        try:
            # Отправляем приглашения всем участникам (включая комнату)
            event.save(send_meeting_invitations=EwsCalendarItem.SEND_TO_ALL_AND_SAVE_COPY)
            return True, "✅ Встреча успешно создана! Приглашения отправлены.", event.id
        except Exception as e:
            error_msg = str(e)
            # Проверяем на конфликт времени
            if "Conflict" in error_msg or "conflict" in error_msg or "occupied" in error_msg.lower():
                return False, f"❌ Конфликт времени: комната или участник уже заняты в это время.", None
            return False, f"❌ Ошибка создания: {error_msg}", None

    def cancel_event(self, event_id: str) -> bool:
        try:
            item = self.account.calendar.get(id=event_id)
            if item:
                item.delete()
                return True
            return False
        except Exception:
            return False

    def reschedule_event(self, event_id: str, new_start: datetime, new_end: datetime) -> Tuple[bool, str]:
        try:
            item = self.account.calendar.get(id=event_id)
            if not item:
                return False, "❌ Встреча не найдена."
            item.start = self._make_tz_aware(new_start)
            item.end = self._make_tz_aware(new_end)
            item.save()
            return True, "✅ Встреча успешно перенесена."
        except Exception as e:
            return False, f"❌ Не удалось перенести: {e}"

    def get_upcoming_events(self, days: int = 7) -> List[CalendarItem]:
        now = datetime.now()
        start = self._make_tz_aware(now.replace(hour=0, minute=0, second=0, microsecond=0))
        end = start + timedelta(days=days)
        events = list(self.account.calendar.view(start, end))
        return sorted(events, key=lambda x: x.start)

