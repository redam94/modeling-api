# Python SDK (`mapi-sdk`)

`mapi-sdk` is the runtime that operates on your arrays. It runs in runner images and in your
notebook. It never runs in the API server. It does all the work between a spec and a fitted
model that you can score:

* binding
* preprocessing with fitted state
* the train and holdout split
* the model plugin
* the coordinates at score time
* inverse transforms
* summaries and bundles

```bash
uv add mapi-sdk          # or: pip install mapi-sdk
```

## Fit and score in a notebook

The [first-model walkthrough](../getting-started/first-model.md) shows these steps one by one.
The short version is below. It uses `spec`, `data` and `schema` from that page:

```python
--8<-- "quickstart.py:fit"
```

```python
--8<-- "quickstart.py:forecast"
```

```python
--8<-- "quickstart.py:bundle"
```

For a model with the `draws` representation, add `draws: 50` to `output`. Then the tree also
has a `draws` group. This group has 50 joint draws from the thinning index of the bundle.

`score_bundle` returns an `xr.DataTree`. Its root attributes record these items:

* The representation.
* The output kind (`expected` or `predictive`).
* The **output space** of each variable, for example `weekly`. Inverse transforms stop at the
  last reshape step.
* The retransformation labels for point models.
* All warnings.

Counterfactual requests add `baseline/` and `delta/` groups.

## How the fit works

`fit.mode: evaluate_then_refit` (the default) does these steps:

1. Parse the settings of the model (`plugin.parse_settings`). A settings rule that the JSON
   Schema cannot give (a pydantic `model_validator`) refuses the fit here with
   `SETTINGS_INVALID`, before the SDK changes the data.
2. Bind the dataset names to canonical names. Do a check of the data against its schema.
3. Run the stateless prefix of the pipeline on all the data, up to the split stage.
4. Split the data. Fit the remaining steps and the model on **train**.
5. Score the holdout **through the usual score path** (with context rows from train). Calculate
   `evaluate.metrics`.
6. Fit each step and the model again on **all** the data. Thus, forecasts start from the latest
   context tails and the latest terminal state. The lineage of the production bundle has a link
   to the evaluation bundle.

`train_only` stops after step 5 and keeps the fit on train.

When `BindingMap` changes the name of a dim, it also changes that name in the `grid` attr of
each variable (the keys `y`, `x`, `frame` and `component`). Thus a plot recipe finds the grid
of an output after a `data.bind`.

## How the score works

