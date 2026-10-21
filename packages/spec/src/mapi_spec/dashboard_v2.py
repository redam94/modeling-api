"""Client dashboards for any model type: layout 2 (v0.17).

Layout 1 (:class:`~mapi_spec.dashboards.DashboardSpec`) is the media planner: one KPI, the
channel scorecard and fixed tiles. Layout 2 (:class:`DashboardSpecV2`) works for every model
family. The analyst picks a **preset** (:data:`PresetKey`: forecast, regression, classifier,
causal impact, field map, image, MMM planner or generic) and edits its **panels**: KPI
tiles, charts, tables, notes and plot recipes, each bound to a typed **source** (a model
output, a fit metric, a scorecard check, a view or an accuracy metric). Clients always see a
clean readout: the server resolves every panel into a capped **block** of the snapshot
(:data:`PanelBlock`) and the web draws the blocks without plot libraries.

Three inputs never refit the model: **scenario rows** (a client edits analyst-exposed values
within analyst-set ranges and the model scores them), **actuals** (family-appropriate
accuracy over time, or the updated effect of a causal model) and **batch scoring** (a CSV of
new cases; a predictions download and a summary readout).

Rules that keep v0.16 working:

* ``layout_version`` is **required** on layout 2 (no default), so it is always in the
  canonical JSON and a layout 2 hash can never equal a layout 1 hash. A document without
  ``layout_version`` (or with 1) is layout 1 (:func:`parse_dashboard_spec`).
* :class:`~mapi_spec.dashboards.ReadoutSnapshot` only gains optional fields; a stored v0.16
  snapshot reads back with ``schema_version == 1`` and the v0.16 caps.

Everything here is plain pydantic: no numpy, no I/O. The pure helpers that compile presets,
compute metrics and check rows live in ``dashboard_presets``, ``dashboard_metrics``,
``dashboard_inputs`` and ``output_shape``.
"""

from __future__ import annotations

import base64
import binascii
import math
from collections.abc import Mapping
from datetime import date
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, NamedTuple, cast, get_args

from pydantic import (
    AwareDatetime,
    Discriminator,
    Field,
    StringConstraints,
    Tag,
    TypeAdapter,
    field_validator,
    model_validator,
)

import mapi_spec.dashboards as _dashboards
from mapi_spec._dto import RequestModel, ResponseModel, dto_examples
from mapi_spec._types import Name
from mapi_spec.capabilities import OutputMeaning, Representation, ScoreMode
from mapi_spec.dashboards import (
    _DEFAULT_TILES,
    MAX_CHANNELS,
    MAX_SPEC_BYTES,
    MAX_TILES,
    ActualsInputs,
    ChannelDef,
    Dashboard,
    DashboardLimits,
    DashboardSource,
    DashboardSpec,
    KpiDef,
    KpiValue,
    RefreshPolicy,
    Series,
    TileKey,
    TileUnit,
    WhatIfInputs,
    _plain,
)
from mapi_spec.errors import Issue
from mapi_spec.schema import DimPolicy
from mapi_spec.tracking import MetricKey
from mapi_spec.xrjson import XrJsonDataset

__all__ = [
    "ASSET_ID_RE",
    "MAX_BATCH_ROWS",
    "MAX_BLOCK_BYTES",
    "MAX_FIELD_BLOCK_BYTES",
    "MAX_FIELD_SIDE",
    "MAX_KPI_PANELS",
    "MAX_PANELS",
    "MAX_READOUT_BYTES_V2",
    "MAX_ROW_ACTUALS",
    "MAX_SCENARIO_ROWS",
    "MAX_SERIES_POINTS_V2",
    "PANEL_ID_RE",
    "PRESET_RULES",
    "RECIPE_INTENTS",
    "SNAPSHOT_SCHEMA_VERSION",
    "UPGRADE_NOTE",
    "UPGRADE_NOTE_RANGES",
    "AccuracyDetail",
    "AccuracyMetric",
    "AccuracyPoint",
    "AccuracySource",
    "ActualsInputsV2",
    "AnyDashboardSpec",
    "BarRow",
    "BarsBlock",
    "BatchColumn",
    "BatchDef",
    "BatchDownload",
    "BatchForm",
    "BatchStats",
    "BatchSubmit",
    "BatchSummary",
    "BatchTemplate",
    "CalibrationBin",
    "CalibrationBlock",
    "CatalogInput",
    "CatalogMetric",
    "CatalogOutput",
    "CatalogPicture",
    "ChartIntent",
    "ChartPanel",
    "ClassAccuracy",
    "ClassCount",
    "ConfusionBlock",
    "ConfusionMatrix",
    "DashboardLimitsV2",
    "DashboardPresets",
    "DashboardSpecV2",
    "DashboardUpgrade",
    "DimBind",
    "EffectAccuracy",
    "ErrorAccuracy",
    "FieldBlock",
    "FieldFrame",
    "HistogramBin",
    "HorizonDef",
    "InputKey",
    "KpiBlock",
    "KpiPanel",
    "MetricSource",
    "MmmPanel",
    "MmmPlanner",
    "NoteBlock",
    "NotePanel",
    "OutputShape",
    "OutputSource",
    "Panel",
    "PanelBlock",
    "PanelKind",
    "PanelMeta",
    "PictureBlock",
    "PredictionResult",
    "PredictionRow",
    "PredictionValue",
    "PresetKey",
    "PresetRules",
    "PresetSuggestion",
    "RecipePanel",
    "Reduce",
    "ScatterBlock",
    "ScatterPoint",
    "ScenarioForm",
    "ScenarioInputDef",
    "ScenarioRowsDef",
    "ScenarioRowsRequest",
    "ScorecardSource",
    "SeriesBlock",
    "SourceCatalog",
    "SourceRef",
    "SubjectDef",
    "TabKeyV2",
    "TableBlock",
    "TablePanel",
    "UnavailableBlock",
    "VarBind",
    "ViewSource",
    "block_bytes",
    "kpi_of_subject",
    "panel_meta",
    "parse_dashboard_spec",
]

# ===================================================================================== limits

MAX_PANELS: Final = 24
MAX_KPI_PANELS: Final = 8
MAX_READOUT_BYTES_V2: Final = 393_216
"""Cap on a serialised layout 2 snapshot (``schema_version == 2``; 384 KiB)."""
MAX_SERIES_POINTS_V2: Final = 1040
"""Points over the legacy series, the series blocks and the scatter blocks of a v2 snapshot."""
MAX_BLOCK_BYTES: Final = 24_576
"""Budget of one serialised panel block (the server shows a larger block as unavailable)."""
MAX_FIELD_BLOCK_BYTES: Final = 65_536
"""Budget of one serialised ``field`` block."""
MAX_FIELD_SIDE: Final = 64
MAX_FIELD_FRAMES: Final = 8
MAX_BATCH_ROWS: Final = 5000
MAX_ROW_ACTUALS: Final = 5000
MAX_SCENARIO_ROWS: Final = 20
MAX_SCENARIO_INPUTS: Final = 16
MAX_SCENARIO_STEPS: Final = 10_000
MAX_HORIZON: Final = 104
SNAPSHOT_SCHEMA_VERSION: Final = 2
PANEL_ID_RE: Final = r"^[a-z][a-z0-9_]{0,31}$"
ASSET_ID_RE: Final = r"^dsa_[0-9a-z]{26}$"
"""Ids of revision assets (pictures rendered at publish), ``dsa_`` + a ULID."""

