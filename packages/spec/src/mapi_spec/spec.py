"""Declarative documents: ``ModelSpec``, ``ScoreRequest`` and the wrapper kinds.

Every document has ``apiVersion: mapi/v1`` and a ``kind`` discriminator. All models forbid
unknown fields. See :mod:`mapi_spec.loader` for strict parsing and canonical hashing and
:mod:`mapi_spec.validate` for static validation against a catalog.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from mapi_spec._types import (
    RESERVED_STAGE,
    Duration,
    LabelKey,
    LabelValue,
    MetricName,
    Name,
    Scalar,
    Slug,
    StepId,
    StrictModel,
    unique,
)
from mapi_spec.capabilities import OutputSelector, ScoreMode
from mapi_spec.errors import parse_pointer
from mapi_spec.refs import DatasetRef, ModelTarget, SpecRef, UseRef
from mapi_spec.resources import ResourceClass
from mapi_spec.schema import DimPolicy

__all__ = [
    "API_VERSION",
    "SBC",
    "AnyDocument",
    "AsOfSplit",
    "Backtest",
    "Binding",
    "Campaign",
    "Checkpoint",
    "DataBlock",
    "DocumentKind",
    "EvaluateBlock",
    "ExecutionBlock",
    "FitConfig",
    "FitMode",
    "Horizon",
    "Intervention",
    "KeepConfig",
    "LastPerGroupSplit",
    "LeaveGroupOutSplit",
    "MaskBlockSplit",
    "Metadata",
    "ModelBlock",
    "ModelSpec",
    "OutputsBlock",
    "ScorePolicy",
    "ScoreRequest",
    "SideTable",
    "Split",
    "StepCall",
    "Sweep",
    "TimeHoldoutSplit",
]

API_VERSION = "mapi/v1"
ApiVersion = Literal["mapi/v1"]

MAX_PIPELINE_STEPS = 64


class Metadata(StrictModel):
    """Identity and labels of a document."""

    name: Slug
    project: Slug
    labels: dict[LabelKey, LabelValue] = Field(default_factory=dict, max_length=64)
    description: str | None = Field(None, max_length=4000)


# --------------------------------------------------------------------------------- data


class ScorePolicy(StrEnum):
    """Which side-table version is used at score time."""

    PINNED = "pinned"
    """The exact version used at fit."""
    LATEST = "latest"
    """The latest version at score time."""
    SUPPLIED = "supplied"
    """The score request must supply it."""


class SideTable(StrictModel):
    """A small constant dataset (FX rates, holidays ...) used by steps."""

    ref: DatasetRef
    score_policy: ScorePolicy = ScorePolicy.PINNED


class Binding(StrictModel):
    """Canonical name → dataset name, for dims and variables (``geo: market``).

    Names that are not bound keep their dataset name. The binding is stored in the bundle
    and reversed on every output by default.
    """

    dims: dict[Name, Name] = Field(default_factory=dict, max_length=256)
    vars: dict[Name, Name] = Field(default_factory=dict, max_length=4096)

    @model_validator(mode="after")
    def _check(self) -> Binding:
        both = sorted(set(self.dims) & set(self.vars))
        if both:
            raise ValueError(f"canonical names bound as both dim and variable: {both}")
        targets = list(self.dims.values()) + list(self.vars.values())
        if dups := unique(targets):
            raise ValueError(f"dataset names bound more than once: {dups}")
        return self

    def canonical_of(self) -> dict[str, str]:
        """dataset name → canonical name."""
        return {ds: canon for canon, ds in {**self.dims, **self.vars}.items()}

    def dataset_of(self) -> dict[str, str]:
        """canonical name → dataset name."""
        return {**self.dims, **self.vars}


class _SplitBase(StrictModel):
    after_step: str | None = Field(
        None,
        description="Stage the split happens at (a step id or 'raw'); default: end of the "
        "stateless prefix",
    )
    allow_leakage: bool = Field(
        False, description="Downgrade LEAKAGE_RISK to a warning (acknowledged)"
    )

    def split_dims(self) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def forecast_dim(self) -> str | None:
        """Dim along which the holdout behaves like a forecast (for recursion checks)."""
        return None

    @property
    def forecast_periods(self) -> int | None:
        return None


class TimeHoldoutSplit(_SplitBase):
    """Hold out the end of an ordered dim: ``last`` periods, a ``fraction`` or ``after`` a coord."""

    kind: Literal["time_holdout"]
    dim: Name
    last: int | None = Field(None, ge=1)
    fraction: float | None = Field(None, gt=0, lt=1)
    after: Scalar | None = None

    @model_validator(mode="after")
    def _one(self) -> TimeHoldoutSplit:
        if sum(x is not None for x in (self.last, self.fraction, self.after)) != 1:
            raise ValueError("time_holdout needs exactly one of last, fraction or after")
        return self

    def split_dims(self) -> list[str]:
        return [self.dim]

    @property
    def forecast_dim(self) -> str | None:
        return self.dim

    @property
    def forecast_periods(self) -> int | None:
        return self.last


class LastPerGroupSplit(_SplitBase):
    """Hold out the last ``last`` rows along ``dim`` within each group (``min_len`` kept)."""

    kind: Literal["last_per_group"]
    dim: Name
    within: list[Name] = Field(..., min_length=1, max_length=8)
    last: int = Field(1, ge=1)
    min_len: int = Field(1, ge=1)

    def split_dims(self) -> list[str]:
        return [self.dim, *self.within]

    @property
    def forecast_dim(self) -> str | None:
        return self.dim

    @property
    def forecast_periods(self) -> int | None:
        return self.last


class AsOfSplit(_SplitBase):
    """Train on coords up to and including ``as_of``; hold out the rest (optionally ``gap``)."""

    kind: Literal["as_of"]
    dim: Name
    as_of: Scalar
    gap: int = Field(0, ge=0)

    def split_dims(self) -> list[str]:
        return [self.dim]

    @property
    def forecast_dim(self) -> str | None:
        return self.dim


class LeaveGroupOutSplit(_SplitBase):
    """Hold out whole groups of ``dim`` (explicit ``groups`` or ``n`` random ones)."""

    kind: Literal["leave_group_out"]
    dim: Name
    groups: list[Scalar] | None = Field(None, min_length=1, max_length=10_000)
    n: int | None = Field(None, ge=1)
    seed: int | None = Field(None, ge=0, lt=2**32)

    @model_validator(mode="after")
    def _one(self) -> LeaveGroupOutSplit:
        if (self.groups is None) == (self.n is None):
            raise ValueError("leave_group_out needs exactly one of groups or n")
        return self

    def split_dims(self) -> list[str]:
        return [self.dim]


class MaskBlockSplit(_SplitBase):
    """Hold out random blocks over ``dims`` (block sizes per dim) covering ``fraction``."""

    kind: Literal["mask_block"]
    dims: list[Name] = Field(..., min_length=1, max_length=8)
    fraction: float = Field(..., gt=0, lt=1)
    block: dict[Name, int] = Field(default_factory=dict)
    seed: int | None = Field(None, ge=0, lt=2**32)

    @model_validator(mode="after")
    def _block_dims(self) -> MaskBlockSplit:
        extra = sorted(set(self.block) - set(self.dims))
        if extra:
            raise ValueError(f"block sizes given for dims not in dims: {extra}")
        if any(v < 1 for v in self.block.values()):
            raise ValueError("block sizes must be >= 1")
        return self

    def split_dims(self) -> list[str]:
        return list(self.dims)


Split = Annotated[
    TimeHoldoutSplit | LastPerGroupSplit | AsOfSplit | LeaveGroupOutSplit | MaskBlockSplit,
    Field(discriminator="kind"),
]


class DataBlock(StrictModel):
    """The dataset, side tables, binding to canonical names and the split."""

    dataset: DatasetRef
    side_tables: dict[Name, SideTable] = Field(default_factory=dict, max_length=32)
    bind: Binding = Field(default_factory=Binding)
    split: Split | None = None


# ----------------------------------------------------------------------------- pipeline


class StepCall(StrictModel):
    """One pipeline step: ``{id, use: core.resample@^1, with: {...settings}}``."""

    id: StepId
    use: UseRef
    with_: dict[str, JsonValue] = Field(default_factory=dict, alias="with")

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v == RESERVED_STAGE:
            raise ValueError(f"{RESERVED_STAGE!r} is a reserved stage name")
        return v


# -------------------------------------------------------------------------------- model


class FitMode(StrEnum):
    EVALUATE_THEN_REFIT = "evaluate_then_refit"
    TRAIN_ONLY = "train_only"


class PriorPredictive(StrictModel):
    draws: int = Field(..., ge=1, le=100_000)


class PosteriorPredictiveKeep(StrictModel):
    thin: int = Field(1, ge=1, le=10_000)


class KeepConfig(StrictModel):
    """Optional groups kept in the bundle."""

    log_likelihood: bool = False
    posterior_predictive: PosteriorPredictiveKeep | bool = False
    prior: bool = False


class Checkpoint(StrictModel):
    every_draws: int = Field(..., ge=1, le=1_000_000)


class FitConfig(StrictModel):
    """Platform fit settings. Plugin-specific limits come from the manifest's ``fit_schema``."""

    mode: FitMode = FitMode.EVALUATE_THEN_REFIT
    engine: str | None = Field(None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    tune: int | None = Field(None, ge=0, le=1_000_000)
    draws: int | None = Field(None, ge=1, le=1_000_000)
    chains: int | None = Field(None, ge=1, le=64)
    target_accept: float | None = Field(None, gt=0, lt=1)
    seed: int | None = Field(None, ge=0, lt=2**32)
    prior_predictive: PriorPredictive | None = None
    keep: KeepConfig = Field(default_factory=KeepConfig)
    checkpoint: Checkpoint | None = None


DIM_POLICIES_KEY = "dim_policies"


class ModelBlock(StrictModel):
    """The model plugin, its settings and fit configuration.

    ``settings.dim_policies`` is platform-reserved: a mapping ``dim → policy`` that may only
    *tighten* the plugin's policies. It is removed before plugin settings validation.
    """

    use: UseRef
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    fit: FitConfig = Field(default_factory=FitConfig)

    @field_validator("settings")
    @classmethod
    def _dim_policies(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        dp = v.get(DIM_POLICIES_KEY)
        if dp is None:
            return v
        if not isinstance(dp, dict):
            raise ValueError("settings.dim_policies must be a mapping of dim -> policy")
        for dim, pol in dp.items():
            if not isinstance(pol, str) or pol not in DimPolicy._value2member_map_:
                raise ValueError(
                    f"settings.dim_policies.{dim}: must be one of fixed, extend, new_levels"
                )
        return v

    @property
    def dim_policies(self) -> dict[str, DimPolicy]:
        dp = self.settings.get(DIM_POLICIES_KEY) or {}
        assert isinstance(dp, dict)
        return {k: DimPolicy(str(v)) for k, v in dp.items()}

    @property
    def plugin_settings(self) -> dict[str, JsonValue]:
        """Settings without platform-reserved keys (what the plugin schema validates)."""
        return {k: v for k, v in self.settings.items() if k != DIM_POLICIES_KEY}


class OutputsBlock(StrictModel):
    derived: list[Name] = Field(default_factory=list, max_length=64)
    plots: list[UseRef] = Field(default_factory=list, max_length=64)
    ui_draws: int = Field(300, ge=0, le=2000)


class EvaluateBlock(StrictModel):
    metrics: list[MetricName] = Field(default_factory=list, max_length=32)


class ExecutionBlock(StrictModel):
    resource_class: ResourceClass = ResourceClass.S
    timeout: Duration = Field(default_factory=lambda: Duration(1800))

    @field_validator("timeout")
    @classmethod
    def _timeout(cls, v: Duration) -> Duration:
        if not 60 <= v.seconds <= 24 * 3600:
            raise ValueError("timeout must be between 1m and 24h")
        return v


class _Document(StrictModel):
    api_version: ApiVersion = Field(..., alias="apiVersion")


class ModelSpec(_Document):
    """The declarative modelling file."""

    kind: Literal["ModelSpec"]
    metadata: Metadata
    data: DataBlock
    pipeline: list[StepCall] = Field(default_factory=list, max_length=MAX_PIPELINE_STEPS)
    model: ModelBlock
    outputs: OutputsBlock = Field(default_factory=OutputsBlock)
    evaluate: EvaluateBlock = Field(default_factory=EvaluateBlock)
    execution: ExecutionBlock = Field(default_factory=ExecutionBlock)

    @model_validator(mode="after")
    def _check(self) -> ModelSpec:
        ids = [s.id for s in self.pipeline]
        if dups := unique(ids):
            raise ValueError(f"duplicate step ids: {dups}")
        split = self.data.split
        after = split.after_step if split is not None else None
        if after is not None and after != RESERVED_STAGE and after not in ids:
            raise ValueError(f"data.split.after_step {after!r} is not a step id or 'raw'")
        return self

    @property
    def stage_names(self) -> list[str]:
        return [RESERVED_STAGE, *(s.id for s in self.pipeline)]


# -------------------------------------------------------------------------------- score


class Horizon(StrictModel):
    dim: Name
    periods: int = Field(..., ge=1, le=100_000)


class Intervention(StrictModel):
    """Change ``var`` on the coords selected by ``sel``: exactly one of multiply/add/set."""

    var: Name
    sel: dict[Name, Scalar | list[Scalar]] = Field(default_factory=dict)
    multiply: float | None = None
    add: float | None = None
    set_: float | None = Field(None, alias="set")

    @model_validator(mode="after")
    def _one(self) -> Intervention:
        if sum(x is not None for x in (self.multiply, self.add, self.set_)) != 1:
            raise ValueError("an intervention needs exactly one of multiply, add or set")
        return self


class ScoreRequest(_Document):
    """Score a fitted model with replayed preprocessing."""

    kind: Literal["ScoreRequest"]
    model: ModelTarget
    mode: ScoreMode = "predict"
    horizon: Horizon | None = None
    inputs_stage: str = Field(RESERVED_STAGE, pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    data: DatasetRef | None = None
    scenario: DatasetRef | None = None
    side_tables: dict[Name, DatasetRef] = Field(default_factory=dict, max_length=32)
    select: dict[Name, list[Scalar]] = Field(default_factory=dict, max_length=64)
    interventions: list[Intervention] = Field(default_factory=list, max_length=256)
    output: OutputSelector = Field(default_factory=OutputSelector)
    seed: int | None = Field(None, ge=0, lt=2**32)

    @model_validator(mode="after")
    def _check(self) -> ScoreRequest:
        if self.mode == "forecast" and self.horizon is None:
            raise ValueError("mode 'forecast' needs a horizon")
        if self.mode == "counterfactual" and not self.interventions and self.scenario is None:
            raise ValueError("mode 'counterfactual' needs interventions or a scenario")
        if self.mode in ("filter", "evaluate") and self.data is None:
            raise ValueError(f"mode {self.mode!r} needs data")
        return self


# ----------------------------------------------------------------------------- wrappers


class Sweep(_Document):
    """A grid over spec paths (JSON pointers) of a base revision, compared by a metric."""

    kind: Literal["Sweep"]
    metadata: Metadata
    base: SpecRef
    grid: dict[str, list[JsonValue]] = Field(..., min_length=1, max_length=16)
    compare_by: MetricName
    max_parallel: int | None = Field(None, ge=1, le=1000)
    execution: ExecutionBlock | None = None

    @field_validator("grid")
    @classmethod
    def _grid(cls, v: dict[str, list[JsonValue]]) -> dict[str, list[JsonValue]]:
        total = 1
        for ptr, values in v.items():
            parse_pointer(ptr)
            if not ptr or ptr.startswith(("/apiVersion", "/kind", "/metadata")):
                raise ValueError(f"grid path {ptr!r} may not target identity fields")
            if not values:
                raise ValueError(f"grid path {ptr!r} has no values")
            total *= len(values)
        if total > 1000:
            raise ValueError("a sweep may have at most 1000 points")
        return v

    def points(self) -> list[dict[str, JsonValue]]:
        """Every grid point as ``{pointer: value}`` (cartesian product, stable order)."""
        pts: list[dict[str, JsonValue]] = [{}]
        for ptr, values in self.grid.items():
            pts = [{**p, ptr: val} for p in pts for val in values]
        return pts


class Backtest(_Document):
    """Refit-and-score at several ``as_of`` coords along ``dim``."""

    kind: Literal["Backtest"]
    metadata: Metadata
    base: SpecRef
    dim: Name = "time"
    as_of: list[Scalar] = Field(..., min_length=1, max_length=500)
    horizon: int | None = Field(None, ge=1)
    metrics: list[MetricName] = Field(default_factory=list)
    execution: ExecutionBlock | None = None


class SBC(_Document):
    """Simulation-based calibration: ``n`` simulate→fit→rank runs."""

    kind: Literal["SBC"]
    metadata: Metadata
    base: SpecRef
    n: int = Field(..., ge=1, le=1000)
    seed: int | None = Field(None, ge=0, lt=2**32)
    params: list[Name] = Field(default_factory=list)
    execution: ExecutionBlock | None = None


class Campaign(_Document):
    """A plugin-owned fit→recommend loop (e.g. Bayesian optimisation) in one sandbox run."""

    kind: Literal["Campaign"]
    metadata: Metadata
    base: SpecRef
    rounds: int = Field(..., ge=1, le=1000)
    batch_size: int = Field(1, ge=1, le=1000)
    candidates: DatasetRef | None = None
    seed: int | None = Field(None, ge=0, lt=2**32)
    execution: ExecutionBlock | None = None


AnyDocument = Annotated[
    ModelSpec | ScoreRequest | Sweep | Backtest | SBC | Campaign,
    Field(discriminator="kind"),
]
DocumentKind = Literal["ModelSpec", "ScoreRequest", "Sweep", "Backtest", "SBC", "Campaign"]

DOCUMENT_TYPES: dict[str, type[_Document]] = {
    "ModelSpec": ModelSpec,
    "ScoreRequest": ScoreRequest,
    "Sweep": Sweep,
    "Backtest": Backtest,
    "SBC": SBC,
    "Campaign": Campaign,
}


def document_type(kind: Any) -> type[_Document] | None:
    """The model class for a ``kind`` value, or ``None``."""
    return DOCUMENT_TYPES.get(kind) if isinstance(kind, str) else None
