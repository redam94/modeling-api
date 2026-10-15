"""The allow-listed estimator registry.

Plugins never import arbitrary classes. An estimator is chosen by ``kind`` from
:data:`ESTIMATORS`, and its hyper-parameters are validated by that kind's Pydantic params
model (``extra="forbid"``, bounded values). The platform then builds the estimator with
every parameter passed explicitly, so behaviour does not drift when a library changes a
default. Parameters the platform owns (seeds, threads, categorical handling and the
loss/objective an uncertainty kind needs) are not part of the params models.

In a spec::

    estimator: {kind: xgboost.XGBRegressor, params: {n_estimators: 600, max_depth: 6}}
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from mapi_spec.errors import ErrorCode, MapiError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "ESTIMATORS",
    "ElasticNetChoice",
    "EstimatorChoice",
    "EstimatorEntry",
    "GradientBoostingRegressorChoice",
    "HistGradientBoostingRegressorChoice",
    "LassoChoice",
    "LinearRegressionChoice",
    "LogisticRegressionChoice",
    "QuantileRegressorChoice",
    "RandomForestClassifierChoice",
    "RandomForestRegressorChoice",
    "RidgeChoice",
    "XGBClassifierChoice",
    "XGBRegressorChoice",
    "build_estimator",
    "choice_from_instance",
    "get_entry",
    "resolve_choice",
]

Task = Literal["regression", "classification"]


class Params(BaseModel):
    """Base for estimator hyper-parameters: unknown keys rejected, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


Fraction = Annotated[float, Field(gt=0, le=1)]
MaxFeatures = Literal["sqrt", "log2"] | Fraction | None


# ----------------------------------------------------------------------------- linear models


class LinearRegressionParams(Params):
    fit_intercept: bool = True
    positive: bool = False


class RidgeParams(Params):
    alpha: float = Field(1.0, ge=0, le=1e12)
    fit_intercept: bool = True
    positive: bool = False
    solver: Literal["auto", "svd", "cholesky", "lsqr", "sparse_cg", "sag", "saga", "lbfgs"] = "auto"
    max_iter: int | None = Field(None, ge=1, le=1_000_000)
    tol: float = Field(1e-4, gt=0, le=1)


class LassoParams(Params):
    alpha: float = Field(1.0, gt=0, le=1e12)
    fit_intercept: bool = True
    positive: bool = False
    max_iter: int = Field(1000, ge=1, le=1_000_000)
    tol: float = Field(1e-4, gt=0, le=1)
    selection: Literal["cyclic", "random"] = "cyclic"


class ElasticNetParams(LassoParams):
    l1_ratio: float = Field(0.5, ge=0, le=1)


class QuantileRegressorParams(Params):
    quantile: float = Field(0.5, gt=0, lt=1)
    alpha: float = Field(1.0, ge=0, le=1e12)
    fit_intercept: bool = True
    solver: Literal["highs", "highs-ds", "highs-ipm"] = "highs"


class LogisticRegressionParams(Params):
    C: float = Field(1.0, gt=0, le=1e12)
    l1_ratio: float = Field(0.0, ge=0, le=1)
    fit_intercept: bool = True
    solver: Literal["lbfgs", "liblinear", "newton-cg", "newton-cholesky", "sag", "saga"] = "lbfgs"
    max_iter: int = Field(100, ge=1, le=100_000)
    tol: float = Field(1e-4, gt=0, le=1)
    class_weight: Literal["balanced"] | None = None


# ------------------------------------------------------------------------------- tree models


class _ForestParams(Params):
    n_estimators: int = Field(100, ge=1, le=5000)
    max_depth: int | None = Field(None, ge=1, le=256)
    min_samples_split: int = Field(2, ge=2, le=1_000_000)
    min_samples_leaf: int = Field(1, ge=1, le=1_000_000)
    bootstrap: bool = True
    max_samples: Fraction | None = None
    min_impurity_decrease: float = Field(0.0, ge=0)
    ccp_alpha: float = Field(0.0, ge=0)


