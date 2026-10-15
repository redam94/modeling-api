# modeling-api

**Fit, score and share labelled multi-dimensional models — safely.**

modeling-api is a platform for Bayesian and classical models whose data has named dimensions
(geo × channel × week, household × trip, state × day). You describe a model with a declarative YAML spec or a
small Python plugin; the platform validates the data against an xarray schema, replays the *exact* fitted
preprocessing at score time, runs every job in an isolated sandbox, and gives analysts and data scientists a
web UI with Vega-Lite and Plotly plots.

> Status: under active development. See [CHANGELOG.md](CHANGELOG.md).

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
| `packages/cli`, `packages/client` | `mapi` CLI and Python client |
| `plugins/*` | Built-in model plugins (MMM, time series, tabular) |
| `conformance/` | Conformance corpus of reference models |
| `apps/web`, `apps/plot-sandbox` | React SPA and the isolated plot renderer |
| `docs/` | Documentation site (MkDocs Material) |

## Licence

SDK, contracts, plugins, CLI and client are **Apache-2.0**; the platform (server, runner, apps, infra) is
**AGPLv3 or commercial**. See [LICENSE.md](LICENSE.md).
