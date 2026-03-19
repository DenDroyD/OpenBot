from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time, date as date_cls
from typing import Dict, List, Optional, Tuple

from exchangelib import Account, Configuration, Credentials, DELEGATE, EWSTimeZone, NTLM
from exchangelib.items import CalendarItem, SEND_TO_ALL_AND_SAVE_COPY
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
            
            # ЛОГИРОВАНИЕ
            print(f"✅ Метод 1 сработал! Найдено {len(events)} событий в {room_email}")
            
            if events:
                return sorted(events, key=lambda x: x.start)
        except Exception as e:
            # ЛОГИРОВАНИЕ
            print(f"⚠️ Метод 1 не сработал: {type(e).__name__}: {e}")
            pass

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
                    # "Москва" найдет "Переговорная Москва"
                    # "СПб" найдет "Переговорная Санкт-Петербург"
                    if room_name_lower in location_lower or room_email_lower in location_lower:
                        is_room_found = True
                        print(f"📍 Найдено по location: {ev.subject} в {ev.location}")
                    
                    # Для СПб: проверяем альтернативные названия
                    if not is_room_found and room_name_lower == "спб":
                        if "санкт-петербург" in location_lower or "петербург" in location_lower:
                            is_room_found = True
                            print(f"📍 Найдено СПб по location: {ev.subject} в {ev.location}")
                
                # 2. Если не нашли по location, проверяем attendees
                if not is_room_found:
                    # Проверяем required_attendees
                    if hasattr(ev, 'required_attendees') and ev.required_attendees:
                        for att in ev.required_attendees:
                            if hasattr(att, 'mailbox') and hasattr(att.mailbox, 'email_address'):
                                if att.mailbox.email_address.lower() == room_email.lower():
                                    is_room_found = True
                                    print(f"👥 Найдено по required_attendee: {ev.subject}")
                                    break
                    
                    # Проверяем optional_attendees
                    if not is_room_found and hasattr(ev, 'optional_attendees') and ev.optional_attendees:
                        for att in ev.optional_attendees:
                            if hasattr(att, 'mailbox') and hasattr(att.mailbox, 'email_address'):
                                if att.mailbox.email_address.lower() == room_email.lower():
                                    is_room_found = True
                                    print(f"👥 Найдено по optional_attendee: {ev.subject}")
                                    break

                if is_room_found:
                    room_events.append(ev)

            # ЛОГИРОВАНИЕ
            print(f"✅ Метод 2 нашел {len(room_events)} событий")
            
            if room_events:
                return sorted(room_events, key=lambda x: x.start)
        except Exception as e:
            # ЛОГИРОВАНИЕ
            print(f"⚠️ Метод 2 не сработал: {type(e).__name__}: {e}")
            pass

        return None

    def get_room_freebusy_periods(self, room_name: str, date: date_cls) -> List[Tuple[datetime, datetime]]:
        """
        Получает периоды занятости комнаты через Free/Busy API.
        Этот метод работает БЕЗ прав доступа к календарю комнаты, используя стандартный механизм Exchange.
        Возвращает список кортежей (start, end) занятых периодов в московском времени.
        """
        if room_name not in self.cfg.rooms:
            return []
        
        room_email = self.cfg.rooms[room_name]
        # Используем правильный метод для получения часового пояса в новых версиях exchangelib
        tz = EWSTimeZone('Europe/Moscow')
        
        # Формируем начало и конец дня в нужном часовом поясе
        start_dt = datetime.combine(date, time.min)
        end_dt = datetime.combine(date, time.max)
        
        start = self._make_tz_aware(start_dt)
        end = self._make_tz_aware(end_dt)
        
        try:
            # Запрос Free/Busy информации
            # Возвращает список объектов FreeBusyView (по одному на каждый email в запросе)
            fb_views = self.account.protocol.get_free_busy_info(
                attendees=[room_email],
                start=start,
                end=end,
            )
            
            if not fb_views or len(fb_views) == 0:
                logging.warning(f"FreeBusy: пустой ответ для {room_email}")
                return []
            
            fb_view = fb_views[0]
            busy_periods = []
            
            # Извлекаем периоды занятости
            if hasattr(fb_view, 'busy_periods') and fb_view.busy_periods:
                for period in fb_view.busy_periods:
                    # Статусы: 'Free', 'Tentative', 'Busy', 'OOF'
                    # Нас интересуют все, кроме 'Free'
                    status = getattr(period, 'free_busy_status', None)
                    if status and status != 'Free':
                        p_start = period.start.astimezone(tz)
                        p_end = period.end.astimezone(tz)
                        busy_periods.append((p_start, p_end))
                        logging.debug(f"  Период: {p_start.strftime('%H:%M')} - {p_end.strftime('%H:%M')} ({status})")
            
            # Сортируем по времени начала
            busy_periods.sort(key=lambda x: x[0])
            
            # Объединяем смежные и перекрывающиеся интервалы
            # Например: 11:30-12:00 и 12:00-13:00 -> 11:30-13:00
            if not busy_periods:
                logging.info(f"FreeBusy для {room_name}: нет занятых периодов")
                return []
                
            merged_periods = [busy_periods[0]]
            
            for current_start, current_end in busy_periods[1:]:
                last_start, last_end = merged_periods[-1]
                
                # Если текущий начинается раньше или ровно когда закончился предыдущий
                if current_start <= last_end:
                    # Продлеваем последний период, если текущий заканчивается позже
                    new_end = max(last_end, current_end)
                    merged_periods[-1] = (last_start, new_end)
                else:
                    # Добавляем новый отдельный период
                    merged_periods.append((current_start, current_end))
            
            logging.info(f"FreeBusy для {room_name}: найдено {len(busy_periods)} периодов, объединено в {len(merged_periods)}")
            for i, (s, e) in enumerate(merged_periods):
                logging.info(f"  [{i+1}] {s.strftime('%H:%M')} - {e.strftime('%H:%M')}")
            return merged_periods
            
        except Exception as e:
            logging.error(f"Ошибка GetFreeBusyInfo для {room_email}: {e}", exc_info=True)
            return []


    def is_room_available(self, room_name: str, start: datetime, end: datetime) -> Tuple[bool, str]:
        """
        Проверяет доступность комнаты, используя протокол GetFreeBusyInfo.
        Это более надежный способ, чем прямой доступ к календарю.
        """
        if room_name not in self.cfg.rooms:
            return False, f"⚠️ Комната '{room_name}' не найдена в конфигурации."
        
        room_email = self.cfg.rooms[room_name]
        
        try:
            # Конвертируем время в формат EWS
            ews_start = self._make_tz_aware(start)
            ews_end = self._make_tz_aware(end)
            
            # Используем протокол для получения информации о занятости
            # Возвращает список статусов для каждого участника
            free_busy_info = self.account.protocol.get_free_busy_info(
                attendees=[room_email],
                start=ews_start,
                end=ews_end,
            )
            
            # Анализируем результат
            for status in free_busy_info:
                # Если есть занятые периоды в запрошенном интервале
                if hasattr(status, 'busy_periods') and status.busy_periods:
                    for period in status.busy_periods:
                        # Проверяем пересечение с нашим интервалом
                        if not (period.end <= ews_start or period.start >= ews_end):
                            # Нашли конфликт
                            conflict_start = max(period.start, ews_start)
                            conflict_end = min(period.end, ews_end)
                            return (
                                False,
                                f"Комната занята: {conflict_start.strftime('%H:%M')}-{conflict_end.strftime('%H:%M')} "
                                f"(Конфликт с существующим бронированием)"
                            )
            
            # Если занятых периодов нет или они не пересекаются
            return True, "Слот свободен."
            
        except Exception as e:
            # Если GetFreeBusyInfo не сработал, пробуем старый метод (как запасной)
            # Это может быть менее надежно, но лучше чем ничего
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
        """
        Получает все периоды занятости переговорки за указанный день.
        Использует GetFreeBusyInfo для получения информации о занятости.
        Возвращает список кортежей (start, end) - периоды когда комната занята.
        Если нет доступа - возвращает None.
        Объединяет смежные или перекрывающиеся периоды.
        """
        if room_name not in self.cfg.rooms:
            return None
        
        room_email = self.cfg.rooms[room_name]
        
        try:
            # Определяем начало и конец дня в часовом поясе
            day_start = self._make_tz_aware(datetime.combine(date, time.min))
            day_end = self._make_tz_aware(datetime.combine(date, time.max))
            
            # Получаем информацию о занятости через GetFreeBusyInfo
            free_busy_info = self.account.protocol.get_free_busy_info(
                attendees=[room_email],
                start=day_start,
                end=day_end,
            )
            
            busy_periods = []
            for status in free_busy_info:
                if hasattr(status, 'busy_periods') and status.busy_periods:
                    for period in status.busy_periods:
                        # Сохраняем периоды занятости, конвертируя в наш часовой пояс
                        period_start = period.start.astimezone(self.TZ) if hasattr(period.start, 'astimezone') else period.start
                        period_end = period.end.astimezone(self.TZ) if hasattr(period.end, 'astimezone') else period.end
                        busy_periods.append((period_start, period_end))
            
            # Сортируем периоды по времени начала
            busy_periods.sort(key=lambda x: x[0])
            
            # Объединяем смежные и перекрывающиеся периоды
            if len(busy_periods) <= 1:
                return busy_periods
            
            merged_periods = [busy_periods[0]]
            for current_start, current_end in busy_periods[1:]:
                last_start, last_end = merged_periods[-1]
                
                # Если текущий период начинается сразу после последнего или перекрывается с ним
                # (с учетом возможной разницы в 1 минуту из-за округления)
                if current_start <= last_end or (current_start - last_end).total_seconds() <= 60:
                    # Объединяем периоды, беря максимальный конец
                    new_end = max(last_end, current_end)
                    merged_periods[-1] = (last_start, new_end)
                else:
                    # Добавляем как отдельный период
                    merged_periods.append((current_start, current_end))
            
            # ЛОГИРОВАНИЕ
            print(f"📊 GetFreeBusyInfo: найдено {len(busy_periods)} периодов, после объединения: {len(merged_periods)}")
            for start, end in merged_periods:
                print(f"   {start.strftime('%H:%M')} - {end.strftime('%H:%M')}")
            
            return merged_periods
            
        except Exception as e:
            # ЛОГИРОВАНИЕ
            print(f"⚠️ GetFreeBusyInfo не сработал: {type(e).__name__}: {e}")
            # Если GetFreeBusyInfo не сработал, пробуем получить из календаря
            events = self.get_room_events(room_name, date)
            if events is None:
                return None
            
            busy_periods = []
            for ev in events:
                busy_periods.append((ev.start, ev.end))
            
            busy_periods.sort(key=lambda x: x[0])
            
            # Объединяем смежные и перекрывающиеся периоды
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
            
            # ЛОГИРОВАНИЕ
            print(f"📊 Календарь: найдено {len(busy_periods)} событий, после объединения: {len(merged_periods)}")
            for start, end in merged_periods:
                print(f"   {start.strftime('%H:%M')} - {end.strftime('%H:%M')}")
            
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
            event.save(send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY)
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

