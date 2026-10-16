"""Precomputed UI views: small JSON documents the runner writes next to datasets and bundles
and the browser reads through signed URLs (the control plane never decodes them).

Every view carries a ``format`` tag so readers can refuse versions they do not understand:

=====================  ========================  ==========================================
file                   ``format``                content
=====================  ========================  ==========================================
``preview.json``       ``mapi.preview/v1``       a long-format sample: dim then var columns
``profile.json``       ``mapi.profile/v1``       per-variable statistics + missingness grid
``ui/tree_index.json`` ``mapi.tree-index/v1``    structure of the bundle DataTree (no arrays)
``ui/preprocess.json`` ``mapi.preprocess/v1``    learned preprocessing state per pipeline step
``ui/grids/            ``mapi.response-grid/v1`` per-draw response curves for what-if sliders
response_curves.json``                           (M3; :class:`ResponseGridView`)
``ui/guided/           ``mapi.forecast/v1``      a forecast score run's KPI: history, then the
forecast.json``                                  forecast fan after the split (M3)
``ui/guided/           ``mapi.counterfactual/v1`` a counterfactual score run's change of the
counterfactual.json``                            KPI, in total and per period (M3)
=====================  ========================  ==========================================

Views are produced inside the sandbox from untrusted data: strings are capped and readers
must render them as text only.
"""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mapi_spec._types import JsonValue
from mapi_spec.calibration import DistributionSummary, FanChart
from mapi_spec.splitplan import Fold
from mapi_spec.stattests import StatTestResult

__all__ = [
    "COUNTERFACTUAL_FORMAT",
    "COUNTERFACTUAL_VIEW",
    "DATA_SECTIONS",
    "EXPLORER_INDEX_FORMAT",
    "EXPLORE_INDEX",
    "EXPLORE_PREFIX",
    "FORECAST_FORMAT",
    "FORECAST_VIEW",
    "MAX_EXPLORE_LAGS",
    "MAX_EXPLORE_POINTS",
    "MAX_EXPLORE_SERIES",
    "MAX_EXPLORE_VARS",
    "MAX_GRID_CELLS",
    "MAX_GRID_DRAWS",
    "MAX_GRID_GEOS",
    "MAX_GRID_POINTS",
    "MAX_LEARNED_BYTES",
    "MAX_STATE_ARRAYS",
    "MAX_STATE_ARRAY_CELLS",
    "MODEL_SECTIONS",
    "PREPROCESS_FORMAT",
    "PREVIEW_FORMAT",
    "PROFILE_FORMAT",
    "RESPONSE_GRID_FORMAT",
    "RESPONSE_GRID_VIEW",
    "TREE_INDEX_FORMAT",
    "AcfPacfView",
    "BacktestFoldResult",
    "BacktestView",
    "ComparisonView",
    "CorrelationView",
    "CounterfactualView",
    "CrossCorrelationView",
    "DataOverviewView",
    "DataSection",
    "DecompositionView",
    "DimOverview",
    "DistributionsView",
    "ExplorerIndex",
    "ExplorerSectionRef",
    "FevdView",
    "FitVsActualView",
    "ForecastView",
    "GroupComparison",
    "GroupComparisonView",
    "GroupStats",
    "Histogram",
    "IrfView",
    "LagProfile",
    "MissingnessGrid",
    "MissingnessView",
    "ModelDiagnosticsView",
    "ModelForecastView",
    "ModelSection",
    "OutlierPoint",
    "OutliersView",
    "ParamRow",
    "ParamTableView",
    "PreprocessView",
    "PreviewColumn",
    "PreviewView",
    "ProfileView",
    "QQPoints",
    "QualityView",
    "QualityWarning",
    "ResidualDiagnosticsView",
    "ResponseCurvesView",
    "ResponseGridView",
    "SeriesView",
    "StateArrayView",
    "StationarityRow",
    "StationarityView",
    "StepStateView",
    "StlComponents",
    "StlView",
    "TimeSeriesView",
    "TreeCoord",
    "TreeGroup",
    "TreeIndexView",
    "TreeVar",
    "VarDistribution",
    "VarOutliers",
    "VarOverview",
    "VarProfile",
    "ViewScalar",
    "VifView",
    "explore_view_name",
]

