"""Column profiles of a local CSV file, for ``mapi guided detect --csv``.

Like the web app, the CLI profiles an upload *locally* and sends only the
:class:`~mapi_spec.guided.ColumnProfile` summaries (name, type, counts, range, a few sample
values) for role detection; the file itself is not uploaded. The profile follows the
ingest's rules where they matter for detection: numbers (integer or not), ISO dates (with
the detected frequency), booleans, and strings; empty cells are missing.
"""

from __future__ import annotations

import csv
import itertools
import math
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, Final

from mapi_spec.guided import ColumnProfile

__all__ = ["MAX_PROFILE_ROWS", "profile_csv"]

MAX_PROFILE_ROWS: Final = 200_000
MAX_COLUMNS: Final = 2048
_MISSING: Final = frozenset({"", "na", "nan", "null", "none", "n/a"})
_WEEKDAYS: Final = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _date(text: str) -> date | None:
    if len(text) < 8 or len(text) > 32:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _freq(days: list[date]) -> str | None:
    """``D``, ``W-<DAY>`` or ``MS`` when every gap between the sorted dates agrees."""
    if len(days) < 3:
        return None
    gaps = {(b - a).days for a, b in itertools.pairwise(days)}
    if gaps == {1}:
        return "D"
    if gaps == {7}:
        return f"W-{_WEEKDAYS[days[0].weekday()]}"
    if all(d.day == 1 for d in days) and gaps <= {28, 29, 30, 31}:
        return "MS"
    return None


def _rows(path: Path, limit: int) -> Iterator[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.reader(fh)):
            if i > limit:
                return
            yield row


def _profile(name: str, cells: list[str]) -> ColumnProfile:
    values = [c.strip() for c in cells]
    present = [v for v in values if v.lower() not in _MISSING]
    base: dict[str, Any] = {
        "name": name[:256],
        "n_rows": len(values),
        "n_missing": len(values) - len(present),
        "n_distinct": len(set(present)),
        "origin": "column",
    }
    distinct = list(dict.fromkeys(present))[:5]
    if present and all(v.lower() in ("true", "false") for v in present):
        return ColumnProfile(
            dtype="boolean", sample=[v.lower() == "true" for v in distinct], **base
        )
    numbers = [_number(v) for v in present]
    if present and all(n is not None for n in numbers):
        nums = [n for n in numbers if n is not None]
        integral = all(n.is_integer() for n in nums)
        return ColumnProfile(
            dtype="integer" if integral else "number",
            min=min(nums),
            max=max(nums),
            monotonic=all(a <= b for a, b in itertools.pairwise(nums)) if len(nums) > 1 else None,
            sample=[int(n) if integral else n for n in nums[:5]],
            **base,
        )
    dates = [_date(v) for v in present]
    if present and all(d is not None for d in dates):
        days = [d for d in dates if d is not None]
        uniq = sorted(set(days))
        return ColumnProfile(
            dtype="date",
            min_date=uniq[0].isoformat(),
            max_date=uniq[-1].isoformat(),
            detected_freq=_freq(uniq),
            monotonic=all(a <= b for a, b in itertools.pairwise(days)) if len(days) > 1 else None,
            sample=[v[:200] for v in distinct],
            **base,
        )
    return ColumnProfile(dtype="string", sample=[v[:200] for v in distinct], **base)


def profile_csv(path: Path, *, max_rows: int = MAX_PROFILE_ROWS) -> list[ColumnProfile]:
    """One profile per column of a CSV file with a header row (the first ``max_rows`` data
    rows are read)."""
    rows = _rows(path, max_rows)
    try:
        header = next(rows)
    except StopIteration:
        raise ValueError(f"{path.name} is empty") from None
    if not header or len(header) > MAX_COLUMNS:
        raise ValueError(f"{path.name}: expected a header row with 1 to {MAX_COLUMNS} columns")
    if len(set(header)) != len(header):
        raise ValueError(f"{path.name}: column names must be unique")
    columns: list[list[str]] = [[] for _ in header]
    for line, row in enumerate(rows, start=2):
        if len(row) != len(header):
            raise ValueError(f"{path.name} line {line}: {len(row)} cells, expected {len(header)}")
        for cells, value in zip(columns, row, strict=True):
            cells.append(value)
    return [_profile(name, cells) for name, cells in zip(header, columns, strict=True)]
