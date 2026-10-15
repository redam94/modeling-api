"""``tabular.regressor``: the built-in store-demand style quantile regressor (plan §6.2).

A thin, opinionated layer over :class:`mapi_sklearn.SklearnQuantileModel`:

* the settings' ``estimator`` accepts exactly the quantile-capable kinds of the allow-listed
  estimator registry (:data:`QUANTILE_ESTIMATORS`), so a non-quantile estimator is rejected
  statically by the exported JSON Schema rather than at fit time;
* defaults suit weekly demand panels: xgboost with a moderate forest, quantiles
  ``[0.1, 0.5, 0.9]`` (an 80% band around the median), pooled new levels and direct
  forecasting;
* the manifest lists the plots of §6.2 (``core.fit_vs_actual``,
  ``core.residuals_over_time``, ``ml.feature_importance``).

Representation is ``quantiles``: per-cell and never aggregated across cells.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

from mapi_sklearn.estimators import (
    ESTIMATORS,
    GradientBoostingRegressorChoice,
    HistGradientBoostingRegressorChoice,
    QuantileRegressorChoice,
    XGBRegressorChoice,
    XGBRegressorParams,
)
from mapi_sklearn.model import SklearnQuantileModel
from mapi_sklearn.settings import QuantileSettings, QuantileUncertainty
from pydantic import Field

__all__ = [
    "DEFAULT_ESTIMATOR",
    "QUANTILE_ESTIMATORS",
    "QuantileEstimatorChoice",
    "TabularRegressor",
    "TabularRegressorSettings",
]

QuantileEstimatorChoice = Annotated[
    XGBRegressorChoice
    | HistGradientBoostingRegressorChoice
    | GradientBoostingRegressorChoice
    | QuantileRegressorChoice,
    Field(discriminator="kind"),
]
"""The registry's quantile-capable regressors (checked against the registry in the tests)."""

QUANTILE_ESTIMATORS: tuple[str, ...] = (
    "xgboost.XGBRegressor",
    "sklearn.HistGradientBoostingRegressor",
    "sklearn.GradientBoostingRegressor",
    "sklearn.QuantileRegressor",
)

DEFAULT_ESTIMATOR = XGBRegressorChoice(
    kind="xgboost.XGBRegressor",
    params=XGBRegressorParams(n_estimators=300, max_depth=5, learning_rate=0.05),
)


class TabularRegressorSettings(QuantileSettings):
    """Settings of ``tabular.regressor`` (everything except ``rows`` has a default)."""

    estimator: QuantileEstimatorChoice = Field(
        default=DEFAULT_ESTIMATOR,
        description="A quantile-capable estimator from the allow-listed registry",
    )
    uncertainty: QuantileUncertainty = Field(
        default=QuantileUncertainty(quantiles=[0.1, 0.5, 0.9]),
        description="Quantile levels (the default gives an 80% band around the median)",
    )


class TabularRegressor(SklearnQuantileModel[TabularRegressorSettings]):
    """Quantile regression over stacked rows with an allow-listed estimator."""

    Settings = TabularRegressorSettings
    id = "tabular.regressor"
    version = "1.0.0"
    description = (
        "Tabular quantile regression for panels such as store x sku x week demand: rows are "
        "stacked from labelled dims, coordinates become categorical features, new levels are "
        "pooled and forecasts are direct. Quantiles are per cell and never aggregated."
    )
    plots: ClassVar[tuple[str, ...]] = (
        "core.fit_vs_actual",
        "core.residuals_over_time",
        "ml.feature_importance",
    )

    @classmethod
    def estimators(cls) -> dict[str, dict[str, Any]]:
        """The estimator kinds this plugin accepts, with their registry facts."""
        out: dict[str, dict[str, Any]] = {}
        for kind in QUANTILE_ESTIMATORS:
            entry = ESTIMATORS[kind]
            out[kind] = {
                "quantile": entry.quantile,
                "native_nan": entry.native_nan,
                "native_categorical": entry.native_categorical,
                "persistence": entry.persistence,
                "params_schema": entry.params.model_json_schema(),
                "description": entry.description,
            }
        return out
