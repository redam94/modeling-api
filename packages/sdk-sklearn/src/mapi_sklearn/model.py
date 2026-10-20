"""``SklearnModel``: scikit-learn and xgboost estimators on labelled data (plan §5).

A plugin author implements only :meth:`SklearnModel.estimator`, returning an allow-listed
estimator (a choice from :data:`~mapi_sklearn.estimators.ESTIMATORS`, a
``{kind, params}`` mapping, or an instance of an allow-listed class). The platform:

1. stacks the ``rows`` dims of the model-stage dataset into samples and builds the design
   matrix from the selected variables and coordinate features (:mod:`mapi_sklearn.design`),
   dropping incomplete rows through the SDK row mask;
2. fits one or more estimators for the plugin's uncertainty kind;
3. persists them with skops or xgboost JSON (never pickle);
4. at score time rebuilds the same columns, handles unseen levels
   (:mod:`mapi_sklearn.levels`), predicts and unstacks back to labelled arrays in the
   plugin's representation:

=================  =====================  ==========================================
base class         representation         ``predictions`` group
=================  =====================  ==========================================
SklearnModel       point                  ``y_hat(*rows)`` (+ ``contributions``)
SklearnQuantile    quantiles              ``y_hat(quantile, *rows)``
SklearnBootstrap   draws                  ``y_hat(chain, draw, *rows)``
SklearnConformal   interval               ``y_hat(bound=[lower, upper, point], *rows)``
SklearnClassifier  point                  ``y_hat(*rows)`` = P(target == positive)
=================  =====================  ==========================================

Forecasting is ``direct`` only: future features must be supplied (or produced by the
pipeline from coordinates); ``forecast_strategy: recursive`` raises
``RECURSIVE_UNSUPPORTED``.
"""

from __future__ import annotations

import functools
import hashlib
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import numpy as np
import xarray as xr
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ScoreContext,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ScoreMode
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mapi_sklearn.contributions import exact_contributions, zeroing_contributions
from mapi_sklearn.design import Design, DimEncoding, Layout, build_design, plan_layout
from mapi_sklearn.estimators import (
    AnyChoice,
    EstimatorEntry,
    build_estimator,
    get_entry,
    resolve_choice,
)
from mapi_sklearn.levels import Expansion, expand_new_levels
from mapi_sklearn.persistence import artifact_name, dump_estimator, load_estimator
from mapi_sklearn.settings import (
    BootstrapSettings,
    ClassifierSettings,
    ConformalSettings,
    PointSettings,
    QuantileSettings,
    SklearnSettings,
)
from mapi_sklearn.uncertainty import (
    bootstrap_samples,
    calibration_mask,
    conformal_quantile,
    quantile_overrides,
    rearrange,
)

__all__ = [
    "MAX_RESIDUALS",
    "SklearnBootstrapModel",
    "SklearnClassifier",
    "SklearnConformalModel",
    "SklearnModel",
    "SklearnQuantileModel",
    "capabilities_for",
    "sklearn_schema_template",
]

Task = Literal["regression", "classification"]
UncertaintyKind = Literal["none", "quantile", "bootstrap", "conformal"]
MAX_RESIDUALS = 5000
SCORE_MODES: frozenset[ScoreMode] = frozenset({"predict", "forecast", "counterfactual", "evaluate"})
_REPRESENTATION: dict[str, Representation] = {
    "none": Representation.POINT,
    "quantile": Representation.QUANTILES,
    "bootstrap": Representation.DRAWS,
    "conformal": Representation.INTERVAL,
}
_INFERENCE: dict[str, Literal["point", "bootstrap", "custom"]] = {
    "none": "point",
    "quantile": "custom",
    "bootstrap": "bootstrap",
    "conformal": "custom",
}


def capabilities_for(kind: UncertaintyKind) -> Capabilities:
    """Capabilities of a plugin with uncertainty ``kind``."""
    return Capabilities(
        inference=_INFERENCE[kind],
        representation=_REPRESENTATION[kind],
        score_modes=SCORE_MODES,
        loo_valid=False,
        loo_invalid_reason="scikit-learn and xgboost estimators provide no pointwise "
        "log-likelihood",
        deterministic_given_seed=True,
    )


