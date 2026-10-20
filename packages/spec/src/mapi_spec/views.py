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
``ui/explore/          ``mapi.model-explorer.    quantised posterior rasters of the outputs
fields.json``          fields/v1``               that declare a ``grid`` (:class:`FieldsView`)
=====================  ========================  ==========================================

Views are produced inside the sandbox from untrusted data: strings are capped and readers
must render them as text only.
"""

from __future__ import annotations

import base64
import binascii
import sys
from array import array
from collections.abc import Sequence
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from mapi_spec._types import JsonValue
from mapi_spec.calibration import DistributionSummary, FanChart
from mapi_spec.splitplan import Fold
from mapi_spec.stattests import StatTestResult
from mapi_spec.tree import NodePath

__all__ = [
    "COUNTERFACTUAL_FORMAT",
    "COUNTERFACTUAL_VIEW",
    "DATA_SECTIONS",
    "EXPLORER_INDEX_FORMAT",
    "EXPLORE_INDEX",
    "EXPLORE_PREFIX",
    "FIELD_LEVELS",
    "FIELD_NAN",
    "FORECAST_FORMAT",
    "FORECAST_VIEW",
    "MAX_CIRC_BINS",
    "MAX_CIRC_GROUPS",
    "MAX_EXPLORE_LAGS",
    "MAX_EXPLORE_POINTS",
    "MAX_EXPLORE_SERIES",
    "MAX_EXPLORE_VARS",
    "MAX_FIELDS",
    "MAX_FIELD_ARROWS",
    "MAX_FIELD_CELLS",
    "MAX_FIELD_DRAWS",
    "MAX_FIELD_FRAMES",
    "MAX_FIELD_PIXELS",
    "MAX_FRAME_STATS",
    "MAX_GRID_CELLS",
    "MAX_GRID_DRAWS",
    "MAX_GRID_GEOS",
    "MAX_GRID_POINTS",
    "MAX_IMAGE_GROUPS",
    "MAX_LEARNED_BYTES",
    "MAX_MAP_AREAS",
    "MAX_MAP_EDGES",
    "MAX_MAP_VERTICES",
    "MAX_NET_EDGES",
    "MAX_NET_MATRIX",
    "MAX_NET_NODES",
    "MAX_POINTS",
    "MAX_POINT_DENSITY",
    "MAX_PROFILE_FRAMES",
    "MAX_PROFILE_POINTS",
    "MAX_STATE_ARRAYS",
    "MAX_STATE_ARRAY_CELLS",
    "MAX_TREE_NODES",
    "MODEL_SECTIONS",
    "PREPROCESS_FORMAT",
    "PREVIEW_FORMAT",
    "PROFILE_FORMAT",
    "RESPONSE_GRID_FORMAT",
    "RESPONSE_GRID_VIEW",
    "SCIENCE_SECTIONS",
    "SCORE_FIELDS_VIEW",
    "SHAPE_KINDS",
    "TREE_INDEX_FORMAT",
    "AcfPacfView",
    "BacktestFoldResult",
    "BacktestView",
    "CircularEntry",
    "CircularGroup",
    "CircularPair",
    "CircularView",
    "ClarkEvans",
    "ComparisonView",
    "CorrelationView",
    "CounterfactualView",
    "CrossCorrelationView",
    "DataOverviewView",
    "DataSection",
    "DecompositionView",
    "DimOverview",
    "DimRole",
    "DistributionsView",
    "ExplorerIndex",
    "ExplorerSectionRef",
    "FevdView",
    "FieldArrows",
    "FieldEntry",
    "FieldFrame",
    "FieldRaster",
    "FieldsView",
    "FitVsActualView",
    "ForecastView",
    "FrameStats",
    "GroupComparison",
    "GroupComparisonView",
    "GroupStats",
    "HexColour",
    "Histogram",
    "Homophily",
    "ImageComponent",
    "ImageEntry",
    "ImageGroupMean",
    "ImageStats",
    "ImagesView",
    "IrfView",
    "LagProfile",
    "MapFrame",
    "MapLayer",
    "MapPoints",
    "MapRing",
    "MapView",
    "MissingnessGrid",
    "MissingnessView",
    "ModelDiagnosticsView",
    "ModelForecastView",
    "ModelSection",
    "MoranRow",
    "NetComponents",
    "NetPositions",
    "NetworkEdge",
    "NetworkEntry",
    "NetworkView",
    "OutlierPoint",
    "OutliersView",
    "ParamRow",
    "ParamTableView",
    "PhyloSignal",
    "PointAnswers",
    "PointColour",
    "PointDensity",
    "PointSample",
    "PointsView",
    "PreprocessView",
    "PreviewColumn",
    "PreviewView",
    "ProfileEntry",
    "ProfileFrame",
    "ProfilePeak",
    "ProfileView",
    "ProfilesView",
    "QQPoints",
    "QualityView",
    "QualityWarning",
    "ResidualDiagnosticsView",
    "ResponseCurvesView",
    "ResponseGridView",
    "SeriesView",
    "ShapeKind",
    "ShapeView",
    "SkipReason",
    "StateArrayView",
    "StationarityRow",
    "StationarityView",
    "StepStateView",
    "StlComponents",
    "StlView",
    "StructureDim",
    "StructureVar",
    "StructureView",
    "TimeSeriesView",
    "TipTrait",
    "TreeCoord",
    "TreeGroup",
    "TreeIndexView",
    "TreeNodeLayout",
    "TreeVar",
    "TreeView",
    "VarDistribution",
    "VarOutliers",
    "VarOverview",
    "VarProfile",
    "VarUse",
    "ViewScalar",
    "VifView",
    "decode_raster",
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
SCORE_FIELDS_VIEW: Final = "ui/fields.json"
"""Where a predict/forecast/evaluate score run writes the :class:`FieldsView` of its grid
outputs."""
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
MAX_FIELDS: Final = 8
"""Fields per ``fields`` view (outputs with a ``grid``, times the levels of any other dim)."""
MAX_FIELD_PIXELS: Final = 160
"""Raster side cap: larger grids are block-averaged (flagged with ``downsampled``)."""
MAX_FIELD_FRAMES: Final = 24
"""Frames per field (more levels of the ``frame`` dim are thinned evenly)."""
MAX_FIELD_DRAWS: Final = 16
"""Posterior draws kept as rasters per frame (for hypothetical outcome plots)."""
MAX_FIELD_ARROWS: Final = 24
"""Arrow grid side cap of a vector field."""
MAX_FIELD_CELLS: Final = 1_000_000
"""Cap on the raster cells of one ``fields`` view (about 2.7 MB of base64); producers drop
draws, then pixels, to stay under it."""
FIELD_NAN: Final = 65535
"""The uint16 code of a missing (NaN) cell in a :class:`FieldRaster`."""
FIELD_LEVELS: Final = 65534
"""Codes ``0..FIELD_LEVELS`` map linearly onto ``[lo, hi]``."""

MAX_FRAME_STATS: Final = 5000
"""Frames with per-frame statistics in an ``images`` entry (more are thinned evenly)."""
MAX_IMAGE_GROUPS: Final = 12
"""Class-mean rasters per ``images`` entry (levels of a label coord on the frame dim)."""
MAX_PROFILE_FRAMES: Final = 60
"""Curves per ``profiles`` entry (more levels of the frame dim are thinned evenly)."""
MAX_PROFILE_POINTS: Final = MAX_EXPLORE_POINTS
MAX_MAP_AREAS: Final = 2000
MAX_MAP_VERTICES: Final = 20_000
"""Ring vertices of one ``map`` view in total (rings are decimated, ends kept)."""
MAX_MAP_EDGES: Final = 20_000
MAX_NET_NODES: Final = 300
"""Nodes of a node-link drawing (above: the nodes of highest degree)."""
MAX_NET_MATRIX: Final = 140
"""Side of the adjacency heatmap (140 x 140 = 19 600 rows, under the sandbox row cap)."""
MAX_NET_EDGES: Final = 3000
MAX_TREE_NODES: Final = 5000
MAX_CIRC_BINS: Final = 72
MAX_CIRC_GROUPS: Final = 12
MAX_POINTS: Final = 5000
"""Points of a ``points`` sample (above: an even sample plus a 64 x 64 density grid)."""
MAX_POINT_DENSITY: Final = 64

DataSection = Literal[
    "overview", "distributions", "timeseries", "missingness", "outliers", "correlation",
    "ccf", "acf_pacf", "stationarity", "stl", "vif", "groups", "quality",
    "structure", "images", "profiles", "map", "network", "tree", "circular", "points",
]  # fmt: skip
"""Sections of the Data Explorer (``ui/explore/<section>.json``)."""
SCIENCE_SECTIONS: Final[tuple[str, ...]] = (
    "structure", "images", "profiles", "map", "network", "tree", "circular", "points",
)  # fmt: skip
"""The Data Explorer sections for data that is not (only) a time series: the detected
structure and one picture per data shape."""
ShapeKind = Literal[
    "timeseries", "sequence", "image", "vector_field", "areal_map", "points", "network",
    "tree", "circular", "profile", "table",
]  # fmt: skip
"""A data shape the Data Explorer detects (see ``structure``)."""
SHAPE_KINDS: Final[tuple[str, ...]] = (
    "timeseries", "sequence", "image", "vector_field", "areal_map", "points", "network",
    "tree", "circular", "profile", "table",
)  # fmt: skip
SkipReason = Literal["not_applicable", "insufficient_data", "needs_option"]
"""Why a section was skipped: it does not apply to this data, the data is too short, or it
needs a request option (a KPI, a season length)."""
ModelSection = Literal[
    "params", "diagnostics", "fit", "residuals", "decomposition", "response_curves", "irf",
    "fevd", "fields", "forecast", "backtest", "comparison",
]  # fmt: skip
"""Sections of the Model Explorer (``ui/explore/<section>.json``)."""
DATA_SECTIONS: Final[tuple[str, ...]] = (
    "overview", "distributions", "timeseries", "missingness", "outliers", "correlation",
    "ccf", "acf_pacf", "stationarity", "stl", "vif", "groups", "quality",
    "structure", "images", "profiles", "map", "network", "tree", "circular", "points",
)  # fmt: skip
MODEL_SECTIONS: Final[tuple[str, ...]] = (
    "params", "diagnostics", "fit", "residuals", "decomposition", "response_curves", "irf",
    "fevd", "fields", "forecast", "backtest", "comparison",
)  # fmt: skip


def explore_view_name(section: str) -> str:
    """``ui/explore/<section>.json``: where an explore run writes a section."""
    if section not in DATA_SECTIONS and section not in MODEL_SECTIONS:
        raise ValueError(f"unknown explorer section {section!r}")
    return f"{EXPLORE_PREFIX}{section}.json"


Coord = str | int | float | bool
Facet = dict[str, Coord]


class _Explore(_View):
    """Base of explorer views: a plain-language caption and warnings (and, M4, the tree
    node a data-explorer view describes)."""

    caption_plain: str | None = Field(None, max_length=1000)
    warnings_plain: list[str] = Field(default_factory=list, max_length=50)
    node: NodePath | None = Field(
        None,
        description="M4: the node of a tree dataset the view describes (absent for a flat dataset)",
        exclude_if=lambda v: v is None,
    )


# ----------------------------------------------------------------------------- Data Explorer


class DimOverview(_View):
    name: str = Field(..., max_length=256)
    kind: str = Field(..., max_length=32)
    size: int = Field(..., ge=0)
    start: ViewScalar = None
    end: ViewScalar = None
    freq: str | None = Field(None, max_length=32)


VarUse = Literal["data", "geometry", "mask", "weight", "index", "label"]
DimRole = Literal[
    "time", "sequence", "frame", "y", "x", "component", "channel", "area", "vertex", "node",
    "pair", "item", "columns", "tip", "along", "category", "index", "other",
]  # fmt: skip


class VarOverview(_View):
    name: str = Field(..., max_length=256)
    dtype: str = Field(..., max_length=64)
    dims: list[str] = Field(default_factory=list, max_length=32)
    role: str | None = Field(None, max_length=32)
    units: str | None = Field(None, max_length=64)
    missing_share: float = Field(0.0, ge=0, le=1)
    distinct: int | None = Field(None, ge=0)
    constant: bool = False
    used_as: VarUse | None = Field(
        None,
        description="How the explorer uses the variable: data, or geometry (polygon rings, "
        "neighbours, tree links), a mask, a weight, an index or a label. Absent for a "
        "plain time-series panel",
    )


class ShapeView(_View):
    """One detected data shape: which variables it shows, the dims it uses by role
    (``{"y": "row", "x": "col", "frame": "quarter"}``) and where to see it."""

    kind: ShapeKind
    vars: list[str] = Field(default_factory=list, max_length=MAX_EXPLORE_VARS)
    dims: dict[str, str] = Field(default_factory=dict, max_length=16)
    score: float = Field(..., ge=0, le=1)
    label_plain: str = Field(..., max_length=300)
    sections: list[str] = Field(default_factory=list, max_length=8)


class DataOverviewView(_Explore):
    """``overview``: shape, dims, variables and a plain-language summary."""

    format: Literal["mapi.data-explorer.overview/v1"] = "mapi.data-explorer.overview/v1"
    n_cells: int = Field(..., ge=0)
    dims: list[DimOverview] = Field(default_factory=list, max_length=32)
    vars: list[VarOverview] = Field(default_factory=list, max_length=MAX_EXPLORE_VARS)
    summary_plain: str = Field(..., max_length=1000)
    quality_score: float | None = Field(None, ge=0, le=1, description="1 = no issues found")
    shapes: list[ShapeView] | None = Field(
        None,
        max_length=64,
        description="The detected data shapes, best first (absent for a plain time-series panel)",
    )


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


class FieldRaster(_View):
    """One quantised raster: ``height`` x ``width`` cells, row-major (row 0 first).

    ``data`` is the base64 of little-endian ``uint16`` codes, two bytes per cell. Code
    :data:`FIELD_NAN` (65535) is a missing cell; a code ``q`` in ``0..FIELD_LEVELS`` decodes
    to ``lo + (hi - lo) * q / FIELD_LEVELS`` (every code is ``lo`` when ``lo == hi``). When the
    field's ``encoding`` is ``log10`` that number is ``log10`` of the value (values <= 0 are
    missing). ``lo``/``hi`` are ``None`` only when every cell is missing. The step between two
    codes is ``(hi - lo) / 65534``: four to five significant digits.
    """

    lo: float | None = None
    hi: float | None = None
    data: str = Field(..., max_length=4 * MAX_FIELD_PIXELS * MAX_FIELD_PIXELS)

    @model_validator(mode="after")
    def _range(self) -> Self:
        if (self.lo is None) != (self.hi is None):
            raise ValueError("a raster has both lo and hi, or neither")
        if self.lo is not None and self.hi is not None and self.hi < self.lo:
            raise ValueError("a raster's hi is below its lo")
        return self

    def cells(self) -> int:
        """The number of cells ``data`` holds (it must be valid base64 of whole uint16s)."""
        try:
            raw = base64.b64decode(self.data, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("raster data is not base64") from None
        if len(raw) % 2:
            raise ValueError("raster data is not a whole number of uint16 codes")
        return len(raw) // 2


def decode_raster(
    r: FieldRaster, encoding: Literal["linear", "log10"] = "linear"
) -> list[float | None]:
    """The values of a raster (row-major, ``None`` = missing), as a reader decodes them."""
    codes = array("H")
    codes.frombytes(base64.b64decode(r.data))
    if sys.byteorder == "big":
        codes.byteswap()
    lo, hi = r.lo, r.hi
    out: list[float | None] = []
    for q in codes:
        if q == FIELD_NAN or lo is None or hi is None:
            out.append(None)
            continue
        v = lo + (hi - lo) * min(q, FIELD_LEVELS) / FIELD_LEVELS
        out.append(10.0**v if encoding == "log10" else v)
    return out


class FieldArrows(_View):
    """The mean ``(x, y)`` components of a vector field on a coarse grid (one arrow per cell).

    ``x``/``y`` are the arrow centres in raster pixel units (column, row; fractional, 0 =
    the centre of the first pixel). ``u`` is the component along increasing column index and
    ``v`` along increasing row index (so with ``origin="lower"`` a positive ``v`` points up),
    both ``[row][col]`` in the field's units.
    """

    x: list[float] = Field(..., min_length=1, max_length=MAX_FIELD_ARROWS)
    y: list[float] = Field(..., min_length=1, max_length=MAX_FIELD_ARROWS)
    u: list[list[float | None]]
    v: list[list[float | None]]

    @model_validator(mode="after")
    def _shape(self) -> Self:
        ny, nx = len(self.y), len(self.x)
        for a in (self.u, self.v):
            if len(a) != ny or any(len(row) != nx for row in a):
                raise ValueError("arrow components are [row][col] over y and x")
        return self


class FieldFrame(_View):
    """One frame of a field (one level of its ``frame`` dim, or the only frame): the posterior
    mean, sd, 5% and 95% quantiles and a few draws, each a :class:`FieldRaster`. For a vector
    field the rasters are of the magnitude ``sqrt(u**2 + v**2)`` (per draw, then summarised)
    and ``arrows`` holds the mean components."""

    label: ViewScalar = None
    mean: FieldRaster
    sd: FieldRaster | None = None
    q05: FieldRaster | None = None
    q95: FieldRaster | None = None
    draws: list[FieldRaster] = Field(default_factory=list, max_length=MAX_FIELD_DRAWS)
    arrows: FieldArrows | None = None

    def rasters(self) -> list[FieldRaster]:
        return [r for r in (self.mean, self.sd, self.q05, self.q95, *self.draws) if r is not None]


class FieldEntry(_View):
    """One output drawn as a 2-D field (see :class:`mapi_spec.capabilities.GridDecl`).

    ``y_coords``/``x_coords`` label the raster rows and columns (for a downsampled raster,
    the coordinate at the middle of each block). Row 0 is drawn at the top unless ``origin``
    is ``lower``. ``facet`` names the level of each other dim of the output (one entry per
    level). ``kind`` is ``vector`` when the output has a ``component`` dim.
    """

    output: str = Field(..., max_length=128)
    units: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=2000)
    facet: Facet = Field(default_factory=dict)
    kind: Literal["scalar", "vector"] = "scalar"
    y_dim: str = Field(..., max_length=128)
    x_dim: str = Field(..., max_length=128)
    frame_dim: str | None = Field(None, max_length=128)
    component_dim: str | None = Field(None, max_length=128)
    height: int = Field(..., ge=1, le=MAX_FIELD_PIXELS)
    width: int = Field(..., ge=1, le=MAX_FIELD_PIXELS)
    source_height: int = Field(..., ge=1)
    source_width: int = Field(..., ge=1)
    downsampled: bool = False
    y_coords: list[ViewScalar] = Field(..., max_length=MAX_FIELD_PIXELS)
    x_coords: list[ViewScalar] = Field(..., max_length=MAX_FIELD_PIXELS)
    colormap: Literal["viridis", "magma", "cividis", "gray", "diverging"] = "viridis"
    log: bool = Field(False, description="Draw log10 of the value")
    origin: Literal["upper", "lower"] = "upper"
    encoding: Literal["linear", "log10"] = Field(
        "linear", description="How the raster codes map to values (see FieldRaster)"
    )
    n_draws: int = Field(0, ge=0, description="Posterior draws behind the statistics")
    frames: list[FieldFrame] = Field(..., min_length=1, max_length=MAX_FIELD_FRAMES)
    omitted_frames: int = Field(0, ge=0, description="Frames left out by even thinning")

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if len(self.y_coords) != self.height or len(self.x_coords) != self.width:
            raise ValueError("y_coords and x_coords need one label per raster row and column")
        if self.height > self.source_height or self.width > self.source_width:
            raise ValueError("a raster cannot be larger than its source grid")
        cells = self.height * self.width
        for f in self.frames:
            for r in f.rasters():
                if r.cells() != cells:
                    raise ValueError(f"every raster needs {cells} cells (height x width)")
            if self.kind == "scalar" and f.arrows is not None:
                raise ValueError("only vector fields have arrows")
        return self

    def cells(self) -> int:
        return self.height * self.width * sum(len(f.rasters()) for f in self.frames)


class FieldsView(_Explore):
    """``fields``: the outputs that declare a ``grid`` as interactive rasters (intensity maps,
    images, space-time densities, vector fields), from the in-sample predictions.

    Rasters are quantised to uint16 (:class:`FieldRaster`); one view holds at most
    :data:`MAX_FIELD_CELLS` cells. ``omitted`` lists the grid outputs that are not shown (more
    than :data:`MAX_FIELDS`, or not in the predictions).
    """

    format: Literal["mapi.model-explorer.fields/v1"] = "mapi.model-explorer.fields/v1"
    kind: Literal["expected", "predictive"] = "expected"
    fields: list[FieldEntry] = Field(..., min_length=1, max_length=MAX_FIELDS)
    omitted: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def _size(self) -> Self:
        cells = sum(f.cells() for f in self.fields)
        if cells > MAX_FIELD_CELLS:
            raise ValueError(f"the view holds {cells} raster cells (max {MAX_FIELD_CELLS})")
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


# ------------------------------------------------------------- Data Explorer: scientific data
#
# The sections for data that is not (only) a time series. ``structure`` lists the data
# shapes the runner detects; each picture section draws one kind of shape. Every list is
# capped (constants above) and every string is user data (text only).


def _examples(section: str) -> ConfigDict:
    """The config of a scientific view: extra fields allowed, frozen, one JSON Schema
    example (:mod:`mapi_spec.explorer_examples`)."""
    from mapi_spec.explorer_examples import SCIENCE_VIEW_EXAMPLES

    return ConfigDict(
        extra="allow",
        frozen=True,
        json_schema_extra={"examples": [SCIENCE_VIEW_EXAMPLES[section]]},
    )


class StructureDim(_View):
    """One dim and the part it plays in the data (``role_in_data``)."""

    name: str = Field(..., max_length=256)
    size: int = Field(..., ge=0)
    kind: str = Field(..., max_length=32)
    role_in_data: DimRole = "other"
    start: ViewScalar = None
    end: ViewScalar = None
    evenly_spaced: bool | None = None


class StructureVar(_View):
    """One variable: its dims and how the explorer uses it."""

    name: str = Field(..., max_length=256)
    dims: list[str] = Field(default_factory=list, max_length=32)
    shape: list[int] = Field(default_factory=list, max_length=32)
    dtype: str = Field(..., max_length=64)
    role: str | None = Field(None, max_length=32)
    units: str | None = Field(None, max_length=64)
    used_as: VarUse = "data"


class StructureView(_Explore):
    """``structure``: the data shapes found in the dataset (best first), every dim with its
    part in the data, every variable with its use, the sections in order of relevance and
    the sections that do not apply (with the reason in plain words)."""

    format: Literal["mapi.data-explorer.structure/v1"] = "mapi.data-explorer.structure/v1"

    model_config = _examples("structure")

    shapes: list[ShapeView] = Field(default_factory=list, max_length=64)
    dims: list[StructureDim] = Field(default_factory=list, max_length=32)
    vars: list[StructureVar] = Field(default_factory=list, max_length=MAX_EXPLORE_VARS)
    recommended: list[str] = Field(default_factory=list, max_length=32)
    not_applicable: dict[str, str] = Field(default_factory=dict, max_length=32)
    time_dim: str | None = Field(None, max_length=256)
    sequence_dim: str | None = Field(None, max_length=256)
    unit_dim: str | None = Field(None, max_length=256)
    categorical_dims: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _sections(self) -> Self:
        known = set(DATA_SECTIONS)
        for s in (*self.recommended, *self.not_applicable):
            if s not in known:
                raise ValueError(f"unknown section {s!r}")
        return self


def _same_len(name: str, n: int, *lists: Sequence[object] | None) -> None:
    for xs in lists:
        if xs is not None and len(xs) != n:
            raise ValueError(f"{name}: every list needs {n} values")


class FrameStats(_View):
    """Statistics of every frame of an image (``coords`` = the frame labels; thinned evenly
    above :data:`MAX_FRAME_STATS` frames, flagged with ``downsampled``)."""

    coords: list[ViewScalar] = Field(..., max_length=MAX_FRAME_STATS)
    mean: list[float | None] = Field(..., max_length=MAX_FRAME_STATS)
    sd: list[float | None] = Field(..., max_length=MAX_FRAME_STATS)
    min: list[float | None] = Field(..., max_length=MAX_FRAME_STATS)
    max: list[float | None] = Field(..., max_length=MAX_FRAME_STATS)
    missing_share: list[float] = Field(..., max_length=MAX_FRAME_STATS)
    downsampled: bool = False

    @model_validator(mode="after")
    def _len(self) -> Self:
        n = len(self.coords)
        _same_len("frame_stats", n, self.mean, self.sd, self.min, self.max, self.missing_share)
        return self


class ImageStats(_View):
    """Statistics over every cell of an image (all frames)."""

    min: float | None = None
    max: float | None = None
    mean: float | None = None
    sd: float | None = None
    missing_share: float = Field(0.0, ge=0, le=1)
    saturated_share: float = Field(
        0.0, ge=0, le=1, description="share of the cells at the maximum value"
    )


class ImageGroupMean(_View):
    """The mean raster of the frames with one label (``group_by`` of the entry)."""

    label: ViewScalar = None
    n: int = Field(..., ge=0)
    mean: FieldRaster


class ImageComponent(_View):
    """The histogram of one component of a vector field."""

    label: ViewScalar = None
    histogram: Histogram | None = None


class ImageEntry(FieldEntry):
    """One data variable drawn as an image (a :class:`FieldEntry` with ``n_draws = 0``: each
    frame holds the data value in ``mean``; for a vector field the magnitude, with
    ``arrows``). ``output`` repeats ``var`` so the field viewer reads it as is.

    ``mask`` is the mask variable ``mask_var`` on the same raster: the share of frames where
    the mask is true (0 or 1 for a mask without a frame dim)."""

    var: str = Field(..., max_length=256)
    role: str | None = Field(None, max_length=32)
    mask_var: str | None = Field(None, max_length=256)
    mask: FieldRaster | None = None
    frame_stats: FrameStats | None = None
    histogram: Histogram | None = None
    log_counts: bool = Field(False, description="the histogram is skewed: show log counts")
    stats: ImageStats = Field(default_factory=ImageStats)
    group_by: str | None = Field(None, max_length=256)
    groups: list[ImageGroupMean] = Field(default_factory=list, max_length=MAX_IMAGE_GROUPS)
    components: list[ImageComponent] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def _extra_rasters(self) -> Self:
        cells = self.height * self.width
        for r in (self.mask, *(g.mean for g in self.groups)):
            if r is not None and r.cells() != cells:
                raise ValueError(f"every raster needs {cells} cells (height x width)")
        return self

    def cells(self) -> int:
        extra = (self.mask is not None) + len(self.groups)
        return super().cells() + self.height * self.width * extra


class ImagesView(_Explore):
    """``images``: data variables on a 2-D grid (pictures, movies, plates, vector fields)
    as quantised rasters for the field viewer, with per-frame statistics, a histogram of
    every cell and class means. One view holds at most :data:`MAX_FIELD_CELLS` cells."""

    format: Literal["mapi.data-explorer.images/v1"] = "mapi.data-explorer.images/v1"

    model_config = _examples("images")

    fields: list[ImageEntry] = Field(..., min_length=1, max_length=MAX_FIELDS)
    omitted: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def _size(self) -> Self:
        cells = sum(f.cells() for f in self.fields)
        if cells > MAX_FIELD_CELLS:
            raise ValueError(f"the view holds {cells} raster cells (max {MAX_FIELD_CELLS})")
        return self


class ProfileFrame(_View):
    """One curve of a profile (one level of the frame dim, or the only curve)."""

    label: ViewScalar = None
    values: list[float | None] = Field(..., max_length=MAX_PROFILE_POINTS)


class ProfilePeak(_View):
    """Where a curve is highest."""

    frame: ViewScalar = None
    at: ViewScalar = None
    value: float | None = None


class ProfileEntry(_View):
    """A variable along a non-time axis (position, multipole, wavelength): one curve per
    level of ``frame_dim``; ``values`` of each frame align with ``coords``."""

    var: str = Field(..., max_length=256)
    units: str | None = Field(None, max_length=64)
    along: str = Field(..., max_length=256)
    coords: list[ViewScalar] = Field(..., max_length=MAX_PROFILE_POINTS)
    frame_dim: str | None = Field(None, max_length=256)
    frames: list[ProfileFrame] = Field(..., min_length=1, max_length=MAX_PROFILE_FRAMES)
    omitted_frames: int = Field(0, ge=0)
    facet: Facet = Field(default_factory=dict)
    log_x: bool = False
    log_y: bool = False
    peak: list[ProfilePeak] = Field(default_factory=list, max_length=MAX_PROFILE_FRAMES)
    downsampled: bool = False

    @model_validator(mode="after")
    def _len(self) -> Self:
        _same_len(f"profile {self.var}", len(self.coords), *(f.values for f in self.frames))
        return self


class ProfilesView(_Explore):
    """``profiles``: curves and spectra along a non-time axis."""

    format: Literal["mapi.data-explorer.profiles/v1"] = "mapi.data-explorer.profiles/v1"

    model_config = _examples("profiles")

    items: list[ProfileEntry] = Field(..., min_length=1, max_length=64)
    omitted: list[str] = Field(default_factory=list, max_length=256)


class MapRing(_View):
    """One polygon ring of area ``area`` (an index into ``areas``); an area can have more
    than one ring (islands)."""

    area: int = Field(..., ge=0)
    x: list[float] = Field(..., min_length=1, max_length=MAX_MAP_VERTICES)
    y: list[float] = Field(..., min_length=1, max_length=MAX_MAP_VERTICES)

    @model_validator(mode="after")
    def _len(self) -> Self:
        _same_len("ring", len(self.x), self.y)
        return self


class MapPoints(_View):
    """Columnar points (one per area)."""

    x: list[float | None] = Field(..., max_length=MAX_MAP_AREAS)
    y: list[float | None] = Field(..., max_length=MAX_MAP_AREAS)

    @model_validator(mode="after")
    def _len(self) -> Self:
        _same_len("points", len(self.x), self.y)
        return self


class MapFrame(_View):
    label: ViewScalar = None
    values: list[float | None] = Field(..., max_length=MAX_MAP_AREAS)


class MapLayer(_View):
    """One value per area (``values``, the first frame or the only one); ``frames`` when
    the variable has a second dim. ``derived_from`` names the variables of a derived layer
    (``ratio = target / exposure``)."""

    var: str = Field(..., max_length=256)
    units: str | None = Field(None, max_length=64)
    role: str | None = Field(None, max_length=32)
    values: list[float | None] = Field(..., max_length=MAX_MAP_AREAS)
    frame_dim: str | None = Field(None, max_length=256)
    frames: list[MapFrame] = Field(default_factory=list, max_length=MAX_PROFILE_FRAMES)
    derived_from: list[str] = Field(default_factory=list, max_length=2)


class MoranRow(_View):
    """Moran's I of one layer: ``morans_i`` against its expectation ``-1/(n-1)``, the z
    score and the p value of ``permutations`` random permutations."""

    var: str = Field(..., max_length=256)
    morans_i: float | None = None
    expected: float | None = None
    z: float | None = None
    p_value: float | None = Field(None, ge=0, le=1)
    weights: Literal["adjacency", "knn4"] = "adjacency"
    permutations: int = Field(999, ge=0)


class MapView(_Explore):
    """``map``: areas (polygons) coloured by each data variable, the neighbour links and
    the spatial autocorrelation of each layer."""

    format: Literal["mapi.data-explorer.map/v1"] = "mapi.data-explorer.map/v1"

    model_config = _examples("map")

    area_dim: str = Field(..., max_length=256)
    areas: list[ViewScalar] = Field(..., min_length=1, max_length=MAX_MAP_AREAS)
    rings: list[MapRing] = Field(default_factory=list, max_length=4 * MAX_MAP_AREAS)
    decimated: bool = False
    bbox: list[float] | None = Field(
        None, min_length=4, max_length=4, description="xmin, ymin, xmax, ymax"
    )
    centroids: MapPoints | None = None
    layers: list[MapLayer] = Field(default_factory=list, max_length=64)
    edges: list[list[int]] = Field(default_factory=list, max_length=MAX_MAP_EDGES)
    isolates: list[int] = Field(default_factory=list, max_length=MAX_MAP_AREAS)
    moran: list[MoranRow] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        n = len(self.areas)
        if sum(len(r.x) for r in self.rings) > MAX_MAP_VERTICES:
            raise ValueError(f"the rings hold more than {MAX_MAP_VERTICES} vertices")
        if any(r.area >= n for r in self.rings):
            raise ValueError("a ring names an area that is not in areas")
        if self.centroids is not None:
            _same_len("centroids", n, self.centroids.x)
        for layer in self.layers:
            _same_len(f"layer {layer.var}", n, layer.values, *(f.values for f in layer.frames))
        for e in self.edges:
            if len(e) != 2 or not all(0 <= i < n for i in e):
                raise ValueError("an edge is a pair of area indices")
        if any(not 0 <= i < n for i in self.isolates):
            raise ValueError("an isolate is an area index")
        return self


class NetworkEdge(_View):
    """An edge between shown nodes ``i`` and ``j`` (indices into ``labels``), weight ``w``."""

    i: int = Field(..., ge=0)
    j: int = Field(..., ge=0)
    w: float


class NetPositions(_View):
    """Columnar layout positions, one per shown node."""

    x: list[float] = Field(..., max_length=MAX_NET_NODES)
    y: list[float] = Field(..., max_length=MAX_NET_NODES)


class NetComponents(_View):
    count: int = Field(..., ge=0)
    largest_share: float = Field(..., ge=0, le=1)


class Homophily(_View):
    """The share of edges within one level of node attribute ``attr`` against the share
    expected when edges ignore it."""

    attr: str = Field(..., max_length=256)
    within_share: float | None = Field(None, ge=0, le=1)
    expected_share: float | None = Field(None, ge=0, le=1)


class NetworkEntry(_View):
    """A variable on a pair of node dims (``X``, ``X_``) drawn as a network.

    ``labels`` are the shown nodes (at most :data:`MAX_NET_NODES`, the ones of highest
    degree; ``omitted_nodes`` counts the rest); ``edges``, ``degree``, ``positions`` and
    ``node_attrs`` index them. ``matrix`` (absent above :data:`MAX_NET_MATRIX` nodes) is the
    adjacency in seriation order: ``matrix[a][b]`` links ``labels[order[a]]`` and
    ``labels[order[b]]``."""

    var: str = Field(..., max_length=256)
    node_dim: str = Field(..., max_length=256)
    facet: Facet = Field(default_factory=dict)
    binary: bool = False
    signed: bool = False
    directed: bool = False
    weighted: bool = True
    n_nodes: int = Field(..., ge=0)
    labels: list[ViewScalar] = Field(..., max_length=MAX_NET_NODES)
    omitted_nodes: int = Field(0, ge=0)
    matrix: list[list[float | None]] | None = None
    order: list[int] = Field(default_factory=list, max_length=MAX_NET_MATRIX)
    positions: NetPositions | None = None
    edges: list[NetworkEdge] = Field(default_factory=list, max_length=MAX_NET_EDGES)
    omitted_edges: int = Field(0, ge=0)
    threshold: float | None = Field(None, description="|w| below this is not drawn")
    degree: list[float] = Field(default_factory=list, max_length=MAX_NET_NODES)
    degree_hist: Histogram | None = None
    density: float | None = Field(None, ge=0, le=1)
    transitivity: float | None = Field(None, ge=0, le=1)
    reciprocity: float | None = Field(None, ge=0, le=1)
    components: NetComponents | None = None
    node_attrs: dict[str, list[ViewScalar]] = Field(default_factory=dict, max_length=32)
    homophily: list[Homophily] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        n = len(self.labels)
        if self.degree:
            _same_len("degree", n, self.degree)
        if self.positions is not None:
            _same_len("positions", n, self.positions.x, self.positions.y)
        for vals in self.node_attrs.values():
            _same_len("node_attrs", n, vals)
        if any(e.i >= n or e.j >= n for e in self.edges):
            raise ValueError("an edge names a node that is not shown")
        if self.matrix is not None:
            m = len(self.order)
            if len(self.matrix) != m or any(len(r) != m for r in self.matrix):
                raise ValueError("the matrix is square with one row per entry of order")
            if any(not 0 <= i < n for i in self.order):
                raise ValueError("order holds indices into labels")
        return self


class NetworkView(_Explore):
    """``network``: adjacency heatmaps, node-link drawings and degree statistics."""

    format: Literal["mapi.data-explorer.network/v1"] = "mapi.data-explorer.network/v1"

    model_config = _examples("network")

    items: list[NetworkEntry] = Field(..., min_length=1, max_length=16)
    omitted: list[str] = Field(default_factory=list, max_length=256)


class TreeNodeLayout(_View):
    """Columnar layout of the tree (one entry per node): ``x`` = distance from the root,
    ``y`` = the vertical place (tips at 0, 1, 2, ...), ``parent`` = index of the parent
    (-1 for a root)."""

    x: list[float] = Field(..., max_length=MAX_TREE_NODES)
    y: list[float] = Field(..., max_length=MAX_TREE_NODES)
    parent: list[int] = Field(..., max_length=MAX_TREE_NODES)
    is_tip: list[bool] = Field(..., max_length=MAX_TREE_NODES)
    label: list[str | None] = Field(..., max_length=MAX_TREE_NODES)

    @model_validator(mode="after")
    def _len(self) -> Self:
        n = len(self.x)
        _same_len("layout", n, self.y, self.parent, self.is_tip, self.label)
        if any(not -1 <= p < n for p in self.parent):
            raise ValueError("parent holds node indices or -1")
        return self


class TipTrait(_View):
    """One trait per tip, in the order of ``tips`` (tips sorted by their place ``y``)."""

    var: str = Field(..., max_length=256)
    units: str | None = Field(None, max_length=64)
    values: list[float | None] = Field(..., max_length=MAX_TREE_NODES)


class PhyloSignal(_View):
    """Moran's I of a trait with inverse patristic distance weights."""

    var: str = Field(..., max_length=256)
    morans_i: float | None = None
    p_value: float | None = Field(None, ge=0, le=1)


