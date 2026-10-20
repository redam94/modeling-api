# Tabular models with scikit-learn and xgboost

`mapi-sklearn` changes each scikit-learn or xgboost estimator on the allow-list into a full
modeling-api plugin. You tell *which dims are samples* and *which variables are features*. The
platform does the rest:

- It stacks your labelled `xr.Dataset` into a design matrix.
- It fits the estimator and keeps it safely.
- At score time, it builds exactly the same columns again.
- It accepts new stores and forecast horizons.
- It unstacks the predictions back into labelled arrays, with correct uncertainty.

## The plugins

The kind of uncertainty sets the **representation** of the output. The representation then sets
which summaries and aggregations are valid. Each kind is a different plugin:

| Plugin | Representation | What `y_hat` contains | Aggregate over cells? |
|---|---|---|---|
| `sklearn.tabular_regressor` | point | A point prediction (+ `contributions`) | Linear only, with a flag |
| `sklearn.tabular_quantiles` | quantiles | Conditional quantiles `(quantile, ...)` | No |
| `sklearn.tabular_bootstrap` | draws | One joint draw for each bootstrap replicate `(chain, draw, ...)` | Yes, all |
| `sklearn.tabular_conformal` | interval | `(bound=[lower, upper, point], ...)`, attr `alpha` | No |
| `sklearn.tabular_classifier` | point | The probability of the positive class | Linear only, with a flag |

!!! note "Why conformal is an interval, not draws"
    A split-conformal interval guarantees the coverage *for each cell*
    (`P(y in interval) >= 1 - alpha`). It is not a distribution. A sum of intervals across
    stores or weeks claims a coverage that nobody measured. Thus the platform rejects these
    aggregations with `CAPABILITY_UNSUPPORTED`. For totals, use `sklearn.tabular_bootstrap`,
    because its draws are joint.

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

What occurs:

1. The platform scores the holdout (the last 12 weeks) **through the score path**. It uses
   context tails from the training weeks. It calculates `rmse`, `mape` and `coverage_80` in
   units sold. The platform inverts the `log1p`. Quantiles map exactly through monotone
   transforms.
2. The platform fits the model again on all weeks. The bundle keeps the xgboost model as JSON.
3. A forecast request gives the future prices and promotions. The platform builds the lags again
   from the stored tail. It also does a check that the weeks continue the fitted range without
   gaps.

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
| `estimator` | — | `{kind, params}` from the allow-list below (necessary for the built-in plugins) |
| `target` | `y` | The canonical name of the target variable |
| `rows` | — | The dims that the platform stacks into samples. Each row dim of the target. |
| `time_dim` / `time_kind` | `time` / `time` | The row dim that extends at forecast time, and its kind (`time`, `integer`, `index`). Use `none` if no row dim extends. |
| `columns` | `{roles: [covariate, media, spend, control, exposure]}` | The feature variables by role, or an explicit `vars` list. `exclude` removes names. |
| `coord_features` | `{}` | Encode the labels of row dims as features: `category`, `onehot`, `ordinal`, `numeric` |
| `new_levels` | `pooled` | What an unseen level of a row dim becomes: `pooled`, `zero` or `error` |
| `pooled_max_levels` | 256 | Pool over a maximum of this number of fitted levels (the most frequent) |
| `weight`, `mask` | — | A variable for the sample weight. A boolean mask of the cells that you can use. |
| `forecast_strategy` | `direct` | In v1, the platform rejects `recursive` with `RECURSIVE_UNSUPPORTED`. |
| `retransform` | `median` | Point predictions through a non-linear inverse (`log1p`): `median` or Duan `smearing` |
| `contributions` | `auto` | For the point and bootstrap plugins: `auto`, `exact`, `zeroing`, `none` |
| `n_jobs` | 1 | The threads of the estimator. The default is 1, so that fits are the same bit for bit. |

A variable with dims that are not in `rows` becomes one column for each label. For example,
`spend(store, channel, time)` with the rows `[store, time]` gives `spend[tv]`, `spend[search]`.
The platform broadcasts a variable that does not have a row dim (calendar features over `time`
only). The platform does not use some rows for the training:

- Rows with a missing target
- Rows with a weight that is zero or less
- Rows where `mask` is false
- Rows with a missing feature, for estimators that cannot accept missing values natively

A `ROWS_DROPPED` diagnostic reports these rows.

