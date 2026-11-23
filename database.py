"""SQLite: графіки, історія нормалізованих змін і стійка черга повідомлень."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path

from models import Change, DaySchedule, Event


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class Database:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedules (
                operator TEXT NOT NULL, queue TEXT NOT NULL, day TEXT NOT NULL,
                fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(operator, queue, day)
            );
            CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, operator TEXT NOT NULL,
                queue TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                event_key TEXT NOT NULL, chat_id INTEGER NOT NULL, kind TEXT NOT NULL,
                due_at TEXT NOT NULL, expires_at TEXT NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, sent_at TEXT,
                PRIMARY KEY(event_key, chat_id)
            );
            CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, due_at);
            PRAGMA user_version=1;
        """)
        # Telegram не має idempotency key для sendMessage. Не повторюємо неоднозначну доставку.
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET status='uncertain', "
                "last_error='Перезапуск під час надсилання' WHERE status='sending'"
            )

    def close(self) -> None:
        self.conn.close()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self._meta(key, value)

    def get(self, operator: str, queue: str, day: date) -> DaySchedule | None:
        row = self.conn.execute(
            "SELECT payload FROM schedules WHERE operator=? AND queue=? AND day=?",
            (operator, queue, day.isoformat()),
        ).fetchone()
        return DaySchedule.from_dict(json.loads(row[0])) if row else None

    def range(self, operator: str, first: date, last: date) -> list[DaySchedule]:
        rows = self.conn.execute(
            "SELECT payload FROM schedules WHERE operator=? "
            "AND day BETWEEN ? AND ? ORDER BY day,queue",
            (operator, first.isoformat(), last.isoformat()),
        ).fetchall()
        return [DaySchedule.from_dict(json.loads(r[0])) for r in rows]

    def apply_schedules(
        self,
        schedules: list[DaySchedule],
        now: datetime,
        events_for_changes: Callable[[list[Change]], Iterable[Event]],
    ) -> list[Change]:
        """Новий стан і сповіщення про зміни комітяться однією транзакцією."""
        if not schedules or len({s.operator for s in schedules}) != 1:
            raise ValueError("Одна транзакція має містити дані одного оператора")
        operator = schedules[0].operator
        initialized = self.get_meta(f"initialized:{operator}") == "1"
        changes = []
        with self.conn:
            for schedule in schedules:
                previous = self.get(operator, schedule.queue, schedule.day)
                payload = json.dumps(schedule.to_dict(), ensure_ascii=False)
                changed = previous is None or previous.fingerprint != schedule.fingerprint
                if changed:
                    cursor = self.conn.execute(
                        "INSERT INTO revisions(operator,queue,day,payload,observed_at) "
                        "VALUES (?,?,?,?,?)",
                        (operator, schedule.queue, schedule.day.isoformat(), payload, stamp(now)),
                    )
                    if initialized and (previous is not None or schedule.usable):
                        changes.append(Change(cursor.lastrowid, previous, schedule))
                self.conn.execute(
                    "INSERT INTO schedules VALUES (?,?,?,?,?) "
                    "ON CONFLICT(operator,queue,day) DO UPDATE SET "
                    "fingerprint=excluded.fingerprint,payload=excluded.payload",
                    (
                        operator,
                        schedule.queue,
                        schedule.day.isoformat(),
                        schedule.fingerprint,
                        payload,
                    ),
                )
            for event in events_for_changes(changes):
                self._enqueue(event)
            self._meta(f"initialized:{operator}", "1")
            self._meta("last_success", stamp(now))
            self._meta("last_error", "")
        return changes

    def _enqueue(self, event: Event) -> None:
        self.conn.execute(
            "INSERT INTO outbox(event_key,chat_id,kind,due_at,expires_at,body) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(event_key,chat_id) DO UPDATE SET "
            "body=excluded.body,expires_at=excluded.expires_at WHERE outbox.status='pending'",
            (
                event.key,
                event.chat_id,
                event.kind,
                stamp(event.due_at),
                stamp(event.expires_at),
                event.body,
            ),
        )

    def enqueue(self, events: Iterable[Event]) -> None:
        with self.conn:
            for event in events:
                self._enqueue(event)

    def reconcile_warnings(self, events: list[Event], now: datetime) -> None:
        """Відновлює актуальні й скасовує застарілі, не оживляючи вже надіслані."""
        desired = {(e.key, e.chat_id) for e in events}
        with self.conn:
            pending = self.conn.execute(
                "SELECT event_key,chat_id FROM outbox "
                "WHERE kind LIKE 'warning_%' AND status='pending'"
            )
            for row in pending.fetchall():
                if (row[0], row[1]) not in desired:
                    self.conn.execute(
                        "UPDATE outbox SET status='cancelled' WHERE event_key=? AND chat_id=?",
                        tuple(row),
                    )
            for event in events:
                self._enqueue(event)
                self.conn.execute(
                    "UPDATE outbox SET body=?,due_at=?,expires_at=?,status='pending' "
                    "WHERE event_key=? AND chat_id=? AND status IN ('pending','cancelled')",
                    (
                        event.body,
                        stamp(event.due_at),
                        stamp(event.expires_at),
                        event.key,
                        event.chat_id,
                    ),
                )
            self.conn.execute(
                "UPDATE outbox SET status='expired' WHERE status='pending' AND expires_at<=?",
                (stamp(now),),
            )

    def cancel_disallowed(self, chat_ids: tuple[int, ...]) -> None:
        placeholders = ",".join("?" for _ in chat_ids) or "NULL"
        with self.conn:
            self.conn.execute(
                f"UPDATE outbox SET status='cancelled' WHERE status='pending' "
                f"AND chat_id NOT IN ({placeholders})",
                chat_ids,
            )

    def due(self, now: datetime, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM outbox WHERE status='pending' AND due_at<=? AND expires_at>? "
            "ORDER BY due_at,event_key LIMIT ?",
            (stamp(now), stamp(now), limit),
        ).fetchall()

    def claim(self, key: str, chat_id: int) -> bool:
        with self.conn:
            return (
                self.conn.execute(
                    "UPDATE outbox SET status='sending',attempts=attempts+1 "
                    "WHERE event_key=? AND chat_id=? AND status='pending'",
                    (key, chat_id),
                ).rowcount
                == 1
            )

    def finish(
        self,
        key: str,
        chat_id: int,
        status: str,
        now: datetime,
        error: str = "",
        retry_at: datetime | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET status=?,sent_at=?,last_error=?, "
                "due_at=COALESCE(?,due_at) WHERE event_key=? AND chat_id=?",
                (
                    status,
                    stamp(now) if status == "sent" else None,
                    error,
                    stamp(retry_at) if retry_at else None,
                    key,
                    chat_id,
                ),
            )

    def delivery_counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status"))

    def cleanup(self, cutoff: date) -> None:
        """Залишаємо 90 днів історії; ключі майбутніх подій не видаляємо."""
        with self.conn:
            self.conn.execute("DELETE FROM schedules WHERE day<?", (cutoff.isoformat(),))
            self.conn.execute("DELETE FROM revisions WHERE day<?", (cutoff.isoformat(),))
            self.conn.execute(
                "DELETE FROM outbox WHERE substr(expires_at,1,10)<?", (cutoff.isoformat(),)
            )
