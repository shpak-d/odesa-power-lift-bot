from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime

import httpx
from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from aiogram.utils.backoff import BackoffConfig

from bot import create_router
from config import Config
from database import Database
from instance_lock import InstanceLock
from monitor import Monitor
from notifications import NotificationDispatcher
from schedule_provider import HttpScheduleProvider
from telegram_client import GuardedBot

logger = logging.getLogger(__name__)


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...]):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        value = super().format(record)
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return value


async def telegram_loop(bot: GuardedBot, config: Config, db: Database, monitor: Monitor) -> None:
    # me() заповнює кеш; start_polling() використає його без другого getMe.
    await bot.me()
    webhook = await bot.get_webhook_info(request_timeout=20)
    if webhook.url:
        raise RuntimeError(
            "Для цього токена налаштовано webhook. Спочатку зупиніть "
            "інший сервіс і видаліть webhook свідомо."
        )
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Початок роботи"),
            BotCommand(command="menu", description="Кнопки керування"),
            BotCommand(command="today", description="Графік на сьогодні"),
            BotCommand(command="tomorrow", description="Графік на завтра"),
            BotCommand(command="lift", description="Прогноз ліфта"),
            BotCommand(command="status", description="Стан моніторингу"),
            BotCommand(command="ping", description="Перевірити, чи бот відповідає"),
            BotCommand(command="help", description="Допомога"),
        ],
        request_timeout=20,
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(config, db))
    tasks = [asyncio.create_task(monitor.notification_loop(), name="notifications")]
    try:
        tasks.append(asyncio.create_task(
            dispatcher.start_polling(
                bot,
                polling_timeout=50,
                # Fallback for errors outside GuardedBot's transient-error handling.
                backoff_config=BackoffConfig(min_delay=10, max_delay=300, factor=2, jitter=1),
                handle_signals=False,
                close_bot_session=False,
                allowed_updates=["message"],
                tasks_shutdown_timeout=5,
            ),
            name="telegram-polling",
        ))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        with contextlib.suppress(RuntimeError):
            await dispatcher.stop_polling()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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
                tasks = [
                    asyncio.create_task(monitor.monitor_loop(), name="monitor"),
                    asyncio.create_task(telegram_loop(bot, config, db, monitor), name="polling"),
                    asyncio.create_task(bot.diagnostics_loop(), name="telegram-diagnostics"),
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
            bot.session.log_summary()
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
