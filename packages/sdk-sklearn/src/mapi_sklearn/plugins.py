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
========================== ============= ==================================================
"""

from __future__ import annotations

from mapi_sklearn.model import (
    SklearnBootstrapModel,
    SklearnClassifier,
    SklearnConformalModel,
    SklearnModel,
    SklearnQuantileModel,
)
from mapi_sklearn.settings import (
    BootstrapSettings,
    ClassifierSettings,
    ConformalSettings,
    PointSettings,
    QuantileSettings,
)

__all__ = [
    "PLUGINS",
    "TabularBootstrap",
    "TabularClassifier",
    "TabularConformal",
    "TabularQuantiles",
    "TabularRegressor",
]

VERSION = "1.0.0"


class TabularRegressor(SklearnModel[PointSettings]):
    """Point predictions from an allow-listed regressor."""

    id = "sklearn.tabular_regressor"
    version = VERSION
    description = (
        "Tabular regression with an allow-listed scikit-learn or xgboost estimator: rows are "
        "stacked from labelled dims, predictions are point estimates (with exact linear or "
        "counterfactual-zeroing contributions)."
    )


class TabularQuantiles(SklearnQuantileModel[QuantileSettings]):
    """Conditional quantiles (xgboost multi-quantile or one sklearn model per level)."""

    id = "sklearn.tabular_quantiles"
    version = VERSION
    description = (
        "Tabular quantile regression: native xgboost multi-quantile or one quantile model per "
        "level; quantiles are per cell and cannot be aggregated."
    )


class TabularBootstrap(SklearnBootstrapModel[BootstrapSettings]):
    """A bootstrap ensemble giving joint draws."""

    id = "sklearn.tabular_bootstrap"
    version = VERSION
    description = (
        "Tabular regression with a bootstrap ensemble: one joint draw per replicate, so any "
        "aggregation is valid."
    )


class TabularConformal(SklearnConformalModel[ConformalSettings]):
    """Split-conformal prediction intervals."""

    id = "sklearn.tabular_conformal"
    version = VERSION
    description = (
        "Tabular regression with split-conformal prediction intervals (per-cell coverage "
        "1 - alpha; intervals cannot be aggregated)."
    )


class TabularClassifier(SklearnClassifier[ClassifierSettings]):
    """Binary class probabilities."""

    id = "sklearn.tabular_classifier"
    version = VERSION
    description = "Binary classification: the probability that the target is the positive class."


PLUGINS = (
    TabularRegressor,
    TabularQuantiles,
    TabularBootstrap,
    TabularConformal,
    TabularClassifier,
)
"""Every built-in plugin (the ``mapi.models`` entry point loads this tuple)."""