class RandomForestRegressorParams(_ForestParams):
    criterion: Literal["squared_error", "absolute_error", "friedman_mse", "poisson"] = (
        "squared_error"
    )
    max_features: MaxFeatures = 1.0


class RandomForestClassifierParams(_ForestParams):
    criterion: Literal["gini", "entropy", "log_loss"] = "gini"
    max_features: MaxFeatures = "sqrt"
    class_weight: Literal["balanced", "balanced_subsample"] | None = None


class GradientBoostingRegressorParams(Params):
    loss: Literal["squared_error", "absolute_error", "huber", "quantile"] = "squared_error"
    alpha: float = Field(0.9, gt=0, lt=1)
    learning_rate: float = Field(0.1, gt=0, le=10)
    n_estimators: int = Field(100, ge=1, le=10_000)
    subsample: Fraction = 1.0
    criterion: Literal["friedman_mse", "squared_error"] = "friedman_mse"
    min_samples_split: int = Field(2, ge=2, le=1_000_000)
    min_samples_leaf: int = Field(1, ge=1, le=1_000_000)
    max_depth: int | None = Field(3, ge=1, le=64)
    max_features: MaxFeatures = None
    n_iter_no_change: int | None = Field(None, ge=1, le=10_000)
    validation_fraction: float = Field(0.1, gt=0, lt=1)
    tol: float = Field(1e-4, gt=0, le=1)


class HistGradientBoostingRegressorParams(Params):
    loss: Literal["squared_error", "absolute_error", "gamma", "poisson", "quantile"] = (
        "squared_error"
    )
    quantile: float | None = Field(None, gt=0, lt=1)
    learning_rate: float = Field(0.1, gt=0, le=10)
    max_iter: int = Field(100, ge=1, le=10_000)
    max_leaf_nodes: int | None = Field(31, ge=2, le=65_536)
    max_depth: int | None = Field(None, ge=1, le=256)
    min_samples_leaf: int = Field(20, ge=1, le=1_000_000)
    l2_regularization: float = Field(0.0, ge=0)
    max_features: Fraction = 1.0
    max_bins: int = Field(255, ge=2, le=255)
    early_stopping: bool = False
    validation_fraction: float = Field(0.1, gt=0, lt=1)
    n_iter_no_change: int = Field(10, ge=1, le=10_000)
    tol: float = Field(1e-7, ge=0, le=1)


class _XGBParams(Params):
    n_estimators: int = Field(100, ge=1, le=10_000)
    max_depth: int = Field(6, ge=0, le=64)
    learning_rate: float = Field(0.3, gt=0, le=1)
    min_child_weight: float = Field(1.0, ge=0)
    subsample: Fraction = 1.0
    colsample_bytree: Fraction = 1.0
    colsample_bylevel: Fraction = 1.0
    colsample_bynode: Fraction = 1.0
    reg_alpha: float = Field(0.0, ge=0)
    reg_lambda: float = Field(1.0, ge=0)
    gamma: float = Field(0.0, ge=0)
    max_bin: int = Field(256, ge=2, le=4096)
    tree_method: Literal["hist", "approx", "exact"] = "hist"
    grow_policy: Literal["depthwise", "lossguide"] = "depthwise"
    max_leaves: int = Field(0, ge=0, le=1_000_000)
    max_cat_to_onehot: int | None = Field(None, ge=1, le=1024)


class XGBRegressorParams(_XGBParams):
    objective: Literal[
        "reg:squarederror",
        "reg:squaredlogerror",
        "reg:absoluteerror",
        "reg:pseudohubererror",
        "reg:quantileerror",
        "count:poisson",
        "reg:gamma",
        "reg:tweedie",
    ] = "reg:squarederror"
    quantile_alpha: float | None = Field(None, gt=0, lt=1)
    tweedie_variance_power: float | None = Field(None, ge=1, lt=2)
    huber_slope: float | None = Field(None, gt=0)


