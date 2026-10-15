# `mapi_sklearn`

scikit-learn and xgboost models: write `estimator()`, get stacking, safe persistence and
uncertainty.

## Base classes

::: mapi_sklearn.model
    options:
      members: [SklearnModel, SklearnQuantileModel, SklearnBootstrapModel, SklearnConformalModel, SklearnClassifier, capabilities_for, sklearn_schema_template]
      heading_level: 3

## Settings

::: mapi_sklearn.settings
    options:
      members: [SklearnSettings, PointSettings, QuantileSettings, BootstrapSettings, ConformalSettings, ClassifierSettings]
      heading_level: 3

## Estimator registry

::: mapi_sklearn.estimators
    options:
      members: [ESTIMATORS, EstimatorEntry, EstimatorChoice, get_entry]
      heading_level: 3

## Built-in plugins

::: mapi_sklearn.plugins
    options:
      members: [TabularRegressor, TabularQuantiles, TabularBootstrap, TabularConformal, TabularClassifier]
      heading_level: 3
