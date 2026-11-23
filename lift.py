"""Перетин напіввідкритих інтервалів [start, end), без припущень про статус ліфта."""

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from models import Interval


def normalize_intervals(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    merged: list[Interval] = []
    for current in sorted(intervals):
        if merged and current.start <= merged[-1].end:
            previous = merged.pop()
            current = Interval(previous.start, max(previous.end, current.end))
        merged.append(current)
    return tuple(merged)


def intersect_intervals(
    first: Iterable[Interval],
    second: Iterable[Interval],
) -> tuple[Interval, ...]:
    a, b = normalize_intervals(first), normalize_intervals(second)
    result: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start, end = max(a[i].start, b[j].start), min(a[i].end, b[j].end)
        if start < end:
            result.append(Interval(start, end))
        if a[i].end <= b[j].end:
            i += 1
        else:
            j += 1
    return normalize_intervals(result)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, tz).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC),
    )


def clip_to_day(intervals: Iterable[Interval], day: date, tz: ZoneInfo) -> tuple[Interval, ...]:
    start, end = day_bounds(day, tz)
    return intersect_intervals(intervals, (Interval(start, end),))


def future_intervals(intervals: Iterable[Interval], now: datetime) -> tuple[Interval, ...]:
    """Минулі відкидаються; поточний зберігає початковий час початку."""
    return tuple(i for i in normalize_intervals(intervals) if i.end > now)


def wall_time(day: date, minute: int, tz: ZoneInfo, *, end: bool = False) -> datetime:
    """При осінньому повторі години охоплюємо обидва fold; неіснуючий час відхиляємо."""
    naive = datetime.combine(day, time.min) + timedelta(minutes=minute)
    candidates = []
    for fold in (0, 1):
        local = naive.replace(tzinfo=tz, fold=fold)
        utc = local.astimezone(UTC)
        if utc.astimezone(tz).replace(tzinfo=None) == naive:
            candidates.append(utc)
    if not candidates:
        raise ValueError("Джерело вказало неіснуючий місцевий час при переході на літній час")
    return max(candidates) if end else min(candidates)


def interval_from_clock(day: date, start: str, end: str, tz: ZoneInfo) -> Interval:
    def minute(value: str) -> int:
        hour, part = map(int, value.split(":"))
        if not (0 <= hour <= 24 and 0 <= part < 60 and (hour != 24 or part == 0)):
            raise ValueError("Некоректний час")
        return hour * 60 + part

    a, b = minute(start), minute(end)
    if a == b:
        raise ValueError("Нульовий інтервал неоднозначний")
    if b < a:
        b += 1440
    return Interval(wall_time(day, a, tz), wall_time(day, b, tz, end=True))
