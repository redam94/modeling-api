"""Five-field cron expressions for workflow schedules (M3.5; pure stdlib).

``minute hour day-of-month month day-of-week`` with ``*``, numbers, ranges ``a-b``, steps
``*/n`` and ``a-b/n``, lists ``a,b,c`` and the names ``jan``-``dec`` / ``sun``-``sat``
(Sunday is 0 or 7). As in Vixie cron, when both day fields are restricted a day matches if
*either* matches. Macros: ``@hourly``, ``@daily``, ``@weekly``, ``@monthly``, ``@yearly``.

:func:`next_fire` computes the next time strictly after a given instant in an IANA time
zone (``zoneinfo``); a local time skipped by a daylight-saving jump does not fire, a
repeated one fires once. Schedules may not fire more often than every
:data:`MIN_INTERVAL_MINUTES` minutes (checked by :func:`check_cron`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "MIN_INTERVAL_MINUTES",
    "CronSpec",
    "check_cron",
    "check_timezone",
    "next_fire",
    "parse_cron",
]

MIN_INTERVAL_MINUTES: Final = 15
"""The tightest schedule allowed (workflows spend compute unattended)."""

_MACROS: Final = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_MONTHS: Final = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
)}  # fmt: skip
_DAYS: Final = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
_FIELD_RE: Final = re.compile(r"^[0-9a-z*/,\-]{1,100}$")


def _bad(message: str) -> MapiError:
    return MapiError(message, code=ErrorCode.SCHEDULE_INVALID, pointer="/cron")


@dataclass(frozen=True, slots=True)
class CronSpec:
    """A parsed expression: the allowed values of every field."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    """0 = Sunday ... 6 = Saturday."""
    days_any: bool
    weekdays_any: bool
    source: str

    def matches_day(self, d: datetime) -> bool:
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays
        if self.days_any and self.weekdays_any:
            return True
        if self.days_any:
            return dow
        if self.weekdays_any:
            return dom
        return dom or dow


def _value(token: str, lo: int, hi: int, names: dict[str, int] | None) -> int:
    if names is not None and token in names:
        return names[token]
    if not token.isdigit() or len(token) > 2:
        raise _bad(f"{token!r} is not a number in {lo}-{hi}")
    v = int(token)
    if not lo <= v <= hi:
        raise _bad(f"{v} is outside {lo}-{hi}")
    return v


def _field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> frozenset[int]:
    if not _FIELD_RE.match(text):
        raise _bad(f"invalid cron field {text[:40]!r}")
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise _bad("empty list item")
        rng, slash, step_txt = part.partition("/")
        step = 1
        if slash:
            if not step_txt.isdigit() or not 1 <= int(step_txt) <= hi - lo + 1:
                raise _bad(f"invalid step in {part!r}")
            step = int(step_txt)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            left, _, right = rng.partition("-")
            a, b = _value(left, lo, hi, names), _value(right, lo, hi, names)
            if a > b:
                raise _bad(f"range {part!r} runs backwards")
        else:
            a = _value(rng, lo, hi, names)
            b = hi if slash else a
        out.update(range(a, b + 1, step))
    return frozenset(out)


def parse_cron(expr: str) -> CronSpec:
    """Parse an expression (``SCHEDULE_INVALID`` with a plain message otherwise)."""
    if not isinstance(expr, str) or len(expr) > 200:
        raise _bad("a cron expression is a short text")
    text = _MACROS.get(expr.strip().lower(), expr.strip().lower())
    fields = text.split()
    if len(fields) != 5:
        raise _bad("a cron expression has 5 fields: minute hour day month weekday")
    minute, hour, dom, month, dow = fields
    weekdays = _field(dow, 0, 7, _DAYS)
    weekdays = frozenset(0 if d == 7 else d for d in weekdays)
    return CronSpec(
        minutes=_field(minute, 0, 59),
        hours=_field(hour, 0, 23),
        days=_field(dom, 1, 31),
        months=_field(month, 1, 12, _MONTHS),
        weekdays=weekdays,
        days_any=dom == "*",
        weekdays_any=dow == "*",
        source=expr.strip(),
    )


def check_timezone(name: str) -> ZoneInfo:
    """The IANA zone ``name`` (``SCHEDULE_INVALID`` when unknown)."""
    if not re.fullmatch(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,2}", name or ""):
        raise MapiError("unknown time zone", code=ErrorCode.SCHEDULE_INVALID, pointer="/timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise MapiError(
            f"unknown time zone {name[:64]!r}", code=ErrorCode.SCHEDULE_INVALID,
            pointer="/timezone",
        ) from None  # fmt: skip


def next_fire(spec: CronSpec | str, after: datetime, tz: str = "UTC") -> datetime:
    """The first firing time strictly after ``after`` (aware), returned in UTC.

    Searches day by day for at most ~5 years (``SCHEDULE_INVALID`` if nothing matches, e.g.
    ``0 0 30 2 *``).
    """
    cs = parse_cron(spec) if isinstance(spec, str) else spec
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    zone = check_timezone(tz)
    local = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = local.replace(hour=0, minute=0)
    for _ in range(366 * 5 + 2):
        if day.month in cs.months and cs.matches_day(day):
            for hour in sorted(cs.hours):
                for minute in sorted(cs.minutes):
                    cand = day.replace(hour=hour, minute=minute, tzinfo=None)
                    if cand < local.replace(tzinfo=None):
                        continue
                    aware = cand.replace(tzinfo=zone)
                    # skip local times that do not exist (spring-forward gaps)
                    roundtrip = aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
                    if roundtrip != cand:
                        continue
                    return aware.astimezone(UTC)
        day = (day.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=zone)
    raise _bad("the schedule never fires")


def check_cron(expr: str, *, min_interval_minutes: int = MIN_INTERVAL_MINUTES) -> str:
    """Validate ``expr`` and refuse schedules that fire more often than allowed.

    The check walks a week of firing times in UTC (the tightest gap of a cron expression
    repeats within a week for every field but day-of-month/month, whose gaps are larger).
    """
    cs = parse_cron(expr)
    t = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday
    prev = next_fire(cs, t)
    for _ in range(600):
        nxt = next_fire(cs, prev)
        if (nxt - prev) < timedelta(minutes=min_interval_minutes):
            raise _bad(f"schedules may fire at most every {min_interval_minutes} minutes")
        if nxt - t > timedelta(days=8):
            break
        prev = nxt
    return expr
