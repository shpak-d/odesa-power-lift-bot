import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramConflictError, TelegramRetryAfter
from aiogram.methods import GetMe, GetUpdates, GetWebhookInfo, SendMessage, SetMyCommands
from aiogram.types import BotCommand, BotFullInfo, WebhookInfo

from main import GuardedBot


@pytest.fixture
async def guarded_bot(config, db):
    bot = GuardedBot(config.bot_token, db=db, conflict=asyncio.Event())
    try:
        yield bot
    finally:
        await bot.session.close()


@pytest.fixture
def bot_info():
    return BotFullInfo(
        id=123456,
        is_bot=True,
        first_name="Test bot",
        can_join_groups=True,
        can_read_all_group_messages=False,
        supports_guest_queries=False,
        supports_inline_queries=False,
        needs_inline_query_location=False,
        can_connect_to_business=False,
        has_main_web_app=False,
        has_topics_enabled=False,
        allows_users_to_create_topics=False,
        has_topic_sidebar=False,
        can_manage_bots=False,
        supports_join_request_queries=False,
    )


@pytest.fixture(params=["get_me", "get_webhook_info", "set_my_commands"])
def startup_call(request, bot_info):
    if request.param == "get_me":
        return (
            request.param,
            {},
            GetMe(),
            bot_info,
        )
    if request.param == "get_webhook_info":
        return (
            request.param,
            {},
            GetWebhookInfo(),
            WebhookInfo(
                url="",
                has_custom_certificate=False,
                pending_update_count=0,
            ),
        )
    commands = [BotCommand(command="today", description="Графік на сьогодні")]
    return request.param, {"commands": commands}, SetMyCommands(commands=commands), True


@pytest.mark.parametrize("delays", [(7,), (2, 11, 120)])
async def test_startup_retries_every_429_with_server_delay(
    guarded_bot,
    startup_call,
    delays,
    monkeypatch,
    caplog,
):
    name, kwargs, method, result = startup_call
    attempts = []
    responses = iter(
        [
            *(
                TelegramRetryAfter(method=method, message="Flood control", retry_after=delay)
                for delay in delays
            ),
            result,
        ]
    )

    async def request(received, request_timeout=None):
        assert received == method
        assert request_timeout == 20
        attempts.append("request")
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    async def sleep(delay):
        attempts.append(delay)

    monkeypatch.setattr(Bot, "__call__", AsyncMock(side_effect=request))
    pause = AsyncMock(side_effect=sleep)
    monkeypatch.setattr("main.asyncio.sleep", pause)
    with caplog.at_level(logging.WARNING, logger="main"):
        actual = await getattr(guarded_bot, name)(**kwargs, request_timeout=20)

    assert actual == result
    assert attempts == [item for delay in delays for item in ("request", delay)] + ["request"]
    assert [call.args[0] for call in pause.await_args_list] == list(delays)
    assert not guarded_bot.conflict.is_set()
    assert len(caplog.records) == len(delays)
    for record, delay in zip(caplog.records, delays, strict=True):
        assert "Telegram 429" in record.message
        assert method.__api_method__ in record.message
        assert f"через {delay} с" in record.message


@pytest.mark.parametrize("after_rate_limit", [False, True])
async def test_startup_conflict_propagates_without_retry(
    guarded_bot,
    startup_call,
    after_rate_limit,
    monkeypatch,
):
    name, kwargs, method, _ = startup_call
    conflict = TelegramConflictError(method=method, message="Other polling instance")
    responses = [conflict]
    if after_rate_limit:
        responses.insert(0, TelegramRetryAfter(method=method, message="Retry", retry_after=5))
    request = AsyncMock(side_effect=responses)
    pause = AsyncMock()
    monkeypatch.setattr(Bot, "__call__", request)
    monkeypatch.setattr("main.asyncio.sleep", pause)

    with pytest.raises(TelegramConflictError) as caught:
        await getattr(guarded_bot, name)(**kwargs, request_timeout=20)

    assert caught.value is conflict
    assert guarded_bot.conflict.is_set()
    assert request.await_count == len(responses)
    assert pause.await_count == int(after_rate_limit)


@pytest.mark.parametrize("method", [GetUpdates(), SendMessage(chat_id=123, text="Test")])
async def test_other_methods_keep_existing_rate_limit_handling(guarded_bot, method, monkeypatch):
    error = TelegramRetryAfter(method=method, message="Retry", retry_after=30)
    request, pause = AsyncMock(side_effect=error), AsyncMock()
    monkeypatch.setattr(Bot, "__call__", request)
    monkeypatch.setattr("main.asyncio.sleep", pause)

    with pytest.raises(TelegramRetryAfter) as caught:
        await guarded_bot(method)

    assert caught.value is error
    request.assert_awaited_once()
    pause.assert_not_awaited()
    assert not guarded_bot.conflict.is_set()


async def test_implicit_get_me_used_by_polling_also_retries(guarded_bot, bot_info, monkeypatch):
    request = AsyncMock(
        side_effect=[
            TelegramRetryAfter(method=GetMe(), message="Retry", retry_after=9),
            bot_info,
        ]
    )
    pause = AsyncMock()
    monkeypatch.setattr(Bot, "__call__", request)
    monkeypatch.setattr("main.asyncio.sleep", pause)

    assert await guarded_bot.me() == bot_info
    assert await guarded_bot.me() == bot_info
    assert request.await_count == 2  # Друга me() читає кеш aiogram.
    pause.assert_awaited_once_with(9)


async def test_startup_retry_wait_remains_cancellable(guarded_bot, monkeypatch):
    waiting = asyncio.Event()

    async def sleep(delay):
        assert delay == 300
        waiting.set()
        await asyncio.Event().wait()

    request = AsyncMock(
        side_effect=TelegramRetryAfter(
            method=GetMe(),
            message="Retry",
            retry_after=300,
        )
    )
    monkeypatch.setattr(Bot, "__call__", request)
    monkeypatch.setattr("main.asyncio.sleep", sleep)
    task = asyncio.create_task(guarded_bot.get_me())
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    request.assert_awaited_once()
