from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from config import Config
from database import Database
from lift import interval_from_clock
from models import DaySchedule


@pytest.fixture
def config(tmp_path):
    return Config(
        bot_token="123456:TEST_ONLY",
        allowed_chat_ids=(123,),
        database_path=tmp_path / "bot.sqlite3",
    )


@pytest.fixture
def now():
    return datetime(2026, 11, 24, 5, 0, tzinfo=UTC)  # 07:00 Київ


@pytest.fixture(autouse=True)
def notification_wall_clock(now, monkeypatch):
    # The outbox also checks wall time after sends. Historical fixtures must not
    # expire simply because pytest runs later in the day or on another date.
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr("notifications.datetime", FixedDateTime)


@pytest.fixture
def db(config):
    result = Database(config.database_path)
    yield result
    result.close()


@pytest.fixture
def make_schedule(config, now):
    def make(
        queue="2.1",
        intervals=(("10:00", "14:00"),),
        day=date(2026, 11, 24),
        status="published",
        possible=(),
        fetched_at=None,
    ):
        return DaySchedule(
            config.operator_id,
            queue,
            day,
            status,
            tuple(interval_from_clock(day, a, b, config.timezone) for a, b in intervals),
            tuple(interval_from_clock(day, a, b, config.timezone) for a, b in possible),
            fetched_at or now,
            now,
            "https://www.dtek-oem.com.ua/ua/shutdowns",
        )

    return make


class RecordingSender:
    def __init__(self, failure=None):
        self.messages = []
        self.failure = failure

    async def send_message(self, **kwargs):
        if self.failure:
            raise self.failure
        self.messages.append(kwargs)


class StaticProvider:
    def __init__(self, schedules):
        self.schedules = schedules
        self.failure = None

    async def fetch(self, now):
        if self.failure:
            raise self.failure
        return [replace(s, fetched_at=now) for s in self.schedules]


def batch(make_schedule):
    today = date(2026, 11, 24)
    return [
        make_schedule(queue=q, day=today + timedelta(days=d))
        for q in ("2.1", "4.1")
        for d in (0, 1)
    ]
