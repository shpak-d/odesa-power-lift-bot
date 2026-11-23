from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetMe, GetUpdates, GetWebhookInfo, SetMyCommands
from aiogram.types import BotCommand

from bot import create_router
from config import Config
from database import Database, stamp
from instance_lock import InstanceLock
from monitor import Monitor
from notifications import NotificationDispatcher
from schedule_provider import HttpScheduleProvider

logger = logging.getLogger(__name__)
# Також охоплює getMe з bot.me() при запуску polling в aiogram.
RETRYABLE_STARTUP_METHODS = (GetMe, GetWebhookInfo, SetMyCommands)


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...]):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        value = super().format(record)
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return value


class GuardedBot(Bot):
    def __init__(self, *args, db: Database, conflict: asyncio.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self.db, self.conflict = db, conflict

    async def __call__(self, method, request_timeout=None):
        network_retry_delay = 5
        while True:
            try:
                return await super().__call__(method, request_timeout=request_timeout)
            except TelegramRetryAfter as exc:
                if not isinstance(method, RETRYABLE_STARTUP_METHODS):
                    raise
                logger.warning(
                    "Telegram 429 під час %s: повторна спроба через %s с",
                    method.__api_method__,
                    exc.retry_after,
                )
                await asyncio.sleep(exc.retry_after)
            except TelegramNetworkError:
                if not isinstance(method, RETRYABLE_STARTUP_METHODS):
                    raise
                logger.warning(
                    "Мережева помилка Telegram під час %s: повторна спроба через %s с",
                    method.__api_method__,
                    network_retry_delay,
                )
                await asyncio.sleep(network_retry_delay)
                network_retry_delay = min(network_retry_delay * 2, 60)
            except TelegramConflictError:
                self.conflict.set()
                raise
            finally:
                if isinstance(method, GetUpdates):
                    self.db.set_meta("heartbeat_polling", stamp(datetime.now(UTC)))


async def run(config: Config) -> None:
    if config.app_mode == "web":
        from web_dashboard import serve

        await serve(config)
        return
    with InstanceLock(config.bot_token, config.database_path):
        db = Database(config.database_path)
        conflict, stop = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop.set)
        tasks: list[asyncio.Task] = []
        bot = GuardedBot(
            config.bot_token,
            db=db,
            conflict=conflict,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        try:
            async with httpx.AsyncClient() as client:
                monitor = Monitor(
                    config,
                    db,
                    HttpScheduleProvider(config, client),
                    NotificationDispatcher(db, bot),
                )
                retry = db.get_meta("source_retry_at")
                if not retry or datetime.now(UTC) >= datetime.fromisoformat(retry):
                    await monitor.check()
                # getMe перевіряє токен. Бот із webhook не переводимо у polling мовчки.
                await bot.get_me(request_timeout=20)
                webhook = await bot.get_webhook_info(request_timeout=20)
                if webhook.url:
                    raise RuntimeError(
                        "Для цього токена налаштовано webhook. Спочатку зупиніть "
                        "інший сервіс і видаліть webhook свідомо."
                    )
                await bot.set_my_commands(
                    [
                        BotCommand(command="start", description="Початок роботи"),
                        BotCommand(command="today", description="Графік на сьогодні"),
                        BotCommand(command="tomorrow", description="Графік на завтра"),
                        BotCommand(command="lift", description="Прогноз ліфта"),
                        BotCommand(command="status", description="Стан моніторингу"),
                        BotCommand(command="help", description="Допомога"),
                    ],
                    request_timeout=20,
                )
                dispatcher = Dispatcher()
                dispatcher.include_router(create_router(config, db))
                db.set_meta("heartbeat_polling", stamp(datetime.now(UTC)))
                tasks = [
                    asyncio.create_task(monitor.monitor_loop(), name="monitor"),
                    asyncio.create_task(monitor.notification_loop(), name="notifications"),
                    asyncio.create_task(
                        dispatcher.start_polling(
                            bot,
                            handle_signals=False,
                            close_bot_session=False,
                            allowed_updates=["message"],
                        ),
                        name="polling",
                    ),
                    asyncio.create_task(conflict.wait(), name="conflict"),
                    asyncio.create_task(stop.wait(), name="shutdown"),
                ]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if conflict.is_set():
                    raise RuntimeError(
                        "Конфлікт Telegram polling (409). Зупиніть інший екземпляр "
                        "із цим токеном; цей процес завершується."
                    )
                for task in done:
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await bot.session.close()
            db.close()


def main() -> None:
    config = Config.from_env()
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter((config.bot_token, config.alerts_api_key)))
    logging.basicConfig(level=config.log_level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