PREVIEW_FORMAT: Final = "mapi.preview/v1"
PROFILE_FORMAT: Final = "mapi.profile/v1"
TREE_INDEX_FORMAT: Final = "mapi.tree-index/v1"
PREPROCESS_FORMAT: Final = "mapi.preprocess/v1"
RESPONSE_GRID_FORMAT: Final = "mapi.response-grid/v1"
RESPONSE_GRID_VIEW: Final = "ui/grids/response_curves.json"
"""Where a fit writes its :class:`ResponseGridView` (served by ``…/artifacts/{name}``)."""
FORECAST_FORMAT: Final = "mapi.forecast/v1"
FORECAST_VIEW: Final = "ui/guided/forecast.json"
"""Where a ``forecast`` score run writes its :class:`ForecastView` (M3)."""
COUNTERFACTUAL_FORMAT: Final = "mapi.counterfactual/v1"
COUNTERFACTUAL_VIEW: Final = "ui/guided/counterfactual.json"
"""Where a ``counterfactual`` score run writes its :class:`CounterfactualView` (M3)."""
MAX_GRID_DRAWS: Final = 500
MAX_GRID_CELLS: Final = 2_000_000
"""Cap on the number of floats in one response grid (draws x channels x points [x geos])."""
MAX_GRID_GEOS: Final = 256
"""Cap on a response grid's per-geo axis (``geos``); a model with more geos gets the national
grid only."""
MAX_GRID_POINTS: Final = 202
"""Cap on a response grid's multipliers: up to 201 evenly spaced points plus 1 (today's spend)
when the spacing skips it."""

ViewScalar = str | int | float | bool | None
"""A JSON scalar (datetimes are ISO strings; NaN is ``null``)."""

MAX_PREVIEW_ROWS: Final = 1000
MAX_GRID_COORDS: Final = 1000
MAX_TREE_COORD_VALUES: Final = 1000
MAX_STATE_ARRAY_CELLS: Final = 10_000
"""State arrays larger than this are listed in ``omitted`` instead of being inlined."""
MAX_STATE_ARRAYS: Final = 64
MAX_LEARNED_BYTES: Final = 16 * 1024
"""Serialised size cap of one step's ``learned`` JSON in ``ui/preprocess.json``."""


class _View(BaseModel):
    """Views tolerate extra producer fields (readers ignore what they do not know)."""

    model_config = ConfigDict(extra="allow", frozen=True)


class PreviewColumn(_View):
    name: str = Field(..., max_length=256)
    dtype: str = Field(..., max_length=64)
    role: Literal["dim", "var"]


class PreviewView(_View):
    """``preview.json``: the first rows of the dataset in long format."""

    format: Literal["mapi.preview/v1"] = PREVIEW_FORMAT
    columns: list[PreviewColumn] = Field(..., max_length=1024)
    rows: list[list[ViewScalar]] = Field(..., max_length=MAX_PREVIEW_ROWS)
    total_rows: int | None = Field(None, ge=0)


class VarProfile(_View):
    dtype: str = Field(..., max_length=64)
    dims: list[str] = Field(default_factory=list, max_length=32)
    size: int = Field(0, ge=0)
    count: int = Field(0, ge=0, description="non-missing values")
    missing: int = Field(0, ge=0)
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    std: float | None = None


class MissingnessGrid(_View):
    """Fraction missing per (variable, coordinate of the time-like dim ``along``)."""

    along: str
    labels: list[str] = Field(..., max_length=1024)
    coords: list[str] = Field(..., max_length=MAX_GRID_COORDS)
    fraction: list[list[float]] = Field(..., max_length=1024)


class ProfileView(_View):
    """``profile.json``: per-variable statistics and a missingness heatmap."""

    format: Literal["mapi.profile/v1"] = PROFILE_FORMAT
    vars: dict[str, VarProfile] = Field(default_factory=dict, max_length=1024)
    missingness: MissingnessGrid | None = None


class TreeCoord(_View):
    dims: list[str] = Field(default_factory=list)
    dtype: str
    size: int = Field(..., ge=0)
    values: list[ViewScalar] | None = Field(None, max_length=MAX_TREE_COORD_VALUES)


class TreeVar(_View):
    dims: list[str] = Field(default_factory=list)
    shape: list[int] = Field(default_factory=list)
    dtype: str
    units: str | None = None
    description: str | None = None


class TreeGroup(_View):
    dims: dict[str, int] = Field(default_factory=dict)
    coords: dict[str, TreeCoord] = Field(default_factory=dict)
    vars: dict[str, TreeVar] = Field(default_factory=dict)
    attrs: dict[str, ViewScalar] = Field(default_factory=dict)


class TreeIndexView(_View):
    """``ui/tree_index.json``: one entry per DataTree node (keyed by its path)."""

    format: Literal["mapi.tree-index/v1"] = TREE_INDEX_FORMAT
    groups: dict[str, TreeGroup] = Field(default_factory=dict, max_length=4096)


class StateArrayView(_View):
    """One learned array (at most 2-D, numeric) of a step state, e.g. ``scale(geo, channel)``.

    ``values`` is flat for 0-D (one element) and 1-D arrays and row-major (a list of rows
    along ``dims[0]``) for 2-D arrays. ``coords`` holds the labels of the dims that have them.
    """

    name: str = Field(..., max_length=256)
    dims: list[str] = Field(default_factory=list, max_length=2)
    coords: dict[str, list[ViewScalar]] = Field(default_factory=dict)
    values: list[float | None] | list[list[float | None]] = Field(
        ..., max_length=MAX_STATE_ARRAY_CELLS
    )
    units: str | None = Field(None, max_length=256)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        nested = any(isinstance(v, list) for v in self.values)
        if len(self.dims) == 2:
            if not all(isinstance(v, list) for v in self.values):
                raise ValueError("a 2-D state array needs a list of rows")
        elif nested:
            raise ValueError("a 0-D or 1-D state array needs a flat list of values")
        for d, labels in self.coords.items():
            if d not in self.dims:
                raise ValueError(f"coords name {d!r}, which is not one of the dims")
            if len(labels) > MAX_TREE_COORD_VALUES:
                raise ValueError(f"too many labels for {d!r}")
        return self


