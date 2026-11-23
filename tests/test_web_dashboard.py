import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import StaticProvider, batch

import main
from config import Config
from healthcheck import healthy
from instance_lock import InstanceLock
from monitor import Monitor
from schedule_provider import ProviderError, parse_dtek
from web_dashboard import build_dashboard, create_app, serve


@pytest.fixture
async def client(config, db, now):
    async with TestClient(TestServer(create_app(config, db, clock=lambda: now))) as result:
        yield result


def save(db, make_schedule, now):
    db.apply_schedules(batch(make_schedule), now, lambda _: [])


def test_web_config_starts_without_telegram_credentials(monkeypatch):
    monkeypatch.setenv("APP_MODE", "web")
    monkeypatch.setenv("BOT_TOKEN", "")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "unused-old-value")
    config = Config.from_env()
    assert config.app_mode == "web"
    assert not config.bot_token and not config.allowed_chat_ids
    assert config.web_port == 8080


@pytest.mark.parametrize(
    "key,value", [("APP_MODE", "invalid"), ("WEB_PORT", "65536"), ("WEB_PORT", "0")]
)
def test_invalid_web_config_rejected(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Config.from_env()


async def test_main_web_mode_never_creates_a_telegram_bot(config, monkeypatch):
    serve = AsyncMock()
    bot = Mock(side_effect=AssertionError("Telegram must stay disabled"))
    monkeypatch.setattr("web_dashboard.serve", serve)
    monkeypatch.setattr(main, "GuardedBot", bot)
    await main.run(replace(config, app_mode="web", bot_token="", allowed_chat_ids=()))
    serve.assert_awaited_once()
    bot.assert_not_called()


async def test_web_monitor_saves_changes_without_creating_telegram_events(
    config,
    db,
    make_schedule,
    now,
):
    provider = StaticProvider(batch(make_schedule))
    monitor = Monitor(config, db, provider)
    assert await monitor.check(now)
    provider.schedules[0] = replace(provider.schedules[0], intervals=())
    assert await monitor.check(now + timedelta(minutes=10))
    await monitor.tick(now + timedelta(hours=1))
    assert db.get(config.operator_id, config.queue_home, now.date()).intervals == ()
    assert not db.delivery_counts()


async def test_dashboard_missing_data_is_unknown(client):
    response = await client.get("/api/dashboard")
    data = await response.json()
    assert response.status == 200
    assert data["monitoring"]["state"] == "loading"
    assert len(data["days"]) == 2
    assert not data["days"][0]["lift"]["known"]
    assert data["days"][0]["home"]["status"] == "missing"
    assert response.headers["Cache-Control"] == "no-store"


async def test_real_dtek_snapshot_produces_expected_lift_forecast(config, db):
    now = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)
    path = Path(__file__).parent / "fixtures" / "dtek_2026-11-23.html"
    db.apply_schedules(parse_dtek(path.read_text(encoding="utf-8"), config, now), now, lambda _: [])
    result = build_dashboard(db, config, now)
    tomorrow = result["days"][1]
    assert tomorrow["date"] == "2026-11-24"
    assert tomorrow["lift"]["known"] and tomorrow["lift"]["fresh"]
    assert [(i["start_label"], i["end_label"]) for i in tomorrow["lift"]["intervals"]] == [
        ("12:00", "13:30"),
    ]
    assert tomorrow["lift"]["intervals"][0]["minutes"] == 90


async def test_api_never_exposes_credentials_or_database_path(
    client, db, config, make_schedule, now
):
    save(db, make_schedule, now)
    response = await client.get("/api/dashboard")
    body = await response.text()
    assert config.bot_token not in body
    assert str(config.database_path) not in body
    data = await response.json()
    assert data["days"][0]["home"]["queue"] == "2.1"
    assert data["days"][0]["secondary"]["queue"] == "4.1"


def test_stale_schedule_keeps_intervals_but_marks_forecast_old(db, config, make_schedule, now):
    save(db, make_schedule, now)
    data = build_dashboard(db, config, now + timedelta(hours=1))
    assert data["monitoring"]["state"] == "stale"
    assert data["days"][0]["home"]["stale"]
    assert data["days"][0]["lift"]["known"]
    assert not data["days"][0]["lift"]["fresh"]
    assert data["days"][0]["lift"]["intervals"]


