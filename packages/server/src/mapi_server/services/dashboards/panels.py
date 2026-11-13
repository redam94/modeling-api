"""The panels of a layout 2 readout (v0.17, I-3, I-6): every panel of a
:class:`~mapi_spec.dashboard_v2.DashboardSpecV2` resolved into a capped snapshot block.

Everything here is pure (no I/O, no numpy): :func:`readouts._build_v2
<mapi_server.services.dashboards.readouts>` reads the artefacts into a :class:`Material`
(the score summary, the guided views, the fields view, the fit metrics, the scorecard, the
accuracy of uploaded actuals, the accuracy history over published readouts, the previous
published panel values and the revision's pictures) and :func:`build_snapshot_v2` turns it
into a :class:`~mapi_spec.dashboards.ReadoutSnapshot` with ``schema_version == 2``.

Rules (plan D5-D9, D16):

* one panel never fails a readout: a source that is missing, a reduction the numbers do
  not allow, or a block over its byte budget becomes an ``unavailable`` block with a plain
  reason;
* **no interval is ever built by adding interval bounds.** A total keeps a range only when
  the score run aggregated the draws (``OutputSelector.aggregate``) or a guided view holds
  it (``ForecastView.total``, the forecast fan of a sum); otherwise the total of the
  centres is shown without a range and marked ``approximate``;
* the centre of a table is the first of ``mean, median, q50, p50, point``; its range the
  widest ``*_lower``/``*_upper`` pair (or the outer quantiles);
* ``band: "outcome"`` shows the range the run gave and says so when it is the range of the
  average (an expected-value run, or an output without single-result ranges);
* the legacy fields stay filled for v0.16-era readers: ``tiles`` (``key="panel"``),
  ``series`` (the subject output) and ``accuracy`` (error metrics).

v0.19 (D16): a recipe in an uploaded namespace is never drawn as a picture: clients and
share links see the readout, and nothing made with uploaded code leaves the workspace.

Leads are plain sentences filled from numbers only, at most 400 characters, and never use
media words outside the ``mmm_planner`` preset.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from mapi_spec.api import ScoreSummary, ScoreSummaryTable
from mapi_spec.calibration import DistributionSummary, FanChart
from mapi_spec.dashboard_metrics import (
    classification_metrics_of,
    effect_of,
    error_metrics_of,
    multiclass_metrics_of,
    normalise_label,
)
from mapi_spec.dashboard_presets import GENERIC_NOUNS, LEADS
from mapi_spec.dashboard_v2 import (
    MAX_PANELS,
    MAX_SERIES_POINTS_V2,
    RECIPE_INTENTS,
    SNAPSHOT_SCHEMA_VERSION,
    AccuracyMetric,
    AccuracyPoint,
    AccuracySource,
    BarRow,
    BarsBlock,
    BatchClassCount,
    BatchDownload,
    BatchStats,
    BatchSummary,
    CalibrationBlock,
    CatalogOutput,
    ChartIntent,
    ChartPanel,
    ClassAccuracy,
    ConfusionBlock,
    DashboardSpecV2,
    EffectAccuracy,
    ErrorAccuracy,
    HistogramBin,
    KpiBlock,
    KpiPanel,
    MetricSource,
    MmmPanel,
    NoteBlock,
    NotePanel,
    OutputSource,
    PictureBlock,
    PredictionResult,
    PredictionRow,
    PredictionValue,
    PresetKey,
    RecipePanel,
    Reduce,
    ScatterBlock,
    ScatterPoint,
    ScorecardSource,
    SeriesBlock,
    SourceCatalog,
    TableBlock,
    TablePanel,
    UnavailableBlock,
    ViewSource,
    block_budget,
    block_bytes,
)
from mapi_spec.dashboards import (
    MAX_SERIES_POINTS,
    ActualsAccuracy,
    KpiValue,
    ReadoutSnapshot,
    Series,
    SeriesPoint,
    TileUnit,
)
from mapi_spec.output_shape import band_of, table_dims, table_stats, time_dim_of
from mapi_spec.scorecard import Scorecard
from mapi_spec.sharing import uses_uploaded_ids
from mapi_spec.views import CounterfactualView, FieldsView, ForecastView
from mapi_spec.xrjson import XrJsonDataset
from pydantic import ValidationError

from mapi_server.services.dashboards.fields import field_block, field_entry
from mapi_server.services.tracking import lttb

__all__ = [
    "CENTRES",
    "AssetInfo",
    "Cell",
    "Material",
    "MetricValue",
    "Pair",
    "Table",
    "accuracy_kind",
    "accuracy_of",
    "accuracy_point",
    "actual_cells",
    "batch_summary",
    "build_snapshot_v2",
    "history_point",
    "join_actuals",
    "lead_for",
    "prediction_result",
    "resolve_panels",
    "run_band",
    "table_of",
]

CENTRES: Final = ("mean", "median", "q50", "p50", "point")
"""The centre columns of a summary table, in the order a v2 readout looks for them."""
_CLASS_DIMS: Final = ("class", "label", "category")
_STATE_DIMS: Final = ("state", "regime")
_MAX_LEGACY_TILES: Final = 6
_MAX_SERIES: Final = 12
_ISO_DAY: Final = re.compile(r"^\d{4}-\d{2}-\d{2}")
_UNSAFE: Final = re.compile(r"[\x00-\x1f\x7f<>&]")
_MINUS: Final = "−"

_PROBABILITY_METRICS: Final = frozenset(
    {"accuracy", "accuracy_naive", "auc", "coverage_80", "coverage_90", "coverage_95",
     "one_step_coverage_90", "p_effect_positive"}
)  # fmt: skip
_PERCENT_METRICS: Final = frozenset({"mape", "smape", "wape"})
_FRACTION_PERCENT_METRICS: Final = frozenset({"effect_relative", "pre_mape"})
"""Fit metrics the causal plugins log as fractions: shown in percent."""
_SUBJECT_METRICS: Final = frozenset({"effect_total", "effect_average", "att", "mae", "rmse"})

_ACCURACY_UNITS: Final[Mapping[str, TileUnit]] = {
    "n": "count", "mape": "percent", "wape": "percent", "smape": "percent",
    "band_hit_rate": "percent", "accuracy": "probability", "auc": "probability",
    "brier": "number", "log_score": "number", "r2": "number", "effect_relative": "percent",
}  # fmt: skip

AVERAGE_RANGE: Final = "The range shows the average, not single results."
OUTCOME_RANGE: Final = "The range shows where one new result will probably be."
NO_ACTUALS: Final = "This shows after you send actual results."
NO_TOTAL_RANGE: Final = "The ranges of the parts cannot be added up, so the total has no range."


# ==================================================================================== material


@dataclass(frozen=True, slots=True)
class MetricValue:
    """One metric the fit run logged (``labels`` ``{}``: its scalar value)."""

    key: str
    labels: Mapping[str, str]
    last: float | None


@dataclass(frozen=True, slots=True)
class AssetInfo:
    """A picture of the revision (``status``: queued, ready or failed)."""

    asset_id: str
    status: str
    width: int | None = None
    height: int | None = None
    problem: str | None = None


@dataclass(frozen=True, slots=True)
class Pair:
    """One prediction joined to its actual (``t``: the period, when the output has one)."""

    label: str
    t: str | None
    predicted: float
    actual: float
    low: float | None = None
    high: float | None = None
    coords: dict[str, str] = field(default_factory=dict)
    """The table coordinates of the key (so a chart of selected series joins only theirs)."""


@dataclass(slots=True)
class Material:
    """What one v2 readout is built from (all read and validated by the caller).

    ``band``: the kind of range the run's summary holds (``outcome`` for a predictive score
    and for quantile/interval models, whose range is already one of single results;
    ``mean`` for an expected-value score of draws). ``mode``: the score mode of the run.
    ``previous``: the KPI values of the previous published readout, by panel id."""

    kind: str = "baseline"
    mode: str | None = None
    summary: ScoreSummary | None = None
    forecast: ForecastView | None = None
    counterfactual: CounterfactualView | None = None
    fields: FieldsView | None = None
    metrics: Sequence[MetricValue] = ()
    scorecard: Scorecard | None = None
    catalog: SourceCatalog | None = None
    band: Literal["outcome", "mean"] = "mean"
    accuracy: ErrorAccuracy | ClassAccuracy | EffectAccuracy | None = None
    pairs: Sequence[Pair] = ()
    history: Sequence[AccuracyPoint] = ()
    previous: Mapping[str, float] = field(default_factory=dict)
    sent: Sequence[Mapping[str, str]] = ()
    """The coordinates of the rows an actuals upload matched (``sent_rows``): charts and
    KPIs of an actuals readout show only the cases the client sent, never the whole grid."""
    previous_captions: Mapping[str, str] = field(default_factory=dict)
    """The previous readout's KPI tile captions by panel id (they name a picked case)."""
    previous_same_inputs: bool = True
    """Whether the previous readout scored the same inputs (its ``input_fingerprint``)."""
    assets: Mapping[str, AssetInfo] = field(default_factory=dict)


def run_band(output: Mapping[str, Any] | None, representation: str | None) -> Literal[
    "outcome", "mean"
]:  # fmt: skip
    """The kind of range of a run that asked ``output`` (its request's output selector) of
    a model of ``representation``."""
    if representation in ("quantiles", "interval"):
        return "outcome"
    kind = (output or {}).get("kind")
    return "outcome" if kind == "predictive" else "mean"


# ===================================================================================== tables


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    x = float(v)
    return x if math.isfinite(x) else None


def _label(v: object) -> str:
    """A coordinate as plain text (ISO timestamps as their day; at most 64 characters)."""
    if v is None:
        return ""
    text = "true" if v is True else "false" if v is False else str(v)
    if _ISO_DAY.match(text) and len(text) > 10 and text[10] in "T ":
        text = text[:10]
    return _UNSAFE.sub("", text)[:64]


@dataclass(frozen=True, slots=True)
class Cell:
    """One row of a summary table: its coordinates (as text), centre and range."""

    coords: Mapping[str, str]
    centre: float | None
    low: float | None
    high: float | None


@dataclass(frozen=True, slots=True)
class Table:
    """A summary table read for panels (``centre_col``: the centre statistic it holds)."""

    variable: str
    dims: list[str]
    time_dim: str | None
    centre_col: str | None
    banded: bool
    cells: list[Cell]

    @property
    def class_dim(self) -> str | None:
        return next((d for d in _CLASS_DIMS if d in self.dims), None)

    def levels(self, dim: str, cells: Sequence[Cell] | None = None) -> list[str]:
        return list(dict.fromkeys(c.coords.get(dim, "") for c in (cells or self.cells)))