| Phase | What occurs |
|---|---|
| Mode | `score_bundle` refuses a mode that the fitted model does not support (`MODE_NOT_SUPPORTED`, with the supported modes in `context`). This check comes before all work for a mode. For example, a forecast never starts on a model that cannot forecast |
| CoordPlan | The SDK finds typed errors early from the metadata: `UNSEEN_LEVELS`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT` (409), `MISSING_FUTURE_COVARIATE`, `NON_CONTIGUOUS_EXTENSION`, `MODE_NOT_SUPPORTED`, ... |
| Horizon | A forecast makes the labels of the horizon after the last fitted label. A horizon along a dim with text labels gives `COORD_NOT_ALLOWED` at `/horizon/dim`, because text labels have no sequence. The labels must be numbers or dates |
| Entry | The data goes in at `inputs_stage` (default `raw`). The SDK skips the steps before this stage and their side tables. |
| Replay | The SDK applies the stored state of each step. Some steps look back (lags). For these steps, the SDK first does a contiguity check. Then it puts the stored context rows of each group before the data. |
| Canonical coords | The SDK puts the model-stage data in the fit order and pads it to the full fit set. It adds new levels at the end with a `<dim>__is_new` flag. |
| Model | `plugin.score(...)` returns the `predictions` group in the representation of the plugin. |
| Back to you | The SDK removes the context rows and selects the requested coords. It inverts the outputs to the output space. It aggregates them (only if this is statistically valid) and makes summaries. Then it binds the names back to your dataset names. |

## Representations and summaries

| Representation | Shape | Summaries | Aggregation |
|---|---|---|---|
| `draws` | `(chain, draw, …)` | `mean`, `sd`, `median`, `hdi_<p>`, `eti_<p>`, `q<p>` | All (for each draw) |
| `quantiles` | `(quantile, …)` | `median`, `q<p>`, `eti_<p>` (interpolated, never extrapolated) | None |
| `interval` | `(bound, …)` | `interval`, `point` | None |
| `point` | `(…)` | `mean`, `point` | `sum`/`mean`, flagged |
| `moments` | `(moment, …)` | `mean`, `sd`, `eti_<p>` | None |

If you ask for something that is not valid, the request fails with `CAPABILITY_UNSUPPORTED`.
For example, you cannot add quantiles together, or get draws from a point model. The SDK does
not return a number that can cause an incorrect decision.

### Circular outputs

An output can be an angle, for example a direction or a time of day. The model declares it
with `OutputDecl.circular: true` (the default is `false`). It also sets a `period` attr (one
turn, in the units of the output, for example 360, 2π or 24) on the scored array.
`sci.circular_regression` declares `direction_hat` and `mean_direction` circular.

For the `draws` representation, `summarise(..., periods={var: period})` calculates the
summaries on the circle:

* `mean` is the circular mean. `sd` is the circular standard deviation, `sqrt(-2 ln R)`
  converted to the units of the output.
* `median`, `q<p>`, `eti_<p>` and `hdi_<p>` come from the wrapped differences between the
  draws and the circular mean. Thus each interval is centred on the circular mean.
* The mean, the median, the quantiles and the interval bounds are in `[0, period)`. An
  interval with `lower` more than `upper` crosses the seam, for example from 350 to 10
  degrees.
* The difference between a scenario and its baseline (`delta/summary`, `delta/draws`) is
  wrapped to `[-period/2, period/2)` (`signed=True`). Thus 10 − 350 degrees is 20, not −340.

The summary variable has the attr `circular_period` (`mapi_sdk.summarise.CIRCULAR_ATTR`).
If the output has no valid `period` attr, or if the request aggregates it over a dim, the
summary stays on a line. The result then has a `CAPABILITY_UNSUPPORTED` warning at
`/output/summary`.

The holdout metrics of a circular output use the same rules
(`compute_metrics(..., period=...)`):

* The point is the circular mean of the draws.
* `mae`, `rmse` and `bias` use the error wrapped to half a turn. Thus 359 degrees against 1
  degree is an error of −2 degrees, not 358.
* `coverage_<p>` and `interval_width_<p>` use central intervals of the draws around the
  circular mean.
* `crps` uses the arc distance.
* The SDK skips `mape`, `smape`, `wape` and `r2`, because they have no meaning on a circle.
  It also skips all metrics if the representation is not `draws` or the `period` is not
  valid. `skipped` gives the reason.

## Data on the wire

`mapi_sdk.wire` reads and writes the formats of the platform. The readers think that each
payload can be hostile.

* **XrJSON** (`to_xrjson` / `from_xrjson`): The `xr.Dataset.to_dict()` shape with explicit
  dtypes and `null` for missing values. It has limits on elements, dims and string length. Use
  it for small payloads and for curl.
* **zarr zip** (`write_zarr_zip` / `read_zarr_zip`, `write_datatree_zip` / `read_datatree_zip`):
  zarr v3 archives in one object. These rules apply:
    * Strings are `vlen-utf8`.
    * Datetimes are int64 nanoseconds with explicit units.
    * The reader accepts only `zstd`/`blosc` compression.
    * The writer resets MultiIndexes, and the reader restores them.
    * The bytes are deterministic.
* **Rows** (`stack_rows` / `unstack_rows`): These functions stack dims into rows and remove
  empty rows. Later, they restore the rows from the recorded row mask.
* **Arrow IPC** (`to_arrow_table`, `write_arrow_ipc`): Tidy tables for UI views. One column
  for each dim coordinate, then one column for each variable. The schema metadata
  `mapi.attrs` holds the attrs of the variables and of the dataset as JSON. The schema
  metadata `mapi.dims` holds the names of the dim columns as a JSON list. A reader uses it to
  find the key columns. The type of a column does not tell this, for example for a numeric
  dim such as a grid position in cm.

The SDK never uses pickle.

## Data checks in plain language

```python
--8<-- "concepts_data.py:infer"
```

```python
--8<-- "concepts_data.py:validate"
```

## Bundles

A bundle directory contains `bundle.zarr.zip` and the files `manifest.json`,
`diagnostics.json`, `plots.json` and `metrics.json`. `bundle.zarr.zip` contains these items:

* the spec, the schemas and the binding
* the state and the context of each step
* the groups of the model, with ArviZ names
* native artifacts
* the fit coordinates of each stage
* pinned side tables
* the thinning index

The manifest records CRC32C and MD5 (in the form that GCS reports) and SHA-256 for each file.
`Bundle.read` verifies them.

`metrics.json` is a `MetricsReport`. It has these fields:

| Field | Meaning |
|---|---|
| `values` | The holdout metrics. `null` if a metric has no finite value |
| `skipped` | The metrics that the SDK did not calculate, with the reason |
| `n_holdout` | The number of holdout values that count |
| `split`, `evaluation_bundle`, `output_space` | The split, the link to the evaluation bundle and the output space of the metrics |
| `elpd` | `{name: ElpdDetail}` for `elpd_loo` and `elpd_waic` (v0.9) |

The SDK calculates `elpd_loo` and `elpd_waic` only when `evaluate.metrics` lists them and the
bundle has a `log_likelihood` group. For example, a PyMC model needs
`fit.keep: {log_likelihood: true}`. An `ElpdDetail` (`mapi_sdk.bundle`) has these fields:

* `elpd`: the estimate.
* `se`: its standard error.
* `p`: the effective number of parameters.
* `n`: the number of data points.
* `pointwise`: the value for each point, in the order of the flattened `log_likelihood`
  variable. The field is `null` above 10 000 points (`mapi_sdk.metrics.MAX_POINTWISE_ELPD`).

The workflow node `compare.stacking` uses the pointwise values. If the estimate is not finite
(a log-likelihood of `-inf`), the SDK keeps no `elpd` entry for it. A bundle that has a `null`
entry from an earlier version still loads.

## Missing values by design

Some datasets have `NaN` that are correct, for example the cells of a grid that have no
sensor. A `CustomModel` lists these canonical variables in the class attribute
`structural_missing` (the default is empty). The scorecard check `data.missing` (Few missing
values) does not count their missing values. `sci.heat_plate` declares `temperature`.

The check also does not count the variables with the role `constant`, `index` or `mask`. The
message of the check names the variables that it does not count. Gaps in a target, a spend or
a covariate count as before, also with `missing: allow`.

## Run work with an executor

`InprocExecutor` implements the `Executor` protocol of the platform in your own process. Use it
for notebooks and tests. Never use it for untrusted code:

```python
--8<-- "executor.py:setup"
```

```python
--8<-- "executor.py:runs"
```

See [Runs and executors](../getting-started/executor.md).

## Error codes that you can see

Each error is a `MapiError` with a stable `code`. Where it is useful, the error also has a JSON
pointer. Text from your data or from a plugin goes in `untrusted_text`, separate from the
message. These are the error types:

* `StepError` (`USER_CODE_ERROR`) contains failures in steps.
* `SchemaValidationError` (`SCHEMA_INVALID`) reports problems in the data.
* `BundleError` (`INTEGRITY_FAILURE`) reports bundles that are damaged or that someone changed.
* `RunCancelled` (`CANCELLED`) reports a cooperative cancellation.
