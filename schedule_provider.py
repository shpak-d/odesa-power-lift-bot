"""Лише перевірені джерела: JSON зі сторінки ДТЕК або документований Alerts API."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from config import DTEK_OPERATOR, Config
from lift import day_bounds, normalize_intervals
from models import DaySchedule, Interval

DTEK_URL = "https://www.dtek-oem.com.ua/ua/shutdowns"
ALERTS_URL = "https://alerts.energy/api/v1/source-registry/areas/odeska-oblast/shutdowns"
MAX_RESPONSE_BYTES = 5_000_000


class ProviderError(Exception):
    def __init__(self, message: str, retry_at: datetime | None = None):
        super().__init__(message)
        self.retry_at = retry_at


class ScheduleProvider(Protocol):
    async def fetch(self, now: datetime) -> list[DaySchedule]: ...


def _json_assignment(html: str, name: str) -> dict:
    # Декодуємо JSON, а не JavaScript. Жодного eval або виконання коду сайту.
    matches = list(re.finditer(rf"\bDisconSchedule\.{re.escape(name)}\s*=\s*", html))
    if len(matches) != 1:
        raise ProviderError(f"ДТЕК: відсутній або неоднозначний блок {name}")
    try:
        value, _ = json.JSONDecoder().raw_decode(html[matches[0].end() :])
    except ValueError as exc:
        raise ProviderError(f"ДТЕК: некоректний JSON у {name}") from exc
    if not isinstance(value, dict):
        raise ProviderError(f"ДТЕК: змінено формат {name}")
    return value


def decode_hours(
    day: date,
    values: list[int],
    tz: ZoneInfo,
) -> tuple[Interval, ...]:
    if len(values) != 24 or any(type(v) is not int or v not in (0, 1, 2, 3) for v in values):
        raise ProviderError("Очікується 24 значення годин: 0, 1, 2 або 3")
    # Ітеруємо реальний UTC-час: 23/25-годинний день і повторена година без помилок.
    cursor, end = day_bounds(day, tz)
    result = []
    while cursor < end:
        local = cursor.astimezone(tz)
        code = values[local.hour]
        if code == 1 or (code == 2 and local.minute == 0) or (code == 3 and local.minute == 30):
            result.append(Interval(cursor, cursor + timedelta(minutes=30)))
        cursor += timedelta(minutes=30)
    return normalize_intervals(result)


def parse_dtek(html: str, config: Config, now: datetime) -> list[DaySchedule]:
    if config.operator_id != DTEK_OPERATOR:
        raise ProviderError("Оператор не відповідає джерелу ДТЕК")
    if not re.search(
        r"<meta\s+[^>]*property=[\"\']og:site_name[\"\'][^>]*"
        r"content=[\"\']ДТЕК Одеські електромережі[\"\']",
        html,
    ):
        raise ProviderError("ДТЕК: сторінка перевірки доступу або невідомий оператор")
    fact = _json_assignment(html, "fact")
    preset = _json_assignment(html, "preset")
    zones = preset.get("time_zone")
    if not isinstance(zones, dict) or len(zones) != 24:
        raise ProviderError("ДТЕК: змінено часову сітку")
    for h in range(1, 25):
        expected = [f"{h - 1:02}-{h:02}", f"{h - 1:02}:00", f"{h:02}:00"]
        if zones.get(str(h)) != expected:
            raise ProviderError("ДТЕК: невідома нумерація годин")
    data = fact.get("data")
    if data == []:
        data = {}
    if not isinstance(data, dict):
        raise ProviderError("ДТЕК: відсутній словник дат")
    try:
        updated = (
            datetime.strptime(fact["update"], "%d.%m.%Y %H:%M")
            .replace(
                tzinfo=config.timezone,
            )
            .astimezone(UTC)
        )
        source_today = (
            datetime.fromtimestamp(int(fact["today"]), UTC)
            .astimezone(
                config.timezone,
            )
            .date()
        )
    except (KeyError, ValueError, TypeError, OverflowError, OSError) as exc:
        raise ProviderError("ДТЕК: некоректна дата оновлення") from exc
    today = now.astimezone(config.timezone).date()
    if source_today != today:
        raise ProviderError("ДТЕК: сторінка містить застарілу дату; кеш збережено")
    if updated > now + timedelta(minutes=10):
        raise ProviderError("ДТЕК: дата оновлення у майбутньому; перевірте годинник")
    by_date = {}
    for stamp, queues in data.items():
        try:
            local = datetime.fromtimestamp(int(stamp), UTC).astimezone(config.timezone)
        except (ValueError, TypeError, OverflowError, OSError) as exc:
            raise ProviderError("ДТЕК: некоректний ключ дати") from exc
        if local.hour or local.minute or local.second or local.date() in by_date:
            raise ProviderError("ДТЕК: неоднозначна дата графіка")
        if not isinstance(queues, dict):
            raise ProviderError("ДТЕК: некоректний словник черг")
        by_date[local.date()] = queues
    active = bool(re.search(r"DisconSchedule\.showTableFact\s*=\s*true\b", html))
    mapping = {"yes": 0, "no": 1, "first": 2, "second": 3, "maybe": 0, "mfirst": 0, "msecond": 0}
    possible_mapping = {
        "yes": 0,
        "no": 0,
        "first": 0,
        "second": 0,
        "maybe": 1,
        "mfirst": 2,
        "msecond": 3,
    }
    result = []
    for day in (today, today + timedelta(days=1)):
        for queue in config.queues:
            values = by_date.get(day, {}).get(f"GPV{queue}") if active else None
            intervals = possible = ()
            status = "missing"
            if values not in (None, {}, []):
                if not isinstance(values, dict) or set(values) != {str(h) for h in range(1, 25)}:
                    raise ProviderError(f"ДТЕК: неповні години черги {queue}")
                if any(v not in mapping for v in values.values()):
                    raise ProviderError("ДТЕК: невідоме позначення стану години")
                intervals = decode_hours(
                    day, [mapping[values[str(h)]] for h in range(1, 25)], config.timezone
                )
                possible = decode_hours(
                    day,
                    [possible_mapping[values[str(h)]] for h in range(1, 25)],
                    config.timezone,
                )
                status = "published"
            result.append(
                DaySchedule(
                    config.operator_id,
                    queue,
                    day,
                    status,
                    intervals,
                    possible,
                    now,
                    updated,
                    DTEK_URL,
                )
            )
    return result


def parse_alerts(payload: object, config: Config, now: datetime) -> list[DaySchedule]:
    if not isinstance(payload, list):
        raise ProviderError("Alerts API: очікується масив графіків")
    today = now.astimezone(config.timezone).date()
    result = []
    states = {
        "ScheduleApplies": "published",
        "NoShutdowns": "published",
        "WaitingForSchedule": "missing",
        "EmergencyShutdowns": "emergency",
    }
    for queue in config.queues:
        rows = [
            v
            for v in payload
            if isinstance(v, dict)
            and v.get("initiator") == config.operator_id
            and v.get("queue") == queue
        ]
        if len(rows) != 1:
            raise ProviderError(f"Alerts API: відсутня або неоднозначна черга {queue} оператора")
        row = rows[0]
        try:
            updated = datetime.fromisoformat(row["updated"]) if row.get("updated") else None
            if updated and updated.tzinfo is None:
                raise ValueError("Час оновлення без поясу")
            for label, day in (("today", today), ("tomorrow", today + timedelta(days=1))):
                if date.fromisoformat(row[f"{label}_date"]) != day:
                    raise ValueError("Застаріла дата графіка")
                raw_status = row[f"{label}_status"]
                status = states[raw_status]
                hours = row[label]
                if not isinstance(hours, list):
                    raise ValueError("Некоректні години")
                if raw_status == "NoShutdowns" and (len(hours) != 24 or any(hours)):
                    raise ValueError("Суперечливий стан NoShutdowns")
                if status == "published" and not hours:
                    status = "missing"
                intervals = (
                    decode_hours(day, hours, config.timezone) if status == "published" else ()
                )
                possible_hours = row.get(f"{label}_possible", [])
                possible = (
                    decode_hours(day, possible_hours, config.timezone)
                    if status == "published" and possible_hours
                    else ()
                )
                result.append(
                    DaySchedule(
                        config.operator_id,
                        queue,
                        day,
                        status,
                        intervals,
                        possible,
                        now,
                        updated,
                        ALERTS_URL,
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("Alerts API: некоректний формат, стан або дата графіка") from exc
    return result


class HttpScheduleProvider:
    def __init__(self, config: Config, client: httpx.AsyncClient):
        self.config, self.client = config, client

    async def fetch(self, now: datetime) -> list[DaySchedule]:
        headers = {"User-Agent": "OdesaPowerLiftBot/1.0 (personal schedule monitor)"}
        url = DTEK_URL
        if self.config.provider == "alerts_api":
            url = ALERTS_URL
            headers["Authorization"] = f"Bearer {self.config.alerts_api_key}"
        try:
            async with self.client.stream(
                "GET",
                url,
                headers=headers,
                timeout=self.config.http_timeout_seconds,
                follow_redirects=False,
            ) as response:
                if response.status_code in (429, 503):
                    retry_at = None
                    raw = response.headers.get("Retry-After", "")
                    try:
                        retry_at = now + timedelta(seconds=max(0, int(raw)))
                    except ValueError:
                        try:
                            retry_at = parsedate_to_datetime(raw).astimezone(UTC)
                        except (ValueError, TypeError, OverflowError):
                            pass
                    raise ProviderError(
                        f"Джерело тимчасово недоступне (HTTP {response.status_code})", retry_at
                    )
                if response.status_code != 200:
                    raise ProviderError(
                        f"Джерело відхилило запит (HTTP {response.status_code}); "
                        "захист і перенаправлення не обходимо"
                    )
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ProviderError("Завелика відповідь джерела")
                    chunks.append(chunk)
                body = b"".join(chunks).decode("utf-8")
        except (httpx.HTTPError, UnicodeError) as exc:
            raise ProviderError(
                "Помилка мережі або кодування джерела; останні дані збережено"
            ) from exc
        if self.config.provider == "dtek_html":
            return parse_dtek(body, self.config, now)
        try:
            return parse_alerts(json.loads(body), self.config, now)
        except json.JSONDecodeError as exc:
            raise ProviderError("Alerts API повернув не JSON") from exc


async def check_source() -> int:
    try:
        config = Config.from_env(require_telegram=False)
        async with httpx.AsyncClient() as client:
            schedules = await HttpScheduleProvider(config, client).fetch(datetime.now(UTC))
        print(json.dumps([s.to_dict() for s in schedules], ensure_ascii=False, indent=2))
        return 0
    except (ProviderError, ValueError) as exc:
        print(f"Джерело недоступне: {exc}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Перевірка реального джерела без Telegram і SQLite"
    )
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args()
    raise SystemExit(asyncio.run(check_source()))
