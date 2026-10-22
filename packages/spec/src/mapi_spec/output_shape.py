"""The shape of a score output: how a dashboard (and the Score page) draws it (v0.17).

A pure-Python port of ``apps/web/src/features/score/shape.ts`` ``classify()`` over one
:class:`~mapi_spec.api.ScoreSummaryTable`: an output the run wrote to ``ui/fields.json`` is a
``grid`` (a map with a frame slider); an output the run drew on areas (``sci.area_map``) is
``areal``; one the run judged as a classifier (``core.roc_pr``, ``core.confusion``,
``core.calibration``) is ``classification``; else the table's dims decide: no dims is a
``scalar``, a time dim is ``time``, other dims are ``categorical``. The golden file
``tests/golden/output_shape.json`` holds the same answers as the TypeScript function.

The one addition to ``shape.ts``: an :class:`~mapi_spec.capabilities.OutputDecl` that says
its numbers are class probabilities (``meaning: class_probability``, v0.17) is a
``classification`` even when the run drew no classifier plot. Without a declaration (or with
``decl=None``) the answer is always the one of ``shape.ts``.

No numpy, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from mapi_spec.api import ScoreSummaryTable, SummaryCell
from mapi_spec.capabilities import OutputDecl
from mapi_spec.dashboard_v2 import OutputShape

__all__ = [
    "AREA_PLOT_IDS",
    "CENTERS",
    "CLASSIFICATION_PLOT_IDS",
    "FIELD_PLOT_IDS",
    "MAX_SERIES",
    "OutputShape",
    "band_of",
    "centre_of",
    "classify_table",
    "is_date_dim",
    "is_stat",
    "quantile_of",
    "series_dim",
    "table_dims",
    "table_stats",
    "time_dim_of",
]

FIELD_PLOT_IDS: Final[tuple[str, ...]] = ("sci.field_map", "sci.vector_field")
"""The run plots that a field viewer replaces when ``ui/fields.json`` is there."""
AREA_PLOT_IDS: Final[tuple[str, ...]] = ("sci.area_map",)
"""The run plots that draw an output on areas."""
CLASSIFICATION_PLOT_IDS: Final[tuple[str, ...]] = (
    "core.roc_pr",
    "core.confusion",
    "core.calibration",
)
"""The run plots that judge a classifier."""
CENTERS: Final[tuple[str, ...]] = ("mean", "median", "point", "q50")
"""Centre columns, in the order ``compare.ts`` ``CENTERS`` tries them."""
MAX_SERIES: Final = 12
"""The most levels a second dim can have and still be drawn as coloured lines."""

_TIME_NAME: Final = re.compile(r"^(time|date|week|month|quarter|year|day|period|t|ds|step)$", re.I)
_ISO_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}")
_QUANTILE: Final = re.compile(r"^q(\d+)(?:_(\d+))?$")
_DIGITS: Final = re.compile(r"\d+")
_PROBABILITY_MEANINGS: Final = frozenset({"class_probability"})


def quantile_of(name: str) -> float | None:
    """The quantile (in percent) a ``q<pct>[_<fraction>]`` column holds, else ``None``."""
    m = _QUANTILE.match(name)
    return float(f"{m.group(1)}.{m.group(2) or '0'}") if m else None


def is_stat(name: str) -> bool:
    """Whether a column name is a statistic (a centre, a bound, a quantile or ``sd``)."""
    return (
        name in CENTERS
        or name.endswith("_lower")
        or name.endswith("_upper")
        or quantile_of(name) is not None
        or name == "sd"
    )


def table_dims(table: ScoreSummaryTable) -> list[str]:
    """The coordinate columns of a table: its ``dims``, else the leading columns that are not
    statistics."""
    if table.dims is not None:
        return [d for d in table.dims if d in table.columns]
    first = next((i for i, c in enumerate(table.columns) if is_stat(c)), -1)
    return [] if first < 0 else list(table.columns[:first])


def table_stats(table: ScoreSummaryTable) -> list[str]:
    """The statistic columns of a table (everything after the dims)."""
    dims = set(table_dims(table))
    return [c for c in table.columns if c not in dims]


def _column(table: ScoreSummaryTable, name: str) -> list[SummaryCell]:
    try:
        i = table.columns.index(name)
    except ValueError:
        return []
    return [r[i] if i < len(r) else None for r in table.rows]


def is_date_dim(table: ScoreSummaryTable, dim: str) -> bool:
    """Whether the dim's values are ISO dates (a temporal axis), not labels or numbers."""
    values = [v for v in _column(table, dim) if v is not None]
    return bool(values) and all(isinstance(v, str) and _ISO_DATE.match(v) for v in values)


