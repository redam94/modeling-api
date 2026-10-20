"""The built-in ``sklearn.*`` model plugins (registered through the ``mapi.models`` entry point).

Each plugin takes its estimator from ``settings.estimator``; the uncertainty kind, and so
the output representation, is fixed by the plugin:

========================== ============= ==================================================
plugin                     representation  uncertainty
========================== ============= ==================================================
sklearn.tabular_regressor  point         none (``contributions``, ``retransform``)
sklearn.tabular_quantiles  quantiles     ``{kind: quantile, quantiles: [...]}``
sklearn.tabular_bootstrap  draws         ``{kind: bootstrap, n: 50, by: store}``
sklearn.tabular_conformal  interval      ``{kind: conformal, alpha: 0.1, calibration: 0.2}``
sklearn.tabular_classifier point         binary class probability
sklearn.multiclass_classifier point      one probability per class (``y_hat(..., class)``)
========================== ============= ==================================================
"""

from __future__ import annotations

from mapi_sklearn.model import (
    SklearnBootstrapModel,
    SklearnClassifier,
    SklearnConformalModel,
    SklearnModel,
    SklearnMulticlassClassifier,
    SklearnQuantileModel,
)
from mapi_sklearn.settings import (
    MulticlassClassifierSettings,
    TabularBootstrapSettings,
    TabularClassifierSettings,
    TabularConformalSettings,
    TabularPointSettings,
    TabularQuantileSettings,
)

__all__ = [
    "PLUGINS",
    "MulticlassClassifier",
    "TabularBootstrap",
    "TabularClassifier",
    "TabularConformal",
    "TabularQuantiles",
    "TabularRegressor",
]

VERSION = "1.0.0"


class TabularRegressor(SklearnModel[TabularPointSettings]):
    """Point predictions from an allow-listed regressor."""

    Settings = TabularPointSettings
    id = "sklearn.tabular_regressor"
    version = VERSION
    description = (
        "Tabular regression with an allow-listed scikit-learn or xgboost estimator: rows are "
        "stacked from labelled dims, predictions are point estimates (with exact linear or "
        "counterfactual-zeroing contributions)."
    )


class TabularQuantiles(SklearnQuantileModel[TabularQuantileSettings]):
    """Conditional quantiles (xgboost multi-quantile or one sklearn model per level)."""

    Settings = TabularQuantileSettings
    id = "sklearn.tabular_quantiles"
    version = VERSION
    description = (
        "Tabular quantile regression: native xgboost multi-quantile or one quantile model per "
        "level; quantiles are per cell and cannot be aggregated."
    )


class TabularBootstrap(SklearnBootstrapModel[TabularBootstrapSettings]):
    """A bootstrap ensemble giving joint draws."""

    Settings = TabularBootstrapSettings
    id = "sklearn.tabular_bootstrap"
    version = VERSION
    description = (
        "Tabular regression with a bootstrap ensemble: one joint draw per replicate, so any "
        "aggregation is valid."
    )


class TabularConformal(SklearnConformalModel[TabularConformalSettings]):
    """Split-conformal prediction intervals."""

    Settings = TabularConformalSettings
    id = "sklearn.tabular_conformal"
    version = VERSION
    description = (
        "Tabular regression with split-conformal prediction intervals (per-cell coverage "
        "1 - alpha; intervals cannot be aggregated)."
    )


class TabularClassifier(SklearnClassifier[TabularClassifierSettings]):
    """Binary class probabilities."""

    Settings = TabularClassifierSettings
    id = "sklearn.tabular_classifier"
    version = VERSION
    description = (
        "Binary classification: the probability that the target is the positive class "
        "(scikit-learn, xgboost, LightGBM or CatBoost classifiers)."
    )


class MulticlassClassifier(SklearnMulticlassClassifier[MulticlassClassifierSettings]):
    """One probability per class."""

    Settings = MulticlassClassifierSettings
    id = "sklearn.multiclass_classifier"
    version = VERSION
    description = (
        "Multiclass classification with an allow-listed scikit-learn, xgboost, LightGBM or "
        "CatBoost classifier: the probability of each class of a text or integer target."
    )


PLUGINS = (
    TabularRegressor,
    TabularQuantiles,
    TabularBootstrap,
    TabularConformal,
    TabularClassifier,
    MulticlassClassifier,
)
"""Every built-in plugin (the ``mapi.models`` entry point loads this tuple)."""
