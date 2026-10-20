# Spec format reference

Each model on modeling-api starts as a **ModelSpec**. This is a small YAML (or JSON) file. It
gives your dataset, the preprocessing pipeline and the model plugin. It also tells how to fit
and evaluate the model. The Guided wizard, the Expert editor, the CLI and the API all use the
same file.

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: eu-geo-mmm, project: growth}

data:
  dataset: dataset://growth/eu_media_daily@latest
  side_tables:
    fx: {ref: dataset://growth/fx_daily@latest, score_policy: supplied}
  bind:
    dims: {geo: market, channel: channel, time: date}   # canonical name: dataset name
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 8, after_step: weekly}

pipeline:
  - {id: to_eur, use: core.fx_convert@^1, with: {vars: [y, spend], table: fx, to: EUR}}
  - {id: weekly, use: core.resample@^1,   with: {dim: time, freq: W-SUN, how: sum}}

model:
  use: mmm.bayesian@^1              # mmm-framework scales and carries spend over itself
  settings:
    hierarchy: {pool_over: [geo]}
    dim_policies: {geo: fixed}      # may only tighten the plugin's policy
  fit: {mode: evaluate_then_refit, draws: 1000, chains: 4, seed: 7}

evaluate: {metrics: [elpd_loo, coverage_90, mape]}
execution: {resource_class: M, timeout: 45m}
```

## Sections

| Section | Purpose |
|---|---|
| `metadata` | `name` and `project` (lowercase slugs). Optional: `labels` and `description` |
| `data.dataset` | `dataset://<project>/<name>@latest|vN|sha256:<hex>` — The API pins it to an exact version when you submit |
| `data.side_tables` | Small constant datasets (FX, holidays). `score_policy` is `pinned`, `latest` or `supplied`. A side table that is a tree dataset reads one node: `node: /fx` (v0.8). From v0.15 a model can read a side table too (`side_table_params` in its manifest, for example the pretrained weights of `dl.finetune_classifier`). A score request never needs a table that only the model reads |
| `data.nodes` | v0.8, tree datasets only. The nodes whose variables make the flat input of the pipeline: `{key: {path, vars?, prefix?}}`, a maximum of 64 entries. Refer to [Tree datasets in a spec](#tree-datasets-in-a-spec) |
| `data.join` | v0.8, used only with `data.nodes` (a value other than `exact` without them is an error). How the labels of the selected nodes align: `exact` (the default), `inner` or `outer` |
| `data.bind` | Maps **canonical** names (the names that plugins and steps use) to **dataset** names. By default, the outputs get the dataset names again. When a dim gets a new name, the `grid` attr of each variable gets the new name too (v0.9) |
| `data.split` | `time_holdout`, `last_per_group`, `as_of`, `leave_group_out`, `mask_block`, and from v0.14 `column` or `stratified` (see [Column and stratified splits](#column-and-stratified-splits)). The split occurs at `after_step` |
| `data.collection` | v0.15, image collections only. How a model that reads datasets sees the collection: a seeded sample as `pixels(image, y, x, channel)`. Refer to [Image collections in a spec](#image-collections-in-a-spec) |
| `pipeline` | Steps in sequence: `{id, use: <id>@<semver range>, with: {...}}` |
| `model` | The plugin `use`, its `settings` and the `fit` block |
| `outputs` | Derived quantities, plot recipes and the number of draws that the UI keeps |
| `evaluate` | Holdout metrics (`elpd_loo`, `coverage_90`, `mape`, `rmse`, `auc`, `mase` and [more](#evaluate-metrics)). `elpd_loo` and `elpd_waic` need a `log_likelihood` group, for example `model.fit.keep: {log_likelihood: true}`. If not, `metrics.json` gives the reason in `skipped`. A [circular output](sdk.md#circular-outputs) gets circular metrics and skips `mape`, `smape`, `wape` and `r2` |
| `execution` | The resource class (`S`, `M`, `L`, `XL`, and from v0.15 `GPU`) and the timeout (`45m`, `1h30m`). Each class has a limit for the input data that a run reads (dataset plus side tables): S 0.5 GB, M 2 GB, L 5 GB, XL 50 GB, GPU 50 GB. If the data is larger, validation reports `PAYLOAD_TOO_LARGE` at `/execution/resource_class`. The message gives the class that fits. Ingest runs select the smallest CPU class that holds the upload. An image collection is read in parts while the model trains, so its size does not count. Refer to [Resource classes](#resource-classes) |

## Metrics in `evaluate` <span class="mapi-badge">v0.10</span> { #evaluate-metrics }

`evaluate.metrics` names the metrics that the fit calculates on the holdout data. The fit
writes them in `metrics.json`. If the fit cannot calculate a metric, `metrics.json` gives the
metric and the reason in `skipped`. The fit does not fail.

| Metric | What it measures | Needs |
|---|---|---|
| `mape`, `smape`, `wape`, `rmse`, `mae`, `r2`, `bias` | The error of the point prediction (the mean or the median) | Actual values on the holdout |
| `mase` | `mae` divided by the mean absolute error of a seasonal naive forecast on the training data | A fit with a time holdout. The season length comes from the frequency of the time dim. |
| `crps`, `coverage_N`, `interval_width_N`, `log_score` | The quality of the predicted distribution | Draws, quantiles or intervals |
| `elpd_loo`, `elpd_waic` | The expected log predictive density | A `log_likelihood` group |
| `auc`, `brier`, `log_score`, `accuracy` | The quality of a 0/1 prediction (see below) | A 0/1 target and a predicted probability |
| `accuracy_naive`, `accuracy_lift` | The accuracy of the most frequent class, and the gain of the model over it | A categorical target |

**Targets with two values.** If the target has exactly two values and the prediction is a
probability from 0 to 1, the fit uses these definitions. The larger target value is the
positive class.

| Metric | Definition |
|---|---|
| `auc` | The area under the ROC curve (the Mann-Whitney statistic, with the average rank for ties) |
| `brier` | `mean((p - y)^2)`. For two classes, the categorical Brier score is two times larger, because it adds the error of the two classes. |
| `log_score` | `mean(log(p_y))`, where `p_y` is the probability that the model gave to the actual class (minimum 1e-12) |
| `accuracy` | The share of rows where `p >= 0.5` agrees with the target |

A prediction with draws uses the mean of the draws as `p`. A classifier that gives one
probability for each row (for example `sklearn.tabular_classifier`) also gets `rmse`, `mae` and
the other point metrics on the probability. `rmse` is then the root of the Brier score.

**Reasons in `skipped`.** `metrics.json` gives these texts:

- `auc` on a target that is not 0/1: "AUC needs a 0/1 target and predicted probabilities in
  [0, 1]".
- `auc` when the holdout has only one of the two classes: "AUC needs both classes in the
  holdout".
- `mase` without the training data: "MASE needs the in-sample naive error of the training data
  (a fit with a time holdout)". Only the evaluation of a fit has the training data. Thus an
  evaluate run from the **Score** page skips `mase`.
- `brier`, `log_score` and `accuracy` on a target that is not 0/1 or categorical: "defined for
  categorical targets only".

!!! note "A known limit"
    Validation refuses `log_score` for a model that gives only point predictions, because the
    metric usually needs uncertainty. Thus a point classifier cannot request `log_score` in
    v0.10. Use `brier` or `auc`.

## Tree datasets in a spec <span class="mapi-badge">v0.8</span> { #tree-datasets-in-a-spec }

A [tree dataset](../guides/tree-data.md) has nodes, for example `/sales` and `/media`. Steps
and most models read one flat dataset. `data.nodes` selects the nodes, and the platform
merges their variables before the pipeline:

```yaml
--8<-- "tree_spec.yaml"
```

!!! note "The model of this example"
    `toy.tree_linear` is a reference model of the SDK test kit (`mapi_sdk.testing.toy`). The
    documentation tests use it to check and fit this spec. The platform and the CLI catalog do
    not have it, so `mapi check` reports `REF_UNRESOLVED` for it. Put a model of your platform
    in `model.use` (`mapi catalog` lists them).

| Field of an entry | Meaning |
|---|---|
| `path` | The node path (`/` is the root) |
| `vars` | The variables to read from the node. Default: all |
| `prefix` | Text before each variable name of the node (1 to 32 characters, starts with a letter or `_`). Use it when two nodes have variables with the same name |

- Dims keep their names. A dim that two selected nodes use must have the same `DimSpec`.
- A variable name can come from one entry only. Validation reports a clash and tells you to
  add a `prefix`.
- `data.bind` and the pipeline use the merged names (for example `plan_spend`).
- Without `data.nodes`, a spec on a tree dataset reads the root node. If the root has no
  variables, validation reports `SCHEMA_INVALID` ("this dataset is a tree: pick nodes in
  data.nodes").
- `data.nodes` on a plain dataset is an error. So is a `data.join` other than `exact` without
  `data.nodes` (`exact` is the default and has no effect without nodes).

Static validation uses the `tree_schema` of the dataset version. Thus it finds these problems
before a run starts.

## Column and stratified splits <span class="mapi-badge">v0.14</span> { #column-and-stratified-splits }

These two split kinds are for data without a time order, for example images or customers.
Both cut whole rows along `dim`. They do not mask cells.

```yaml
split: {kind: column, dim: image, column: split, holdout: [test]}
split: {kind: column, dim: image, column: split, holdout: [test], train: [train]}
split: {kind: stratified, dim: image, by: label, fraction: 0.25, seed: 7}
```

| Kind | Field | Meaning |
|---|---|---|
| `column` | `dim` | The dim of the rows |
| | `column` | A coordinate or a 1-D variable along `dim` with the assignment of each row |
| | `holdout` | The values of the holdout rows (default `[test]`, a maximum of 64). The values compare as text: `1` and `"1"` are the same. |
| | `train` | The values of the training rows (a maximum of 64). Rows with a value in neither list are not used. The default (none) trains on all other rows. A value cannot be in `holdout` and in `train`. |
| `stratified` | `dim` | The dim of the rows |
| | `by` | A coordinate or a 1-D variable along `dim` with the class of each row |
| | `fraction` | The share of each class to hold out (more than 0, less than 1) |
| | `seed` | The random seed (default: the seed of the fit) |

`stratified` holds out `round(n × fraction)` rows of each class, with a maximum of n − 1. A
class with one row stays in training. Rows with an empty or missing class stay in training.
The same seed gives the same split. If the split gives no holdout rows or no training rows,
the fit stops with an error.

Validation does a check of `column` and `by` when the schema of the split stage has the
variable: it must be on `dim` only (`SPLIT_INVALID`). A coordinate that is not a dim is not
in the schema. Then validation gives an information message, and the fit does the check.

Do not use these splits for a time series forecast. Use `time_holdout` or `as_of`.

## Image folders <span class="mapi-badge">v0.14</span> { #image-folders }

An upload of the format `image_folder` is a zip of images and a manifest table. The mapping
has only `format` and `images` (an `ImageFolderOptions`), not `dims`, `vars`, `wide` or a
preset. `images` can be left out: then the defaults apply. An `image_folder` upload must be the
only source of a dataset version.

```yaml
format: image_folder
images:
  manifest: manifest.csv        # .csv, .tsv or .parquet, a relative path in the zip
  path_column: path
  id_column: id                 # optional: unique labels of the image dim
  label_column: label           # optional: the variable label(image), role target
  split_column: split           # optional: the coordinate split(image)
  columns: null                 # the other columns to keep (null: all)
  var: pixels
  dim: image                    # not sample, chain, draw, quantile, bound, moment, y, x or channel
  mode: auto                    # auto, gray or rgb
  size: [32, 32]                # optional (height, width), each 1 to 4096
  resize: crop                  # stretch, crop or pad (only with size)
  max_images: 20000             # 1 to 100 000
```

The ingest writes `pixels(image, y, x, channel)` with the dtype `uint8`. `uint8` is a numeric
dtype of the schema (v0.14), for values from 0 to 255. For the full layout and the limits,
see [Image data](../guides/image-data.md).

## Resource classes { #resource-classes }

| Class | vCPU | Memory | Scratch | Input data | Bundle | Accelerator |
|---|---|---|---|---|---|---|
| `S` | 2 | 8 GiB | 3 GiB | 0.5 GB | 1 GB | — |
| `M` | 4 | 16 GiB | 6 GiB | 2 GB | 4 GB | — |
| `L` | 8 | 32 GiB | 12 GiB | 5 GB | 8 GB | — |
| `XL` | (GKE) | (GKE) | (GKE) | 50 GB | 50 GB | — |
| `GPU` (v0.15) | 8 | 32 GiB | 12 GiB | 50 GB | 8 GB | 1 NVIDIA L4 |

The classes are in this sequence: `S < M < L < XL < GPU`. A quota with a maximum class below
`GPU` refuses the GPU class. After `RESOURCE_EXHAUSTED`, the platform offers the next CPU
class only (the GPU class is never offered). Validation refuses a spec that selects `GPU`
for a model that does not use a GPU (`resources.gpu` in its manifest). It shows a note
when a model can use the GPU and the spec selects a CPU class. Refer to
[Deep learning](../guides/deep-learning.md#use-the-gpu-class).

## Checkpoints { #checkpoints }

`model.fit.checkpoint` saves resumable checkpoints during a fit. Give exactly one of:

| Field | For | Meaning |
|---|---|---|
| `every_draws` | Samplers (PyMC) | Save a checkpoint every N draws (1 to 1 000 000) |
| `every_epochs` | Networks (v0.15, `dl.*`) | Save a checkpoint every N epochs (1 to 10 000) |

```yaml
model:
  fit:
    checkpoint: {every_epochs: 2}
```

If the model cannot save checkpoints (`capabilities.checkpointable` is `false`), validation
shows a warning at `/model/fit/checkpoint/every_epochs` and the run ignores the setting.

## Image collections in a spec <span class="mapi-badge">v0.15</span> { #image-collections-in-a-spec }

An image collection is a dataset version with the layout `collection` (see
[Image data](../guides/image-data.md#large-image-sets-collections)). A model reads it in one of
two ways:

- A model with `capabilities.input: collection` (`dl.cnn_classifier`,
  `dl.finetune_classifier`) trains on batches of all images. The spec takes no pipeline
  steps (`SCHEMA_INVALID` at `/pipeline`).
- Any other model reads a seeded sample as `pixels(image, y, x, channel)`, as set by
  `data.collection` (a `CollectionRead`). Validation shows a note at `/data/collection`.

```yaml
data:
  dataset: dataset://default/product_photos@v5
  collection:
    max_samples: 5000   # 1 to 100 000
    size: [32, 32]      # (height, width); default: the size of the collection
    resize: stretch     # stretch, crop or pad
    mode: auto          # auto, gray or rgb
    stratify: label     # a variable or coordinate along the image dim; null: uniform
    seed: 7             # default: the fit seed
```

When the collection has no common size and `size` is not set, the run fails with
`SCHEMA_INVALID` at `/data/collection/size`. `data.collection` on a dataset that is not a
collection gives a warning, and the run ignores it.

The other rules for a collection:

- `data.split` must be a row split: `column`, `stratified` or `leave_group_out`
  (`SPLIT_INVALID` at `/data/split/kind`).
- A side table must be a dataset, not a collection.

### Collection options

The options of a collection version (`CollectionOptions`, the `options` of
`DatasetVersionCreate.collection`):

| Option | Default | Meaning |
|---|---|---|
| `sha256_column` | `sha256` | The column with the lowercase hex SHA-256 of each image |
| `id_column` | none | Unique ids of the image dim. Default: the path, else the SHA-256. |
| `path_column` | `path` | Kept as the coordinate `path`. A missing default column is ignored. |
| `label_column` | none | The class: the text variable `label(image)`, role target |
| `split_column` | none | The text coordinate `split(image)` |
| `columns` | all others | The other columns to keep as coordinates (a maximum of 64). They cannot use the names of the fixed columns of `manifest.parquet` (`image_id`, `sha256`, `pack`, `offset`, `stored`, `method`, `crc32`, `nbytes`, `format`, `width`, `height`, `mode`). |
| `var`, `dim` | `pixels`, `image` | The names of the virtual pixel variable and of the image dim |
| `mode` | `auto` | `gray`, `rgb` or `auto` (rgb when an image has colour) |
| `size` | none | The size (height, width) for samples and model defaults, each 1 to 4096 |
| `resize` | `stretch` | `stretch`, `crop` or `pad` |
| `max_images` | 100 000 | 1 to 1 000 000 |

## Pretrained weights: the `safetensors` format <span class="mapi-badge">v0.15</span> { #safetensors }

An upload of the format `safetensors` is a file of network weights. The mapping has only
`format` and, optionally, `weights` (a `WeightsOptions`). A `safetensors` upload must be the
only source of a dataset version.

```yaml
format: safetensors
weights:
  arch: {kind: resnet, channels: 3, widths: [16, 32, 64], blocks: [1, 1, 1]}  # optional
  max_parameters: 200000000   # 1 to 500 000 000
```

`arch` is necessary only when the `__metadata__` of the file has no `mapi_arch` key. Its JSON
can have a maximum of 64 KiB. The ingest writes a weights dataset:

| Name | Dims | Type | Contents |
|---|---|---|---|
| `weights` | `param` | `float32` | All tensors, flattened, role `constant` |
| attribute `mapi_weights` | | JSON | `tensors` (name, shape, source dtype, offset), `metadata`, `arch`, `sha256` of the file and `parameters` |

The ingest accepts the dtypes F64, F32, F16, BF16, I64, I32, I16, I8, U8 and BOOL, a
maximum of 4096 tensors and a header of a maximum of 8 MiB. Use the weights dataset as a
side table of a model or a step (see
[Deep learning](../guides/deep-learning.md#pretrained-weights)).

## Retired documents

The `Sweep` document is retired in v0.14. Stored `Sweep` documents still load, but the server
does not accept new ones (`422 SCHEMA_INVALID` at `/kind`). Use a workflow with a
`search.grid` node.

## Versions in `use:`

`use:` takes a semver range in the npm style: `^1` (all 1.x), `~1.2` (1.2.x), `>=1.2 <2`,
`1.x`, an exact `1.2.3`, or `*`. When you submit, the API selects the highest version that
matches and is not yanked. The run records this version. Thus, you can run a revision again
and get the same result.

## Where the split happens

Some dims exist only after preprocessing. For example, a resample step can change daily data
to weeks. Thus, the split occurs at a named **stage** (`after_step`). If you do not give a
stage, the split occurs immediately after the first steps that learn nothing from the data.
Examples are unit conversion and resample steps. A step that *learns* from the data (scale,
encode, seasonality fit) can run before the split. Then validation reports **`LEAKAGE_RISK`**,
because the step learns its state from the holdout.

## Validation messages

`mapi check` and `POST /v1/specs:validate` return all the problems at the same time.
Each problem has a stable `code` and a JSON pointer to the incorrect field:

```json
{"code": "SPLIT_INVALID", "pointer": "/data/split/dim",
 "message": "split dim 'week' does not exist at stage 'raw'",
 "suggested_action": "Set data.split.after_step to the stage where the dim exists (e.g. after a resample step)."}
```

| Code | Meaning |
|---|---|
| `SCHEMA_INVALID` | A field has the wrong shape, or a step or model needs data that the pipeline does not make |
| `SPEC_PARSE_ERROR` | The text is not valid YAML/JSON, or it uses anchors, aliases or tags (not permitted) |
| `PAYLOAD_TOO_LARGE` | More than 256 KB, more than 32 levels of nesting, or more than 10 000 values |
| `REF_UNRESOLVED` | The step, model, plot, side table or derived quantity is unknown, or no version matches |
| `SETTINGS_INVALID` | The settings do not agree with the settings schema of the step or model |
| `BINDING_INVALID` | `data.bind` gives a name that the dataset does not have |
| `SPLIT_INVALID` | The split dim does not exist at the split stage, or the `column` or `by` variable of a split is not on the split dim |
| `LEAKAGE_RISK` | A stateful step runs before the split |
| `DIM_POLICY_LOOSENED` | `dim_policies` tries to make the dim policy of a plugin less strict |
| `RECURSIVE_UNSUPPORTED` | A lag of the target is shorter than the holdout/horizon |

## Score requests

```yaml
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://growth/eu-geo-mmm-prod
mode: forecast                    # predict | forecast | counterfactual | simulate | filter | evaluate
horizon: {dim: time, periods: 26}
inputs_stage: scale_spend         # supply weekly EUR spend directly; earlier steps are skipped
scenario: dataset://growth/plan_2027h1
interventions: [{var: spend, sel: {channel: meta}, multiply: 1.2}]
output: {kind: expected, summary: [mean, hdi_90], draws: 200}
```

Before something runs, the API does a check of each score request against the fitted model.
It finds these problems:

* A mode that the model does not support (`MODE_NOT_SUPPORTED`). The runner also does this
  check first, before all work for the mode.
* A request without data: no `data`, no inline values, no `scenario` and no `horizon`
  (`SCHEMA_INVALID` at `/data`, "the score request has no data").
* Unseen levels on fixed dims (`UNSEEN_LEVELS`).
* A horizon along a dim that is not a model dim, or that does not have the policy `extend`
  (`COORD_NOT_ALLOWED`).
* Horizons that are longer than the fit can support (`HORIZON_EXCEEDS_FIT`, HTTP 409). You must
  fit the model again.
* Missing future values of covariates (`MISSING_FUTURE_COVARIATE`).
* Gaps after the fitted range (`NON_CONTIGUOUS_EXTENSION`).
* Summaries that the uncertainty representation of the model cannot support
  (`CAPABILITY_UNSUPPORTED`). An example is the sum of quantiles.

The runner does more checks when the run starts. For example, a horizon along a dim with
text labels gives `COORD_NOT_ALLOWED`, because text labels have no sequence.

The summaries of a circular output (`OutputDecl.circular`, for example a wind direction) are
on the circle: the circular mean, and intervals centred on it in `[0, period)`. In
`summary.json`, the table of such an output has the field `period`. An interval with `lower`
more than `upper` crosses the seam. Refer to [Circular outputs](sdk.md#circular-outputs).

## JSON Schemas

Editors can use the published JSON Schemas to validate specs while you type
(`GET /v1/schemas/modelspec.json`, or `python -m mapi_spec export-schemas ./schemas`).
