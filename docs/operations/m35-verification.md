---
description: What was verified of M3.5 (workflows, governance, explorers, mmm-framework, statsmodels) against a live local stack, the defects the runs found and fixed, how to rerun the journeys, and what still needs Google Cloud, Docker or people.
---

# M3.5 verification <span class="mapi-badge">v0.5</span>

M3.5 adds these items: workflows, governance policies and validations, the Data and Model
Explorers, mmm-framework as the marketing-mix engine, the statsmodels time-series models,
About cards and *Help me choose*. Each part has unit, integration and mocked-browser tests.
This page records the next step. The five journeys below run **through the running stack**,
from outside its processes. Real fits run in the runner, the real orchestrator operates the
workflow runs, and the web app sends requests to the real API.

We did the verification on 2026-10-16 on branch `fix/m35-e2e` (v0.5 development), on an
Apple-silicon laptop (8 cores, 16 GB, macOS), against a live `make dev` stack:

- The API, worker, web app (Vite dev server) and plot sandbox on ports that are not the
  defaults (API `18000`, GCS shim `14443`, web `15173`, plot sandbox `15174`), with their
  own `MAPI_DATA_DIR`.
- The `dev` profile: the development identity provider (`admin@dev.local` submits,
  `analyst@dev.local` reviews) and the outbox mailer.
- The GCS shim in place of Cloud Storage, and the embedded `pgserver` Postgres.
- All runs on the `local-process` executor (no Docker daemon, no cloud project).

## Summary

| Journey | Verified through the live stack | Result |
|---|---|---|
| 1. The canonical time-series workflow | A weekly KPI with a unit root (a random walk and a price driver with a true effect of -3). It went through the `ts_readiness_arimax` template. We followed it over server-sent events (API), in the run monitor (browser, Expert) and as a Guided recipe that we started from the Data Explorer | ADF (p = 0.62), KPSS, Phillips-Perron and Zivot-Andrews all show that the series wanders. The branch differences it (and skips the seasonal difference), and records the four values that it read. The AIC search selects ARIMA(1, 1, 1). The fitted ARIMAX and the backtest use exactly `[1, 1, 1]` with `price`. Six expanding-window folds score ARIMAX and the naive benchmarks for each horizon. ARIMAX is better (MAE 1.45 against naive 1.68), and the workflow selects it. The governance node passes the default forecasting policy. **Pass, after two fixes** (the workflow graph of the dev server, the first run of a recipe) |
| 2. Governance | A project policy that needs `null.permutation_target` and `null.noise_regressors` with the approval of a reviewer. ARIMAX fitted on real signal and on pure noise | Noise: the shuffled-target test fails (across runs, 13 to 18 of 19 shuffled fits were equally good, p = 0.7-0.95). The validation is `checks_failed`, and promotion answers `409 NOT_VALIDATED`. Signal: p = 0.05 (better than all shuffled fits), and fake drivers get 0.1 % of the result, `awaiting_approval`. The server refuses promotion until approval. The approval of the submitter gets `SELF_APPROVAL_FORBIDDEN`. The reviewer (an editor with the `reviewer` capability) approves, and the result is `validated`. Promotion succeeds. The evidence pack exports as HTML (no script) and JSON (with its digest). **Pass, after three fixes** (the families of the policy, the reviewer capability in the client, an upload of the same data again) |
| 3. mmm-framework | `mmm.bayesian` (150 tuning steps and draws, 2 chains), pooled over 3 regions, 2 channels, 40 weeks | Fitted in about 30 s. The Model Explorer shows the contributions and response curves (they do not decrease when spend increases, for the two channels). A budget plan keeps the total and obeys the 0.5-1.5x bounds of tv. It reports the chance to be better than the plan of today, and a 5-point efficient frontier, in about 10 s. **Pass, after one fix** |
| 4. Explorers and the chooser | The Data Explorer of the unit-root series. A statsmodels VAR(2) on three weekly series. *Help me choose* for "forecast weekly sales with drivers" | Stationarity for each series. Impulse responses with bands, and a variance decomposition whose shares add up to 1. The browser shows the two in the plot sandbox. The chooser now puts `ts.sarimax` first with four reasons and no caution, and its About card opens. **Pass, after two fixes** |
| 5. The CLI | `mapi workflows validate`, `apply` and `run --watch` on the canonical document of the cookbook, exactly as `docs/snippets/cli_v05.sh` runs them (with a different project name) | 23 nodes are valid. The live board prints each node and decision. The rolling-origin backtest (window 104) and the Diebold-Mariano node run. The exit status is 0, with the reference of the fitted model. **Pass** |

