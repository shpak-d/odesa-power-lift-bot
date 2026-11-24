"""Telegram-only request diagnostics and a shared, cancellable error cooldown."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
from collections import Counter
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import uuid4

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import GetMe, GetUpdates, GetWebhookInfo, SetMyCommands

from database import Database, stamp

logger = logging.getLogger(__name__)
RETRYABLE_METHODS = (GetMe, GetUpdates, GetWebhookInfo, SetMyCommands)
REQUEST_ATTEMPT: ContextVar[int] = ContextVar("telegram_attempt", default=1)
HTTP_STATUS: ContextVar[int | None] = ContextVar("telegram_http_status", default=None)
BACKOFF_SECONDS = (10, 20, 40, 80, 160, 300)
RECOVERY_SECONDS = 15 * 60


class DiagnosticSession(AiohttpSession):
    """Count actual session attempts, never local cooldowns or cached bot.me()."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.instance = f"{os.getpid()}-{uuid4().hex[:8]}"
        self.total = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.window_started = monotonic()
        self.methods: Counter[str] = Counter()
        self.outcomes: Counter[str] = Counter()

    def check_response(self, bot, method, status_code, content):
        # Capture the real HTTP status even when JSON decoding subsequently fails.
        HTTP_STATUS.set(status_code)
        try:
            return super().check_response(bot, method, status_code, content)
        except ClientDecodeError:
            if status_code >= 500:
                raise TelegramServerError(
                    method=method, message=f"Telegram HTTP {status_code} (non-JSON response)"
                ) from None
            raise

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        name = method.__api_method__
        self.total += 1
        request_id = self.total
        self.methods[name] += 1
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        started = monotonic()
        status_token = HTTP_STATUS.set(None)
        outcome, retry_after = "ok", None
        logger.info(
            "telegram_request_start instance=%s request=%s method=%s attempt=%s "
            "in_flight=%s timeout_s=%s",
            self.instance, request_id, name, REQUEST_ATTEMPT.get(), self.in_flight,
            self.timeout if timeout is None else timeout,
        )
        try:
            return await super().make_request(bot, method, timeout=timeout)
        except BaseException as exc:
            # Only class names: exception messages/bodies/URLs can contain secrets.
            outcome = type(exc).__name__
            retry_after = getattr(exc, "retry_after", None)
            raise
        finally:
            self.in_flight -= 1
            self.outcomes[outcome] += 1
            logger.log(
                logging.INFO if outcome in {"ok", "CancelledError"} else logging.WARNING,
                "telegram_request_end instance=%s request=%s method=%s attempt=%s "
                "duration_s=%.3f http_status=%s outcome=%s retry_after=%s in_flight=%s",
                self.instance, request_id, name, REQUEST_ATTEMPT.get(), monotonic() - started,
                HTTP_STATUS.get(), outcome, retry_after, self.in_flight,
            )
            HTTP_STATUS.reset(status_token)

    def log_summary(self) -> None:
        elapsed = max(monotonic() - self.window_started, 0.001)
        requests = sum(self.methods.values())
        logger.info(
            "telegram_request_summary instance=%s window_s=%.3f requests=%s "
            "requests_per_minute=%.3f methods=%s outcomes=%s total=%s "
            "in_flight=%s peak_in_flight=%s",
            self.instance, elapsed, requests, requests * 60 / elapsed,
            dict(self.methods), dict(self.outcomes), self.total,
            self.in_flight, self.peak_in_flight,
        )
        self.window_started = monotonic()
        self.methods.clear()
        self.outcomes.clear()
        self.peak_in_flight = self.in_flight


class TelegramCooldown(TelegramRetryAfter):
    """Local deferral: no HTTP request was made and no delivery is ambiguous."""


class GuardedBot(Bot):
    def __init__(self, *args, db: Database, conflict: asyncio.Event, **kwargs):
        if kwargs.get("session") is None:
            kwargs["session"] = DiagnosticSession(timeout=30)
        super().__init__(*args, **kwargs)
        self.db, self.conflict = db, conflict
        self.failures: Counter[str] = Counter()
        self._poll_lock, self._request_lock, self._me_lock = (
            asyncio.Lock(), asyncio.Lock(), asyncio.Lock()
        )
        retry_at = db.get_meta("telegram_retry_at")
        remaining = (
            (datetime.fromisoformat(retry_at) - datetime.now(UTC)).total_seconds()
            if retry_at else 0
        )
        self._retry_at = monotonic() + max(0, remaining)

    async def me(self):
        async with self._me_lock:
            return await super().me()

    async def __call__(self, method, request_timeout=None):
        # One long poll and one short API request may coexist. Retries never spawn tasks.
        lock = self._poll_lock if isinstance(method, GetUpdates) else self._request_lock
        async with lock:
            try:
                return await self._call_with_backoff(method, request_timeout)
            finally:
                if isinstance(method, GetUpdates):
                    self.db.set_meta("heartbeat_polling", stamp(datetime.now(UTC)))

    async def _call_with_backoff(self, method, request_timeout):
        name = method.__api_method__
        retryable = isinstance(method, RETRYABLE_METHODS)
        while True:
            remaining = self._retry_at - monotonic()
            if remaining > 0:
                if not retryable:
                    # Defer sends rather than sleeping with an expiring outbox event.
                    raise TelegramCooldown(
                        method=method, message="Local Telegram cooldown; request not sent",
                        retry_after=math.ceil(remaining),
                    )
                await asyncio.sleep(remaining)
                continue  # A concurrent request may have extended the shared cooldown.
            token = REQUEST_ATTEMPT.set(self.failures[name] + 1)
            try:
                result = await super().__call__(method, request_timeout=request_timeout)
            except TelegramConflictError:
                self.conflict.set()
                raise
            except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError) as exc:
                self.failures[name] += 1
                failures = self.failures[name]
                base = (
                    BACKOFF_SECONDS[failures - 1]
                    if failures <= len(BACKOFF_SECONDS) else RECOVERY_SECONDS
                )
                delay = max(base, getattr(exc, "retry_after", 0)) + random.uniform(2, 5)
                self._retry_at = max(self._retry_at, monotonic() + delay)
                retry_at = datetime.now(UTC) + timedelta(seconds=self._retry_at - monotonic())
                self.db.set_meta("telegram_retry_at", stamp(retry_at))
                logger.warning(
                    "telegram_backoff method=%s consecutive_failures=%s error=%s "
                    "retry_after=%s delay_s=%.3f recovery_probe=%s",
                    name, failures, type(exc).__name__, getattr(exc, "retry_after", None),
                    delay, failures > len(BACKOFF_SECONDS),
                )
                if not retryable:
                    # sendMessage can have succeeded despite a timeout: never replay it here.
                    raise
            else:
                self.failures[name] = 0
                return result
            finally:
                REQUEST_ATTEMPT.reset(token)

    async def diagnostics_loop(self) -> None:
        while True:
            # Liveness includes intentional backoff; it does not assert Telegram is available.
            self.db.set_meta("heartbeat_polling", stamp(datetime.now(UTC)))
            await asyncio.sleep(60)
            self.session.log_summary()