def sklearn_schema_template(target_dtype: str = "float64") -> dict[str, Any]:
    """The model input schema template shared by the sklearn plugins.

    Every row dim is declared categorical with policy ``new_levels`` (strategy from
    ``settings.new_levels``) except ``time_dim``, which ``time_kind`` declares as an
    extending ``time``/``integer``/``index`` dim (``time_kind: none``: no such dim). The
    effective policy is the tighter of this and the data's own policy.
    """
    kinds = ("time", "integer", "index")
    return {
        "params": {
            "target": {"type": "string", "default": "y"},
            "rows": {"type": "string_list", "required": True},
            "time_dim": {"type": "string", "default": "time"},
            "time_kind": {
                "type": "string",
                "enum": [*kinds, "none"],
                "default": "time",
            },
            "new_levels": {
                "type": "string",
                "enum": ["pooled", "zero", "error"],
                "default": "pooled",
            },
        },
        "template": {
            "dims": {
                "{*rows as d}": {
                    "kind": "categorical",
                    "policy": "new_levels",
                    "new_level_strategy": "{new_levels}",
                }
            },
            "vars": {
                "{target}": {
                    "dims": "{rows}",
                    "dtype": target_dtype,
                    "role": "target",
                    "missing": "allow",
                }
            },
        },
        "cases": [
            {
                "when": {"time_kind": k},
                "merge": {
                    "dims": {
                        "{time_dim}": {
                            "kind": k,
                            "policy": "extend",
                            "new_level_strategy": "error",
                        }
                    }
                },
            }
            for k in kinds
        ],
    }