class TreeView(_Explore):
    """``tree``: a tree stored in variables (``parent``, ``branch_length``) drawn as a
    dendrogram with the traits of its tips. (Not a *tree dataset*, whose groups are picked
    with ``?node=``.)"""

    format: Literal["mapi.data-explorer.tree/v1"] = "mapi.data-explorer.tree/v1"

    model_config = _examples("tree")

    node_dim: str = Field(..., max_length=256)
    n_nodes: int = Field(..., ge=1)
    n_tips: int = Field(..., ge=0)
    roots: list[int] = Field(default_factory=list, max_length=MAX_TREE_NODES)
    depth: float | None = None
    ultrametric: bool = False
    layout: TreeNodeLayout
    tips: list[int] = Field(default_factory=list, max_length=MAX_TREE_NODES)
    tip_dim: str | None = Field(None, max_length=256)
    matched_by: Literal["label", "count", "order"] | None = None
    unmatched: int = Field(0, ge=0)
    traits: list[TipTrait] = Field(default_factory=list, max_length=64)
    signal: list[PhyloSignal] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        n = len(self.layout.x)
        if n != self.n_nodes:
            raise ValueError("the layout needs one entry per node")
        if any(not 0 <= i < n for i in (*self.tips, *self.roots)):
            raise ValueError("tips and roots hold node indices")
        for t in self.traits:
            _same_len(f"trait {t.var}", len(self.tips), t.values)
        return self