class XGBClassifierParams(_XGBParams):
    objective: Literal["binary:logistic"] = "binary:logistic"
    scale_pos_weight: float = Field(1.0, gt=0)


# --------------------------------------------------------------------------------- choices
#
# One choice class per kind gives a discriminated union: the exported JSON Schema checks the
# params of each kind statically (``oneOf`` on ``kind``), so the control plane rejects
# ``max_depth: -1`` with a pointer before any run starts.


class _Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LinearRegressionChoice(_Choice):
    kind: Literal["sklearn.LinearRegression"]
    params: LinearRegressionParams = LinearRegressionParams()


class RidgeChoice(_Choice):
    kind: Literal["sklearn.Ridge"]
    params: RidgeParams = RidgeParams()


class LassoChoice(_Choice):
    kind: Literal["sklearn.Lasso"]
    params: LassoParams = LassoParams()


class ElasticNetChoice(_Choice):
    kind: Literal["sklearn.ElasticNet"]
    params: ElasticNetParams = ElasticNetParams()


class QuantileRegressorChoice(_Choice):
    kind: Literal["sklearn.QuantileRegressor"]
    params: QuantileRegressorParams = QuantileRegressorParams()


class LogisticRegressionChoice(_Choice):
    kind: Literal["sklearn.LogisticRegression"]
    params: LogisticRegressionParams = LogisticRegressionParams()


class RandomForestRegressorChoice(_Choice):
    kind: Literal["sklearn.RandomForestRegressor"]
    params: RandomForestRegressorParams = RandomForestRegressorParams()


class RandomForestClassifierChoice(_Choice):
    kind: Literal["sklearn.RandomForestClassifier"]
    params: RandomForestClassifierParams = RandomForestClassifierParams()


class GradientBoostingRegressorChoice(_Choice):
    kind: Literal["sklearn.GradientBoostingRegressor"]
    params: GradientBoostingRegressorParams = GradientBoostingRegressorParams()


class HistGradientBoostingRegressorChoice(_Choice):
    kind: Literal["sklearn.HistGradientBoostingRegressor"]
    params: HistGradientBoostingRegressorParams = HistGradientBoostingRegressorParams()


class XGBRegressorChoice(_Choice):
    kind: Literal["xgboost.XGBRegressor"]
    params: XGBRegressorParams = XGBRegressorParams()


class XGBClassifierChoice(_Choice):
    kind: Literal["xgboost.XGBClassifier"]
    params: XGBClassifierParams = XGBClassifierParams()


EstimatorChoice = Annotated[
    LinearRegressionChoice
    | RidgeChoice
    | LassoChoice
    | ElasticNetChoice
    | QuantileRegressorChoice
    | LogisticRegressionChoice
    | RandomForestRegressorChoice
    | RandomForestClassifierChoice
    | GradientBoostingRegressorChoice
    | HistGradientBoostingRegressorChoice
    | XGBRegressorChoice
    | XGBClassifierChoice,
    Field(discriminator="kind"),
]
"""An allow-listed estimator and its validated hyper-parameters."""

AnyChoice = (
    LinearRegressionChoice
    | RidgeChoice
    | LassoChoice
    | ElasticNetChoice
    | QuantileRegressorChoice
    | LogisticRegressionChoice
    | RandomForestRegressorChoice
    | RandomForestClassifierChoice
    | GradientBoostingRegressorChoice
    | HistGradientBoostingRegressorChoice
    | XGBRegressorChoice
    | XGBClassifierChoice
)


# ---------------------------------------------------------------------------------- entries