def _levels(table: ScoreSummaryTable, dim: str) -> int:
    # JS Set semantics: true is not 1, and 1 is 1.0
    return len({(type(v) is bool, v) for v in _column(table, dim)})


def time_dim_of(table: ScoreSummaryTable) -> str | None:
    """The time dim of a table: its ``time_dim``, else the first ISO-date dim, else the first
    dim with a time name (``date``, ``week``, ``t``, ...). ``None`` for a circular output."""
    if table.period is not None:
        return None
    dims = table_dims(table)
    if table.time_dim and table.time_dim in dims:
        return table.time_dim
    return next((d for d in dims if is_date_dim(table, d)), None) or next(
        (d for d in dims if _TIME_NAME.match(d)), None
    )


def series_dim(table: ScoreSummaryTable) -> str | None:
    """The one other dim of a time table that has at most :data:`MAX_SERIES` levels (drawn as
    coloured lines), else ``None``."""
    time = time_dim_of(table)
    if time is None:
        return None
    others = [d for d in table_dims(table) if d != time]
    if len(others) == 1 and _levels(table, others[0]) <= MAX_SERIES:
        return others[0]
    return None


def centre_of(stats: Sequence[str]) -> str | None:
    """The centre column of a table's statistics (``mean``, ``median``, ``point``, ``q50``)."""
    return next((c for c in CENTERS if c in stats), None)


def band_of(stats: Sequence[str]) -> tuple[str, str] | None:
    """The widest interval of the statistics as ``(lower, upper)`` columns: a
    ``<x>_lower``/``<x>_upper`` pair, else the outer quantiles (``compare.ts`` ``bandOf``)."""
    pairs = [s[:-6] for s in stats if s.endswith("_lower") and f"{s[:-6]}_upper" in stats]
    if pairs:

        def level(b: str) -> int:
            m = _DIGITS.search(b)
            return int(m.group(0)) if m else 0

        widest = sorted(pairs, key=level, reverse=True)[0]
        return f"{widest}_lower", f"{widest}_upper"
    qs = [(s, q) for s in stats if (q := quantile_of(s)) is not None and q != 50]
    lows = sorted((x for x in qs if x[1] < 50), key=lambda x: x[1])
    highs = sorted((x for x in qs if x[1] > 50), key=lambda x: -x[1])
    return (lows[0][0], highs[0][0]) if lows and highs else None


def classify_table(
    table: ScoreSummaryTable, decl: OutputDecl | None, *, has_field: bool
) -> OutputShape:
    """The shape of one output (``shape.ts`` ``classify``).

    ``has_field``: the run's ``ui/fields.json`` holds this output. ``decl``: the output's
    manifest declaration; ``meaning: class_probability`` makes it a classification (after
    the grid and area checks)."""
    if has_field:
        return "grid"
    named = table.plots or []
    if any(p in named for p in AREA_PLOT_IDS):
        return "areal"
    if any(p in named for p in CLASSIFICATION_PLOT_IDS):
        return "classification"
    if decl is not None and decl.meaning in _PROBABILITY_MEANINGS:
        return "classification"
    if not table_dims(table):
        return "scalar"
    if time_dim_of(table) is not None:
        return "time"
    return "categorical"
