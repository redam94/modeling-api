# Roadmap

modeling-api ships as three products, in this sequence:

1. A typed modelling runtime.
2. A job platform around the runtime.
3. A hosted platform for many users.

Some rules make the hosted platform safe. These rules are in the code from the first
release: no pickle, contracts in JSON only, static validation, sanitized plots, and
sandboxed runs with verified outputs.

```mermaid
timeline
    title Releases
    v0.1 · M0 · contract layer : SDK, specs and schemas : 28 steps, PyMC and sklearn base classes : 11 built-in models, plots, sanitizer : conformance corpus
    v0.2 · M1 · local platform : HTTP API, queue, sandboxed runner : GCS shim, signed bundles : Expert web app, CLI, Python client : make dev and Docker Compose
    v0.3 · M2 · cloud and multi-tenant : tenants, OIDC sign-in, tokens, row-level security : quotas, fair share, cost ledger, kill switches : Cloud Run Jobs executor, Terraform : observability, security suite, chaos
    v0.4 · M3 · Guided mode and integrations : templates, question flow, business priors : scorecard, what-if, forecasts : endpoints, snippets, webhooks, embeds : sign-up, deletion, GDPR export
    v0.5 · M3.5 · workflows and governance : workflows of tests, transforms and models : governance policies and null tests : Data and Model Explorers : mmm-framework and statsmodels : About cards, Help me choose
    v0.6 · M4 · expert plugins and MCP (next) : plugin upload for allow-listed experts : curated MCP server : sweeps and SBC as parent runs
```

| Release | Milestone | Theme | Status |
|---|---|---|---|
| **v0.1** | M0 | The contract layer: specs, the xarray runtime, steps, models, plots and the conformance corpus. All of it runs in your own Python process. | <span class="mapi-badge">available</span> |
| **v0.2** | M1 | [The local platform](platform-api.md): the same work as API calls and sandboxed runs, with the Expert web app. It runs on a laptop or on one Docker host. | <span class="mapi-badge">available</span> |
| **v0.3** | M2 | [Cloud and multi-tenant core](cloud.md): tenants, sign-in, tokens, quotas and cost. Each run gets one new sandbox on Cloud Run. This release is a private beta. | <span class="mapi-badge">available</span> |
| **v0.4** | M3 | [Guided mode and integrations](guided-ui.md): analysts go from a spreadsheet to a report and a scenario. Other apps get endpoints, webhooks and embeds. This release is the first public launch. | <span class="mapi-badge">available</span> |
| **v0.5** | M3.5 | [Workflows, governance, explorers, mmm-framework and statsmodels](workflows-governance.md): automated, versioned analyses. Models must pass null tests and get the approval of a second person. Also budget plans, About cards and Help me choose. | <span class="mapi-badge">available</span> |
| **v0.6** | M4 | [Expert plugins and MCP](expert-plugins.md): plugin upload for allow-listed experts, a curated MCP server for AI agents, and sweeps and SBC as parent runs. | <span class="mapi-badge mapi-badge--soon">next</span> |

## What v0.5 contains

- [x] **Workflows**: versioned DAGs of statistical tests, transforms, branches, split plans,
      order searches, model fits, evaluations, comparisons, governance and reports. Also
      live events, a cache, schedules and dataset triggers
      ([cookbook](../guides/workflow-cookbook.md)).
- [x] **Governance**: versioned project and tenant policies and twelve checks. The checks
      include null tests against shuffled targets and placebo regressors. Also
      second-person approval, evidence packs and promotion that needs validation
      ([null tests explained](../guides/null-tests.md)).
- [x] **Data and Model Explorers**, with side-by-side comparisons and Diebold-Mariano tests
- [x] **mmm-framework** as the marketing-mix engine, with **budget optimisation** in the sandbox
- [x] **Twelve statsmodels** time-series models
- [x] **About cards** on each item that you can select, and **Help me choose**
      ([choosing a model](../guides/choosing-a-model.md))
