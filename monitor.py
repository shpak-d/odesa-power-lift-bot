from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from config import Config
from database import Database, stamp
from lift import day_bounds
from models import Event
from notifications import (
    NotificationDispatcher,
    change_events,
    day_report,
    warning_events,
)
from schedule_provider import ProviderError, ScheduleProvider

logger = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        config: Config,
        db: Database,
        provider: ScheduleProvider,
        dispatcher: NotificationDispatcher | None = None,
    ):
        self.config, self.db, self.provider, self.dispatcher = config, db, provider, dispatcher
        self.lock = asyncio.Lock()

    async def check(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        self.db.set_meta("last_check", stamp(now))
        try:
            async with asyncio.timeout(self.config.http_timeout_seconds + 5):
                schedules = await self.provider.fetch(now)
            expected = {
                (queue, now.astimezone(self.config.timezone).date() + timedelta(days=d))
                for queue in self.config.queues
                for d in (0, 1)
            }
            if (
                len(schedules) != 4
                or {(s.queue, s.day) for s in schedules} != expected
                or any(s.operator != self.config.operator_id for s in schedules)
            ):
                raise ProviderError("Неповний набір графіків або інший оператор")
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, ProviderError)
                else (f"Помилка отримання даних ({type(exc).__name__}); попередній стан збережено")
            )
            async with self.lock:
                self.db.set_meta("last_error", error)
                retry = exc.retry_at if isinstance(exc, ProviderError) else None
                if retry:
                    self.db.set_meta("source_retry_at", stamp(retry))
            logger.warning("%s", error)
            return False
        async with self.lock:
            changes = self.db.apply_schedules(
                schedules,
                now,
                lambda c: change_events(c, self.db, self.config, now) if self.dispatcher else [],
            )
            self.db.set_meta("source_retry_at", "")
            if self.dispatcher:
                self.db.reconcile_warnings(warning_events(self.db, self.config, now), now)
            today = now.astimezone(self.config.timezone).date()
            self.db.cleanup(today - timedelta(days=90))
        logger.info("Отримано %s графіки; нормалізованих змін: %s", len(schedules), len(changes))
        return True

    async def tick(self, now: datetime | None = None) -> None:
        if self.dispatcher is None:
            return
        now = now or datetime.now(UTC)
        async with self.lock:
            self.db.set_meta("heartbeat_scheduler", stamp(now))
            self.db.cancel_disallowed(self.config.allowed_chat_ids)
            self.db.reconcile_warnings(warning_events(self.db, self.config, now), now)
            local = now.astimezone(self.config.timezone)
            if local.time().replace(tzinfo=None) >= self.config.morning_report_time:
                day = local.date()
                body = day_report(self.db, day, self.config, now)
                self.db.enqueue(
                    Event(
                        f"morning:{self.config.operator_id}:{day}",
                        "morning",
                        chat_id,
                        now,
                        day_bounds(day, self.config.timezone)[1],
                        body,
                    )
                    for chat_id in self.config.allowed_chat_ids
                )
            retry = self.db.get_meta("telegram_retry_at")
            if retry and datetime.fromisoformat(retry) > now:
                return
            await self.dispatcher.dispatch(now)

    async def monitor_loop(self) -> None:
        # Початкове завантаження виконується під час запуску, далі кожні N хвилин.
        next_check = datetime.now(UTC) + timedelta(minutes=self.config.check_interval_minutes)
        while True:
            now = datetime.now(UTC)
            self.db.set_meta("heartbeat_monitor", stamp(now))
            retry = self.db.get_meta("source_retry_at")
            permitted = not retry or now >= datetime.fromisoformat(retry)
            if now >= next_check and permitted:
                await self.check()
                next_check = datetime.now(UTC) + timedelta(
                    minutes=self.config.check_interval_minutes,
                )
            await asyncio.sleep(5)

    async def notification_loop(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(5)