QuantileMode = Literal["native_multi", "per_level"]
_TREE = frozenset({"sklearn.tree._tree.Tree"})
_HIST = frozenset(
    {
        "functools.partial",
        "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor",
        "sklearn.utils.validation.check_array",
        "sklearn.ensemble._hist_gradient_boosting.binning._BinMapper",
        "sklearn._loss.loss.AbsoluteError",
        "sklearn._loss.loss.HalfSquaredError",
        "sklearn._loss.loss.HalfGammaLoss",
        "sklearn._loss.loss.HalfPoissonLoss",
        "sklearn._loss.loss.PinballLoss",
        "sklearn._loss.link.IdentityLink",
        "sklearn._loss.link.LogLink",
        "sklearn._loss.link.Interval",
    }
)
_GB = frozenset(
    {
        *_TREE,
        "sklearn._loss.loss.AbsoluteError",
        "sklearn._loss.loss.HalfSquaredError",
        "sklearn._loss.loss.HuberLoss",
        "sklearn._loss.loss.PinballLoss",
        "sklearn._loss.link.IdentityLink",
        "sklearn._loss.link.Interval",
        "sklearn.dummy.DummyRegressor",
    }
)


def _set(field_name: str) -> Callable[[dict[str, Any], float], dict[str, Any]]:
    def apply(kw: dict[str, Any], q: float) -> dict[str, Any]:
        return {**kw, field_name: q}

    return apply


@dataclass(frozen=True, slots=True)
class EstimatorEntry:
    """Static facts about one allow-listed estimator.

    ``linear`` estimators expose ``coef_``/``intercept_`` (exact additive contributions);
    ``native_nan`` ones accept missing feature values; ``native_categorical`` says how
    integer-coded categorical columns are declared (``xgboost`` feature types or the
    ``categorical_features`` of sklearn's histogram boosting). ``quantile`` says how a
    quantile model is built: one native multi-quantile model, or one model per level with
    ``quantile_setter``. ``loss_param``/``loss_default``/``quantile_loss`` name the loss or
    objective the quantile mode manages. ``trusted`` lists the extra types skops may load.
    """

    kind: str
    module: str
    name: str
    params: type[Params]
    choice: type[_Choice]
    task: Task
    persistence: Literal["skops", "xgboost"] = "skops"
    linear: bool = False
    native_nan: bool = False
    native_categorical: Literal["xgboost", "sklearn_hist"] | None = None
    quantile: QuantileMode | None = None
    quantile_setter: Callable[[dict[str, Any], float], dict[str, Any]] | None = None
    loss_param: str | None = None
    quantile_loss: str | None = None
    seed_param: str | None = "random_state"
    n_jobs_param: str | None = None
    trusted: frozenset[str] = field(default_factory=frozenset)
    description: str = ""

    def load_class(self) -> type[Any]:
        cls: type[Any] = getattr(importlib.import_module(self.module), self.name)
        return cls