class _Learned(BaseModel):
    """What ``fit`` stores in ``FitOutput.learned`` (validated again at score)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    layout: Layout
    estimator: dict[str, JsonValue]
    task: Task
    uncertainty: UncertaintyKind
    models: list[str] = Field(..., min_length=1)
    quantiles: list[float] | None = None
    quantile_mode: Literal["native_multi", "per_level"] | None = None
    rearrange: bool = True
    conformal: dict[str, float | int | bool] | None = None
    classes: list[float] | None = None
    contributions: Literal["exact", "zeroing"] | None = None
    dropped: dict[str, int] = Field(default_factory=dict)

    @property
    def kind(self) -> str:
        return str(self.estimator["kind"])


@dataclass(slots=True)
class _Fitter:
    """Builds and fits estimators for one choice, collecting warnings."""

    entry: EstimatorEntry
    choice: AnyChoice
    layout: Layout
    seed: int
    n_jobs: int
    warnings: list[str] = field(default_factory=list)

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        w: np.ndarray | None,
        overrides: Mapping[str, Any] | None = None,
        offset: int = 0,
    ) -> Any:
        est = build_estimator(
            self.choice,
            seed=self.seed + offset,
            n_jobs=self.n_jobs,
            overrides=overrides,
            categorical=self.layout.categorical,
            n_features=X.shape[1],
        )
        kwargs = {"sample_weight": w} if w is not None else {}
        try:
            from sklearn.utils._param_validation import InvalidParameterError
        except ImportError:  # pragma: no cover - private sklearn module moved
            InvalidParameterError = ValueError
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                est.fit(X, y, **kwargs)
            except InvalidParameterError as exc:
                raise MapiError(
                    f"estimator {self.entry.kind!r} rejected its parameters",
                    code=ErrorCode.SETTINGS_INVALID,
                    pointer="/model/settings/estimator/params",
                    untrusted_text=str(exc)[:4000],
                ) from None
        for wrn in caught:
            msg = f"{wrn.category.__name__}: {wrn.message}"[:500]
            if msg not in self.warnings:
                self.warnings.append(msg)
        return est


@dataclass(slots=True)
class _FitResult:
    models: list[tuple[str, Any]]
    learned: dict[str, Any] = field(default_factory=dict)
    groups: dict[str, xr.Dataset] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def _with_counts(layout: Layout, design: Design) -> Layout:
    """Record how many training rows each fitted level has (used to pool new levels)."""
    encodings: dict[str, DimEncoding] = {}
    for dim, enc in layout.encodings.items():
        if not enc.leveled:
            encodings[dim] = enc
            continue
        cols = layout.columns_of(dim)
        if enc.encoding == "onehot":
            counts = design.X[:, cols].sum(axis=0)
        else:
            codes = design.X[:, cols[0]]
            codes = codes[np.isfinite(codes)].astype(int)
            counts = np.bincount(codes, minlength=len(enc.levels))
        encodings[dim] = enc.model_copy(update={"counts": [int(c) for c in counts]})
    return layout.model_copy(update={"encodings": encodings})


def _diag(
    code: str, severity: Literal["info", "warning"], expert: str, plain: str, **kw: Any
) -> Diagnostic:
    return Diagnostic(
        code=code, severity=severity, message_expert=expert[:2000], message_plain=plain[:2000], **kw
    )


class SklearnModel[SettingsT: SklearnSettings](CustomModel[SettingsT]):
    """Point-prediction plugin base class (representation ``point``).

    Subclass it (or :class:`SklearnQuantileModel`, :class:`SklearnBootstrapModel`,
    :class:`SklearnConformalModel`, :class:`SklearnClassifier`), set ``id`` and
    ``version``, and optionally override :meth:`estimator`.
    """

    Settings: ClassVar[type[BaseModel]] = PointSettings
    task: ClassVar[Task] = "regression"
    uncertainty_kind: ClassVar[UncertaintyKind] = "none"
    capabilities: ClassVar[Capabilities] = capabilities_for("none")
    schema_template: ClassVar[Mapping[str, Any]] = sklearn_schema_template()
    outputs: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "y_hat": {
            "dims": [],
            "dims_of": "y",  # plus the row dims (settings.rows)
            "description": "Prediction of the target over the row dims (settings.rows)",
        },
        "contributions": {
            "dims": ["component"],
            "dims_of": "y",  # plus the row dims (settings.rows)
            "description": "Per-feature contributions (exact for linear estimators, "
            "counterfactual zeroing otherwise), in model space",
        },
    }
    plots: ClassVar[Sequence[str]] = (
        "core.fit_vs_actual",
        "core.residuals_over_time",
        "ml.feature_importance",
    )
    pad_extend: ClassVar[bool] = False

    def __init__(self) -> None:
        self._loaded: dict[str, list[Any]] = {}

    # ------------------------------------------------------------------ author hook
    def estimator(self, s: SettingsT) -> Any:
        """The estimator to fit: an allow-listed choice, ``{kind, params}`` or instance.

        The default returns ``settings.estimator``.
        """
        choice = getattr(s, "estimator", None)
        if choice is None:
            raise MapiError(
                f"model {self.id!r} needs settings.estimator",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/estimator",
            )
        return choice

    # ------------------------------------------------------------------ settings
    def parse_settings(self, raw: Mapping[str, Any] | BaseModel | None) -> SettingsT:
        strategy = (
            raw.get("forecast_strategy")
            if isinstance(raw, Mapping)
            else getattr(raw, "forecast_strategy", None)
        )
        if strategy == "recursive":
            raise MapiError(
                "recursive forecasting is not supported for scikit-learn models in v1; "
                "use forecast_strategy: direct and supply (or derive) future features",
                code=ErrorCode.RECURSIVE_UNSUPPORTED,
                pointer="/model/settings/forecast_strategy",
            )
        return super().parse_settings(raw)

    # ------------------------------------------------------------------ fit
    def fit(self, ds: xr.Dataset, s: SettingsT, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        seed = int(cfg.seed if cfg.seed is not None else ctx.seed)
        choice = resolve_choice(self.estimator(s))
        entry = get_entry(choice.kind)
        if entry.task != self.task:
            raise MapiError(
                f"estimator {entry.kind!r} is a {entry.task} estimator; model {self.id!r} "
                f"needs a {self.task} estimator",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/estimator/kind",
            )
        contributions = self._contribution_method(s, entry)
        layout = plan_layout(ds, s, entry=entry, stage_schema=ctx.stage_schema, log=ctx.info)
        design = build_design(ds, layout, phase="fit", allow_nan=entry.native_nan)
        if design.n < 2:
            raise MapiError(
                f"only {design.n} complete training row(s) after dropping missing values",
                code=ErrorCode.SCHEMA_INVALID,
            )
        layout = _with_counts(layout, design)
        assert design.y is not None
        y = design.y
        classes: list[float] | None = None
        if self.task == "classification":
            y, classes = _binary(y, getattr(s, "positive_class", None))
        fitter = _Fitter(entry, choice, layout, seed, s.n_jobs)
        ctx.check_cancelled()
        result = self._fit_models(fitter, design, y, s, ctx)
        ctx.progress(0.95, "persisting the fitted estimators")
        native: dict[str, bytes] = {}
        keys: list[str] = []
        for i, (role, est) in enumerate(result.models):
            key = artifact_name(entry, role, i)
            native[key] = dump_estimator(entry, est)
            keys.append(key)
        learned = _Learned(
            layout=layout,
            estimator=choice.model_dump(mode="json"),
            task=self.task,
            uncertainty=self.uncertainty_kind,
            models=keys,
            classes=classes,
            contributions=contributions,
            dropped=design.dropped,
            **result.learned,
        )
        groups = dict(result.groups)
        summary = _model_summary([est for _, est in result.models], layout, self.uncertainty_kind)
        if summary is not None:
            groups[summary[0]] = summary[1]
        diagnostics = list(result.diagnostics) + _fit_diagnostics(design, fitter.warnings)
        return FitOutput(
            groups=groups,
            learned=learned.model_dump(mode="json"),
            native=native,
            diagnostics=diagnostics,
        )

    def _contribution_method(
        self, s: SettingsT, entry: EstimatorEntry
    ) -> Literal["exact", "zeroing"] | None:
        method = getattr(s, "contributions", "none")
        if method == "auto":
            return "exact" if entry.linear and self.task == "regression" else None
        if method == "exact":
            if not entry.linear or self.task != "regression":
                raise MapiError(
                    f"exact contributions need a linear regression estimator; {entry.kind!r} "
                    "is not one (use contributions: zeroing)",
                    code=ErrorCode.SETTINGS_INVALID,
                    pointer="/model/settings/contributions",
                )
            return "exact"
        if method == "zeroing":
            return "zeroing"
        return None

    def _fit_models(
        self, fitter: _Fitter, design: Design, y: np.ndarray, s: SettingsT, ctx: FitContext
    ) -> _FitResult:
        """Fit the estimator(s) for this plugin's uncertainty kind (point by default)."""
        return _FitResult([("point", fitter.fit(design.X, y, design.w))])

    # ------------------------------------------------------------------ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        info = _Learned.model_validate(fitted.learned)
        entry = get_entry(info.kind)
        models = self._models(fitted, entry, info)
        layout = info.layout
        design = build_design(ds, layout, phase="score", allow_nan=entry.native_nan)
        expansion = expand_new_levels(
            design,
            layout,
            lambda dim: _strategy(dim, s, ctx),
            native_nan=entry.native_nan,
            max_levels=s.pooled_max_levels,
            log=ctx.info,
        )
        ctx.check_cancelled()
        outputs = self._outputs(models, expansion, info, fitted, req, ctx)
        if info.contributions is not None and (
            not req.output.vars or "contributions" in req.output.vars
        ):
            outputs["contributions"] = self._contributions(models, expansion, info, entry)
        pred = design.unstack(outputs)
        unpredicted = design.n_cells - design.n - design.dropped.get("padding", 0)
        if unpredicted > 0:
            ctx.info(f"{unpredicted} row(s) have missing features or are masked; not predicted")
        for name, attrs in self._attrs(info, expansion).items():
            if name in pred:
                pred[name].attrs.update(attrs)
        return xr.DataTree.from_dict({PREDICTIONS: pred})

    def _predict(self, est: Any, X: np.ndarray) -> np.ndarray:
        """Single-output prediction (class-1 probability for classifiers)."""
        if X.shape[0] == 0:
            return np.zeros(0)
        if self.task == "classification":
            proba = np.asarray(est.predict_proba(X), dtype=float)
            col = [float(c) for c in est.classes_].index(1.0)
            return proba[:, col]
        return np.asarray(est.predict(X), dtype=float)

    def _outputs(
        self,
        models: Sequence[Any],
        exp: Expansion,
        info: _Learned,
        fitted: Fitted,
        req: ScoreRequest,
        ctx: ScoreContext,
    ) -> dict[str, tuple[Sequence[str], Mapping[str, Any], np.ndarray]]:
        point = exp.reduce(self._predict(models[0], exp.X))
        return {"y_hat": ((), {}, point)}

    def _contributions(
        self, models: Sequence[Any], exp: Expansion, info: _Learned, entry: EstimatorEntry
    ) -> tuple[Sequence[str], Mapping[str, Any], np.ndarray]:
        per_model = []
        names: list[str] = []
        for est in models:
            if info.contributions == "exact":
                names, vals = exact_contributions(est, exp.X, info.layout)
            else:
                names, vals = zeroing_contributions(
                    functools.partial(self._predict, est),
                    exp.X,
                    info.layout,
                    native_nan=entry.native_nan,
                )
            per_model.append(exp.reduce(vals))
        if self.uncertainty_kind == "bootstrap":
            arr = np.stack(per_model)[None]
            return (
                ("chain", "draw", "component"),
                {"chain": [0], "draw": np.arange(len(models)), "component": names},
                arr,
            )
        return (("component",), {"component": names}, per_model[0])

    def _attrs(self, info: _Learned, exp: Expansion) -> dict[str, dict[str, Any]]:
        attrs: dict[str, dict[str, Any]] = {"y_hat": {"estimator": info.kind}}
        if exp.pooled:
            attrs["y_hat"]["pooled_new_levels"] = ",".join(exp.pooled)
        if info.contributions is not None:
            attrs["contributions"] = {
                "method": "exact_linear"
                if info.contributions == "exact"
                else "counterfactual_zeroing",
                "sums_to_prediction": "true",
                "space": "model",
            }
        return attrs

    def _models(self, fitted: Fitted, entry: EstimatorEntry, info: _Learned) -> list[Any]:
        missing = [k for k in info.models if k not in fitted.native]
        if missing:
            raise MapiError(
                f"the bundle is missing native model artifacts {missing[:3]}",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        digest = hashlib.sha256()
        for k in info.models:
            digest.update(k.encode() + b"\0" + hashlib.sha256(fitted.native[k]).digest())
        key = digest.hexdigest()
        if key not in self._loaded:
            self._loaded[key] = [
                load_estimator(entry, fitted.native[k], name=k) for k in info.models
            ]
        return self._loaded[key]


# =============================================================================== variants


class SklearnQuantileModel[SettingsT: QuantileSettings](SklearnModel[SettingsT]):
    """Conditional quantiles (representation ``quantiles``).

    xgboost fits one native multi-quantile model (``reg:quantileerror``); quantile-capable
    sklearn estimators (``QuantileRegressor``, ``GradientBoostingRegressor``,
    ``HistGradientBoostingRegressor``) fit one model per level.
    """

    Settings: ClassVar[type[BaseModel]] = QuantileSettings
    uncertainty_kind: ClassVar[UncertaintyKind] = "quantile"
    capabilities: ClassVar[Capabilities] = capabilities_for("quantile")
    outputs: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "y_hat": {
            "dims": ["quantile"],
            "dims_of": "y",  # plus the row dims (settings.rows)
            "description": "Conditional quantiles of the target over the row dims",
        }
    }

    def _contribution_method(
        self, s: SettingsT, entry: EstimatorEntry
    ) -> Literal["exact", "zeroing"] | None:
        return None

    def _fit_models(
        self, fitter: _Fitter, design: Design, y: np.ndarray, s: SettingsT, ctx: FitContext
    ) -> _FitResult:
        qs = list(s.uncertainty.quantiles)
        overrides = quantile_overrides(fitter.entry, fitter.choice, qs)
        models = []
        for k, ov in enumerate(overrides):
            ctx.check_cancelled()
            models.append(("quantile", fitter.fit(design.X, y, design.w, ov)))
            ctx.progress((k + 1) / len(overrides) * 0.9, f"fitted quantile model {k + 1}")
        mode = fitter.entry.quantile
        learned: dict[str, Any] = {
            "quantiles": qs,
            "quantile_mode": mode,
            "rearrange": s.uncertainty.rearrange,
        }
        raw = self._raw_quantiles([m for _, m in models], design.X, mode)
        _, crossed = rearrange(raw, axis=0)
        frac = crossed / max(design.n, 1)
        diag = _diag(
            "QUANTILE_CROSSING",
            "warning" if frac > 0.05 else "info",
            f"{crossed} of {design.n} training rows have crossing quantile predictions "
            f"({frac:.1%}); "
            + (
                "they are sorted (monotone rearrangement)"
                if s.uncertainty.rearrange
                else "left as is"
            ),
            "Some predicted ranges were not in the correct order. The model sorted them."
            if crossed
            else "The predicted quantiles are in the correct order.",
            value=frac,
            threshold=0.05,
        )
        return _FitResult(models, learned=learned, diagnostics=[diag])

    def _raw_quantiles(self, models: Sequence[Any], X: np.ndarray, mode: str | None) -> np.ndarray:
        if mode == "native_multi":
            out = np.asarray(models[0].predict(X), dtype=float) if X.shape[0] else np.zeros((0, 0))
            return out.reshape(X.shape[0], -1).T
        return np.stack([np.asarray(m.predict(X), dtype=float) for m in models])

    def _outputs(
        self,
        models: Sequence[Any],
        exp: Expansion,
        info: _Learned,
        fitted: Fitted,
        req: ScoreRequest,
        ctx: ScoreContext,
    ) -> dict[str, tuple[Sequence[str], Mapping[str, Any], np.ndarray]]:
        qs = info.quantiles or []
        if exp.X.shape[0] == 0:
            raw = np.zeros((len(qs), 0))
        else:
            raw = self._raw_quantiles(models, exp.X, info.quantile_mode)
        if info.rearrange:
            raw, _ = rearrange(raw, axis=0)
        return {"y_hat": (("quantile",), {"quantile": qs}, exp.reduce(raw))}

    def _attrs(self, info: _Learned, exp: Expansion) -> dict[str, dict[str, Any]]:
        attrs = super()._attrs(info, exp)
        attrs["y_hat"]["quantile_method"] = str(info.quantile_mode)
        attrs["y_hat"]["rearranged"] = "true" if info.rearrange else "false"
        return attrs


