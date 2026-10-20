# mapi-workflows

The workflow engine of modeling-api. A workflow is a DAG of nodes: statistical tests,
transforms, splits, searches, model fits, posterior checks, comparisons, selections, branches,
plots, governance checks and reports.

License: Apache License 2.0.

## Install

```bash
uv add mapi-workflows               # the JSON-only engine (control plane safe)
uv add "mapi-workflows[runtime]"    # + the node implementations (numpy, xarray, statsmodels, mapi-plots[recipes])
uv add "mapi-workflows[testing]"    # + mapi-plugin-statsmodels for tests and examples
```

## Two layers

- **The core** (`engine`, `orchestrator`, `payloads`, `reference`) is Python over `mapi-spec`.
  It decides which nodes run, evaluates branch conditions with the safe expression language,
  and calculates comparisons, selections and reports. It keys the result cache. The control
  plane imports it. It never reads data.
- **The runtime** (`mapi_workflows.runtime`, the `runtime` extra) has the node implementations
  that work on data, and `LocalRunner`, an in-process runner for notebooks, the CLI and tests.
  It runs in the runner sandbox. The entry point `mapi.workflow_nodes: core` registers the
  built-in nodes.

Each node runs in one of three places (`NodeImplementation.runs_in`):

| Runs in | Nodes |
|---|---|
| `orchestrator` | `compare.models`, `select.best`, `select.threshold`, `report.summary`, `branch.if` |
| `sandbox` | `test.*`, `transform.*`, `split.*`, `search.*` (not `search.grid`), `evaluate.*`, `compare.diebold_mariano`, `compare.stacking`, `model.score`, `model.summary`, `model.lift_test`, `plot.render` |
| `child_run` | `model.fit`, `model.backtest`, `model.forecast`, `search.grid`, `governance.policy`, `governance.check`, `report.evidence_pack`, `subworkflow.run` |

On the server, `model.fit` is a `fit` child run, `subworkflow.run` is a child workflow run and
the orchestrator builds `report.evidence_pack` from JSON. The other `child_run` nodes are
`workflow_node` child runs (`mapi_server.services.workflows.orchestrate`).

The posterior nodes are `model.summary` (posterior summaries, R-hat, ESS and divergences, and
one `focus` value), `evaluate.ppc` (coverage, PIT KS distance and Bayesian p-values) and
`compare.stacking` (stacking and pseudo-BMA+ weights from the pointwise elpd that `model.fit`
keeps). `model.score` gives a score mode as a dataset. `plot.render` gives any catalog recipe
as a `plot` output. `model.fit` and `model.backtest` take `nodes` and `join` for tree datasets.

`model.lift_test` (v0.13) reads the `impact` group of a causal model (`causal.impact`,
`causal.synthetic_control`, `causal.did`) and gives the total effect in the test window as a
`LiftTest`: the mean, the standard deviation and the HDI of the sum of the effect over the
test cells. It never imports plugin code. With one treated unit the lift test has that `geo`;
with more units it is national. `model.fit` takes a `calibration` block, so a workflow can
give the lift test to an MMM: `calibration: {lift_tests: {$expr: "[nodes.lift.lift_test]"}}`
(the `geo_lift_mmm` template).

## Run a workflow locally

```python
from mapi_plugin_science.areal_data import areal_dataset
from mapi_spec.workflow_templates import instantiate_template
from mapi_workflows.runtime.local import LocalRunner
from mapi_workflows.testing import science_registry

spec = instantiate_template("sci_spatial_ladder", name="disease-map", project="epidemiology")
res = LocalRunner(registry=science_registry()).run(
    spec, inputs={"data": areal_dataset()}, params={"draws": 500, "tune": 500}
)
print(res.status, res.node("best").result["reason_plain"])
```

`LocalRunner` has no sandbox. Do not use it to run workflows or plugins that you do not trust.
`mapi_workflows.testing.test_registry()` gives the toy models, the statsmodels models and the
built-in steps. `science_registry()` adds the `sci.*` models (`--registry
mapi_workflows.testing:science_registry` for a worker).

The gallery templates are in `mapi_spec.workflow_templates.WORKFLOW_TEMPLATES`:
`ts_readiness_arimax`, `var_vecm_cointegration`, `mmm_governed`, `forecast_bakeoff`,
`sci_spatial_ladder`, `sci_network_ladder`, `sci_phylo_signal`, `sci_calibrate_then_design`,
`bayes_workflow_audit` and `sci_image_model_check`.

See `docs/guides/workflows.md`, `docs/guides/workflow-cookbook.md` and
`docs/reference/workflow-nodes.md`.

## Tests

```bash
uv run pytest packages/workflows -q
```
