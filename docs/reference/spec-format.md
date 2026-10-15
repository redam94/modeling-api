# Spec format reference

Every model on modeling-api starts as a **ModelSpec**: a small YAML (or JSON) file that names
your dataset, the preprocessing pipeline, the model plugin and how to fit and evaluate it.
The same file drives the Guided wizard, the Expert editor, the CLI and the API.

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
  - {id: scale,  use: core.mean_scale@^1, with: {vars: [spend], over: [time]}}

model:
  use: mmm.hierarchical_geo@^1.3
  settings:
    dim_policies: {geo: fixed}      # may only tighten the plugin's policy
  fit: {mode: evaluate_then_refit, draws: 1000, chains: 4, seed: 7}

evaluate: {metrics: [elpd_loo, coverage_90, mape]}
execution: {resource_class: M, timeout: 45m}
```

## Sections

| Section | Purpose |
|---|---|
| `metadata` | `name` and `project` (lowercase slugs), optional `labels` and `description` |
| `data.dataset` | `dataset://<project>/<name>@latest|vN|sha256:<hex>` — pinned to an exact version at submit |
| `data.side_tables` | Small constant datasets (FX, holidays). `score_policy`: `pinned`, `latest` or `supplied` |
| `data.bind` | Maps **canonical** names (what plugins and steps use) to **dataset** names. Outputs are reverse-bound by default |
| `data.split` | `time_holdout`, `last_per_group`, `as_of`, `leave_group_out` or `mask_block`, applied at `after_step` |
| `pipeline` | Ordered steps `{id, use: <id>@<semver range>, with: {...}}` |
| `model` | Plugin `use`, its `settings` and the `fit` block |
| `outputs` | Derived quantities, plot recipes and the number of draws kept for the UI |
| `evaluate` | Holdout metrics (`elpd_loo`, `coverage_90`, `mape`, `rmse`, ...) |
| `execution` | Resource class (`S`, `M`, `L`, `XL`) and timeout (`45m`, `1h30m`) |

## Versions in `use:`

`use:` takes an npm-style semver range: `^1` (any 1.x), `~1.2` (1.2.x), `>=1.2 <2`, `1.x`,
an exact `1.2.3`, or `*`. The highest matching non-yanked version is chosen at submit time and
recorded with the run, so re-running a revision is reproducible.

## Where the split happens

Some dims only exist after preprocessing — daily data resampled to weeks, for instance. The
split therefore happens at a named **stage** (`after_step`). If you leave it out, the split
happens right after the leading steps that learn nothing from the data (such as unit
conversion and resampling). If a step that *learns* from the data (scaling, encoding,
seasonality fits) runs before the split, validation reports **`LEAKAGE_RISK`**: its state
would be learned from the holdout.

## Validation messages

`mapi spec check` and `POST /v1/specs:validate` return every problem at once, each with a
stable `code` and a JSON pointer to the offending field:

```json
{"code": "SPLIT_INVALID", "pointer": "/data/split/dim",
 "message": "split dim 'week' does not exist at stage 'raw'",
 "suggested_action": "Set data.split.after_step to the stage where the dim exists (e.g. after a resample step)."}
```

| Code | Meaning |
|---|---|
| `SCHEMA_INVALID` | A field has the wrong shape, or a step/model needs data the pipeline does not produce |
| `SPEC_PARSE_ERROR` | Not valid YAML/JSON, or uses anchors, aliases or tags (not allowed) |
| `PAYLOAD_TOO_LARGE` | Over 256 KB, nesting deeper than 32 or more than 10 000 values |
| `REF_UNRESOLVED` | Unknown step, model, plot, side table or derived quantity, or no matching version |
| `SETTINGS_INVALID` | Settings don't match the step's or model's settings schema |
| `BINDING_INVALID` | `data.bind` names something the dataset does not have |
| `SPLIT_INVALID` | The split dim does not exist at the split stage |
| `LEAKAGE_RISK` | A stateful step runs before the split |
| `DIM_POLICY_LOOSENED` | `dim_policies` tries to loosen a plugin's dim policy |
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

Score requests are checked against the fitted model before anything runs: unseen levels on
fixed dims (`UNSEEN_LEVELS`), horizons beyond what the fit supports (`HORIZON_EXCEEDS_FIT`,
HTTP 409 — refit required), missing future values of covariates
(`MISSING_FUTURE_COVARIATE`), gaps after the fitted range (`NON_CONTIGUOUS_EXTENSION`) and
summaries the model's uncertainty representation cannot support (`CAPABILITY_UNSUPPORTED` —
for example summing quantiles).

## JSON Schemas

Editors can validate specs as you type using the published JSON Schemas
(`GET /v1/schemas/modelspec.json`, or `python -m mapi_spec export-schemas ./schemas`).
