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

__all__ = [
    "COUNTERFACTUAL_FORMAT",
    "COUNTERFACTUAL_VIEW",
    "FORECAST_FORMAT",
    "FORECAST_VIEW",
    "MAX_GRID_CELLS",
    "MAX_GRID_DRAWS",
    "MAX_LEARNED_BYTES",
    "MAX_STATE_ARRAYS",
    "MAX_STATE_ARRAY_CELLS",
    "PREPROCESS_FORMAT",
    "PREVIEW_FORMAT",
    "PROFILE_FORMAT",
    "RESPONSE_GRID_FORMAT",
    "RESPONSE_GRID_VIEW",
    "TREE_INDEX_FORMAT",
    "CounterfactualView",
    "ForecastView",
    "MissingnessGrid",
    "PreprocessView",
    "PreviewColumn",
    "PreviewView",
    "ProfileView",
    "ResponseGridView",
    "StateArrayView",
    "StepStateView",
    "TreeCoord",
    "TreeGroup",
    "TreeIndexView",
    "TreeVar",
    "VarProfile",
    "ViewScalar",
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
    geos: list[str] | None = Field(None, max_length=256)
    multipliers: list[float] = Field(..., min_length=2, max_length=201)
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
