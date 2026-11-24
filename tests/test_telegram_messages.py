import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import EditMessageText

from database import Database
from models import Event
from notifications import NotificationDispatcher
from telegram_messages import MessagePanel


@pytest.fixture
def transport():
    bot = Mock(spec=Bot)
    bot.id = 123456
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=99))
    bot.edit_message_text = AsyncMock(return_value=True)
    return bot


async def test_repeated_content_is_local_and_new_content_edits_same_post(db, transport):
    panel = MessagePanel(db, transport)
    await panel.send_message(123, "Сьогодні")
    await panel.send_message(123, "Сьогодні")
    transport.send_message.assert_awaited_once()
    transport.edit_message_text.assert_not_awaited()
    await panel.send_message(123, "Завтра")
    assert transport.edit_message_text.await_args.kwargs["message_id"] == 99
    assert transport.edit_message_text.await_args.kwargs["text"] == "Завтра"
    await panel.send_message(123, "Завтра")
    transport.edit_message_text.assert_awaited_once()


async def test_message_identity_and_content_survive_database_restart(config, transport):
    db = Database(config.database_path)
    try:
        await MessagePanel(db, transport).send_message(123, "Сьогодні")
    finally:
        db.close()
    db = Database(config.database_path)
    try:
        panel = MessagePanel(db, transport)
        await panel.send_message(123, "Сьогодні")
        transport.edit_message_text.assert_not_awaited()
        await panel.send_message(123, "Завтра")
        transport.send_message.assert_awaited_once()
        assert transport.edit_message_text.await_args.kwargs["message_id"] == 99
    finally:
        db.close()


async def test_concurrent_commands_and_notifications_do_not_create_parallel_posts(db, transport):
    in_flight, peak = 0, 0

    async def respond(**kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return SimpleNamespace(message_id=99)

    transport.send_message.side_effect = respond
    transport.edit_message_text.side_effect = respond
    panel = MessagePanel(db, transport)
    await asyncio.gather(*(panel.send_message(123, f"Оновлення {i}") for i in range(20)))
    assert peak == 1
    transport.send_message.assert_awaited_once()
    assert transport.edit_message_text.await_count == 19
    assert all(
        call.kwargs["message_id"] == 99 for call in transport.edit_message_text.await_args_list
    )


@pytest.mark.parametrize("error_type,extra", [
    (TelegramNetworkError, {}), (TelegramServerError, {}), (TelegramForbiddenError, {}),
    (TelegramRetryAfter, {"retry_after": 30}),
    (TelegramBadRequest, {"message": "can't parse entities"}),
])
async def test_failed_edit_never_falls_back_to_send(db, transport, error_type, extra):
    panel = MessagePanel(db, transport)
    await panel.send_message(123, "Сьогодні")
    transport.edit_message_text.side_effect = error_type(
        method=EditMessageText(chat_id=123, message_id=99, text="Завтра"),
        **{"message": "Unavailable", **extra},
    )
    with pytest.raises(error_type):
        await panel.send_message(123, "Завтра")
    transport.send_message.assert_awaited_once()
    transport.edit_message_text.side_effect = None
    await panel.send_message(123, "Завтра")  # The failed edit must not update the local digest.
    assert transport.edit_message_text.await_count == 2
    assert transport.edit_message_text.await_args.kwargs["message_id"] == 99


async def test_server_not_modified_updates_local_digest_without_sending(db, transport):
    panel = MessagePanel(db, transport)
    await panel.send_message(123, "Сьогодні")
    transport.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(chat_id=123, message_id=99, text="Завтра"),
        message="Bad Request: message is not modified",
    )
    await panel.send_message(123, "Завтра")
    await panel.send_message(123, "Завтра")
    transport.send_message.assert_awaited_once()
    transport.edit_message_text.assert_awaited_once()


@pytest.mark.parametrize("error", ["message to edit not found", "message can't be edited"])
async def test_explicit_menu_can_recreate_a_removed_post_with_unchanged_content(
    db, transport, error,
):
    panel = MessagePanel(db, transport)
    await panel.send_message(123, "Меню")
    transport.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(chat_id=123, message_id=99, text="Меню"), message=error,
    )
    transport.send_message.return_value = SimpleNamespace(message_id=100)
    await panel.send_message(123, "Меню", force=True)
    assert transport.send_message.await_count == 2
    transport.edit_message_text.side_effect = None
    await panel.send_message(123, "Завтра")
    assert transport.edit_message_text.await_args.kwargs["message_id"] == 100


async def test_outbox_replaces_command_post_and_keeps_delivery_state(db, transport, now):
    panel = MessagePanel(db, transport)
    await panel.send_message(123, "Меню")
    db.enqueue([
        Event("test-warning", "morning", 123, now, now + timedelta(hours=1), "Нагадування"),
    ])
    await NotificationDispatcher(db, panel).dispatch(now)
    transport.send_message.assert_awaited_once()
    transport.edit_message_text.assert_awaited_once()
    assert transport.edit_message_text.await_args.kwargs["message_id"] == 99
    assert transport.edit_message_text.await_args.kwargs["text"] == "Нагадування"
    assert db.delivery_counts() == {"sent": 1}


async def test_distinct_chats_and_tokens_do_not_share_message_ids(db, transport):
    panel = MessagePanel(db, transport)
    await panel.send_message(123, "Меню")
    await panel.send_message(456, "Меню")
    transport.id = 654321
    await MessagePanel(db, transport).send_message(123, "Меню")
    assert transport.send_message.await_count == 3
    transport.edit_message_text.assert_not_awaited()