class StepStateView(_View):
    """One pipeline step: what it is and what it learned at fit."""

    id: str = Field(..., max_length=256)
    use: str = Field(..., max_length=512, description="the resolved step ref (id@version)")
    kind: str = Field("", max_length=32)
    stateful: bool = False
    learned: dict[str, JsonValue] = Field(default_factory=dict)
    learned_truncated: bool = Field(
        False, description="some learned keys were dropped to keep the view small"
    )
    arrays: list[StateArrayView] = Field(default_factory=list, max_length=MAX_STATE_ARRAYS)
    omitted: list[str] = Field(
        default_factory=list,
        max_length=1024,
        description="state arrays not inlined (more than 2-D, non-numeric or too large)",
    )


class PreprocessView(_View):
    """``ui/preprocess.json``: the learned preprocessing state, in pipeline order."""

    format: Literal["mapi.preprocess/v1"] = PREPROCESS_FORMAT
    steps: list[StepStateView] = Field(default_factory=list, max_length=1024)


class ResponseGridView(_View):
    """``ui/grids/response_curves.json``: per-draw response curves for instant what-if sliders.

    For each thinned posterior draw ``d``, channel ``c`` and spend multiplier ``m``,
    ``contribution[d][c][m]`` is the channel's total contribution to the KPI over the fit
    window (``periods`` periods, KPI units) when the channel's spend is scaled by
    ``multipliers[m]``; ``spend[c][m]`` is that spend (spend units). Multiplier ``1`` is the
    actual spend (``current_spend``). ``baseline[d]`` is the KPI over the window that no
    channel explains, so ``baseline[d] + sum_c contribution[d][c][at 1]`` is the model's
    expected KPI. ``*_by_geo`` variants add a trailing geo axis (``geos`` order).

    Sliders interpolate per draw (see :func:`mapi_spec.whatif.evaluate_scenario`), so
    uncertainty bands stay joint across channels; "Run exact" submits a real score run.
    """

    format: Literal["mapi.response-grid/v1"] = RESPONSE_GRID_FORMAT
    kpi: str = Field(..., max_length=128)
    kpi_units: str | None = Field(None, max_length=64)
    spend_units: str | None = Field(None, max_length=64)
    periods: int = Field(..., ge=1)
    channels: list[str] = Field(..., min_length=1, max_length=64)
    geos: list[str] | None = Field(None, max_length=MAX_GRID_GEOS)
    multipliers: list[float] = Field(..., min_length=2, max_length=MAX_GRID_POINTS)
    draws: int = Field(..., ge=1, le=MAX_GRID_DRAWS)
    contribution: list[list[list[float]]]
    spend: list[list[float]]
    current_spend: list[float]
    baseline: list[float] | None = None
    contribution_by_geo: list[list[list[list[float]]]] | None = None
    spend_by_geo: list[list[list[float]]] | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        m = self.multipliers
        if m != sorted(set(m)) or m[0] < 0:
            raise ValueError("multipliers must be unique, ascending and non-negative")
        if not any(abs(x - 1.0) < 1e-9 for x in m):
            raise ValueError("multipliers must include 1 (the actual spend)")
        nc, nm, nd = len(self.channels), len(m), self.draws
        if len(set(self.channels)) != nc:
            raise ValueError("channels must be unique")
        cells = nd * nc * nm * (len(self.geos) if self.geos else 1)
        if cells > MAX_GRID_CELLS:
            raise ValueError(f"the grid holds {cells} values (max {MAX_GRID_CELLS})")
        if len(self.contribution) != nd or any(
            len(per_c) != nc or any(len(row) != nm for row in per_c) for per_c in self.contribution
        ):
            raise ValueError("contribution must be [draw][channel][multiplier]")
        if len(self.spend) != nc or any(len(row) != nm for row in self.spend):
            raise ValueError("spend must be [channel][multiplier]")
        if len(self.current_spend) != nc:
            raise ValueError("current_spend needs one value per channel")
        if self.baseline is not None and len(self.baseline) != nd:
            raise ValueError("baseline needs one value per draw")
        if (
            self.contribution_by_geo is not None or self.spend_by_geo is not None
        ) and not self.geos:
            raise ValueError("per-geo grids need geos")
        if self.geos:
            ng = len(self.geos)
            if len(set(self.geos)) != ng:
                raise ValueError("geos must be unique")
            by = self.contribution_by_geo
            if by is not None and (
                len(by) != nd
                or any(
                    len(per_c) != nc
                    or any(len(row) != nm or any(len(cell) != ng for cell in row) for row in per_c)
                    for per_c in by
                )
            ):
                raise ValueError("contribution_by_geo must be [draw][channel][multiplier][geo]")
            sg = self.spend_by_geo
            if sg is not None and (
                len(sg) != ng
                or any(len(per_c) != nc or any(len(row) != nm for row in per_c) for per_c in sg)
            ):
                raise ValueError("spend_by_geo must be [geo][channel][multiplier]")
        return self


