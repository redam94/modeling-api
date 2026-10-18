"""Model capabilities, uncertainty representations and the model manifest.

The :class:`Representation` of a model's output decides which aggregations and summaries
are statistically valid. Those rules live here as data (:data:`REPRESENTATION_RULES`) and
small functions (:func:`can_aggregate`, :func:`summary_allowed`) so the control plane, the
runner summariser and the UI all agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import Field, JsonValue, field_validator, model_validator

from mapi_spec._types import MetricName, Name, SortedFrozenSet, StrictModel
from mapi_spec.about import About
from mapi_spec.refs import UseRef, Version
from mapi_spec.resources import ResourceClass
from mapi_spec.schema import DataSchema, DataSchemaTemplate

__all__ = [
    "REPRESENTATION_RULES",
    "Aggregation",
    "AggregationVerdict",
    "Capabilities",
    "DerivedDecl",
    "Diagnostic",
    "GridDecl",
    "ModelManifest",
    "OutputDecl",
    "OutputSelector",
    "Owned",
    "Representation",
    "RepresentationRules",
    "ResourceHint",
    "ScoreMode",
    "can_aggregate",
    "default_summary",
    "parse_summary",
    "summary_allowed",
]


class Representation(StrEnum):
    """How a model encodes uncertainty in its outputs."""

    DRAWS = "draws"
    """``(chain, draw, *dims)`` joint samples — any aggregation is valid."""
    QUANTILES = "quantiles"
    """``(quantile, *dims)`` marginal quantiles — no cross-cell aggregation."""
    INTERVAL = "interval"
    """``(bound, *dims)`` + alpha, e.g. conformal — no aggregation."""
    POINT = "point"
    """``(*dims)`` point predictions — linear aggregation only, flagged."""
    MOMENTS = "moments"
    """``(moment=[mean, sd], *dims)`` e.g. analytic GP — per-cell only."""


ScoreMode = Literal["predict", "forecast", "counterfactual", "simulate", "filter", "evaluate"]
SCORE_MODES: tuple[ScoreMode, ...] = (
    "predict",
    "forecast",
    "counterfactual",
    "simulate",
    "filter",
    "evaluate",
)
Owned = Literal["scaling", "carryover", "inverse_units", "recursion"]
AggregateHow = Literal["sum", "mean", "min", "max"]
LINEAR_HOWS: frozenset[str] = frozenset({"sum", "mean"})

# ---------------------------------------------------------------------------- summaries

_SUMMARY_RE = re.compile(
    r"^(?:(?P<plain>mean|sd|median|point|interval)|(?P<kind>hdi|eti)_(?P<pct>[1-9][0-9]?)"
    r"|q(?P<q>[1-9][0-9]?|0)(?:_(?P<qfrac>[0-9]{1,3}))?)$"
)


@dataclass(frozen=True, slots=True)
class ParsedSummary:
    """A parsed output summary name (``mean``, ``hdi_90``, ``q97_5`` ...)."""

    kind: Literal["mean", "sd", "median", "point", "interval", "hdi", "eti", "quantile"]
    level: float | None = None  # interval mass in (0,1) or quantile in (0,1)


def parse_summary(name: str) -> ParsedSummary:
    """Parse a summary name. Raises ``ValueError`` for unknown names."""
    m = _SUMMARY_RE.match(name)
    if m is None:
        raise ValueError(
            f"unknown summary {name!r}: use mean, sd, median, point, interval, "
            "hdi_<pct>, eti_<pct> or q<pct>[_<fraction>]"
        )
    if m.group("plain"):
        return ParsedSummary(m.group("plain"))  # type: ignore[arg-type]
    if m.group("kind"):
        return ParsedSummary(m.group("kind"), int(m.group("pct")) / 100)  # type: ignore[arg-type]
    q = float(f"{m.group('q')}.{m.group('qfrac') or '0'}") / 100
    if not 0 < q < 1:
        raise ValueError(f"quantile summary {name!r} must be strictly between 0 and 100")
    return ParsedSummary("quantile", q)


@dataclass(frozen=True, slots=True)
class RepresentationRules:
    """What a representation supports.

    ``aggregate``: ``any`` (all reductions), ``linear`` (sum/mean only, flagged) or ``none``.
    """

    aggregate: Literal["any", "linear", "none"]
    summaries: frozenset[str]
    draws_available: bool
    predictive: bool
    note: str


REPRESENTATION_RULES: dict[Representation, RepresentationRules] = {
    Representation.DRAWS: RepresentationRules(
        "any",
        frozenset({"mean", "sd", "median", "point", "hdi", "eti", "quantile"}),
        True,
        True,
        "Joint draws: every aggregation and summary is valid.",
    ),
    Representation.QUANTILES: RepresentationRules(
        "none",
        frozenset({"median", "quantile", "eti", "point"}),
        False,
        True,
        "Quantiles are marginal per cell; summing quantiles is not a quantile of the sum.",
    ),
    Representation.INTERVAL: RepresentationRules(
        "none",
        frozenset({"interval", "point"}),
        False,
        True,
        "Intervals (e.g. conformal) have per-cell coverage only and cannot be aggregated.",
    ),
    Representation.POINT: RepresentationRules(
        "linear",
        frozenset({"mean", "point"}),
        False,
        False,
        "Point predictions carry no uncertainty; only linear aggregation is meaningful.",
    ),
    Representation.MOMENTS: RepresentationRules(
        "none",
        frozenset({"mean", "sd", "point", "eti"}),
        False,
        True,
        "Per-cell moments lack covariances, so cross-cell aggregation is undefined.",
    ),
}

_DEFAULT_SUMMARY: dict[Representation, tuple[str, ...]] = {
    Representation.DRAWS: ("mean", "hdi_90"),
    Representation.QUANTILES: ("median", "q5", "q95"),
    Representation.INTERVAL: ("point", "interval"),
    Representation.POINT: ("point",),
    Representation.MOMENTS: ("mean", "sd"),
}


def default_summary(rep: Representation) -> list[str]:
    """The default summary list for a representation."""
    return list(_DEFAULT_SUMMARY[rep])


def summary_allowed(rep: Representation, name: str) -> bool:
    """Whether summary ``name`` can be computed from output in representation ``rep``."""
    try:
        parsed = parse_summary(name)
    except ValueError:
        return False
    return parsed.kind in REPRESENTATION_RULES[rep].summaries


@dataclass(frozen=True, slots=True)
class AggregationVerdict:
    """Result of :func:`can_aggregate`. ``flagged`` means allowed but must be labelled."""

    allowed: bool
    flagged: bool = False
    reason: str = ""


def can_aggregate(
    rep: Representation, over_dims: list[str] | tuple[str, ...], how: str = "sum"
) -> AggregationVerdict:
    """Is reducing an output over ``over_dims`` with ``how`` statistically valid?"""
    if not over_dims:
        return AggregationVerdict(True)
    rules = REPRESENTATION_RULES[rep]
    if rules.aggregate == "any":
        return AggregationVerdict(True)
    if rules.aggregate == "linear":
        if how in LINEAR_HOWS:
            return AggregationVerdict(
                True, True, "point predictions aggregated linearly; no uncertainty attached"
            )
        return AggregationVerdict(False, reason=f"{how!r} is not linear; {rules.note}")
    return AggregationVerdict(False, reason=rules.note)


# --------------------------------------------------------------------------- capabilities


CalibrationKind = Literal["business_priors", "lift_tests"]
"""What a ``ModelSpec.calibration`` block may carry for a model (``Capabilities.calibration``)."""


class Capabilities(StrictModel):
    """What a model plugin can do (declared in its manifest)."""

    inference: Literal["mcmc", "vi", "map", "smc", "kalman", "point", "bootstrap", "custom"]
    representation: Representation
    score_modes: SortedFrozenSet[ScoreMode] = Field(..., min_length=1)
    decision: bool = False
    recommend: bool = False
    evidence: bool = False
    fast_score: bool = False
    contribution_scale: Literal["additive", "multiplicative", "none"] = "none"
    loo_valid: bool = True
    loo_invalid_reason: str | None = Field(None, max_length=500)
    identified_quantities: list[Name] = Field(default_factory=list)
    owns: SortedFrozenSet[Owned] = frozenset()
    deterministic_given_seed: bool = True
    checkpointable: bool = False
    calibration: SortedFrozenSet[CalibrationKind] = Field(
        frozenset(),
        description="Calibration data the model uses from a spec's calibration block (M3)",
        exclude_if=lambda v: not v,
    )
    input: Literal["dataset", "tree"] = Field(
        "dataset",
        description="M4: 'tree' models also receive the whole tree dataset (the bound "
        "xr.DataTree) next to the flat pipeline output; 'dataset' models only the flat one",
        exclude_if=lambda v: v == "dataset",
    )
    conditions_on_actuals: bool = Field(
        False,
        description="A forecast can be conditioned on actuals sent with the request (target "
        "values for periods after, or at the end of, the fitted history): the model updates "
        "its state with them without re-estimating its parameters and forecasts from the last "
        "actual (mapi_spec.actuals). Models without it refuse such requests with "
        "ACTUALS_NOT_SUPPORTED instead of ignoring the actuals",
        exclude_if=lambda v: not v,
    )

    @model_validator(mode="after")
    def _check(self) -> Capabilities:
        if not self.loo_valid and not self.loo_invalid_reason:
            raise ValueError("loo_valid=false requires loo_invalid_reason")
        if self.conditions_on_actuals and "forecast" not in self.score_modes:
            raise ValueError("conditions_on_actuals needs the 'forecast' score mode")
        return self


FieldColormap = Literal["viridis", "magma", "cividis", "gray", "diverging"]


class GridDecl(StrictModel):
    """How to draw an output as a 2-D field (an image, a map, a vector field).

    ``y`` and ``x`` are the dims of the raster rows and columns; ``y`` runs top to bottom
    unless ``origin`` is ``lower``. ``frame`` names a third dim with one raster per level
    (years, eras, times); ``component`` names a dim of length two that holds the ``(x, y)``
    components of a vector field. Every other dim of the output is reduced over the
    posterior draws only, never over space.
    """

    y: Name
    x: Name
    frame: Name | None = None
    component: Name | None = None
    origin: Literal["upper", "lower"] = "upper"
    colormap: FieldColormap = "viridis"
    log: bool = Field(False, description="Draw log10 of the value (for counts and intensities)")

    @model_validator(mode="after")
    def _distinct(self) -> GridDecl:
        named = [d for d in (self.y, self.x, self.frame, self.component) if d is not None]
        if len(set(named)) != len(named):
            raise ValueError("the grid dims y, x, frame and component must be different")
        return self


class OutputDecl(StrictModel):
    """An output variable the model writes (e.g. ``y_hat``)."""

    dims: list[Name]
    dims_of: Name | None = Field(
        None,
        description="The output also carries every dim of this input variable (e.g. 'y' for "
        "models whose data dims come from settings, such as the sklearn row dims)",
    )
    units_of: Name | None = Field(None, description="Take units from this input variable")
    units: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=2000)
    grid: GridDecl | None = Field(
        None, description="Draw the output as a 2-D field (Model Explorer 'fields' section)"
    )

    @model_validator(mode="after")
    def _grid_dims(self) -> OutputDecl:
        if self.grid is None or self.dims_of is not None:
            return self
        named = (self.grid.y, self.grid.x, self.grid.frame, self.grid.component)
        missing = [d for d in named if d is not None and d not in self.dims]
        if missing:
            raise ValueError(f"grid dims {missing} are not dims of the output")
        return self

    def full_dims(self, schema: DataSchema | None) -> list[str]:
        """``dims`` plus the dims of ``dims_of`` in the model's resolved input ``schema``."""
        out = list(self.dims)
        if self.dims_of is not None and schema is not None and self.dims_of in schema.vars:
            out += [d for d in schema.vars[self.dims_of].dims if d not in out]
        return out


