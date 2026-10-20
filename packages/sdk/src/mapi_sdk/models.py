"""The model plugin contract (plan §5).

A plugin turns a model-stage ``xr.Dataset`` into a :class:`FitOutput` and, later, scores a
canonicalised dataset into an ``xr.DataTree`` whose ``predictions`` group holds the
declared outputs in the plugin's :class:`~mapi_spec.capabilities.Representation`::

    draws      (chain, draw, *dims)
    quantiles  (quantile, *dims)       quantile coords in (0, 1)
    interval   (bound, *dims)          bound coords include "lower", "upper" (+ "point")
    point      (*dims)
    moments    (moment, *dims)         moment coords ["mean", "sd"]

The platform owns everything around that: binding, CoordPlan, context, canonical coords,
inverse transforms, reverse binding and summaries. :class:`CustomModel` is the base class
for hand-written plugins; ``mapi_pymc.PyMCModel`` and ``mapi_sklearn.SklearnModel`` build
on the same protocol.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

import numpy as np
import xarray as xr
from mapi_spec.about import About
from mapi_spec.abouts import MODEL_ABOUTS
from mapi_spec.calibration import Calibration, PriorTranslationJob, PriorTranslationResult
from mapi_spec.capabilities import (
    Capabilities,
    DerivedDecl,
    Diagnostic,
    ModelManifest,
    OutputDecl,
    Representation,
    ResourceHint,
    ScoreMode,
)
from mapi_spec.catalog import ModelCatalogEntry
from mapi_spec.errors import ActualsNotSupported, ErrorCode, MapiError
from mapi_spec.optimize import OptimizeRequest, OptimizeResult
from mapi_spec.schema import DataSchema, DataSchemaTemplate, DimPolicy
from mapi_spec.spec import FitConfig, Horizon, ScoreRequest
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from mapi_sdk.actuals import ForecastActuals
from mapi_sdk.errors import BundleError
from mapi_sdk.steps import CancelToken
from mapi_sdk.tracking import Tracker, TrackingMixin

__all__ = [
    "DIFFERENCE_ATTR",
    "PREDICTIONS",
    "BudgetPlan",
    "CustomModel",
    "FitContext",
    "FitOutput",
    "Fitted",
    "ModelPlugin",
    "ModelSettings",
    "PriorTranslation",
    "ScoreContext",
    "build_manifest",
    "new_level_mask",
]

PREDICTIONS = "predictions"
"""Name of the DataTree group a plugin's ``score`` must fill."""

DIFFERENCE_ATTR = "difference_of"
"""Scored array attr naming a reference output: the output is ``<a level of its units_of
variable> - <reference>`` (the effect ``y - y_hat`` of ``causal.impact``). A difference is
not a level, so it cannot go through the target's inverse transforms on its own; the
harness inverts the level and the reference and subtracts them in the output space (draws
and point outputs)."""

ProgressFn = Callable[[float, str], None]


