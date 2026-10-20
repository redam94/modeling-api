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

## Run tracking <span class="mapi-badge">v0.11</span> { #run-tracking }

`FitContext`, `ScoreContext` and the `NodeContext` of workflow nodes have the same logging
methods (`mapi_sdk.tracking.TrackingMixin`):

| Method | What it does |
|---|---|
| `log_metric(key, value, step=None, labels=None)` | One metric point. Without `step`, the point is a scalar and the newest value is kept. |
| `log_metrics(values, step=None, labels=None)` | Some metrics with one step and one label set. `log_metrics({})` does nothing. |
| `log_param(key, value)`, `log_params(params)` | Params. The same value again does nothing. A different value raises `ValueError`. |
| `set_tag(key, value)`, `set_tags(tags)` | Tags. The last value is kept. |
| `log_line(message, level="info", fields=None)` | A structured log line (stream `user`). The SDK cuts the message at 4096 characters. A maximum of 16 fields. |
| `chains(chains)` | The progress of each chain, a list of `mapi_spec.run.ChainProgress` |
| `tracking` | `True` when a tracker records the values |
| `log_image(key, image, step=None, labels=None, caption=None)` | v0.14. An image: a numpy array (gray, RGB or RGBA), PNG or JPEG bytes, a Pillow image or a DataArray |
| `log_figure(key, figure, step=None, labels=None, caption=None)` | v0.14. A matplotlib figure, saved as PNG |
| `log_table(key, table, step=None, labels=None, caption=None)` | v0.14. A DataFrame, a dict of columns or a list of records |
| `tracking_media` | v0.14. `True` when the tracker records media (`MediaTracker`) |

Each method does a check of its input, also when no tracker is attached. A bad key, a bad
label, a negative step or a bad level raises `ValueError` at the call. The SDK ignores
non-finite metric values. The key and label rules are in `mapi_spec.tracking`
(`check_metric_key`, `check_labels`, `check_param_key`, `check_tag_key`).

The values go to a `Tracker` (`mapi_sdk.Tracker`, a protocol). All implementations are
thread-safe:

| Tracker | Use |
|---|---|
| `MetricRecorder(limits=None)` | Keeps all values in memory, with the budgets of `TrackingLimits`. `history()` gives the `metrics_history` dataset (or `None`). `params`, `tags`, `lines`, `last_chains` and `dropped` (the count for each reason) show what it kept and what it did not keep. |
| `TeeTracker(*trackers)` | Sends each call to all the trackers that are not `None` |
| `with_labels(tracker, labels, *, param_prefix="")` | Adds `labels` to each metric call. The labels of the call have priority. `param_prefix` goes before each param key (a key that becomes too long is not sent). |
| `WorkerTracker` (`mapi_runner`) | Inside a run: sends the values to the platform each second |

### Media <span class="mapi-badge">v0.14</span> { #media }

Media use a separate, optional protocol: `mapi_sdk.MediaTracker` (one method,
`log_media(item: MediaItem)`). A `Tracker` that does not have it does not get media. Thus
the trackers of earlier releases continue to work. `log_image`, `log_figure` and `log_table`
always do a check of the key, the labels, the step and the caption. They encode the item only
when the tracker is a `MediaTracker`.

| Name | What it does |
|---|---|
| `MediaItem` | A frozen item: `key`, `kind`, `content_type`, `data`, `sha256`, `step`, `labels`, `caption`, `width`, `height`. `name` is `<sha256>.<ext>`. |
| `encode_png(array)` | `(png_bytes, width, height)` of an array of shape `(H, W)`, `(H, W, 1)`, `(H, W, 3)` or `(H, W, 4)`. `uint8` as is, booleans as 0 and 255, other integers in 0 to 255, floats in 0 to 1 (scaled by 255, NaN becomes 0). Other values raise `ValueError`. The same array always gives the same bytes. It uses numpy and zlib only. |
| `mapi_sdk.media.image_item`, `figure_item`, `table_item` | Make a `MediaItem`. They raise `ValueError` for bad input or an item above the limits of `mapi_spec.media`. |

`MetricRecorder.media` keeps the items in memory, within `media_items` and `media_bytes`.
The items are not written into the bundle. `TeeTracker` and `with_labels` send media to the
trackers that are `MediaTracker`s (`with_labels` adds its labels, and the labels of the call
have priority).

The harness connects the trackers:

- `FitHooks(tracker=...)` gets the values of the fit and the params of the spec
  (`spec_params`: `model`, `fit.*`, `data.split`, `settings.*`). The values of the
  evaluation fit of `evaluate_then_refit` get the label `fit=evaluation`.