class SklearnBootstrapModel[SettingsT: BootstrapSettings](SklearnModel[SettingsT]):
    """Bootstrap ensemble (representation ``draws``: ``chain=1``, one draw per replicate).

    ``expected`` outputs are the replicate predictions (uncertainty of the fitted mean);
    ``predictive`` outputs add an out-of-bag residual drawn per cell, approximating the
    predictive distribution. Draws are joint across cells, so every aggregation is valid.
    """

    Settings: ClassVar[type[BaseModel]] = BootstrapSettings
    uncertainty_kind: ClassVar[UncertaintyKind] = "bootstrap"
    capabilities: ClassVar[Capabilities] = capabilities_for("bootstrap")
    outputs: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "y_hat": {
            "dims": ["chain", "draw"],
            "dims_of": "y",  # plus the row dims (settings.rows)
            "description": "Bootstrap draws of the prediction over the row dims",
        },
        "contributions": {
            "dims": ["chain", "draw", "component"],
            "dims_of": "y",  # plus the row dims (settings.rows)
            "description": "Per-replicate feature contributions, in model space",
        },
    }

    def _fit_models(
        self, fitter: _Fitter, design: Design, y: np.ndarray, s: SettingsT, ctx: FitContext
    ) -> _FitResult:
        unc = s.uncertainty
        groups_by = design.row_labels.get(unc.by) if unc.by is not None else None
        samples = bootstrap_samples(design.n, unc.n, seed=fitter.seed, groups=groups_by)
        models: list[tuple[str, Any]] = []
        residuals: list[np.ndarray] = []
        w = design.w
        for b, idx in enumerate(samples):
            ctx.check_cancelled()
            est = fitter.fit(design.X[idx], y[idx], None if w is None else w[idx], offset=b + 1)
            models.append(("bootstrap", est))
            oob = np.setdiff1d(np.arange(design.n), idx, assume_unique=False)
            if oob.size:
                residuals.append(y[oob] - self._predict(est, design.X[oob]))
            ctx.progress((b + 1) / len(samples) * 0.9, f"fitted bootstrap replicate {b + 1}")
        pool = np.concatenate(residuals) if residuals else np.zeros(1)
        if pool.size > MAX_RESIDUALS:
            pool = np.random.default_rng(fitter.seed).choice(pool, MAX_RESIDUALS, replace=False)
        n = len(models)
        groups = {
            "posterior": xr.Dataset(
                {"replicate": (("chain", "draw"), np.arange(n)[None, :])},
                coords={"chain": [0], "draw": np.arange(n)},
                attrs={"inference": "bootstrap", "by": unc.by or "row"},
            ),
            "bootstrap_residuals": xr.Dataset({"residual": (("residual",), np.sort(pool))}),
        }
        diag = _diag(
            "BOOTSTRAP_ENSEMBLE",
            "info",
            f"{n} bootstrap replicates ({'cluster by ' + unc.by if unc.by else 'rows'}); "
            f"out-of-bag residual sd {float(np.std(pool)):.4g} from {pool.size} residuals",
            f"The model was fit again {n} times on resampled data to measure its uncertainty.",
            value=float(np.std(pool)),
        )
        return _FitResult(models, groups=groups, diagnostics=[diag])

    def _outputs(
        self,
        models: Sequence[Any],
        exp: Expansion,
        info: _Learned,
        fitted: Fitted,
        req: ScoreRequest,
        ctx: ScoreContext,
    ) -> dict[str, tuple[Sequence[str], Mapping[str, Any], np.ndarray]]:
        draws = np.stack([exp.reduce(self._predict(m, exp.X)) for m in models])
        if req.output.kind == "predictive" and draws.shape[-1]:
            pool = np.asarray(fitted.group("bootstrap_residuals")["residual"].values)
            rng = np.random.default_rng(ctx.seed + 7919)
            draws = draws + rng.choice(pool, size=draws.shape, replace=True)
        coords = {"chain": [0], "draw": np.arange(len(models))}
        return {"y_hat": (("chain", "draw"), coords, draws[None])}


