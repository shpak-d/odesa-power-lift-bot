from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject

from config import OPERATOR_NAMES, Config
from database import Database
from notifications import day_report
from telegram_ui import navigation_keyboard

HELP = (
    "Бот стежить за опублікованими графіками двох ліній живлення.\n\n"
    "/menu — показати кнопки керування\n"
    "/today — графік на сьогодні\n"
    "/tomorrow — графік на завтра\n"
    "/lift — прогноз простою ліфта на сьогодні й завтра\n"
    "/status — стан перевірок і доставки повідомлень\n"
    "/ping — перевірити, чи бот відповідає\n"
    "/help — допомога\n\n"
    "За зміни графіка надходить повідомлення, щодня — ранковий звіт. "
    "Попередження надходять перед плановим відключенням квартири та можливим простоєм ліфта. "
    "Доступ обмежено дозволеними Chat ID. Для отримання приватних повідомлень натисніть /start.\n\n"
    "Це прогноз, а не фактичний стан ліфта. Відсутність графіка не означає наявності світла."
)


class AllowedChatsMiddleware(BaseMiddleware):
    def __init__(self, ids: tuple[int, ...]):
        self.allowed = frozenset(ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.chat.id not in self.allowed:
            return None
        return await handler(event, data)


def create_router(config: Config, db: Database) -> Router:
    router = Router()
    router.message.outer_middleware(AllowedChatsMiddleware(config.allowed_chat_ids))
    keyboard = navigation_keyboard()

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer("Моніторинг налаштовано.\n\n" + HELP, reply_markup=keyboard)

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        await message.answer(
            "Оберіть дію кнопками нижче.\n\n"
            "«Перевірити бота» — швидка перевірка відповіді.\n"
            "«Стан моніторингу» — актуальність графіків і стан доставки.",
            reply_markup=keyboard,
        )

    @router.message(Command("ping"))
    @router.message(F.text == "Перевірити бота")
    async def ping(message: Message) -> None:
        now = datetime.now(UTC).astimezone(config.timezone)
        await message.answer(
            f"✅ Бот працює.\nВідповідь сформовано: {now:%d.%m.%Y %H:%M:%S} (Київ).\n\n"
            "Стан графіків і перевірок: /status.",
            reply_markup=keyboard,
        )

    async def report(message: Message, offset: int = 0) -> None:
        now = datetime.now(UTC)
        day = now.astimezone(config.timezone).date() + timedelta(days=offset)
        await message.answer(day_report(db, day, config, now), reply_markup=keyboard)

    @router.message(Command("today"))
    @router.message(F.text == "Сьогодні")
    async def today(message: Message) -> None:
        await report(message)

    @router.message(Command("tomorrow"))
    @router.message(F.text == "Завтра")
    async def tomorrow(message: Message) -> None:
        await report(message, 1)

    @router.message(Command("lift"))
    @router.message(F.text == "Ліфт")
    async def lift(message: Message) -> None:
        now = datetime.now(UTC)
        for offset in (0, 1):
            day = now.astimezone(config.timezone).date() + timedelta(days=offset)
            await message.answer(
                day_report(db, day, config, now, lift_only=True), reply_markup=keyboard
            )

    @router.message(Command("status"))
    @router.message(F.text == "Стан моніторингу")
    async def status(message: Message) -> None:
        def formatted(key: str) -> str:
            value = db.get_meta(key)
            return (
                datetime.fromisoformat(value)
                .astimezone(config.timezone)
                .strftime("%d.%m.%Y %H:%M:%S")
                if value
                else "ще не було"
            )

        last_success = db.get_meta("last_success")
        stale = not last_success or (
            datetime.now(UTC) - datetime.fromisoformat(last_success) > config.stale_after
        )
        counts = db.delivery_counts()
        error = html.escape(db.get_meta("last_error") or "немає")
        text = (
            f"<b>Стан моніторингу</b>\n\nОператор: {OPERATOR_NAMES[config.operator_id]}\n"
            f"Джерело: {config.provider}\nЧерги: {', '.join(config.queues)}\n"
            f"Перевірка кожні {config.check_interval_minutes} хв.\n"
            f"Остання спроба: {formatted('last_check')}\n"
            f"Успішне отримання: {formatted('last_success')}\n"
            f"Актуальність: {'⚠️ дані застаріли або відсутні' if stale else 'отримано нещодавно'}\n"
            f"Остання помилка: {error}\n\n"
            f"Повідомлень очікують: {counts.get('pending', 0)}\n"
            f"Невдала доставка: {counts.get('failed', 0)}\n"
            f"Невизначена доставка: {counts.get('uncertain', 0)}\n"
            "Невизначені доставки не повторюємо автоматично, щоб не створювати дублі.\n"
            "Дати та час публікації кожного графіка: /today, /tomorrow."
        )
        await message.answer(text, reply_markup=keyboard)

    @router.message(Command("help"))
    @router.message(F.text == "Допомога")
    async def help_command(message: Message) -> None:
        await message.answer(HELP, reply_markup=keyboard)

    return router