def table_of(summary: ScoreSummary | None, variable: str) -> Table | None:
    """The table of ``variable`` (no fallback to another output), or ``None``."""
    if summary is None:
        return None
    t = next((x for x in summary.tables if x.variable == variable), None)
    return _read(t) if t is not None else None


def with_centres(banded: ScoreSummary, centres: ScoreSummary) -> ScoreSummary:
    """``banded`` (a predictive score: its range is one of single results) with the centre
    column of each table taken from the same table of ``centres`` (an expected score of the
    same inputs: the expected mean), matched by coordinates. Cells ``centres`` lacks keep
    their own centre."""
    tables: list[ScoreSummaryTable] = []
    for t in banded.tables:
        other = next((x for x in centres.tables if x.variable == t.variable), None)
        stats = table_stats(t)
        centre = next((c for c in CENTRES if c in stats), None)
        if other is None or centre is None or centre not in other.columns:
            tables.append(t)
            continue
        dims = table_dims(t)
        oi = {c: i for i, c in enumerate(other.columns)}
        if any(d not in oi for d in dims):
            tables.append(t)
            continue
        by_key = {
            tuple(_label(r[oi[d]]) for d in dims): r[oi[centre]]
            for r in other.rows
            if len(r) == len(other.columns)
        }
        ti = {c: i for i, c in enumerate(t.columns)}
        rows = []
        for r in t.rows:
            row = list(r)
            k = tuple(_label(r[ti[d]]) if ti[d] < len(r) else "" for d in dims)
            if k in by_key and ti[centre] < len(row):
                row[ti[centre]] = by_key[k]
            rows.append(row)
        tables.append(t.model_copy(update={"rows": rows}))
    return banded.model_copy(update={"tables": tables})


def _read(t: ScoreSummaryTable) -> Table:
    dims = table_dims(t)
    stats = table_stats(t)
    centre = next((c for c in CENTRES if c in stats), None)
    band = band_of(stats)
    idx = {c: i for i, c in enumerate(t.columns)}

    def at(row: Sequence[object], col: str | None) -> object:
        i = idx.get(col) if col is not None else None
        return row[i] if i is not None and i < len(row) else None

    cells = [
        Cell(
            coords={d: _label(at(r, d)) for d in dims},
            centre=_num(at(r, centre)),
            low=_num(at(r, band[0])) if band else None,
            high=_num(at(r, band[1])) if band else None,
        )
        for r in t.rows
    ]
    return Table(t.variable, dims, time_dim_of(t), centre, band is not None, cells)


def _select(cells: Sequence[Cell], select: Mapping[str, str | int]) -> list[Cell]:
    want = {k: normalise_label(_label(v)) for k, v in select.items()}
    return [
        c for c in cells if all(normalise_label(c.coords.get(k, "")) == v for k, v in want.items())
    ]


def _coords_text(coords: Mapping[str, str], dims: Iterable[str]) -> str:
    return ", ".join(f"{d} {coords[d]}" for d in dims if coords.get(d))[:120]


def _varying(table: Table, cells: Sequence[Cell]) -> list[str]:
    return [d for d in table.dims if len(table.levels(d, cells)) > 1]


_NUMBER = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")


def period_key(label: str) -> tuple[int, float, str]:
    """The order of a period label: numbers (integer steps ``0, 1, …, 10``) by value, other
    labels (ISO dates) as text after them."""
    if _NUMBER.match(label.strip()):
        try:
            return (0, float(label), label)
        except ValueError:
            pass
    return (1, 0.0, label)


def _series_keys(table: Table, cells: Iterable[Cell]) -> set[tuple[str, ...]]:
    keep = [d for d in table.dims if d not in (table.time_dim, table.class_dim)]
    return {tuple(c.coords.get(d, "") for d in keep) for c in cells}


def _whole_forecast(table: Table, cells: Sequence[Cell], fc: ForecastView | None) -> bool:
    """Whether the forecast view (its fan, total and history: the KPI summed over
    ``summed_over``) is the value of ``cells``: the view is of this output, every other dim
    of the table that varies is one the view sums over, and ``cells`` hold every series (no
    selection narrows them)."""
    if fc is None or fc.kpi != table.variable:
        return False
    others = [d for d in _varying(table, table.cells) if d not in (table.time_dim, table.class_dim)]
    if any(d not in fc.summed_over for d in others):
        return False
    return _series_keys(table, cells) == _series_keys(table, table.cells)


# ===================================================================================== values


@dataclass(slots=True)
class _Value:
    centre: float | None
    low: float | None = None
    high: float | None = None
    caption: str | None = None
    approximate: bool = False
    unit: TileUnit = "number"
    banded: bool = False
    case: bool = False
    """The value is of one case picked among several (named in the caption): a change is
    shown only against the same case of the same inputs."""


class _Unavailable(Exception):
    """The panel cannot be shown; ``str(exc)`` is the plain reason."""


def _output(mat: Material, name: str) -> CatalogOutput | None:
    if mat.catalog is None:
        return None
    return next((o for o in mat.catalog.outputs if o.name == name), None)


def _is_probability(spec: DashboardSpecV2, mat: Material, table: Table | None, name: str) -> bool:
    o = _output(mat, name)
    if o is not None:
        return o.meaning == "class_probability" or o.shape == "classification"
    if table is not None and table.class_dim is not None:
        return True
    return name == spec.subject.variable and spec.subject.unit == "probability"


def _unit_of(spec: DashboardSpecV2, mat: Material, table: Table | None, name: str) -> TileUnit:
    if name == spec.subject.variable:
        return spec.subject.unit
    return "probability" if _is_probability(spec, mat, table, name) else "number"


def _need_table(mat: Material, variable: str) -> Table:
    table = table_of(mat.summary, variable)
    if table is None:
        raise _Unavailable(f"This readout has no values of {variable}.")
    return table


def _fan_at(fan: FanChart, coord: str) -> tuple[float | None, float | None, float | None] | None:
    """Median and outer quantiles of a fan at ``coord`` (``None`` when absent)."""
    coords = [_label(c) for c in fan.coords]
    if coord not in coords:
        return None
    j = coords.index(coord)
    qs = fan.quantiles

    def q(level: float) -> float | None:
        i = min(range(len(qs)), key=lambda k: abs(qs[k] - level))
        return _num(fan.values[i][j])

    return q(0.5), q(qs[0]), q(qs[-1])


def _dist(d: DistributionSummary) -> tuple[float | None, float | None, float | None]:
    qs = {float(k): v for k, v in d.quantiles.items()}
    lows = sorted(k for k in qs if k < 0.5)
    highs = sorted(k for k in qs if k > 0.5)
    lo = qs.get(0.05, qs[lows[0]] if lows else None)
    hi = qs.get(0.95, qs[highs[-1]] if highs else None)
    return _num(d.mean), _num(lo), _num(hi)


def _cells_value(
    table: Table, cells: Sequence[Cell], *, what: str, mat: Material, t: str | None = None
) -> _Value:
    """One value of ``cells``: the cell itself, the forecast fan of their sum at period
    ``t``, or the sum of their centres without a range (``approximate``)."""
    if not cells:
        raise _Unavailable(f"No value matches {what}.")
    if len(cells) == 1:
        c = cells[0]
        return _Value(c.centre, c.low, c.high, banded=table.banded)
    fc = mat.forecast
    if t is not None and fc is not None and _whole_forecast(table, cells, fc):
        got = _fan_at(fc.fan, t)
        if got is not None and got[0] is not None:
            over = ", ".join(fc.summed_over) or "every series"
            return _Value(*got, caption=f"Summed over {over}"[:200], banded=True)
    centres = [c.centre for c in cells if c.centre is not None]
    if not centres:
        raise _Unavailable(f"No value matches {what}.")
    dims = ", ".join(_varying(table, cells)) or "the parts"
    return _Value(
        math.fsum(centres),
        caption=f"The sum over {dims}. {NO_TOTAL_RANGE}"[:200],
        approximate=True,
    )


def _periods(table: Table, cells: Sequence[Cell]) -> list[str]:
    if table.time_dim is None:
        return []
    return sorted({c.coords.get(table.time_dim, "") for c in cells} - {""}, key=period_key)


def _only_sent(table: Table, cells: Sequence[Cell], mat: Material) -> list[Cell]:
    """The cells of the rows the client sent (an actuals readout scores a grid of every id
    × level: its other cells are cases nobody sent); all of them without an upload."""
    if not mat.sent:
        return list(cells)
    dims = [d for d in mat.sent[0] if d in table.dims]
    if not dims:
        return list(cells)
    want = {tuple(normalise_label(r.get(d, "")) for d in dims) for r in mat.sent}
    kept = [c for c in cells if tuple(normalise_label(c.coords.get(d, "")) for d in dims) in want]
    return kept or list(cells)


def _output_value(
    spec: DashboardSpecV2, mat: Material, src: OutputSource, reduce: Reduce
) -> _Value:
    table = _need_table(mat, src.variable)
    cells = _only_sent(table, _select(table.cells, src.select), mat)
    if not cells:
        raise _Unavailable("No value matches the chosen coordinates.")
    how = reduce.how
    unit = _unit_of(spec, mat, table, src.variable)
    if how in ("none", "select"):
        value = _single(table, cells, mat)
    elif how in ("last", "next"):
        value = _period_value(table, cells, mat, how)
    elif how in ("sum", "mean"):
        value = _aggregate(table, cells, mat, how, reduce.over, src.variable)
    elif how == "share_above":
        value = _share_above(spec, mat, table, cells, reduce.threshold or 0.0)
        unit = "percent"
    else:  # top_class
        value = _top_class(table, cells)
        unit = "probability"
    if value.unit == "number":
        value.unit = unit
    return value


def _single(table: Table, cells: Sequence[Cell], mat: Material) -> _Value:
    """``none``/``select``: the one cell; the latest period of one series; else the first
    cell, named in the caption."""
    if len(cells) == 1:
        return _cells_value(table, cells, what="the choice", mat=mat)
    others = [d for d in _varying(table, cells) if d != table.time_dim]
    periods = _periods(table, cells)
    if not others and periods:
        last = [c for c in cells if c.coords.get(table.time_dim or "") == periods[-1]]
        value = _cells_value(table, last, what="the latest period", mat=mat)
        value.caption = value.caption or f"The latest period ({periods[-1]})"
        return value
    first = cells[0]
    value = _cells_value(table, [first], what="the choice", mat=mat)
    value.caption = f"For {_coords_text(first.coords, _varying(table, cells))}"[:200]
    value.case = True
    return value