ESTIMATORS: dict[str, EstimatorEntry] = {
    e.kind: e
    for e in (
        EstimatorEntry(
            "sklearn.LinearRegression",
            "sklearn.linear_model",
            "LinearRegression",
            LinearRegressionParams,
            LinearRegressionChoice,
            "regression",
            linear=True,
            seed_param=None,
            description="Ordinary least squares.",
        ),
        EstimatorEntry(
            "sklearn.Ridge",
            "sklearn.linear_model",
            "Ridge",
            RidgeParams,
            RidgeChoice,
            "regression",
            linear=True,
            description="Least squares with an L2 penalty.",
        ),
        EstimatorEntry(
            "sklearn.Lasso",
            "sklearn.linear_model",
            "Lasso",
            LassoParams,
            LassoChoice,
            "regression",
            linear=True,
            description="Least squares with an L1 penalty.",
        ),
        EstimatorEntry(
            "sklearn.ElasticNet",
            "sklearn.linear_model",
            "ElasticNet",
            ElasticNetParams,
            ElasticNetChoice,
            "regression",
            linear=True,
            description="Least squares with a mixed L1/L2 penalty.",
        ),
        EstimatorEntry(
            "sklearn.QuantileRegressor",
            "sklearn.linear_model",
            "QuantileRegressor",
            QuantileRegressorParams,
            QuantileRegressorChoice,
            "regression",
            linear=True,
            quantile="per_level",
            quantile_setter=_set("quantile"),
            seed_param=None,
            description="Linear quantile regression (pinball loss, L1 penalty).",
        ),
        EstimatorEntry(
            "sklearn.LogisticRegression",
            "sklearn.linear_model",
            "LogisticRegression",
            LogisticRegressionParams,
            LogisticRegressionChoice,
            "classification",
            linear=True,
            description="Binary logistic regression.",
        ),
        EstimatorEntry(
            "sklearn.RandomForestRegressor",
            "sklearn.ensemble",
            "RandomForestRegressor",
            RandomForestRegressorParams,
            RandomForestRegressorChoice,
            "regression",
            n_jobs_param="n_jobs",
            trusted=_TREE,
            description="Random forest of regression trees.",
        ),
        EstimatorEntry(
            "sklearn.RandomForestClassifier",
            "sklearn.ensemble",
            "RandomForestClassifier",
            RandomForestClassifierParams,
            RandomForestClassifierChoice,
            "classification",
            n_jobs_param="n_jobs",
            trusted=_TREE,
            description="Random forest of classification trees.",
        ),
        EstimatorEntry(
            "sklearn.GradientBoostingRegressor",
            "sklearn.ensemble",
            "GradientBoostingRegressor",
            GradientBoostingRegressorParams,
            GradientBoostingRegressorChoice,
            "regression",
            quantile="per_level",
            quantile_setter=_set("alpha"),
            loss_param="loss",
            quantile_loss="quantile",
            trusted=_GB,
            description="Gradient-boosted regression trees.",
        ),
        EstimatorEntry(
            "sklearn.HistGradientBoostingRegressor",
            "sklearn.ensemble",
            "HistGradientBoostingRegressor",
            HistGradientBoostingRegressorParams,
            HistGradientBoostingRegressorChoice,
            "regression",
            native_nan=True,
            native_categorical="sklearn_hist",
            quantile="per_level",
            quantile_setter=_set("quantile"),
            loss_param="loss",
            quantile_loss="quantile",
            trusted=_HIST,
            description="Histogram gradient boosting (native missing values and categories).",
        ),
        EstimatorEntry(
            "xgboost.XGBRegressor",
            "xgboost",
            "XGBRegressor",
            XGBRegressorParams,
            XGBRegressorChoice,
            "regression",
            persistence="xgboost",
            native_nan=True,
            native_categorical="xgboost",
            quantile="native_multi",
            loss_param="objective",
            quantile_loss="reg:quantileerror",
            n_jobs_param="n_jobs",
            description="XGBoost gradient-boosted trees (native missing values, categories and "
            "multi-quantile objective).",
        ),
        EstimatorEntry(
            "xgboost.XGBClassifier",
            "xgboost",
            "XGBClassifier",
            XGBClassifierParams,
            XGBClassifierChoice,
            "classification",
            persistence="xgboost",
            native_nan=True,
            native_categorical="xgboost",
            n_jobs_param="n_jobs",
            description="XGBoost gradient-boosted trees for binary classification.",
        ),
    )
}
"""Every estimator kind a plugin may use, keyed by ``kind``."""


def _settings_error(
    message: str, pointer: str = "/model/settings/estimator", **kw: Any
) -> MapiError:
    return MapiError(message, code=ErrorCode.SETTINGS_INVALID, pointer=pointer, **kw)


def get_entry(kind: str) -> EstimatorEntry:
    """The registry entry for ``kind``; ``SETTINGS_INVALID`` listing the kinds otherwise."""
    entry = ESTIMATORS.get(kind)
    if entry is None:
        raise _settings_error(
            "estimator kind is not allow-listed; choose one of " + ", ".join(sorted(ESTIMATORS)),
            pointer="/model/settings/estimator/kind",
            untrusted_text=str(kind)[:200],
        )
    return entry


