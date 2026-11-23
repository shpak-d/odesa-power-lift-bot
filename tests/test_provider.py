import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from config import UZ_OPERATOR
from schedule_provider import (
    DTEK_URL,
    HttpScheduleProvider,
    ProviderError,
    _json_assignment,
    parse_alerts,
    parse_dtek,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dtek_2026-11-23.html"
FETCHED = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)


def test_real_dtek_snapshot(config):
    schedules = parse_dtek(FIXTURE.read_text(encoding="utf-8"), config, FETCHED)
    assert len(schedules) == 4
    assert {s.queue for s in schedules} == {"2.1", "4.1"}
    home_tomorrow = next(s for s in schedules if s.queue == "2.1" and s.day.day == 24)
    assert [
        (
            i.start.astimezone(config.timezone).strftime("%H:%M"),
            i.end.astimezone(config.timezone).strftime("%H:%M"),
        )
        for i in home_tomorrow.intervals
    ] == [("06:30", "13:30"), ("17:00", "21:00")]


def mutate_fact(mutator):
    text = FIXTURE.read_text(encoding="utf-8")
    fact = _json_assignment(text, "fact")
    original = json.dumps(fact, ensure_ascii=False, separators=(",", ":"))
    mutator(fact)
    return text.replace(original, json.dumps(fact, ensure_ascii=False, separators=(",", ":")))


def test_missing_tomorrow_is_unknown_not_zero(config):
    text = mutate_fact(lambda f: f["data"].pop("1795471200"))
    result = parse_dtek(text, config, FETCHED)
    assert all(s.status == "missing" for s in result if s.day.day == 24)
    assert all(s.usable for s in result if s.day.day == 23)


def test_empty_published_day_is_different_from_missing(config):
    text = mutate_fact(
        lambda f: f["data"]["1795471200"]["GPV2.1"].update({str(h): "yes" for h in range(1, 25)})
    )
    result = parse_dtek(text, config, FETCHED)
    home = next(s for s in result if s.queue == "2.1" and s.day.day == 24)
    assert home.usable and home.intervals == ()


@pytest.mark.parametrize("replacement", ["unknown", None, 1])
def test_unknown_state_rejected(config, replacement):
    text = mutate_fact(lambda f: f["data"]["1795471200"]["GPV2.1"].update({"1": replacement}))
    with pytest.raises(ProviderError):
        parse_dtek(text, config, FETCHED)


def test_possible_is_separate(config):
    text = mutate_fact(lambda f: f["data"]["1795471200"]["GPV2.1"].update({"1": "mfirst"}))
    home = next(
        s for s in parse_dtek(text, config, FETCHED) if s.queue == "2.1" and s.day.day == 24
    )
    assert len(home.possible) == 1
    assert home.possible[0].end - home.possible[0].start == timedelta(minutes=30)


def test_stale_page_rejected(config):
    with pytest.raises(ProviderError, match="застарілу дату"):
        parse_dtek(FIXTURE.read_text(encoding="utf-8"), config, FETCHED + timedelta(days=1))


def test_operator_mismatch_challenge_and_missing_script(config):
    with pytest.raises(ProviderError, match="Оператор"):
        parse_dtek("", replace(config, operator_id=UZ_OPERATOR), FETCHED)
    with pytest.raises(ProviderError):
        parse_dtek('<iframe src="/challenge"></iframe>', config, FETCHED)
    with pytest.raises(ProviderError):
        _json_assignment("DisconSchedule.fact = alert('x')", "fact")


def api_rows(config):
    return [
        dict(
            initiator=config.operator_id,
            queue=q,
            updated="2026-11-24T04:00:00Z",
            today=[0] * 24,
            tomorrow=[],
            today_date="2026-11-24",
            tomorrow_date="2026-11-25",
            today_status="NoShutdowns",
            tomorrow_status="WaitingForSchedule",
        )
        for q in config.queues
    ]


def test_documented_alerts_api_format(config, now):
    rows = api_rows(config)
    # Додавання графіка іншого оператора не впливає на вибраний.
    rows.append({**rows[0], "initiator": UZ_OPERATOR})
    result = parse_alerts(rows, config, now)
    assert len(result) == 4
    assert all(s.usable for s in result if s.day.day == 24)
    assert all(s.status == "missing" for s in result if s.day.day == 25)


def test_api_emergency_and_inconsistent_states(config, now):
    rows = api_rows(config)
    rows[0]["today_status"] = "EmergencyShutdowns"
    rows[0]["today"] = [1] * 24
    assert parse_alerts(rows, config, now)[0].status == "emergency"
    rows[0]["today_status"] = "NoShutdowns"
    with pytest.raises(ProviderError):
        parse_alerts(rows, config, now)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503, 302])
async def test_http_errors_no_redirect_or_bypass(config, now, status):
    transport = httpx.MockTransport(
        lambda r: httpx.Response(status, headers={"Retry-After": "3600"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderError) as caught:
            await HttpScheduleProvider(config, client).fetch(now)
    if status in (429, 503):
        assert caught.value.retry_at == now + timedelta(hours=1)


async def test_single_public_request_for_both_queues(config):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        schedules = await HttpScheduleProvider(config, client).fetch(FETCHED)
    assert len(schedules) == 4 and len(requests) == 1
    assert str(requests[0].url) == DTEK_URL
    assert "authorization" not in requests[0].headers