### Coordinate features

* `category`: integer codes. The platform declares them natively to xgboost and to
  `HistGradientBoostingRegressor` (a maximum of 254 levels). For all other estimators, it uses
  one-hot columns.
* `onehot`: one 0/1 column for each fitted level.
* `ordinal`: the position of the level as a number (for ordered dims).
* `numeric`: the label. Numbers stay as they are. Datetimes become the days since the first
  fitted label.

## Estimators

| Kind | Task | Quantiles | Missing values | Categories | Stored as |
|---|---|---|---|---|---|
| `sklearn.LinearRegression` | regression | — | — | one-hot | skops |
| `sklearn.Ridge`, `sklearn.Lasso`, `sklearn.ElasticNet` | regression | — | — | one-hot | skops |
| `sklearn.QuantileRegressor` | regression | one model for each level | — | one-hot | skops |
| `sklearn.RandomForestRegressor` | regression | — | — | one-hot | skops |
| `sklearn.GradientBoostingRegressor` | regression | one model for each level | — | one-hot | skops |
| `sklearn.HistGradientBoostingRegressor` | regression | one model for each level | native | native | skops |
| `xgboost.XGBRegressor` | regression | **one native multi-quantile model** | native | native | xgboost JSON |
| `sklearn.LogisticRegression`, `sklearn.RandomForestClassifier`, `xgboost.XGBClassifier` | binary classification | — | xgboost only | xgboost only | skops / JSON |

Each kind has a bounded Pydantic params model (`extra="forbid"`). You cannot set the parameters
that the platform owns: seeds, threads, categorical declarations, and the loss or objective that
a kind of uncertainty needs. For example, `sklearn.tabular_quantiles` sets the xgboost
`objective: reg:quantileerror`. It rejects an objective that does not agree. The params models
are a discriminated union. Thus the control plane validates them **statically**. For example, it
rejects `max_depth: -1` with a JSON pointer before a run starts.

## Uncertainty

**Quantiles.** xgboost fits all levels at the same time. The sklearn estimators that can do
quantiles fit one model for each level. The platform sorts quantiles that cross (monotone
rearrangement, which never increases the pinball loss). It reports the fraction of rows that
cross as `QUANTILE_CROSSING`. If you do not set `output.summary`, the default
(`median, q5, q95`) changes to agree with your grid. For example, with `[0.1, 0.5, 0.9]` you get
`median, q10, q90`. If you request a level outside the grid, you get an error. The platform
interpolates quantiles. It never extrapolates them.

**Bootstrap.** `n` replicates fit the estimator again on resampled rows. With `by: store`, they
fit it on resampled whole stores. This is a cluster bootstrap that keeps the dependence in each
store. `expected` outputs are the predictions of the replicates. `predictive` outputs add an
out-of-bag residual for each cell. The draws are joint. Thus totals over stores, counterfactual
deltas and all summaries are valid.

**Split conformal.** The platform keeps a `calibration` fraction out of the fit. By default,
this fraction is the most recent weeks (`calibration_by: last`), as in a forecast. `random`
selects rows at random. The radius is the `ceil((n+1)(1-alpha))`-th smallest absolute
calibration residual. The calibration set can be too small for the requested `alpha`. Then you
get a `SETTINGS_INVALID` error, not an infinite interval. `refit: true` fits the point model
again on all rows after the calibration. The interval is then narrower, but the finite-sample
guarantee is only approximate (not exact).

## New stores, skus and markets

Score data can contain a label of a row dim that the fit did not see. Then the effective policy
of the dim decides. This policy is the stricter of the policy of the data and the `new_levels`
of the plugin:

* `pooled`: the prediction is an average of the predictions that the row gets under each fitted
  level. The training frequency of each level is the weight. For a linear model with one-hot
  features, this is equal to a prediction with the average encoding. The platform calculates the
  average of quantiles and interval bounds (Vincentisation). It calculates the average of
  bootstrap draws for each draw, so that they stay joint.
* `zero`: the level adds nothing. All one-hot columns are 0. The category codes are missing (for
  estimators that accept missing values natively).
* `error` (or a `fixed` dim): `UNSEEN_LEVELS` before the run starts.

## Contributions

The point plugins and the bootstrap plugins can return `contributions(component, ...)` next to
`y_hat`:

* **exact** (linear estimators. This is the default with `contributions: auto`): `intercept`
  plus `coef × x` for each column. The platform adds the one-hot columns of a coordinate feature
  into one component. The sum of the components is exactly equal to the prediction.
* **zeroing** (all estimators): counterfactual zeroing. `baseline` is the prediction with all
  features at zero. The contribution of each feature is the decrease when only that feature is
  zero. `interaction` contains the remainder, so that the sum of the components stays correct.
  These contributions have the label `counterfactual_zeroing`. They are **not** Shapley values.
  SHAP is not in the scope of v1.

Contributions are additive in **model space** (`output: {units: model}`). Through a non-linear
inverse, for example `log1p`, the inverse of each component is not a decomposition. If you want
a sum of the parts that is correct, request model units.

## Default plots and metrics <span class="mapi-badge">v0.10</span> { #default-plots-and-metrics }

Each sklearn plugin now declares its standard plots. The fit run draws only the plots that the
spec lists in `outputs.plots`. Thus, add the plots that you want to `outputs.plots`. In the spec
editor, the **Insert** panel shows the plots of the model first.

```yaml
outputs:
  plots: [core.calibration, core.roc_pr, core.confusion, ml.feature_importance]
```

An evaluate run from the **Score** page does not need this list. It draws the plots for the shape
of each output: a classifier gets `core.calibration`, `core.roc_pr` and `core.confusion`.

| Plugins | Plots |
|---|---|
| Regressor, quantiles, bootstrap, conformal | `core.fit_vs_actual`, `core.residuals_over_time`, `ml.feature_importance` |
| Classifier | `core.calibration`, `core.roc_pr`, `core.confusion`, `ml.feature_importance` |

The three classifier plots read the probability in `y_hat` and the 0/1 target. They do not need
draws. `core.calibration` shows a reliability diagram: do the predicted probabilities agree
with how often the positive class occurs? `core.roc_pr` shows how well the model puts the
positive rows before the negative rows. `core.confusion` counts the errors at the threshold
0.5. Refer to [the plot guide](plots.md#plots-for-classifiers-training-and-states).

**Classifier metrics.** The fit pairs the probability in `y_hat` with the target. Thus
`evaluate.metrics` can include `auc`, `brier` and `accuracy`:

```yaml
evaluate: {metrics: [auc, brier, accuracy]}
```

Before v0.10, the fit calculated no metric for a classifier. `metrics.json` was empty. The
point metrics (`rmse`, `mae` and the other point metrics) use the probability too. Thus `rmse` is
the root of the Brier score. Refer to
[Metrics in `evaluate`](../reference/spec-format.md#evaluate-metrics) for the definitions.

!!! note "A known limit"
    Validation refuses `log_score` for the classifier, because the plugin gives point
    predictions. Use `brier`. A regression target that has only the values 0 and 1 also gets
    the classifier plots on the Score page.

## Persistence and safety

The bundle keeps the fitted estimators in its `/native` group. It never uses pickle:

* scikit-learn estimators with [skops](https://skops.readthedocs.io). Before the platform loads
  the file, it lists the types in the file. It refuses all types that are not on the allow-list
  of that estimator. For example, a random forest can contain `sklearn.tree._tree.Tree`. A ridge
  regression can contain no extra types. The loaded object must be exactly the expected class.
* xgboost models as the JSON document of the booster.

The bundle manifest records the size, CRC32C, MD5 and SHA-256 of each file. The contract kit
does a check that the bundle contains no entry that looks like pickle.

## Write your own sklearn plugin

Implement `estimator()`. Then select a base class for the representation that you want:

```python
--8<-- "plugin_sklearn.py:model"
```

`estimator()` can return an instance from the allow-list, a `{kind, params}` mapping or a choice
model. The platform rejects subclasses of estimators and parameters that are not in the params
model. The base classes are `SklearnModel` (point), `SklearnQuantileModel`,
`SklearnBootstrapModel`, `SklearnConformalModel` and `SklearnClassifier`.

Before you publish, run the contract kit on synthetic data:

```python
--8<-- "plugin_sklearn.py:check"
```

## Limits in v1

* Forecasts are direct only. You must give the future features, or the pipeline must calculate
  them.
* Classification is binary. The positive class is the larger target value.
* The platform declares all row dims other than `time_dim` as categorical. Thus groups with
  integer labels (numeric store ids) need string labels.
* Contributions are not available for the quantile plugins or the conformal plugins.
