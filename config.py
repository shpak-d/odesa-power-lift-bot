from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

DTEK_OPERATOR = "odeska_oblast_oblenergo"
UZ_OPERATOR = "odeska_oblast_uz"
OPERATOR_NAMES = {
    DTEK_OPERATOR: "ДТЕК Одеські електромережі",
    UZ_OPERATOR: "АТ «Укрзалізниця»",
}


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    allowed_chat_ids: tuple[int, ...]
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Kyiv"))
    check_interval_minutes: int = 10
    morning_report_time: time = time(8, 0)
    warning_before_minutes: int = 15
    queue_home: str = "2.1"
    queue_secondary: str = "4.1"
    operator_id: str = DTEK_OPERATOR
    provider: str = "dtek_html"
    alerts_api_key: str = field(default="", repr=False)
    database_path: Path = Path("data/bot.sqlite3")
    stale_after_minutes: int = 30
    http_timeout_seconds: int = 25
    log_level: str = "INFO"

    @property
    def stale_after(self) -> timedelta:
        return timedelta(minutes=self.stale_after_minutes)

    @property
    def queues(self) -> tuple[str, str]:
        return self.queue_home, self.queue_secondary

    @classmethod
    def from_env(cls, *, require_telegram: bool = True) -> Config:
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        ids = tuple(
            dict.fromkeys(
                int(v.strip()) for v in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if v.strip()
            )
        )
        if require_telegram and (not token or not ids or 0 in ids):
            raise ValueError("Заповніть BOT_TOKEN і непорожній ALLOWED_CHAT_IDS у .env")

        def positive(name: str, default: int) -> int:
            value = int(os.getenv(name, str(default)))
            if value <= 0:
                raise ValueError(f"{name} має бути додатним")
            return value

        tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
        if tz.key not in {"Europe/Kyiv", "Europe/Kiev"}:
            raise ValueError("Для одеських графіків TIMEZONE має бути Europe/Kyiv")
        queues = tuple(
            os.getenv(k, d).strip() for k, d in (("QUEUE_HOME", "2.1"), ("QUEUE_SECONDARY", "4.1"))
        )
        if any(not re.fullmatch(r"[1-6]\.[12]", q) for q in queues) or queues[0] == queues[1]:
            raise ValueError("Вкажіть дві різні черги від 1.1 до 6.2")
        provider = os.getenv("SCHEDULE_PROVIDER", "dtek_html")
        operator = os.getenv("OPERATOR_ID", DTEK_OPERATOR)
        api_key = os.getenv("ALERTS_API_KEY", "").strip()
        if operator not in OPERATOR_NAMES or provider not in {"dtek_html", "alerts_api"}:
            raise ValueError("Непідтримуваний оператор або провайдер")
        if provider == "dtek_html" and operator != DTEK_OPERATOR:
            raise ValueError("Сторінка ДТЕК не є джерелом графіків Укрзалізниці")
        if provider == "alerts_api" and not api_key:
            raise ValueError("Для Alerts Energy API потрібні ALERTS_API_KEY та активна підписка")
        morning = time.fromisoformat(os.getenv("MORNING_REPORT_TIME", "08:00"))
        if morning.tzinfo is not None or morning.second or morning.microsecond:
            raise ValueError("MORNING_REPORT_TIME має бути місцевим часом HH:MM")
        interval = positive("CHECK_INTERVAL_MINUTES", 10)
        stale = positive("STALE_AFTER_MINUTES", 30)
        if stale < interval:
            raise ValueError("STALE_AFTER_MINUTES має бути не менше CHECK_INTERVAL_MINUTES")
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Некоректний LOG_LEVEL")
        return cls(
            bot_token=token,
            allowed_chat_ids=ids,
            timezone=tz,
            check_interval_minutes=interval,
            morning_report_time=morning,
            warning_before_minutes=positive("WARNING_BEFORE_MINUTES", 15),
            queue_home=queues[0],
            queue_secondary=queues[1],
            operator_id=operator,
            provider=provider,
            alerts_api_key=api_key,
            database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
            stale_after_minutes=stale,
            http_timeout_seconds=positive("HTTP_TIMEOUT_SECONDS", 25),
            log_level=level,
        )