class SklearnConformalModel[SettingsT: ConformalSettings](SklearnModel[SettingsT]):
    """Split-conformal prediction intervals (representation ``interval``, never draws).

    ``y_hat(bound=[lower, upper, point], ...)`` with attribute ``alpha``: each interval
    covers the observed value with probability at least ``1 - alpha`` for data exchangeable
    with the calibration rows. Intervals have per-cell coverage only and cannot be summed.
    """

    Settings: ClassVar[type[BaseModel]] = ConformalSettings
    uncertainty_kind: ClassVar[UncertaintyKind] = "conformal"
    capabilities: ClassVar[Capabilities] = capabilities_for("conformal")
    outputs: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "y_hat": {
            "dims": ["bound"],
            "dims_of": "y",  # plus the row dims (settings.rows)
            "description": "Split-conformal prediction interval (lower, upper) and point",
        }
    }

    def _contribution_method(
        self, s: SettingsT, entry: EstimatorEntry
    ) -> Literal["exact", "zeroing"] | None:
        return None

    def _fit_models(
        self, fitter: _Fitter, design: Design, y: np.ndarray, s: SettingsT, ctx: FitContext
    ) -> _FitResult:
        unc = s.uncertainty
        times = design.row_labels.get(s.time_dim) if unc.calibration_by == "last" else None
        cal = calibration_mask(
            design.n, unc.calibration, by=unc.calibration_by, seed=fitter.seed, time_labels=times
        )
        w = design.w
        est = fitter.fit(design.X[~cal], y[~cal], None if w is None else w[~cal])
        scores = np.abs(y[cal] - self._predict(est, design.X[cal]))
        radius = conformal_quantile(scores, unc.alpha)
        if unc.refit:
            est = fitter.fit(design.X, y, w)
        learned = {
            "conformal": {
                "alpha": unc.alpha,
                "radius": radius,
                "n_calibration": int(cal.sum()),
                "n_train": int((~cal).sum()),
                "refit": unc.refit,
            }
        }
        diag = _diag(
            "CONFORMAL_CALIBRATION",
            "info",
            f"split conformal: {int(cal.sum())} calibration rows ({unc.calibration_by}), "
            f"radius {radius:.4g} for alpha={unc.alpha}"
            + ("; point model refitted on all rows" if unc.refit else ""),
            f"The prediction ranges have a size that puts about {1 - unc.alpha:.0%} of new "
            "values in them.",
            value=radius,
        )
        return _FitResult([("point", est)], learned=learned, diagnostics=[diag])

    def _outputs(
        self,
        models: Sequence[Any],
        exp: Expansion,
        info: _Learned,
        fitted: Fitted,
        req: ScoreRequest,
        ctx: ScoreContext,
    ) -> dict[str, tuple[Sequence[str], Mapping[str, Any], np.ndarray]]:
        assert info.conformal is not None
        radius = float(info.conformal["radius"])
        point = exp.reduce(self._predict(models[0], exp.X))
        if req.output.kind == "expected":
            ctx.info("conformal intervals are prediction intervals for observed values")
        vals = np.stack([point - radius, point + radius, point])
        return {"y_hat": (("bound",), {"bound": ["lower", "upper", "point"]}, vals)}

    def _attrs(self, info: _Learned, exp: Expansion) -> dict[str, dict[str, Any]]:
        attrs = super()._attrs(info, exp)
        assert info.conformal is not None
        attrs["y_hat"].update(
            {
                "alpha": float(info.conformal["alpha"]),
                "interval": "split_conformal_prediction",
                "conformal_radius": float(info.conformal["radius"]),
            }
        )
        return attrs