UPGRADE_NOTE: Final = "This dashboard has a new layout. The numbers come from the same model."
"""The one-time plain note that clients see after the automatic layout 1 → 2 upgrade."""
UPGRADE_NOTE_RANGES: Final = (
    "The likely ranges now show where one new result will probably be. They are wider than before."
)
"""Added to :data:`UPGRADE_NOTE` when the upgrade changed the ranges (predictive bands)."""

_ShortText = Annotated[str, StringConstraints(max_length=64)]

# ===================================================================================== keys

PresetKey = Literal[
    "mmm_planner", "forecast", "regression", "classifier", "causal_impact", "field_map",
    "image", "generic",
]  # fmt: skip
TabKeyV2 = Literal["overview", "try", "accuracy", "batch", "history"]
"""Sections of a layout 2 readout (layout 1 keeps :data:`~mapi_spec.dashboards.TabKey`)."""
PeriodLabelV2 = Literal["week", "month", "quarter", "year", "day", "period", "case"]
InputKey = Literal["whatif", "scenario_rows", "actuals", "batch"]
PanelKind = Literal["kpi", "chart", "table", "note", "recipe", "mmm"]
ChartIntent = Literal[
    "fan", "line", "bars", "range", "diverging", "scatter", "residuals", "calibration",
    "confusion", "probability_bars", "field", "area_bars", "effect", "accuracy_history",
    "decomposition", "state_sequence",
]  # fmt: skip
AccuracyMetric = Literal[
    "n", "mae", "rmse", "mape", "wape", "smape", "bias", "r2", "band_hit_rate", "accuracy",
    "brier", "log_score", "auc", "effect_total", "effect_relative",
]  # fmt: skip
OutputShape = Literal["grid", "areal", "classification", "time", "categorical", "scalar"]
"""How an output is drawn (``output_shape.classify_table``; parity with ``shape.ts``)."""

RECIPE_INTENTS: Final[Mapping[str, ChartIntent]] = MappingProxyType(
    {
        "core.ppc_fan": "fan",
        "core.fit_vs_actual": "scatter",
        "core.residuals_over_time": "residuals",
        "core.calibration": "calibration",
        "core.confusion": "confusion",
        "core.counterfactual": "diverging",
        "causal.impact": "effect",
        "sci.field_map": "field",
        "sci.area_map": "bars",
        "core.state_sequence": "state_sequence",
        "ml.feature_importance": "bars",
    }
)
"""Platform plot recipes drawn natively (as a chart intent). Any other recipe of a recipe
panel is a **picture**: the model version's fit-time plot rendered to PNG at publish."""


class PresetRules(NamedTuple):
    """What a preset allows: panel kinds, chart intents and inputs."""

    kinds: frozenset[PanelKind]
    intents: frozenset[ChartIntent]
    inputs: frozenset[InputKey]


_BASE_KINDS: Final[frozenset[PanelKind]] = frozenset({"kpi", "chart", "table", "note", "recipe"})


def _rules(intents: str, inputs: str, *, mmm: bool = False) -> PresetRules:
    kinds: frozenset[PanelKind] = _BASE_KINDS | {"mmm"} if mmm else _BASE_KINDS
    return PresetRules(
        kinds,
        cast(frozenset[ChartIntent], frozenset(intents.split())),
        cast(frozenset[InputKey], frozenset(inputs.split())),
    )


PRESET_RULES: Final[Mapping[PresetKey, PresetRules]] = MappingProxyType(
    {
        "mmm_planner": _rules(
            "fan line bars range diverging decomposition accuracy_history scatter residuals",
            "whatif actuals", mmm=True,
        ),
        "forecast": _rules(
            "fan line range bars accuracy_history decomposition state_sequence residuals "
            "scatter diverging",
            "scenario_rows actuals",
        ),
        "regression": _rules(
            "range bars scatter residuals accuracy_history line fan decomposition",
            "scenario_rows actuals batch",
        ),
        "classifier": _rules(
            "probability_bars bars calibration confusion accuracy_history range line",
            "scenario_rows actuals batch",
        ),
        "causal_impact": _rules(
            "effect diverging bars line fan range accuracy_history scatter", "actuals"
        ),
        "field_map": _rules("field area_bars bars line fan range", "scenario_rows actuals"),
        "image": _rules("bars probability_bars confusion accuracy_history line", "batch"),
        "generic": _rules(" ".join(get_args(ChartIntent)), "scenario_rows actuals batch"),
    }
)  # fmt: skip
"""The kinds, intents and inputs each preset allows (validated by :class:`DashboardSpecV2`;
``dashboard_presets.PRESETS`` adds the default panels and lead templates). Image dashboards
allow batch for signed-in analysts only: clients and share links never get it (v0.17)."""

if set(PRESET_RULES) != set(get_args(PresetKey)):  # pragma: no cover - import-time contract
    raise RuntimeError("PRESET_RULES and PresetKey disagree")
if not set(RECIPE_INTENTS.values()) <= set(get_args(ChartIntent)):  # pragma: no cover
    raise RuntimeError("RECIPE_INTENTS names an unknown chart intent")


def _line(v: str | None) -> str | None:
    return _plain(v, multiline=False)


def _text(v: str | None) -> str | None:
    return _plain(v, multiline=True)


# ==================================================================================== subject


class SubjectDef(RequestModel):
    """What the dashboard is about (the main output and how to show its numbers).

    ``positive_label`` is the class a classifier reports the chance of; ``value_per_unit``
    (currency per unit of a count) is for the MMM planner only."""

    variable: Name
    label: str = Field(..., min_length=1, max_length=60)
    unit: TileUnit
    currency: str | None = Field(None, pattern=r"^[A-Z]{3}$", description="ISO 4217 code")
    decimals: int = Field(0, ge=0, le=6)
    positive_label: str | None = Field(None, min_length=1, max_length=64)
    value_per_unit: float | None = Field(None, gt=0)

    model_config = dto_examples(
        {"variable": "y_hat", "label": "Sales", "unit": "currency", "currency": "EUR"},
        {"variable": "y_hat", "label": "Chance of churn", "unit": "probability",
         "positive_label": "churned"},
    )  # fmt: skip

    _plain_line = field_validator("label", "positive_label")(_line)

    @model_validator(mode="after")
    def _currency(self) -> SubjectDef:
        if (self.unit == "currency") != (self.currency is not None):
            raise ValueError("give a currency exactly when the unit is currency")
        return self


def kpi_of_subject(subject: SubjectDef) -> KpiDef:
    """The layout 1 :class:`~mapi_spec.dashboards.KpiDef` of a subject (``ClientDashboard.kpi``
    stays required): probability → percent, rate → ratio, number/label/index → count."""
    unit_map: dict[str, Literal["count", "currency", "ratio", "percent"]] = {
        "count": "count", "currency": "currency", "ratio": "ratio", "percent": "percent",
        "probability": "percent", "rate": "ratio", "number": "count", "label": "count",
        "index": "count",
    }  # fmt: skip
    return KpiDef(
        variable=subject.variable,
        label=subject.label,
        unit=unit_map[subject.unit],
        currency=subject.currency,
        decimals=min(subject.decimals, 4),
        value_per_unit=subject.value_per_unit,
    )


# ==================================================================================== sources