class ForecastView(_View):
    """``ui/guided/forecast.json``: the KPI of a ``forecast`` score run for Guided mode.

    ``fan.coords`` are the last ``history`` fitted periods followed by the forecast periods;
    ``fan.observed`` holds the actual KPI over the history (``null`` after it) and
    ``fan.values`` the forecast quantiles (``null`` over the history), so the split line sits
    at ``split_at``, the first forecast period. The KPI is summed over ``summed_over``
    (geos, stores, ...) per posterior draw before the quantiles are taken; ``total`` is the
    KPI summed over the whole horizon.
    """

    format: Literal["mapi.forecast/v1"] = FORECAST_FORMAT
    kpi: str = Field(..., max_length=128)
    units: str | None = Field(None, max_length=64)
    kind: Literal["expected", "predictive"] = "expected"
    dim: str = Field(..., max_length=128)
    history: int = Field(0, ge=0)
    split_at: ViewScalar = None
    summed_over: list[str] = Field(default_factory=list, max_length=32)
    fan: FanChart
    total: DistributionSummary | None = None

    @model_validator(mode="after")
    def _split(self) -> Self:
        if self.history > len(self.fan.coords):
            raise ValueError("history cannot exceed the number of coords")
        return self


class CounterfactualView(_View):
    """``ui/guided/counterfactual.json``: how a counterfactual changed the KPI.

    ``delta_total`` is ``scenario - baseline`` summed over every period and ``summed_over``
    dims per posterior draw (common random numbers keep the difference precise);
    ``delta_fan`` gives the change per period along ``dim``. ``interventions`` echo the
    request's (``var``, ``sel``, ``multiply``/``add``/``set``).
    """

    format: Literal["mapi.counterfactual/v1"] = COUNTERFACTUAL_FORMAT
    kpi: str = Field(..., max_length=128)
    units: str | None = Field(None, max_length=64)
    kind: Literal["expected", "predictive"] = "expected"
    dim: str | None = Field(None, max_length=128)
    summed_over: list[str] = Field(default_factory=list, max_length=32)
    interventions: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=64)
    scenario_total: DistributionSummary
    baseline_total: DistributionSummary
    delta_total: DistributionSummary
    delta_share: DistributionSummary | None = Field(
        None, description="delta_total / baseline_total per draw (None if the baseline is 0)"
    )
    delta_fan: FanChart | None = None


# ================================================================================ explorers
#
# M3.5: the Data Explorer and the Model Explorer. An ``explore`` run computes the sections a
# request asks for in the sandbox and writes one view per section under ``ui/explore/``
# plus ``ui/explore/index.json`` (:class:`ExplorerIndex`); the browser fetches them through
# signed links. Every view is capped (series, points, cells) so it stays small.

EXPLORE_PREFIX: Final = "ui/explore/"
EXPLORE_INDEX: Final = "ui/explore/index.json"
EXPLORER_INDEX_FORMAT: Final = "mapi.explorer-index/v1"
MAX_EXPLORE_SERIES: Final = 200
"""Series per time-series facet view (the rest are listed as omitted)."""
MAX_EXPLORE_POINTS: Final = 5000
"""Points per series (longer series are downsampled, flagged with ``downsampled``)."""
MAX_EXPLORE_VARS: Final = 256
MAX_EXPLORE_LAGS: Final = 200

DataSection = Literal[
    "overview", "distributions", "timeseries", "missingness", "outliers", "correlation",
    "ccf", "acf_pacf", "stationarity", "stl", "vif", "groups", "quality",
]  # fmt: skip
"""Sections of the Data Explorer (``ui/explore/<section>.json``)."""
ModelSection = Literal[
    "params", "diagnostics", "fit", "residuals", "decomposition", "response_curves", "irf",
    "fevd", "forecast", "backtest", "comparison",
]  # fmt: skip
"""Sections of the Model Explorer (``ui/explore/<section>.json``)."""
DATA_SECTIONS: Final[tuple[str, ...]] = (
    "overview", "distributions", "timeseries", "missingness", "outliers", "correlation",
    "ccf", "acf_pacf", "stationarity", "stl", "vif", "groups", "quality",
)  # fmt: skip
MODEL_SECTIONS: Final[tuple[str, ...]] = (
    "params", "diagnostics", "fit", "residuals", "decomposition", "response_curves", "irf",
    "fevd", "forecast", "backtest", "comparison",
)  # fmt: skip


def explore_view_name(section: str) -> str:
    """``ui/explore/<section>.json``: where an explore run writes a section."""
    if section not in DATA_SECTIONS and section not in MODEL_SECTIONS:
        raise ValueError(f"unknown explorer section {section!r}")
    return f"{EXPLORE_PREFIX}{section}.json"