class SklearnClassifier[SettingsT: ClassifierSettings](SklearnModel[SettingsT]):
    """Binary classification: ``y_hat`` is the probability of ``positive_class``."""

    Settings: ClassVar[type[BaseModel]] = ClassifierSettings
    task: ClassVar[Task] = "classification"
    outputs: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "y_hat": {
            "dims": [],
            "dims_of": "y",  # plus the row dims (settings.rows)
            "units": "1",
            "description": "Probability that the target equals the positive class",
        },
        "contributions": {
            "dims": ["component"],
            "dims_of": "y",  # plus the row dims (settings.rows)
            "description": "Counterfactual-zeroing contributions to the probability",
        },
    }
    plots: ClassVar[Sequence[str]] = (
        "core.calibration",
        "core.roc_pr",
        "core.confusion",
        "ml.feature_importance",
    )

    def _attrs(self, info: _Learned, exp: Expansion) -> dict[str, dict[str, Any]]:
        attrs = super()._attrs(info, exp)
        assert info.classes is not None
        attrs["y_hat"]["positive_class"] = info.classes[1]
        attrs["y_hat"]["meaning"] = "probability"
        return attrs


# ================================================================================= helpers


def _strategy(dim: str, s: SklearnSettings, ctx: ScoreContext) -> str:
    if ctx.schema is not None and dim in ctx.schema.dims:
        return ctx.strategy(dim)
    return s.new_levels


