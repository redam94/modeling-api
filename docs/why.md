---
description: The problems modeling-api solves, and a fair comparison with notebooks plus an ad-hoc Flask app, with marketing-mix vendors and libraries, and with generic ML model serving - including when you should use something else.
---

# Why modeling-api?

Most statistical models that matter to a business are built in a notebook, explained in a
slide deck and then stop. Getting them in front of the people who make decisions, keeping
them honest after the data changes, and letting other tools use them takes a second project
that is mostly plumbing. modeling-api is that second project, done once, for a specific kind
of model: **Bayesian and classical models of labelled, multi-dimensional data** (panels,
time series, hierarchies) whose preprocessing and uncertainty must survive the trip to
production.

## The problems it solves

| The problem | What usually happens | What modeling-api does |
|---|---|---|
| **The preprocessing at score time is not the preprocessing at fit time** | The scaling, lags and currency conversion are rewritten in the service, or refitted on the new data, or forgotten | Steps store what they learned at fit time and **replay** it at score time, including per-group look-back tails; inverse transforms bring answers back to your units ([Pipelines](concepts/pipelines.md)) |
| **Data is positional** | Column 7 changes meaning, a new market is "row 12", a forecast's lags start from the wrong week | Data is an `xr.Dataset` with named dims; each dim declares what may happen at score time (`fixed`, `extend`, `new_levels`) and requests are checked against it before any compute ([Labelled data](concepts/labelled-data.md)) |
| **Uncertainty is dropped or summed wrongly** | A point estimate drives a budget decision; someone adds up 90% quantiles of 40 stores | Every output carries its **representation** (draws, quantiles, interval, point, moments) and only the valid summaries and aggregations are offered ([Representations](concepts/representations.md)) |
| **Every scenario needs the data scientist** | The analyst asks "what if we move 20% to search?" and waits a week | Guided mode: templates, beliefs in business terms, a plain-language report and instant what-if sliders over per-draw response curves ([Guided mode](concepts/guided-mode.md)) |
| **Nobody knows whether to trust the model** | Convergence warnings in a log nobody reads | A **sanity scorecard** (convergence, holdout error and coverage, plausibility, data) in plain words, and a promotion gate that uses it ([Scorecard](concepts/guided-mode.md#scorecard)) |
| **Other tools need the model** | A Flask app per model, a shared pickle, a CSV emailed every Monday | **Endpoints** with aliases, history and rollback, scoped tokens, generated snippets, signed webhooks, embeddable charts ([Integrations](concepts/integrations.md)) |
| **Many teams, untrusted code and data** | One shared server where anything can read anything | Tenants with row-level security, a fresh sandbox per run, outputs verified against the storage's own checksums and signed, quotas and a kill switch ([Security model](concepts/security.md)) |

## Compared with notebooks and an ad-hoc Flask app

The most common alternative, and a perfectly good one for a single model and a single team.

| | Notebook + a small web app | modeling-api |
|---|---|---|
| Getting started | Minutes: you already have the notebook | A spec or a `build()` method; Guided templates need no code at all |
| Freedom | Any library, any code | Any PyMC or scikit-learn model through the plugin contract; built-in models for MMM, forecasting, demand and choice. Your own plugins run today on a deployment you control (installed into its runner); uploading them to a shared deployment comes in v0.5, first for allow-listed teams |
| Preprocessing at score time | Copied into the app by hand, or pickled with the model | Declared steps with fitted state, replayed exactly; checked statically for leakage |
| Forecasts and new groups | Whatever the code does with an unseen row or date | Declared per dim, checked before compute, with typed errors (`UNSEEN_LEVELS`, `HORIZON_EXCEEDS_FIT`, …) |
| Model versions | File names, a model registry if you set one up | Immutable spec revisions, signed bundles, lineage, endpoints with history and rollback |
| Who can use it | People who can run the notebook or call the app | Analysts in Guided mode, other apps through endpoints, sheets, BI embeds, webhooks |
| Security | Whatever the app does; often pickle and an open port | Sign-in, roles, scoped tokens, sandboxed runs, no pickle, verified outputs |
| Operating it | You own the server | You own the deployment (a laptop, Docker Compose or Google Cloud); multi-tenant from the start |

Stay with the notebook when the model is a one-off analysis nobody else needs to call. Move
when a second person, a second model or a second application needs it.

## Compared with marketing-mix vendors and libraries

Marketing-mix modelling is the first domain pack, so this comparison comes up most. modeling-api
is **not** a new MMM methodology and does not compete with the statistics of the established
libraries; it is a platform that runs MMMs and puts them in front of planners.

**Open-source MMM libraries** (such as PyMC-Marketing, Google's Meridian, Meta's Robyn) are
excellent at the model. They are libraries: you run them in a notebook, and serving,
scenario tools for analysts, versioning and access control are up to you.

- PyMC-Marketing models run on the platform through the `mmm.pymc_marketing` adapter, with
  business priors translated into its own `model_config` ([MMM guide](guides/mmm.md)).
- Meridian-shaped tables (`time`, `geo`, `kpi`, `<channel>_spend`) are recognised by the
  ingest; the Meridian model itself is not shipped (it needs TensorFlow).
- BayesInsight's `model_def.json` runs as a built-in model or maps onto the geo MMM
  ([import tutorial](tutorials/bayesinsight-import.md)).
- Robyn (R) is not supported.

**Managed MMM services** typically bundle data collection from ad platforms, a methodology,
modelling as a service and planning dashboards. modeling-api does **not** collect data from
ad platforms and offers no consulting; in exchange you own the spec, the data and the fitted
posterior, can read every prior and every check, run models other than MMM on the same
platform, and connect the result to your own tools.

| | Typical managed MMM service | Open-source MMM library | modeling-api |
|---|---|---|---|
| Data collection from ad platforms | Often included | No | No: upload CSV, Parquet, MFF or Meridian-shaped tables |
| The model | The vendor's | Yours, in code | A built-in hierarchical geo MMM, the PyMC-Marketing adapter, BayesInsight's model, or your own plugin |
| Priors and calibration | Varies, often hidden | Yours, in code | Business priors as ROI or share ranges, lift tests, all visible in the spec |
| Analyst scenarios | Dashboards | You build them | What-if sliders with joint uncertainty, "Run exact", forecasts and counterfactuals |
| Budget optimisation | Usually included | Library functions | Atlas against an endpoint today; an in-sandbox `optimize` job in v0.5 |
| Other models on the same platform | No | No | Forecasting, store demand, customer choice, your own |
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
| Promotion | Usually manual, or a CI job | Gated on a sanity scorecard; overrides need an admin's audited reason |
| Latency | Milliseconds, warm replicas, autoscaling, GPUs | Batch: one sandboxed run per call (seconds, plus cold start in the cloud), and instant what-if from precomputed grids. No GPUs, no warm scorers yet |
| Frameworks | Almost any | PyMC, scikit-learn, xgboost and hand-written models through the plugin contract |
| Who it is for | ML engineers serving many model types | Statisticians, data scientists and the analysts and apps that use their models |

</div>

## When to use something else

- **You need sub-second answers from the exact model**, or thousands of requests per second:
  use a model server. modeling-api answers sliders instantly from precomputed grids, but an
  exact score is a sandboxed run.
- **Deep learning or GPU work**: not supported before the scale milestone.
- **Arbitrary workflows or DAGs**: v1 pipelines are linear, and there is no general
  orchestration.
- **Your model needs the network at run time** (an API, a database): runs have no egress by
  design.
- **You want hosted public code upload today**: on a shared deployment, uploaded plugins come
  first to allow-listed teams (v0.5) and to the public only after an external penetration
  test. On a deployment you control you can install and run your own plugins now.

## Where it stands

v0.4 is complete and tested on a laptop: the whole M3 journey passes end to end against a
live stack, and the numbers on the [home page](index.md#measured-not-promised) come from
those runs. Two things are honestly still open: the cloud controls are tested against fakes
until they meet a real Google Cloud project, and the study with five non-technical testers is
still to run. See [M3 verification](operations/m3-verification.md) and the
[roadmap](roadmap/index.md).
