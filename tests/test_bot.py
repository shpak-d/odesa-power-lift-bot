from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetUpdates, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from conftest import batch

from bot import create_router
from main import GuardedBot


class LocalTelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self) -> None:
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 - aiogram interface
        assert isinstance(method, (SendMessage, EditMessageText, AnswerCallbackQuery)), (
            "Unexpected Telegram call in an offline test"
        )
        self.sent.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        return Message(
            message_id=method.message_id if isinstance(method, EditMessageText) else len(self.sent),
            date=datetime.now(UTC),
            chat=Chat(id=method.chat_id, type="private"),
            text=method.text,
        )

    async def stream_content(self, *args, **kwargs) -> AsyncGenerator[bytes, None]:
        yield b""


@pytest.mark.parametrize(
    "command,count,expected",
    [
        ("/start", 1, "Моніторинг налаштовано"),
        ("/menu", 1, "Оберіть дію"),
        ("/today", 1, "Електроенергія"),
        ("/tomorrow", 1, "Електроенергія"),
        ("/lift", 1, "Прогноз ліфта"),
        ("/status", 1, "Стан моніторингу"),
        ("/ping", 1, "Бот працює"),
        ("/help", 1, "Бот стежить"),
        ("Сьогодні", 1, "Електроенергія"),
        ("Завтра", 1, "Електроенергія"),
        ("Ліфт", 1, "Прогноз ліфта"),
        ("Стан моніторингу", 1, "Стан моніторингу"),
        ("Перевірити бота", 1, "Бот працює"),
        ("Допомога", 1, "Бот стежить"),
    ],
)
@pytest.mark.parametrize("chat_id", [123, 999])
async def test_commands_through_real_aiogram_dispatcher(
    config,
    db,
    make_schedule,
    now,
    command,
    count,
    expected,
    chat_id,
):
    db.apply_schedules(batch(make_schedule), now, lambda _: [])
    session = LocalTelegramSession()
    bot = Bot(config.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(config, db))
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=now,
            chat=Chat(id=chat_id, type="private"),
            text=command,
        ),
    )
    try:
        await dispatcher.feed_update(bot, update)
        if chat_id == 999:
            assert not session.sent
            return
        assert len(session.sent) == count
        assert expected in session.sent[0].text
        assert config.bot_token not in session.sent[0].text
        for response in session.sent:
            keyboard = response.reply_markup
            assert {button.text for row in keyboard.inline_keyboard for button in row} == {
                "Сьогодні", "Завтра", "Ліфт", "Стан моніторингу", "Перевірити бота", "Допомога",
            }
    finally:
        await session.close()


@pytest.mark.parametrize("chat_id", [123, 999])
@pytest.mark.parametrize("action,expected", [
    ("today", "Електроенергія"), ("tomorrow", "Електроенергія"),
    ("lift", "Прогноз ліфта"), ("status", "Джерело:"),
    ("ping", "Бот працює"), ("help", "Бот стежить"),
])
async def test_inline_buttons_edit_one_post_and_keep_access_control(
    config, db, make_schedule, now, chat_id, action, expected,
):
    db.apply_schedules(batch(make_schedule), now, lambda _: [])
    session = LocalTelegramSession()
    bot = Bot(config.bot_token, session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(config, db))
    message = Message(message_id=10, date=now, chat=Chat(id=chat_id, type="private"), text="/menu")
    try:
        await dispatcher.feed_update(bot, Update(update_id=1, message=message))
        callback = CallbackQuery(
            id="test-button", from_user=User(id=chat_id, is_bot=False, first_name="Test"),
            chat_instance="test-chat", message=message.model_copy(update={"message_id": 1}),
            data=f"nav:{action}",
        )
        await dispatcher.feed_update(bot, Update(update_id=2, callback_query=callback))
        if chat_id == 999:
            assert not session.sent
            return
        assert [type(method) for method in session.sent] == [
            SendMessage, AnswerCallbackQuery, EditMessageText,
        ]
        assert session.sent[-1].message_id == 1
        assert expected in session.sent[-1].text
        if action in {"today", "tomorrow", "lift"}:
            assert "Джерело:" not in session.sent[-1].text
            assert "https://www.dtek-oem.com.ua" not in session.sent[-1].text
        if action == "status":
            assert "https://www.dtek-oem.com.ua" in session.sent[-1].text
        if action == "lift":
            assert "24.11.2026" in session.sent[-1].text
            assert "25.11.2026" in session.sent[-1].text
    finally:
        await session.close()


async def test_polling_conflict_signals_shutdown(config, db, monkeypatch):
    import asyncio

    async def conflict_call(self, method, request_timeout=None):
        raise TelegramConflictError(method=method, message="Other polling instance")

    monkeypatch.setattr(Bot, "__call__", conflict_call)
    signal = asyncio.Event()
    session = LocalTelegramSession()
    bot = GuardedBot(config.bot_token, db=db, conflict=signal, session=session)
    with pytest.raises(TelegramConflictError):
        await bot(GetUpdates())
    assert signal.is_set()
    assert db.get_meta("heartbeat_polling")
    await session.close()