def _binary(y: np.ndarray, positive: float | None) -> tuple[np.ndarray, list[float]]:
    labels = np.unique(y)
    if labels.size != 2:
        raise MapiError(
            f"binary classification needs exactly two target values; found {labels.size}",
            code=ErrorCode.SCHEMA_INVALID,
        )
    pos = float(labels.max()) if positive is None else float(positive)
    if pos not in labels.tolist():
        raise MapiError(
            f"positive_class {pos!r} is not one of the target values {labels.tolist()}",
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/settings/positive_class",
        )
    neg = float(labels[labels != pos][0])
    return (y == pos).astype(int), [neg, pos]


def _fit_diagnostics(design: Design, warned: Sequence[str]) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    if design.dropped:
        total = sum(design.dropped.values())
        reasons = ", ".join(f"{k}: {v}" for k, v in sorted(design.dropped.items()))
        only_target = set(design.dropped) <= {"missing_target"}
        out.append(
            _diag(
                "ROWS_DROPPED",
                "info" if only_target else "warning",
                f"{total} of {design.n_cells} rows were not used for training ({reasons})",
                f"The training did not use {total} rows because they have missing values.",
                value=float(total),
            )
        )
    for msg in warned[:5]:
        out.append(
            _diag(
                "ESTIMATOR_WARNING",
                "warning",
                msg,
                "The estimator reported a problem during the fit. Do a check of its settings.",
            )
        )
    return out


