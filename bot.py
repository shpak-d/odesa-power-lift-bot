from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import OPERATOR_NAMES, Config
from database import Database
from notifications import day_report
from telegram_messages import MessagePanel
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
    "Зміни графіка, ранковий звіт і нагадування надходять окремими повідомленнями "
    "з оповіщенням і зберігаються в історії. "
    "Кнопки перемикають вміст основного допису з меню. "
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
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.id not in self.allowed:
            return None
        return await handler(event, data)


def create_router(config: Config, db: Database, panel: MessagePanel | None = None) -> Router:
    router = Router()
    router.message.outer_middleware(AllowedChatsMiddleware(config.allowed_chat_ids))
    router.callback_query.outer_middleware(AllowedChatsMiddleware(config.allowed_chat_ids))
    keyboard = navigation_keyboard()

    async def respond(message: Message, text: str, *, force: bool = False) -> None:
        nonlocal panel
        if panel is None:
            panel = MessagePanel(db, message.bot)
        await panel.send_message(message.chat.id, text, reply_markup=keyboard, force=force)

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await respond(message, "Моніторинг налаштовано.\n\n" + HELP, force=True)

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        await respond(
            message,
            "Оберіть дію кнопками нижче.\n\n"
            "«Перевірити бота» — швидка перевірка відповіді.\n"
            "«Стан моніторингу» — актуальність графіків і стан доставки.",
            force=True,
        )

    @router.message(Command("ping"))
    @router.message(F.text == "Перевірити бота")
    async def ping(message: Message) -> None:
        now = datetime.now(UTC).astimezone(config.timezone)
        await respond(
            message,
            f"✅ Бот працює.\nВідповідь сформовано: {now:%d.%m.%Y %H:%M:%S} (Київ).\n\n"
            "Стан графіків і перевірок: /status.",
        )

    async def report(message: Message, offset: int = 0) -> None:
        now = datetime.now(UTC)
        day = now.astimezone(config.timezone).date() + timedelta(days=offset)
        await respond(message, day_report(db, day, config, now))

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
        reports = []
        for offset in (0, 1):
            day = now.astimezone(config.timezone).date() + timedelta(days=offset)
            reports.append(day_report(db, day, config, now, lift_only=True))
        await respond(message, "\n\n".join(reports))

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
        today = datetime.now(UTC).astimezone(config.timezone).date()
        sources = sorted({
            schedule.source
            for schedule in db.range(config.operator_id, today, today + timedelta(days=1))
        })
        source_urls = "\n".join(html.escape(source) for source in sources)
        text = (
            f"<b>Стан моніторингу</b>\n\nОператор: {OPERATOR_NAMES[config.operator_id]}\n"
            f"Джерело: {config.provider}\nЧерги: {', '.join(config.queues)}\n"
            + (f"{source_urls}\n" if source_urls else "") +
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
        await respond(message, text)

    @router.message(Command("help"))
    @router.message(F.text == "Допомога")
    async def help_command(message: Message) -> None:
        await respond(message, HELP)

    actions = {
        "today": today, "tomorrow": tomorrow, "lift": lift,
        "status": status, "ping": ping, "help": help_command,
    }

    @router.callback_query(F.data.in_({f"nav:{action}" for action in actions}))
    async def navigate(query: CallbackQuery) -> None:
        # Acknowledge the button without adding a message to chat history.
        await query.answer()
        await actions[query.data.removeprefix("nav:")](query.message)

    return router
