# Tabular models with scikit-learn and xgboost

`mapi-sklearn` turns any allow-listed scikit-learn or xgboost estimator into a full
modeling-api plugin. You describe *which dims are samples* and *which variables are
features*; the platform stacks your labelled `xr.Dataset` into a design matrix, fits,
persists safely, and at score time rebuilds exactly the same columns, handles new stores
and forecast horizons, and unstacks predictions back to labelled arrays — with honest
uncertainty.

## The plugins

The uncertainty kind decides the output **representation**, which in turn decides which
summaries and aggregations are valid. Each kind is its own plugin:

| Plugin | Representation | What `y_hat` holds | Aggregate over cells? |
|---|---|---|---|
| `sklearn.tabular_regressor` | point | point prediction (+ `contributions`) | linear only, flagged |
| `sklearn.tabular_quantiles` | quantiles | conditional quantiles `(quantile, ...)` | no |
| `sklearn.tabular_bootstrap` | draws | one joint draw per bootstrap replicate `(chain, draw, ...)` | yes, any |
| `sklearn.tabular_conformal` | interval | `(bound=[lower, upper, point], ...)`, attr `alpha` | no |
| `sklearn.tabular_classifier` | point | probability of the positive class | linear only, flagged |

!!! note "Why conformal is an interval, not draws"
    A split-conformal interval guarantees *per-cell* coverage (`P(y in interval) >= 1 - alpha`).
    It is not a distribution, so summing intervals across stores or weeks would claim a
    coverage nobody measured. The platform rejects such aggregations with
    `CAPABILITY_UNSUPPORTED`. If you need totals, use `sklearn.tabular_bootstrap`, whose draws
    are joint.

## A store-demand forecast

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: store-demand-xgb, project: retail}
data:
  dataset: dataset://retail/store_sku_weekly@latest
  bind:
    dims: {store: store_id, sku: sku, time: week}
    vars: {y: units_sold, price: price, promo: promo_flag}
  split: {kind: time_holdout, dim: time, last: 12}
pipeline:
  - {id: imp,   use: core.impute@^1,   with: {vars: [price], method: ffill, along: time, within: [store, sku]}}
  - {id: plags, use: core.lag@^1,      with: {vars: [promo], along: time, within: [store, sku], lags: [1, 2]}}
  - {id: cal,   use: core.calendar@^1, with: {along: time, features: [week_of_year, month]}}
  - {id: logy,  use: core.log1p@^1,    with: {vars: [y]}}
model:
  use: sklearn.tabular_quantiles@^1
  settings:
    estimator: {kind: xgboost.XGBRegressor, params: {n_estimators: 600, max_depth: 6, learning_rate: 0.05}}
    rows: [store, sku, time]                  # dims stacked into samples
    columns: {roles: [covariate]}             # which variables become features
    coord_features: {store: category, sku: category}
    uncertainty: {kind: quantile, quantiles: [0.1, 0.5, 0.9]}
    forecast_strategy: direct
  fit: {seed: 7, mode: evaluate_then_refit}