def _period_value(
    table: Table, cells: Sequence[Cell], mat: Material, how: Literal["last", "next"]
) -> _Value:
    periods = _periods(table, cells)
    if not periods:
        raise _Unavailable(f"{table.variable} has no periods.")
    if how == "next":
        split = _label(mat.forecast.split_at) if mat.forecast is not None else ""
        if split:
            ahead = [p for p in periods if period_key(p) >= period_key(split)]
            t = ahead[0] if ahead else None
        elif mat.mode == "forecast":
            t = periods[0]
        else:
            t = None
        if t is None:
            raise _Unavailable("The next period shows when the model makes a forecast.")
    else:
        t = periods[-1]
    at = [c for c in cells if c.coords.get(table.time_dim or "") == t]
    value = _cells_value(table, at, what=f"the period {t}", mat=mat, t=t)
    value.caption = value.caption or f"Period {t}"
    return value


def _aggregate(
    table: Table,
    cells: Sequence[Cell],
    mat: Material,
    how: Literal["sum", "mean"],
    over: Sequence[str],
    variable: str,
) -> _Value:
    """A total or mean over ``over``: the run already aggregated the draws (the dims are
    gone from the table: the range is the range of the aggregate), the forecast total of a
    guided view, or the centres of point predictions (``approximate``). Never a sum of
    bounds."""
    left = [d for d in over if d in table.dims]
    if not left:
        value = _single(table, cells, mat)
        value.approximate = value.approximate or table.centre_col == "point"
        if value.approximate:
            value.low = value.high = None
        return value
    fc = mat.forecast
    if (
        how == "sum"
        and fc is not None
        and fc.total is not None
        and fc.kpi == variable
        and left == [table.time_dim]
        and _whole_forecast(table, cells, fc)
    ):
        centre, lo, hi = _dist(fc.total)
        return _Value(centre, lo, hi, caption="The total over the forecast", banded=True)
    if table.banded:
        raise _Unavailable(
            "The range of a total needs the model run to add up its draws. "
            "Show the values per period instead."
        )
    groups: dict[tuple[str, ...], list[float]] = {}
    keep = [d for d in table.dims if d not in left]
    for c in cells:
        if c.centre is not None:
            groups.setdefault(tuple(c.coords.get(d, "") for d in keep), []).append(c.centre)
    if not groups:
        raise _Unavailable(f"{variable} has no values to add up.")
    key, xs = next(iter(groups.items()))
    total = math.fsum(xs) if how == "sum" else math.fsum(xs) / len(xs)
    word = "sum" if how == "sum" else "mean"
    caption = f"The {word} over {', '.join(left)} of single predictions (no range)"
    if len(groups) > 1:
        caption += f" for {_coords_text(dict(zip(keep, key, strict=True)), keep)}"
    return _Value(total, caption=caption[:200], approximate=True, case=len(groups) > 1)


def _share_above(
    spec: DashboardSpecV2, mat: Material, table: Table, cells: Sequence[Cell], threshold: float
) -> _Value:
    xs = [c.centre for c in cells if c.centre is not None]
    if not xs:
        raise _Unavailable(f"{table.variable} has no values.")
    k = sum(1 for x in xs if x > threshold)
    o = _output(mat, table.variable)
    noun = "areas" if (o is not None and o.shape == "areal") else "values"
    caption = f"{k} of {len(xs)} {noun} are above {fmt(threshold, _decimals(threshold))}"
    return _Value(k / len(xs) * 100, caption=caption[:200])


def _top_class(table: Table, cells: Sequence[Cell]) -> _Value:
    cdim = table.class_dim
    if cdim is None:
        raise _Unavailable(f"{table.variable} has no classes.")
    groups = _by_case(table, cells)
    if not groups:
        raise _Unavailable(f"{table.variable} has no values.")
    key, probs = next(iter(groups.items()))
    label, p = max(probs.items(), key=lambda kv: kv[1])
    caption = f"Most likely: {label}"
    if len(groups) > 1:
        caption += f" (for {', '.join(key)})"
    return _Value(p, caption=caption[:200], unit="probability", case=len(groups) > 1)


def _by_case(table: Table, cells: Sequence[Cell]) -> dict[tuple[str, ...], dict[str, float]]:
    """Class probabilities per case (the coordinates other than the class dim)."""
    cdim = table.class_dim
    out: dict[tuple[str, ...], dict[str, float]] = {}
    keep = [d for d in table.dims if d != cdim]
    for c in cells:
        if c.centre is None or cdim is None:
            continue
        out.setdefault(tuple(c.coords.get(d, "") for d in keep), {})[c.coords.get(cdim, "")] = (
            c.centre
        )
    return out


def _metric(mat: Material, key: str, labels: Mapping[str, str]) -> float | None:
    for m in mat.metrics:
        if m.key == key and dict(m.labels) == dict(labels):
            return m.last
    return None


def _metric_value(spec: DashboardSpecV2, mat: Material, src: MetricSource) -> _Value:
    labels = dict(src.labels)
    found = [m for m in mat.metrics if m.key == src.key and dict(m.labels) == labels]
    if not found or found[0].last is None:
        raise _Unavailable("The model did not record this number.")
    v = found[0].last
    lo = _metric(mat, f"{src.key}_p05", labels)
    hi = _metric(mat, f"{src.key}_p95", labels)
    unit: TileUnit = "number"
    scale = 1.0
    if src.key in _PROBABILITY_METRICS:
        unit = "probability"
    elif src.key in _PERCENT_METRICS:
        unit = "percent"
    elif src.key in _FRACTION_PERCENT_METRICS:
        unit, scale = "percent", 100.0
    elif src.key in _SUBJECT_METRICS and spec.subject.unit not in ("probability", "label"):
        unit = spec.subject.unit
    return _Value(
        v * scale,
        lo * scale if lo is not None else None,
        hi * scale if hi is not None else None,
        caption=_fit_caption(mat, src.key),
        unit=unit,
        banded=lo is not None and hi is not None,
    )


FIT_CAPTION: Final = "When the model was built"


def _fit_caption(mat: Material, key: str) -> str:
    """A fit metric's caption: when it was measured, so it never reads as the accuracy on
    the client's own cases - which, when this readout has it, follows by name."""
    detail = mat.accuracy
    raw = getattr(detail, key, None) if not isinstance(detail, EffectAccuracy) else None
    n = getattr(detail, "n", 0) if detail is not None else 0
    if not isinstance(raw, int | float) or isinstance(raw, bool) or not n:
        return FIT_CAPTION
    unit = _ACCURACY_UNITS.get(key, "number")
    text = (
        f"{_pct(float(raw))}%" if unit == "probability"
        else f"{fmt(float(raw), 1)}%" if unit == "percent"
        else fmt(float(raw), 2)
    )  # fmt: skip
    return f"{FIT_CAPTION}. On the {n} cases you sent: {text}"[:200]


def _scorecard_value(mat: Material, src: ScorecardSource) -> _Value:
    item = next(
        (i for i in (mat.scorecard.items if mat.scorecard else []) if i.check == src.check), None
    )
    if item is None:
        raise _Unavailable("The model check is not available.")
    return _Value(_num(item.value), caption=item.title[:200])


def _view_value(spec: DashboardSpecV2, mat: Material, src: ViewSource) -> _Value:
    unit = spec.subject.unit if spec.subject.unit != "label" else "number"
    if src.view == "forecast_total":
        if mat.forecast is None or mat.forecast.total is None:
            raise _Unavailable("The total shows when the model makes a forecast with draws.")
        c, lo, hi = _dist(mat.forecast.total)
        n = max(0, len(mat.forecast.fan.coords) - mat.forecast.history)
        caption = f"The total over the next {n} periods" if n else "The total over the forecast"
        return _Value(c, lo, hi, caption=caption, unit=unit, banded=True)
    if src.view == "counterfactual_delta":
        if mat.counterfactual is None:
            raise _Unavailable("The change shows after a comparison run.")
        c, lo, hi = _dist(mat.counterfactual.delta_total)
        return _Value(c, lo, hi, caption="The change against the case without it", unit=unit,
                      banded=True)  # fmt: skip
    cond = mat.summary.conditioning if mat.summary is not None else None
    if cond is None:
        raise _Unavailable("This shows after you send actual results.")
    return _Value(float(cond.n_actuals), caption=cond.message[:200], unit="count")


def _accuracy_value(mat: Material, metric: AccuracyMetric) -> _Value:
    detail = mat.accuracy
    v: float | None = None
    lo = hi = None
    if detail is not None:
        if isinstance(detail, EffectAccuracy):
            if metric == "effect_total":
                v, lo, hi = detail.total, detail.low, detail.high
            elif metric == "effect_relative":
                v = detail.relative
            elif metric == "n":
                v = float(detail.n_periods)
        else:
            raw = getattr(detail, metric, None)
            v = float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else None
    if v is None:
        last = next((p for p in reversed(mat.history) if p.values.get(metric) is not None), None)
        if last is not None:
            v = last.values.get(metric)
            return _Value(v, caption=f"From the results sent on {last.created_at:%d/%m/%Y}",
                          unit=_ACCURACY_UNITS.get(metric, "number"))  # fmt: skip
    if v is None:
        raise _Unavailable(NO_ACTUALS)
    n = detail.n_periods if isinstance(detail, EffectAccuracy) else getattr(detail, "n", 0)
    return _Value(
        v,
        lo,
        hi,
        caption=f"On the {n} cases you sent" if n else None,
        unit=_ACCURACY_UNITS.get(metric, "number"),
        banded=lo is not None and hi is not None,
    )


# ====================================================================================== blocks


def _band_caption(
    band: str, mat: Material, spec: DashboardSpecV2, variable: str | None, banded: bool
) -> str | None:
    if band == "none" or not banded or variable is None:
        return None
    o = _output(mat, variable)
    ok = o.predictive_ok if o is not None else True
    if band == "outcome" and (mat.band != "outcome" or not ok):
        return AVERAGE_RANGE
    if (
        band == "mean"
        and mat.band == "outcome"
        and ok
        and o is not None
        and o.meaning not in ("rate", "effect")
        and variable == spec.subject.variable
    ):
        # only the observed subject gets the noise of single results in a predictive
        # score: a rate, a field total or another latent output keeps the range of its
        # average, as the analyst asked - no sentence about "one new result" there
        return OUTCOME_RANGE
    return None


def _join(*parts: str | None) -> str:
    """Caption parts as sentences: a part without an end mark gets a full stop when another
    part follows ("Period 2012-10-01. The range…", never two sentences run together)."""
    kept = [p.strip() for p in parts if p and p.strip()]
    if len(kept) > 1:
        kept = [p if p[-1] in ".!?:)" else p + "." for p in kept[:-1]] + kept[-1:]
    return " ".join(kept)[:200]


