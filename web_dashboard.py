"""Вебперегляд SQLite та незалежний від Telegram цикл перевірок ДТЕК."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx
from aiohttp import web

from config import OPERATOR_NAMES, Config
from database import Database, stamp
from healthcheck import healthy
from instance_lock import InstanceLock
from lift import clip_to_day, day_bounds
from models import DaySchedule, Interval
from monitor import Monitor
from notifications import fresh, predicted_lift
from schedule_provider import HttpScheduleProvider

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


def serialize_intervals(
    intervals: Iterable[Interval],
    day: date,
    config: Config,
    now: datetime,
) -> list[dict]:
    day_start, day_end = day_bounds(day, config.timezone)
    duration = (day_end - day_start).total_seconds()
    result = []
    for interval in clip_to_day(intervals, day, config.timezone):
        result.append(
            {
                "start": interval.start.isoformat(),
                "end": interval.end.isoformat(),
                "start_label": interval.start.astimezone(config.timezone).strftime("%H:%M"),
                "end_label": (
                    "24:00"
                    if interval.end == day_end
                    else interval.end.astimezone(config.timezone).strftime("%H:%M")
                ),
                "minutes": int((interval.end - interval.start).total_seconds() / 60),
                "left": round((interval.start - day_start).total_seconds() / duration * 100, 4),
                "width": round((interval.end - interval.start).total_seconds() / duration * 100, 4),
                "phase": (
                    "past"
                    if interval.end <= now
                    else "current"
                    if interval.start <= now
                    else "upcoming"
                ),
            }
        )
    return result


def serialize_queue(
    schedule: DaySchedule | None,
    queue: str,
    day: date,
    config: Config,
    now: datetime,
) -> dict:
    return {
        "queue": queue,
        "status": schedule.status if schedule else "missing",
        "fresh": fresh(schedule, now, config),
        "stale": bool(schedule and now - schedule.fetched_at > config.stale_after),
        "intervals": serialize_intervals(schedule.intervals, day, config, now) if schedule else [],
        "possible": serialize_intervals(schedule.possible, day, config, now) if schedule else [],
        "fetched_at": schedule.fetched_at.isoformat() if schedule else None,
        "source_updated_at": (
            schedule.source_updated_at.isoformat()
            if schedule and schedule.source_updated_at
            else None
        ),
        "source": schedule.source if schedule else None,
    }


def build_dashboard(db: Database, config: Config, now: datetime) -> dict:
    today = now.astimezone(config.timezone).date()
    days = []
    for offset in (0, 1):
        day = today + timedelta(days=offset)
        home = db.get(config.operator_id, config.queue_home, day)
        secondary = db.get(config.operator_id, config.queue_secondary, day)
        known = bool(home and secondary and home.usable and secondary.usable)
        lift = predicted_lift(home, secondary) if known else ()
        start, end = day_bounds(day, config.timezone)
        seconds = (end - start).total_seconds()
        ticks = []
        for hour in (0, 6, 12, 18, 24):
            tick = (
                end
                if hour == 24
                else datetime.combine(
                    day,
                    time(hour),
                    config.timezone,
                ).astimezone(UTC)
            )
            ticks.append(
                {"label": f"{hour:02}:00", "left": (tick - start).total_seconds() / seconds * 100}
            )
        days.append(
            {
                "date": day.isoformat(),
                "date_label": day.strftime("%d.%m.%Y"),
                "ticks": ticks,
                "now_position": (now - start).total_seconds() / seconds * 100
                if offset == 0
                else None,
                "home": serialize_queue(home, config.queue_home, day, config, now),
                "secondary": serialize_queue(secondary, config.queue_secondary, day, config, now),
                "lift": {
                    "known": known,
                    "fresh": fresh(home, now, config) and fresh(secondary, now, config),
                    "intervals": serialize_intervals(lift, day, config, now),
                },
            }
        )
    last_success = db.get_meta("last_success")
    last_check = db.get_meta("last_check")
    error = db.get_meta("last_error")
    stale = not last_success or now - datetime.fromisoformat(last_success) > config.stale_after
    return {
        "now": now.isoformat(),
        "timezone": config.timezone.key,
        "operator": OPERATOR_NAMES[config.operator_id],
        "check_interval_minutes": config.check_interval_minutes,
        "stale_after_minutes": config.stale_after_minutes,
        "monitoring": {
            "state": "loading"
            if not last_check and not last_success
            else "error"
            if error
            else "stale"
            if stale
            else "ok",
            "last_check": last_check or None,
            "last_success": last_success or None,
            "error": error or None,
        },
        "days": days,
    }


def create_app(
    config: Config,
    db: Database,
    *,
    clock: Callable[[], datetime] | None = None,
) -> web.Application:
    clock = clock or (lambda: datetime.now(UTC))
    app = web.Application(client_max_size=1024)

    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def asset(request: web.Request) -> web.FileResponse:
        name = request.match_info["name"]
        if name not in {"style.css", "app.js", "favicon.svg"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC_DIR / name, headers={"X-Content-Type-Options": "nosniff"})

    async def dashboard(request: web.Request) -> web.Response:
        return web.json_response(
            build_dashboard(db, config, clock()), headers={"Cache-Control": "no-store"}
        )

    async def health(request: web.Request) -> web.Response:
        ok = healthy(config.database_path, clock(), mode="web")
        return web.json_response(
            {"healthy": ok}, status=200 if ok else 503, headers={"Cache-Control": "no-store"}
        )

    app.add_routes(
        [
            web.get("/", index),
            web.get("/static/{name}", asset),
            web.get("/api/dashboard", dashboard),
            web.get("/healthz", health),
        ]
    )
    return app


async def _monitor_worker(monitor: Monitor) -> None:
    now = datetime.now(UTC)
    monitor.db.set_meta("heartbeat_monitor", stamp(now))
    retry = monitor.db.get_meta("source_retry_at")
    if not retry or now >= datetime.fromisoformat(retry):
        await monitor.check()
    await monitor.monitor_loop()


async def _web_heartbeat(db: Database) -> None:
    while True:
        db.set_meta("heartbeat_web", stamp(datetime.now(UTC)))
        await asyncio.sleep(5)


async def serve(config: Config, *, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    with InstanceLock("", config.database_path):
        db = Database(config.database_path)
        runner = None
        tasks: list[asyncio.Task] = []
        try:
            async with httpx.AsyncClient() as client:
                monitor = Monitor(config, db, HttpScheduleProvider(config, client))
                runner = web.AppRunner(create_app(config, db))
                await runner.setup()
                await web.TCPSite(runner, config.web_host, config.web_port).start()
                logger.info(
                    "Вебсторінка: http://%s:%s · Telegram вимкнено",
                    config.web_host,
                    config.web_port,
                )
                tasks = [
                    asyncio.create_task(_monitor_worker(monitor), name="monitor"),
                    asyncio.create_task(_web_heartbeat(db), name="web-heartbeat"),
                    asyncio.create_task(stop.wait(), name="shutdown"),
                ]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if runner:
                await runner.cleanup()
            db.close()
