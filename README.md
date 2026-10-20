# modeling-api

**Fit, score and share labelled multi-dimensional models — safely.**

modeling-api is a platform for Bayesian and classical models whose data has named dimensions
(geo × channel × week, household × trip, state × day). You describe a model with a declarative YAML spec or a
small Python plugin; the platform validates the data against an xarray schema, replays the *exact* fitted
preprocessing at score time, runs every job in an isolated sandbox, and gives analysts and data scientists a
web UI with Vega-Lite and Plotly plots.

Since v0.5, whole analyses run as versioned **workflows**. An example is unit-root tests →
differencing decisions → AR/MA order search → rolling-origin backtests against naive forecasts →
selection. A **governance policy** can check each model before persons use it. The model must be
better than the same model fitted on shuffled data, and it must give no credit to fake noise
drivers. A second person signs off. Analysts get **Data and Model Explorers**, **About cards**
with *Help me choose*, **mmm-framework** marketing-mix models with budget optimisation and twelve
**statsmodels** time-series models. Later releases added a spec builder with a Visual view
(v0.6), models of 2-D fields (v0.7) and tree datasets from NetCDF-4 and HDF5 files (v0.8).

v0.9 adds the **science gallery**: 15 `sci.*` models from the pymc-challenges case studies, for
images, fields, maps and graphs, new plot recipes for maps, networks, trees, polar charts and
images, and workflow nodes for posterior summaries, predictive checks, stacking weights, scores
and plots. Each ported case has a tutorial in `docs/tutorials/pymc-challenges/`, and
`python -m mapi_plugin_science.export` writes the data of a case to a file that you can upload.

Releases v0.10 to v0.15 add:

- **Score plots by output shape** (v0.10): a map with a frame slider for a grid output, a fan for
  a time series, and ROC, precision-recall, confusion and loss-curve plots for classifiers and
  ADVI fits. The metrics `auc` and `mase` are calculated.
- **Run tracking** (v0.11): model code, workflow nodes and API users log metrics, params, tags,
  notes and structured log lines to a run. Live chain lanes, and run search by metric, param
  and tag (`mapi runs ls --filter 'metric.rmse < 0.5'`).
- **The MMM pack** (v0.12): `mmm.nested` (effects through intermediate measures),
  `mmm.multivariate` (several related results), time-varying channel effects, count and share
  likelihoods, and charts for ROI and mROAS, adstock, waterfalls and lift tests.
- **Causal models and BSTS** (v0.13): `causal.impact`, `causal.synthetic_control` (geo-lift
  tests) and `causal.did`, a Bayesian structural time series (`ts.bsts`), and a workflow that
  turns a geo-lift test into a lift test for `mmm.bayesian`.
- **Run comparison, media and image data** (v0.14): a compare page for 2 to 10 runs,
  experiments and saved views, images, figures and tables logged by a run, image datasets from a
  zip, an image gallery, and multiclass classification (LightGBM, CatBoost, new GLM families).
- **Deep learning** (v0.15): `mapi-jax` networks for tables and images (`dl.mlp`,
  `dl.cnn_classifier`, `dl.finetune_classifier`, `dl.embed`) with training curves, early
  stopping, checkpoints and draws, image collections of up to a million images with resumable
  uploads, pretrained weights as datasets, and a GPU resource class.

The built-in catalog has 50 models: marketing mix (mmm-framework), statsmodels and Bayesian time
series, causal models, tabular and classification models (scikit-learn, xgboost, LightGBM,
CatBoost, hierarchical GLMs), customer choice, science models and neural networks. The
documentation (`make docs-serve`) starts at `docs/index.md`. See
`docs/whats-new-0.15.md` for the details of v0.10 to v0.15.

> Status: under active development. The latest release is v0.15.0 (Deep learning and image collections). See [CHANGELOG.md](CHANGELOG.md).

## Quick start

```bash
make install      # uv workspace + pnpm
make test         # local test suite
make help         # everything else
```

## Repository layout

| Path | What |
|---|---|
| `packages/spec` | Pure-Pydantic contracts (specs, schemas, manifests, plot envelopes) |
| `packages/sdk` | xarray runtime: steps, pipelines, coord plans, bundles, contract kit |
| `packages/sdk-pymc`, `packages/sdk-sklearn`, `packages/sdk-jax` | Model base classes for PyMC and scikit-learn/xgboost, and the JAX/Flax deep learning models (`mapi-jax`) |
| `packages/steps-core`, `packages/plots-core` | Built-in preprocessing steps and plot recipes |
| `packages/runner` | Sandboxed run harness (supervisor + worker) |
| `packages/server` | FastAPI control plane, job orchestration, executors |
| `packages/gcs-shim` | Local Google Cloud Storage emulator |
| `packages/workflows` | Workflow engine and node implementations (tests, transforms, splits, searches) |
| `packages/cli`, `packages/client` | `mapi` CLI and Python client |
| `plugins/*` | Built-in model plugins (MMM on mmm-framework, time series and BSTS, statsmodels, tabular and classification, science, causal) |
| `conformance/` | Conformance corpus of reference models |
| `apps/web`, `apps/plot-sandbox` | React SPA and the isolated plot renderer |
| `docs/` | Documentation site (MkDocs Material) |

## Licence

SDK, contracts, plugins, CLI and client are **Apache-2.0**; the platform (server, runner, apps, infra) is
**AGPLv3 or commercial**. See [LICENSE.md](LICENSE.md).