class OutputSource(RequestModel):
    """A model output (its score summary table; names as the dataset names them).
    ``select`` fixes coordinates (``{"store": "s1"}``) before any reduction."""

    kind: Literal["output"] = "output"
    variable: Name
    select: dict[Name, str | int] = Field(default_factory=dict, max_length=8)


class MetricSource(RequestModel):
    """A metric logged by the fit run (``labels`` pick one row, e.g. ``{"unit": "s1"}``;
    the empty labels are the run's scalar value)."""

    kind: Literal["metric"] = "metric"
    key: MetricKey
    labels: dict[str, _ShortText] = Field(default_factory=dict, max_length=4)
    run: Literal["fit"] = "fit"


class ScorecardSource(RequestModel):
    """One check of the model version's scorecard."""

    kind: Literal["scorecard"] = "scorecard"
    check: str = Field(..., min_length=1, max_length=64)


class ViewSource(RequestModel):
    """A guided view of the readout's run: the forecast total over the horizon, the
    counterfactual delta, or the conditioning of a forecast on actuals."""

    kind: Literal["view"] = "view"
    view: Literal["forecast_total", "counterfactual_delta", "conditioning"]


class AccuracySource(RequestModel):
    """An accuracy metric of the readout's actuals (or its history over readouts)."""

    kind: Literal["accuracy"] = "accuracy"
    metric: AccuracyMetric


SourceRef = Annotated[
    OutputSource | MetricSource | ScorecardSource | ViewSource | AccuracySource,
    Field(discriminator="kind"),
]


class Reduce(RequestModel):
    """How an output becomes the panel's numbers.

    ``select``: the cells of ``source.select``; ``last``/``next``: the last fitted or the
    next forecast period; ``mean``/``sum`` over dims ``over`` (draws only, asked from the
    score run so the range is the range of the aggregate; point outputs are allowed and
    flagged ``approximate``); ``share_above``: the share of cells above ``threshold``;
    ``top_class``: the most probable class. The server never adds interval bounds."""

    how: Literal["none", "select", "last", "next", "mean", "sum", "share_above", "top_class"] = (
        "none"
    )
    over: list[Name] = Field(default_factory=list, max_length=4)
    threshold: float | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Reduce:
        if (self.how == "share_above") != (self.threshold is not None):
            raise ValueError("give a threshold exactly when how is share_above")
        if self.threshold is not None and not math.isfinite(self.threshold):
            raise ValueError("threshold must be a finite number")
        if self.how in ("mean", "sum") and not self.over:
            raise ValueError(f"how={self.how!r} needs the dims to reduce over")
        if self.how in ("none", "select", "last", "next") and self.over:
            raise ValueError(f"how={self.how!r} does not take dims to reduce over")
        if len(set(self.over)) != len(self.over):
            raise ValueError("the dims to reduce over must be unique")
        return self


# ===================================================================================== panels


class _PanelBase(RequestModel):
    id: str = Field(..., pattern=PANEL_ID_RE)
    title: str = Field(..., min_length=1, max_length=80)
    caption: str | None = Field(None, max_length=200)

    _plain_line = field_validator("title", "caption")(_line)


class KpiPanel(_PanelBase):
    """A KPI tile. ``stat``: the centre or a bound of an output (``value`` for metrics,
    checks and accuracy); ``band``: ``outcome`` (the range of one new result, scored as
    predictive), ``mean`` (the range of the average) or ``none``; ``compare``: the change
    against the previous published readout; ``good``: which direction is good news."""

    kind: Literal["kpi"] = "kpi"
    source: SourceRef
    stat: Literal["centre", "low", "high", "value"] = "centre"
    reduce: Reduce = Field(default_factory=Reduce)
    band: Literal["outcome", "mean", "none"] = "outcome"
    compare: Literal["previous", "none"] = "previous"
    good: Literal["up", "down", "none"] = "up"

    @model_validator(mode="after")
    def _source(self) -> KpiPanel:
        if self.source.kind != "output" and self.reduce.how != "none":
            raise ValueError("only an output source can be reduced")
        return self


class ChartPanel(_PanelBase):
    """A native chart (``intent``) of an output or of the accuracy history."""

    kind: Literal["chart"] = "chart"
    intent: ChartIntent
    source: Annotated[OutputSource | AccuracySource, Field(discriminator="kind")]
    reduce: Reduce = Field(default_factory=Reduce)
    band: Literal["outcome", "mean", "none"] = "outcome"
    facet: Name | None = None
    max_items: int = Field(20, ge=1, le=60)

    @model_validator(mode="after")
    def _source(self) -> ChartPanel:
        if (self.intent == "accuracy_history") != (self.source.kind == "accuracy"):
            raise ValueError("an accuracy_history chart takes an accuracy source (and only it)")
        if self.source.kind != "output" and self.reduce.how != "none":
            raise ValueError("only an output source can be reduced")
        return self


class TablePanel(_PanelBase):
    """A table of an output (``columns``: dims and stats, in order)."""

    kind: Literal["table"] = "table"
    source: OutputSource
    columns: list[_ShortText] = Field(default_factory=list, max_length=12)
    max_rows: int = Field(20, ge=1, le=50)


class NotePanel(_PanelBase):
    """Plain text from the analyst (rendered as text, never HTML or markdown)."""

    kind: Literal["note"] = "note"
    text: str = Field(..., min_length=1, max_length=1200)

    _plain_text = field_validator("text")(_text)


