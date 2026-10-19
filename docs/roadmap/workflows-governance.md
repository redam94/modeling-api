# Workflows, governance and explorers <span class="mapi-badge">v0.5</span>

!!! success "Status: available in v0.5 (milestone M3.5)"
    v0.4 made one model easy to fit, read and share. v0.5 makes the analysis around the
    model repeatable and defensible. v0.5 has these parts:

    - workflows that run the statistics that a careful analyst runs
    - governance policies that make each model pass null tests before a person uses it
    - Data and Model Explorers
    - mmm-framework as the marketing-mix engine, with budget optimisation
    - twelve statsmodels time-series models
    - an About card on each item that a user can select.

    For instructions, read [What's new in 0.5](../whats-new-0.5.md), [the workflow cookbook](../guides/workflow-cookbook.md)
    and [Can you trust this model?](../guides/null-tests.md).

## Workflows

| Shipped | Where to read about it |
|---|---|
| Versioned `WorkflowSpec` DAGs with typed ports, parameters, alternatives, branch gating and sub-workflows, validated with JSON pointers before anything runs | [Workflows](../concepts/workflows.md) |
| 51 nodes: 17 statistical tests, 6 transforms, 7 train/test plans, 5 searches, fits, backtests, forecasts, evaluations, comparisons with Diebold-Mariano, selection, governance, reports | [Workflow node reference](../reference/workflow-nodes.md) |
| A safe condition language for branches and computed parameters (`mapi_spec.expr`, never `eval`) | [Conditions](../concepts/workflows.md#conditions-a-safe-expression-language) |
| Server orchestration: per-node status, server-sent events, retries, caching of unchanged nodes, child runs, cost | [Where nodes run](../guides/workflows.md#where-nodes-run) |
| Cron schedules and dataset triggers | [Schedules and triggers](../guides/workflows.md#schedules-and-triggers) |
| A gallery: readiness to ARIMAX, VAR/VECM cointegration, governed MMM, forecast bake-off | [Workflow cookbook](../guides/workflow-cookbook.md) |
| A visual workflow builder, a live run monitor and Guided recipes in the web app | [In the web app](../guides/workflows.md#in-the-web-app) |

!!! note "Added after v0.5"
    - v0.9 (M5): five new nodes, 56 in all. `model.summary`, `evaluate.ppc`,
      `compare.stacking`, `model.score` and `plot.render` give posterior summaries,
      posterior predictive checks, stacking weights, scores and plots in a workflow. The new
      node kind is `plot`, and reports have plot sections.
    - v0.9 (M5): `model.fit` and `model.backtest` read the tree datasets of v0.8 (the
      parameters `nodes` and `join`). The local runner accepts `xr.DataTree` inputs.
    - v0.9 (M5): six new recipes, ten in the gallery in all: `sci_spatial_ladder`,
      `sci_network_ladder`, `sci_phylo_signal`, `sci_calibrate_then_design`,
      `bayes_workflow_audit` and `sci_image_model_check`.

    Read [Bayesian models in a workflow](../concepts/workflows.md#bayesian-models-in-a-workflow)
    and [the gallery](../concepts/workflows.md#the-gallery).

## Governance

| Shipped | Where to read about it |
|---|---|
| Versioned tenant and project policies, per model family | [Validate models with governance policies](../guides/governance.md) |
| Twelve checks, including null tests against shuffled targets, noise drivers and time-shifted placebos | [Governance checks](../reference/generated/governance-checks.md), [Null tests explained](../guides/null-tests.md) |
| The validation state machine, second-person approval with the `reviewer` capability, promotion that refuses unvalidated versions (`NOT_VALIDATED`) | [Governance](../concepts/governance.md) |
| Evidence packs (HTML and JSON with a digest), audit events and webhooks | [Export the evidence pack](../guides/governance.md#export-the-evidence-pack) |

## Models, explorers and choosing

| Shipped | Where to read about it |
|---|---|
| mmm-framework (`mmm.bayesian`, `mmm.frequentist`) as the only marketing-mix engine; budget optimisation as `optimize` runs on its planning module | [Marketing-mix models](../models/mmm-framework.md), [Optimise a budget](../tutorials/optimise-a-budget.md) |
| Twelve statsmodels models, from SARIMAX to VECM and Markov switching | [statsmodels time-series models](../models/statsmodels.md) |
| The Data Explorer and the Model Explorer, including side-by-side comparisons | [Explore a dataset](../guides/data-explorer.md), [Explore a model](../guides/model-explorer.md) |
| About cards on every catalog item and Help me choose | [Choosing a model](../guides/choosing-a-model.md) |
| The Python client and the CLI for all of it | [The Python client](../guides/python-client.md), [The command line](../guides/cli.md) |

## Verification

The five journeys of v0.5 pass on a live local stack, with real fits in the runner and the
real orchestrator. The journeys are:

- the canonical workflow
- governance with a second person
- an mmm-framework fit with a budget plan
- the explorers and the chooser
- the CLI.

The runs found eight defects that unit tests and mocked tests could not find. We repaired
each defect and added a regression test
([M3.5 verification](../operations/m35-verification.md)). The same as for v0.4, no part of
v0.5 ran on real Google Cloud or in Docker yet.
