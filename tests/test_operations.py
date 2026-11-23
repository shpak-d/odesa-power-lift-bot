from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType
from aiogram.types import Chat, Message

from bot import AllowedChatsMiddleware
from config import Config
from healthcheck import healthy
from instance_lock import InstanceLock


def test_config_requires_credentials_and_never_repr_token(monkeypatch, config):
    monkeypatch.setenv("BOT_TOKEN", "")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "")
    with pytest.raises(ValueError):
        Config.from_env()
    assert "TEST_ONLY" not in repr(config)


def test_config_operator_guard(monkeypatch):
    monkeypatch.setenv("OPERATOR_ID", "odeska_oblast_uz")
    monkeypatch.setenv("SCHEDULE_PROVIDER", "dtek_html")
    with pytest.raises(ValueError, match="Укрзалізниці"):
        Config.from_env(require_telegram=False)


def test_health_is_read_only_and_independent_of_source_failure(db, config, now):
    for key in ("heartbeat_monitor", "heartbeat_scheduler", "heartbeat_polling"):
        db.set_meta(key, now.isoformat())
    db.set_meta("last_error", "Джерело недоступне")
    assert healthy(config.database_path, now)
    assert not healthy(config.database_path, now + timedelta(minutes=2))
    assert not healthy(config.database_path.parent / "missing.db", now)


def test_two_instances_same_token_refused(tmp_path):
    with InstanceLock("test-token-not-real", tmp_path / "one" / "db.sqlite3"):
        with pytest.raises(RuntimeError):
            with InstanceLock("test-token-not-real", tmp_path / "two" / "db.sqlite3"):
                pytest.fail("Second instance acquired token lock")
    # Після завершення власника файл lock не заважає перезапуску.
    with InstanceLock("test-token-not-real", tmp_path / "one" / "db.sqlite3"):
        pass


async def test_chat_access_filter(now):
    handler = AsyncMock()
    middleware = AllowedChatsMiddleware((123,))
    denied = Message(message_id=1, date=now, chat=Chat(id=999, type=ChatType.PRIVATE))
    await middleware(handler, denied, {})
    handler.assert_not_awaited()
    allowed = denied.model_copy(update={"chat": Chat(id=123, type=ChatType.PRIVATE)})
    await middleware(handler, allowed, {})
    handler.assert_awaited_once()