Coord = str | int | float | bool
Facet = dict[str, Coord]


class _Explore(_View):
    """Base of explorer views: a plain-language caption and warnings."""

    caption_plain: str | None = Field(None, max_length=1000)
    warnings_plain: list[str] = Field(default_factory=list, max_length=50)


# ----------------------------------------------------------------------------- Data Explorer


class DimOverview(_View):
    name: str = Field(..., max_length=256)
    kind: str = Field(..., max_length=32)
    size: int = Field(..., ge=0)
    start: ViewScalar = None
    end: ViewScalar = None
    freq: str | None = Field(None, max_length=32)


class VarOverview(_View):
    name: str = Field(..., max_length=256)
    dtype: str = Field(..., max_length=64)
    dims: list[str] = Field(default_factory=list, max_length=32)
    role: str | None = Field(None, max_length=32)
    units: str | None = Field(None, max_length=64)
    missing_share: float = Field(0.0, ge=0, le=1)
    distinct: int | None = Field(None, ge=0)
    constant: bool = False


class DataOverviewView(_Explore):
    """``overview``: shape, dims, variables and a plain-language summary."""

    format: Literal["mapi.data-explorer.overview/v1"] = "mapi.data-explorer.overview/v1"
    n_cells: int = Field(..., ge=0)
    dims: list[DimOverview] = Field(default_factory=list, max_length=32)
    vars: list[VarOverview] = Field(default_factory=list, max_length=MAX_EXPLORE_VARS)
    summary_plain: str = Field(..., max_length=1000)
    quality_score: float | None = Field(None, ge=0, le=1, description="1 = no issues found")


class Histogram(_View):
    edges: list[float] = Field(..., min_length=2, max_length=202)
    counts: list[int] = Field(..., min_length=1, max_length=201)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if len(self.edges) != len(self.counts) + 1:
            raise ValueError("a histogram has one more edge than counts")
        if self.edges != sorted(self.edges):
            raise ValueError("histogram edges must ascend")
        return self


class VarDistribution(_View):
    var: str = Field(..., max_length=256)
    facet: Facet = Field(default_factory=dict)
    n: int = Field(..., ge=0)
    mean: float | None = None
    sd: float | None = None
    skew: float | None = None
    kurtosis: float | None = None
    zero_share: float | None = Field(None, ge=0, le=1)
    quantiles: dict[str, float] = Field(default_factory=dict, max_length=16)
    histogram: Histogram | None = None


class DistributionsView(_Explore):
    """``distributions``: a histogram and summary statistics per variable."""

    format: Literal["mapi.data-explorer.distributions/v1"] = "mapi.data-explorer.distributions/v1"
    items: list[VarDistribution] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)


class SeriesView(_View):
    """One series along a dim; ``values`` align with ``coords``."""

    var: str = Field(..., max_length=256)
    facet: Facet = Field(default_factory=dict)
    coords: list[ViewScalar] = Field(..., max_length=MAX_EXPLORE_POINTS)
    values: list[float | None] = Field(..., max_length=MAX_EXPLORE_POINTS)
    downsampled: bool = False

    @model_validator(mode="after")
    def _len(self) -> Self:
        if len(self.coords) != len(self.values):
            raise ValueError("coords and values must have the same length")
        return self


class TimeSeriesView(_Explore):
    """``timeseries``: every variable over time, faceted by ``facet_by`` (geo, product)."""

    format: Literal["mapi.data-explorer.timeseries/v1"] = "mapi.data-explorer.timeseries/v1"
    dim: str = Field(..., max_length=128)
    facet_by: list[str] = Field(default_factory=list, max_length=4)
    series: list[SeriesView] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)
    omitted_series: int = Field(0, ge=0)


class MissingnessView(_Explore):
    """``missingness``: the heatmap plus the share missing per variable."""

    format: Literal["mapi.data-explorer.missingness/v1"] = "mapi.data-explorer.missingness/v1"
    grid: MissingnessGrid | None = None
    share_by_var: dict[str, float] = Field(default_factory=dict, max_length=MAX_EXPLORE_VARS)
    longest_gap: dict[str, int] = Field(default_factory=dict, max_length=MAX_EXPLORE_VARS)


class OutlierPoint(_View):
    coords: dict[str, ViewScalar] = Field(default_factory=dict)
    value: float
    score: float


class VarOutliers(_View):
    var: str = Field(..., max_length=256)
    count: int = Field(..., ge=0)
    share: float = Field(..., ge=0, le=1)
    points: list[OutlierPoint] = Field(default_factory=list, max_length=100)


class OutliersView(_Explore):
    """``outliers``: unusual values per variable by a robust rule."""

    format: Literal["mapi.data-explorer.outliers/v1"] = "mapi.data-explorer.outliers/v1"
    method: Literal["iqr", "mad", "zscore"] = "mad"
    threshold: float = Field(..., gt=0)
    items: list[VarOutliers] = Field(default_factory=list, max_length=MAX_EXPLORE_VARS)