class CircularGroup(_View):
    """Counts per bin and circular statistics of one group (or of all the data)."""

    label: ViewScalar = None
    n: int = Field(..., ge=0)
    counts: list[int] = Field(..., min_length=1, max_length=MAX_CIRC_BINS)
    mean_direction: float | None = Field(None, description="in the variable's units")
    resultant_length: float | None = Field(None, ge=0, le=1, description="R: 0 = spread out")
    circular_sd: float | None = Field(None, ge=0, description="in the variable's units")
    rayleigh_z: float | None = None
    rayleigh_p: float | None = Field(None, ge=0, le=1)


class CircularEntry(_View):
    """One angle or time of day (``period`` = 360 degrees, 24 hours, ...): a rose per group.

    ``bins`` are the ``len(counts) + 1`` bin edges in the variable's units. ``zero`` is where
    0 sits (``north`` = top, compass and clock) and ``clockwise`` the direction of increase.
    """

    var: str = Field(..., max_length=256)
    units: str | None = Field(None, max_length=64)
    period: float = Field(..., gt=0)
    zero: Literal["north", "east"] = "north"
    clockwise: bool = True
    group_by: str | None = Field(None, max_length=256)
    bins: list[float] = Field(..., min_length=2, max_length=MAX_CIRC_BINS + 1)
    groups: list[CircularGroup] = Field(..., min_length=1, max_length=MAX_CIRC_GROUPS + 1)
    omitted_groups: int = Field(0, ge=0)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        for g in self.groups:
            _same_len(f"counts of {self.var}", len(self.bins) - 1, g.counts)
        return self


