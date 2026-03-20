from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time, date as date_cls
from typing import Dict, List, Optional, Tuple
import logging

from exchangelib import Account, Configuration, Credentials, DELEGATE, EWSTimeZone, NTLM
from exchangelib.items import CalendarItem, SEND_TO_ALL_AND_SAVE_COPY, SEND_TO_NONE
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
        """
        Получает события комнаты за весь день.
        Пытается получить через прямой доступ к календарю комнаты.
        Если нет прав доступа, использует альтернативный метод через поиск встреч с комнатой.
        """
        if room_name not in self.cfg.rooms:
            return None
        room_email = self.cfg.rooms[room_name]

        # Метод 1: Прямой доступ к календарю комнаты
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
            
            logging.info(f"✅ Метод 1 сработал! Найдено {len(events)} событий в {room_email}")
            
            if events:
                return sorted(events, key=lambda x: x.start)
        except Exception as e:
            logging.warning(f"⚠️ Метод 1 не сработал: {type(e).__name__}: {e}")

        # Метод 2: Получаем ВСЕ встречи за день из ВАШЕГО календаря
        # И фильтруем по location ИЛИ по участникам
        try:
            start = self._make_tz_aware(datetime.combine(date, time.min))
            end = self._make_tz_aware(datetime.combine(date, time.max))
            all_events = list(self.account.calendar.view(start, end))

            # Фильтруем встречи по location или attendees
            room_events = []
            for ev in all_events:
                is_room_found = False
                
                # 1. Проверяем location (место проведения)
                if ev.location:
                    location_lower = ev.location.lower()
                    room_name_lower = room_name.lower()
                    room_email_lower = room_email.lower()
                    
                    # Ищем название комнаты или email в location
                    if room_name_lower in location_lower or room_email_lower in location_lower:
                        is_room_found = True
                        logging.debug(f"📍 Найдено по location: {ev.subject} в {ev.location}")
                    
                    # Для СПб: проверяем альтернативные названия
                    if not is_room_found and room_name_lower == "спб":
                        if "санкт-петербург" in location_lower or "петербург" in location_lower:
                            is_room_found = True
                            logging.debug(f"📍 Найдено СПб по location: {ev.subject} в {ev.location}")
                
                # 2. Если не нашли по location, проверяем attendees
                if not is_room_found:
                    if hasattr(ev, 'required_attendees') and ev.required_attendees:
                        for att in ev.required_attendees:
                            if hasattr(att, 'mailbox') and hasattr(att.mailbox, 'email_address'):
                                if att.mailbox.email_address.lower() == room_email.lower():
                                    is_room_found = True
                                    logging.debug(f"👥 Найдено по required_attendee: {ev.subject}")
                                    break
                    
                    if not is_room_found and hasattr(ev, 'optional_attendees') and ev.optional_attendees:
                        for att in ev.optional_attendees:
                            if hasattr(att, 'mailbox') and hasattr(att.mailbox, 'email_address'):
                                if att.mailbox.email_address.lower() == room_email.lower():
                                    is_room_found = True
                                    logging.debug(f"👥 Найдено по optional_attendee: {ev.subject}")
                                    break

                if is_room_found:
                    room_events.append(ev)

            logging.info(f"✅ Метод 2 нашел {len(room_events)} событий")
            
            if room_events:
                return sorted(room_events, key=lambda x: x.start)
        except Exception as e:
            logging.warning(f"⚠️ Метод 2 не сработал: {type(e).__name__}: {e}")

        return None

    def get_room_freebusy_periods(self, room_name: str, date: date_cls) -> Tuple[List[Tuple[datetime, datetime]], List[Tuple[datetime, datetime]]]:
        """
        Получает периоды занятости и свободы комнаты через Free/Busy API.
        Этот метод работает БЕЗ прав доступа к календарю комнаты, используя стандартный механизм Exchange.
        Возвращает кортеж: (список кортежей (start, end) занятых периодов, список кортежей (start, end) свободных периодов) в московском времени.
        """
        if room_name not in self.cfg.rooms:
            logging.warning(f"[FreeBusy] Комната '{room_name}' не найдена в конфигурации")
            return ([], [])
        
        room_email = self.cfg.rooms[room_name]
        logging.info(f"[FreeBusy] Запрос для комнаты: {room_name} ({room_email}), дата: {date}")
        
        # Формируем начало и конец дня в нужном часовом поясе
        start_dt = datetime.combine(date, time.min)
        end_dt = datetime.combine(date, time.max)
        
        day_start = self._make_tz_aware(start_dt)
        day_end = self._make_tz_aware(end_dt)
        
        logging.info(f"[FreeBusy] Интервал запроса: {day_start} - {day_end}")
        
        try:
            logging.info(f"[FreeBusy] Попытка 1: Вызов get_free_busy_info с accounts=[(email, 'Resource', False)]...")
            
            fb_views_gen = self.account.protocol.get_free_busy_info(
                accounts=[(room_email, 'Resource', False)],
                start=day_start,
                end=day_end,
            )
            fb_views = list(fb_views_gen)
            
            logging.info(f"[FreeBusy] Получено {len(fb_views)} ответов")
            
            if not fb_views:
                logging.warning(f"[FreeBusy] Пустой ответ для {room_email}")
                return ([], [(day_start, day_end)])
            
            fb_view = fb_views[0]
            busy_raw = []
            
            logging.info(f"[FreeBusy] Обработка calendar_events...")
            
            if hasattr(fb_view, 'calendar_events') and fb_view.calendar_events:
                logging.info(f"[FreeBusy] Найдено {len(fb_view.calendar_events)} событий в calendar_events")
                for idx, event in enumerate(fb_view.calendar_events):
                    status = getattr(event, 'busy_type', None)
                    
                    if status and status != 'Free':
                        e_start = event.start.astimezone(self.TZ)
                        e_end = event.end.astimezone(self.TZ)
                        
                        logging.info(f"[FreeBusy] Событие #{idx+1}: {e_start.strftime('%H:%M')} - {e_end.strftime('%H:%M')} (статус: {status})")
                        busy_raw.append((e_start, e_end))
            else:
                logging.warning(f"[FreeBusy] Атрибут calendar_events отсутствует или пуст")
            
            busy_raw.sort(key=lambda x: x[0])
            
            if not busy_raw:
                logging.info(f"[FreeBusy] для {room_name}: нет занятых периодов (все свободны)")
                return ([], [(day_start, day_end)])
                
            merged_busy = [busy_raw[0]]
            logging.info(f"[FreeBusy] Начало объединения: {len(busy_raw)} периодов")
            
            for current_start, current_end in busy_raw[1:]:
                last_start, last_end = merged_busy[-1]
                
                if current_start <= last_end:
                    new_end = max(last_end, current_end)
                    merged_busy[-1] = (last_start, new_end)
                    logging.debug(f"[FreeBusy] Объединение: {last_start.strftime('%H:%M')}-{last_end.strftime('%H:%M')} + {current_start.strftime('%H:%M')}-{current_end.strftime('%H:%M')} -> {last_start.strftime('%H:%M')}-{new_end.strftime('%H:%M')}")
                else:
                    merged_busy.append((current_start, current_end))
                    logging.debug(f"[FreeBusy] Новый период: {current_start.strftime('%H:%M')}-{current_end.strftime('%H:%M')}")
            
            logging.info(f"[FreeBusy] для {room_name}: найдено {len(busy_raw)} периодов, объединено в {len(merged_busy)}")
            for i, (s, e) in enumerate(merged_busy):
                logging.info(f"[FreeBusy] Занятый период [{i+1}]: {s.strftime('%H:%M')} - {e.strftime('%H:%M')}")
            
            free_intervals = self._get_free_intervals(merged_busy, day_start, day_end)
            
            logging.info(f"[FreeBusy] для {room_name}: найдено {len(free_intervals)} свободных периодов")
            for i, (s, e) in enumerate(free_intervals):
                logging.info(f"[FreeBusy] Свободный период [{i+1}]: {s.strftime('%H:%M')} - {e.strftime('%H:%M')}")
            
            return (merged_busy, free_intervals)
            
        except Exception as e:
            logging.error(f"[FreeBusy] Критическая ошибка для {room_email}: {type(e).__name__}: {e}", exc_info=True)
            return ([], [])

    def _get_free_intervals(self, busy_intervals: List[Tuple[datetime, datetime]], day_start: datetime, day_end: datetime) -> List[Tuple[datetime, datetime]]:
        if not busy_intervals:
            return [(day_start, day_end)]
        
        free = []
        
        if busy_intervals[0][0] > day_start:
            free.append((day_start, busy_intervals[0][0]))
        
        for i in range(len(busy_intervals) - 1):
            free_start = busy_intervals[i][1]
            free_end = busy_intervals[i + 1][0]
            if free_end > free_start:
                free.append((free_start, free_end))
        
        if busy_intervals[-1][1] < day_end:
            free.append((busy_intervals[-1][1], day_end))
        
        return free


    def is_room_available(self, room_name: str, start: datetime, end: datetime) -> Tuple[bool, str]:
        if room_name not in self.cfg.rooms:
            return False, f"⚠️ Комната '{room_name}' не найдена в конфигурации."
        
        room_email = self.cfg.rooms[room_name]
        
        try:
            ews_start = self._make_tz_aware(start)
            ews_end = self._make_tz_aware(end)
            
            free_busy_gen = self.account.protocol.get_free_busy_info(
                accounts=[(room_email, 'Resource', False)],
                start=ews_start,
                end=ews_end,
            )
            free_busy_info = list(free_busy_gen)
            
            for status in free_busy_info:
                if hasattr(status, 'calendar_events') and status.calendar_events:
                    for event in status.calendar_events:
                        busy_type = getattr(event, 'busy_type', None)
                        if busy_type and busy_type != 'Free':
                            if not (event.end <= ews_start or event.start >= ews_end):
                                conflict_start = max(event.start, ews_start)
                                conflict_end = min(event.end, ews_end)
                                return (
                                    False,
                                    f"Комната занята: {conflict_start.strftime('%H:%M')}-{conflict_end.strftime('%H:%M')} "
                                    f"(Конфликт с существующим бронированием)"
                                )
            
            return True, "Слот свободен."
            
        except Exception as e:
            logging.warning(f"[is_room_available] GetFreeBusyInfo failed: {type(e).__name__}: {e}")
            events = self.get_room_events(room_name, start.date())
            if events is None:
                return False, "Не удалось проверить занятость (нет доступа к календарю или Free/Busy)."
            
            for ev in events:
                if not (ev.end <= start or ev.start >= end):
                    organizer = getattr(ev, 'organizer', None)
                    organizer_text = ""
                    if organizer and hasattr(organizer, 'name') and organizer.name:
                        organizer_text = f" — {organizer.name}"
                    elif hasattr(ev, 'required_attendees') and ev.required_attendees:
                        for att in ev.required_attendees[:1]:
                            if hasattr(att, 'mailbox') and hasattr(att.mailbox, 'name') and att.mailbox.name:
                                organizer_text = f" — {att.mailbox.name}"
                                break
                    
                    return (
                        False,
                        f"Комната занята: {ev.start.strftime('%H:%M')}-{ev.end.strftime('%H:%M')}{organizer_text} ({ev.subject or 'Без темы'})",
                    )
            return True, "Слот свободен."

    def get_room_busy_periods(self, room_name: str, date: date_cls) -> Optional[List[Tuple[datetime, datetime]]]:
        if room_name not in self.cfg.rooms:
            return None
        
        room_email = self.cfg.rooms[room_name]
        
        try:
            day_start = self._make_tz_aware(datetime.combine(date, time.min))
            day_end = self._make_tz_aware(datetime.combine(date, time.max))
            
            free_busy_gen = self.account.protocol.get_free_busy_info(
                accounts=[(room_email, 'Resource', False)],
                start=day_start,
                end=day_end,
            )
            free_busy_info = list(free_busy_gen)
            
            busy_periods = []
            for status in free_busy_info:
                if hasattr(status, 'calendar_events') and status.calendar_events:
                    for event in status.calendar_events:
                        busy_type = getattr(event, 'busy_type', None)
                        if busy_type and busy_type != 'Free':
                            period_start = event.start.astimezone(self.TZ) if hasattr(event.start, 'astimezone') else event.start
                            period_end = event.end.astimezone(self.TZ) if hasattr(event.end, 'astimezone') else event.end
                            busy_periods.append((period_start, period_end))
            
            busy_periods.sort(key=lambda x: x[0])
            
            if len(busy_periods) <= 1:
                return busy_periods
            
            merged_periods = [busy_periods[0]]
            for current_start, current_end in busy_periods[1:]:
                last_start, last_end = merged_periods[-1]
                if current_start <= last_end or (current_start - last_end).total_seconds() <= 60:
                    new_end = max(last_end, current_end)
                    merged_periods[-1] = (last_start, new_end)
                else:
                    merged_periods.append((current_start, current_end))
            
            logging.info(f"📊 GetFreeBusyInfo: найдено {len(busy_periods)} периодов, после объединения: {len(merged_periods)}")
            for start, end in merged_periods:
                logging.debug(f"   {start.strftime('%H:%M')} - {end.strftime('%H:%M')}")
            
            return merged_periods
            
        except Exception as e:
            logging.warning(f"⚠️ GetFreeBusyInfo не сработал: {type(e).__name__}: {e}")
            events = self.get_room_events(room_name, date)
            if events is None:
                return None
            
            busy_periods = []
            for ev in events:
                busy_periods.append((ev.start, ev.end))
            
            busy_periods.sort(key=lambda x: x[0])
            
            if len(busy_periods) <= 1:
                return busy_periods
            
            merged_periods = [busy_periods[0]]
            for current_start, current_end in busy_periods[1:]:
                last_start, last_end = merged_periods[-1]
                if current_start <= last_end or (current_start - last_end).total_seconds() <= 60:
                    new_end = max(last_end, current_end)
                    merged_periods[-1] = (last_start, new_end)
                else:
                    merged_periods.append((current_start, current_end))
            
            logging.info(f"📊 Календарь: найдено {len(busy_periods)} событий, после объединения: {len(merged_periods)}")
            for start, end in merged_periods:
                logging.debug(f"   {start.strftime('%H:%M')} - {end.strftime('%H:%M')}")
            
            return merged_periods if merged_periods else []

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
            event.save(send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY)
            return True, "✅ Встреча успешно создана! Приглашения отправлены.", event.id
        except Exception as e:
            error_msg = str(e)
            if "Conflict" in error_msg or "conflict" in error_msg or "occupied" in error_msg.lower():
                return False, f"❌ Конфликт времени: комната или участник уже заняты в это время.", None
            return False, f"❌ Ошибка создания: {error_msg}", None

    def cancel_event(self, event_id: str) -> bool:
        try:
            logging.info(f"[Cancel] Попытка удаления встречи {event_id}...")
            item = self.account.calendar.get(id=event_id)
            
            if item:
                logging.info(f"[Cancel] Встреча найдена: {item.subject}. Отправка уведомлений...")
                item.delete(send_meeting_cancellations=SEND_TO_ALL_AND_SAVE_COPY)
                logging.info(f"[Cancel] Встреча {event_id} успешно удалена, уведомления отправлены.")
                return True
            else:
                logging.warning(f"[Cancel] Встреча {event_id} не найдена.")
                return False
                
        except Exception as e:
            logging.error(f"[Cancel] Критическая ошибка при удалении встречи {event_id}: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return False

    def reschedule_event(self, event_id: str, new_start: datetime, new_end: datetime) -> Tuple[bool, str]:
        try:
            item = self.account.calendar.get(id=event_id)
            if not item:
                return False, "❌ Встреча не найдена."
            item.start = self._make_tz_aware(new_start)
            item.end = self._make_tz_aware(new_end)
            item.save(send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY)
            return True, "✅ Встреча успешно перенесена."
        except Exception as e:
            return False, f"❌ Не удалось перенести: {e}"

    def get_upcoming_events(self, days: int = 7) -> List[CalendarItem]:
        now = datetime.now()
        start = self._make_tz_aware(now.replace(hour=0, minute=0, second=0, microsecond=0))
        end = start + timedelta(days=days)
        events = list(self.account.calendar.view(start, end))
        return sorted(events, key=lambda x: x.start)