def _direction(delta: float | None) -> Literal["up", "down", "flat"]:
    if delta is None or abs(delta) < 0.005:
        return "flat"
    return "up" if delta > 0 else "down"


def _kpi(p: KpiPanel, spec: DashboardSpecV2, mat: Material) -> KpiBlock:
    src = p.source
    variable: str | None = None
    if isinstance(src, OutputSource):
        value = _output_value(spec, mat, src, p.reduce)
        variable = src.variable
    elif isinstance(src, MetricSource):
        value = _metric_value(spec, mat, src)
    elif isinstance(src, ScorecardSource):
        value = _scorecard_value(mat, src)
    elif isinstance(src, ViewSource):
        value = _view_value(spec, mat, src)
        variable = spec.subject.variable if src.view == "forecast_total" else None
    else:
        assert isinstance(src, AccuracySource)
        value = _accuracy_value(mat, src.metric)
    shown = {"centre": value.centre, "value": value.centre, "low": value.low, "high": value.high}
    v = shown[p.stat]
    low, high = (None, None) if p.band == "none" or p.stat != "centre" else (value.low, value.high)
    if isinstance(src, MetricSource) and p.stat == "value":
        # a fit metric's own recorded 90% interval (``<key>_p05``/``_p95``, e.g. the effect
        # of a causal model) is part of the number, not a band of a score
        low, high = value.low, value.high
    note = _band_caption(p.band, mat, spec, variable, value.banded and low is not None)
    caption = _join(p.caption or value.caption, note)
    prev = mat.previous.get(p.id) if p.compare == "previous" else None
    if value.case and not (mat.previous_same_inputs and mat.previous_captions.get(p.id) == caption):
        prev = None  # a picked case: never compared with a different case or other inputs
    delta = (v - prev) / abs(prev) if v is not None and prev is not None and prev != 0 else None
    tile = KpiValue(
        key="panel",
        label=p.title,
        value=v,
        unit=value.unit,
        delta=delta,
        delta_basis="the readout before" if delta is not None else None,
        delta_dir=_direction(delta),
        low=low,
        high=high,
        caption=caption,
        panel_id=p.id,
        approximate=value.approximate,
    )
    return KpiBlock(
        panel_id=p.id, title=p.title, caption=p.caption, approximate=value.approximate, tile=tile
    )


def _points(points: list[SeriesPoint], n: int) -> list[SeriesPoint]:
    if len(points) <= n:
        return points
    ys = [p.mean if p.mean is not None else (p.actual or 0.0) for p in points]
    keep = lttb([float(i) for i in range(len(points))], ys, max(2, n))
    return [points[i] for i in keep]


@dataclass(frozen=True, slots=True)
class _Chart:
    """The parts of a chart panel (a ``chart`` panel, or a native ``recipe`` panel)."""

    id: str
    title: str
    caption: str | None
    intent: ChartIntent
    source: OutputSource | AccuracySource
    reduce: Reduce
    band: str
    facet: str | None
    max_items: int


def _actual_by_t(mat: Material, select: Mapping[str, str | int] | None = None) -> dict[str, float]:
    """The actual per period, added up over the joined series that ``select`` keeps (a
    chart of one selected series gets that series' actuals only)."""
    want = {k: normalise_label(_label(v)) for k, v in (select or {}).items()}
    out: dict[str, float] = {}
    for p in mat.pairs:
        if p.t is None:
            continue
        if any(k in p.coords and normalise_label(p.coords[k]) != v for k, v in want.items()):
            continue
        out[p.t] = out.get(p.t, 0.0) + p.actual
    return out


def _x_dim(c: _Chart, table: Table, cells: Sequence[Cell]) -> str | None:
    """The x axis: the time dim, else the first varying dim that does not split lines."""
    if table.time_dim is not None:
        return table.time_dim
    varying = _varying(table, cells) or list(table.dims)
    lines = {c.facet, "component", *_STATE_DIMS}
    return next((d for d in varying if d not in lines), varying[0] if varying else None)


def _split_dim(c: _Chart, table: Table, cells: Sequence[Cell], x_dim: str | None) -> str | None:
    """The dim that splits the chart into lines (``facet``, else a component or state dim,
    else the one other varying dim); ``_Unavailable`` when more dims vary."""
    others = [d for d in _varying(table, cells) if d != x_dim]
    if c.facet is not None:
        if c.facet not in table.dims:
            raise _Unavailable(f"{c.facet} is not part of {table.variable}.")
        pick: str | None = c.facet
    else:
        prefer = [d for d in others if d == "component" or d in _STATE_DIMS]
        pick = prefer[0] if prefer else (others[0] if len(others) == 1 else None)
    rest = [d for d in others if d != pick]
    if rest:
        raise _Unavailable(
            f"Choose values of {', '.join(rest)} to draw this chart (it has too many lines)."
        )
    return pick


def _series_block(c: _Chart, spec: DashboardSpecV2, mat: Material) -> SeriesBlock:
    assert isinstance(c.source, OutputSource)
    if c.intent == "diverging" and mat.counterfactual is not None:
        fan = mat.counterfactual.delta_fan
        if fan is not None:
            return _fan_block(c, fan, None, "The change")
    table = _need_table(mat, c.source.variable)
    cells = _select(table.cells, c.source.select)
    if not cells:
        raise _Unavailable("No value matches the chosen coordinates.")
    fc = mat.forecast
    split_at = _label(fc.split_at) if fc is not None and fc.split_at is not None else None
    x_dim = _x_dim(c, table, cells)
    x_kind: Literal["time", "category"] = "time" if table.time_dim is not None else "category"
    try:
        split = _split_dim(c, table, cells, x_dim)
    except _Unavailable:
        if fc is not None and _whole_forecast(table, cells, fc):  # the fan of the sum
            return _fan_block(c, fc.fan, split_at, table.variable)
        raise
    if split is not None and len(table.levels(split, cells)) > _MAX_SERIES:
        raise _Unavailable(f"{split} has more than {_MAX_SERIES} values: choose some.")
    keep_band = c.band != "none"
    actual = _actual_by_t(mat, c.source.select) if split is None else {}
    whole = _whole_forecast(table, cells, fc)
    series: list[Series] = []
    groups: dict[str, list[Cell]] = {}
    for cell in cells:
        groups.setdefault(cell.coords.get(split, "") if split else "", []).append(cell)
    for level, members in list(groups.items())[:_MAX_SERIES]:
        pts = [
            SeriesPoint(
                t=(cell.coords.get(x_dim, "") if x_dim else str(i))[:32],
                mean=cell.centre,
                low=cell.low if keep_band else None,
                high=cell.high if keep_band else None,
                actual=actual.get(cell.coords.get(x_dim, "")) if x_dim else None,
            )
            for i, cell in enumerate(members)
        ]
        if x_kind == "time":
            pts.sort(key=lambda p: period_key(p.t))
        if split is None and whole and fc is not None and fc.history:
            pts = _history(fc) + [p for p in pts if p.t not in {h.t for h in _history(fc)}]
        series.append(
            Series(
                name=f"{table.variable}:{level}"[:128] if level else table.variable[:128],
                label=(level or c.title)[:80],
                points=_points(pts, MAX_SERIES_POINTS),
            )
        )
    note = _band_caption(c.band, mat, spec, table.variable, table.banded and keep_band)
    return SeriesBlock(
        panel_id=c.id,
        title=c.title,
        caption=_join(c.caption, note) or None,
        intent=c.intent,
        series=series,
        split_at=split_at[:32] if split_at else None,
        x_kind=x_kind,
    )


def _history(fc: ForecastView) -> list[SeriesPoint]:
    """The fitted history of a forecast view (observed values only)."""
    observed = fc.fan.observed or []
    out: list[SeriesPoint] = []
    for j in range(min(fc.history, len(fc.fan.coords), len(observed))):
        out.append(SeriesPoint(t=_label(fc.fan.coords[j])[:32], actual=_num(observed[j])))
    return out


def _fan_block(c: _Chart, fan: FanChart, split_at: str | None, name: str) -> SeriesBlock:
    pts: list[SeriesPoint] = []
    observed = fan.observed or []
    for j, coord in enumerate(fan.coords):
        got = _fan_at(fan, _label(coord))
        centre, lo, hi = got if got is not None else (None, None, None)
        pts.append(
            SeriesPoint(
                t=_label(coord)[:32],
                mean=centre,
                low=lo if c.band != "none" else None,
                high=hi if c.band != "none" else None,
                actual=_num(observed[j]) if j < len(observed) else None,
            )
        )
    return SeriesBlock(
        panel_id=c.id,
        title=c.title,
        caption=c.caption,
        intent=c.intent,
        series=[
            Series(name=name[:128], label=c.title[:80], points=_points(pts, MAX_SERIES_POINTS))
        ],
        split_at=split_at[:32] if split_at else None,
        x_kind="time",
    )


def _history_block(c: _Chart, mat: Material) -> SeriesBlock:
    assert isinstance(c.source, AccuracySource)
    metric = c.source.metric
    pts = [
        SeriesPoint(
            t=(p.period[1].isoformat() if p.period else p.created_at.date().isoformat())[:32],
            mean=p.values.get(metric),
        )
        for p in mat.history
        if p.values.get(metric) is not None
    ]
    if not pts:
        raise _Unavailable(NO_ACTUALS)
    return SeriesBlock(
        panel_id=c.id,
        title=c.title,
        caption=c.caption,
        intent="accuracy_history",
        series=[Series(name=metric, label=c.title[:80], points=_points(pts, MAX_SERIES_POINTS))],
        x_kind="time",
    )


