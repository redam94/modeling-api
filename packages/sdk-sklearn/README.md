# mapi-sklearn

scikit-learn and xgboost model plugins for modeling-api.

An author implements only `estimator(settings)`, choosing from an allow-listed estimator
registry (each estimator has a bounded Pydantic params model). The platform stacks the
labelled `xr.Dataset` into a design matrix (`rows`, `columns`, `coord_features`), fits,
persists with **skops or xgboost JSON — never pickle**, handles unseen levels and direct
forecasting, and unstacks predictions back to labelled arrays.

| Plugin | Representation |
|---|---|
| `sklearn.tabular_regressor` | point (+ exact linear or counterfactual-zeroing contributions) |
| `sklearn.tabular_quantiles` | quantiles (xgboost multi-quantile, or one sklearn model per level) |
| `sklearn.tabular_bootstrap` | draws (one joint draw per bootstrap replicate) |
| `sklearn.tabular_conformal` | interval (split conformal; never draws) |
| `sklearn.tabular_classifier` | point (binary class probability) |

```python
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sklearn.testing import demand_dataset, demand_registry, demand_schema, demand_spec

data = demand_dataset()                                   # store x sku x week demand
bundle = fit_spec(
    demand_spec("sklearn.tabular_quantiles@^1"),          # xgboost, quantiles 5%..95%
    data,
    registry=demand_registry(),
    raw_schema=demand_schema(data),
)
print(bundle.metrics.values)
```

Modules: `estimators` (allow-list and params models), `settings`, `design` (stacking and
coordinate features), `levels` (pooled/zero new levels), `uncertainty` (quantile, bootstrap,
split-conformal blocks), `contributions`, `persistence` (skops allow-list, xgboost JSON),
`model` (`SklearnModel` and variants), `plugins` (the registered `sklearn.*` plugins, entry
point group `mapi.models`) and `testing` (the synthetic demand panel and spec).

Guide: `docs/guides/sklearn.md`. License: Apache License 2.0.