class CircularPair(_View):
    """Two circular variables on one dim: counts ``[a bin][b bin]`` and the circular
    correlation ``rho`` (Jammalamadaka-SenGupta)."""

    a: str = Field(..., max_length=256)
    b: str = Field(..., max_length=256)
    a_bins: list[float] = Field(..., min_length=2, max_length=MAX_CIRC_BINS + 1)
    b_bins: list[float] = Field(..., min_length=2, max_length=MAX_CIRC_BINS + 1)
    counts: list[list[int]]
    rho: float | None = Field(None, ge=-1, le=1)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        _same_len("pair counts", len(self.a_bins) - 1, self.counts)
        for row in self.counts:
            _same_len("pair counts", len(self.b_bins) - 1, row)
        return self


class CircularView(_Explore):
    """``circular``: directions and times of day as rose diagrams, with the mean
    direction, its strength and the Rayleigh test, per group."""

    format: Literal["mapi.data-explorer.circular/v1"] = "mapi.data-explorer.circular/v1"

    model_config = _examples("circular")

    items: list[CircularEntry] = Field(..., min_length=1, max_length=16)
    pairs: list[CircularPair] = Field(default_factory=list, max_length=8)


HexColour = Annotated[str, StringConstraints(pattern=r"^#[0-9a-f]{6}$")]