def _model_summary(
    models: Sequence[Any], layout: Layout, kind: UncertaintyKind
) -> tuple[str, xr.Dataset] | None:
    """Coefficients (linear) or feature importances (trees) as a labelled group."""
    names = layout.feature_names
    feature = {"feature": names}
    first = models[0]
    if hasattr(first, "coef_"):
        coefs = np.stack([np.asarray(m.coef_, dtype=float).reshape(-1) for m in models])
        icpt = np.array(
            [float(np.asarray(getattr(m, "intercept_", 0.0)).reshape(-1)[0]) for m in models]
        )
        if coefs.shape[1] != len(names):
            return None
        if kind == "quantile" and len(models) > 1:
            return "coefficients", xr.Dataset(
                {"coefficient": (("model", "feature"), coefs), "intercept": (("model",), icpt)},
                coords={**feature, "model": np.arange(len(models))},
            )
        return "coefficients", xr.Dataset(
            {
                "coefficient": (("feature",), coefs.mean(axis=0)),
                "intercept": ((), float(icpt.mean())),
            },
            coords=feature,
        )
    if hasattr(first, "feature_importances_"):
        try:
            imps = np.stack([np.asarray(m.feature_importances_, dtype=float) for m in models])
        except (AttributeError, ValueError):  # pragma: no cover - estimator quirk
            return None
        if imps.shape[1] != len(names):
            return None
        return "feature_importance", xr.Dataset(
            {"importance": (("feature",), imps.mean(axis=0))}, coords=feature
        )
    return None
