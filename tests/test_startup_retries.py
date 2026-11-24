import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetMe, GetUpdates, GetWebhookInfo, SendMessage, SetMyCommands
from aiogram.types import BotCommand, BotFullInfo

from main import run, telegram_loop
from telegram_client import GuardedBot, TelegramCooldown


@pytest.fixture
async def guarded_bot(config, db):
    bot = GuardedBot(config.bot_token, db=db, conflict=asyncio.Event())
    try:
        yield bot
    finally:
        await bot.session.close()


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        value = 1000.0
        delays = None

        def __init__(self):
            self.delays = []

        async def sleep(self, delay):
            self.delays.append(delay)
            self.value += delay

    result = Clock()
    monkeypatch.setattr("telegram_client.monotonic", lambda: result.value)
    monkeypatch.setattr("telegram_client.random.uniform", lambda *_: 2)
    # GuardedBot is created before/after this fixture depending on the test.
    monkeypatch.setattr("telegram_client.asyncio.sleep", result.sleep)
    return result


@pytest.fixture(params=[
    GetMe(), GetWebhookInfo(),
    SetMyCommands(commands=[BotCommand(command="today", description="Today")]),
    GetUpdates(timeout=50),
])
def read_method(request):
    return request.param


def clear_initial_cooldown(bot):
    # The fake monotonic clock starts at 1000, unlike the process monotonic clock.
    bot._retry_at = 0


@pytest.mark.parametrize("error_type", [TelegramNetworkError, TelegramServerError])
async def test_backoff_enters_sparse_recovery_until_success(
    guarded_bot, read_method, clock, monkeypatch, error_type,
):
    clear_initial_cooldown(guarded_bot)
    error = error_type(method=read_method, message="Unavailable")
    request = AsyncMock(side_effect=[*[error] * 9, True])
    monkeypatch.setattr(Bot, "__call__", request)
    assert await guarded_bot(read_method, request_timeout=20) is True
    assert clock.delays == [12, 22, 42, 82, 162, 302, 902, 902, 902]
    assert request.await_count == 10
    assert guarded_bot.failures[read_method.__api_method__] == 0
    # A successful request resets this method's consecutive-failure counter.
    request.side_effect = [error, True]
    assert await guarded_bot(read_method) is True
    assert clock.delays[-1] == 12


async def test_429_respects_server_minimum_and_does_not_reset_backoff(
    guarded_bot, read_method, clock, monkeypatch,
):
    clear_initial_cooldown(guarded_bot)
    error = TelegramNetworkError(method=read_method, message="Timeout")
    request = AsyncMock(side_effect=[
        error,
        TelegramRetryAfter(method=read_method, message="Flood", retry_after=137),
        error,
        TelegramRetryAfter(method=read_method, message="Flood", retry_after=3),
        True,
    ])
    monkeypatch.setattr(Bot, "__call__", request)
    assert await guarded_bot(read_method) is True
    assert clock.delays == [12, 139, 42, 82]


@pytest.mark.parametrize("error_type", [TelegramConflictError, TelegramUnauthorizedError])
async def test_permanent_errors_propagate_without_retry(
    guarded_bot, read_method, clock, monkeypatch, error_type,
):
    clear_initial_cooldown(guarded_bot)
    error = error_type(method=read_method, message="Stop")
    request = AsyncMock(side_effect=error)
    monkeypatch.setattr(Bot, "__call__", request)
    with pytest.raises(error_type):
        await guarded_bot(read_method)
    request.assert_awaited_once()
    assert not clock.delays
    assert guarded_bot.conflict.is_set() == (error_type is TelegramConflictError)


@pytest.mark.parametrize("error_type", [TelegramRetryAfter, TelegramNetworkError])
async def test_send_failure_pauses_all_requests_without_replaying_send(
    guarded_bot, clock, monkeypatch, error_type,
):
    clear_initial_cooldown(guarded_bot)
    method = SendMessage(chat_id=123, text="private message")
    kwargs = {"retry_after": 30} if error_type is TelegramRetryAfter else {}
    error = error_type(method=method, message="Failure", **kwargs)
    request = AsyncMock(side_effect=[error, True])
    monkeypatch.setattr(Bot, "__call__", request)
    with pytest.raises(error_type):
        await guarded_bot(method)
    with pytest.raises(TelegramCooldown):
        await guarded_bot(method)
    request.assert_awaited_once()  # Local rejection is not another HTTP request.
    assert not clock.delays
    assert await guarded_bot(GetUpdates(timeout=50)) is True
    assert clock.delays == [32 if kwargs else 12]
    assert request.await_count == 2


async def test_shared_cooldown_survives_process_restart(config, db, monkeypatch):
    db.set_meta("telegram_retry_at", (datetime.now(UTC) + timedelta(minutes=5)).isoformat())
    bot = GuardedBot(config.bot_token, db=db, conflict=asyncio.Event())
    request = AsyncMock()
    monkeypatch.setattr(Bot, "__call__", request)
    try:
        with pytest.raises(TelegramCooldown) as caught:
            await bot.send_message(123, "Test")
        assert 295 <= caught.value.retry_after <= 300
        request.assert_not_awaited()
    finally:
        await bot.session.close()