- `fit_spec(..., record_metrics=True)` (the default) also records each fit in a
  `MetricRecorder`, and writes `/model/metrics_history` into the bundle (see
  [Bundles](../concepts/bundles.md#metrics-history)). With `record_metrics=False`, the bundle
  has no such group.
- `score_bundle` gives `hooks.tracker` to `ScoreContext.tracker`.

```python
from mapi_sdk import FitHooks, MetricRecorder, fit_spec

rec = MetricRecorder()
bundle = fit_spec(spec, data, hooks=FitHooks(tracker=rec))
rec.params["model"]                         # 'mmm.bayesian@1.2.0'
bundle.model.groups["metrics_history"]      # when the model logged stepped metrics
```

For the full guide, see [Track metrics, params and tags](../guides/tracking.md).

## Image collections, loaders and weights <span class="mapi-badge">v0.15</span> { #collections }

`mapi_sdk.collection` reads image collections lazily. `mapi_sdk.images` decodes images.
`mapi_sdk.safetensors` and `mapi_sdk.checkpoints` save network weights. All of them are
exported from `mapi_sdk`.

| Name | What it does |
|---|---|
| `Collection` | A collection: `info` (`CollectionInfo`), `manifest` (a DataFrame, one row for each image), `source` (a `BlobSource`) and `schema`. `Collection.open(info, manifest, source)` validates the columns, the ids, the digests and the packs. `manifest_dataset()` gives the manifest as a 1-D dataset along the image dim. `loader(ids=None)` gives a `CollectionLoader`. |
| `ImageLoader` | The protocol of a loader: `n`, `dim`, `ids`, `labels`, `classes`, `size`, `channels`, `manifest()`, `subset()`, `with_classes()`, `with_size()`, `with_mode()`, `split()`, `batches()` and `to_xarray()` |
| `ArrayImageLoader` | An `ImageLoader` over an in-memory image dataset (`pixels(image, y, x, channel)`). Pixels can be `uint8`, or floats in 0 to 1 or 0 to 255. |
| `CollectionLoader` | An `ImageLoader` that reads the images from the packs while you iterate. Each read makes sure that the SHA-256 is correct. |
| `Batch` | `ids`, `pixels` (B, H, W, C) `uint8`, `labels`, `codes` (`int32`, −1 for a missing label or an unknown class) and `index` |
| `Augment` | Random changes for each training image: `flip_x`, `flip_y`, `rotate90`, `crop_pad`, `brightness`, `contrast`, `noise`. Models put it in their settings. |
| `BlobSource`, `LocalPackSource` | Where the packs are. The runner gives a source that reads the signed pack URLs in ranges. `LocalPackSource({digest: path})` reads local files. |
| `read_safetensors`, `write_safetensors` | Read and write safetensors files with numpy only. The writer gives the same bytes for the same tensors. |
| `weights_dataset`, `tensors_from_dataset` | Change tensors into the weights dataset (`weights(param)` and the attribute `mapi_weights`) and back |
| `FileCheckpoint`, `FileCheckpointSink` | Checkpoints as files (weights, optimizer state, meta). `MemoryFileCheckpointSink` keeps them in memory for tests. |
| `requested_positions(ds, dim)` | The positions of the rows that a score request asked for (it skips the padded rows of a `new_levels` dim) |

The order of `batches(batch_size, epoch=e, shuffle=True, seed=s)` is a function of the seed
and the epoch only. The loader reads blocks of up to 64 images of one pack, puts the blocks
in a random sequence and shuffles the images in windows of 8 blocks. The random changes of
`Augment` use a generator for each image, from the seed, the epoch and the position. Thus
the batches are the same for each number of threads. `shard=(i, n)` gives every n-th block
to shard i.

`to_xarray(max_samples, size, seed=..., stratify="label")` gives the v0.14 image layout:
`pixels(image, y, x, channel)` `uint8` with `label(image)`. When the loader has more than
`max_samples` images, it keeps a seeded share of each label.

```python
from mapi_sdk import Augment
from mapi_sdk.testing.images import local_collection

collection = local_collection(tmp_path, n_per_class=8, size=12)
loader = collection.loader().with_size((12, 12))
train, val = loader.split(0.25, seed=0)
for batch in train.batches(16, epoch=0, shuffle=True, seed=0, augment=Augment(flip_x=True)):
    print(batch.pixels.shape, batch.codes)
```

`fit_spec(spec, collection)` and `score_bundle(bundle, request, collection)` take a
`Collection` in place of a dataset. A model with `capabilities.input: collection` gets a
loader in `fit_batches` and `score_batches`. Any other model gets
`collection_read(collection, spec.data.collection, seed=...)`. Read
[Train on batches](../guides/plugins.md#fit-batches).

A model can read a side table (v0.15). `CustomModel.side_table_params` names the settings
keys whose value is a `data.side_tables` entry, for example the pretrained weights of
`dl.finetune_classifier`. The harness gives these tables, raw, in `FitContext.side_tables`.
A score request never needs a table that only the model reads. Read
[Read a side table in a model](../guides/plugins.md#model-side-tables).

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
