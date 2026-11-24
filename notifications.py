from __future__ import annotations

import html
import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from config import OPERATOR_NAMES, Config
from database import Database
from lift import clip_to_day, day_bounds, intersect_intervals, normalize_intervals
from models import Change, DaySchedule, Event, Interval

logger = logging.getLogger(__name__)


def fresh(schedule: DaySchedule | None, now: datetime, config: Config) -> bool:
    return bool(
        schedule
        and schedule.usable
        and timedelta(0) <= now - schedule.fetched_at <= config.stale_after
    )


def interval_lines(intervals: Iterable[Interval], day: date, config: Config) -> str:
    result = []
    _, day_end = day_bounds(day, config.timezone)
    for interval in clip_to_day(intervals, day, config.timezone):
        start = interval.start.astimezone(config.timezone).strftime("%H:%M")
        end = (
            "24:00"
            if interval.end == day_end
            else interval.end.astimezone(config.timezone).strftime("%H:%M")
        )
        result.append(f"• {start}–{end}")
    return "\n".join(result)


def schedule_lines(schedule: DaySchedule | None, day: date, config: Config) -> str:
    if schedule is None or schedule.status == "missing":
        return "Графік не опублікований / даних немає."
    if schedule.status == "emergency":
        return "Аварійні обмеження: опублікований план не застосовується."
    value = interval_lines(schedule.intervals, day, config) or "Планових відключень не зазначено."
    if schedule.possible:
        value += "\nМожливі відключення:\n" + interval_lines(schedule.possible, day, config)
    return value


def predicted_lift(home: DaySchedule, secondary: DaySchedule) -> tuple[Interval, ...]:
    if home.operator != secondary.operator or home.day != secondary.day:
        raise ValueError("Не можна змішувати операторів або дати")
    if not home.usable or not secondary.usable:
        return ()
    # Консервативний прогноз включає явно позначені джерелом можливі відключення.
    return intersect_intervals(
        (*home.intervals, *home.possible), (*secondary.intervals, *secondary.possible)
    )


def lift_lines(
    home: DaySchedule | None,
    secondary: DaySchedule | None,
    day: date,
    config: Config,
) -> str:
    if not home or not secondary or not home.usable or not secondary.usable:
        return "Недостатньо даних обох ліній для прогнозу."
    value = interval_lines(predicted_lift(home, secondary), day, config)
    return value or "За опублікованим планом одночасних відключень не передбачено."


def day_report(db: Database, day: date, config: Config, now: datetime, *, lift_only=False) -> str:
    home = db.get(config.operator_id, config.queue_home, day)
    secondary = db.get(config.operator_id, config.queue_secondary, day)
    title = "Прогноз ліфта" if lift_only else "Електроенергія"
    parts = [f"<b>{title} — {day:%d.%m.%Y}</b>", OPERATOR_NAMES[config.operator_id]]
    if not lift_only:
        parts += [
            f"🏠 Квартира ({config.queue_home}), без світла за планом:\n"
            + schedule_lines(home, day, config),
            f"🏢 Друга лінія ({config.queue_secondary}), без світла за планом:\n"
            + schedule_lines(secondary, day, config),
        ]
    parts.append("🛗 Можливий простій ліфта:\n" + lift_lines(home, secondary, day, config))
    for schedule in (home, secondary):
        if schedule:
            checked = schedule.fetched_at.astimezone(config.timezone)
            source_time = (
                schedule.source_updated_at.astimezone(config.timezone).strftime("%d.%m.%Y %H:%M")
                if schedule.source_updated_at
                else "не вказано"
            )
            parts.append(
                f"Черга {schedule.queue}: отримано {checked:%d.%m.%Y %H:%M}; "
                f"оновлено джерелом {source_time}."
            )
            if now - schedule.fetched_at > config.stale_after:
                parts.append(
                    f"⚠️ Дані черги {schedule.queue} застаріли: актуальність не підтверджено."
                )
    sources = sorted({s.source for s in (home, secondary) if s})
    if sources:
        parts.append("Джерело: " + ", ".join(html.escape(s) for s in sources))
    parts.append(
        "Це прогноз за графіком, а не фактичний статус ліфта чи живлення. "
        "Аварійні відключення можливі поза планом. Час — Europe/Kyiv."
    )
    return "\n\n".join(parts)


