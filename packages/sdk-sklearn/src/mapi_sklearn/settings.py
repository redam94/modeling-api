"""Settings of the scikit-learn / xgboost model plugins.

A spec's ``model.settings`` for ``sklearn.*`` plugins looks like::

    estimator: {kind: xgboost.XGBRegressor, params: {n_estimators: 600, max_depth: 6}}
    rows: [store, sku, time]               # dims stacked into samples
    time_dim: time                         # the row dim that extends at forecast time
    columns: {roles: [covariate]}          # which variables become feature columns
    coord_features: {store: category, sku: category}
    uncertainty: {kind: quantile, quantiles: [0.05, 0.5, 0.95]}
    forecast_strategy: direct
    retransform: median

The uncertainty ``kind`` fixes the output representation, so each kind is its own plugin
(``sklearn.tabular_quantiles`` accepts only ``kind: quantile`` and so on).
"""

from __future__ import annotations

from typing import Annotated, Literal

from mapi_sdk.models import ModelSettings
from mapi_spec.schema import Role
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mapi_sklearn.estimators import ESTIMATORS, EstimatorChoice

__all__ = [
    "BootstrapSettings",
    "BootstrapUncertainty",
    "ClassifierSettings",
    "ColumnsSettings",
    "ConformalSettings",
    "ConformalUncertainty",
    "CoordEncoding",
    "MulticlassClassifierSettings",
    "NoUncertainty",
    "PointSettings",
    "QuantileSettings",
    "QuantileUncertainty",
    "SklearnSettings",
    "TabularBootstrapSettings",
    "TabularClassifierSettings",
    "TabularConformalSettings",
    "TabularPointSettings",
    "TabularQuantileSettings",
]

Name = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
CoordEncoding = Literal["category", "onehot", "ordinal", "numeric"]
ContributionMethod = Literal["none", "auto", "exact", "zeroing"]
DEFAULT_ROLES: tuple[Role, ...] = (
    Role.COVARIATE,
    Role.MEDIA,
    Role.SPEND,
    Role.CONTROL,
    Role.EXPOSURE,
)


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ColumnsSettings(_Block):
    """Which variables become feature columns.

    ``vars`` (explicit list) wins over ``roles``; ``exclude`` removes names from either.
    Variables with dims beyond ``rows`` are flattened into one column per label
    (``spend[tv]``, ``promo_lag[1]``).
    """

    roles: list[Role] = Field(default_factory=lambda: list(DEFAULT_ROLES), max_length=16)
    vars: list[Name] | None = Field(None, max_length=1024)
    exclude: list[Name] = Field(default_factory=list, max_length=1024)

    @field_validator("roles")
    @classmethod
    def _no_target(cls, v: list[Role]) -> list[Role]:
        if Role.TARGET in v:
            raise ValueError("the target role cannot be a feature column")
        return v


class NoUncertainty(_Block):
    """Point predictions (representation ``point``)."""

    kind: Literal["none"] = "none"


class QuantileUncertainty(_Block):
    """Conditional quantiles (representation ``quantiles``).

    xgboost fits one native multi-quantile model; sklearn's quantile-capable estimators fit
    one model per level. ``rearrange`` sorts crossing quantiles per cell (monotone
    rearrangement), which never makes the pinball loss worse.
    """

    kind: Literal["quantile"] = "quantile"
    quantiles: list[float] = Field(
        default_factory=lambda: [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95],
        min_length=1,
        max_length=99,
    )
    rearrange: bool = True

    @field_validator("quantiles")
    @classmethod
    def _levels(cls, v: list[float]) -> list[float]:
        if any(not 0 < q < 1 for q in v):
            raise ValueError("quantile levels must be strictly between 0 and 1")
        if len(set(v)) != len(v):
            raise ValueError("quantile levels must be unique")
        return sorted(v)


class BootstrapUncertainty(_Block):
    """Bootstrap ensemble (representation ``draws``, one draw per replicate).

    ``by`` resamples whole levels of a row dim (cluster bootstrap) instead of rows.
    ``expected`` outputs are the replicate predictions; ``predictive`` outputs add a
    resampled out-of-bag residual to each draw.
    """

    kind: Literal["bootstrap"] = "bootstrap"
    n: int = Field(50, ge=2, le=1000)
    by: Name | None = None


class ConformalUncertainty(_Block):
    """Split-conformal prediction intervals (representation ``interval``, never draws).

    A ``calibration`` fraction of the training rows is held out (the most recent periods of
    ``time_dim`` for ``calibration_by: last``, a seeded random subset for ``random``); the
    interval is ``point ± q`` where ``q`` is the ``ceil((n+1)(1-alpha))``-th smallest
    absolute calibration residual. ``refit`` refits the point model on all rows afterwards
    (sharper, but the finite-sample guarantee then only holds approximately).
    """

    kind: Literal["conformal"] = "conformal"
    alpha: float = Field(0.1, gt=0, lt=1)
    calibration: float = Field(0.2, ge=0.05, le=0.5)
    calibration_by: Literal["last", "random"] = "last"
    refit: bool = False


