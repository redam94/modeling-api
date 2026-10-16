---
description: What was verified of M3.5 (workflows, governance, explorers, mmm-framework, statsmodels) against a live local stack, the defects the runs found and fixed, how to rerun the journeys, and what still needs Google Cloud, Docker or people.
---

# M3.5 verification <span class="mapi-badge">v0.5</span>

M3.5 adds workflows, governance policies and validations, the Data and Model Explorers,
mmm-framework as the marketing-mix engine, the statsmodels time-series models, About cards
and *Help me choose*. Every part has unit, integration and mocked-browser tests. This page
records the step after those: the five journeys below run **through the running stack**,
from outside its processes, with real fits in the runner, the real orchestrator driving
workflow runs, and the web app talking to the real API.

Verified on 2026-10-16 on branch `fix/m35-e2e` (v0.5 development), on an Apple-silicon
laptop (8 cores, 16 GB, macOS), against a live `make dev` stack:

- the API, worker, web app (Vite dev server) and plot sandbox on non-default ports (API
  `18000`, GCS shim `14443`, web `15173`, plot sandbox `15174`) with their own
  `MAPI_DATA_DIR`;
- the `dev` profile: the development identity provider (`admin@dev.local` submits,
  `analyst@dev.local` reviews) and the outbox mailer;
- the GCS shim instead of Cloud Storage and the embedded `pgserver` Postgres;
- every run on the `local-process` executor (no Docker daemon, no cloud project).

## Summary