def change_events(
    changes: list[Change],
    db: Database,
    config: Config,
    now: datetime,
) -> list[Event]:
    result = []
    for change in changes:
        after = change.after
        home = db.get(config.operator_id, config.queue_home, after.day)
        secondary = db.get(config.operator_id, config.queue_secondary, after.day)
        body = (
            f"<b>Змінено графік відключень</b>\n\nЧерга: {after.queue}\n"
            f"Дата: {after.day:%d.%m.%Y}\nОператор: {OPERATOR_NAMES[after.operator]}\n\n"
            f"Було:\n{schedule_lines(change.before, after.day, config)}\n\n"
            f"Стало:\n{schedule_lines(after, after.day, config)}\n\n"
            f"🛗 Можливий простій ліфта:\n{lift_lines(home, secondary, after.day, config)}\n\n"
            f"Отримано: {now.astimezone(config.timezone):%d.%m.%Y %H:%M}.\n"
            f"Джерело: {html.escape(after.source)}\n"
            "Розрахунок за графіком, не фактичний статус ліфта."
        )
        for chat_id in config.allowed_chat_ids:
            result.append(
                Event(
                    f"change:{config.operator_id}:{change.revision}",
                    "change",
                    chat_id,
                    now,
                    day_bounds(after.day, config.timezone)[1],
                    body,
                )
            )
    return result


def warning_events(db: Database, config: Config, now: datetime) -> list[Event]:
    today = now.astimezone(config.timezone).date()
    schedules = db.range(config.operator_id, today - timedelta(days=1), today + timedelta(days=1))
    home_intervals: list[Interval] = []
    lift_intervals: list[Interval] = []
    by_key = {(s.day, s.queue): s for s in schedules}
    for schedule in schedules:
        if schedule.queue == config.queue_home and fresh(schedule, now, config):
            home_intervals.extend(schedule.intervals)
            secondary = by_key.get((schedule.day, config.queue_secondary))
            if fresh(secondary, now, config):
                lift_intervals.extend(predicted_lift(schedule, secondary))
    result = []
    for kind, intervals, label in (
        ("warning_home", home_intervals, f"🏠 Планове відключення квартири ({config.queue_home})"),
        ("warning_lift", lift_intervals, "🛗 Можливий простій ліфта"),
    ):
        for interval in normalize_intervals(intervals):
            if interval.start <= now:
                continue
            start, end = (v.astimezone(config.timezone) for v in (interval.start, interval.end))
            due = interval.start - timedelta(minutes=config.warning_before_minutes)
            body = (
                f"<b>{label}</b>\n\nПочаток: {start:%d.%m.%Y %H:%M}\n"
                f"Завершення за планом: {end:%d.%m.%Y %H:%M}\n\n"
                "Нагадування за опублікованим графіком. "
                "Фактичний стан живлення та ліфта невідомий."
            )
            queue = config.queue_home if kind == "warning_home" else ":".join(config.queues)
            key = f"{kind}:{config.operator_id}:{queue}:{interval.start.isoformat()}"
            for chat_id in config.allowed_chat_ids:
                result.append(Event(key, kind, chat_id, due, interval.start, body))
    return result


class Sender(Protocol):
    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> object: ...


class NotificationDispatcher:
    def __init__(self, db: Database, sender: Sender):
        self.db, self.sender = db, sender

    async def dispatch(self, now: datetime) -> None:
        for row in self.db.due(now):
            key, chat_id = row["event_key"], row["chat_id"]
            # Поки надсилали попереднім чатам, наступна подія могла вже початися.
            effective_now = max(now, datetime.now(UTC))
            if datetime.fromisoformat(row["expires_at"]) <= effective_now:
                self.db.finish(key, chat_id, "expired", effective_now)
                continue
            if not self.db.claim(key, chat_id):
                continue
            try:
                await self.sender.send_message(
                    chat_id=chat_id, text=row["body"], parse_mode="HTML", request_timeout=20
                )
            except TelegramRetryAfter as exc:
                # The request itself may have taken time. Preserve the longer shared
                # cooldown (including backoff/jitter) installed by GuardedBot.
                retry = max(now, datetime.now(UTC)) + timedelta(seconds=exc.retry_after)
                shared_retry = self.db.get_meta("telegram_retry_at")
                if shared_retry:
                    retry = max(retry, datetime.fromisoformat(shared_retry))
                self.db.finish(key, chat_id, "pending", now, "Ліміт Telegram", retry)
                # Спільна локальна пауза зменшує навантаження з усіх чатів.
                self.db.set_meta("telegram_retry_at", retry.isoformat())
                return
            except (TelegramForbiddenError, TelegramBadRequest):
                self.db.finish(key, chat_id, "failed", now, "Telegram відхилив повідомлення")
                logger.warning("Telegram відхилив повідомлення (%s)", row["kind"])
            except (TelegramNetworkError, TelegramServerError, TimeoutError, OSError):
                self.db.finish(key, chat_id, "uncertain", now, "Результат доставки невідомий")
                logger.warning("Неоднозначна доставка (%s); автоповтор вимкнено", row["kind"])
            except Exception:
                # Статус уже sending: безпечний щодо дублювання навіть при невідомій помилці.
                self.db.finish(key, chat_id, "uncertain", now, "Невідома помилка доставки")
                logger.exception("Помилка диспетчера повідомлень")
            else:
                self.db.finish(key, chat_id, "sent", datetime.now(UTC))