class ModelSettings(BaseModel):
    """Base for plugin settings: unknown keys are rejected, instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


@dataclass(slots=True)
class FitContext(TrackingMixin):
    """Everything a plugin's ``fit`` may use besides data, settings and fit config.

    ``schema`` is the model's resolved input schema (its template); ``stage_schema`` is the
    schema of the pipeline stage the data comes from, with every variable and its role.

    ``checkpoint_sink`` is an engine-specific sink for segmented fits (for example
    ``mapi_pymc.CheckpointSink``); the runner supplies one that writes to the run prefix.
    ``None`` disables checkpointing.

    ``calibration`` (M3) is the spec's ``calibration`` block - business priors and lift
    tests - for plugins whose capabilities declare them (``Capabilities.calibration``).
    ``to_original`` (M3) maps a model-stage array in the units of input variable ``src``
    back to the output space with the *fitted* preprocessing state (as
    :attr:`ScoreContext.to_original` does for derived quantities), so a plugin can turn
    business-unit calibration data into model units. Both are ``None`` outside the harness.

    ``tree`` (M4) is the whole tree dataset, binding renames applied per node
    (:meth:`~mapi_sdk.binding.BindingMap.apply_tree`), for models whose capabilities declare
    ``input="tree"`` and were fitted from a tree dataset; ``None`` otherwise. ``ds`` is still
    the flat pipeline output (the nodes ``data.nodes`` selects, preprocessed).

    ``tracker`` (v0.11) receives what the plugin logs with the
    :class:`~mapi_sdk.tracking.TrackingMixin` methods (``log_metric``, ``log_params``,
    ``set_tag``, ``log_line``, ``chains``); the harness attaches one that records the
    bundle's ``metrics_history`` group and forwards to the runner. ``None``: validate only.
    """

    seed: int = 0
    schema: DataSchema | None = None
    policies: Mapping[str, DimPolicy] = field(default_factory=dict)
    coords: Mapping[str, np.ndarray] = field(default_factory=dict)
    workdir: Path | None = None
    log: Callable[[str], None] | None = None
    on_progress: ProgressFn | None = None
    cancel: CancelToken | None = None
    checkpoint_sink: Any | None = None
    stage_schema: DataSchema | None = None
    calibration: Calibration | None = None
    to_original: Callable[[xr.DataArray, str], xr.DataArray] | None = None
    tree: xr.DataTree | None = None
    tracker: Tracker | None = None
    rng: np.random.Generator = field(init=False)
    _logged_params: dict[str, Any] = field(init=False, default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    def progress(self, fraction: float, message: str = "") -> None:
        if self.on_progress is not None:
            self.on_progress(min(max(fraction, 0.0), 1.0), message)

    def info(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def check_cancelled(self) -> None:
        if self.cancel is not None:
            self.cancel.check()


@dataclass(slots=True)
class FitOutput:
    """What a plugin's ``fit`` returns. Everything here is persisted in the bundle.

    ``groups`` uses ArviZ group names (``posterior``, ``sample_stats``, ``log_likelihood``,
    ``observed_data``, ``prior``, ...). ``native`` holds safe native formats only (skops,
    xgboost JSON, safetensors) - never pickle. ``learned`` is small JSON state.
    """

    groups: dict[str, xr.Dataset] = field(default_factory=dict)
    learned: dict[str, JsonValue] = field(default_factory=dict)
    native: dict[str, bytes] = field(default_factory=dict)
    terminal_state: xr.Dataset | None = None
    derived: dict[str, xr.Dataset] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)


@dataclass(slots=True)
class PriorTranslation:
    """What a plugin's optional ``translate_priors`` hook returns (M3).

    ``result`` is the ``priors.json`` document (model-space priors, coverage and the
    prior-predictive summaries). ``preview`` holds the prior-predictive draws in business
    units along a ``sample`` dim for plots: ``roi(sample, channel)``,
    ``contribution_share(sample, channel)``, ``baseline_share(sample)`` and
    ``kpi(sample, <time>)`` with ``kpi_observed(<time>)``; the stated ranges are in the
    variables' ``range_low``/``range_high``/``credibility`` attributes.
    """

    result: PriorTranslationResult
    preview: xr.Dataset = field(default_factory=xr.Dataset)


@dataclass(slots=True)
class BudgetPlan:
    """What a plugin's optional ``optimize`` hook returns (M3.5).

    ``result`` is the ``optimize.json`` document. ``plans`` holds the plans compared for
    the ``mmm.budget_plans`` plot: ``spend(plan, channel)`` and ``outcome(sample, plan)``
    (the expected KPI per posterior draw) with ``plan`` coords ``current`` and
    ``optimal``.
    """

    result: OptimizeResult
    plans: xr.Dataset = field(default_factory=xr.Dataset)


@dataclass(slots=True)
class Fitted:
    """A fitted model as ``score`` sees it (rebuilt from the bundle)."""

    manifest: ModelManifest
    settings: dict[str, Any]
    fit_config: FitConfig
    groups: dict[str, xr.Dataset] = field(default_factory=dict)
    learned: dict[str, JsonValue] = field(default_factory=dict)
    native: dict[str, bytes] = field(default_factory=dict)
    terminal_state: xr.Dataset | None = None
    derived: dict[str, xr.Dataset] = field(default_factory=dict)
    schema: DataSchema | None = None
    coords: dict[str, np.ndarray] = field(default_factory=dict)

    def group(self, name: str) -> xr.Dataset:
        try:
            return self.groups[name]
        except KeyError:
            raise BundleError(f"the fitted model has no {name!r} group") from None

    @property
    def posterior(self) -> xr.Dataset:
        return self.group("posterior")

    @property
    def representation(self) -> Representation:
        return self.manifest.capabilities.representation


@dataclass(slots=True)
class ScoreContext(TrackingMixin):
    """Everything a plugin's ``score`` may use besides the fitted model and data.

    The dataset handed to ``score`` is canonicalised: fixed and new-level dims are in fit
    order (padded to the full fit set), new levels are appended and flagged by the bool
    coordinate ``<dim>__is_new``, and ``<dim>__requested`` marks real (non-padding) labels.

    ``to_original`` is set when the harness computes fit-cached derived quantities
    (:meth:`CustomModel.derive`): ``to_original(da, src)`` maps a model-stage array in the
    units of input variable ``src`` (e.g. a contribution in ``y`` units, a spend level in
    ``spend`` units) to the output space - the pipeline inverse back to its last reshape
    step - and labels it with ``units`` and ``output_space`` attributes.

    ``tree`` (M4) is the bound tree dataset the payload came from, for models whose
    capabilities declare ``input="tree"`` (as :attr:`FitContext.tree`); ``None`` when the
    payload is a flat dataset or the model reads datasets only.

    ``tracker`` (v0.11): as :attr:`FitContext.tracker` (score runs log through it too).
    """

    mode: ScoreMode = "predict"
    kind: Literal["expected", "predictive"] = "expected"
    seed: int = 0
    schema: DataSchema | None = None
    policies: Mapping[str, DimPolicy] = field(default_factory=dict)
    horizon: Horizon | None = None
    draw_index: np.ndarray | None = None
    log: Callable[[str], None] | None = None
    cancel: CancelToken | None = None
    to_original: Callable[[xr.DataArray, str], xr.DataArray] | None = None
    tree: xr.DataTree | None = None
    tracker: Tracker | None = None
    rng: np.random.Generator = field(init=False)
    _logged_params: dict[str, Any] = field(init=False, default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    def check_cancelled(self) -> None:
        if self.cancel is not None:
            self.cancel.check()

    def info(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def strategy(self, dim: str) -> str:
        if self.schema is None or dim not in self.schema.dims:
            return "error"
        return self.schema.dims[dim].new_level_strategy


def new_level_mask(ds: xr.Dataset, dim: str) -> xr.DataArray:
    """Bool mask along ``dim``: True for levels unseen at fit (from canonicalisation)."""
    flag = f"{dim}__is_new"
    if flag in ds.coords:
        return ds[flag]
    return xr.zeros_like(ds[dim], dtype=bool)


@runtime_checkable
class ModelPlugin(Protocol):
    """Structural protocol every model plugin satisfies."""

    id: ClassVar[str]
    version: ClassVar[str]

    @classmethod
    def manifest(cls) -> ModelManifest: ...

    def parse_settings(self, raw: Mapping[str, Any] | BaseModel | None) -> BaseModel: ...

    def fit(self, ds: xr.Dataset, s: Any, cfg: FitConfig, ctx: FitContext) -> FitOutput: ...

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree: ...


def build_manifest(
    *,
    id: str,
    version: str,
    capabilities: Capabilities,
    schema_template: DataSchemaTemplate | DataSchema | Mapping[str, Any],
    settings_model: type[BaseModel] | None = None,
    fit_schema: Mapping[str, Any] | None = None,
    outputs: Mapping[str, OutputDecl | Mapping[str, Any]] | None = None,
    derived: Sequence[DerivedDecl | Mapping[str, Any]] = (),
    plots: Sequence[str] = (),
    resources: ResourceHint | None = None,
    artifact_format: str = "zarr.zip/v1",
    description: str | None = None,
    about: About | None = None,
    family: str | None = None,
) -> ModelManifest:
    """Assemble and validate a :class:`~mapi_spec.capabilities.ModelManifest`."""
    if isinstance(schema_template, DataSchema):
        template = DataSchemaTemplate.from_schema(schema_template)
    elif isinstance(schema_template, DataSchemaTemplate):
        template = schema_template
    else:
        template = DataSchemaTemplate.model_validate(dict(schema_template))
    settings_schema = (
        settings_model.model_json_schema(by_alias=True) if settings_model is not None else {}
    )
    return ModelManifest(
        id=id,
        version=version,
        capabilities=capabilities,
        schema_template=template,
        settings_schema=settings_schema,
        fit_schema=dict(fit_schema or {}),
        outputs={k: OutputDecl.model_validate(v) for k, v in (outputs or {}).items()},
        derived=[DerivedDecl.model_validate(d) for d in derived],
        plots=list(plots),
        resources=resources or ResourceHint(),
        artifact_format=artifact_format,
        description=description,
        about=about,
        family=family,
    )


class CustomModel[SettingsT: BaseModel]:
    """Base class for hand-written plugins: declare metadata, implement ``fit``/``score``.

    Optional hooks: :meth:`diagnostics`, :meth:`postprocess`, :meth:`derive`,
    :meth:`objective`, :meth:`recommend`, :meth:`translate_priors`, :meth:`optimize`,
    :meth:`forecast_with_actuals` (with ``capabilities.conditions_on_actuals``). Set
    ``pad_extend = False`` if the model does not index time positionally (scoring then skips
    padding ``extend`` dims to the fit range). List in ``structural_missing`` the canonical
    variables whose NaN are part of the design of the data (not gaps), so that the scorecard
    check ``data.missing`` does not count them.
    """

    id: ClassVar[str]
    version: ClassVar[str]
    Settings: ClassVar[type[BaseModel]] = ModelSettings
    capabilities: ClassVar[Capabilities]
    schema_template: ClassVar[DataSchemaTemplate | DataSchema | Mapping[str, Any]]
    outputs: ClassVar[Mapping[str, OutputDecl | Mapping[str, Any]]] = {}
    derived: ClassVar[Sequence[DerivedDecl | Mapping[str, Any]]] = ()
    plots: ClassVar[Sequence[str]] = ()
    resources: ClassVar[ResourceHint | None] = None
    fit_schema: ClassVar[Mapping[str, Any] | None] = None
    description: ClassVar[str | None] = None
    pad_extend: ClassVar[bool] = True
    structural_missing: ClassVar[Sequence[str]] = ()
    """Canonical variables whose NaN are expected by design (for example cells without a
    sensor). The scorecard check ``data.missing`` does not count their missing values."""
    about: ClassVar[About | None] = None
    """M3.5: the plain-language About card. Built-in models take theirs from
    :data:`mapi_spec.abouts.MODEL_ABOUTS` when this is unset."""
    family: ClassVar[str | None] = None
    """M3.5: the family governance policies match (default: the id's namespace)."""

    @classmethod
    def manifest(cls) -> ModelManifest:
        return build_manifest(
            id=cls.id,
            version=cls.version,
            capabilities=cls.capabilities,
            schema_template=cls.schema_template,
            settings_model=cls.Settings,
            fit_schema=cls.fit_schema,
            outputs=cls.outputs,
            derived=cls.derived,
            plots=cls.plots,
            resources=cls.resources,
            description=cls.description,
            about=cls.about if cls.about is not None else MODEL_ABOUTS.get(cls.id),
            family=cls.family,
        )

    @classmethod
    def catalog_entry(cls) -> ModelCatalogEntry:
        return ModelCatalogEntry(manifest=cls.manifest())

    @classmethod
    def ref(cls) -> str:
        return f"{cls.id}@{cls.version}"

    def parse_settings(self, raw: Mapping[str, Any] | BaseModel | None) -> SettingsT:
        if isinstance(raw, self.Settings):
            return raw  # type: ignore[return-value]
        if isinstance(raw, BaseModel):
            raw = raw.model_dump()
        try:
            return self.Settings.model_validate(dict(raw or {}))  # type: ignore[return-value]
        except ValidationError as exc:
            raise MapiError(
                f"invalid settings for model {self.id!r}",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings",
                untrusted_text=str(exc)[:4000],
            ) from None

    def fit(self, ds: xr.Dataset, s: SettingsT, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        raise NotImplementedError(f"model {self.id!r} does not implement fit")

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        raise NotImplementedError(f"model {self.id!r} does not implement score")

    # optional hooks ---------------------------------------------------------------
    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        return []

    def postprocess(self, out: FitOutput, ds: xr.Dataset, s: SettingsT) -> FitOutput:
        return out

    def derive(self, fitted: Fitted, name: str, ds: xr.Dataset, ctx: ScoreContext) -> xr.Dataset:
        raise NotImplementedError(f"model {self.id!r} cannot derive {name!r}")

    def objective(self, fitted: Fitted, decision: xr.Dataset, ctx: ScoreContext) -> xr.DataArray:
        raise NotImplementedError(f"model {self.id!r} does not declare a decision objective")

    def recommend(
        self, fitted: Fitted, candidates: xr.Dataset, q: int, ctx: ScoreContext
    ) -> xr.Dataset:
        raise NotImplementedError(f"model {self.id!r} does not recommend")

    def forecast_with_actuals(
        self,
        fitted: Fitted,
        ds: xr.Dataset,
        actuals: ForecastActuals,
        req: ScoreRequest,
        ctx: ScoreContext,
    ) -> xr.DataTree:
        """A forecast conditioned on actuals (:mod:`mapi_spec.actuals`): plugins that declare
        ``capabilities.conditions_on_actuals`` override it; the harness calls it instead of
        :meth:`score` whenever a ``forecast`` payload carries actuals.

        ``ds`` is the canonicalised model-stage payload (actuals, periods to forecast and
        their drivers); ``actuals`` says which periods are which per series
        (:class:`~mapi_sdk.actuals.ForecastActuals`). Update the model's state with the
        actuals using the *fitted* parameters - never re-estimate - and return the
        ``predictions`` group over every period of ``ds``, as :meth:`score` does: the
        conditioned multi-step forecast on ``actuals.future``, one-step-ahead predictions
        elsewhere. Supplied actuals inside the fitted range replace the fitted values.

        The default refuses (``ACTUALS_NOT_SUPPORTED``), so a model never ignores actuals.
        """
        raise ActualsNotSupported(
            f"model {self.id!r} cannot update a forecast with actuals",
            pointer="/data",
        )

    def optimize(self, fitted: Fitted, request: OptimizeRequest, ctx: ScoreContext) -> BudgetPlan:
        """Budget optimisation (M3.5, ``POST …:optimize``; plugins that declare
        ``capabilities.decision``): split ``request``'s budget across channels from the
        fitted model's posterior and return the plan with its uncertainty. The runner
        fills in the KPI name and units."""
        raise NotImplementedError(f"model {self.id!r} does not optimise budgets")

    def translate_priors(
        self, ds: xr.Dataset, s: SettingsT, job: PriorTranslationJob, ctx: FitContext
    ) -> PriorTranslation:
        """Business priors to model-space priors over model-stage ``ds`` (M3; plugins that
        declare ``business_priors`` in ``Capabilities.calibration``). ``ctx.to_original``
        carries the data-derived preprocessing scales."""
        raise NotImplementedError(f"model {self.id!r} does not translate business priors")
