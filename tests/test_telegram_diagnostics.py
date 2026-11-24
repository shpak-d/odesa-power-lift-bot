import asyncio
import logging

import pytest
from aiogram import Bot
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiohttp import web
from aiohttp.test_utils import TestServer

from telegram_client import DiagnosticSession, GuardedBot, TelegramCooldown


def api_session(server, *, timeout=1):
    return DiagnosticSession(
        api=TelegramAPIServer.from_base(str(server.make_url("/")).rstrip("/")),
        timeout=timeout,
    )


@pytest.mark.parametrize("status,error", [
    (200, None), (429, TelegramRetryAfter), (502, TelegramServerError),
])
async def test_real_http_attempts_record_status_and_never_log_secrets(
    config, caplog, status, error,
):
    calls = []

    async def handle(request):
        calls.append(request.match_info["method"])
        if status == 502:
            return web.Response(status=502, text="<html>private upstream diagnostic</html>")
        if status == 429:
            return web.json_response({
                "ok": False, "error_code": 429, "description": "private error body",
                "parameters": {"retry_after": 32},
            }, status=429)
        return web.json_response({"ok": True, "result": {
            "url": "", "has_custom_certificate": False, "pending_update_count": 0,
        }})

    app = web.Application()
    app.router.add_post("/bot{token}/{method}", handle)
    async with TestServer(app) as server:
        session = api_session(server)
        bot = Bot(config.bot_token, session=session)
        try:
            with caplog.at_level(logging.INFO, logger="telegram_client"):
                if error:
                    with pytest.raises(error):
                        await bot.get_webhook_info()
                else:
                    assert (await bot.get_webhook_info()).url == ""
                session.log_summary()
            assert calls == ["getWebhookInfo"]
            assert session.total == 1 and session.in_flight == 0
            assert not session.methods  # The reporting window was reset.
        finally:
            await session.close()
    assert f"http_status={status}" in caplog.text
    assert "method=getWebhookInfo attempt=1" in caplog.text
    assert "requests=1" in caplog.text
    assert "requests_per_minute=" in caplog.text
    assert "duration_s=" in caplog.text
    assert f"retry_after={32 if status == 429 else None}" in caplog.text
    assert config.bot_token not in caplog.text
    assert "private" not in caplog.text


async def test_timeout_is_an_attempt_with_no_invented_http_status(config, caplog):
    async def handle(request):
        await asyncio.sleep(0.1)
        return web.json_response({"ok": True, "result": True})

    app = web.Application()
    app.router.add_post("/bot{token}/{method}", handle)
    async with TestServer(app) as server:
        session = api_session(server, timeout=0.01)
        bot = Bot(config.bot_token, session=session)
        try:
            with caplog.at_level(logging.INFO, logger="telegram_client"):
                with pytest.raises(TelegramNetworkError):
                    await bot.get_webhook_info()
                session.log_summary()
            assert session.total == 1
        finally:
            await session.close()
    assert "http_status=None" in caplog.text
    assert "outcome=TelegramNetworkError" in caplog.text
    assert "requests=1" in caplog.text


async def test_local_cooldown_makes_no_http_request(config, db, caplog):
    calls = []

    async def handle(request):
        calls.append(request.match_info["method"])
        return web.json_response({
            "ok": False, "error_code": 429, "description": "Flood control",
            "parameters": {"retry_after": 30},
        }, status=429)

    app = web.Application()
    app.router.add_post("/bot{token}/{method}", handle)
    async with TestServer(app) as server:
        session = api_session(server)
        bot = GuardedBot(config.bot_token, db=db, conflict=asyncio.Event(), session=session)
        try:
            with caplog.at_level(logging.INFO, logger="telegram_client"):
                with pytest.raises(TelegramRetryAfter):
                    await bot.send_message(123, "private message")
                with pytest.raises(TelegramCooldown):
                    await bot.send_message(123, "private message")
                session.log_summary()
            assert calls == ["sendMessage"]
            assert session.total == 1
            assert "methods={'sendMessage': 1}" in caplog.text
        finally:
            await session.close()


async def test_parallel_responses_keep_their_own_status(config, caplog):
    async def handle(request):
        if request.match_info["method"] == "getWebhookInfo":
            await asyncio.sleep(0.03)
            return web.json_response({"ok": True, "result": {
                "url": "", "has_custom_certificate": False, "pending_update_count": 0,
            }})
        return web.json_response({
            "ok": False, "error_code": 429, "description": "Flood control",
            "parameters": {"retry_after": 7},
        }, status=429)

    app = web.Application()
    app.router.add_post("/bot{token}/{method}", handle)
    async with TestServer(app) as server:
        session = api_session(server)
        bot = Bot(config.bot_token, session=session)
        try:
            with caplog.at_level(logging.INFO, logger="telegram_client"):
                results = await asyncio.gather(
                    bot.get_webhook_info(), bot.send_chat_action(123, "typing"),
                    return_exceptions=True,
                )
                session.log_summary()
            assert results[0].url == ""
            assert isinstance(results[1], TelegramRetryAfter)
        finally:
            await session.close()
    ends = [r.message for r in caplog.records if "telegram_request_end" in r.message]
    assert any("method=getWebhookInfo" in r and "http_status=200" in r for r in ends)
    assert any("method=sendChatAction" in r and "http_status=429" in r for r in ends)
    assert "peak_in_flight=2" in caplog.text