class CorrelationView(_Explore):
    """``correlation``: the matrix (``matrix[i][j]`` for ``vars[i]``, ``vars[j]``)."""

    format: Literal["mapi.data-explorer.correlation/v1"] = "mapi.data-explorer.correlation/v1"
    method: Literal["pearson", "spearman"] = "pearson"
    vars: list[str] = Field(..., max_length=MAX_EXPLORE_VARS)
    matrix: list[list[float | None]]
    p_values: list[list[float | None]] | None = None

    @model_validator(mode="after")
    def _square(self) -> Self:
        n = len(self.vars)
        for m in (self.matrix, self.p_values):
            if m is not None and (len(m) != n or any(len(row) != n for row in m)):
                raise ValueError("matrices must be square with one row per variable")
        return self


class LagProfile(_View):
    """Values by lag with a significance bound (``±bound`` is the 95% band)."""

    var: str = Field(..., max_length=256)
    facet: Facet = Field(default_factory=dict)
    lags: list[int] = Field(..., max_length=2 * MAX_EXPLORE_LAGS + 1)
    values: list[float | None] = Field(..., max_length=2 * MAX_EXPLORE_LAGS + 1)
    bound: float | None = Field(None, ge=0)
    peak_lag: int | None = None
    significant_lags: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _len(self) -> Self:
        if len(self.lags) != len(self.values):
            raise ValueError("lags and values must have the same length")
        return self


class CrossCorrelationView(_Explore):
    """``ccf``: correlation of each driver with the KPI by lag (positive lag = the driver
    leads the KPI by that many periods)."""

    format: Literal["mapi.data-explorer.ccf/v1"] = "mapi.data-explorer.ccf/v1"
    kpi: str = Field(..., max_length=256)
    max_lag: int = Field(..., ge=0, le=MAX_EXPLORE_LAGS)
    prewhitened: bool = False
    items: list[LagProfile] = Field(default_factory=list, max_length=MAX_EXPLORE_VARS)


class AcfPacfView(_Explore):
    """``acf_pacf``: autocorrelation and partial autocorrelation per series."""

    format: Literal["mapi.data-explorer.acf-pacf/v1"] = "mapi.data-explorer.acf-pacf/v1"
    acf: list[LagProfile] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)
    pacf: list[LagProfile] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)
    suggested: dict[str, dict[str, int]] = Field(
        default_factory=dict, description="var -> {p, q} suggested ARMA orders"
    )


class StationarityRow(_View):
    """ADF and KPSS on one series and what to do about it."""

    var: str = Field(..., max_length=256)
    facet: Facet = Field(default_factory=dict)
    adf: StatTestResult | None = None
    kpss: StatTestResult | None = None
    verdict: Literal["stable", "difference", "unclear", "trend_stable"]
    verdict_plain: str = Field(..., max_length=500)


class StationarityView(_Explore):
    """``stationarity``: the unit-root / stationarity table (ADF + KPSS per series)."""

    format: Literal["mapi.data-explorer.stationarity/v1"] = "mapi.data-explorer.stationarity/v1"
    alpha: float = Field(0.05, gt=0, lt=1)
    rows: list[StationarityRow] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)


class StlComponents(_View):
    var: str = Field(..., max_length=256)
    facet: Facet = Field(default_factory=dict)
    period: int = Field(..., ge=2)
    coords: list[ViewScalar] = Field(..., max_length=MAX_EXPLORE_POINTS)
    observed: list[float | None]
    trend: list[float | None]
    seasonal: list[float | None]
    resid: list[float | None]
    seasonal_strength: float | None = Field(None, ge=0, le=1)
    trend_strength: float | None = Field(None, ge=0, le=1)

    @model_validator(mode="after")
    def _len(self) -> Self:
        n = len(self.coords)
        if any(len(x) != n for x in (self.observed, self.trend, self.seasonal, self.resid)):
            raise ValueError("every component needs one value per coord")
        return self


class StlView(_Explore):
    """``stl``: trend, season and remainder per series, with their strengths."""

    format: Literal["mapi.data-explorer.stl/v1"] = "mapi.data-explorer.stl/v1"
    items: list[StlComponents] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)


class VifView(_Explore):
    """``vif``: variance inflation per driver and the condition number."""

    format: Literal["mapi.data-explorer.vif/v1"] = "mapi.data-explorer.vif/v1"
    vif: dict[str, float | None] = Field(default_factory=dict, max_length=MAX_EXPLORE_VARS)
    limit: float = Field(10.0, gt=0)
    flagged: list[str] = Field(default_factory=list)
    condition_number: float | None = None


class GroupStats(_View):
    group: Coord
    n: int = Field(..., ge=0)
    mean: float | None = None
    sd: float | None = None
    median: float | None = None
    share_of_total: float | None = Field(None, ge=0, le=1)


class GroupComparison(_View):
    var: str = Field(..., max_length=256)
    groups: list[GroupStats] = Field(default_factory=list, max_length=1000)
    test: Literal["anova", "kruskal"] | None = None
    p_value: float | None = Field(None, ge=0, le=1)
    differs: bool | None = None