def _bars_block(c: _Chart, spec: DashboardSpecV2, mat: Material) -> BarsBlock:
    assert isinstance(c.source, OutputSource)
    table = _need_table(mat, c.source.variable)
    cells = _only_sent(table, _select(table.cells, c.source.select), mat)
    if not cells:
        raise _Unavailable("No value matches the chosen coordinates.")
    keep_band = c.band != "none"
    caption = c.caption
    rows: list[BarRow] = []
    if c.intent == "probability_bars" and table.class_dim is not None:
        cdim = table.class_dim
        keep = [d for d in table.dims if d != cdim]
        first = tuple(cells[0].coords.get(d, "") for d in keep)
        case = [c2 for c2 in cells if tuple(c2.coords.get(d, "") for d in keep) == first]
        if len(case) < len(cells):
            caption = _join(caption, f"For {_coords_text(case[0].coords, keep)}")
        classes = [cell.coords.get(cdim, "") for cell in case]
        for cell in case:
            word = class_word(cell.coords.get(cdim, ""), classes)
            rows.append(BarRow(label=word[:64], value=cell.centre,
                               low=cell.low if keep_band else None,
                               high=cell.high if keep_band else None))  # fmt: skip
    else:
        varying = _varying(table, cells) or table.dims
        for cell in cells:
            label = " · ".join(cell.coords.get(d, "") for d in varying) or table.variable
            rows.append(BarRow(label=label[:64], value=cell.centre,
                               low=cell.low if keep_band else None,
                               high=cell.high if keep_band else None))  # fmt: skip
    if c.intent == "area_bars":
        rows.sort(key=lambda r: -(r.value if r.value is not None else -math.inf))
    if len(rows) > c.max_items:
        what = "highest" if c.intent == "area_bars" else "first"
        caption = _join(caption, f"The {what} {c.max_items} of {len(rows)}.")
        rows = rows[: c.max_items]
    note = _band_caption(c.band, mat, spec, table.variable, table.banded and keep_band)
    return BarsBlock(
        panel_id=c.id, title=c.title, caption=_join(caption, note) or None, intent=c.intent,
        rows=rows,
    )  # fmt: skip


def _scatter_block(c: _Chart, mat: Material) -> ScatterBlock:
    if not mat.pairs:
        raise _Unavailable(NO_ACTUALS)
    residuals = c.intent == "residuals"
    pts = [
        ScatterPoint(x=p.predicted, y=(p.actual - p.predicted) if residuals else p.actual)
        for p in mat.pairs[:520]
    ]
    return ScatterBlock(
        panel_id=c.id,
        title=c.title,
        caption=c.caption,
        intent=c.intent,
        x_label="Predicted",
        y_label="Actual minus predicted" if residuals else "Actual",
        points=pts,
    )


def _class_detail(mat: Material) -> ClassAccuracy:
    if not isinstance(mat.accuracy, ClassAccuracy):
        raise _Unavailable(NO_ACTUALS)
    return mat.accuracy


def _chart(c: _Chart, spec: DashboardSpecV2, mat: Material) -> Any:
    if c.intent == "accuracy_history":
        return _history_block(c, mat)
    if c.intent in ("scatter", "residuals"):
        return _scatter_block(c, mat)
    if c.intent == "calibration":
        detail = _class_detail(mat)
        if not detail.bins:
            raise _Unavailable(NO_ACTUALS)
        return CalibrationBlock(panel_id=c.id, title=c.title, caption=c.caption, bins=detail.bins)
    if c.intent == "confusion":
        detail = _class_detail(mat)
        if detail.confusion is None:
            raise _Unavailable(NO_ACTUALS)
        return ConfusionBlock(
            panel_id=c.id, title=c.title, caption=c.caption,
            labels=detail.confusion.labels, counts=detail.confusion.counts,
        )  # fmt: skip
    assert isinstance(c.source, OutputSource)
    if c.intent == "field":
        entry = field_entry(mat.fields, c.source.variable, c.source.select)
        if entry is None:
            raise _Unavailable("The map shows when the model draws it.")
        return field_block(entry, panel_id=c.id, title=c.title, caption=c.caption)
    if c.intent in ("bars", "area_bars", "probability_bars"):
        return _bars_block(c, spec, mat)
    table = _need_table(mat, c.source.variable)
    if c.intent == "range" and table.time_dim is None:
        return _bars_block(c, spec, mat)
    return _series_block(c, spec, mat)


def _of_panel(p: ChartPanel) -> _Chart:
    return _Chart(p.id, p.title, p.caption, p.intent, p.source, p.reduce, p.band, p.facet,
                  p.max_items)  # fmt: skip


def _of_recipe(p: RecipePanel, spec: DashboardSpecV2) -> _Chart:
    intent = RECIPE_INTENTS[p.recipe]
    return _Chart(p.id, p.title, p.caption, intent,
                  OutputSource(variable=p.output or spec.subject.variable), Reduce(), "outcome",
                  None, 20)  # fmt: skip


UPLOADED_PICTURE: Final = "Pictures made with uploaded code are not shown here."


def _picture(p: RecipePanel, mat: Material) -> PictureBlock | UnavailableBlock:
    if uses_uploaded_ids([p.recipe]):
        return _unavailable(p.id, p.title, UPLOADED_PICTURE)
    asset = mat.assets.get(p.plot_id or "")
    if asset is None:
        return _unavailable(p.id, p.title, "The picture is not available.")
    if asset.status == "queued":
        return _unavailable(p.id, p.title, "The picture is being prepared.")
    if asset.status != "ready" or not asset.width or not asset.height:
        return _unavailable(p.id, p.title, (asset.problem or "The picture could not be drawn."))
    return PictureBlock(
        panel_id=p.id, title=p.title, caption=p.caption, asset_id=asset.asset_id,
        alt=(p.caption or p.title)[:300], width=asset.width, height=asset.height,
    )  # fmt: skip


def _table_block(p: TablePanel, spec: DashboardSpecV2, mat: Material) -> TableBlock:
    table = _need_table(mat, p.source.variable)
    cells = _select(table.cells, p.source.select)
    cols = list(p.columns) or [*table.dims, "centre", *(["low", "high"] if table.banded else [])]
    if table.time_dim is not None:
        cells = sorted(cells, key=lambda c: period_key(c.coords.get(table.time_dim or "", "")))
    stat = {"centre": lambda c: c.centre, "low": lambda c: c.low, "high": lambda c: c.high}
    rows: list[list[str | float | None]] = []
    for cell in cells[: p.max_rows]:
        row: list[str | float | None] = []
        for col in cols:
            if col in stat:
                row.append(stat[col](cell))
            else:
                row.append(cell.coords.get(col) or None)
        rows.append(row)
    caption = p.caption
    if len(cells) > p.max_rows:
        caption = _join(caption, f"The first {p.max_rows} of {len(cells)} rows.")
    heads = _column_words(cols[:12], table, spec)
    return TableBlock(
        panel_id=p.id, title=p.title, caption=caption or None, columns=heads,
        rows=[r[:12] for r in rows],
    )  # fmt: skip


_STAT_WORDS: Final[Mapping[str, str]] = {
    "centre": "Expected", "low": "Likely low", "high": "Likely high",
}  # fmt: skip


def _column_words(cols: Sequence[str], table: Table, spec: DashboardSpecV2) -> list[str]:
    """Plain column headers ("Month", "Expected", "Likely low"), never the raw ``t``,
    ``centre``, ``low`` and ``high``; a header that would repeat keeps its raw name."""
    out: list[str] = []
    for col in cols:
        if col in _STAT_WORDS:
            word = _STAT_WORDS[col]
        elif col == table.time_dim:
            word = (spec.period_label or "period").capitalize()
        else:
            word = col.replace("_", " ").strip().capitalize() or col
        out.append(word if word not in out else col)
    return [w[:64] for w in out]


def _unavailable(panel_id: str, title: str, reason: str) -> UnavailableBlock:
    return UnavailableBlock(panel_id=panel_id, title=title, reason=reason[:200])


def _resolve(p: Any, spec: DashboardSpecV2, mat: Material) -> Any:
    if isinstance(p, KpiPanel):
        return _kpi(p, spec, mat)
    if isinstance(p, ChartPanel):
        return _chart(_of_panel(p), spec, mat)
    if isinstance(p, TablePanel):
        return _table_block(p, spec, mat)
    if isinstance(p, NotePanel):
        return NoteBlock(panel_id=p.id, title=p.title, caption=p.caption, text=p.text)
    if isinstance(p, RecipePanel):
        if p.is_picture:
            return _picture(p, mat)
        return _chart(_of_recipe(p, spec), spec, mat)
    return None  # MmmPanel: the planner parts are the legacy tiles, channels and series


def resolve_panels(spec: DashboardSpecV2, material: Material) -> list[Any]:
    """One block per panel, in the spec's order (media planner parts excepted: they read
    the legacy ``tiles``, ``channels`` and ``series``). A panel that cannot be shown, or whose
    block is over its byte budget, becomes an ``unavailable`` block."""
    out: list[Any] = []
    for p in spec.panels:
        if isinstance(p, MmmPanel):
            continue
        try:
            block = _resolve(p, spec, material)
        except _Unavailable as exc:
            block = _unavailable(p.id, p.title, str(exc))
        except (ValueError, ArithmeticError):  # pydantic's ValidationError included
            block = _unavailable(p.id, p.title, "This panel could not be drawn.")
        if block is None:
            continue
        block = _within_budget(block)
        if block is None:
            block = _unavailable(p.id, p.title, "This panel holds too much to show.")
        out.append(block)
    return out[:MAX_PANELS]


def _within_budget(block: Any) -> Any:
    """``block`` within its byte budget: a series or scatter block is thinned (at most four
    halvings of its points); ``None`` when it still does not fit."""
    for _ in range(5):
        size = block_bytes(block)
        budget = block_budget(block)
        if size <= budget:
            return block
        share = max(0.05, min(0.5, budget / size * 0.9))
        if isinstance(block, SeriesBlock) and any(len(s.points) > 8 for s in block.series):
            block = block.model_copy(
                update={
                    "series": [
                        s.model_copy(
                            update={"points": _points(list(s.points), int(len(s.points) * share))}
                        )
                        for s in block.series
                    ]
                }
            )
        elif isinstance(block, ScatterBlock) and len(block.points) > 8:
            keep = max(2, int(len(block.points) * share))
            step = len(block.points) / keep
            block = block.model_copy(
                update={"points": [block.points[int(i * step)] for i in range(keep)]}
            )
        else:
            return None
    return block if block_bytes(block) <= block_budget(block) else None


# =================================================================================== accuracy


