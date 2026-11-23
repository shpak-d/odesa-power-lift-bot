from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates, SendMessage
from aiogram.types import Chat, Message, Update
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
        assert isinstance(method, SendMessage), "Unexpected Telegram call in an offline test"
        self.sent.append(method)
        return Message(
            message_id=len(self.sent),
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
        ("/today", 1, "Електроенергія"),
        ("/tomorrow", 1, "Електроенергія"),
        ("/lift", 2, "Прогноз ліфта"),
        ("/status", 1, "Стан моніторингу"),
        ("/help", 1, "Бот стежить"),
        ("Сьогодні", 1, "Електроенергія"),
    ],
)
async def test_commands_through_real_aiogram_dispatcher(
    config,
    db,
    make_schedule,
    now,
    command,
    count,
    expected,
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
            chat=Chat(id=123, type="private"),
            text=command,
        ),
    )
    try:
        await dispatcher.feed_update(bot, update)
        assert len(session.sent) == count
        assert expected in session.sent[0].text
        assert config.bot_token not in session.sent[0].text
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
