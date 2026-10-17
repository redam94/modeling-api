"""Coordinate metadata summaries and calendar-frequency arithmetic (no pandas).

The control plane never sees arrays, but it does see *summaries* of coordinates (written by
sandboxed ingest/fit runs): category levels, time ranges with a frequency, integer ranges.
These are enough to run CoordPlan checks statically.

Frequencies use pandas-compatible aliases so that runner-side code can hand them straight to
pandas/xarray: ``D``, ``h``/``H``, ``min``/``T``, ``s``/``S``, ``W``/``W-SUN``..``W-SAT``,
``MS``, ``ME``/``M``, ``QS``, ``QE``/``Q``, ``YS``/``AS``, ``YE``/``Y``/``A``, each with an
optional integer multiple (``2W-SUN``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from mapi_spec._types import Scalar, StrictModel

__all__ = [
    "CoordSummary",
    "DataSummary",
    "Freq",
    "parse_freq",
    "parse_time",
]

_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
_UNIT_ALIASES = {
    "D": "D",
    "h": "h",
    "H": "h",
    "min": "min",
    "T": "min",
    "s": "s",
    "S": "s",
    "W": "W",
    "MS": "MS",
    "ME": "ME",
    "M": "ME",
    "QS": "QS",
    "QE": "QE",
    "Q": "QE",
    "YS": "YS",
    "AS": "YS",
    "YE": "YE",
    "Y": "YE",
    "A": "YE",
}
_FREQ_RE = re.compile(r"^(\d{1,4})?([A-Za-z]+)(?:-([A-Z]{3}))?$")
_EPOCH = date(1970, 1, 1)  # a Thursday


@dataclass(frozen=True, slots=True)
class Freq:
    """A parsed frequency: ``n`` units of ``unit``; ``anchor`` is the weekday for weekly."""

    n: int
    unit: str
    anchor: int | None = None

    def ordinal(self, value: datetime) -> int | None:
        """Integer period index of ``value``; ``None`` if ``value`` is not on the grid."""
        naive = value.replace(tzinfo=None)
        total = (naive - datetime(1970, 1, 1)).total_seconds()
        if total != int(total):
            return None
        seconds = int(total)
        d = naive.date()
        on_midnight = seconds % 86400 == 0
        idx: int | None
        match self.unit:
            case "s":
                idx = seconds
            case "min":
                idx = seconds // 60 if seconds % 60 == 0 else None
            case "h":
                idx = seconds // 3600 if seconds % 3600 == 0 else None
            case "D":
                idx = (d - _EPOCH).days if on_midnight else None
            case "W":
                ok = on_midnight and d.weekday() == self.anchor
                idx = (d - _EPOCH).days // 7 if ok else None
            case _:
                idx = self._calendar_ordinal(d) if on_midnight else None
        if idx is None:
            return None
        return idx // self.n if idx % self.n == 0 else None

    def _calendar_ordinal(self, d: date) -> int | None:
        month_index = d.year * 12 + d.month - 1
        is_start = d.day == 1
        is_end = (date(d.year + (d.month == 12), d.month % 12 + 1, 1) - d).days == 1
        match self.unit:
            case "MS":
                return month_index if is_start else None
            case "ME":
                return month_index if is_end else None
            case "QS":
                return month_index // 3 if is_start and (d.month - 1) % 3 == 0 else None
            case "QE":
                return month_index // 3 if is_end and d.month % 3 == 0 else None
            case "YS":
                return d.year if is_start and d.month == 1 else None
            case "YE":
                return d.year if is_end and d.month == 12 else None
        return None  # pragma: no cover

    def label(self, ordinal: int) -> datetime:
        """The inverse of :meth:`ordinal`: the (naive UTC) start of period ``ordinal``."""
        idx = ordinal * self.n
        match self.unit:
            case "s":
                return datetime(1970, 1, 1) + timedelta(seconds=idx)
            case "min":
                return datetime(1970, 1, 1) + timedelta(minutes=idx)
            case "h":
                return datetime(1970, 1, 1) + timedelta(hours=idx)
            case "D":
                return datetime(1970, 1, 1) + timedelta(days=idx)
            case "W":
                assert self.anchor is not None
                offset = (self.anchor - _EPOCH.weekday()) % 7
                return datetime(1970, 1, 1) + timedelta(days=7 * idx + offset)
            case "MS" | "ME" | "QS" | "QE":
                months = idx * 3 if self.unit in ("QS", "QE") else idx
                if self.unit in ("ME", "QE"):
                    months += 2 if self.unit == "QE" else 0
                    year, month = divmod(months + 1, 12)
                    return datetime(year, month + 1, 1) - timedelta(days=1)
                year, month = divmod(months, 12)
                return datetime(year, month + 1, 1)
            case "YS":
                return datetime(idx, 1, 1)
            case "YE":
                return datetime(idx, 12, 31)
        raise ValueError(f"unsupported frequency unit {self.unit!r}")  # pragma: no cover

    def __str__(self) -> str:
        n = "" if self.n == 1 else str(self.n)
        if self.unit == "W":
            day = next(k for k, v in _WEEKDAYS.items() if v == self.anchor)
            return f"{n}W-{day}"
        return f"{n}{self.unit}"


def parse_freq(text: str) -> Freq:
    """Parse a pandas-style frequency alias. Raises ``ValueError`` for unsupported ones."""
    m = _FREQ_RE.match(text.strip())
    if m is None or m.group(2) not in _UNIT_ALIASES:
        raise ValueError(f"unsupported frequency {text!r}")
    n = int(m.group(1)) if m.group(1) else 1
    if n < 1:
        raise ValueError("frequency multiple must be >= 1")
    unit = _UNIT_ALIASES[m.group(2)]
    anchor_txt = m.group(3)
    if unit == "W":
        anchor = _WEEKDAYS.get(anchor_txt or "SUN")
        if anchor is None:
            raise ValueError(f"unknown weekly anchor in {text!r}")
        return Freq(n, unit, anchor)
    if anchor_txt is not None:
        if unit in ("QS", "QE", "YS", "YE") and anchor_txt in ("JAN", "DEC"):
            # Calendar-aligned quarters/years are the only anchors supported in v1.
            return Freq(n, unit)
        raise ValueError(f"anchored frequency {text!r} is not supported")
    return Freq(n, unit)


def parse_time(value: Scalar) -> datetime:
    """Parse an ISO-8601 date/datetime coordinate into a naive UTC datetime."""
    if not isinstance(value, str):
        raise ValueError("time coordinates must be ISO-8601 strings")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


CoordKind = Literal["categorical", "time", "integer", "float", "index"]


class CoordSummary(StrictModel):
    """Metadata summary of one coordinate (never the full array for large dims).

    ``values`` lists categorical levels when there are at most :attr:`MAX_LEVELS` of them;
    ordered dims (time/integer) describe a range by ``start``/``end``/``size`` and whether
    the coordinate is ``contiguous`` (no gaps at its frequency/step).
    """

    MAX_LEVELS: ClassVar[int] = 10_000

    kind: CoordKind
    size: int = Field(..., ge=0)
    values: list[Scalar] | None = Field(None, max_length=10_000)
    start: Scalar | None = None
    end: Scalar | None = None
    freq: str | None = None
    step: int | float | None = None
    contiguous: bool = True

    @model_validator(mode="after")
    def _check(self) -> CoordSummary:
        if self.values is not None and len(self.values) != self.size:
            raise ValueError("len(values) must equal size")
        if self.freq is not None:
            parse_freq(self.freq)
        if self.kind == "time" and self.start is not None:
            parse_time(self.start)
            if self.end is not None:
                parse_time(self.end)
        return self

    def ordinal(self, value: Scalar) -> int | None:
        """Period index of ``value`` on this coordinate's grid (time or integer)."""
        if self.kind == "time":
            if self.freq is None:
                return None
            return parse_freq(self.freq).ordinal(parse_time(value))
        if (
            self.kind in ("integer", "index")
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            step = int(self.step or 1)
            return value // step if value % step == 0 else None
        return None

    @property
    def level_set(self) -> set[Scalar] | None:
        return None if self.values is None else set(self.values)


class DataSummary(StrictModel):
    """Summary of a dataset (or score payload): variable names and coordinate summaries.

    ``observed`` (forecasts conditioned on actuals, :mod:`mapi_spec.actuals`) maps a target
    variable - and a driver the forecast needs (``future_required``) - to, per ordered dim,
    the summary of the labels at which it holds at least one value. Only summarisers that
    read the values set it (the sandbox, and the control plane for inline JSON payloads,
    where it is a null check); a variable left out is unknown.
    """

    vars: list[str] = Field(default_factory=list)
    coords: dict[str, CoordSummary] = Field(default_factory=dict)
    observed: dict[str, dict[str, CoordSummary]] = Field(
        default_factory=dict, exclude_if=lambda v: not v
    )
