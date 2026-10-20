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
images, fields, maps and graphs, and 14 new plot recipes (36 in all) for maps, networks, trees,
polar charts and images. New workflow nodes give posterior summaries, posterior predictive
checks, stacking weights, scores and plots in a run. Six new recipes (ten in the gallery) use
them. Each ported case has a tutorial in `docs/tutorials/pymc-challenges/`, and
`python -m mapi_plugin_science.export` writes the data of a case to a file that you can upload.
The documentation (`make docs-serve`) starts at `docs/index.md`.

> Status: under active development. The latest release is v0.13.0 (Causal models and Bayesian state space). See [CHANGELOG.md](CHANGELOG.md).

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
| `packages/sdk-pymc`, `packages/sdk-sklearn` | Model base classes for PyMC and scikit-learn/xgboost |
| `packages/steps-core`, `packages/plots-core` | Built-in preprocessing steps and plot recipes |
| `packages/runner` | Sandboxed run harness (supervisor + worker) |
| `packages/server` | FastAPI control plane, job orchestration, executors |
| `packages/gcs-shim` | Local Google Cloud Storage emulator |
| `packages/workflows` | Workflow engine and node implementations (tests, transforms, splits, searches) |
| `packages/cli`, `packages/client` | `mapi` CLI and Python client |
| `plugins/*` | Built-in model plugins (MMM on mmm-framework, time series, statsmodels, tabular, science, causal) |
| `conformance/` | Conformance corpus of reference models |
| `apps/web`, `apps/plot-sandbox` | React SPA and the isolated plot renderer |
| `docs/` | Documentation site (MkDocs Material) |

## Licence

SDK, contracts, plugins, CLI and client are **Apache-2.0**; the platform (server, runner, apps, infra) is
**AGPLv3 or commercial**. See [LICENSE.md](LICENSE.md).