class PointSample(_View):
    """The shown points (all of them, or an even sample): positions and their index along
    ``point_dim``."""

    x: list[float | None] = Field(..., max_length=MAX_POINTS)
    y: list[float | None] = Field(..., max_length=MAX_POINTS)
    index: list[int] = Field(..., max_length=MAX_POINTS)
    labels: list[ViewScalar] | None = Field(None, max_length=MAX_POINTS)

    @model_validator(mode="after")
    def _len(self) -> Self:
        _same_len("sample", len(self.index), self.x, self.y, self.labels)
        return self


class PointDensity(_View):
    """Counts of all points on a grid (``counts[row][col]`` over ``y_edges`` x
    ``x_edges``)."""

    x_edges: list[float] = Field(..., min_length=2, max_length=MAX_POINT_DENSITY + 1)
    y_edges: list[float] = Field(..., min_length=2, max_length=MAX_POINT_DENSITY + 1)
    counts: list[list[int]]

    @model_validator(mode="after")
    def _shape(self) -> Self:
        _same_len("density", len(self.y_edges) - 1, self.counts)
        for row in self.counts:
            _same_len("density", len(self.x_edges) - 1, row)
        return self


class PointColour(_View):
    """A numeric variable on the points, aligned with the sample."""

    var: str = Field(..., max_length=256)
    units: str | None = Field(None, max_length=64)
    values: list[float | None] = Field(..., max_length=MAX_POINTS)