class RecipePanel(_PanelBase):
    """A platform plot recipe: drawn natively when the recipe is in :data:`RECIPE_INTENTS`,
    else a **picture** of the model version's plot ``plot_id`` (rendered once at publish)."""

    kind: Literal["recipe"] = "recipe"
    recipe: str = Field(..., pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$", max_length=128)
    output: Name | None = None
    plot_id: str | None = Field(None, min_length=1, max_length=128)

    @property
    def is_picture(self) -> bool:
        return self.recipe not in RECIPE_INTENTS

    @model_validator(mode="after")
    def _picture(self) -> RecipePanel:
        if self.is_picture and self.plot_id is None:
            raise ValueError(f"recipe {self.recipe!r} is drawn as a picture: give its plot_id")
        return self


class MmmPanel(_PanelBase):
    """A part of the media planner (``mmm_planner`` preset only): the layout 1 tiles, the
    channel scorecard, the changes against the expectation, the return trend or the saved
    scenarios."""

    kind: Literal["mmm"] = "mmm"
    part: Literal["tiles", "scorecard", "changes", "roi_trend", "scenarios"]


Panel = Annotated[
    KpiPanel | ChartPanel | TablePanel | NotePanel | RecipePanel | MmmPanel,
    Field(discriminator="kind"),
]


# ===================================================================================== inputs


class VarBind(RequestModel):
    """The input sets an input variable (a driver column)."""

    var: Name


class DimBind(RequestModel):
    """The input picks a coordinate of a dim (a store, a period)."""

    dim: Name


ScalarValue = bool | float | Annotated[str, StringConstraints(max_length=128)]


def _finite_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


class ScenarioInputDef(RequestModel):
    """One value a client may edit in a scenario row (or a batch column, when ranges are
    enforced). ``number``/``integer`` need ``min`` < ``max`` (and at most
    :data:`MAX_SCENARIO_STEPS` steps); ``choice`` needs ``choices``; ``date`` needs
    ``date_min``/``date_max``. ``applies``: per row, or for the forecast horizon."""

    name: Name
    label: str = Field(..., min_length=1, max_length=60)
    help: str | None = Field(None, max_length=200)
    bind: VarBind | DimBind
    type: Literal["number", "integer", "choice", "date", "toggle"]
    min: float | None = None
    max: float | None = None
    step: float | None = Field(None, gt=0)
    choices: list[_ShortText] = Field(default_factory=list, max_length=200)
    date_min: date | None = None
    date_max: date | None = None
    default: ScalarValue
    applies: Literal["row", "horizon"] = "row"

    _plain_line = field_validator("label", "help")(_line)

    @field_validator("choices")
    @classmethod
    def _choices(cls, v: list[str]) -> list[str]:
        for c in v:
            _plain(c, multiline=False)
        if len(set(v)) != len(v):
            raise ValueError("choices must be unique")
        return v

    @model_validator(mode="after")
    def _typed(self) -> ScenarioInputDef:
        numeric = self.type in ("number", "integer")
        if not numeric and (self.min, self.max, self.step) != (None, None, None):
            raise ValueError(f"a {self.type} input takes no min, max or step")
        if self.type != "choice" and self.choices:
            raise ValueError(f"a {self.type} input takes no choices")
        if self.type != "date" and (self.date_min, self.date_max) != (None, None):
            raise ValueError(f"a {self.type} input takes no date range")
        if numeric:
            self._check_numeric()
        elif self.type == "choice":
            if not self.choices:
                raise ValueError("a choice input needs choices")
            if not isinstance(self.default, str) or self.default not in self.choices:
                raise ValueError("the default must be one of the choices")
        elif self.type == "date":
            if self.date_min is None or self.date_max is None or self.date_min > self.date_max:
                raise ValueError("a date input needs date_min <= date_max")
            try:
                day = date.fromisoformat(str(self.default))
            except ValueError:
                raise ValueError("the default of a date input must be an ISO date") from None
            if not isinstance(self.default, str) or not self.date_min <= day <= self.date_max:
                raise ValueError("the default date must be in [date_min, date_max]")
        elif not isinstance(self.default, bool):
            raise ValueError("the default of a toggle must be true or false")
        return self

    def _check_numeric(self) -> None:
        lo, hi = self.min, self.max
        if lo is None or hi is None or not math.isfinite(lo) or not math.isfinite(hi):
            raise ValueError(f"a {self.type} input needs finite min and max")
        if lo >= hi:
            raise ValueError("min must be smaller than max")
        step = self.step if self.step is not None else (1.0 if self.type == "integer" else None)
        if step is not None:
            if not math.isfinite(step) or step > hi - lo:
                raise ValueError("step must be smaller than the range")
            if (hi - lo) / step > MAX_SCENARIO_STEPS:
                raise ValueError(f"the range holds more than {MAX_SCENARIO_STEPS} steps")
        if not _finite_number(self.default):
            raise ValueError(f"the default of a {self.type} input must be a finite number")
        assert isinstance(self.default, int | float)
        if not lo <= self.default <= hi:
            raise ValueError("the default must be in [min, max]")
        if self.type == "integer":
            for v in (lo, hi, step, self.default):
                if v is not None and not float(v).is_integer():
                    raise ValueError("an integer input needs integer min, max, step and default")


class HorizonDef(RequestModel):
    """How many periods ahead a forecast scenario row reaches."""

    dim: Name
    min: int = Field(1, ge=1, le=MAX_HORIZON)
    max: int = Field(..., ge=1, le=MAX_HORIZON)
    default: int = Field(..., ge=1, le=MAX_HORIZON)

    @model_validator(mode="after")
    def _range(self) -> HorizonDef:
        if not self.min <= self.default <= self.max:
            raise ValueError("the horizon needs min <= default <= max")
        return self


class ScenarioRowsDef(RequestModel):
    """Scenario rows ("Try a case"): clients edit ``inputs`` (at most ``max_rows`` rows per
    request); ``fixed`` values are sent with every row and never shown; the model scores
    ``outputs`` with their uncertainty."""

    enabled: bool = False
    max_rows: int = Field(5, ge=1, le=MAX_SCENARIO_ROWS)
    outputs: list[Name] = Field(default_factory=list, max_length=4)
    inputs: list[ScenarioInputDef] = Field(default_factory=list, max_length=MAX_SCENARIO_INPUTS)
    fixed: dict[Name, ScalarValue] = Field(default_factory=dict, max_length=64)
    horizon: HorizonDef | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ScenarioRowsDef:
        names = [i.name for i in self.inputs]
        if len(set(names)) != len(names):
            raise ValueError("scenario input names must be unique")
        if set(names) & set(self.fixed):
            raise ValueError("a scenario input cannot also be a fixed value")
        if len(set(self.outputs)) != len(self.outputs):
            raise ValueError("scenario outputs must be unique")
        for k, v in self.fixed.items():
            if isinstance(v, float) and not math.isfinite(v):
                raise ValueError(f"fixed value {k!r} must be a finite number")
        if any(i.applies == "horizon" for i in self.inputs) and self.horizon is None:
            raise ValueError("inputs that apply to the horizon need a horizon")
        if self.enabled:
            if not self.outputs:
                raise ValueError("scenario rows need 1 to 4 outputs")
            if not self.inputs and self.horizon is None:
                raise ValueError("scenario rows need at least one input or a horizon")
        return self


class ActualsInputsV2(ActualsInputs):
    """Actuals for any model type. ``match="time"``: per period, as layout 1;
    ``match="row"``: per case, joined on ``id_column`` (the outcome is ``label_column`` or
    the target). ``threshold``: the probability above which a classifier says "positive"
    (the analyst sets it; clients cannot change it)."""

    match: Literal["time", "row"] = "time"
    id_column: Name | None = None
    label_column: Name | None = None
    threshold: float = Field(0.5, ge=0.05, le=0.95)

    @model_validator(mode="after")
    def _row(self) -> ActualsInputsV2:
        if self.enabled and self.match == "row" and self.id_column is None:
            raise ValueError("actuals matched by row need an id_column")
        if self.match == "time" and (self.id_column, self.label_column) != (None, None):
            raise ValueError("id_column and label_column are for actuals matched by row")
        return self


class BatchDef(RequestModel):
    """Batch scoring: a CSV of up to ``max_rows`` new cases (one ``id_column`` plus the
    driver columns); with ``enforce_ranges`` every cell is checked like a scenario row."""

    enabled: bool = False
    outputs: list[Name] = Field(..., min_length=1, max_length=4)
    max_rows: int = Field(2000, ge=1, le=MAX_BATCH_ROWS)
    id_column: Name
    enforce_ranges: bool = True

    @field_validator("outputs")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("batch outputs must be unique")
        return v


class DashboardLimitsV2(DashboardLimits):
    batch_runs_per_day: int = Field(3, ge=0, le=50)
    """Batch scores per dashboard per 24 h (also counted in ``exact_runs_per_day``)."""


class MmmPlanner(RequestModel):
    """The media planner part of the ``mmm_planner`` preset (layout 1's channels, what-ifs
    and tiles)."""

    channels: list[ChannelDef] = Field(..., min_length=1, max_length=MAX_CHANNELS)
    whatif: WhatIfInputs = Field(default_factory=WhatIfInputs)
    tiles: list[TileKey] = Field(default_factory=lambda: list(_DEFAULT_TILES), max_length=MAX_TILES)

    @model_validator(mode="after")
    def _unique(self) -> MmmPlanner:
        names = [c.name for c in self.channels]
        if len(set(names)) != len(names):
            raise ValueError("channel names must be unique")
        if len(set(self.tiles)) != len(self.tiles) or "panel" in self.tiles:
            raise ValueError("tiles must be unique layout 1 tiles")
        return self


# ======================================================================================= spec

_SPEC_V2_EXAMPLE: Final[dict[str, Any]] = {
    "layout_version": 2,
    "preset": "forecast",
    "title": "Monthly sales forecast",
    "client_name": "Northwind Outdoor",
    "summary": "The sales that we expect for the next months.",
    "period_label": "month",
    "source": {"kind": "endpoint", "alias": "sales-forecast"},
    "subject": {"variable": "y_hat", "label": "Sales", "unit": "currency", "currency": "EUR"},
    "panels": [
        {"kind": "kpi", "id": "next", "title": "Sales next month",
         "source": {"kind": "output", "variable": "y_hat"}, "reduce": {"how": "next"}},
        {"kind": "kpi", "id": "typical_error", "title": "Typical error",
         "source": {"kind": "accuracy", "metric": "mae"}, "stat": "value", "good": "down"},
        {"kind": "chart", "id": "fan", "title": "Sales and forecast", "intent": "fan",
         "source": {"kind": "output", "variable": "y_hat"}},
        {"kind": "chart", "id": "history", "title": "Accuracy over time",
         "intent": "accuracy_history", "source": {"kind": "accuracy", "metric": "mape"}},
        {"kind": "note", "id": "about", "title": "About these numbers",
         "text": "The forecast starts from the newest month that you sent."},
    ],
    "scenario_rows": {
        "enabled": True, "max_rows": 3, "outputs": ["y_hat"],
        "inputs": [{"name": "price", "label": "Average price", "bind": {"var": "price"},
                    "type": "number", "min": 10, "max": 30, "step": 0.5, "default": 19.5,
                    "applies": "horizon"}],
        "horizon": {"dim": "date", "min": 1, "max": 12, "default": 6},
    },
    "actuals": {"enabled": True, "publish": "auto"},
    "refresh": {"follow_endpoint": True, "horizon": 6},
    "limits": {"exact_runs_per_day": 20},
}  # fmt: skip


class DashboardSpecV2(RequestModel):
    """What a layout 2 dashboard shows (the stored draft and each published revision).

    ``layout_version`` is required (never a default: the hash rule). Panels are at most
    :data:`MAX_PANELS` (at most :data:`MAX_KPI_PANELS` KPI tiles) with unique ids, of the
    kinds and intents the preset allows (:data:`PRESET_RULES`); ``mmm`` is given exactly for
    the ``mmm_planner`` preset. Text fields are plain text. The serialised spec holds at most
    :data:`~mapi_spec.dashboards.MAX_SPEC_BYTES`."""

    layout_version: Literal[2]
    preset: PresetKey
    title: str = Field(..., min_length=1, max_length=120)
    client_name: str | None = Field(None, max_length=80)
    summary: str | None = Field(None, max_length=600)
    note: str | None = Field(None, max_length=2000)
    period_label: PeriodLabelV2 = "period"
    source: DashboardSource
    subject: SubjectDef
    panels: list[Panel] = Field(..., min_length=1, max_length=MAX_PANELS)
    mmm: MmmPlanner | None = None
    scenario_rows: ScenarioRowsDef = Field(default_factory=ScenarioRowsDef)
    actuals: ActualsInputsV2 = Field(default_factory=ActualsInputsV2)
    batch: BatchDef | None = None
    refresh: RefreshPolicy = Field(default_factory=RefreshPolicy)
    limits: DashboardLimitsV2 = Field(default_factory=DashboardLimitsV2)
    show_technical: bool = False

    model_config = dto_examples(_SPEC_V2_EXAMPLE)

    _plain_line = field_validator("title", "client_name")(_line)
    _plain_text = field_validator("summary", "note")(_text)

    def enabled_inputs(self) -> set[InputKey]:
        """The inputs this spec turns on (before any share link narrows them)."""
        out: set[InputKey] = set()
        if self.mmm is not None and self.mmm.whatif.enabled:
            out.add("whatif")
        if self.scenario_rows.enabled:
            out.add("scenario_rows")
        if self.actuals.enabled:
            out.add("actuals")
        if self.batch is not None and self.batch.enabled:
            out.add("batch")
        return out

    @model_validator(mode="after")
    def _consistent(self) -> DashboardSpecV2:
        ids = [p.id for p in self.panels]
        if len(set(ids)) != len(ids):
            raise ValueError("panel ids must be unique")
        kpis = sum(1 for p in self.panels if p.kind == "kpi")
        if kpis > MAX_KPI_PANELS:
            raise ValueError(f"a dashboard shows at most {MAX_KPI_PANELS} KPI panels")
        rules = PRESET_RULES[self.preset]
        for i, p in enumerate(self.panels):
            if p.kind not in rules.kinds:
                raise ValueError(f"panel {i} ({p.id}): kind {p.kind!r} is not in {self.preset}")
            if isinstance(p, ChartPanel) and p.intent not in rules.intents:
                raise ValueError(
                    f"panel {i} ({p.id}): a {p.intent!r} chart is not in {self.preset}"
                )
        if (self.preset == "mmm_planner") != (self.mmm is not None):
            raise ValueError("give mmm exactly for the mmm_planner preset")
        blocked = self.enabled_inputs() - rules.inputs
        if blocked:
            raise ValueError(f"the {self.preset} preset does not allow {sorted(blocked)}")
        if self.refresh.follow_endpoint and self.source.kind != "endpoint":
            raise ValueError("follow_endpoint needs an endpoint source")
        size = len(self.model_dump_json().encode("utf-8"))
        if size > MAX_SPEC_BYTES:
            raise ValueError(f"the dashboard spec is {size} bytes (max {MAX_SPEC_BYTES})")
        return self


def _layout(v: Any) -> str:
    lv = v.get("layout_version", 1) if isinstance(v, dict) else getattr(v, "layout_version", 1)
    return "2" if lv == 2 else "1"


AnyDashboardSpec = Annotated[
    Annotated[DashboardSpec, Tag("1")] | Annotated[DashboardSpecV2, Tag("2")],
    Discriminator(_layout),
]
"""A dashboard spec of either layout: ``layout_version`` 2 is :class:`DashboardSpecV2`, a
missing ``layout_version`` (or 1) is :class:`~mapi_spec.dashboards.DashboardSpec`."""

_ANY_SPEC: Final[TypeAdapter[DashboardSpec | DashboardSpecV2]] = TypeAdapter(AnyDashboardSpec)


def parse_dashboard_spec(obj: object) -> DashboardSpec | DashboardSpecV2:
    """Validate a stored or submitted spec (a mapping, a model, or JSON text/bytes) as
    either layout. Use it everywhere instead of ``DashboardSpec.model_validate``."""
    if isinstance(obj, str | bytes | bytearray):
        return _ANY_SPEC.validate_json(obj)
    return _ANY_SPEC.validate_python(obj)


# ============================================================================ snapshot blocks


class _BlockBase(ResponseModel):
    panel_id: str = Field(..., pattern=PANEL_ID_RE)
    title: str = Field(..., max_length=80)
    caption: str | None = Field(None, max_length=200)
    approximate: bool = False


class KpiBlock(_BlockBase):
    kind: Literal["kpi"] = "kpi"
    tile: KpiValue


class SeriesBlock(_BlockBase):
    """Lines or fans over time or categories (``split_at``: where the forecast starts)."""

    kind: Literal["series"] = "series"
    intent: ChartIntent
    series: list[Series] = Field(default_factory=list, max_length=12)
    split_at: str | None = Field(None, max_length=32)
    x_kind: Literal["time", "category"] = "time"


class BarRow(ResponseModel):
    label: str = Field(..., max_length=64)
    value: float | None = None
    low: float | None = None
    high: float | None = None
    tag: str | None = Field(None, max_length=32)


class BarsBlock(_BlockBase):
    kind: Literal["bars"] = "bars"
    intent: ChartIntent = "bars"
    rows: list[BarRow] = Field(default_factory=list, max_length=60)


class ScatterPoint(ResponseModel):
    x: float
    y: float


class ScatterBlock(_BlockBase):
    kind: Literal["scatter"] = "scatter"
    intent: ChartIntent = "scatter"
    x_label: str | None = Field(None, max_length=80)
    y_label: str | None = Field(None, max_length=80)
    points: list[ScatterPoint] = Field(default_factory=list, max_length=520)


class CalibrationBin(ResponseModel):
    p_mean: float = Field(..., ge=0, le=1)
    observed: float = Field(..., ge=0, le=1)
    n: int = Field(..., ge=0)


class CalibrationBlock(_BlockBase):
    kind: Literal["calibration"] = "calibration"
    bins: list[CalibrationBin] = Field(default_factory=list, max_length=20)


class ConfusionMatrix(ResponseModel):
    """``counts[i][j]``: cases of actual class ``labels[i]`` predicted as ``labels[j]``."""

    labels: list[_ShortText] = Field(default_factory=list, max_length=20)
    counts: list[list[int]] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _square(self) -> ConfusionMatrix:
        n = len(self.labels)
        if len(self.counts) != n or any(len(r) != n for r in self.counts):
            raise ValueError("counts must be a square matrix with one row per label")
        if any(c < 0 for r in self.counts for c in r):
            raise ValueError("counts must be >= 0")
        return self


class ConfusionBlock(_BlockBase, ConfusionMatrix):
    kind: Literal["confusion"] = "confusion"


class FieldFrame(ResponseModel):
    """One frame of a field: ``width * height`` uint8 codes (row-major), base64 encoded."""

    label: str = Field(..., max_length=64)
    codes_b64: str = Field(..., max_length=8192)


class FieldBlock(_BlockBase):
    """A 2-D field map (at most 64 × 64 cells, 8 frames). Code ``c`` maps to
    ``vmin + c / 255 * (vmax - vmin)`` (on log10 when ``log``)."""

    kind: Literal["field"] = "field"
    width: int = Field(..., ge=1, le=MAX_FIELD_SIDE)
    height: int = Field(..., ge=1, le=MAX_FIELD_SIDE)
    frames: list[FieldFrame] = Field(..., min_length=1, max_length=MAX_FIELD_FRAMES)
    vmin: float
    vmax: float
    log: bool = False
    colormap: Literal["viridis", "magma", "cividis", "gray", "diverging"] = "viridis"
    units: str | None = Field(None, max_length=64)
    x_label: str | None = Field(None, max_length=64)
    y_label: str | None = Field(None, max_length=64)

    @model_validator(mode="after")
    def _codes(self) -> FieldBlock:
        if not (math.isfinite(self.vmin) and math.isfinite(self.vmax)) or self.vmin > self.vmax:
            raise ValueError("vmin and vmax must be finite with vmin <= vmax")
        for f in self.frames:
            try:
                raw = base64.b64decode(f.codes_b64, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"frame {f.label!r}: codes_b64 is not base64") from None
            if len(raw) != self.width * self.height:
                raise ValueError(f"frame {f.label!r} holds {len(raw)} codes, not width*height")
        return self


TableCell = Annotated[str, StringConstraints(max_length=64)] | float | None


class TableBlock(_BlockBase):
    kind: Literal["table"] = "table"
    columns: list[_ShortText] = Field(default_factory=list, max_length=12)
    rows: list[list[TableCell]] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _cells(self) -> TableBlock:
        for r in self.rows:
            if len(r) != len(self.columns):
                raise ValueError("every row needs one cell per column")
            for c in r:
                if isinstance(c, str):
                    _plain(c, multiline=False)
        return self


class NoteBlock(_BlockBase):
    kind: Literal["note"] = "note"
    text: str = Field(..., max_length=1200)


class PictureBlock(_BlockBase):
    """A revision asset (PNG) served same-origin: ``…/assets/{asset_id}``."""

    kind: Literal["picture"] = "picture"
    asset_id: str = Field(..., pattern=ASSET_ID_RE)
    alt: str = Field(..., max_length=300)
    width: int = Field(..., ge=1, le=4096)
    height: int = Field(..., ge=1, le=4096)


class UnavailableBlock(_BlockBase):
    """A panel whose source was missing at materialise (one panel never fails a readout)."""

    kind: Literal["unavailable"] = "unavailable"
    reason: str = Field(..., max_length=200)


PanelBlock = Annotated[
    KpiBlock | SeriesBlock | BarsBlock | ScatterBlock | CalibrationBlock | ConfusionBlock
    | FieldBlock | TableBlock | NoteBlock | PictureBlock | UnavailableBlock,
    Field(discriminator="kind"),
]  # fmt: skip

_BLOCK_ADAPTER: Final[TypeAdapter[Any]] = TypeAdapter(PanelBlock)


def block_bytes(block: _BlockBase) -> int:
    """Serialised size of a panel block (compare with :data:`MAX_BLOCK_BYTES`, or
    :data:`MAX_FIELD_BLOCK_BYTES` for a field)."""
    return len(block.model_dump_json().encode("utf-8"))


def block_budget(block: _BlockBase) -> int:
    return MAX_FIELD_BLOCK_BYTES if isinstance(block, FieldBlock) else MAX_BLOCK_BYTES


# =================================================================================== accuracy


class ErrorAccuracy(ResponseModel):
    """Errors of numeric predictions (``mape``, ``wape``, ``smape`` and ``band_hit_rate`` in
    percent; ``bias`` = mean of predicted − actual)."""

    kind: Literal["error"] = "error"
    n: int = Field(0, ge=0)
    mae: float | None = None
    rmse: float | None = None
    mape: float | None = None
    wape: float | None = None
    smape: float | None = None
    bias: float | None = None
    r2: float | None = None
    band_hit_rate: float | None = None


class ClassAccuracy(ResponseModel):
    """How right a classifier was (``threshold`` and ``positive`` for a binary one;
    ``rate_pred``/``rate_obs`` the predicted and observed share of ``positive``)."""

    kind: Literal["classification"] = "classification"
    n: int = Field(0, ge=0)
    positive: str | None = Field(None, max_length=64)
    threshold: float | None = Field(None, ge=0, le=1)
    accuracy: float | None = None
    brier: float | None = None
    log_score: float | None = None
    auc: float | None = None
    rate_pred: float | None = None
    rate_obs: float | None = None
    bins: list[CalibrationBin] = Field(default_factory=list, max_length=20)
    confusion: ConfusionMatrix | None = None


class EffectAccuracy(ResponseModel):
    """The updated effect of a causal model: Σ actual − the counterfactual, with its range."""

    kind: Literal["effect"] = "effect"
    n_periods: int = Field(0, ge=0)
    total: float | None = None
    low: float | None = None
    high: float | None = None
    relative: float | None = None
    note: str | None = Field(None, max_length=200)


AccuracyDetail = Annotated[
    ErrorAccuracy | ClassAccuracy | EffectAccuracy, Field(discriminator="kind")
]


class AccuracyPoint(ResponseModel):
    """One published actuals readout in the accuracy history."""

    seq: int = Field(..., ge=1)
    created_at: AwareDatetime
    period: tuple[date, date] | None = None
    n: int = Field(0, ge=0)
    values: dict[AccuracyMetric, float | None] = Field(default_factory=dict, max_length=16)


# ============================================================================ prediction/batch


class PredictionValue(ResponseModel):
    """One output of one scenario row: its centre and range (``label``/``probability``:
    the most probable class of a classifier)."""

    centre: float | None = None
    low: float | None = None
    high: float | None = None
    label: str | None = Field(None, max_length=64)
    probability: float | None = Field(None, ge=0, le=1)


class PredictionRow(ResponseModel):
    inputs: dict[Name, ScalarValue | None] = Field(default_factory=dict, max_length=64)
    outputs: dict[Name, PredictionValue] = Field(default_factory=dict, max_length=4)


class PredictionResult(ResponseModel):
    """The result of scenario rows. ``band``: the kind of range (``outcome``: one new result;
    ``mean``: the average; ``none``)."""

    rows: list[PredictionRow] = Field(default_factory=list, max_length=MAX_SCENARIO_ROWS)
    band: Literal["outcome", "mean", "none"] = "outcome"
    approximate: bool = False


class BatchStats(ResponseModel):
    """The spread of the row centres of one output (never an interval)."""

    mean: float | None = None
    min: float | None = None
    p10: float | None = None
    p50: float | None = None
    p90: float | None = None
    max: float | None = None


class ClassCount(ResponseModel):
    label: str = Field(..., max_length=64)
    count: int = Field(..., ge=0)


class HistogramBin(ResponseModel):
    lo: float
    hi: float
    count: int = Field(..., ge=0)


class BatchDownload(ResponseModel):
    """Whether ``…/readouts/{seq}/predictions.csv`` can be downloaded (by its owner)."""

    available: bool = False
    rows: int = Field(0, ge=0)
    bytes_estimate: int = Field(0, ge=0)


class BatchSummary(ResponseModel):
    n_rows: int = Field(..., ge=0)
    n_scored: int = Field(..., ge=0)
    outputs: list[Name] = Field(default_factory=list, max_length=4)
    stats: dict[Name, BatchStats] = Field(default_factory=dict, max_length=4)
    share_positive: float | None = Field(None, ge=0, le=1)
    class_counts: list[ClassCount] = Field(default_factory=list, max_length=20)
    histogram: list[HistogramBin] = Field(default_factory=list, max_length=20)
    preview: list[dict[str, ScalarValue | None]] = Field(default_factory=list, max_length=20)
    download: BatchDownload = Field(default_factory=BatchDownload)


# ============================================================================== client forms


class PanelMeta(ResponseModel):
    """A panel as a client page lays it out (no sources, no model internals)."""

    id: str = Field(..., pattern=PANEL_ID_RE)
    kind: PanelKind
    title: str = Field(..., max_length=80)
    caption: str | None = Field(None, max_length=200)
    intent: ChartIntent | None = None


def panel_meta(
    panel: KpiPanel | ChartPanel | TablePanel | NotePanel | RecipePanel | MmmPanel,
) -> PanelMeta:
    """The client view of one panel."""
    intent: ChartIntent | None = None
    if isinstance(panel, ChartPanel):
        intent = panel.intent
    elif isinstance(panel, RecipePanel):
        intent = RECIPE_INTENTS.get(panel.recipe)
    return PanelMeta(
        id=panel.id, kind=panel.kind, title=panel.title, caption=panel.caption, intent=intent
    )


class ScenarioForm(ResponseModel):
    """The "Try a case" form a client fills in (the revision's scenario input defs)."""

    max_rows: int = Field(..., ge=1, le=MAX_SCENARIO_ROWS)
    outputs: list[Name] = Field(default_factory=list, max_length=4)
    inputs: list[ScenarioInputDef] = Field(default_factory=list, max_length=MAX_SCENARIO_INPUTS)
    horizon: HorizonDef | None = None


class BatchForm(ResponseModel):
    """The "Score a file" form: the columns a file needs and its row cap."""

    max_rows: int = Field(..., ge=1, le=MAX_BATCH_ROWS)
    id_column: Name
    columns: list[Name] = Field(default_factory=list, max_length=64)
    outputs: list[Name] = Field(default_factory=list, max_length=4)


# ================================================================================= link to v1

_dashboards._link_v2(globals())
"""The layout 1 DTOs that carry layout 2 types (``Dashboard.draft_spec``,
``ReadoutSnapshot.panels``, ``ClientDashboard.subject``, ...) are complete from here on."""


# =========================================================================== HTTP: requests


class ScenarioRowsRequest(RequestModel):
    """``POST …/predictions``: up to 20 rows of the scenario inputs (names as the form's
    ``inputs``; missing values take the defaults). The server checks every value against
    the published revision's defs and refuses (never clips) a value out of range."""

    rows: list[dict[Name, ScalarValue | None]] = Field(
        ..., min_length=1, max_length=MAX_SCENARIO_ROWS
    )
    label: str | None = Field(None, max_length=80)

    model_config = dto_examples(
        {"rows": [{"price": 18.0}, {"price": 22.5}], "label": "Two prices"},
        {"rows": [{"store": "s_new", "promo": True, "week": "2026-11-01"}]},
    )

    _plain_line = field_validator("label")(_line)

    @field_validator("rows")
    @classmethod
    def _rows(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for i, row in enumerate(v):
            if len(row) > 64:
                raise ValueError(f"row {i} holds more than 64 values")
            for k, x in row.items():
                if isinstance(x, float) and not math.isfinite(x):
                    raise ValueError(f"row {i}: {k!r} must be a finite number")
                if isinstance(x, str) and len(x) > 128:
                    raise ValueError(f"row {i}: {k!r} holds more than 128 characters")
        return v


class BatchColumn(ResponseModel):
    """One column of a batch file (``input`` names the scenario input that checks it)."""

    name: Name
    role: Literal["id", "driver"]
    type: Literal["number", "integer", "choice", "date", "toggle", "text"] = "number"
    required: bool = True
    min: float | None = None
    max: float | None = None
    choices: list[_ShortText] = Field(default_factory=list, max_length=200)


class BatchTemplate(ResponseModel):
    """``GET …/batch/template``: the columns of a batch file, an example CSV and the cap."""

    columns: list[BatchColumn] = Field(..., min_length=1, max_length=64)
    id_column: Name
    example_csv: str = Field(..., max_length=65_536)
    max_rows: int = Field(..., ge=1, le=MAX_BATCH_ROWS)

    model_config = dto_examples(
        {
            "columns": [
                {"name": "case_id", "role": "id", "type": "text"},
                {"name": "tenure", "role": "driver", "type": "number", "min": 0, "max": 120},
                {"name": "plan", "role": "driver", "type": "choice",
                 "choices": ["basic", "plus"]},
            ],
            "id_column": "case_id",
            "example_csv": "case_id,tenure,plan\nc1,12,basic\nc2,40,plus\n",
            "max_rows": 2000,
        }
    )  # fmt: skip


class BatchSubmit(RequestModel):
    """``POST …/batch``: the file as inline XrJSON (parsed in the browser from CSV), one row
    per case along the id column."""

    inputs: XrJsonDataset
    label: str | None = Field(None, max_length=80)

    model_config = dto_examples(
        {
            "inputs": {
                "coords": {"case_id": {"dims": ["case_id"], "data": ["c1", "c2"]}},
                "data_vars": {
                    "tenure": {"dims": ["case_id"], "data": [12.0, 40.0]},
                    "plan": {"dims": ["case_id"], "data": ["basic", "plus"]},
                },
            },
            "label": "October leads",
        }
    )

    _plain_line = field_validator("label")(_line)


# =================================================================== HTTP: catalog, presets


class CatalogOutput(ResponseModel):
    """One output a panel can bind: its summary table ``name`` (the score summary names it
    as the manifest does; only the dim columns take the dataset names), dims, shape and
    representation, and what may be asked of it.

    ``centre`` is the centre column the readout selector gives (draws ``mean``, quantiles
    ``median``, interval ``point``, point ``mean``); ``predictive_ok`` is false when a
    predictive score would not give an outcome range (point outputs, which refuse
    ``kind: predictive``, and outputs that ignore it, e.g. ``sci.lgcp_grid`` ``total``):
    the panel then falls back to the ``mean`` band with a caption."""

    name: Name
    manifest_name: Name
    dims: list[Name] = Field(default_factory=list, max_length=16)
    time_dim: Name | None = None
    shape: OutputShape
    representation: Representation
    meaning: OutputMeaning | None = None
    classes: list[_ShortText] = Field(default_factory=list, max_length=50)
    centre: Literal["mean", "median", "q50", "p50", "point"] = "mean"
    stats: list[Literal["centre", "low", "high", "value"]] = Field(
        default_factory=list, max_length=4
    )
    reduces: list[
        Literal["none", "select", "last", "next", "mean", "sum", "share_above", "top_class"]
    ] = Field(default_factory=list, max_length=8)
    intents: list[ChartIntent] = Field(default_factory=list, max_length=16)
    predictive_ok: bool = False
    units: str | None = Field(None, max_length=64)


class CatalogMetric(ResponseModel):
    """A metric logged by the fit run (``labels`` ``{}`` is the scalar value)."""

    key: MetricKey
    labels: dict[str, _ShortText] = Field(default_factory=dict, max_length=4)
    last: float | None = None


class CatalogPicture(ResponseModel):
    """An embeddable plot of the model version (platform recipe provenance only)."""

    plot_id: str = Field(..., max_length=128)
    recipe: str = Field(..., max_length=128)
    title: str = Field(..., max_length=200)


class CatalogInput(ResponseModel):
    """An input a scenario row or batch column can bind (a variable or a dim), with its
    declared bounds and the fit data's range (``profile_min``/``profile_max``: defaults
    that the analyst must confirm)."""

    var: Name | None = None
    dim: Name | None = None
    kind: Literal["number", "integer", "choice", "date", "toggle", "text"]
    dtype: str | None = Field(None, max_length=32)
    bound_min: float | None = None
    bound_max: float | None = None
    profile_min: float | None = None
    profile_max: float | None = None
    levels: list[_ShortText] = Field(default_factory=list, max_length=200)
    date_min: date | None = None
    date_max: date | None = None
    policy: DimPolicy | None = None

    @model_validator(mode="after")
    def _one(self) -> CatalogInput:
        if (self.var is None) == (self.dim is None):
            raise ValueError("give var or dim")
        return self


class SourceCatalog(ResponseModel):
    """``GET …/sources``: what the draft's model version offers to panels and inputs
    (cached per model version). ``row_policy`` is the policy of ``row_dim`` (``fixed``
    rows: no scenario rows or batch except horizon-only); ``views``: the guided views the
    version's runs write."""

    outputs: list[CatalogOutput] = Field(default_factory=list, max_length=64)
    metrics: list[CatalogMetric] = Field(default_factory=list, max_length=500)
    scorecard: list[_ShortText] = Field(default_factory=list, max_length=64)
    pictures: list[CatalogPicture] = Field(default_factory=list, max_length=64)
    inputs: list[CatalogInput] = Field(default_factory=list, max_length=128)
    score_modes: list[ScoreMode] = Field(default_factory=list, max_length=6)
    conditions_on_actuals: bool = False
    input_kind: Literal["dataset", "tree", "collection"] = "dataset"
    row_dim: Name | None = None
    row_policy: DimPolicy | None = None
    time_dim: Name | None = None
    has_grid: bool = False
    views: list[Literal["forecast", "fields", "counterfactual"]] = Field(
        default_factory=list, max_length=3
    )
    family: str | None = Field(None, max_length=64)
    namespace: str | None = Field(None, max_length=64)

    model_config = dto_examples(
        {
            "outputs": [
                {"name": "y_hat", "manifest_name": "y_hat", "dims": ["geo", "date"],
                 "time_dim": "date", "shape": "time", "representation": "quantiles",
                 "centre": "median", "stats": ["centre", "low", "high"],
                 "reduces": ["none", "select", "last", "next"],
                 "intents": ["fan", "line", "range"], "predictive_ok": True},
            ],
            "metrics": [{"key": "mape", "last": 6.2}, {"key": "coverage_90", "last": 0.88}],
            "inputs": [{"dim": "geo", "kind": "choice", "levels": ["north", "south"],
                        "policy": "fixed"}],
            "score_modes": ["predict", "forecast"],
            "time_dim": "date",
            "views": [],
            "namespace": "ts",
        }
    )  # fmt: skip


class PresetSuggestion(ResponseModel):
    """A preset that fits the model version, why, and which inputs it cannot offer."""

    preset: PresetKey
    reasons: list[str] = Field(default_factory=list, max_length=10)
    blocked_inputs: list[str] = Field(default_factory=list, max_length=10)


class DashboardPresets(ResponseModel):
    """``GET /v1/dashboards:presets``: compatible presets, best first, and the compiled
    spec of the first (a starting draft; nothing is saved)."""

    suggestions: list[PresetSuggestion] = Field(default_factory=list, max_length=8)
    spec: DashboardSpecV2 | None = None
    issues: list[Issue] = Field(default_factory=list, max_length=200)

    model_config = dto_examples(
        {
            "suggestions": [
                {"preset": "forecast", "reasons": ["The model forecasts a time output."]},
                {"preset": "generic", "reasons": ["Any model."],
                 "blocked_inputs": ["batch: the model has no row dim"]},
            ],
            "spec": _SPEC_V2_EXAMPLE,
        }
    )  # fmt: skip


class DashboardUpgrade(ResponseModel):
    """``POST …:upgrade``: the draft converted to layout 2 (saved; publish it as usual),
    what to fix (``issues``) and what changed (``notes``)."""

    dashboard: Dashboard
    issues: list[Issue] = Field(default_factory=list, max_length=200)
    notes: list[str] = Field(default_factory=list, max_length=50)

    model_config = dto_examples(
        {
            "dashboard": {
                **_dashboards._DASHBOARD_EXAMPLE,
                "status": "draft",
                "published_revision": None,
                "draft_spec": _SPEC_V2_EXAMPLE,
            },
            "notes": ["The channel 'price' was a placeholder: the forecast preset shows the "
                      "sales forecast without channels."],
        }
    )  # fmt: skip