class DerivedDecl(StrictModel):
    """A derived quantity (contributions, response curves, ...)."""

    name: Name
    dims: list[Name]
    units: str | None = Field(None, max_length=64)
    cache: Literal["fit", "on_demand"] = "on_demand"
    description: str | None = Field(None, max_length=2000)


class ResourceHint(StrictModel):
    """Resource guidance for scheduling."""

    min_class: ResourceClass = ResourceClass.S
    default_class: ResourceClass = ResourceClass.S
    est_fit_seconds: int | None = Field(None, ge=0)
    gpu: bool = False

    @model_validator(mode="after")
    def _check(self) -> ResourceHint:
        if self.default_class < self.min_class:
            raise ValueError("default_class must be >= min_class")
        return self


def check_json_schema(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"not a valid JSON Schema (2020-12): {exc.message}") from None
    return schema


class ModelManifest(StrictModel):
    """Everything the control plane knows about a model plugin version (static JSON)."""

    id: str
    version: str
    capabilities: Capabilities
    schema_template: DataSchemaTemplate
    settings_schema: dict[str, JsonValue] = Field(default_factory=dict)
    fit_schema: dict[str, JsonValue] = Field(default_factory=dict)
    outputs: dict[Name, OutputDecl] = Field(default_factory=dict)
    derived: list[DerivedDecl] = Field(default_factory=list)
    plots: list[str] = Field(default_factory=list)
    resources: ResourceHint = Field(default_factory=ResourceHint)
    artifact_format: str = Field("zarr.zip/v1", max_length=64)
    description: str | None = Field(None, max_length=4000)
    about: About | None = Field(
        None,
        description="M3.5: the plain-language About card (required by the catalog rule and "
        "the contract kit; optional here so older manifests still parse)",
        exclude_if=lambda v: v is None,
    )
    family: str | None = Field(
        None,
        pattern=r"^[a-z][a-z0-9_]{0,31}$",
        description="M3.5: the model family governance policies match (mmm, forecast, "
        "ts_multivariate, tabular, choice, regime); default: the id's namespace",
        exclude_if=lambda v: v is None,
    )

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        UseRef.parse(v)
        if "@" in v:
            raise ValueError("manifest id must not carry a version range")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        Version.parse(v)
        return v

    @field_validator("settings_schema", "fit_schema")
    @classmethod
    def _schemas(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return check_json_schema(v)

    @field_validator("plots")
    @classmethod
    def _plots(cls, v: list[str]) -> list[str]:
        for p in v:
            UseRef.parse(p)
        return v

    @model_validator(mode="after")
    def _check(self) -> ModelManifest:
        names = [d.name for d in self.derived]
        if len(names) != len(set(names)):
            raise ValueError("derived quantity names must be unique")
        return self

    def derived_names(self) -> set[str]:
        return {d.name for d in self.derived}

    @property
    def model_family(self) -> str:
        """``family`` or, when unset, the id's namespace (``ts.sarimax`` -> ``ts``)."""
        return self.family or self.id.split(".", 1)[0]


# ----------------------------------------------------------------------- output selection


class Aggregation(StrictModel):
    """Reduce outputs over dims before summarising (validity depends on representation)."""

    over: list[Name] = Field(..., min_length=1, max_length=16)
    how: AggregateHow = "sum"


class OutputSelector(StrictModel):
    """What a score request returns."""

    kind: Literal["expected", "predictive"] = "expected"
    summary: list[str] | None = Field(
        None, max_length=32, description="Summaries to return; default depends on representation"
    )
    draws: int = Field(0, ge=0, le=10_000)
    thin_index: Literal["bundle"] = "bundle"
    units: Literal["original", "model"] = "original"
    names: Literal["dataset", "canonical"] = "dataset"
    aggregate: Aggregation | None = None
    vars: list[Name] = Field(default_factory=list, max_length=256)

    @field_validator("summary")
    @classmethod
    def _summary(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for name in v:
            parse_summary(name)
        if len(v) != len(set(v)):
            raise ValueError("summary entries must be unique")
        return v

    def summary_for(self, rep: Representation) -> list[str]:
        """The requested summary, or the representation default if none was given."""
        return default_summary(rep) if self.summary is None else list(self.summary)


# ---------------------------------------------------------------------------- diagnostics


class Diagnostic(StrictModel):
    """A diagnostic finding that rolls up into the sanity scorecard."""

    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    severity: Literal["info", "warning", "error"] = "warning"
    metric: MetricName | None = None
    value: float | None = None
    threshold: float | None = None
    message_expert: str = Field(..., max_length=2000)
    message_plain: str = Field(..., max_length=2000)
    suggested_action: str | None = Field(None, max_length=2000)
    var: Name | None = None
    coords: dict[Name, JsonValue] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.severity == "info"