- [x] The Python client and the CLI for all of these items
- [x] The screens of the web app for all of these items:
      the visual workflow builder, the run monitor, Guided recipes, the policy editor, the
      review queue, the explorers, About cards and the chooser, and budget plans

We verified these items end to end on a live local stack
([M3.5 verification](../operations/m35-verification.md)):

- the canonical workflow
- governance with a second person
- mmm-framework with a budget plan
- the explorers and the chooser
- the CLI.

Some work is not done yet, the same as for v0.4. It includes a real Google Cloud project,
Docker-isolated executors, and the sessions with five non-technical testers.

## What v0.4 contains

- [x] **Guided mode**: four templates (geo marketing mix, weekly forecast, demand by store,
      customer choice). Also a question flow with role detection, drafts with "View as YAML",
      and the persona lock ([tutorial](../guides/guided-tutorial.md)).
- [x] **Sandboxed ingest** of CSV, Parquet, MFF and Meridian-shaped tables. Refusals are in
      plain language.
- [x] **Business priors** as ROI or share ranges. A sandbox translates them and shows a
      prior-predictive preview. Also **lift tests** as calibration
      ([Guided modelling](../guides/guided-modelling.md)).
- [x] The **sanity scorecard** that each fit writes, and the **promotion gate** that uses it
- [x] **What-if sliders** over response grids for each draw, "Run exact", and forecast and
      counterfactual views
- [x] **Endpoints and aliases**, `score:<alias>` tokens, **snippets** in seven languages, and
      a **Google Sheets** card ([Connect Google Sheets](../tutorials/google-sheets.md))
- [x] **Signed webhooks** with an outbox, retries, a delivery log, replays and an SSRF guard
      ([Receive webhooks](../guides/webhook-receivers.md))
- [x] **Saved plots, static renders and embed links** ([Embed a chart](../tutorials/embed-a-chart.md))
- [x] The endpoints of the Python client, and new CLI commands
- [x] **Sign-up** with e-mail verification and abuse controls, **tenant deletion**, the
      **GDPR export**, and a **Cloud Armor** policy
- [x] The Integrations hub, trust pages and admin additions in the web app

We verified these items end to end on a laptop
([M3 verification](../operations/m3-verification.md)). The exit of M3 also needs the
sessions with five non-technical testers. These sessions are not done yet. Some work against
a real Google Cloud project is not done yet, the same as for v0.3:

- the [[verify M0] checklist](../operations/gcp-deployment.md#before-you-start-the-verify-m0-checklist)
- cold-start measurements
- public workspaces on an executor that enforces egress and DNS.

## Next: v0.6 · M4, expert plugins and MCP

**Plugin upload for allow-listed experts.** Experts upload their own model plugins. The
platform builds them into hash-pinned overlays, with native wheels from a curated mirror. The
contract kit checks each plugin on upload. Also fast dev overlays and a vetted tier of ported
sibling models.

An **MCP server** gives AI assistants and agents a small, curated set of tools that are safe
from injection. The tools list and describe models, score an endpoint, read a run and get a
plot.

Budget optimisations already run as **in-sandbox `optimize` jobs**, and backtests run as
workflow nodes (v0.5). **`Sweep` and `SBC`** each run as one parent. [More](expert-plugins.md).

## After v0.6

This is the planned sequence:

1. **Public code upload** (M5): uploaded plugins for all tenants. This release needs these
   controls:
    - a separate execution project
    - signed base images
    - verification before code runs
    - Sybil controls
    - a takedown process
    - a rehearsed kill-switch drill
    - an external penetration test.

    The security suite must first be green for two weeks.
2. **Scale and depth** (M6). Each item comes when real usage needs it:
    - warm scorers for exact answers in less than one second
    - GPU and large-data executors
    - other clouds
    - recursive forecasting
    - chained models (full-funnel and KPI graphs)
    - a plugin marketplace with a consent model.

!!! note "Dates"
    We cut a release when it passes its exit criteria, not on a calendar date. Read the
    [changelog](../project/changelog.md) to see the changes that we released.
