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
| `data.side_tables` | Small constant datasets (FX, holidays). `score_policy` is `pinned`, `latest` or `supplied`. A side table that is a tree dataset reads one node: `node: /fx` (v0.8) |
| `data.nodes` | v0.8, tree datasets only. The nodes whose variables make the flat input of the pipeline: `{key: {path, vars?, prefix?}}`, a maximum of 64 entries. Refer to [Tree datasets in a spec](#tree-datasets-in-a-spec) |
| `data.join` | v0.8, only with `data.nodes`. How the labels of the selected nodes align: `exact` (the default), `inner` or `outer` |
| `data.bind` | Maps **canonical** names (the names that plugins and steps use) to **dataset** names. By default, the outputs get the dataset names again |
| `data.split` | `time_holdout`, `last_per_group`, `as_of`, `leave_group_out` or `mask_block`. The split occurs at `after_step` |
| `pipeline` | Steps in sequence: `{id, use: <id>@<semver range>, with: {...}}` |
| `model` | The plugin `use`, its `settings` and the `fit` block |
| `outputs` | Derived quantities, plot recipes and the number of draws that the UI keeps |
| `evaluate` | Holdout metrics (`elpd_loo`, `coverage_90`, `mape`, `rmse`, ...) |
| `execution` | The resource class (`S`, `M`, `L`, `XL`) and the timeout (`45m`, `1h30m`). Each class has a limit for the input data that a run reads (dataset plus side tables): S 0.5 GB, M 2 GB, L 5 GB, XL 50 GB. If the data is larger, validation reports `PAYLOAD_TOO_LARGE` at `/execution/resource_class`. The message gives the class that fits. Ingest runs select the smallest class that holds the upload. |

## Tree datasets in a spec <span class="mapi-badge">v0.8</span> { #tree-datasets-in-a-spec }

A [tree dataset](../guides/tree-data.md) has nodes, for example `/sales` and `/media`. Steps
and most models read one flat dataset. `data.nodes` selects the nodes, and the platform
merges their variables before the pipeline:

```yaml
--8<-- "tree_spec.yaml"
```

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
- `data.nodes` on a plain dataset, and `data.join` without `data.nodes`, are errors.

Static validation uses the `tree_schema` of the dataset version. Thus it finds these problems
before a run starts.

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

`mapi spec check` and `POST /v1/specs:validate` return all the problems at the same time.
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
| `SPLIT_INVALID` | The split dim does not exist at the split stage |
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

* Unseen levels on fixed dims (`UNSEEN_LEVELS`).
* Horizons that are longer than the fit can support (`HORIZON_EXCEEDS_FIT`, HTTP 409). You must
  fit the model again.
* Missing future values of covariates (`MISSING_FUTURE_COVARIATE`).
* Gaps after the fitted range (`NON_CONTIGUOUS_EXTENSION`).
* Summaries that the uncertainty representation of the model cannot support
  (`CAPABILITY_UNSUPPORTED`). An example is the sum of quantiles.

## JSON Schemas

Editors can use the published JSON Schemas to validate specs while you type
(`GET /v1/schemas/modelspec.json`, or `python -m mapi_spec export-schemas ./schemas`).
