"""Liveness без Telegram-запитів і без модифікації бази."""

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path


def healthy(path: Path, now: datetime | None = None, *, mode: str = "telegram") -> bool:
    now = now or datetime.now(UTC)
    try:
        keys = (
            ("heartbeat_monitor", "heartbeat_web")
            if mode == "web"
            else ("heartbeat_monitor", "heartbeat_scheduler", "heartbeat_polling")
        )
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        ) as conn:
            for key in keys:
                row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                if not row or not timedelta(0) <= now - datetime.fromisoformat(row[0]) < timedelta(
                    seconds=90,
                ):
                    return False
        return True
    except (sqlite3.Error, ValueError, OSError):
        return False


if __name__ == "__main__":
    raise SystemExit(
        0
        if healthy(
            Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")), mode=os.getenv("APP_MODE", "web")
        )
        else 1
    )