def actual_cells(ds: XrJsonDataset, column: str) -> list[tuple[dict[str, str], float | str]]:
    """The values of ``column`` of an upload with their coordinates. A dense variable takes
    its coordinates from its dims; a table (one row dim) also from the other variables on
    that dim (``store``, ``week``, ...). Missing values are left out."""
    var = ds.data_vars.get(column)
    if var is None:
        return []
    dims = list(var.dims)
    labels: dict[str, list[object]] = {}
    for d in dims:
        coord = ds.coords.get(d)
        data = coord.data if coord is not None else None
        labels[d] = list(data) if isinstance(data, list) else []
    side = {
        name: v.data
        for name, v in ds.data_vars.items()
        if name != column and list(v.dims) == dims and len(dims) == 1 and isinstance(v.data, list)
    }
    out: list[tuple[dict[str, str], float | str]] = []

    def walk(node: object, depth: int, index: list[int]) -> None:
        if depth == len(dims):
            if node is None or isinstance(node, list | dict):
                return
            value: float | str | None
            if isinstance(node, bool):
                value = "true" if node else "false"
            elif isinstance(node, int | float):
                value = float(node) if math.isfinite(float(node)) else None
            else:
                value = str(node)
            if value is None:
                return
            coords = {
                d: _label(labels[d][k]) if k < len(labels[d]) else str(k)
                for d, k in zip(dims, index, strict=True)
            }
            if len(dims) == 1:
                for name, data in side.items():
                    if isinstance(data, list) and index[0] < len(data):
                        coords[name] = _label(data[index[0]])
            out.append((coords, value))
            return
        if isinstance(node, list):
            for k, child in enumerate(node):
                walk(child, depth + 1, [*index, k])

    walk(var.data, 0, [])
    return out


def _summary_actuals(
    summary: ScoreSummary | None, variable: str
) -> list[tuple[dict[str, str], float | str]]:
    """The actuals a conditioned forecast echoes next to its predictions."""
    if summary is None:
        return []
    t = next((x for x in summary.actuals if x.variable == variable), None)
    if t is None or "actual" not in t.columns:
        return []
    ai = t.columns.index("actual")
    dims = [c for c in table_dims(t) if c != "actual"] or [c for c in t.columns if c != "actual"]
    out: list[tuple[dict[str, str], float | str]] = []
    for r in t.rows:
        v = r[ai] if ai < len(r) else None
        if v is None or isinstance(v, bool):
            continue
        coords = {d: _label(r[t.columns.index(d)]) for d in dims if t.columns.index(d) < len(r)}
        out.append((coords, float(v) if isinstance(v, int | float) else str(v)))
    return out


def _key(
    coords: Mapping[str, str], dims: Sequence[str], names: Mapping[str, str]
) -> tuple[str, ...]:
    return tuple(normalise_label(coords.get(names.get(d, d), coords.get(d, ""))) for d in dims)


def join_actuals(
    table: Table,
    cells: Sequence[tuple[Mapping[str, str], float | str]],
    names: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[tuple[str, ...], list[Cell]], dict[tuple[str, ...], float | str]]:
    """Join uploaded values to the table on the dims both have. ``names`` maps a table dim
    (dataset name) to the upload's name of it (canonical, e.g. ``week`` → ``time``).
    Returns the key dims, the table cells per key and the actual per key (numbers of one
    key are added up: they are exact)."""
    names = dict(names or {})
    if not cells:
        return [], {}, {}
    upload_keys = set().union(*(set(c) for c, _ in cells))
    cdim = table.class_dim
    key_dims = [
        d for d in table.dims if d != cdim and (names.get(d, d) in upload_keys or d in upload_keys)
    ]
    if not key_dims:
        return [], {}, {}
    actual: dict[tuple[str, ...], float | str] = {}
    for coords, v in cells:
        k = _key(coords, key_dims, names)
        prev = actual.get(k)
        if isinstance(v, float) and isinstance(prev, float):
            actual[k] = prev + v
        else:
            actual[k] = v
    preds: dict[tuple[str, ...], list[Cell]] = {}
    for c in table.cells:
        k = tuple(normalise_label(c.coords.get(d, "")) for d in key_dims)
        if k in actual:
            preds.setdefault(k, []).append(c)
    return key_dims, preds, actual


def sent_rows(
    spec: DashboardSpecV2,
    mat: Material,
    uploaded: Sequence[tuple[Mapping[str, str], float | str]],
    names: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """The table coordinates (on the join dims) of each uploaded row that matched a
    prediction of the subject, in upload order (for :attr:`Material.sent`)."""
    table = table_of(mat.summary, spec.subject.variable)
    if table is None or not uploaded:
        return []
    key_dims, preds, _ = join_actuals(table, uploaded, names)
    out: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for coords, _v in uploaded:
        k = _key(coords, key_dims, dict(names or {}))
        if k in preds and k not in seen:
            seen.add(k)
            first = preds[k][0]
            out.append({d: first.coords.get(d, "") for d in key_dims})
    return out


AccuracyKind = Literal["error", "classification", "effect"]


def accuracy_kind(spec: DashboardSpecV2, mat: Material, table: Table | None) -> AccuracyKind:
    """The family of accuracy of a dashboard: the effect of a causal model that forecasts
    without the change, class accuracy for probabilities, else errors."""
    if spec.preset == "causal_impact" and mat.forecast is not None and mat.forecast.total:
        return "effect"
    if _is_probability(spec, mat, table, spec.subject.variable):
        return "classification"
    return "error"


def accuracy_of(
    spec: DashboardSpecV2,
    mat: Material,
    uploaded: Sequence[tuple[Mapping[str, str], float | str]],
    *,
    names: Mapping[str, str] | None = None,
) -> tuple[ErrorAccuracy | ClassAccuracy | EffectAccuracy | None, list[Pair], list[str]]:
    """The accuracy of the readout's predictions of the subject against ``uploaded`` (or,
    when nothing was uploaded, the actuals a conditioned forecast echoes). Returns the
    detail, the joined pairs (for scatter and residual charts) and plain notes."""
    variable = spec.subject.variable
    table = table_of(mat.summary, variable)
    notes: list[str] = []
    if table is None:
        return None, [], [f"This readout has no values of {variable} to compare."]
    # the upload, else the actuals a conditioned forecast echoes: never both (a conditioned
    # forecast echoes the upload itself, and numbers of one key are added up)
    cells = list(uploaded) or _summary_actuals(mat.summary, variable)
    key_dims, preds, actual = join_actuals(table, cells, names)
    if not preds:
        return None, [], ["None of the results you sent match a prediction of the model."]
    kind = accuracy_kind(spec, mat, table)
    try:
        if kind == "classification":
            return _worded(_class_accuracy(spec, table, preds, actual)), [], notes
        pairs = _pairs(table, key_dims, preds, actual)
        if kind == "effect":
            assert mat.forecast is not None and mat.forecast.total is not None
            total = math.fsum(p.actual for p in pairs)
            detail: Any = effect_of(
                total, mat.forecast.total, n_periods=len(pairs),
                note="Your results minus the forecast without the change.",
            )  # fmt: skip
            return detail, pairs, notes
        bands = [(p.low, p.high) for p in pairs]
        detail = error_metrics_of([(p.predicted, p.actual) for p in pairs], bands)
        return detail, pairs, notes
    except ValueError as exc:
        return None, [], [f"The results could not be compared: {str(exc)[:200]}"]


def _pairs(
    table: Table,
    key_dims: Sequence[str],
    preds: Mapping[tuple[str, ...], Sequence[Cell]],
    actual: Mapping[tuple[str, ...], float | str],
) -> list[Pair]:
    out: list[Pair] = []
    t_i = key_dims.index(table.time_dim) if table.time_dim in key_dims else None
    for k, members in preds.items():
        a = actual.get(k)
        centres = [c.centre for c in members if c.centre is not None]
        if not isinstance(a, float) or not centres:
            continue
        one = len(members) == 1
        out.append(
            Pair(
                label=" · ".join(members[0].coords.get(d, "") for d in key_dims)[:64],
                t=members[0].coords.get(table.time_dim or "") if t_i is not None else None,
                predicted=math.fsum(centres),
                actual=a,
                low=members[0].low if one else None,  # never a sum of bounds
                high=members[0].high if one else None,
                coords={d: members[0].coords.get(d, "") for d in key_dims},
            )
        )
    out.sort(key=lambda p: (period_key(p.t or ""), p.label))
    return out


def _class_accuracy(
    spec: DashboardSpecV2,
    table: Table,
    preds: Mapping[tuple[str, ...], Sequence[Cell]],
    actual: Mapping[tuple[str, ...], float | str],
) -> ClassAccuracy:
    def label_of(v: float | str) -> str:
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)

    cdim = table.class_dim
    threshold = spec.actuals.threshold
    if cdim is None:
        ps: list[float] = []
        ys: list[str] = []
        raw: list[float | str] = []
        for k, members in preds.items():
            c = members[0]
            if c.centre is None or k not in actual:
                continue
            ps.append(c.centre)
            ys.append(label_of(actual[k]))
            raw.append(actual[k])
        positive = spec.subject.positive_label or binary_positive(raw)
        return classification_metrics_of(ps, ys, positive, threshold)
    probs: list[dict[str, float]] = []
    ys = []
    for k, members in preds.items():
        row = {c.coords.get(cdim, ""): c.centre for c in members if c.centre is not None}
        if not row or k not in actual:
            continue
        probs.append({k2: v2 for k2, v2 in row.items() if v2 is not None})
        ys.append(label_of(actual[k]))
    classes = list(dict.fromkeys(cl for row in probs for cl in row))
    pos = spec.subject.positive_label
    if len(classes) == 2 and pos is not None and pos in classes:
        return classification_metrics_of([r.get(pos, 0.0) for r in probs], ys, pos, threshold)
    return multiclass_metrics_of(probs, ys)


_YES_NO: Final[Mapping[str, str]] = {"1": "Yes", "0": "No", "true": "Yes", "false": "No"}


def class_word(code: str, labels: Iterable[str]) -> str:
    """A class as the client reads it: the codes of a yes/no outcome (``0``/``1``,
    ``false``/``true``) as "No"/"Yes"; any other label (the analyst's own words, the
    classes of a multiclass model) as it is."""
    codes = {normalise_label(x) for x in labels}
    if codes and (codes <= {"0", "1"} or codes <= {"false", "true"}):
        return _YES_NO.get(normalise_label(code), code)
    return code


def _worded(detail: ClassAccuracy) -> ClassAccuracy:
    """``detail`` with its class labels in words (:func:`class_word`); the numbers stay."""
    labels = list(detail.confusion.labels) if detail.confusion is not None else []
    known = [*labels, *([detail.positive] if detail.positive else [])]
    update: dict[str, Any] = {}
    if detail.positive:
        update["positive"] = class_word(detail.positive, known)
    if detail.confusion is not None:
        update["confusion"] = detail.confusion.model_copy(
            update={"labels": [class_word(x, known) for x in labels]}
        )
    return detail.model_copy(update=update)