def resolve_choice(obj: Any) -> AnyChoice:
    """Normalise what ``SklearnModel.estimator()`` returns into a validated choice.

    Accepts a choice model, a ``{"kind": ..., "params": {...}}`` mapping, or an instance of
    an allow-listed estimator class (see :func:`choice_from_instance`).
    """
    if isinstance(obj, _Choice):
        return obj  # type: ignore[return-value]
    if isinstance(obj, Mapping):
        entry = get_entry(str(obj.get("kind")))
        try:
            return entry.choice.model_validate(dict(obj))  # type: ignore[return-value]
        except ValidationError as exc:
            raise _settings_error(
                f"invalid params for estimator {entry.kind!r}", untrusted_text=str(exc)[:4000]
            ) from None
    return choice_from_instance(obj)


def choice_from_instance(est: Any) -> AnyChoice:
    """The choice for an allow-listed estimator instance.

    The class must be exactly an allow-listed class (subclasses are rejected: they could
    run arbitrary code), and every parameter that differs from the library default must be
    one the kind's params model declares and accepts.
    """
    entry = next(
        (e for e in ESTIMATORS.values() if type(est) is e.load_class()),
        None,
    )
    if entry is None:
        raise _settings_error(
            f"estimator class {type(est).__module__}.{type(est).__qualname__} is not "
            "allow-listed; choose one of " + ", ".join(sorted(ESTIMATORS))
        )
    current = est.get_params(deep=False)
    defaults = entry.load_class()().get_params(deep=False)
    owned = {entry.seed_param, entry.n_jobs_param, "verbosity", "verbose"}
    changed = {
        k: v
        for k, v in current.items()
        if k not in owned and not _same(v, defaults.get(k)) and v is not None
    }
    unknown = sorted(set(changed) - set(entry.params.model_fields))
    if unknown:
        raise _settings_error(
            f"estimator {entry.kind!r} sets parameters the platform does not allow: "
            + ", ".join(unknown)
        )
    params = {k: (v.tolist() if hasattr(v, "tolist") else v) for k, v in changed.items()}
    try:
        return entry.choice.model_validate(  # type: ignore[return-value]
            {"kind": entry.kind, "params": params}
        )
    except ValidationError as exc:
        raise _settings_error(
            f"invalid params for estimator {entry.kind!r}", untrusted_text=str(exc)[:4000]
        ) from None


def _same(a: Any, b: Any) -> bool:
    try:
        return bool(a == b)
    except Exception:  # pragma: no cover - array-valued params
        return a is b


def build_estimator(
    choice: AnyChoice,
    *,
    seed: int,
    n_jobs: int = 1,
    overrides: Mapping[str, Any] | None = None,
    categorical: Sequence[int] = (),
    n_features: int | None = None,
) -> Any:
    """Instantiate the estimator for ``choice`` with every parameter passed explicitly.

    ``overrides`` are platform-owned parameters (quantile objective ...). ``categorical``
    lists the column indices holding integer category codes; they are declared natively
    when the estimator supports it (callers one-hot encode otherwise).
    """
    entry = get_entry(choice.kind)
    kwargs: dict[str, Any] = choice.params.model_dump()
    if entry.persistence == "xgboost":
        # optional objective-specific xgboost parameters are omitted when unset
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
    kwargs.update(overrides or {})
    if entry.seed_param is not None:
        kwargs[entry.seed_param] = int(seed) % (2**32)
    if entry.n_jobs_param is not None:
        kwargs[entry.n_jobs_param] = n_jobs
    if entry.persistence == "xgboost":
        kwargs["verbosity"] = 0
    if categorical:
        if entry.native_categorical == "xgboost":
            if n_features is None:
                raise ValueError("n_features is required for xgboost categorical columns")
            cats = set(categorical)
            kwargs["enable_categorical"] = True
            kwargs["feature_types"] = ["c" if j in cats else "q" for j in range(n_features)]
        elif entry.native_categorical == "sklearn_hist":
            kwargs["categorical_features"] = list(categorical)
        else:
            raise ValueError(f"{entry.kind} has no native categorical support")
    elif entry.native_categorical == "sklearn_hist":
        kwargs["categorical_features"] = None
    return entry.load_class()(**kwargs)