evaluate: {metrics: [rmse, mape, coverage_80]}
execution: {resource_class: S, timeout: 10m}
```

What happens:

1. The holdout (last 12 weeks) is scored **through the score path**, using context tails from
   the training weeks, and `rmse`, `mape` and `coverage_80` are computed in units sold (the
   `log1p` is inverted; quantiles map exactly through monotone transforms).
2. The model is refitted on all weeks; the bundle stores the xgboost model as JSON.
3. A forecast request supplies future prices and promotions; lags are rebuilt from the stored
   tail, and the platform checks the weeks continue the fitted range without gaps.

```yaml
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://retail/store-demand-xgb
mode: forecast
horizon: {dim: time, periods: 4}
data: dataset://retail/plan_next_4_weeks
output: {summary: [median, q10, q90]}
```

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `estimator` | — | `{kind, params}` from the allow-list below (required for the built-in plugins) |
| `target` | `y` | canonical name of the target variable |
| `rows` | — | dims stacked into samples; every row dim of the target |
| `time_dim` / `time_kind` | `time` / `time` | the row dim that extends at forecast time and its kind (`time`, `integer`, `index`; `none` when no row dim extends) |
| `columns` | `{roles: [covariate, media, spend, control, exposure]}` | feature variables by role, or an explicit `vars` list; `exclude` removes names |
| `coord_features` | `{}` | encode row-dim labels as features: `category`, `onehot`, `ordinal`, `numeric` |
| `new_levels` | `pooled` | what an unseen level of a row dim becomes: `pooled`, `zero` or `error` |
| `pooled_max_levels` | 256 | pool over at most this many (most frequent) fitted levels |
| `weight`, `mask` | — | sample-weight variable; boolean mask of usable cells |
| `forecast_strategy` | `direct` | `recursive` is rejected with `RECURSIVE_UNSUPPORTED` in v1 |
| `retransform` | `median` | point predictions through a non-linear inverse (`log1p`): `median` or Duan `smearing` |
| `contributions` | `auto` | point/bootstrap plugins: `auto`, `exact`, `zeroing`, `none` |
| `n_jobs` | 1 | estimator threads (kept at 1 by default for bit-reproducible fits) |

Variables with dims beyond `rows` become one column per label — `spend(store, channel, time)`
with rows `[store, time]` gives `spend[tv]`, `spend[search]`. Variables missing a row dim
(calendar features over `time` only) are broadcast. Rows with a missing target, a non-positive
weight, `mask` false or (for estimators without native missing-value support) a missing
feature are left out of training and reported in a `ROWS_DROPPED` diagnostic.

### Coordinate features

* `category` — integer codes declared natively to xgboost and to
  `HistGradientBoostingRegressor` (up to 254 levels); one-hot for every other estimator.
* `onehot` — one 0/1 column per fitted level.
* `ordinal` — the level's position as a number (for ordered dims).
* `numeric` — the label itself: numbers as-is, datetimes as days since the first fitted label.

## Estimators

| Kind | Task | Quantiles | Missing values | Categories | Stored as |
|---|---|---|---|---|---|
| `sklearn.LinearRegression` | regression | — | — | one-hot | skops |
| `sklearn.Ridge`, `sklearn.Lasso`, `sklearn.ElasticNet` | regression | — | — | one-hot | skops |
| `sklearn.QuantileRegressor` | regression | one model per level | — | one-hot | skops |
| `sklearn.RandomForestRegressor` | regression | — | — | one-hot | skops |
| `sklearn.GradientBoostingRegressor` | regression | one model per level | — | one-hot | skops |
| `sklearn.HistGradientBoostingRegressor` | regression | one model per level | native | native | skops |
| `xgboost.XGBRegressor` | regression | **one native multi-quantile model** | native | native | xgboost JSON |
| `sklearn.LogisticRegression`, `sklearn.RandomForestClassifier`, `xgboost.XGBClassifier` | binary classification | — | xgboost only | xgboost only | skops / JSON |

Every kind has a bounded Pydantic params model (`extra="forbid"`). Parameters the platform
owns — seeds, threads, categorical declarations, and the loss/objective an uncertainty kind
needs — are not settable; for example `sklearn.tabular_quantiles` sets xgboost's
`objective: reg:quantileerror` itself and rejects a conflicting one. Because the params
models are a discriminated union, the control plane validates them **statically**: a
`max_depth: -1` is rejected with a JSON pointer before any run starts.

## Uncertainty

**Quantiles.** xgboost fits all levels at once; quantile-capable sklearn estimators fit one
model per level. Crossing quantiles are sorted (monotone rearrangement, which never increases
pinball loss) and the fraction of crossing rows is reported as `QUANTILE_CROSSING`. When you
leave `output.summary` unset, the default (`median, q5, q95`) adapts to your grid — with
`[0.1, 0.5, 0.9]` you get `median, q10, q90`. Explicitly requested levels outside the grid
are an error: quantiles are interpolated, never extrapolated.

**Bootstrap.** `n` replicates refit the estimator on resampled rows — or, with `by: store`,
on resampled whole stores (a cluster bootstrap that keeps within-store dependence). `expected`
outputs are the replicate predictions; `predictive` outputs add an out-of-bag residual per
cell. Draws are joint, so totals over stores, counterfactual deltas and any summary are valid.

**Split conformal.** A `calibration` fraction is held out — by default the most recent weeks
(`calibration_by: last`), which mimics forecasting; `random` picks rows at random. The radius
is the `ceil((n+1)(1-alpha))`-th smallest absolute calibration residual; too small a
calibration set for the requested `alpha` is a `SETTINGS_INVALID` error rather than an
infinite interval. `refit: true` refits the point model on all rows afterwards (sharper, but
the finite-sample guarantee then holds only approximately).

## New stores, skus and markets

When score data contains a label of a row dim that was not seen at fit, the dim's effective
policy decides (the tighter of the data's policy and the plugin's `new_levels`):

* `pooled` — the prediction is the training-frequency-weighted average of the predictions the
  row would get under each fitted level. For a linear model with one-hot features this equals
  predicting with the average encoding; quantiles and interval bounds are averaged
  (Vincentisation); bootstrap draws are averaged per draw so they stay joint.
* `zero` — the level contributes nothing: all one-hot columns are 0, category codes are
  missing (estimators with native missing-value support).
* `error` (or a `fixed` dim) — `UNSEEN_LEVELS` before anything runs.

## Contributions

Point and bootstrap plugins can return `contributions(component, ...)` next to `y_hat`:

* **exact** (linear estimators; the default with `contributions: auto`) — `intercept` plus
  `coef × x` per column, with a coordinate feature's one-hot columns summed into one
  component. Components add up to the prediction exactly.
* **zeroing** (any estimator) — counterfactual zeroing: `baseline` is the prediction with every
  feature at zero, each feature's contribution is the drop when only it is zeroed, and
  `interaction` holds the remainder so the components still add up. These are labelled
  `counterfactual_zeroing` and are **not** Shapley values (SHAP is out of scope for v1).

Contributions are additive in **model space** (`output: {units: model}`). Through a non-linear
inverse such as `log1p` the per-component inverse is not a decomposition; request model units
when you want the parts to add up.

## Persistence and safety

Fitted estimators are stored in the bundle's `/native` group — never pickled:

* scikit-learn estimators with [skops](https://skops.readthedocs.io). Loading lists the
  file's types first and refuses anything outside that estimator's allow-list (for example a
  random forest may contain `sklearn.tree._tree.Tree`, a ridge regression nothing extra); the
  loaded object must be exactly the expected class.
* xgboost models as the booster's JSON document.

The bundle manifest records size, CRC32C, MD5 and SHA-256 of every file, and the contract kit
checks that no pickle-like entry exists.

## Write your own sklearn plugin

Implement `estimator()` and pick a base class for the representation you want:

```python
--8<-- "plugin_sklearn.py:model"
```

`estimator()` may return an allow-listed instance, a `{kind, params}` mapping or a choice
model; subclasses of estimators and parameters outside the params model are rejected. Base
classes: `SklearnModel` (point), `SklearnQuantileModel`, `SklearnBootstrapModel`,
`SklearnConformalModel`, `SklearnClassifier`.

Run the contract kit on synthetic data before you publish:

```python
--8<-- "plugin_sklearn.py:check"
```

## Limits in v1

* Forecasting is direct only: future features must be supplied or derived by the pipeline.
* Classification is binary.
* Row dims other than `time_dim` are declared categorical, so integer-labelled groups (numeric
  store ids) need string labels.
* Contributions are not available for quantile or conformal plugins.