def binary_positive(values: Sequence[float | str]) -> str:
    """The positive class of binary outcomes when the dashboard names none, as the SDK's
    metrics pick it: ``1`` of 0/1 outcomes, the larger of two numbers, ``true`` of
    booleans. Raises ``ValueError`` for two other labels (the analyst sets the positive
    label of the subject)."""

    def text(v: float | str) -> str:
        return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)

    labels = {text(v) for v in values}
    if labels <= {"0", "1"}:
        return "1"
    if {normalise_label(x) for x in labels} <= {"true", "false"}:
        return "true"
    numbers = [v for v in values if isinstance(v, float)]
    if len(numbers) == len(values) and len(set(numbers)) == 2:
        return text(max(numbers))
    shown = ", ".join(sorted(labels)[:3])
    raise ValueError(
        f"the outcomes are {shown}: the dashboard must name which one is the positive class "
        "(the subject's positive label)"
    )


def accuracy_point(
    seq: int,
    created_at: Any,
    period: Any,
    detail: ErrorAccuracy | ClassAccuracy | EffectAccuracy | None,
    legacy: ActualsAccuracy | None = None,
) -> AccuracyPoint | None:
    """One actuals readout as a point of the accuracy history (its layout 2 detail, else the
    layout 1 error metrics); ``None`` when it has neither."""
    values: dict[str, float | None] = {}
    n = 0
    if isinstance(detail, ErrorAccuracy):
        n = detail.n
        for k in ("mae", "rmse", "mape", "wape", "smape", "bias", "r2", "band_hit_rate"):
            values[k] = getattr(detail, k)
    elif isinstance(detail, ClassAccuracy):
        n = detail.n
        for k in ("accuracy", "brier", "log_score", "auc"):
            values[k] = getattr(detail, k)
    elif isinstance(detail, EffectAccuracy):
        n = detail.n_periods
        values["effect_total"] = detail.total
        values["effect_relative"] = detail.relative
    elif legacy is not None:
        n = legacy.n
        for k in ("mae", "rmse", "mape", "wape", "smape", "bias", "r2", "band_hit_rate"):
            values[k] = getattr(legacy, k)
    else:
        return None
    values["n"] = float(n)
    return AccuracyPoint.model_validate(
        {"seq": seq, "created_at": created_at, "period": period, "n": n,
         "values": {k: v for k, v in values.items() if v is not None}}
    )  # fmt: skip


def history_point(
    seq: int, created_at: Any, period: Any, snapshot: ReadoutSnapshot
) -> AccuracyPoint | None:
    """A stored (published) actuals readout as a point of the accuracy history."""
    return accuracy_point(seq, created_at, period, snapshot.accuracy_detail, snapshot.accuracy)


# ============================================================================== prediction/batch


def _row_groups(
    table: Table, row_dim: str | None, labels: Sequence[str]
) -> list[tuple[str, list[Cell]]]:
    """The cells of each scored row: by ``row_dim`` label when given, else by the distinct
    coordinates other than the class dim (in table order)."""
    cdim = table.class_dim
    if row_dim is not None and row_dim in table.dims and labels:
        want = [_label(x) for x in labels]
        return [(lab, [c for c in table.cells if c.coords.get(row_dim) == lab]) for lab in want]
    keep = [d for d in table.dims if d != cdim]
    groups: dict[tuple[str, ...], list[Cell]] = {}
    for c in table.cells:
        groups.setdefault(tuple(c.coords.get(d, "") for d in keep), []).append(c)
    return [(" · ".join(k) or str(i + 1), v) for i, (k, v) in enumerate(groups.items())]


BatchKey = tuple[str, Mapping[str, str]]
"""One row of a scored file: its id and the model coordinates of its cell (the id dim and
each per-row dim column, as text)."""


def keyed_groups(table: Table, keys: Sequence[BatchKey]) -> list[tuple[str, list[Cell]]]:
    """The cells of each file row (in file order): those at the row's own coordinates. A
    file whose rows give other dims (``sku`` per row) is scored on the grid of every id ×
    level, so grouping by the id alone would mix in cells of levels the row never gave.
    Rows without a cell are left out (not scored)."""
    dims = [d for d in (keys[0][1] if keys else {}) if d in table.dims]
    index: dict[tuple[str, ...], list[Cell]] = {}
    for c in table.cells:
        index.setdefault(tuple(c.coords.get(d, "") for d in dims), []).append(c)
    tdim = table.time_dim or ""
    out: list[tuple[str, list[Cell]]] = []
    for label, coords in keys:
        cells = index.get(tuple(coords.get(d, "") for d in dims))
        if cells:
            out.append((label, sorted(cells, key=lambda c: c.coords.get(tdim, ""))))
    return out


def batch_keys(
    upload: XrJsonDataset | None, id_column: str, id_dim: str, dim_columns: Mapping[str, str]
) -> list[BatchKey] | None:
    """The :data:`BatchKey` of each row of a stored file (``None`` without per-row dim
    columns or a readable one-dim file)."""
    if upload is None or not dim_columns:
        return None
    coord = upload.coords.get(id_column)
    ids = coord.data if coord is not None and isinstance(coord.data, list) else None
    if ids is None:
        return None
    cols = {
        c: v.data
        for c in dim_columns
        if (v := upload.data_vars.get(c)) is not None and isinstance(v.data, list)
    }
    if len(cols) != len(dim_columns):
        return None
    keys: list[BatchKey] = []
    for i, raw in enumerate(ids):
        coords = {id_dim: _label(raw)}
        for c, d in dim_columns.items():
            data = cols[c]
            coords[d] = _label(data[i] if i < len(data) else None)
        keys.append((_label(raw), coords))
    return keys


def _value_of(table: Table, cells: Sequence[Cell], band: bool) -> PredictionValue:
    if table.class_dim is not None:
        best = max((c for c in cells if c.centre is not None), key=lambda c: c.centre or 0.0,
                   default=None)  # fmt: skip
        if best is None or best.centre is None:
            return PredictionValue()
        p = min(1.0, max(0.0, best.centre))
        return PredictionValue(label=best.coords.get(table.class_dim, "")[:64], probability=p)
    first = next((c for c in cells if c.centre is not None), None)
    if first is None:
        return PredictionValue()
    return PredictionValue(
        centre=first.centre,
        low=first.low if band else None,
        high=first.high if band else None,
    )


def prediction_result(
    spec: DashboardSpecV2,
    mat: Material,
    rows: Sequence[Mapping[str, Any]] = (),
    *,
    row_dim: str | None = None,
    row_labels: Sequence[str] = (),
) -> PredictionResult:
    """The result of scenario rows: one row per scored case with its inputs (``rows``, the
    values the client sent) and the outputs' centre and range (classifiers: the most likely
    class and its chance). A forecast row shows its first period."""
    outputs = list(spec.scenario_rows.outputs) or [spec.subject.variable]
    per_output: dict[str, list[tuple[str, list[Cell]]]] = {}
    tables: dict[str, Table] = {}
    for name in outputs:
        t = table_of(mat.summary, name)
        if t is not None:
            tables[name] = t
            per_output[name] = _row_groups(t, row_dim, row_labels)
    n = max([len(rows), *(len(g) for g in per_output.values())], default=0)
    out_rows: list[PredictionRow] = []
    for i in range(min(n, 20)):
        values: dict[str, PredictionValue] = {}
        for name, groups in per_output.items():
            if i < len(groups):
                tdim = tables[name].time_dim or ""
                cells = sorted(groups[i][1], key=lambda c: period_key(c.coords.get(tdim, "")))
                if tables[name].time_dim and tables[name].class_dim is None:
                    cells = cells[:1]
                values[name] = _value_of(tables[name], cells, True)
        echo = {k: v for k, v in (rows[i] if i < len(rows) else {}).items()}
        out_rows.append(PredictionRow.model_validate({"inputs": echo, "outputs": values}))
    banded = any(t.banded for t in tables.values())
    return PredictionResult(rows=out_rows, band=mat.band if banded else "none")


