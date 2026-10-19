---
description: Run the whole platform on your laptop and fit, inspect and score a geo marketing-mix model from the browser, in about ten minutes.
---

# Quickstart: zero to first model in 10 minutes <span class="mapi-badge">v0.2</span>

In this quickstart, you start the platform on your own machine and load a demo dataset. Then
you fit a hierarchical geo marketing-mix model from the web app. You read its scorecard and
plots, and you score an 8-week forecast. You do not need Docker or a cloud account.
PostgreSQL is embedded, and the object storage is a local emulator of Google Cloud Storage.

| Step | What occurs | Time on a laptop |
|---|---|---|
| [1. Install](#1-install) | Python and JavaScript dependencies | 2–5 min the first time |
| [2. Start the stack](#2-start-the-stack) | Database, object store, API, worker, web app, plot sandbox | about 10 s |
| [3. Load the demo](#3-load-the-demo) | Three datasets and a model spec, through the API | about 40 s |
| [4. Fit from the browser](#4-fit-from-the-browser) | A sandboxed fit run that you watch live | 20–60 s |
| [5. Inspect](#5-inspect-the-model) | Scorecard, diagnostics, plots, provenance | – |
| [6. Score](#6-score-a-forecast) | An 8-week forecast. The run uses the fitted preprocessing again. | about 10 s |

We did exactly these steps on a new clone when we wrote this page. The time from `make dev`
to a scored forecast was less than three minutes. The screenshots below come from that run.

## Before you start

You need these items:

- macOS or Linux, with about 2 GB of free disk space.
- [uv](https://docs.astral.sh/uv/). If necessary, it installs Python 3.12 for you.
- Node.js 22 or newer, with pnpm. `corepack enable` gives the pinned pnpm version.
- `git`. If you want to follow the HTTP examples later, you also need `jq` and `curl`.

## 1. Install

```bash
git clone <repository-url> modeling-api
cd modeling-api
make install          # uv sync --all-packages, then pnpm install
```

`make install` puts all the Python packages of the workspace into `.venv`. It puts the
dependencies of the web app into `node_modules`. It installs nothing globally.

## 2. Start the stack

```bash
make dev
```

`make dev` runs all the services in the foreground. Each output line starts with the name of
its service. When the stack is ready, `make dev` shows `all 6 services up`. Do not stop
`make dev`. To stop all the services, push ++ctrl+c++.

The first start also puts a sandboxed *describe* run in the queue. This run exports the
catalog of installed models and steps. It finishes in a few seconds.

| Service | Address |
|---|---|
| Web app | <http://127.0.0.1:5173> |
| API | <http://127.0.0.1:8000> (OpenAPI document at `/openapi.json`) |
| Plot sandbox (a separate origin, on purpose) | <http://127.0.0.1:5174> |
| GCS shim (signed URLs point here) | <http://127.0.0.1:4443> |

!!! tip "Port 8000 (or any other) already taken?"
    Export the applicable variable before `make dev`. Also export it in each other shell
    that you use:

    ```bash
    export MAPI_API_URL=http://127.0.0.1:18000
    export MAPI_GCS_ENDPOINT=http://127.0.0.1:14443
    export MAPI_WEB_ORIGIN=http://127.0.0.1:15173
    export MAPI_PLOTS_ORIGIN=http://127.0.0.1:15174
    ```

    `make dev`, `make seed`, the CLI, the Python client and the web app all read the same
    variables. We made this page on exactly these ports.
    [Troubleshooting](../operations/troubleshooting.md#port-conflicts) gives more data.

## 3. Load the demo

In a second shell, type this command:

```bash
make seed
```

```text
API http://127.0.0.1:8000 is ok (version 0.9.0)
uploading eu_media_daily (17 KiB) ...
  dataset://default/eu_media_daily@v1 ready
uploading fx_daily (17 KiB) ...
  dataset://default/fx_daily@v1 ready
uploading holidays_eu (7 KiB) ...
  dataset://default/holidays_eu@v1 ready
spec spec://default/eu-geo-mmm@r1: valid
```

`make seed` uses only the public API. For each table, it does these steps:

1. It asks for a signed upload URL.
2. It sends the file directly to object storage.
3. It starts a sandboxed *ingest* run. The run opens the file and checks it against its
   schema. Then it stores an immutable, content-addressed version.

Then `make seed` saves the model spec and validates it.

The data is the E33 example of the conformance corpus. It contains these tables:

- 30 weeks of daily spend for each market and channel, for NL, DE and UK.
- Daily revenue for each market (UK in GBP).
- An FX table.
- A holiday calendar.

To score, you need the spend that you plan for the next weeks. Upload it with the CLI:

```bash
uv run python scripts/seed.py --export demo/     # the demo tables, score payloads and spec
uv run mapi datasets upload demo/eu_media_future.zarr.zip --name eu_media_future
```

## 4. Fit from the browser

1. Open <http://127.0.0.1:5173>.
2. Go to **Build**.
3. Click the `eu-geo-mmm` row.

![The spec editor with the seeded spec, valid, and its pipeline](../assets/screenshots/editor.png){ .mapi-shot }

When the editor opened, it validated the spec on the server. All `dataset://` references,
all steps and the model settings resolve. The panel on the right shows what each step of the
pipeline does to the data. The split into training and holdout weeks is after the `weekly`
step. The reason is that a week exists only from that stage on.

Push **Fit**. The run page follows the run through server-sent events:

![A fit run while the sampler draws](../assets/screenshots/run-progress.png){ .mapi-shot }

- *Starting the sandbox*: the supervisor of the run read its one-time bootstrap object. It
  deleted the object before model code started.
- `DEV_MODE_ISOLATION` is usual on a laptop. The local executor cannot run the model code as
  a separate user. Containers and the cloud executor can.
- The progress bar and the message come directly from the sampler.

The spec uses very small sampler settings, so the fit takes much less than a minute. On a new
machine, the first fit also compiles PyTensor code. When the fit succeeds, the platform does
these steps:

1. It checks each file that the run wrote against the checksums of the storage.
2. It copies the files to a location that runs cannot get to.
3. It signs a manifest.

Push **Inspect model://default/eu-geo-mmm@v1**.

## 5. Inspect the model

![The scorecard: convergence warnings and holdout metrics](../assets/screenshots/scorecard.png){ .mapi-shot }

The scorecard flags R-hat and effective sample size. Two chains of 100 draws are not
sufficient to converge. Each warning tells you what to do. The metrics come from the 8
holdout weeks. The model scored these weeks through the same code path as a forecast. This
occurred before the model was fitted again on all the data (`fit.mode: evaluate_then_refit`).

Open **Plots** → **Contribution by channel**:

![Revenue in parts: baseline, channels, seasonality and holidays](../assets/screenshots/decomposition.png){ .mapi-shot }

Each plot is a Vega-Lite or Plotly spec that the server sanitized. The plot shows inside an
`<iframe sandbox="allow-scripts">` that comes from the own origin of the plot sandbox.
**Provenance** shows the signed manifest, each stored object with its generation, and the
lineage.

## 6. Score a forecast

Push **Score** (top right). **What this model needs** lists the inputs that the model reads
and the data that it was fitted on. The form shows only the modes that this model supports.

1. Keep **Forecast** and **8** periods.
2. Under **Values to score**, select **Use a dataset**.
3. Type `dataset://default/eu_media_future@v1`. Alternatively, keep **Type values** and type
   the planned spend into the grid. Then you do not need an upload.
4. Under **Advanced**, keep the `fx` side table at the version that the fit used. It covers
   the forecast weeks.
5. Push **Forecast**.

![An 8-week forecast for each market, in weekly EUR](../assets/screenshots/score-forecast.png){ .mapi-shot }

The run did not fit the model again. It used the fitted preprocessing again: currency
conversion, weekly resampling, scaling, and the last weeks of spend that each market carries
into the adstock. Then it inverted the result back to weekly EUR. The `output_space` of the
result tells you this.

## Stop, and start again later

```bash
make stop             # or Ctrl-C in the make dev shell
make dev              # everything you loaded is still there (.local/)
make clean-local      # delete the local database and objects, start from scratch
```

## Where next

<div class="grid cards" markdown>

-   :material-monitor-dashboard: **[Tour every page of the web app](../guides/web-ui.md)**

    Datasets and CSV mapping, revisions and dry-runs, run controls, the data tree and
    counterfactuals.

-   :material-console: **[Do the same from the command line](../guides/cli.md)**

    `mapi datasets upload`, `mapi fit --remote @`, `mapi score` and `--json` for scripts.

-   :material-language-python: **[Or from Python](../guides/python-client.md)**

    `mapi_client.Client`: uploads, live events, model versions and `xr.DataTree` results.

-   :material-book-open-variant: **[Understand what happened](../concepts/architecture.md)**

    The control plane, the run sandbox, storage integrity and the event path.

</div>