class GroupComparisonView(_Explore):
    """``groups``: each variable compared across the levels of ``by`` (markets, stores)."""

    format: Literal["mapi.data-explorer.groups/v1"] = "mapi.data-explorer.groups/v1"
    by: str = Field(..., max_length=128)
    items: list[GroupComparison] = Field(default_factory=list, max_length=MAX_EXPLORE_VARS)


class QualityWarning(_View):
    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    severity: Literal["info", "warning", "error"] = "warning"
    var: str | None = Field(None, max_length=256)
    coords: dict[str, ViewScalar] = Field(default_factory=dict)
    message_plain: str = Field(..., max_length=500)
    suggested_action: str | None = Field(None, max_length=500)


class QualityView(_Explore):
    """``quality``: data-quality warnings in plain words (gaps, constant columns, negative
    spend, duplicated periods, short history, outliers, collinear drivers)."""

    format: Literal["mapi.data-explorer.quality/v1"] = "mapi.data-explorer.quality/v1"
    items: list[QualityWarning] = Field(default_factory=list, max_length=500)


# ---------------------------------------------------------------------------- Model Explorer


class ParamRow(_View):
    name: str = Field(..., max_length=256)
    coords: dict[str, ViewScalar] = Field(default_factory=dict)
    mean: float | None = None
    sd: float | None = None
    lower: float | None = Field(None, description="Interval lower bound (hdi or ci)")
    upper: float | None = None
    p_value: float | None = Field(None, ge=0, le=1)
    r_hat: float | None = None
    ess_bulk: float | None = None
    prior: str | None = Field(None, max_length=200)


class ParamTableView(_Explore):
    """``params``: estimates with intervals (Bayesian posteriors or frequentist CIs)."""

    format: Literal["mapi.model-explorer.params/v1"] = "mapi.model-explorer.params/v1"
    interval: str = Field("hdi_90", max_length=32, description="hdi_90, ci_95, ...")
    rows: list[ParamRow] = Field(default_factory=list, max_length=5000)
    omitted: int = Field(0, ge=0)


class ModelDiagnosticsView(_Explore):
    """``diagnostics``: convergence and fit statistics."""

    format: Literal["mapi.model-explorer.diagnostics/v1"] = "mapi.model-explorer.diagnostics/v1"
    inference: str = Field(..., max_length=32)
    stats: dict[str, float | None] = Field(
        default_factory=dict, description="r_hat_max, ess_bulk_min, divergences, aic, bic, ..."
    )
    items: list[dict[str, JsonValue]] = Field(
        default_factory=list, max_length=200, description="Diagnostic documents"
    )


class FitVsActualView(_Explore):
    """``fit``: actual vs fitted with an interval, the split line at ``split_at``."""

    format: Literal["mapi.model-explorer.fit/v1"] = "mapi.model-explorer.fit/v1"
    dim: str = Field(..., max_length=128)
    split_at: ViewScalar = None
    series: list[SeriesView] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)
    fitted: list[FanChart] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)
    facets: list[Facet] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)


class QQPoints(_View):
    theoretical: list[float] = Field(..., max_length=2000)
    sample: list[float] = Field(..., max_length=2000)

    @model_validator(mode="after")
    def _len(self) -> Self:
        if len(self.theoretical) != len(self.sample):
            raise ValueError("theoretical and sample quantiles must pair up")
        return self


class ResidualDiagnosticsView(_Explore):
    """``residuals``: residuals over time, their ACF, Ljung-Box, normality and spread tests."""

    format: Literal["mapi.model-explorer.residuals/v1"] = "mapi.model-explorer.residuals/v1"
    series: list[SeriesView] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)
    acf: list[LagProfile] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)
    tests: list[StatTestResult] = Field(default_factory=list, max_length=50)
    histogram: Histogram | None = None
    qq: QQPoints | None = None


class DecompositionView(_Explore):
    """``decomposition``: additive components over time and their share of the total."""

    format: Literal["mapi.model-explorer.decomposition/v1"] = "mapi.model-explorer.decomposition/v1"
    dim: str = Field(..., max_length=128)
    coords: list[ViewScalar] = Field(..., max_length=MAX_EXPLORE_POINTS)
    components: dict[str, list[float | None]] = Field(default_factory=dict, max_length=128)
    shares: dict[str, float | None] = Field(default_factory=dict, max_length=128)
    scale: Literal["additive", "multiplicative"] = "additive"

    @model_validator(mode="after")
    def _len(self) -> Self:
        n = len(self.coords)
        if any(len(v) != n for v in self.components.values()):
            raise ValueError("every component needs one value per coord")
        return self


