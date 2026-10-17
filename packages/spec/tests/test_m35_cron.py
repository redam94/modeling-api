"""M3.5 cron schedules: parsing, the next firing time (including daylight-saving jumps) and
the minimum interval."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mapi_spec.cron import check_cron, check_timezone, next_fire, parse_cron
from mapi_spec.errors import ErrorCode, MapiError

pytestmark = pytest.mark.unit


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    ("expr", "after", "expected"),
    [
        ("0 6 * * 1", _utc(2026, 10, 15, 9, 30), _utc(2026, 10, 19, 6, 0)),  # next Monday
        ("30 */6 * * *", _utc(2026, 10, 15, 9, 30), _utc(2026, 10, 15, 12, 30)),
        ("0 0 1 * *", _utc(2026, 12, 15), _utc(2027, 1, 1)),
        ("@weekly", _utc(2026, 10, 15), _utc(2026, 10, 18)),  # Sunday 00:00
        ("0 9 * jan,jul mon-fri", _utc(2026, 7, 31, 10), _utc(2027, 1, 1, 9)),
        ("0 12 29 2 *", _utc(2026, 3, 1), _utc(2028, 2, 29, 12)),
        ("0 0 13 * 5", _utc(2026, 10, 1), _utc(2026, 10, 2)),  # day OR weekday (Friday)
        ("15 10 * * 7", _utc(2026, 10, 15), _utc(2026, 10, 18, 10, 15)),  # 7 = Sunday
        ("0 6 * * 1", _utc(2026, 10, 19, 6, 0), _utc(2026, 10, 26, 6, 0)),  # strictly after
    ],
)
def test_next_fire_in_utc(expr: str, after: datetime, expected: datetime) -> None:
    assert next_fire(expr, after) == expected


def test_time_zones_and_daylight_saving() -> None:
    # Berlin is UTC+2 in summer and UTC+1 in winter
    assert next_fire("0 6 * * *", _utc(2026, 7, 1), "Europe/Berlin") == _utc(2026, 7, 1, 4)
    assert next_fire("0 6 * * *", _utc(2026, 12, 1), "Europe/Berlin") == _utc(2026, 12, 1, 5)
    # 02:30 does not exist on 2026-03-29 in Berlin: the next day's 02:30 fires instead
    assert next_fire("30 2 * * *", _utc(2026, 3, 28, 12), "Europe/Berlin") == _utc(
        2026, 3, 30, 0, 30
    )
    # 02:30 happens twice on 2026-10-25: it fires once (the first one, still UTC+2)
    first = next_fire("30 2 * * *", _utc(2026, 10, 24, 12), "Europe/Berlin")
    assert first == _utc(2026, 10, 25, 0, 30)
    assert next_fire("30 2 * * *", first, "Europe/Berlin") == _utc(2026, 10, 26, 1, 30)
    assert check_timezone("America/New_York").key == "America/New_York"


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("* * * *", "5 fields"),
        ("60 * * * *", "not in the range 0-59"),
        ("0 24 * * *", "not in the range 0-23"),
        ("0 0 0 * *", "not in the range 1-31"),
        ("0 0 * 13 *", "not in the range 1-12"),
        ("0 0 * * 8", "not in the range 0-7"),
        ("5-1 * * * *", "starts after its end"),
        ("*/0 * * * *", "the step in"),
        ("0 0 * foo *", "not a number"),
        ("0,,1 * * * *", "list item is empty"),
        ("0 0 30 2 *", "never runs"),
        ("$(rm) * * * *", "is not correct"),
    ],
)
def test_invalid_expressions(expr: str, message: str) -> None:
    with pytest.raises(MapiError) as exc:
        next_fire(parse_cron(expr), _utc(2026, 1, 1))
    assert exc.value.code is ErrorCode.SCHEDULE_INVALID and message in exc.value.detail


def test_minimum_interval_and_unknown_zones() -> None:
    assert check_cron("*/15 * * * *") == "*/15 * * * *"
    for tight in ("* * * * *", "*/10 * * * *", "0,5 * * * *"):
        with pytest.raises(MapiError, match="every 15 minutes"):
            check_cron(tight)
    for bad in ("Mars/Olympus", "../../etc/passwd", ""):
        with pytest.raises(MapiError):
            check_timezone(bad)
    with pytest.raises(ValueError, match="timezone-aware"):
        next_fire("@daily", datetime(2026, 1, 1))


@pytest.mark.parametrize("expr", ["*/30 * * * *", "15 1 * * *", "0 * * * *", "45 1 * * *"])
def test_next_fire_never_returns_the_past_in_the_repeated_hour(expr: str) -> None:
    """Regression: after the fall-back jump the first occurrence of the repeated hour is in
    the past; returning it made a schedule fire on every loop tick."""
    zone = "America/New_York"
    # 2026-11-01 01:00-02:00 EDT (05:00-06:00Z) repeats as 01:00-02:00 EST (06:00-07:00Z)
    for minute in range(0, 125, 5):
        after = _utc(2026, 11, 1, 5) + timedelta(minutes=minute)
        nxt = next_fire(expr, after, zone)
        assert nxt > after, (expr, after, nxt)


def test_a_repeated_local_time_still_fires_once() -> None:
    zone = "America/New_York"
    first = next_fire("15 1 * * *", _utc(2026, 11, 1, 4), zone)
    assert first == _utc(2026, 11, 1, 5, 15)  # 01:15 EDT
    # the second 01:15 (EST, 06:15Z) does not fire again: the next one is the next day
    assert next_fire("15 1 * * *", first, zone) == _utc(2026, 11, 2, 6, 15)
    # a loop that was down through the first 01:15 fires the missed run once, not per tick
    assert next_fire("15 1 * * *", _utc(2026, 11, 1, 6, 5), zone) == _utc(2026, 11, 2, 6, 15)
    # a simulated 5 s loop through the repeated hour fires once per local half hour at most
    t, fires = _utc(2026, 11, 1, 5), 0
    nxt = next_fire("*/30 * * * *", t, zone)
    while t < _utc(2026, 11, 1, 7):
        if nxt <= t:
            fires += 1
            nxt = next_fire("*/30 * * * *", t, zone)
        t += timedelta(seconds=5)
    assert fires <= 4
