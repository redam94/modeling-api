from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mapi_spec.coords import CoordSummary, DataSummary, parse_freq, parse_time
from pydantic import ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("D", "D"),
        ("W", "W-SUN"),
        ("W-SUN", "W-SUN"),
        ("2W-MON", "2W-MON"),
        ("MS", "MS"),
        ("M", "ME"),
        ("ME", "ME"),
        ("QS", "QS"),
        ("Q", "QE"),
        ("QS-JAN", "QS"),
        ("A", "YE"),
        ("AS", "YS"),
        ("H", "h"),
        ("15min", "15min"),
        ("T", "min"),
        ("S", "s"),
    ],
)
def test_parse_freq(text: str, canonical: str) -> None:
    assert str(parse_freq(text)) == canonical


@pytest.mark.parametrize("bad", ["", "X", "W-XYZ", "Q-MAR", "D-SUN", "0D", "fortnight"])
def test_parse_freq_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_freq(bad)


def _ord(freq: str, value: str) -> int | None:
    return parse_freq(freq).ordinal(parse_time(value))


def _step(freq: str, a: str, b: str) -> int:
    oa, ob = _ord(freq, a), _ord(freq, b)
    assert oa is not None and ob is not None
    return ob - oa


def test_ordinals_contiguous() -> None:
    assert _step("D", "2026-01-01", "2026-01-02") == 1
    # 2026-01-04 and 2026-01-11 are Sundays
    assert _step("W-SUN", "2026-01-04", "2026-01-11") == 1
    assert _ord("W-SUN", "2026-01-05") is None  # a Monday
    assert _ord("W-MON", "2026-01-05") is not None
    assert _step("MS", "2026-01-01", "2026-02-01") == 1
    assert _ord("MS", "2026-02-02") is None
    assert _step("ME", "2026-01-31", "2026-02-28") == 1
    assert _ord("ME", "2024-02-28") is None and _ord("ME", "2024-02-29") is not None
    assert _step("QS", "2026-01-01", "2026-04-01") == 1
    assert _ord("QS", "2026-02-01") is None
    assert _step("QE", "2026-03-31", "2026-06-30") == 1
    assert _step("YS", "2026-01-01", "2027-01-01") == 1
    assert _ord("YE", "2026-12-31") is not None and _ord("YE", "2026-11-30") is None
    assert _step("h", "2026-01-01T00:00:00", "2026-01-01T01:00:00") == 1
    assert _ord("h", "2026-01-01T01:30:00") is None
    assert _step("min", "2026-01-01T00:00:00", "2026-01-01T00:01:00") == 1
    assert _step("s", "2026-01-01T00:00:00", "2026-01-01T00:00:01") == 1
    assert _ord("D", "2026-01-01T12:00:00") is None
    assert _ord("2D", "1970-01-03") == 1 and _ord("2D", "1970-01-02") is None
    assert _step("W-SUN", "2025-12-28", "2026-03-08") == 10


def test_parse_time_utc() -> None:
    assert parse_time("2026-01-01T01:00:00+01:00") == datetime(2026, 1, 1, 0, 0)
    with pytest.raises(ValueError):
        parse_time(3)


@given(st.dates(min_value=date(1900, 1, 1), max_value=date(2200, 1, 1)))
def test_daily_and_weekly_ordinal_property(d: date) -> None:
    assert _step("D", d.isoformat(), (d + timedelta(days=1)).isoformat()) == 1
    anchor = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][d.weekday()]
    assert _step(f"W-{anchor}", d.isoformat(), (d + timedelta(days=7)).isoformat()) == 1


def test_coord_summary() -> None:
    cs = CoordSummary(kind="categorical", size=2, values=["a", "b"])
    assert cs.level_set == {"a", "b"}
    t = CoordSummary(kind="time", size=10, start="2026-01-04", end="2026-03-08", freq="W-SUN")
    o1, o2 = t.ordinal("2026-01-04"), t.ordinal("2026-01-11")
    assert o1 is not None and o2 == o1 + 1
    assert CoordSummary(kind="time", size=1, start="2026-01-01").ordinal("2026-01-01") is None
    i = CoordSummary(kind="integer", size=5, start=0, end=8, step=2)
    assert i.ordinal(4) == 2 and i.ordinal(3) is None and i.ordinal("x") is None
    with pytest.raises(ValidationError, match="size"):
        CoordSummary(kind="categorical", size=3, values=["a"])
    with pytest.raises(ValidationError):
        CoordSummary(kind="time", size=1, start="not a date")
    DataSummary(vars=["spend"], coords={"time": t})
