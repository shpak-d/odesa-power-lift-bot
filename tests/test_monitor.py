from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import RecordingSender, StaticProvider, batch

from database import Database
from monitor import Monitor
from notifications import NotificationDispatcher, day_report, warning_events
from schedule_provider import ProviderError


def setup_monitor(config, db, make_schedule):
    sender = RecordingSender()
    provider = StaticProvider(batch(make_schedule))
    return Monitor(config, db, provider, NotificationDispatcher(db, sender)), provider, sender


async def test_initial_snapshot_silent_then_only_real_changes(config, db, make_schedule, now):
    monitor, provider, sender = setup_monitor(config, db, make_schedule)
    assert await monitor.check(now)
    await monitor.dispatcher.dispatch(now)
    assert not sender.messages
    assert await monitor.check(now + timedelta(minutes=10))
    assert not db.conn.execute("SELECT 1 FROM outbox WHERE kind='change'").fetchone()
    # Змінюється лише metadata — сповіщення не потрібне.
    provider.schedules = [
        replace(s, source_updated_at=now + timedelta(minutes=20)) for s in provider.schedules
    ]
    await monitor.check(now + timedelta(minutes=20))
    assert not db.conn.execute("SELECT 1 FROM outbox WHERE kind='change'").fetchone()
    # Змінюємо лише 4.1 на завтра.
    provider.schedules[-1] = replace(provider.schedules[-1], intervals=())
    await monitor.check(now + timedelta(minutes=30))
    await monitor.dispatcher.dispatch(now + timedelta(minutes=30))
    assert len(sender.messages) == 1
    assert "Черга: 4.1" in sender.messages[0]["text"]
    assert "25.11.2026" in sender.messages[0]["text"]
    assert "Можливий простій ліфта" in sender.messages[0]["text"]
    await monitor.check(now + timedelta(minutes=40))
    await monitor.dispatcher.dispatch(now + timedelta(minutes=40))
    assert len(sender.messages) == 1


async def test_normalized_order_and_adjacency_do_not_trigger_change(
    config,
    db,
    make_schedule,
    now,
):
    monitor, provider, _ = setup_monitor(config, db, make_schedule)
    await monitor.check(now)
    provider.schedules[0] = make_schedule(intervals=(("12:00", "14:00"), ("10:00", "12:00")))
    await monitor.check(now + timedelta(minutes=10))
    assert not db.conn.execute("SELECT 1 FROM outbox WHERE kind='change'").fetchone()


async def test_source_failure_preserves_snapshot_and_last_success(config, db, make_schedule, now):
    monitor, provider, sender = setup_monitor(config, db, make_schedule)
    await monitor.check(now)
    previous = db.get(config.operator_id, "2.1", now.date())
    success = db.get_meta("last_success")
    provider.failure = ProviderError("Джерело недоступне")
    assert not await monitor.check(now + timedelta(minutes=10))
    assert db.get(config.operator_id, "2.1", now.date()) == previous
    assert db.get_meta("last_success") == success
    assert db.get_meta("last_check") != success
    assert not sender.messages
    report = day_report(db, now.date(), config, now + timedelta(hours=1))
    assert "застаріли" in report
    assert "10:00–14:00" in report


async def test_missing_day_explicit_and_reminders_cancelled(config, db, make_schedule, now):
    monitor, provider, _ = setup_monitor(config, db, make_schedule)
    await monitor.check(now)
    assert warning_events(db, config, now)
    provider.schedules = [replace(s, status="missing", intervals=()) for s in provider.schedules]
    await monitor.check(now + timedelta(minutes=10))
    assert not warning_events(db, config, now + timedelta(minutes=10))
    report = day_report(db, now.date(), config, now + timedelta(minutes=10))
    assert "Графік не опублікований" in report
    assert "Недостатньо даних" in report
    assert db.conn.execute("SELECT count(*) FROM revisions").fetchone()[0] == 8


async def test_atomic_snapshot_and_outbox_rollback(config, db, make_schedule, now):
    monitor, provider, _ = setup_monitor(config, db, make_schedule)
    await monitor.check(now)
    before = db.get(config.operator_id, "2.1", now.date())

    def fail(_):
        raise RuntimeError("Test transaction rollback")

    with pytest.raises(RuntimeError):
        db.apply_schedules([replace(before, intervals=())], now, fail)
    assert db.get(config.operator_id, "2.1", now.date()) == before


async def test_morning_report_once_per_chat_after_restart(config, make_schedule, now):
    database = Database(config.database_path)
    monitor, _, sender = setup_monitor(config, database, make_schedule)
    eight = now + timedelta(hours=1)
    await monitor.check(eight)
    await monitor.tick(eight - timedelta(seconds=1))
    assert not sender.messages
    await monitor.tick(eight)
    await monitor.tick(eight + timedelta(seconds=5))
    assert len(sender.messages) == 1
    assert "Електроенергія — 24.11.2026" in sender.messages[0]["text"]
    assert sender.messages[0]["disable_notification"] is False
    database.close()
    database = Database(config.database_path)
    try:
        monitor, _, after_restart = setup_monitor(config, database, make_schedule)
        await monitor.tick(eight + timedelta(minutes=1))
        assert not after_restart.messages
    finally:
        database.close()


@pytest.mark.parametrize("lift_only", [False, True])
def test_daily_report_omits_forecast_footer(config, db, make_schedule, now, lift_only):
    db.apply_schedules(batch(make_schedule), now, lambda _: [])
    text = day_report(db, now.astimezone(config.timezone).date(), config, now, lift_only=lift_only)
    assert "10:00–14:00" in text
    assert "Це прогноз за графіком" not in text
    assert "Аварійні відключення можливі поза планом" not in text