| Journey | Verified through the live stack | Result |
|---|---|---|
| 1. The canonical time-series workflow | A weekly KPI with a unit root (a random walk plus a price driver with a true effect of -3) through the `ts_readiness_arimax` template, followed over server-sent events (API), in the run monitor (browser, Expert) and as a Guided recipe started from the Data Explorer | ADF (p = 0.62), KPSS, Phillips-Perron and Zivot-Andrews all say the series wanders; the branch differences it (and skips the seasonal difference) and records the four values it read; the AIC search picks ARIMA(1, 1, 1), and the fitted ARIMAX and the backtest use exactly `[1, 1, 1]` with `price`; six expanding-window folds score ARIMAX and the naive benchmarks per horizon; ARIMAX wins (MAE 1.45 against naive 1.68) and is selected; the governance node passes the default forecasting policy. **Pass, after two fixes** (the dev server's workflow graph, a recipe's first run) |
| 2. Governance | A project policy requiring `null.permutation_target` and `null.noise_regressors` with a reviewer's approval; ARIMAX fitted on real signal and on pure noise | Noise: the shuffled-target test fails (13 to 18 of 19 shuffled fits did as well across runs, p = 0.7-0.95), the validation is `checks_failed`, promotion answers `409 NOT_VALIDATED`. Signal: p = 0.05 (better than every shuffled fit), fake drivers get 0.1 % of the result, `awaiting_approval`; promotion refused until approved; the submitter's approval is `SELF_APPROVAL_FORBIDDEN`; the reviewer (an editor with the `reviewer` capability) approves; `validated`; promotion succeeds; the evidence pack exports as HTML (no script) and JSON (with its digest). **Pass, after three fixes** (the policy's families, the reviewer capability in the client, uploading the same data again) |
| 3. mmm-framework | `mmm.bayesian` (150 tuning steps and draws, 2 chains) pooled over 3 regions, 2 channels, 40 weeks | Fitted in about 30 s; the Model Explorer's contributions and response curves (non-decreasing in spend, both channels); a budget plan that keeps the total, respects tv's 0.5-1.5x bounds, reports the chance of beating today's plan and a 5-point efficient frontier, in about 10 s. **Pass, after one fix** |
| 4. Explorers and the chooser | The Data Explorer of the unit-root series; a statsmodels VAR(2) on three weekly series; *Help me choose* for "forecast weekly sales with drivers" | Stationarity per series; impulse responses with bands and a variance decomposition whose shares sum to 1; the browser renders both in the plot sandbox. The chooser now ranks `ts.sarimax` first with four reasons and no caution, and its About card opens. **Pass, after two fixes** |
| 5. The CLI | `mapi workflows validate`, `apply` and `run --watch` on the cookbook's canonical document, exactly as `docs/snippets/cli_v05.sh` runs them (project renamed) | 23 nodes valid; the live board prints every node and decision; the rolling-origin backtest (window 104) and the Diebold-Mariano node run; exit status 0 with the fitted model's reference. **Pass** |

`tests/e2e/test_m35_stack.py` (5 tests, the API and the CLI, about 5 minutes) and
`apps/web/e2e/m35-live.e2e.spec.ts` (7 tests, the browser in Expert and Guided mode, about
3 minutes including a 90-second seed) both pass. The cookbook run from the CLI reuses the
canonical run's unchanged unit-root tests from the cache (`cached_from` names that run).
The earlier end-to-end suites (M1 to M3) were run against the same stack too; see
[Full verification set](#full-verification-set).

## What the runs found and fixed

Eight defects, none of which the unit, integration or mocked-browser tests could see. Each
fix has a regression test.

| Found in | Defect | Fix |
|---|---|---|
| Governance | A policy with `applies_to: {families: [ts]}` - the example on every governance page and in `forecast-policy.yaml` - covered **no** statsmodels model, whose manifests declare the family `forecast` (or `ts_multivariate`, `regime`). Its checks, approval and promotion gate silently never applied: the model fitted on noise was judged by the default policy only, came out `validated` and was promoted. | A family matches a model's declared family or the namespace of its id (`mapi_spec.governance.PolicyMatch`). |
| Governance | The client could not give a member the `reviewer` capability (`add_member` and `set_role` dropped it), so the second person could not be set up without raw HTTP. | `tenants.add_member(..., capabilities=[...])` and `tenants.set_capabilities(...)`, sync and async. |
| Every upload | Uploading data that had been uploaded before - the same file as the next version, or into another dataset or project - failed its ingest with `INTEGRITY_FAILURE` ("ui/explore/index.json already holds different content"). Ingest outputs are stored under the data's content hash, and the Data Explorer views an ingest writes since v0.5 name the dataset version and carry plot timestamps. | Each ingest's explorer views are stored under its run (`t/<tenant>/datasets/<sha>/runs/<run>/ui/explore/`); the data and its sidecars keep the strict same-content check. |
| mmm-framework | `api.explorations.view(..., "response_curves")` (and `mapi explore --show`) returned the section's list of plots instead of its view, for every section whose name sorts after `plots` (`residuals`, `response_curves`, `stationarity`, `stl`...). | The exact view is looked up first; plot files never match. |
| Chooser | *Help me choose* ranked `ts.sarimax` sixth for "forecast weekly sales with drivers" although it is the only model that fits all four answers: items silent on a question also scored 1.0 and ties broke on difficulty. | Ties break on the number of answers an item fits explicitly. |
| Browser | On the dev server, the first page to open a workflow graph crashed with "Invalid hook call" in `ReactFlowProvider`, and a route loading while the spec editor's YAML worker first started failed with "Failed to fetch dynamically imported module": Vite found React Flow and the worker's dependency late, optimised them in a second pass and reloaded. | Both are pre-bundled when the dev server starts (`apps/web/dev-prebundle.ts`; a test requires every package a web worker imports to be listed). Production builds were not affected. |
| Data Explorer | The overview of the most common upload, a time dimension called `time`, read "2 variables over 156 time". | The time dimension is named by its frequency ("156 weeks"). |
| Guided recipes | A Guided user's first run of a recipe in a project failed with "The recipe did not start: Not Found": the click created the workflow from the template, then posted the run to `/v1/workflows//runs` with the id the page was rendered with. The mock project always had the workflow. | A run can name its workflow; the recipe runs the one it just created. |

### The earlier journeys, brought up to date

Run against the same stack, four assertions of the M1 and M3 journeys had gone stale with
M3.5 (the product was right): the E33 spec on mmm-framework no longer has scaling or lag
steps (M1 now checks its currency, weekly and holiday steps), and its scores carry six
components, not five (the M1 browser flow counts the rows); a Guided draft records no
units, so the budget plan is checked by its KPI's name; and the M3 webhook receiver verified
deliveries minutes after they arrived, against the 5-minute signature tolerance (it now
verifies each delivery at its arrival, as a real receiver does).

### An observation, not changed

Under a project policy that makes the shuffled-target test blocking, the cookbook's ARIMA(1,
1, 1) on the random walk fails it: 4 of 19 shuffled refits did as well (p = 0.25), although
the model beats the naive forecast by 25 % on the held-out weeks. The null shuffles the
target in blocks of about a twentieth of the history; for a model that differences the
series (d = 1) the blocks keep most of the persistence the model exploits, so the shuffled
fits forecast nearly as well. The test is right to warn that most of the skill is
persistence, but for integrated series a null that shuffles the differences, or a
time-shift placebo of the drivers, measures the drivers' contribution more directly. The
default forecasting policy keeps this test at *warning*, which is why the canonical
workflow passes it.

## How to rerun

Start a stack on free ports with its own data directory (the variables are those of
[local development](local-development.md)):

```bash
export MAPI_API_URL=http://127.0.0.1:18000 MAPI_GCS_ENDPOINT=http://127.0.0.1:14443
export MAPI_WEB_ORIGIN=http://127.0.0.1:15173 MAPI_PLOTS_ORIGIN=http://127.0.0.1:15174
export VITE_API_BASE=$MAPI_API_URL MAPI_DATA_DIR=$PWD/.local/m35e2e
make dev                    # in one shell; `make stop` with the same variables stops it
```

Then, in another shell with the same variables:

```bash
uv run pytest -m e2e tests/e2e/test_m35_stack.py -s          # the API and CLI journeys
pnpm --filter @mapi/web exec env MAPI_E2E=1 \
  playwright test e2e/m35-live.e2e.spec.ts                    # the browser journeys
make e2e                                                      # or every end-to-end suite
```

Each suite creates its own project in the `default` tenant (`m35e2e-<id>`, `m35ui-<id>`) and
deletes it afterwards; `MAPI_E2E_KEEP=1` keeps it to look at in the app. Timings are written
to `$MAPI_DATA_DIR/e2e/m35-timings.json` and `apps/web/e2e/screenshots/m35-timings.json`, and
the browser suite saves its screens as `apps/web/e2e/screenshots/m35-*.png` (git-ignored).

## Timings

Wall-clock seconds of the last run, on the laptop above (no Docker, no cold starts):

| Step | Seconds |
|---|---|
| Upload and ingest of a small dataset (the sandboxed ingest with its Data Explorer views) | 8-13 |
| The canonical workflow, 19 nodes, through the orchestrator and the runner | 37-40 |
| The cookbook workflow from the CLI, 23 nodes | 45 |
| A validation with the null tests (19 shuffled refits plus 3 fake drivers) | 26 |
| `mmm.bayesian`, tiny settings, 3 regions x 2 channels x 40 weeks | 31-34 |
| A statsmodels VAR(2) fit | 13 |
| A Model Explorer request served from the fit's views | 8 |
| A budget plan with a 5-point frontier | 10-16 |

## Full verification set

On the final tree of `fix/m35-e2e`:

| Check | Result |
|---|---|
| `uv run pytest -q -n 6` | 6988 passed, 12 skipped (Docker, Linux-only and cloud-library tests), 0 failed |
| `make conformance` | 11 of 11 fixtures |
| `make lint` (ruff, the 5 import contracts), `make typecheck`, `make release-check` | clean, clean, passed |
| `pnpm -r lint`, `pnpm -r typecheck`, `pnpm -r build` | clean; the entry chunk is 598 kB of its 640 KiB budget |
| Vitest | web 373, plot sandbox 97 |
| Playwright smoke (mock mode, CSP and Trusted Types enforced, ports 15173/15174) | 78 passed, 2 skipped (the opt-in screenshot specs) |
| The generated fixtures and types (`gen_*_fixtures.py --check`, `openapi:check`) | up to date |
| `uv run --group docs mkdocs build --strict` | passes |
| Every end-to-end suite against the live stack: `uv run pytest -m e2e tests/e2e` (M1, M2, M3, M3.5 and the corpus) and the browser `@e2e` specs (M1, M2, M3, M3.5 and the stack spec) | Python: 48 passed, 1 skipped (the opt-in fit burst); the M1, M3 and M3.5 modules were re-run after their fixes. Browser: 23 passed; the M1 and `stack` specs were re-run after `make seed` (the `stack` spec needs its demo data) and the dev-server fix. |

## What remains unverified

- **Real Google Cloud.** Cloud Storage (the GCS shim stood in), Cloud Run Jobs (every
  workflow node, fit, governance, explore and optimise run used the `local-process`
  executor), Pub/Sub, Cloud SQL and Identity Platform. Cold starts of the runner images add
  to every workflow node, which runs as its own sandboxed run: a 19-node workflow starts about
  15 of them.
- **Docker.** No daemon here, so the images were not built: in particular `base-bayes`
  with mmm-framework, JAX and nutpie, and `base-forecast` with statsmodels and the workflow
  runtime. The `docker` and `docker-gvisor` executors, and so public (shared-tier)
  workspaces, never ran a workflow, a validation or an optimisation.
- **People.** No non-technical user tried the Guided recipes, Help me choose or the About
  cards, and no reviewer used the approval screens on real models; nothing was tried with a
  screen reader. The protocol of the [M3 testers](m3-verification.md#protocol-for-the-five-non-technical-testers)
  applies, with "run the readiness recipe on your weekly sales and say whether you would
  trust the forecast" as an extra task.
- **Scale and real fits.** Only tiny MCMC settings and small panels ran here; full-size
  mmm-framework fits (4 chains, thousands of draws, dozens of geos), governance refit budgets
  on them, and workflows with hundreds of nodes were not timed.
- **Not driven live by these journeys** (covered by the server's integration tests with
  embedded Postgres): schedules and dataset triggers firing, workflow cancellation,
  sub-workflows, the `workflow.run.*` and validation webhooks, tenant-wide policies,
  deprecation and resubmission, the visual workflow editor and the policy editor (their
  mocked Playwright specs only).