class ResponseCurvesView(_Explore):
    """``response_curves``: mean response per channel with a band (``[channel][point]``;
    the full per-draw grid is :class:`ResponseGridView`)."""

    format: Literal["mapi.model-explorer.response-curves/v1"] = (
        "mapi.model-explorer.response-curves/v1"
    )
    channels: list[str] = Field(..., min_length=1, max_length=64)
    spend: list[list[float]]
    mean: list[list[float]]
    lower: list[list[float]] | None = None
    upper: list[list[float]] | None = None
    current_spend: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        nc = len(self.channels)
        for arr in (self.spend, self.mean, self.lower, self.upper):
            if arr is not None and len(arr) != nc:
                raise ValueError("curves are [channel][point]")
        if any(len(m) != len(s) for m, s in zip(self.mean, self.spend, strict=True)):
            raise ValueError("mean and spend need the same points per channel")
        return self


class IrfView(_Explore):
    """``irf``: impulse responses of VAR/VECM models: ``values[i][j][h]`` is the response of
    ``variables[i]`` to a shock in ``variables[j]`` after ``h`` periods (h = 0..horizons)."""

    format: Literal["mapi.model-explorer.irf/v1"] = "mapi.model-explorer.irf/v1"
    variables: list[str] = Field(..., min_length=1, max_length=20)
    horizons: int = Field(..., ge=1, le=200)
    orthogonalized: bool = True
    values: list[list[list[float]]]
    lower: list[list[list[float]]] | None = None
    upper: list[list[list[float]]] | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        k, h = len(self.variables), self.horizons + 1
        for arr in (self.values, self.lower, self.upper):
            if arr is None:
                continue
            if len(arr) != k or any(len(r) != k or any(len(c) != h for c in r) for r in arr):
                raise ValueError("irf arrays are [response][shock][horizon 0..horizons]")
        return self


class FevdView(_Explore):
    """``fevd``: forecast-error variance decomposition: ``shares[i][h][j]`` is the share of
    ``variables[i]``'s forecast-error variance at horizon ``h + 1`` due to ``variables[j]``."""

    format: Literal["mapi.model-explorer.fevd/v1"] = "mapi.model-explorer.fevd/v1"
    variables: list[str] = Field(..., min_length=1, max_length=20)
    horizons: int = Field(..., ge=1, le=200)
    shares: list[list[list[float]]]

    @model_validator(mode="after")
    def _shape(self) -> Self:
        k = len(self.variables)
        if len(self.shares) != k or any(
            len(per_h) != self.horizons or any(len(row) != k for row in per_h)
            for per_h in self.shares
        ):
            raise ValueError("fevd shares are [variable][horizon][shock]")
        for per_h in self.shares:
            for row in per_h:
                if abs(sum(row) - 1.0) > 1e-6:
                    raise ValueError("each fevd row sums to 1")
        return self


class ModelForecastView(_Explore):
    """``forecast``: forecast fans after the fitted history (see :class:`ForecastView`)."""

    format: Literal["mapi.model-explorer.forecast/v1"] = "mapi.model-explorer.forecast/v1"
    forecasts: list[ForecastView] = Field(default_factory=list, max_length=MAX_EXPLORE_SERIES)


class BacktestFoldResult(_View):
    fold: Fold
    metrics: dict[str, float | None] = Field(default_factory=dict)
    by_horizon: dict[str, dict[str, float | None]] = Field(default_factory=dict)


class BacktestView(_Explore):
    """``backtest``: metrics per fold and horizon for the model and its benchmarks."""

    format: Literal["mapi.model-explorer.backtest/v1"] = "mapi.model-explorer.backtest/v1"
    plan: dict[str, JsonValue] = Field(default_factory=dict, description="The SplitPlan")
    folds: list[BacktestFoldResult] = Field(default_factory=list, max_length=200)
    summary: dict[str, float | None] = Field(default_factory=dict)
    benchmarks: dict[str, dict[str, float | None]] = Field(default_factory=dict)


class ComparisonView(_Explore):
    """``comparison``: several model versions side by side (``table[model][metric]``)."""

    format: Literal["mapi.model-explorer.comparison/v1"] = "mapi.model-explorer.comparison/v1"
    models: list[str] = Field(..., min_length=1, max_length=20, description="model:// refs")
    metrics: list[str] = Field(default_factory=list, max_length=32)
    table: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    ranking: dict[str, list[str]] = Field(
        default_factory=dict, description="metric -> models best first"
    )
    tests: list[StatTestResult] = Field(
        default_factory=list, description="Pairwise Diebold-Mariano tests"
    )


class ExplorerSectionRef(_View):
    section: str = Field(..., max_length=32)
    view: str = Field(..., max_length=256, description="ui/explore/<section>.json")
    status: Literal["ok", "skipped", "error"] = "ok"
    message_plain: str | None = Field(None, max_length=500)


class ExplorerIndex(_View):
    """``ui/explore/index.json``: what an explore run produced (and why a section is
    missing)."""

    format: Literal["mapi.explorer-index/v1"] = EXPLORER_INDEX_FORMAT
    target: Literal["dataset", "model"]
    ref: str = Field(..., max_length=512, description="dataset://… or model://…")
    sections: list[ExplorerSectionRef] = Field(default_factory=list, max_length=32)