async def test_source_failure_keeps_web_data(config, db, make_schedule, now):
    provider = StaticProvider(batch(make_schedule))
    monitor = Monitor(config, db, provider)
    await monitor.check(now)
    before = build_dashboard(db, config, now)["days"][0]["home"]
    provider.failure = ProviderError("ДТЕК тимчасово недоступний")
    assert not await monitor.check(now + timedelta(minutes=10))
    after = build_dashboard(db, config, now + timedelta(minutes=10))
    assert after["days"][0]["home"] == before
    assert after["monitoring"]["state"] == "error"
    assert after["monitoring"]["error"] == "ДТЕК тимчасово недоступний"


def test_empty_published_and_missing_schedule_are_distinct(db, config, make_schedule, now):
    rows = [replace(s, intervals=()) for s in batch(make_schedule)]
    db.apply_schedules(rows, now, lambda _: [])
    empty = build_dashboard(db, config, now)["days"][0]
    assert empty["lift"]["known"] and empty["lift"]["intervals"] == []
    rows[2] = replace(rows[2], status="missing")
    db.apply_schedules(rows, now, lambda _: [])
    unknown = build_dashboard(db, config, now)["days"][0]
    assert not unknown["lift"]["known"]


def test_web_timeline_clips_midnight_and_marks_past(db, config, make_schedule, now):
    rows = [make_schedule(q, (("00:00", "03:00"), ("23:00", "02:00"))) for q in config.queues]
    db.apply_schedules(rows, now, lambda _: [])
    day = build_dashboard(db, config, now)["days"][0]
    assert day["home"]["intervals"][0]["phase"] == "past"
    assert day["home"]["intervals"][1]["end_label"] == "24:00"
    assert day["lift"]["intervals"][1]["minutes"] == 60


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_web_timeline_matches_actual_dst_day(db, config, make_schedule, day):
    now = datetime.combine(day, datetime.min.time(), config.timezone).astimezone(UTC)
    rows = [make_schedule(q, (("00:00", "24:00"),), day=day, fetched_at=now) for q in config.queues]
    db.apply_schedules(rows, now, lambda _: [])
    result = build_dashboard(db, config, now)["days"][0]
    assert result["home"]["intervals"][0]["width"] == 100
    assert result["ticks"][-1] == {"label": "24:00", "left": 100}
    assert result["ticks"][1]["left"] != 25


@pytest.mark.parametrize(
    "path,mime",
    [("/", "text/html"), ("/static/style.css", "text/css"), ("/static/app.js", "javascript")],
)
async def test_page_and_assets_are_served(client, path, mime):
    response = await client.get(path)
    assert response.status == 200
    assert mime in response.content_type
    if path == "/":
        assert "Світло й ліфт" in await response.text()


async def test_web_is_read_only_and_does_not_serve_private_files(client):
    assert (await client.post("/api/dashboard")).status == 405
    for path in ("/.env", "/static/config.py", "/static/.env"):
        assert (await client.get(path)).status == 404


async def test_web_health_has_no_dependency_on_polling(client, db, config, now):
    assert (await client.get("/healthz")).status == 503
    for key in ("heartbeat_monitor", "heartbeat_web"):
        db.set_meta(key, now.isoformat())
    assert (await client.get("/healthz")).status == 200
    assert healthy(config.database_path, now, mode="web")
    assert not healthy(config.database_path, now, mode="telegram")


def test_web_and_telegram_cannot_write_same_database(config):
    with InstanceLock("", config.database_path):
        with pytest.raises(RuntimeError):
            with InstanceLock("test-token", config.database_path):
                pytest.fail("Both application modes acquired the same database")


@pytest.mark.parametrize("source_fails", [False, True])
async def test_running_web_service_is_healthy_without_telegram(
    config,
    make_schedule,
    monkeypatch,
    source_fails,
):
    stop, fetched = asyncio.Event(), asyncio.Event()

    async def fetch(self, now):
        fetched.set()
        if source_fails:
            raise ProviderError("Джерело недоступне")
        today = now.astimezone(config.timezone).date()
        return [
            make_schedule(q, day=today + timedelta(days=d), fetched_at=now)
            for q in config.queues
            for d in (0, 1)
        ]

    monkeypatch.setattr("web_dashboard.HttpScheduleProvider.fetch", fetch)
    task = asyncio.create_task(serve(replace(config, web_port=0, bot_token=""), stop=stop))
    try:
        await asyncio.wait_for(fetched.wait(), timeout=3)
        assert healthy(config.database_path, mode="web")
        assert not task.done()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=3)
