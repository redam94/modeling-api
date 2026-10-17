---
description: Build, run and automate workflows - unit-root tests, differencing decisions, AR/MA order searches, honest backtests, benchmarks, governance and reports in one versioned DAG.
---

# Workflows <span class="mapi-badge">v0.5</span>

A workflow strings statistical tests, data processing, train/test plans, models, governance
checks and reports into one graph that runs the same way every time. This guide is the
practical side: how to start from the gallery, the canonical "readiness → ARIMAX" document
line by line, a cookbook of patterns, where every node runs, and how to schedule and watch
runs. The [Workflows concept page](../concepts/workflows.md) covers the document format,
the condition language and validation in depth. The
[node reference](../reference/workflow-nodes.md) lists every node with its About card,
ports, parameters and result fields.

| You want to | Go to |
|---|---|
| Run a ready-made analysis | [Start from the gallery](#start-from-the-gallery) |
| Build, run and watch workflows in the browser | [In the web app](#in-the-web-app) |
| Understand the canonical example | [The canonical workflow](#the-canonical-workflow) |
| Build your own | [Cookbook](#cookbook) |
| Know what runs where and what it costs | [Where nodes run](#where-nodes-run) |
| Automate it | [Schedules and triggers](#schedules-and-triggers) |
| Try it in a notebook without a server | [Run it locally](#run-it-locally) |
| Copy a complete, tested analysis | [Workflow cookbook](workflow-cookbook.md) |
| Drive it from Python or a terminal | `api.workflows` ([client](python-client.md#workflows-governance-and-explorers)), `mapi workflows` ([CLI](cli.md#workflows-governance-and-explorers)) |

## Start from the gallery

The gallery has four templates, each with a plain-language About card
(`GET /v1/workflow-templates`):

| Template | Pick it when |
|---|---|
| `ts_readiness_arimax` | You have one weekly or daily KPI and want a defensible ARIMAX forecast: the tests decide the differencing, a search picks the AR and MA orders, a backtest proves it beats naive forecasts |
| `var_vecm_cointegration` | Several series move together (sales, price, distribution) and you want to know whether they are tied in the long run before choosing a VECM or a VAR |
| `mmm_governed` | A marketing mix model that must pass the governance policy before anyone uses it |
| `forecast_bakeoff` | You do not know which forecasting model suits the data: SARIMAX, ETS, Theta and a smooth trend compete on the same folds |

Creating a workflow from a template copies its document into your project as revision 1;
from then on it is yours to edit.

```bash
# create it (the name and project come from the request's project)
api POST /v1/workflows -d '{"template": "ts_readiness_arimax"}' | jq '{id, ref, latest_revision}'

# run it on the latest version of a dataset, overriding a few parameters
api POST "/v1/workflows/$WF/runs" -H "Idempotency-Key: $(uuidgen)" -d '{
  "inputs": {"data": "dataset://growth/weekly_sales@latest"},
  "params": {"drivers": ["price"], "n_folds": 4, "gap": 1, "horizons": [1, 4, 8]}
}' | jq '{run_id, status}'

# follow it: each node, each branch decision, then the result
api GET "/v1/workflow-runs/$RUN" | jq '.nodes[] | {node_id, status, result}'
```

`api` is the helper of the [HTTP API guide](http-api.md) (it adds the token and the project
header). The same from the command line is `mapi workflows create --template
ts_readiness_arimax`, then `mapi workflows run ts-readiness-arimax -i
data=dataset://growth/weekly_sales@latest -p 'drivers=["price"]' --watch`; from Python,
`api.workflows.create(template="ts_readiness_arimax")` and `api.workflows.run(...)`. `@latest` is resolved when the run starts and recorded as an exact version, so the
run is reproducible: `GET /v1/workflow-runs/{run_id}` shows `dataset://growth/weekly_sales@v7`.

A run answers `202` with the run's view. Starting one with an `Idempotency-Key` that was
already used returns the first run (`200`) instead of starting another.

## In the web app

**Workflows** in the sidebar (++g++ then ++w++) opens the project's workflows and the
gallery. Everything below uses the same endpoints as the rest of this guide.

### Recipes for Guided users

In Guided mode the sidebar entry reads **Recipes** and the page opens on them: the gallery templates as About cards (what each
does in plain words, when to use it and when not, what it needs and gives, a tiny example).
**Run on my data** asks for a dataset and, only when the recipe needs them, the settings it
cannot guess (the drivers of the marketing-mix recipe); the rest sit under *Fine-tune*. The
first run creates the project's workflow from the template; later runs reuse it. The Data
Explorer of data over time offers the readiness recipe with that data already chosen
(*Check it is ready to forecast*). The run
page then shows each step in plain words as it finishes, with the decisions taken and the
report's summary.

### Start from a template (Expert)

**Use this template** opens a three-step wizard: the template's About card and the workflow's
name, its parameters (saved as the workflow's defaults; every run can still change them) and
the data it runs on by default (saved as the input's default reference, so schedules and runs
started without data use it). The workflow is created as revision 1.

### The editor

A workflow's **Editor** tab has three panes, side by side on wide screens and stacked on
narrow ones:

| Pane | What it does |
|---|---|
| **Nodes** (the palette) | Every node of `GET /v1/catalog/workflow-nodes`, grouped by kind, each kind and node with its About card; search by name, id or what it is for. Drag a node onto the canvas, or pick it and press **Add** (the keyboard path). |
| **Canvas** | The graph. Data edges are coloured by port type and numbered when a port has alternatives (*alt 1*, *alt 2*); dashed edges run from a branch's **then**/**else** arm to the top edge of the nodes it gates; dotted edges show results a condition or `$expr` reads. Drag from an output to an input to bind it: an edge between ports of different types, a node feeding itself or a cycle is refused, with the reason above the canvas. Select a node or edge and press ++delete++ to remove it. Problems are counted on the nodes they concern. Node positions are remembered in your browser only (**Tidy layout** forgets them); the document never stores them. |
| **Node editor** | The selected node's About card, its id and label, each input port with its type and its sources in order (add one, reorder alternatives, disconnect), the branch arm that gates it (**When it runs**), its settings - a form generated from the node's JSON Schema, where each field can also take a workflow **parameter** or an **expression** - and how it runs (retries, time limit, failure handling, caching). A model node picks its model from the catalog with the model's About card; `governance.check` picks a check; `subworkflow.run` picks a saved workflow and revision, whose inputs and outputs become the node's ports. With no node selected, the pane edits the workflow's description, parameters, inputs, outputs and run limits. |

A **branch** node gets a condition editor: write the condition, insert results of upstream
nodes (`nodes.adf.reject`) or parameters from the list, and the server checks it as you type
(`POST /v1/workflows:validate`, the issue at `/nodes/<i>/with/condition`). The browser never
evaluates a condition.

**YAML** and **JSON** show the same document in the code editor, with the server's problems
as markers. An edit there updates the canvas as soon as the text reads as a workflow; until
then the canvas keeps the last readable version and says why. Every change is checked live
(the status reads *Ready to run* or *N problems*; the **Problems** list jumps to the node of
each one), undo and redo work across all three views, and **Save revision** (++cmd+s++)
stores the next revision with a message. A revision with problems is saved too, but cannot
run. If someone saved a newer revision meanwhile, the save is refused and your draft is
kept. **Revisions** lists every revision with its verdict; open one in the editor or compare
two side by side.

### Running and watching

**Run workflow** binds the inputs (a dataset from the project, the latest version when it
runs or an exact one), adjusts the parameters for this run only and starts it with an
`Idempotency-Key`. The run page follows `GET /v1/workflow-runs/{run_id}/events` live and
resumes with `Last-Event-ID` after a dropped connection:

- **Graph**: the canvas with each node's status (waiting, queued, running, done, reused,
  skipped, failed, cancelled).
- **Steps**: every node in plain words: its verdict, the arm a decision took, why a node was
  skipped, how long it took.
- **Decisions**: each branch's condition, the values it read and which nodes ran or were
  skipped because of it.
- **Outputs** and **Events**: the run's outputs (the report, rendered) and the event log.

Select a step to open its drawer: whether it ran or reused an earlier result (with a link to
the run it came from), its test verdict (the statistic, p-value and decision, per series for
panels), its outputs (tables, scores, train/test folds, reports, governance evidence, charts
in the plot sandbox, and a download for file outputs such as derived datasets), the settings
it ran with, its sandbox run, fits or child workflow runs with their logs, and its cost.
**Cancel run** stops a running workflow and its children; the run's cost is shown when it
finishes.

A model the run fitted opens in the [Model Explorer](model-explorer.md#in-the-web-app):
**Explore *name* vN** sits under the run's summary when it succeeds, beside the fit step's
runs and on every `model_version` output, with a **Validation** link to that version's
validations. The Model Explorer's lineage links back to this run.

### Schedules and triggers in the app

**Schedules and triggers** adds a schedule from presets or a cron expression, in a time zone,
and previews the next five firing times exactly as the server computes them (daylight-saving
gaps skipped, repeats fired once); an expression that would fire more often than every 15
minutes is refused before it is sent. Schedules can be paused and deleted. A **trigger**
runs the workflow whenever a chosen dataset gets a new version, bound to one of its dataset
inputs, after a debounce.

## The canonical workflow

This is the `ts_readiness_arimax` template exactly as the server stores it (the docs build
checks they are identical):

```yaml
--8<-- "workflow_ts_readiness_arimax.yaml"
```

Reading it from the top:

`params`
:   Everything a user may change per run, each with a type, a default and bounds. A run that
    passes `{"alpha": "x"}` is refused with `422` and the pointer `/params/alpha`; so is a
    number larger than 1e15 either way (conditions and `$expr` compute with parameters).

`adf`, `kpss`, `pp`, `za`
:   Four unit-root tests on the raw series. ADF and Phillips-Perron's null hypothesis is
    "the series wanders"; KPSS's is "the series is stable", so its decision labels run the
    other way; Zivot-Andrews allows one structural break. Each returns a
    `StatTestResult` with a `verdict_plain` sentence.

`wanders` (`branch.if`)
:   `(not nodes.adf.reject or nodes.kpss.reject) and not (nodes.pp.reject and nodes.za.reject)`
    reads: difference unless the evidence for stationarity is clear. The condition is parsed
    by the safe expression language (never `eval`), and the run records which values it
    read, so the decision can be audited later.

`seasonal` and `sdiff`
:   The seasonal difference only runs on the `then` arm of `seasonal`. When the arm is not
    taken the node is **skipped**, not failed.

`diff`
:   `data: [sdiff.data, inputs.data]` is a list of **alternatives**: the seasonally
    differenced series if that node ran, else the raw series. `acf` uses the same trick
    with three alternatives, so it always looks at the most-processed series.

`order` (`search.arma_order`)
:   An information-criterion grid over AR orders `0..max_p` and MA orders `0..max_q`.
    `d: {$expr: 'if(nodes.wanders.value, 1, 0)'}` passes the differencing decision in, so
    the search fits ARIMA(p, d, q) models on the raw series instead of searching on the
    differenced one.

`folds` (`split.expanding_window`)
:   An expanding-window plan: `n_folds` origins, `step: 4` periods apart, a `gap` between
    training and test (for data that arrives late) and every horizon in `horizons` scored
    separately. The folds are computed from the number of periods alone.

`backtest` and `naive`
:   The ARIMAX model refitted on every fold, and naive and seasonal-naive forecasts on the
    **same** folds. The `settings` are built from upstream results with `$expr`.

`compare`, `fit`, `select`
:   `compare.models` ranks the candidates by MAE; `select.best` keeps the fitted ARIMAX
    only if it beats every benchmark in `must_beat` (here `naive`) by at least `margin`.
    Otherwise the workflow stops with a plain explanation (`reason_plain`).

`governance`
:   Runs the governance policy in force for the project (null tests, residual checks,
    stability...; see [Governance](../concepts/governance.md)) and produces an evidence
    record. The result says `passed` and lists `failed_checks`; the report shows both, and
    the [gate recipe](#gate-on-governance-and-keep-the-evidence) stops a workflow on a
    failure.

`report` and `outputs`
:   A `mapi.report/v1` document with a section per test, decision, table and metric.
    `outputs` names what the run hands back (and what a parent workflow can read).

## Cookbook

Each recipe is a fragment of `nodes:`; paste it into a document that declares
`inputs: {data: {type: dataset}}`. Validate before saving with
`POST /v1/workflows:validate`: every problem comes back with a JSON pointer.

### Difference only when the tests say so

```yaml
- {id: adf, use: test.adf, inputs: {data: inputs.data}}
- {id: kpss, use: test.kpss, inputs: {data: inputs.data}}
- id: wanders
  use: branch.if
  with: {condition: "not nodes.adf.reject or nodes.kpss.reject"}
- {id: diff, use: transform.difference, inputs: {data: inputs.data}, when: wanders.then}
# downstream nodes take the differenced series if it exists, else the original
- {id: lb, use: test.ljung_box, inputs: {data: [diff.data, inputs.data]}}
```

A node that needs `diff.data` *without* an alternative is skipped with it. Add
`when: wanders.else` to a node to run it only when the series was left alone.

### Pick AR and MA terms

```yaml
- {id: acf, use: test.acf_pacf, inputs: {data: [diff.data, inputs.data]}, with: {max_order: 4}}
- id: order
  use: search.arma_order
  inputs: {data: inputs.data}
  with: {d: {$expr: "if(nodes.wanders.value, 1, 0)"}, max_p: 4, max_q: 4, criterion: bic}
- id: fit
  use: model.fit
  inputs: {data: inputs.data}
  with:
    model: ts.sarimax@^1
    settings:
      order: {$expr: "[nodes.order.best_p, if(nodes.wanders.value, 1, 0), nodes.order.best_q]"}
```

`acf` suggests orders from the significant autocorrelations (`suggested_p`, `suggested_q`)
and is quick to read; `search.arma_order` actually fits every candidate and is what the model
should use. `search.sarima_order` adds seasonal orders, `search.var_lag_order` chooses a VAR's
lag length and `search.coint_rank` a VECM's cointegration rank.

### Honest train/test plans

```yaml
# 6 expanding folds, 2 periods between training and test, horizons 1, 4 and 13
- id: folds
  use: split.expanding_window
  inputs: {data: inputs.data}
  with: {n_folds: 6, step: 4, gap: 2, horizons: [1, 4, 13]}

# tune on the inner folds of each outer training window, score on the outer test windows
- id: nested
  use: split.nested
  inputs: {data: inputs.data}
  with:
    outer: {kind: expanding_window, n_folds: 4, horizons: [4]}
    inner: {kind: rolling_origin, n_folds: 3, window: 52, horizons: [4]}

# hold out whole markets (MMM, geo experiments)
- {id: geo, use: split.geo_holdout, inputs: {data: inputs.data}, with: {n: 2, seed: 7}}
```

`split.blocked_kfold` and `split.purged_kfold` cut contiguous blocks (with a purge gap) for
models that are not forecasters. A plan is a `split_plan` value: feed the same plan to every
`model.backtest`, `evaluate.baseline` and `search.grid` node you want to compare.

### Beat the benchmarks, or stop

```yaml
- {id: naive, use: evaluate.baseline, inputs: {data: inputs.data, split: folds.plan},
   with: {methods: [naive, seasonal_naive, drift], season: 52}}
- {id: bt, use: model.backtest, inputs: {data: inputs.data, split: folds.plan},
   with: {model: ts.sarimax@^1, settings: {order: [1, 1, 1]}}}
- {id: compare, use: compare.models, inputs: {candidates: [bt.metrics, naive.metrics]},
   with: {metric: mase}}
- {id: dm, use: compare.diebold_mariano, inputs: {a: bt.metrics, b: naive.metrics},
   with: {loss: squared, horizon: 4}}
- {id: fit, use: model.fit, inputs: {data: inputs.data},
   with: {model: ts.sarimax@^1, settings: {order: [1, 1, 1]}}}
- {id: select, use: select.best, inputs: {table: compare.table, models: [fit.model]},
   with: {metric: mase, must_beat: [naive, seasonal_naive], margin: 0.05}}
```

`compare.diebold_mariano` tests whether the difference in forecast errors is more than
luck (with the Harvey-Leybourne-Newbold small-sample correction). `select.threshold` keeps
the rows of a comparison table within limits (`max`, `min`) when there is no single winner.

### Long-run relationships: VECM or VAR

```yaml
- {id: joh, use: test.johansen, inputs: {data: inputs.data}}
- {id: tied, use: branch.if, with: {condition: "nodes.joh.rank > 0"}}
- {id: vecm, use: model.fit, inputs: {data: inputs.data}, when: tied.then,
   with: {model: ts.vecm@^1, settings: {coint_rank: {$expr: "nodes.joh.rank"}}}}
- {id: d, use: transform.difference, inputs: {data: inputs.data}, when: tied.else}
- {id: lags, use: search.var_lag_order, inputs: {data: d.data}}
- {id: var, use: model.fit, inputs: {data: d.data},
   with: {model: ts.var@^1, settings: {lags: {$expr: "nodes.lags.best_lag"}}}}
```

The data holds the series along one dimension, one value per series and period
(`value(series, quarter)`: the layout of the statsmodels multivariate models; ingest a wide
sheet with the `wide` mapping). The tests and searches use every series of that dimension,
or the ones named in `vars` (labels such as `[sales, price]`), and the model nodes bind the
models' `variable` dimension to it. The `var_vecm_cointegration` template is the complete
version, with Granger tests and a report.

### Search model settings on a plan

```yaml
- id: grid
  use: search.grid
  inputs: {data: inputs.data, split: folds.plan}
  with:
    model: ts.sarimax@^1
    settings: {order: [1, 1, 1]}
    grid: {"/order/0": [0, 1, 2], "/order/2": [0, 1, 2]}   # JSON pointers into settings
    metric: rmse
    max_points: 9
```

Every grid point is backtested on the plan; `best` is the winning settings refitted on all
the data (`best_settings`, `best_score` in the result). A grid larger than `max_points`
fails the node (`WORKFLOW_INVALID`) before any point is fitted.

### Gate on governance and keep the evidence

```yaml
- {id: gov, use: governance.policy, inputs: {model: select.model, data: inputs.data},
   with: {submit: true}}
- {id: approved, use: branch.if, with: {condition: "nodes.gov.passed"}}
- {id: pack, use: report.evidence_pack, inputs: {model: select.model, evidence: gov.evidence},
   when: approved.then}
```

`governance.policy` records the outcome of every check of the policy in force (or the one
named by `policy`) as evidence; it does not fail when a check fails, so the evidence of a
rejected model is kept too. The branch makes everything after it depend on the verdict.
`governance.check` runs a single check with its own thresholds, which is handy while a policy
is being designed. The checks are the platform's own implementations (the same as a
validation request's), measured in the node's sandbox on the model and its data; the policy
in force is the one the project or tenant set up for the model's family (the platform's
default policies apply until you add your own). With `submit: true` a passing model is
recorded as a validation of that model version: `validated`, or `awaiting_approval` when the
policy asks for a reviewer (see [governance](governance.md)). A node's `policy` only adds to
the policies in force (the tenant floor and the project's policies always apply), and the
validation counts as submitted by the person who started the run - or who created its
schedule or trigger - so that person cannot approve it. Governance nodes are never cached:
the checks run again on every run.

### Reuse a workflow as a node

```yaml
# workflow "readiness" declares  outputs: {stationary: wanders.result, report: report.report}
- id: ready
  use: subworkflow.run
  inputs: {data: inputs.data}
  with: {workflow: "workflow://growth/readiness@r3", params: {alpha: 0.01}}
- {id: summary, use: report.summary, inputs: {items: [ready.report]}}
```

The child runs as its own workflow run (`trigger: parent`), visible on its own page and
cancelled with its parent. Its ports are the child's `outputs`. Pin a revision (`@r3`) so a
later edit of the child does not change the parent silently; `@latest` is allowed too.
A workflow that would run itself, directly or through another, fails with
`WORKFLOW_INVALID`, and sub-workflows nest at most 8 deep.

### Failures, retries and time limits

```yaml
- id: za
  use: test.zivot_andrews
  inputs: {data: inputs.data}
  retries: 1            # retry once on a transient failure (0-3)
  timeout: 20m          # this node's limit (default: the server's workflow_node_timeout_s)
  on_failure: continue  # treat a failure like a skip: downstream alternatives still work
  cache: false          # always recompute
execution: {max_parallel: 4, timeout: 2h}
```

`execution` applies to the whole run: how many nodes run at once and a deadline after which
the run fails with `TIMEOUT`. A failed node without `on_failure: continue` fails the run; the
nodes still pending are cancelled, the ones running are stopped, and the run's `error` names
the node. A condition or `$expr` that cannot be computed on the data it reads (a number too
large, values nested too deeply) fails its node with `EXPRESSION_ERROR`. Should the
orchestrator itself be unable to advance a run, the run fails with `INTERNAL_ERROR` and its
running children are stopped; other runs are not held up.

## Where nodes run

The control plane never decodes your data. Each node implementation declares where it runs
(`runs_in` in the node catalog):

| Runs in | Nodes | How |
|---|---|---|
| The orchestrator | `branch.if`, `compare.models`, `select.best`, `select.threshold`, `report.summary`, `report.evidence_pack` | On the JSON results of upstream nodes only, in the API process, with no child run and no cost |
| A sandboxed `workflow_node` run | tests, transforms, splits, order searches, `model.backtest`, `model.forecast`, `evaluate.*`, `compare.diebold_mariano`, `governance.*` | A child run on the same executor and isolation as fits; it reads its inputs through generation-pinned signed URLs and writes a result the finalizer verifies |
| A child `fit` run | `model.fit`, and the winner of `search.grid` (after a `workflow_node` run scored the grid) | An ordinary fit of a stored spec named `wf-<workflow>-<node>`, so the model version is a normal one (scoring, endpoints, approvals) |
| A child workflow run | `subworkflow.run` | The child workflow, with its own nodes |

Two consequences worth knowing:

- **Derived datasets are real datasets.** The differenced series of node `diff` of workflow
  `ts-readiness` is registered as `dataset://<project>/wf-ts-readiness-diff@vN`. You can open
  it in the Data explorer or start another workflow from it. These datasets do not fire
  dataset triggers, so a workflow cannot trigger itself.
- **Fitted models carry their preprocessing.** When `model.fit` reads the output of a
  `transform.steps` node, the steps are written into the stored spec ahead of the model, so
  scoring new data applies the same preprocessing. Differencing nodes are the exception: pass
  `d` (or `order`) to the model instead, as the canonical template does.

The run's `cost` is the sum over its child runs; orchestrator nodes are free. Each node lists
its child runs (`child_runs`, one per attempt); they are ordinary runs with a `parent_run_id`
and the labels `workflow` and `workflow-node`.

### Caching

Every node has a cache key made of its implementation and version, its resolved parameters
and the identity of its inputs (dataset content hashes, model versions, upstream keys). When a
project already has a successful result with the same key, the node is marked `cached` with
`cached_from` naming the run that produced it, and no child run starts. Change one parameter
of the final report and only the report runs again. Start a run with `"use_cache": false` to
recompute everything, or set `cache: false` on a node.

## Watching a run

| Endpoint | What you get |
|---|---|
| `GET /v1/workflow-runs/{run_id}` | Status, every node (status, attempt, result, outputs, child runs, timing), branch decisions with the values they read, outputs, error and cost |
| `GET /v1/workflow-runs/{run_id}/nodes/{node_id}` | One node in detail |
| `GET /v1/workflow-runs/{run_id}/nodes/{node_id}/outputs/{port}` | A signed download link for a file output (a derived dataset, a table too large to inline) |
| `GET /v1/workflow-runs/{run_id}/events` | The event log as JSON, or as server-sent events with `Accept: text/event-stream` (a `snapshot` first, `Last-Event-ID` to resume, `end` when the run finished) |
| `POST /v1/workflow-runs/{run_id}:cancel` | Cancels the run and every running child (`cancelling` until the children have stopped) |

Finished runs send the `workflow.run.succeeded` and `workflow.run.failed`
[webhooks](endpoints-and-webhooks.md), with the run id, the workflow revision, the status and
the outputs.

## Schedules and triggers

```bash
# every Monday at 06:00 Berlin time, on the latest data
api POST "/v1/workflows/$WF/schedules" -d '{
  "cron": "0 6 * * 1", "timezone": "Europe/Berlin",
  "inputs": {"data": "dataset://growth/weekly_sales@latest"},
  "description": "Weekly refresh"
}' | jq '{id, next_run_at}'

# whenever weekly_sales gets a new version (wait 10 minutes for more uploads first)
api POST "/v1/workflows/$WF/triggers" -d '{
  "dataset": "dataset://growth/weekly_sales", "input": "data", "debounce_seconds": 600
}'
```

- A **schedule** fires once per due time, in its time zone (daylight-saving changes
  included). Expressions that would fire more often than every 15 minutes are refused, as are
  unknown time zones. `PATCH` with `{"active": false}` pauses it; `last_run_id` and
  `next_run_at` show where it stands.
- A **trigger** binds a new dataset version to one of the workflow's inputs. Versions that
  arrive within `debounce_seconds` of each other start one run, on the newest version.
- Runs started by schedules and triggers carry `trigger: schedule` or
  `trigger: dataset.version.created` and the schedule or trigger id, are audited as the
  platform acting for the person who created the schedule, and count against the project's
  quotas like any other run.

## Run it locally

The same engine runs in a Python process, which is the fastest way to develop a workflow
against a dataset you have in memory. `LocalRunner` executes every node in threads, honours
branches, alternatives, retries, timeouts and cancellation, and caches results in memory:

```python
--8<-- "workflows_local.py:run"
```

```python
--8<-- "workflows_local.py:inspect"
```

```python
--8<-- "workflows_local.py:cache"
```

The example uses `mapi_workflows.testing.test_registry()`: the real statsmodels models
(`ts.sarimax`, `ts.var`, `ts.vecm`, ...) with the SDK's toy models and the built-in steps
(install `mapi-workflows[testing]`). `mapi_sdk.default_registry()` gives every installed
plugin. `LocalRunner(governance=GovernanceServices(...))` supplies the policy resolver for
governance nodes; the check runners are the installed ones (`mapi-runner` exports every
check through the `mapi.governance_checks` entry point), and `runners=` adds or replaces
some.

## Limits and settings

| Setting | Default | Meaning |
|---|---|---|
| `MAPI_WORKFLOW_TICK_S` | 1.0 | How often the orchestrator advances active runs |
| `MAPI_WORKFLOW_MAX_ACTIVE_RUNS` | 20 | Runs a project may have going at once, sub-workflow runs included (`429` beyond; a `subworkflow.run` node over it fails with `QUOTA_EXCEEDED`) |
| `MAPI_WORKFLOW_MAX_SCHEDULES` | 20 | Active schedules per project |
| `MAPI_WORKFLOW_MAX_TRIGGERS` | 20 | Active triggers per project |
| `MAPI_WORKFLOW_NODE_TIMEOUT_S` | 1800 | Timeout of a node without its own `timeout` |
| `MAPI_WORKFLOW_MAX_JSON_OUTPUT_BYTES` | 1 MiB | Largest JSON output a node may return |

A document has at most 200 nodes; conditions at most 2000 characters. Workflows,
revisions, runs, node runs, events, schedules and triggers are tenant data with row-level
security, like everything else.

## Known limitations in v0.5

- Plot outputs of `evaluate.residuals` and `model.forecast` are not produced yet; their
  tables and results are.
- Detrending, STL adjustment and lag/lead features are not separate nodes: use
  `transform.steps` with the catalog's preprocessing steps.
- In the web editor, node positions are a per-browser view preference (the document has no
  place for them), and a sub-workflow's ports appear once its revision is loaded.
- The [Python client](python-client.md#workflows-governance-and-explorers) and the
  [CLI](cli.md#workflows-governance-and-explorers) cover every workflow operation; the web
  app's screens are in [In the web app](#in-the-web-app).
