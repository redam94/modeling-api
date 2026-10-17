# Workflows, governance and explorers <span class="mapi-badge">v0.5</span>

!!! success "Status: available in v0.5 (milestone M3.5)"
    v0.4 made one model easy to fit, read and share. v0.5 makes the analysis around it
    repeatable and defensible: workflows that run the statistics a careful analyst would,
    governance policies that hold every model to null tests before anyone uses it, Data and
    Model Explorers, mmm-framework as the marketing-mix engine with budget optimisation,
    twelve statsmodels time-series models, and an About card on everything a user can pick.
    To use it: [What's new in 0.5](../whats-new-0.5.md), [the workflow cookbook](../guides/workflow-cookbook.md),
    [Can you trust this model?](../guides/null-tests.md).

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

The five v0.5 journeys - the canonical workflow, governance with a second person, an
mmm-framework fit with a budget plan, the explorers and the chooser, and the CLI - pass
through a live local stack, with real fits in the runner and the real orchestrator; the runs
found eight defects that no unit or mocked test could see, each fixed with a regression test
([M3.5 verification](../operations/m35-verification.md)). As for v0.4, nothing in v0.5 has
run on real Google Cloud or in Docker yet.
