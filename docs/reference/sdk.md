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

1. Bind the dataset names to canonical names. Do a check of the data against its schema.
2. Run the stateless prefix of the pipeline on all the data, up to the split stage.
3. Split the data. Fit the remaining steps and the model on **train**.
4. Score the holdout **through the usual score path** (with context rows from train). Calculate
   `evaluate.metrics`.
5. Fit each step and the model again on **all** the data. Thus, forecasts start from the latest
   context tails and the latest terminal state. The lineage of the production bundle has a link
   to the evaluation bundle.

`train_only` stops after step 4 and keeps the fit on train.

## How the score works

| Phase | What occurs |
|---|---|
| CoordPlan | The SDK finds typed errors early from the metadata: `UNSEEN_LEVELS`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT` (409), `MISSING_FUTURE_COVARIATE`, `NON_CONTIGUOUS_EXTENSION`, `MODE_NOT_SUPPORTED`, ... |
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
* **Arrow IPC** (`to_arrow_table`, `write_arrow_ipc`): Tidy tables for UI views.

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
