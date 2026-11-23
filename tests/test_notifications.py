from dataclasses import replace
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from conftest import RecordingSender, StaticProvider, batch

from database import Database
from monitor import Monitor
from notifications import NotificationDispatcher, predicted_lift, warning_events


def save(db, make_schedule, now):
    db.apply_schedules(batch(make_schedule), now, lambda _: [])


async def test_warning_once_after_restart(config, make_schedule, now):
    db = Database(config.database_path)
    save(db, make_schedule, now)
    due = now + timedelta(hours=2, minutes=45)  # 09:45, за 15 хв до 10:00
    # Остання успішна перевірка повинна бути свіжою.
    schedules = [replace(s, fetched_at=due) for s in batch(make_schedule)]
    db.apply_schedules(schedules, due, lambda _: [])
    events = warning_events(db, config, due)
    db.reconcile_warnings(events, due)
    sender = RecordingSender()
    await NotificationDispatcher(db, sender).dispatch(due)
    assert len(sender.messages) == 2  # квартира + ліфт
    db.close()
    db = Database(config.database_path)
    try:
        db.reconcile_warnings(warning_events(db, config, due), due)
        await NotificationDispatcher(db, sender).dispatch(due)
        assert len(sender.messages) == 2
    finally:
        db.close()


def test_schedule_change_cancels_old_and_restores_new_reminders(config, db, make_schedule, now):
    save(db, make_schedule, now)
    db.reconcile_warnings(warning_events(db, config, now), now)
    old_keys = {r[0] for r in db.conn.execute("SELECT event_key FROM outbox")}
    updated = [
        replace(s, intervals=make_schedule(intervals=(("11:00", "15:00"),), day=s.day).intervals)
        for s in batch(make_schedule)
    ]
    db.apply_schedules(updated, now, lambda _: [])
    db.reconcile_warnings(warning_events(db, config, now), now)
    assert all(
        r[0] == "cancelled"
        for r in db.conn.execute(
            "SELECT status FROM outbox WHERE event_key IN ({})".format(
                ",".join("?" for _ in old_keys)
            ),
            tuple(old_keys),
        )
    )
    assert db.delivery_counts()["pending"] == 4


def test_changed_end_does_not_repeat_same_start(config, db, make_schedule, now):
    save(db, make_schedule, now)
    events = warning_events(db, config, now)
    event = next(e for e in events if e.kind == "warning_home")
    db.enqueue([event])
    assert db.claim(event.key, event.chat_id)
    db.finish(event.key, event.chat_id, "sent", now)
    updated = [
        replace(s, intervals=make_schedule(intervals=(("10:00", "15:00"),), day=s.day).intervals)
        for s in batch(make_schedule)
    ]
    db.apply_schedules(updated, now, lambda _: [])
    db.reconcile_warnings(warning_events(db, config, now), now)
    assert (
        db.conn.execute(
            "SELECT status FROM outbox WHERE event_key=? AND chat_id=?", (event.key, event.chat_id)
        ).fetchone()[0]
        == "sent"
    )


def test_no_past_or_stale_reminders(config, db, make_schedule, now):
    save(db, make_schedule, now)
    assert warning_events(db, config, now)
    assert not warning_events(db, config, now + timedelta(hours=1))
    later = now + timedelta(hours=8)
    db.apply_schedules(
        [replace(s, fetched_at=later) for s in batch(make_schedule)], later, lambda _: []
    )
    assert all(e.expires_at > later for e in warning_events(db, config, later))


def test_midnight_continuation_has_one_warning(config, db, make_schedule, now):
    today = now.astimezone(config.timezone).date()
    rows = [make_schedule(q, (("23:00", "24:00"),), day=today) for q in config.queues]
    rows += [
        make_schedule(q, (("00:00", "02:00"),), day=today + timedelta(days=1))
        for q in config.queues
    ]
    db.apply_schedules(rows, now, lambda _: [])
    events = warning_events(db, config, now)
    assert len(events) == 2
    assert all(e.expires_at.astimezone(config.timezone).hour == 23 for e in events)


async def test_network_ambiguity_not_retried(config, db, make_schedule, now):
    save(db, make_schedule, now)
    events = warning_events(db, config, now)
    event = next(e for e in events if e.kind == "warning_home")
    db.enqueue([event])
    error = TelegramNetworkError(method=SendMessage(chat_id=123, text="x"), message="Timeout")
    sender = RecordingSender(error)
    await NotificationDispatcher(db, sender).dispatch(event.due_at)
    assert db.delivery_counts()["uncertain"] == 1
    sender.failure = None
    await NotificationDispatcher(db, sender).dispatch(event.due_at)
    assert not sender.messages


async def test_known_rate_limit_retries(config, db, make_schedule, now):
    save(db, make_schedule, now)
    event = warning_events(db, config, now)[0]
    db.enqueue([event])
    error = TelegramRetryAfter(
        method=SendMessage(chat_id=123, text="x"), message="Retry", retry_after=30
    )
    sender = RecordingSender(error)
    dispatcher = NotificationDispatcher(db, sender)
    await dispatcher.dispatch(event.due_at)
    assert db.delivery_counts()["pending"] == 1
    sender.failure = None
    await dispatcher.dispatch(event.due_at + timedelta(seconds=31))
    assert len(sender.messages) == 1


def test_crash_in_flight_does_not_duplicate(config, make_schedule, now):
    db = Database(config.database_path)
    save(db, make_schedule, now)
    event = warning_events(db, config, now)[0]
    db.enqueue([event])
    db.claim(event.key, event.chat_id)
    db.close()
    db = Database(config.database_path)
    try:
        assert db.delivery_counts()["uncertain"] == 1
        assert not db.due(event.due_at)
    finally:
        db.close()


async def test_return_to_old_schedule_is_a_new_change(config, db, make_schedule, now):
    provider = StaticProvider(batch(make_schedule))
    sender = RecordingSender()
    monitor = Monitor(config, db, provider, NotificationDispatcher(db, sender))
    await monitor.check(now)
    original = provider.schedules[0]
    provider.schedules[0] = replace(original, intervals=())
    await monitor.check(now + timedelta(minutes=10))
    await monitor.dispatcher.dispatch(now + timedelta(minutes=10))
    provider.schedules[0] = original
    await monitor.check(now + timedelta(minutes=20))
    await monitor.dispatcher.dispatch(now + timedelta(minutes=20))
    assert len(sender.messages) == 2


def test_operators_cannot_mix(make_schedule):
    with pytest.raises(ValueError):
        predicted_lift(make_schedule(), replace(make_schedule("4.1"), operator="another"))


async def test_removed_chat_gets_no_pending_messages(config, db, make_schedule, now):
    save(db, make_schedule, now)
    db.enqueue(warning_events(db, config, now))
    db.cancel_disallowed((999,))
    assert "pending" not in db.delivery_counts()


def test_conservative_lift_includes_possible_outages(make_schedule):
    home = make_schedule(intervals=(), possible=(("10:00", "12:00"),))
    secondary = make_schedule("4.1", (("11:00", "13:00"),))
    assert (
        predicted_lift(home, secondary) == make_schedule(intervals=(("11:00", "12:00"),)).intervals
    )