Two suites pass:

- `tests/e2e/test_m35_stack.py` (5 tests, the API and the CLI, about 5 minutes).
- `apps/web/e2e/m35-live.e2e.spec.ts` (7 tests, the browser in Expert and Guided mode, about
  3 minutes with a 90-second seed).

The unit-root tests of the canonical run did not change. Thus the cookbook run from the CLI
gets them from the cache (`cached_from` names that run). We also ran the earlier
end-to-end suites (M1 to M3) against the same stack. Refer to
[Full verification set](#full-verification-set).

## What the runs found and fixed

The runs found eight defects. The unit, integration and mocked-browser tests could not find
them. Each fix has a regression test.

| Found in | Defect | Fix |
|---|---|---|
| Governance | A policy with `applies_to: {families: [ts]}` applied to **no** statsmodels model. This policy is the example on all governance pages and in `forecast-policy.yaml`. The manifests of the statsmodels models declare the family `forecast` (or `ts_multivariate`, `regime`). Thus the checks, approval and promotion gate of the policy never applied, and there was no message. The model fitted on noise got only the default policy. It became `validated`, and it was promoted. | A family agrees with the declared family of a model, or with the namespace of its id (`mapi_spec.governance.PolicyMatch`). |
| Governance | The client could not give a member the `reviewer` capability (`add_member` and `set_role` removed it). Thus you could not set up the second person without raw HTTP. | `tenants.add_member(..., capabilities=[...])` and `tenants.set_capabilities(...)`, sync and async. |
| Each upload | An upload of data that was uploaded before failed its ingest with `INTEGRITY_FAILURE` ("ui/explore/index.json already holds different content"). This occurred for the same file as the next version, or in a different dataset or project. Ingest outputs are stored under the content hash of the data. Since v0.5, the Data Explorer views that an ingest writes name the dataset version and contain plot timestamps. | The explorer views of each ingest are stored under its run (`t/<tenant>/datasets/<sha>/runs/<run>/ui/explore/`). The data and its sidecars keep the strict same-content check. |
| mmm-framework | `api.explorations.view(..., "response_curves")` (and `mapi explore --show`) returned the list of plots of the section, not its view. This occurred for each section whose name comes after `plots` in alphabetical sequence (`residuals`, `response_curves`, `stationarity`, `stl`...). | The code looks for the exact view first. Plot files never agree. |
| Chooser | *Help me choose* put `ts.sarimax` in position six for "forecast weekly sales with drivers". But it is the only model that agrees with all four answers. Items that did not answer a question also got a score of 1.0, and the difficulty decided ties. | The number of answers that an item agrees with explicitly decides ties. |
| Browser | On the dev server, the first page that opened a workflow graph crashed with "Invalid hook call" in `ReactFlowProvider`. A route that loaded while the YAML worker of the spec editor started for the first time failed with "Failed to fetch dynamically imported module". Vite found React Flow and the dependency of the worker late. It optimised them in a second pass and loaded the page again. | The dev server pre-bundles the two when it starts (`apps/web/dev-prebundle.ts`). A test makes sure that the list includes each package that a web worker imports. Production builds were not affected. |
| Data Explorer | The overview of the most usual upload, a time dimension with the name `time`, showed "2 variables over 156 time". | The name of the time dimension is now its frequency ("156 weeks"). |
| Guided recipes | The first run of a recipe in a project by a Guided user failed with "The recipe did not start: Not Found". The click made the workflow from the template. Then it sent the run to `/v1/workflows//runs` with the id from when the page was shown. The mock project always had the workflow. | A run can name its workflow. The recipe runs the workflow that it made. |

### The earlier journeys, brought up to date

We ran the M1 and M3 journeys against the same stack. Four of their assertions were not
correct after M3.5, but the product was correct:

- The E33 spec on mmm-framework does not have scaling or lag steps now. M1 now does a check
  of its currency, weekly and holiday steps.
- Its scores have six components, not five. The M1 browser flow counts the rows.
- A Guided draft records no units. Thus the test finds the budget plan by the name of its
  KPI.
- The M3 webhook receiver verified deliveries some minutes after they came. This was later
  than the 5-minute signature tolerance. Now it verifies each delivery when it comes, as a
  real receiver does.

### An observation, since fixed

We had a project policy that made the shuffled-target test blocking. Under this policy, the
ARIMA(1, 1, 1) of the cookbook on the random walk failed the test. 4 of 19 shuffled refits
were equally good (p = 0.25). But the model was 25 % better than the naive forecast on the
held-out weeks. The null shuffled the target in blocks. This kept most of the persistence
that a differenced model uses.

The M3.5 review replaced this null before the release. Now, for a model with drivers, the
null shuffles the drivers in blocks and does not change the result. For a model without
drivers, the null shuffles the differences and integrates them again. The null test scores
each fit by its holdout skill against the null forecast (refer to
[null tests explained](../guides/null-tests.md)). On the same data, the model of the
cookbook is now better than all 20 shuffled fits (p = 0.048).

## How to rerun

Start a stack on free ports with its own data directory. The variables are those of
[local development](local-development.md):

```bash
export MAPI_API_URL=http://127.0.0.1:18000 MAPI_GCS_ENDPOINT=http://127.0.0.1:14443
export MAPI_WEB_ORIGIN=http://127.0.0.1:15173 MAPI_PLOTS_ORIGIN=http://127.0.0.1:15174
export VITE_API_BASE=$MAPI_API_URL MAPI_DATA_DIR=$PWD/.local/m35e2e
make dev                    # in one shell; `make stop` with the same variables stops it
```

Then, in a different shell with the same variables:

```bash
uv run pytest -m e2e tests/e2e/test_m35_stack.py -s          # the API and CLI journeys
pnpm --filter @mapi/web exec env MAPI_E2E=1 \
  playwright test e2e/m35-live.e2e.spec.ts                    # the browser journeys
make e2e                                                      # or every end-to-end suite
```

Each suite makes its own project in the `default` tenant (`m35e2e-<id>`, `m35ui-<id>`). It
deletes the project at the end. To keep the project and look at it in the app, set
`MAPI_E2E_KEEP=1`. The suites write the times to `$MAPI_DATA_DIR/e2e/m35-timings.json` and
`apps/web/e2e/screenshots/m35-timings.json`. The browser suite saves its screens as
`apps/web/e2e/screenshots/m35-*.png` (git ignores them).

## Timings

These are the wall-clock seconds of the last run, on the laptop above (no Docker, no cold
starts):

| Step | Seconds |
|---|---|
| Upload and ingest of a small dataset (the sandboxed ingest with its Data Explorer views) | 8-13 |
| The canonical workflow, 19 nodes, through the orchestrator and the runner | 37-40 |
| The cookbook workflow from the CLI, 23 nodes | 45 |
| A validation with the null tests (19 shuffled refits and 3 fake drivers) | 26 |
| `mmm.bayesian`, tiny settings, 3 regions x 2 channels x 40 weeks | 31-34 |
| A statsmodels VAR(2) fit | 13 |
| A Model Explorer request served from the views of the fit | 8 |
| A budget plan with a 5-point frontier | 10-16 |

## Full verification set

On the final tree of `fix/m35-e2e`:

| Check | Result |
|---|---|
| `uv run pytest -q -n 6` | 6988 passed, 12 skipped (Docker, Linux-only and cloud-library tests), 0 failed |
| `make conformance` | 11 of 11 fixtures |
| `make lint` (ruff, the 5 import contracts), `make typecheck`, `make release-check` | clean, clean, passed |
| `pnpm -r lint`, `pnpm -r typecheck`, `pnpm -r build` | clean. The entry chunk is 598 kB of its 640 KiB budget |
| Vitest | web 373, plot sandbox 97 |
| Playwright smoke (mock mode, CSP and Trusted Types enforced, ports 15173/15174) | 78 passed, 2 skipped (the opt-in screenshot specs) |
| The generated fixtures and types (`gen_*_fixtures.py --check`, `openapi:check`) | current |
| `uv run --group docs mkdocs build --strict` | passes |
| All end-to-end suites against the live stack: `uv run pytest -m e2e tests/e2e` (M1, M2, M3, M3.5 and the corpus) and the browser `@e2e` specs (M1, M2, M3, M3.5 and the stack spec) | Python: 48 passed, 1 skipped (the opt-in fit burst). We ran the M1, M3 and M3.5 modules again after their fixes. Browser: 23 passed. We ran the M1 and `stack` specs again after `make seed` (the `stack` spec needs its demo data) and the dev-server fix. |

## What remains unverified

- **Real Google Cloud.** Cloud Storage (the GCS shim was in its place), Cloud Run Jobs (each
  workflow node, fit, governance, explore and optimise run used the `local-process`
  executor), Pub/Sub, Cloud SQL and Identity Platform. Each workflow node runs as its own
  sandboxed run, and the cold starts of the runner images add time to each node. A 19-node
  workflow starts about 15 of these runs.
- **Docker.** There is no daemon here, so we did not build the images. In particular, we did
  not build `base-bayes` with mmm-framework, JAX and nutpie, and `base-forecast` with
  statsmodels and the workflow runtime. The `docker` and `docker-gvisor` executors never ran
  a workflow, a validation or an optimisation. Thus public (shared-tier) workspaces also
  never ran them.
- **People.** No non-technical user tried the Guided recipes, Help me choose or the About
  cards. No reviewer used the approval screens on real models. Nobody tried the app with a
  screen reader. The protocol of the
  [M3 testers](m3-verification.md#protocol-for-the-five-non-technical-testers) applies. Add
  this task: "run the readiness recipe on your weekly sales and say whether you would trust
  the forecast".
- **Scale and real fits.** Only tiny MCMC settings and small panels ran here. We did not
  measure the time of full-size mmm-framework fits (4 chains, thousands of draws, dozens of
  geos), of governance refit budgets on them, or of workflows with hundreds of nodes.
- **Not operated live by these journeys** (the integration tests of the server with
  embedded Postgres include them): schedules and dataset triggers that fire, workflow
  cancellation, sub-workflows, the `workflow.run.*` and validation webhooks, tenant-wide
  policies, deprecation and resubmission. Also the visual workflow editor and the policy
  editor (only their mocked Playwright specs).

## Final integration (v0.5.0)

We merged the M3.5 review fixes: modeling and statistics, platform server, web,
client/CLI/docs, and the v0.5 documentation site. Then we ran the whole set above again on
the integrated `develop`. We used the same laptop and ports, with a new `MAPI_DATA_DIR`:

| Check | Result |
|---|---|
| `uv run pytest -q -n 6`, two times | 7116 passed, 12 skipped. The first run also had one failure that depended on the test sequence (two runner tests left their test models in the shared plugin registry). We repaired it |
| `make conformance` | 11 of 11 fixtures |
| `make lint`, `make typecheck`, `mkdocs build --strict`, `make tf-validate` | clean |
| `pnpm -r lint`, `typecheck`, `build`; Vitest | clean. web 384, plot sandbox 97 |
| Playwright smoke (mock mode, ports 15173/15174) | 79 passed, 4 skipped (the opt-in screenshot specs) |
| `uv run pytest -m e2e tests/e2e` (M1, M2, M3, M3.5) | 46 passed, 3 skipped (opt-in burst, two purge checks that need a zero grace period). This was after we updated two M3.5 assertions that were not correct (the readiness branch now also reads the over-differencing guard) |
| Browser `@e2e` specs (M1, M2, M3, M3.5, stack) | 23 passed, after the M3.5 spec used the seasonal-first readiness sequence |

We captured the outputs of the governance walkthrough in [What's new](../whats-new-0.5.md),
the [cookbook](../guides/workflow-cookbook.md) and
[null tests explained](../guides/null-tests.md) again from this stack.