class PointAnswers(_View):
    """A text variable on (``by_dim``, points), such as the colour term each speaker gave
    each chip: the terms, the modal term per point, the share that gave it (agreement) and
    the counts ``[point][term]``, aligned with the sample."""

    var: str = Field(..., max_length=256)
    by_dim: str = Field(..., max_length=256)
    terms: list[str] = Field(..., max_length=64)
    mode: list[str | None] = Field(..., max_length=MAX_POINTS)
    agreement: list[float | None] = Field(..., max_length=MAX_POINTS)
    counts: list[list[int]] = Field(default_factory=list, max_length=MAX_POINTS)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        _same_len("answers", len(self.mode), self.agreement)
        if self.counts:
            _same_len("answers", len(self.mode), self.counts)
            for row in self.counts:
                _same_len("answer counts", len(self.terms), row)
        return self


class ClarkEvans(_View):
    """The Clark-Evans ratio of the points (R < 1: clustered, R > 1: regular)."""

    r: float | None = None
    z: float | None = None
    p_value: float | None = Field(None, ge=0, le=1)


class PointsView(_Explore):
    """``points``: positions given by two variables (``x``, ``y``) on one dim, drawn as
    dots or as cells of a ``lattice``, coloured by a variable, a CIE Lab colour (``srgb``,
    ``#rrggbb``) or the answers of a text variable."""

    format: Literal["mapi.data-explorer.points/v1"] = "mapi.data-explorer.points/v1"

    model_config = _examples("points")

    point_dim: str = Field(..., max_length=256)
    x: str = Field(..., max_length=256)
    y: str = Field(..., max_length=256)
    lattice: bool = False
    n: int = Field(..., ge=0)
    sample: PointSample
    density: PointDensity | None = None
    colour: list[PointColour] = Field(default_factory=list, max_length=32)
    srgb: list[HexColour] | None = Field(None, max_length=MAX_POINTS)
    lab_vars: list[str] = Field(default_factory=list, max_length=3)
    answers: PointAnswers | None = None
    clark_evans: ClarkEvans | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        n = len(self.sample.index)
        for c in self.colour:
            _same_len(f"colour {c.var}", n, c.values)
        _same_len("srgb", n, self.srgb)
        if self.answers is not None:
            _same_len("answers", n, self.answers.mode)
        return self


