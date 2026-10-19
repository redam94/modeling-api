# mapi-sdk

The xarray modelling runtime of modeling-api: the step and model contracts, the pipeline engine
(stages, split, per-group context, score replay, inverse transforms), CoordPlan and coordinate
canonicalisation, the representation-aware summariser, wire formats (XrJSON, zarr v3 zips,
DataTree zips, Arrow IPC), bundle IO, the fit/score harness, an in-process executor and the
contract kit for plugin authors (`mapi_sdk.testing`, also a pytest plugin).

It runs in runner images and notebooks, never in the control plane (`mapi_server` may not
import it; enforced by import-linter). It never pickles anything.

License: Apache License 2.0.

## Install

```bash
uv add mapi-sdk                 # or: pip install mapi-sdk
uv add "mapi-sdk[testing,loo]"  # contract-kit extras (pytest, hypothesis) and ArviZ for elpd_loo
```

## Quick start

```python
from mapi_sdk import fit_spec, score_bundle
from mapi_sdk.testing import toy
from mapi_spec import load_model_spec, load_score_request

registry = toy.toy_registry()
data, fx = toy.panel_dataset()
bundle = fit_spec(load_model_spec(SPEC_YAML), data, {"fx": fx}, registry)
tree = score_bundle(bundle, load_score_request(REQUEST_YAML), new_data, registry=registry)
```

See `docs/reference/sdk.md` and `docs/guides/plugins.md` for the full tour.

## Modules

| Module | Contents |
|---|---|
| `wire` | XrJSON, zarr v3 zip and DataTree zip IO with hostile-input checks, `stack_rows`/`unstack_rows`, Arrow IPC (the dim columns are named in the schema metadata `mapi.dims`) |
| `schema` | `validate_dataset` (plain-language `SchemaIssue`s), `infer_schema`, coordinate summaries |
| `binding` | `BindingMap`: dataset ↔ canonical names, applied to inputs and reversed on outputs (dim renames also rewrite the `grid` attr of a variable) |
| `steps` | `Step`, `StepSettings`, `StepState`, `StepContext` (`align_state`, side tables, cancellation) |
| `context` | per-group context tails: extract at fit, prepend (contiguity-checked) at score, drop after |
| `split` | `time_holdout`, `as_of`, `leave_group_out`, `last_per_group`, `mask_block` |
| `pipeline` | `Pipeline`: stages, fit, replay, inverse chain with output-space attrs and retransformation |
| `coords` | `CoordPlan` typed errors, `canonicalise_coords`, horizon coordinates |
| `models` | `ModelPlugin` protocol, `CustomModel` (`structural_missing`: variables whose gaps the scorecard check `data.missing` does not count), `FitOutput`, `Fitted`, `FitContext`, `ScoreContext` (`ctx.tree` for tree-input models) |
| `tree` | v0.8 tree datasets: `select_nodes` (the `data.nodes` merge), `infer_tree_schema`, `validate_tree`, `read_tree_zip`/`write_tree_zip` |
| `summarise` | summaries per representation (circular mean, intervals and `sd` for a circular output), aggregation legality, bundle-wide draw thinning |
| `metrics` | holdout metrics (circular metrics for an `OutputDecl.circular` output), `elpd_loo` from ArviZ with the pointwise values |
| `bundle` | `Bundle` write/read with content hashes, pickle rejection, `MetricsReport` (`elpd`: the estimates with up to 10 000 pointwise values) |
| `harness` | `fit_spec` (evaluate-then-refit; the model settings are parsed before the data is transformed or split) and `score_bundle` (`MODE_NOT_SUPPORTED` before any mode-specific work) |
| `registry` | entry-point discovery (`mapi.steps`, `mapi.models`, `mapi.plots`) and catalog export |
| `executors.inproc` | `InprocExecutor` (the `mapi_spec.executor.Executor` protocol, isolation NONE) |
| `testing` | `check_step`, `check_model`, toy reference steps/models, Hypothesis strategies, pytest plugin |

## Tests

```bash
uv run pytest packages/sdk -q
uv run mypy --strict packages/sdk/src
```
