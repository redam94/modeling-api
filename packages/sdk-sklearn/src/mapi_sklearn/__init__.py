"""scikit-learn / xgboost model plugins for modeling-api.

Authors subclass :class:`SklearnModel` (point), :class:`SklearnQuantileModel`,
:class:`SklearnBootstrapModel`, :class:`SklearnConformalModel`, :class:`SklearnClassifier` or
:class:`SklearnMulticlassClassifier` and implement only ``estimator(settings)``, choosing from
the allow-listed :data:`ESTIMATORS`. The platform stacks labelled data into a design matrix,
fits, persists in a safe native format (never pickle), and unstacks predictions back to
labelled arrays in the plugin's representation.
"""

from mapi_sklearn.estimators import ESTIMATORS, EstimatorChoice, EstimatorEntry, get_entry
from mapi_sklearn.model import (
    SklearnBootstrapModel,
    SklearnClassifier,
    SklearnConformalModel,
    SklearnModel,
    SklearnMulticlassClassifier,
    SklearnQuantileModel,
    capabilities_for,
    multiclass_schema_template,
    sklearn_schema_template,
)
from mapi_sklearn.plugins import (
    PLUGINS,
    MulticlassClassifier,
    TabularBootstrap,
    TabularClassifier,
    TabularConformal,
    TabularQuantiles,
    TabularRegressor,
)
from mapi_sklearn.settings import (
    BootstrapSettings,
    ClassifierSettings,
    ConformalSettings,
    MulticlassClassifierSettings,
    PointSettings,
    QuantileSettings,
    SklearnSettings,
    TabularBootstrapSettings,
    TabularClassifierSettings,
    TabularConformalSettings,
    TabularPointSettings,
    TabularQuantileSettings,
)

__version__ = "0.14.0"

__all__ = [
    "ESTIMATORS",
    "PLUGINS",
    "BootstrapSettings",
    "ClassifierSettings",
    "ConformalSettings",
    "EstimatorChoice",
    "EstimatorEntry",
    "MulticlassClassifier",
    "MulticlassClassifierSettings",
    "PointSettings",
    "QuantileSettings",
    "SklearnBootstrapModel",
    "SklearnClassifier",
    "SklearnConformalModel",
    "SklearnModel",
    "SklearnMulticlassClassifier",
    "SklearnQuantileModel",
    "SklearnSettings",
    "TabularBootstrap",
    "TabularBootstrapSettings",
    "TabularClassifier",
    "TabularClassifierSettings",
    "TabularConformal",
    "TabularConformalSettings",
    "TabularPointSettings",
    "TabularQuantileSettings",
    "TabularQuantiles",
    "TabularRegressor",
    "__version__",
    "capabilities_for",
    "get_entry",
    "multiclass_schema_template",
    "sklearn_schema_template",
]