class ExplorerSectionRef(_View):
    section: str = Field(..., max_length=32)
    view: str = Field(..., max_length=256, description="ui/explore/<section>.json")
    status: Literal["ok", "skipped", "error"] = "ok"
    message_plain: str | None = Field(None, max_length=500)
    reason: SkipReason | None = Field(
        None,
        description="Why a skipped section was skipped (absent in indexes written before "
        "the scientific sections: readers then list every section)",
    )


class ExplorerIndex(_View):
    """``ui/explore/index.json``: what an explore run produced (and why a section is
    missing)."""

    format: Literal["mapi.explorer-index/v1"] = EXPLORER_INDEX_FORMAT
    target: Literal["dataset", "model"]
    ref: str = Field(..., max_length=512, description="dataset://… or model://…")
    node: NodePath | None = Field(
        None,
        description="M4: the node of a tree dataset explored (absent for a flat dataset)",
        exclude_if=lambda v: v is None,
    )
    sections: list[ExplorerSectionRef] = Field(default_factory=list, max_length=32)
    shapes: list[ShapeKind] | None = Field(
        None, max_length=64, description="Data Explorer: the detected data shapes, best first"
    )
    order: list[str] | None = Field(
        None,
        max_length=32,
        description="Data Explorer: the sections in order of relevance for this data",
    )

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "target": "dataset",
                    "ref": "dataset://growth/weekly_sales@v4",
                    "sections": [
                        {"section": "stationarity", "view": "ui/explore/stationarity.json"},
                        {
                            "section": "stl",
                            "view": "ui/explore/stl.json",
                            "status": "skipped",
                            "message_plain": "Less than two seasons of data.",
                            "reason": "insufficient_data",
                        },
                    ],
                },
                {
                    "target": "dataset",
                    "ref": "dataset://seismology/oklahoma_quakes@v1",
                    "shapes": ["image", "timeseries"],
                    "order": [
                        "overview",
                        "structure",
                        "images",
                        "quality",
                        "distributions",
                        "timeseries",
                        "missingness",
                    ],
                    "sections": [
                        {"section": "overview", "view": "ui/explore/overview.json"},
                        {"section": "structure", "view": "ui/explore/structure.json"},
                        {"section": "images", "view": "ui/explore/images.json"},
                        {
                            "section": "map",
                            "view": "ui/explore/map.json",
                            "status": "skipped",
                            "message_plain": "Not available: the data has no polygons.",
                            "reason": "not_applicable",
                        },
                    ],
                },
            ]
        },
    )
