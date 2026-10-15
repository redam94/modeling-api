# Python SDK (`mapi-sdk`)

`mapi-sdk` is the runtime that actually touches your arrays. It runs inside runner images and
in your notebook — never in the API server — and does everything between a spec and a fitted,
scoreable model: binding, preprocessing with fitted state, the train/holdout split, the model
plugin, coordinate handling at score time, inverse transforms, summaries and bundles.

```bash
uv add mapi-sdk          # or: pip install mapi-sdk
```

## Fit and score in a notebook

The [first-model walkthrough](../getting-started/first-model.md) builds this step by step. In
short, with `spec`, `data` and `schema` from there:

```python
--8<-- "quickstart.py:fit"
```

```python
--8<-- "quickstart.py:forecast"
```

```python
--8<-- "quickstart.py:bundle"
```

For a model whose representation is `draws`, add `draws: 50` to `output` and the tree also has
a `draws` group: 50 joint draws taken from the bundle-wide thinning index.

`score_bundle` returns an `xr.DataTree`. Its root attributes record the representation, the
output kind (`expected` or `predictive`), the **output space** of every variable (for example
`weekly` — inverse transforms stop at the last reshape step), retransformation labels for point
models, and any warnings. Counterfactual requests add `baseline/` and `delta/` groups.

## How fitting works

`fit.mode: evaluate_then_refit` (the default) does this:

1. Bind dataset names to canonical names and check the data against its schema.
2. Run the stateless prefix of the pipeline on all data, up to the split stage.
3. Split, fit the remaining steps and the model on **train**.
4. Score the holdout **through the normal score path** (with context rows from train) and
   compute `evaluate.metrics`.
5. Refit every step and the model on **all** data, so the context tails and terminal state
   that forecasts start from are the latest ones. The production bundle links the evaluation
   bundle in its lineage.

`train_only` stops after step 4 and keeps the train fit.

## How scoring works

| Phase | What happens |
|---|---|
| CoordPlan | Typed early errors from metadata: `UNSEEN_LEVELS`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT` (409), `MISSING_FUTURE_COVARIATE`, `NON_CONTIGUOUS_EXTENSION`, `MODE_NOT_SUPPORTED`, ... |
| Entry | Data enters at `inputs_stage` (default `raw`). Steps before it — and their side tables — are skipped. |
| Replay | Each step's stored state is applied; steps with a look-back (lags) get their stored per-group context rows prepended first, after a contiguity check. |
| Canonical coords | Model-stage data is reordered to the fit order, padded to the full fit set, and new levels are appended with a `<dim>__is_new` flag. |
| Model | `plugin.score(...)` returns the `predictions` group in the plugin's representation. |
| Back to you | Context rows are dropped, requested coords selected, outputs inverted to the output space, aggregated (only if statistically valid), summarised and reverse-bound to your dataset names. |

## Representations and summaries

| Representation | Shape | Summaries | Aggregation |
|---|---|---|---|
| `draws` | `(chain, draw, …)` | `mean`, `sd`, `median`, `hdi_<p>`, `eti_<p>`, `q<p>` | any (per draw) |
| `quantiles` | `(quantile, …)` | `median`, `q<p>`, `eti_<p>` (interpolated, never extrapolated) | none |
| `interval` | `(bound, …)` | `interval`, `point` | none |
| `point` | `(…)` | `mean`, `point` | `sum`/`mean`, flagged |
| `moments` | `(moment, …)` | `mean`, `sd`, `eti_<p>` | none |

Asking for something invalid — summing quantiles, draws from a point model — fails with
`CAPABILITY_UNSUPPORTED` instead of returning a misleading number.

## Data on the wire

`mapi_sdk.wire` reads and writes the platform's formats. Readers treat every payload as hostile.

* **XrJSON** (`to_xrjson` / `from_xrjson`): the `xr.Dataset.to_dict()` shape with explicit
  dtypes, `null` for missing values and caps on elements, dims and string length. Good for
  small payloads and curl.
* **zarr zip** (`write_zarr_zip` / `read_zarr_zip`, `write_datatree_zip` / `read_datatree_zip`):
  single-object zarr v3 archives. Strings are `vlen-utf8`, datetimes are int64 nanoseconds
  with explicit units, only `zstd`/`blosc` compression is accepted, MultiIndexes are reset and
  restored. The bytes are deterministic.
* **Rows** (`stack_rows` / `unstack_rows`): stack dims into rows, drop empty rows, and restore
  them later from the recorded row mask.
* **Arrow IPC** (`to_arrow_table`, `write_arrow_ipc`): tidy tables for UI views.

Pickle is never used, anywhere.

## Data checks in plain language

```python
--8<-- "concepts_data.py:infer"
```

```python
--8<-- "concepts_data.py:validate"
```

## Bundles

A bundle directory holds `bundle.zarr.zip` (spec, schemas, binding, every step's state and
context, the model's ArviZ-named groups, native artifacts, fit coordinates per stage, pinned
side tables and the thinning index) plus `manifest.json`, `diagnostics.json`, `plots.json` and
`metrics.json`. The manifest records CRC32C and MD5 (in the form GCS reports) and SHA-256 for
every file; `Bundle.read` verifies them.

## Running work through an executor

`InprocExecutor` implements the platform's `Executor` protocol in your own process — handy for
notebooks and tests, never for untrusted code:

```python
--8<-- "executor.py:setup"
```

```python
--8<-- "executor.py:runs"
```

See [Runs and executors](../getting-started/executor.md).

## Error codes you may see

Every error is a `MapiError` with a stable `code` and, where useful, a JSON pointer.
Anything your data or a plugin said is kept in `untrusted_text`, separate from the message.
`StepError` (`USER_CODE_ERROR`) wraps failures inside steps, `SchemaValidationError`
(`SCHEMA_INVALID`) reports data problems, `BundleError` (`INTEGRITY_FAILURE`) reports damaged
or tampered bundles and `RunCancelled` (`CANCELLED`) reports cooperative cancellation.
