"""Нормалізовані дані. Усі абсолютні інтервали зберігаються в UTC."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime


@dataclass(frozen=True, order=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Інтервал повинен містити часовий пояс")
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))
        if self.end <= self.start:
            raise ValueError("Кінець інтервалу має бути пізніше початку")

    def as_pair(self) -> list[str]:
        return [self.start.isoformat(), self.end.isoformat()]


@dataclass(frozen=True)
class DaySchedule:
    operator: str
    queue: str
    day: date
    status: str  # published, missing, emergency
    intervals: tuple[Interval, ...]
    possible: tuple[Interval, ...]
    fetched_at: datetime
    source_updated_at: datetime | None
    source: str

    @property
    def usable(self) -> bool:
        return self.status == "published"

    def normalized(self) -> dict:
        # Imports here avoid a models/lift dependency cycle.
        from lift import normalize_intervals

        return {
            "status": self.status,
            "intervals": [i.as_pair() for i in normalize_intervals(self.intervals)],
            "possible": [i.as_pair() for i in normalize_intervals(self.possible)],
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.normalized(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {
            **self.normalized(),
            "operator": self.operator,
            "queue": self.queue,
            "day": self.day.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "source_updated_at": (
                self.source_updated_at.isoformat() if self.source_updated_at else None
            ),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: dict) -> DaySchedule:
        return cls(
            operator=value["operator"],
            queue=value["queue"],
            day=date.fromisoformat(value["day"]),
            status=value["status"],
            intervals=tuple(
                Interval(*(datetime.fromisoformat(t) for t in p)) for p in value["intervals"]
            ),
            possible=tuple(
                Interval(*(datetime.fromisoformat(t) for t in p)) for p in value.get("possible", [])
            ),
            fetched_at=datetime.fromisoformat(value["fetched_at"]),
            source_updated_at=(
                datetime.fromisoformat(value["source_updated_at"])
                if value.get("source_updated_at")
                else None
            ),
            source=value["source"],
        )


@dataclass(frozen=True)
class Change:
    revision: int
    before: DaySchedule | None
    after: DaySchedule


@dataclass(frozen=True)
class Event:
    key: str
    kind: str
    chat_id: int
    due_at: datetime
    expires_at: datetime
    body: str