class SklearnSettings(ModelSettings):
    """Settings shared by every scikit-learn / xgboost plugin."""

    estimator: EstimatorChoice | None = None
    target: Name = "y"
    rows: list[Name] = Field(..., min_length=1, max_length=8)
    time_dim: Name = "time"
    time_kind: Literal["time", "integer", "index", "none"] = "time"
    columns: ColumnsSettings = Field(default_factory=ColumnsSettings)
    coord_features: dict[Name, CoordEncoding] = Field(default_factory=dict, max_length=8)
    new_levels: Literal["pooled", "zero", "error"] = "pooled"
    pooled_max_levels: int = Field(256, ge=1, le=10_000)
    weight: Name | None = None
    mask: Name | None = None
    forecast_strategy: Literal["direct", "recursive"] = "direct"
    retransform: Literal["median", "smearing"] = "median"
    n_jobs: int = Field(1, ge=1, le=64)

    @model_validator(mode="after")
    def _consistent(self) -> SklearnSettings:
        if len(set(self.rows)) != len(self.rows):
            raise ValueError("rows must not repeat a dim")
        if self.time_kind != "none" and self.time_dim not in self.rows:
            raise ValueError(
                f"time_dim {self.time_dim!r} must be one of rows {self.rows} "
                "(set time_kind: none when the rows have no extending dim)"
            )
        unknown = sorted(set(self.coord_features) - set(self.rows))
        if unknown:
            raise ValueError(f"coord_features {unknown} must be row dims")
        for name in (self.weight, self.mask):
            if name is not None and name == self.target:
                raise ValueError("weight/mask cannot be the target")
        return self

    @property
    def time_row(self) -> str | None:
        """The extending row dim, or ``None``."""
        return None if self.time_kind == "none" else self.time_dim


class PointSettings(SklearnSettings):
    """``sklearn.tabular_regressor``: point predictions with optional contributions.

    ``contributions``: ``auto`` (exact for linear estimators, none otherwise), ``exact``
    (linear estimators only), ``zeroing`` (counterfactual zeroing for any estimator) or
    ``none``.
    """

    uncertainty: NoUncertainty = Field(default_factory=NoUncertainty)
    contributions: ContributionMethod = "auto"


class QuantileSettings(SklearnSettings):
    """``sklearn.tabular_quantiles``: conditional quantiles."""

    uncertainty: QuantileUncertainty = Field(default_factory=QuantileUncertainty)


class BootstrapSettings(SklearnSettings):
    """``sklearn.tabular_bootstrap``: a bootstrap ensemble giving joint draws."""

    uncertainty: BootstrapUncertainty = Field(default_factory=BootstrapUncertainty)
    contributions: ContributionMethod = "auto"

    @model_validator(mode="after")
    def _by_is_row(self) -> BootstrapSettings:
        by = self.uncertainty.by
        if by is not None and by not in self.rows:
            raise ValueError(f"uncertainty.by {by!r} must be one of rows {self.rows}")
        return self


class ConformalSettings(SklearnSettings):
    """``sklearn.tabular_conformal``: split-conformal prediction intervals."""

    uncertainty: ConformalUncertainty = Field(default_factory=ConformalUncertainty)

    @model_validator(mode="after")
    def _calibration_dim(self) -> ConformalSettings:
        if self.uncertainty.calibration_by == "last" and self.time_row is None:
            raise ValueError("calibration_by 'last' needs a time_dim; use 'random'")
        return self


class ClassifierSettings(SklearnSettings):
    """``sklearn.tabular_classifier``: binary class probabilities (``y_hat = P(positive)``).

    ``positive_class`` defaults to the larger of the two target labels.
    """

    uncertainty: NoUncertainty = Field(default_factory=NoUncertainty)
    positive_class: float | None = None
    contributions: Literal["none", "zeroing"] = "none"


class MulticlassClassifierSettings(SklearnSettings):
    """``sklearn.multiclass_classifier``: one probability per class (``y_hat(*rows, class)``).

    ``class_order`` fixes the classes and their order (it must name every training label);
    without it the classes are the sorted training labels. ``target_dtype`` is the dtype the
    input schema declares for the target: ``str`` labels or ``int64`` codes.
    """

    estimator: EstimatorChoice
    uncertainty: NoUncertainty = Field(default_factory=NoUncertainty)
    class_order: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        None, min_length=2, max_length=200
    )
    max_classes: int = Field(50, ge=2, le=200)
    target_dtype: Literal["str", "int64"] = "str"

    @model_validator(mode="after")
    def _classification(self) -> MulticlassClassifierSettings:
        entry = ESTIMATORS[self.estimator.kind]
        if entry.task != "classification":
            raise ValueError(
                f"estimator {entry.kind!r} is a {entry.task} estimator; a classifier is needed"
            )
        order = self.class_order
        if order is not None:
            if len(set(order)) != len(order):
                raise ValueError("class_order must not repeat a class")
            if len(order) > self.max_classes:
                raise ValueError("class_order has more classes than max_classes")
        return self


# The built-in ``sklearn.*`` plugins take the estimator from settings, so it is required there
# (and checked statically); authors who return one from ``estimator()`` use the classes above.


class TabularPointSettings(PointSettings):
    """Settings of ``sklearn.tabular_regressor``."""

    estimator: EstimatorChoice


class TabularQuantileSettings(QuantileSettings):
    """Settings of ``sklearn.tabular_quantiles``."""

    estimator: EstimatorChoice


class TabularBootstrapSettings(BootstrapSettings):
    """Settings of ``sklearn.tabular_bootstrap``."""

    estimator: EstimatorChoice


class TabularConformalSettings(ConformalSettings):
    """Settings of ``sklearn.tabular_conformal``."""

    estimator: EstimatorChoice


class TabularClassifierSettings(ClassifierSettings):
    """Settings of ``sklearn.tabular_classifier``."""

    estimator: EstimatorChoice