def _quantile(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def batch_summary(
    spec: DashboardSpecV2,
    mat: Material,
    *,
    n_rows: int | None = None,
    id_dim: str | None = None,
    keys: Sequence[BatchKey] | None = None,
) -> BatchSummary:
    """The summary of a batch score: per output the spread of the row centres (never an
    interval), the share above the threshold of a binary classifier, the counts of the most
    likely class, a histogram of the first output and a preview of 20 rows. ``keys``: the
    file's rows when they give other dims too (only each row's own cell counts)."""
    outputs = list(spec.batch.outputs) if spec.batch is not None else [spec.subject.variable]
    stats: dict[str, BatchStats] = {}
    share: float | None = None
    counts: dict[str, int] = {}
    histogram: list[HistogramBin] = []
    preview: dict[str, dict[str, Any]] = {}
    scored = 0
    for j, name in enumerate(outputs):
        t = table_of(mat.summary, name)
        if t is None:
            continue
        groups = keyed_groups(t, keys) if keys else _row_groups(t, id_dim, [])
        centres: list[float] = []
        for label, cells in groups:
            v = _value_of(t, cells, False)
            x = v.probability if t.class_dim is not None else v.centre
            if x is None:
                continue
            centres.append(x)
            if t.class_dim is not None and j == 0:
                counts[v.label or ""] = counts.get(v.label or "", 0) + 1
            if label not in preview and len(preview) >= 20:
                continue
            row = preview.setdefault(label, {"id": label})
            if t.class_dim is not None:
                row[name] = v.label
                row[f"{name}_probability"] = x
            else:
                row[name] = x
        if not centres:
            continue
        scored = max(scored, len(centres))
        stats[name] = BatchStats(
            mean=math.fsum(centres) / len(centres), min=min(centres),
            p10=_quantile(centres, 0.1), p50=_quantile(centres, 0.5),
            p90=_quantile(centres, 0.9), max=max(centres),
        )  # fmt: skip
        if j == 0:
            if t.class_dim is None and _is_probability(spec, mat, t, name):
                thr = spec.actuals.threshold
                share = sum(1 for x in centres if x >= thr) / len(centres)
            lo, hi = min(centres), max(centres)
            nb = min(20, max(1, len(centres)))
            width = (hi - lo) / nb if hi > lo else 1.0
            bins = [0] * nb
            for x in centres:
                bins[min(nb - 1, int((x - lo) / width)) if hi > lo else 0] += 1
            histogram = [
                HistogramBin(lo=lo + k * width, hi=lo + (k + 1) * width, count=bins[k])
                for k in range(nb)
            ]
    total = n_rows if n_rows is not None else scored
    first = list(preview.values())[:20]
    cols = 1 + sum(2 if k.endswith("_probability") else 1 for k in (first[0] if first else {}))
    return BatchSummary(
        n_rows=total,
        n_scored=scored,
        outputs=outputs[:4],
        stats=stats,
        share_positive=share,
        class_counts=[BatchClassCount(label=k[:64], count=v) for k, v in counts.items()][:20],
        histogram=histogram,
        preview=first,
        download=BatchDownload(
            available=scored > 0, rows=scored, bytes_estimate=scored * cols * 14
        ),
    )


# ======================================================================================= leads


def fmt(v: float | None, decimals: int = 0) -> str:
    """A number for plain text: thousands separated, U+2212 for a minus sign."""
    if v is None:
        return "no value"
    text = f"{abs(v):,.{decimals}f}"
    return (_MINUS + text) if v < 0 and text.strip("0.,") else text


def _decimals(v: float) -> int:
    return 0 if float(v).is_integer() else 2


def _pct(p: float | None) -> str:
    return fmt(p * 100 if p is not None else None, 0)


def _noun(spec: DashboardSpecV2) -> str:
    """The subject as a noun in a sentence; empty for a default label that names nothing
    ("Prediction", "Result": never "176 prediction next period")."""
    label = spec.subject.label
    if label.strip().lower() in GENERIC_NOUNS:
        return ""
    return label.lower() if label[1:] == label[1:].lower() else label


def _first_kpi(
    spec: DashboardSpecV2, blocks: Sequence[Any], want: Callable[[KpiPanel], bool]
) -> KpiValue | None:
    ok = {p.id for p in spec.panels if isinstance(p, KpiPanel) and want(p)}
    for b in blocks:
        if isinstance(b, KpiBlock) and b.panel_id in ok and b.tile.value is not None:
            return b.tile
    return None


def _subject_kpi(p: KpiPanel) -> bool:
    return isinstance(p.source, OutputSource) and p.stat == "centre"


def lead_for(
    preset: PresetKey,
    spec: DashboardSpecV2,
    material: Material,
    blocks: Sequence[Any],
) -> str:
    """The lead sentence of a readout (plain text from numbers only, at most 400 characters;
    the templates of :data:`mapi_spec.dashboard_presets.LEADS`)."""
    t = LEADS.get(preset, {})
    d = spec.subject.decimals
    noun = _noun(spec)
    period = spec.period_label
    acc = material.accuracy
    head = _first_kpi(spec, blocks, _subject_kpi)
    lead = ""
    if preset == "forecast" and head is not None:
        key = "next" if head.low is not None and head.high is not None else "next_point"
        lead = t[key].format(centre=fmt(head.value, d), noun=noun, period=period,
                             low=fmt(head.low, d), high=fmt(head.high, d))  # fmt: skip
        if isinstance(acc, ErrorAccuracy) and acc.mape is not None:
            lead += (
                f" On the {acc.n} results you sent, the error was {fmt(acc.mape, 1)}% on average."
            )
    elif preset == "classifier":
        top = _first_kpi(spec, blocks, lambda p: p.reduce.how == "top_class")
        if isinstance(acc, ClassAccuracy) and acc.accuracy is not None:
            lead = t["accuracy"].format(n=acc.n, acc=_pct(acc.accuracy))
        elif head is not None and head.unit == "probability":
            pos = spec.subject.positive_label
            coded = pos is None or normalise_label(pos) in _YES_NO  # a code, not a word
            positive = "a positive result" if coded or pos is None else pos
            lead = t["chance"].format(p=_pct(head.value), positive_label=positive)
        elif top is not None:
            label = top.caption.removeprefix("Most likely: ").split(" (", 1)[0]
            lead = t["top"].format(label=label, p=_pct(top.value))
    elif preset == "causal_impact":
        lead = _causal_lead(spec, material, t, noun)
    elif preset == "regression" and head is not None:
        key = "case" if head.low is not None and head.high is not None else "case_point"
        lead = t[key].format(centre=fmt(head.value, d), noun=noun, low=fmt(head.low, d),
                             high=fmt(head.high, d))  # fmt: skip
    elif preset == "field_map":
        share = _first_kpi(spec, blocks, lambda p: p.reduce.how == "share_above")
        lead = t["map"].format(noun=noun)
        if share is not None and share.caption:
            lead += " " + share.caption.split(".", 1)[0] + "."
    elif preset == "image":
        value = _metric(material, "accuracy", {})
        if isinstance(acc, ClassAccuracy) and acc.accuracy is not None:
            value = acc.accuracy
        if value is not None:
            lead = t["accuracy"].format(acc=_pct(value))
    elif preset == "generic" and head is not None:
        key = "value" if head.low is not None and head.high is not None else "value_point"
        lead = t[key].format(centre=fmt(head.value, d), noun=noun, low=fmt(head.low, d),
                             high=fmt(head.high, d))  # fmt: skip
    if not lead:
        about = f" for {noun}" if noun else ""
        lead = (
            f"This readout shows the latest results of the model{about}."
            if blocks
            else f"This readout has no results{about} yet."
        )
    return " ".join(lead.split())[:400]  # an empty noun leaves no double space


def _causal_lead(spec: DashboardSpecV2, mat: Material, t: Mapping[str, str], noun: str) -> str:
    d = spec.subject.decimals
    acc = mat.accuracy
    total = low = high = rel = None
    if isinstance(acc, EffectAccuracy) and acc.total is not None:
        total, low, high, rel = acc.total, acc.low, acc.high, acc.relative
    else:
        for key in ("effect_total", "att"):
            total = _metric(mat, key, {})
            if total is not None:
                low, high = _metric(mat, f"{key}_p05", {}), _metric(mat, f"{key}_p95", {})
                r = _metric(mat, "effect_relative", {})
                rel = r * 100 if r is not None else None
                break
    if total is None:
        return ""
    if low is None or high is None or rel is None:
        return t["effect_short"].format(total=fmt(total, d), noun=noun)
    text = t["effect"].format(total=fmt(total, d), noun=noun, low=fmt(low, d),
                              high=fmt(high, d), rel=fmt(abs(rel), 1))  # fmt: skip
    if rel < 0:
        text = text.replace("% more than", "% less than")
    return text


# ===================================================================================== snapshot


def _legacy_tiles(blocks: Sequence[Any]) -> list[KpiValue]:
    return [b.tile for b in blocks if isinstance(b, KpiBlock)][:_MAX_LEGACY_TILES]


def _legacy_accuracy(acc: Any) -> ActualsAccuracy | None:
    if not isinstance(acc, ErrorAccuracy):
        return None
    return ActualsAccuracy(
        mape=acc.mape, bias=acc.bias, band_hit_rate=acc.band_hit_rate, n=acc.n, mae=acc.mae,
        rmse=acc.rmse, wape=acc.wape, smape=acc.smape, r2=acc.r2,
    )  # fmt: skip


def _primary_series(spec: DashboardSpecV2, blocks: Sequence[Any]) -> list[Series]:
    """The legacy ``series``: the first single-line series block of the subject output."""
    want = {
        p.id
        for p in spec.panels
        if isinstance(p, ChartPanel)
        and isinstance(p.source, OutputSource)
        and p.source.variable == spec.subject.variable
    }
    for b in blocks:
        if isinstance(b, SeriesBlock) and b.panel_id in want and len(b.series) == 1:
            s = b.series[0]
            return [Series(name=spec.subject.variable, label=spec.subject.label, points=s.points)]
    return []


def _series_points(series: Sequence[Series], blocks: Sequence[Any]) -> int:
    n = sum(len(s.points) for s in series)
    for b in blocks:
        if isinstance(b, SeriesBlock):
            n += sum(len(s.points) for s in b.series)
        elif isinstance(b, ScatterBlock):
            n += len(b.points)
    return n


def _fit_points(
    series: list[Series], blocks: list[Any], budget: int
) -> tuple[list[Series], list[Any]]:
    """Thin every series (legacy and blocks) and scatter so they hold at most ``budget``
    points together (lttb keeps the shape)."""
    total = _series_points(series, blocks)
    if total <= budget:
        return series, blocks
    factor = budget / total

    def thin(s: Series) -> Series:
        return s.model_copy(update={"points": _points(list(s.points), int(len(s.points) * factor))})

    series = [thin(s) for s in series]
    out: list[Any] = []
    for b in blocks:
        if isinstance(b, SeriesBlock):
            b = b.model_copy(update={"series": [thin(s) for s in b.series]})
        elif isinstance(b, ScatterBlock):
            b = b.model_copy(update={"points": b.points[: max(2, int(len(b.points) * factor))]})
        out.append(b)
    return series, out


def build_snapshot_v2(
    spec: DashboardSpecV2,
    material: Material,
    *,
    legacy: ReadoutSnapshot | None = None,
    blocks: Sequence[Any] | None = None,
    prediction: PredictionResult | None = None,
    batch: BatchSummary | None = None,
    inputs_echo: Sequence[Mapping[str, Any]] = (),
    source_run_ids: Sequence[str] = (),
    input_fingerprint: str | None = None,
    technical: Mapping[str, str | float] | None = None,
    notes: Sequence[str] = (),
) -> ReadoutSnapshot:
    """The ``schema_version`` 2 snapshot. ``legacy``: the layout 1 snapshot of the media
    planner (its tiles, channels, series, lead and accuracy are kept as they are, so the
    numbers of an upgraded planner equal those of layout 1). Raises ``ValueError`` when the
    snapshot is over the layout 2 caps after the series are thinned."""
    blocks = list(blocks) if blocks is not None else resolve_panels(spec, material)
    series = list(legacy.series) if legacy is not None else _primary_series(spec, blocks)
    series, blocks = _fit_points(series, blocks, MAX_SERIES_POINTS_V2)
    history = list(material.history)[-104:]
    all_notes = [*(legacy.notes_plain if legacy is not None else []), *notes]
    payload: dict[str, Any] = {
        "lead": legacy.lead
        if legacy is not None
        else lead_for(spec.preset, spec, material, blocks),
        "tiles": list(legacy.tiles) if legacy is not None else _legacy_tiles(blocks),
        "channels": list(legacy.channels) if legacy is not None else [],
        "series": series,
        "accuracy": (
            legacy.accuracy if legacy is not None else _legacy_accuracy(material.accuracy)
        ),
        "conditioning": material.summary.conditioning if material.summary is not None else None,
        "scenario": legacy.scenario if legacy is not None else None,
        "source_run_ids": list(source_run_ids)[:16],
        "input_fingerprint": input_fingerprint,
        "notes_plain": [n[:400] for n in all_notes][:20],
        "technical": dict(technical or {}),
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "preset": spec.preset,
        "panels": blocks,
        "prediction": prediction,
        "batch": batch,
        "accuracy_detail": material.accuracy,
        "accuracy_history": history,
        "inputs_echo": [dict(r) for r in inputs_echo][:20],
    }
    try:
        return ReadoutSnapshot.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(str(exc)[:2000]) from None