async def test_retry_sleep_is_cancellable(guarded_bot, monkeypatch):
    sleeping = asyncio.Event()

    async def sleep(delay):
        assert delay >= 300
        sleeping.set()
        await asyncio.Event().wait()

    request = AsyncMock(side_effect=TelegramRetryAfter(
        method=GetMe(), message="Flood", retry_after=300,
    ))
    monkeypatch.setattr(Bot, "__call__", request)
    monkeypatch.setattr("telegram_client.asyncio.sleep", sleep)
    task = asyncio.create_task(guarded_bot.me())
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    request.assert_awaited_once()


async def test_no_parallel_get_me_or_polling_requests(guarded_bot, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    in_flight, peak = 0, 0

    async def request(method, request_timeout=None):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        entered.set()
        await release.wait()
        in_flight -= 1
        return []

    mock = AsyncMock(side_effect=request)
    monkeypatch.setattr(Bot, "__call__", mock)
    polls = [asyncio.create_task(guarded_bot(GetUpdates(timeout=50))) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert mock.await_count == 1
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*polls), timeout=1)
    assert peak == 1
    # Concurrent consumers of me() share one cache fill.
    assert await asyncio.gather(guarded_bot.me(), guarded_bot.me()) == [[], []]
    assert mock.await_count == 3


async def test_initialization_fills_cache_for_real_aiogram_polling(
    guarded_bot, config, db, monkeypatch,
):
    # model_construct keeps this test independent of unrelated Bot API capability fields.
    info = BotFullInfo.model_construct(id=123456, is_bot=True, first_name="Test bot")
    calls, polling, scheduler = [], asyncio.Event(), asyncio.Event()

    async def request(method, request_timeout=None):
        calls.append(method.__api_method__)
        if isinstance(method, GetMe):
            return info
        if isinstance(method, GetWebhookInfo):
            from aiogram.types import WebhookInfo

            return WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)
        if isinstance(method, GetUpdates):
            assert method.timeout == 50
            assert request_timeout > method.timeout
            assert method.allowed_updates == ["message", "callback_query"]
            polling.set()
            await asyncio.Event().wait()
        if isinstance(method, SetMyCommands):
            assert {command.command for command in method.commands} == {
                "start", "menu", "today", "tomorrow", "lift", "status", "ping", "help",
            }
        return True

    async def notification_loop():
        scheduler.set()
        await asyncio.Event().wait()

    monitor = AsyncMock()
    monitor.notification_loop.side_effect = notification_loop
    monkeypatch.setattr(Bot, "__call__", AsyncMock(side_effect=request))
    task = asyncio.create_task(telegram_loop(guarded_bot, config, db, monitor))
    try:
        await asyncio.wait_for(polling.wait(), timeout=2)
        await asyncio.wait_for(scheduler.wait(), timeout=1)
        assert calls == ["getMe", "getWebhookInfo", "setMyCommands", "getUpdates"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    assert not any(
        t.get_name() in {"notifications", "telegram-polling"} and not t.done()
        for t in asyncio.all_tasks()
    )


async def test_startup_backoff_does_not_block_monitor_or_shutdown(config, monkeypatch):
    monitor_running = asyncio.Event()

    async def monitor_loop(self):
        monitor_running.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("main.Monitor.check", AsyncMock(return_value=False))
    monkeypatch.setattr("main.Monitor.monitor_loop", monitor_loop)
    monkeypatch.setattr(Bot, "__call__", AsyncMock(side_effect=TelegramNetworkError(
        method=GetMe(), message="Unavailable",
    )))
    task = asyncio.create_task(run(replace(config, app_mode="telegram")))
    try:
        await asyncio.wait_for(monitor_running.wait(), timeout=3)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)


async def test_startup_routes_morning_alerts_separately_from_menu(config, now, monkeypatch):
    send = AsyncMock(side_effect=[
        SimpleNamespace(message_id=99), SimpleNamespace(message_id=100),
    ])
    edit = AsyncMock(return_value=True)

    async def telegram_loop(bot, config, db, monitor, panel):
        assert monitor.dispatcher.sender is bot
        await panel.send_message(123, "Меню")
        await monitor.tick(now + timedelta(hours=1))
        await panel.send_message(123, "Завтра")

    monkeypatch.setattr("main.Monitor.check", AsyncMock(return_value=False))
    monkeypatch.setattr("main.telegram_loop", telegram_loop)
    monkeypatch.setattr(Bot, "send_message", send)
    monkeypatch.setattr(Bot, "edit_message_text", edit)
    await asyncio.wait_for(run(replace(config, app_mode="telegram")), timeout=3)
    assert send.await_count == 2
    assert "Електроенергія" in send.await_args_list[1].kwargs["text"]
    assert send.await_args_list[1].kwargs["disable_notification"] is False
    edit.assert_awaited_once()
    assert edit.await_args.kwargs["message_id"] == 99
