"""Liveness без Telegram-запитів і без модифікації бази."""

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


def healthy(path: Path, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as conn:
            for key in ("heartbeat_monitor", "heartbeat_scheduler", "heartbeat_polling"):
                row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                if not row or not timedelta(0) <= now - datetime.fromisoformat(row[0]) < timedelta(
                    seconds=90,
                ):
                    return False
        return True
    except (sqlite3.Error, ValueError, OSError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy(Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))) else 1)
