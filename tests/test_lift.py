from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lift import (
    clip_to_day,
    future_intervals,
    intersect_intervals,
    interval_from_clock,
    normalize_intervals,
)
from models import Interval
from schedule_provider import decode_hours

TZ = ZoneInfo("Europe/Kyiv")
DAY = date(2026, 11, 24)


def intervals(*pairs):
    return tuple(interval_from_clock(DAY, a, b, TZ) for a, b in pairs)


def test_requested_example():
    a = intervals(("10:00", "14:00"), ("18:00", "22:00"))
    b = intervals(("12:00", "16:00"), ("20:00", "23:00"))
    assert intersect_intervals(a, b) == intervals(("12:00", "14:00"), ("20:00", "22:00"))


def test_normalize_unsorted_adjacent_overlapping_duplicates():
    assert normalize_intervals(
        intervals(("12:00", "14:00"), ("10:00", "12:00"), ("11:00", "13:00"), ("10:00", "12:00"))
    ) == intervals(
        ("10:00", "14:00"),
    )


def test_touching_is_not_intersection():
    assert not intersect_intervals(intervals(("10:00", "12:00")), intervals(("12:00", "14:00")))
    assert not intersect_intervals((), intervals(("10:00", "14:00")))


def test_cross_midnight_and_daily_clipping():
    a = intervals(("23:00", "02:00"))
    b = intervals(("23:30", "01:00"))
    assert intersect_intervals(a, b) == b
    assert clip_to_day(b, DAY, TZ) == intervals(("23:30", "24:00"))
    assert clip_to_day(b, DAY + timedelta(days=1), TZ)[0].end == b[0].end


def test_past_and_in_progress_events():
    value = intervals(("06:00", "08:00"), ("10:00", "14:00"), ("18:00", "20:00"))
    now = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)  # 12:00
    assert future_intervals(value, now) == value[1:]


@pytest.mark.parametrize("start,end", [("00:00", "00:00"), ("25:00", "01:00"), ("10:60", "12:00")])
def test_invalid_intervals(start, end):
    with pytest.raises(ValueError):
        interval_from_clock(DAY, start, end, TZ)


def test_naive_datetimes_rejected():
    with pytest.raises(ValueError):
        Interval(datetime(2026, 1, 1), datetime(2026, 1, 2))


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_dst_real_duration_and_contiguous_intervals(day, hours):
    result = decode_hours(day, [1] * 24, TZ)
    assert len(result) == 1
    assert result[0].end - result[0].start == timedelta(hours=hours)


def test_half_hour_codes():
    hours = [0] * 24
    hours[10], hours[11] = 3, 2
    assert decode_hours(DAY, hours, TZ) == intervals(("10:30", "11:30"))
