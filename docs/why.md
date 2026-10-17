---
description: The problems modeling-api solves, and a fair comparison with notebooks plus an ad-hoc Flask app, with workflow orchestrators, with marketing-mix vendors and libraries, and with generic ML model serving - including when you should use something else.
---

# Why modeling-api?

Most statistical models that matter to a business are built in a notebook, explained in a
slide deck and then stop. Getting them in front of the people who make decisions, keeping
them honest after the data changes, proving they learned something real, and letting other
tools use them takes a second project that is mostly plumbing. modeling-api is that second
project, done once, for a specific kind of model: **Bayesian and classical models of
labelled, multi-dimensional data** (panels, time series, hierarchies) whose preprocessing and
uncertainty must survive the trip to production - and whose analysis must be repeatable and
defensible.

## The problems it solves

| The problem | What usually happens | What modeling-api does |
|---|---|---|
| **The preprocessing at score time is not the preprocessing at fit time** | The scaling, lags and currency conversion are rewritten in the service, or refitted on the new data, or forgotten | Steps store what they learned at fit time and **replay** it at score time, including per-group look-back tails; inverse transforms bring answers back to your units ([Pipelines](concepts/pipelines.md)) |
| **Data is positional** | Column 7 changes meaning, a new market is "row 12", a forecast's lags start from the wrong week | Data is an `xr.Dataset` with named dims; each dim declares what may happen at score time (`fixed`, `extend`, `new_levels`) and requests are checked against it before any compute ([Labelled data](concepts/labelled-data.md)) |
| **The analysis lives in someone's head** | "I ran ADF, it looked non-stationary, so I differenced and tried a few ARIMAs" - once, by hand, never written down | A **workflow** runs the tests, takes the decisions by stated rules, searches the orders, backtests against naive forecasts and records every value it read; it reruns on a schedule or when the data changes, reusing unchanged steps ([Workflows](concepts/workflows.md)) |
| **A model goes live because it looks good** | A good-looking fit, an R² on the training data, nobody checks whether noise would have done as well | **Governance policies**: the model must beat itself fitted on shuffled results, give fake noise drivers no credit and beat naive benchmarks out of sample; a second person signs off and endpoints refuse unvalidated versions ([Null tests explained](guides/null-tests.md)) |
| **Uncertainty is dropped or summed wrongly** | A point estimate drives a budget decision; someone adds up 90% quantiles of 40 stores | Every output carries its **representation** (draws, quantiles, interval, point, moments) and only the valid summaries and aggregations are offered ([Representations](concepts/representations.md)) |
| **Every scenario needs the data scientist** | The analyst asks "what if we move 20% to search?" and waits a week | Guided mode: templates, beliefs in business terms, a plain-language report, instant what-if sliders over per-draw response curves and budget plans from the posterior ([Guided mode](concepts/guided-mode.md)) |
| **Picking a model needs a statistician** | The analyst uses whatever the last project used, or the fanciest name | **About cards** on every model, step, node and check, in plain words, and **Help me choose**, which turns a few questions into a ranked shortlist with reasons ([Choosing a model](guides/choosing-a-model.md)) |
| **Data problems are found after the model** | Gaps, a constant driver or a wandering series surface in the residuals a week later | The **Data Explorer** profiles every upload in the sandbox: quality warnings, stationarity per series, cross-correlation with the KPI by lag, STL, collinearity ([Explore a dataset](guides/data-explorer.md)) |
| **Nobody knows whether to trust the model** | Convergence warnings in a log nobody reads | A **sanity scorecard** (convergence, holdout error and coverage, plausibility, data) in plain words, and a promotion gate that uses it ([Scorecard](concepts/guided-mode.md#scorecard)) |
| **Other tools need the model** | A Flask app per model, a shared pickle, a CSV emailed every Monday | **Endpoints** with aliases, history and rollback, scoped tokens, generated snippets, signed webhooks, embeddable charts ([Integrations](concepts/integrations.md)) |
| **Many teams, untrusted code and data** | One shared server where anything can read anything | Tenants with row-level security, a fresh sandbox per run, outputs verified against the storage's own checksums and signed, quotas and a kill switch ([Security model](concepts/security.md)) |

## Compared with notebooks and an ad-hoc Flask app

The most common alternative, and a perfectly good one for a single model and a single team.

| | Notebook + a small web app | modeling-api |
|---|---|---|
| Getting started | Minutes: you already have the notebook | A spec or a `build()` method; Guided templates and workflow recipes need no code at all |
| Freedom | Any library, any code | Any PyMC or scikit-learn model through the plugin contract; built-in models for MMM (mmm-framework), twelve statsmodels time-series models, forecasting, demand and choice. Your own plugins run today on a deployment you control (installed into its runner); uploading them to a shared deployment comes in v0.6, first for allow-listed teams |
| Preprocessing at score time | Copied into the app by hand, or pickled with the model | Declared steps with fitted state, replayed exactly; checked statically for leakage |
| Forecasts and new groups | Whatever the code does with an unseen row or date | Declared per dim, checked before compute, with typed errors (`UNSEEN_LEVELS`, `HORIZON_EXCEEDS_FIT`, …) |
| Repeating the analysis | Re-run the cells and hope the order is right | A versioned workflow with typed ports, validated before it runs, scheduled or triggered by new data, with every decision recorded |
| Validating the model | A holdout number in a slide, if anyone asks | Null tests, benchmarks, residual and stability checks under a versioned policy, a second person's approval, an evidence pack for the auditor |
| Model versions | File names, a model registry if you set one up | Immutable spec revisions, signed bundles, lineage, endpoints with history and rollback |
| Who can use it | People who can run the notebook or call the app | Analysts in Guided mode, other apps through endpoints, sheets, BI embeds, webhooks |
| Security | Whatever the app does; often pickle and an open port | Sign-in, roles, scoped tokens, sandboxed runs, no pickle, verified outputs |
| Operating it | You own the server | You own the deployment (a laptop, Docker Compose or Google Cloud); multi-tenant from the start |

Stay with the notebook when the model is a one-off analysis nobody else needs to call or
check. Move when a second person, a second model or a second application needs it - or when
someone asks "how do we know this is not noise?".

## Compared with workflow orchestrators

Airflow, Prefect, Dagster and their cousins are excellent at running arbitrary tasks on a
schedule across systems. modeling-api's workflows are narrower on purpose: they are the
*statistics* of one analysis, not a data pipeline.

| | General-purpose orchestrator | modeling-api workflows |
|---|---|---|
| A step is | Any Python function or container | A node from a catalog (51 of them: 17 statistical tests, transforms, 7 train/test plans, order searches, fits, backtests, comparisons, governance, reports) or a saved sub-workflow |
| Between steps | Whatever the code passes | Typed ports (`dataset`, `test_result`, `split_plan`, `model_version`, …), checked before the run starts with a JSON pointer to every problem |
| Decisions | Python `if` statements | Branch conditions in a small, safe expression language (never `eval`); the run records the values each condition read |
| Where user data is read | Wherever the task runs | In a fresh sandbox without network; the orchestrator only sees JSON results |
| Reuse | Task caching you configure | Every node's identity is its implementation, parameters and the content of its inputs: unchanged steps are reused automatically |
| Output | Whatever the tasks write | Plain-language verdicts, a report, governance evidence and a model version ready to validate and promote |

Use an orchestrator to move data between systems and to call modeling-api; use a workflow
for the analysis itself. They work well together: an orchestrator uploads the new week of
data, and the dataset trigger runs the workflow.

## Compared with marketing-mix vendors and libraries

Marketing-mix modelling is the first domain pack, so this comparison comes up most. modeling-api
is **not** a new MMM methodology and does not compete with the statistics of the established
libraries; it is a platform that runs MMMs, holds them to governance checks and puts them in
front of planners.

**Open-source MMM libraries** (such as Google's Meridian and Meta's Robyn) are
excellent at the model. They are libraries: you run them in a notebook, and serving,
scenario tools for analysts, validation, versioning and access control are up to you.

- The built-in MMMs run on [mmm-framework](models/mmm-framework.md), an open-source
  (Apache-2.0) Bayesian MMM framework on PyMC; business priors become its ROI priors, lift
  tests its experiment likelihoods, and its `planning` module optimises budgets from the
  posterior ([MMM guide](guides/mmm.md)).
- Meridian-shaped tables (`time`, `geo`, `kpi`, `<channel>_spend`) are recognised by the
  ingest; the Meridian model itself is not shipped (it needs TensorFlow).
- Robyn (R) is not supported.

**Managed MMM services** typically bundle data collection from ad platforms, a methodology,
modelling as a service and planning dashboards. modeling-api does **not** collect data from
ad platforms and offers no consulting; in exchange you own the spec, the data and the fitted
posterior, can read every prior and every check, run models other than MMM on the same
platform, and connect the result to your own tools.

| | Typical managed MMM service | Open-source MMM library | modeling-api |
|---|---|---|---|
| Data collection from ad platforms | Often included | No | No: upload CSV, Parquet, MFF or Meridian-shaped tables |
| The model | The vendor's | Yours, in code | mmm-framework's Bayesian (or fast frequentist) MMM, national or pooled over geos and products, or your own plugin |
| Priors and calibration | Varies, often hidden | Yours, in code | Business priors as ROI or share ranges, lift tests, all visible in the spec |
| Proof it is not fitting noise | Rarely shown | Up to you | Shuffled-sales and fake-channel null tests, effect signs, holdout accuracy, prior sensitivity, with evidence and a reviewer's sign-off |
| Analyst scenarios | Dashboards | You build them | What-if sliders with joint uncertainty, "Run exact", forecasts and counterfactuals |
| Budget optimisation | Usually included | Library functions | In-sandbox `optimize` runs on mmm-framework planning: bounds, groups, risk objectives (P10, CVaR), the efficient frontier |
| Other models on the same platform | No | No | Forecasting, macro systems (VAR/VECM), store demand, customer choice, your own |
| Who owns the posterior | The vendor | You | You |

## Compared with generic ML model serving

MLflow, BentoML, KServe, Seldon and the cloud providers' endpoints are excellent at what they
target: packaging a trained model from almost any framework and serving it with low latency
and autoscaling. modeling-api targets a narrower problem and makes different trade-offs.

<div class="mapi-compare" markdown>

| | Generic model serving | modeling-api |
|---|---|---|
| Input contract | Column or tensor signatures: names and shapes, no dimensions | `xr.Dataset` with named dims, coordinates, roles, units and per-dim score policies |
| Preprocessing at score time | Up to you: bundle it into the artifact, or re-implement it | Declared steps with **fitted state**, replayed exactly; stage-aware splits and per-group context tails |
| New groups and horizons | Whatever the model does with an unseen row | Declared per dim (`fixed`, `extend`, `new_levels` + strategy); checked before compute |
| Uncertainty | Point predictions by default; intervals are a custom output | A first-class **representation** that decides which summaries and aggregations are valid |
| Units of the answer | Whatever space the model was trained in | Inverted to original units, with the output space recorded on every variable |
| Serialization | Framework-native; pickle and cloudpickle are common | Zarr zip + JSON, skops or xgboost JSON; **pickle is rejected** on write and on read |
| Promotion | Usually manual, or a CI job | Gated on a sanity scorecard and, when a policy says so, on a governance validation with null tests and a second person's approval |
| Latency | Milliseconds, warm replicas, autoscaling, GPUs | Batch: one sandboxed run per call (seconds, plus cold start in the cloud), and instant what-if from precomputed grids. No GPUs, no warm scorers yet |
| Frameworks | Almost any | PyMC, scikit-learn, xgboost, statsmodels, mmm-framework and hand-written models through the plugin contract |
| Who it is for | ML engineers serving many model types | Statisticians, data scientists and the analysts and apps that use their models |

</div>

## When to use something else

- **You need sub-second answers from the exact model**, or thousands of requests per second:
  use a model server. modeling-api answers sliders instantly from precomputed grids, but an
  exact score is a sandboxed run.
- **Deep learning or GPU work**: not supported before the scale milestone.
- **General data pipelines** (moving data between systems, joins across databases, calling
  APIs): use an orchestrator. Workflows are for the statistics of an analysis, built from
  catalog nodes, and run without network access.
- **Your model needs the network at run time** (an API, a database): runs have no egress by
  design.
- **You want hosted public code upload today**: on a shared deployment, uploaded plugins come
  first to allow-listed teams (v0.6) and to the public only after an external penetration
  test. On a deployment you control you can install and run your own plugins now.

## Where it stands

v0.5 is complete and tested on a laptop: the workflow, governance, mmm-framework, explorer
and CLI journeys pass end to end against a live stack, as does every earlier journey, and
the numbers on the [home page](index.md#measured-not-promised) come from those runs. Two
things are honestly still open: the cloud controls are tested against fakes until they meet
a real Google Cloud project, and the study with five non-technical testers is still to run.
See [M3.5 verification](operations/m35-verification.md), [M3 verification](operations/m3-verification.md)
and the [roadmap](roadmap/index.md).
