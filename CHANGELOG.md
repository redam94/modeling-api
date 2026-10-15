# Changelog

All notable changes are documented here. The project follows [Semantic Versioning](https://semver.org/);
milestone releases are tagged on `main`.

## [Unreleased]

### Added
- **spec**: the M1 shared contracts, so the API, runner, executors, GCS shim, web app and plot
  sandbox can be built in parallel against one source of truth:
  - `mapi_spec.run`: the run state machine (`RunState`, `RunTrigger`, a pure `transition()` with
    owners, failure classes and attempt bumps), the `RunSpec` bootstrap object, typed event payloads,
    canonical HMAC-SHA256 event signing, the `{"mapi_event": …}` stdout format, `OutputManifest`
    with metadata-only verification, the signed `BundleManifest`, `RunnerEnv` and exit codes.
  - `mapi_spec.storage`: `ObjectMeta`, the async `ObjectStore` protocol, signed GET/PUT and POST
    policy shapes, GCS-style CRC32C/MD5 helpers, storage errors and the bucket/key naming scheme.
  - `mapi_spec.api`: REST DTOs for the v1 surface (each with examples), SSE/`Prefer`/idempotency
    helpers and `M1_ENDPOINTS`, the machine-readable endpoint table.
  - `mapi_spec.localdev`: local ports, origins and environment variable names.
- **spec** (additive, contract v0.1): `RunKind` gains the fan-out parents (`sweep`, `backtest`,
  `sbc`, `campaign`); `RunEventType` gains `checkpoint` and `metric`; `RunEvent.ts` must be
  timezone-aware and is normalised to UTC; `PlotSpec` carries a schema example. The new schemas
  join `export_schemas()` and the contract snapshot.
- **docs**: the "HTTP API (v1)" and "Run protocol" reference pages
  (`docs/reference/http-api.md`, `docs/reference/run-protocol.md`).
- **gcs-shim**: `mapi-gcs-shim`, a local Cloud Storage emulator. It serves the GCS JSON-API
  subset the platform uses, with generation and metageneration preconditions, plus V4 signed
  GET (with `Range`) and PUT URLs and V4 POST policies (key prefix and size range). It adds
  IAM-lite principals (`control`, and `exec` with bootstrap get and delete only), a write-once
  bundles bucket, lifecycle sweeps, and server-computed CRC32C and MD5 on a crash-safe
  filesystem backend. `ShimObjectStore` and the import-guarded `GcsObjectStore` implement
  `mapi_spec.storage.ObjectStore`, and a `pytest11` plugin provides the `gcs_shim` fixture.
  See `docs/reference/gcs-shim.md`.
- **runner**: `mapi-runner`, the sandboxed run harness. A trusted supervisor reads and deletes
  the bootstrap object, starts the untrusted worker with a scrubbed environment (uid 10001 and
  no-new-privs as root, dev mode otherwise), signs every stdout event, caps logs, events,
  scratch and outputs, handles SIGTERM/deadline with partial manifests from checkpoints, and
  uploads outputs plus a signed `OutputManifest`. The worker runs `ingest` (CSV/Parquet with a
  column mapping or the MFF preset, zarr zip, netCDF), `describe`, `validate` (dry run), `fit`
  (bundle, sidecars, plots, `ui/` views, checkpoints), `score`, `evaluate`, `derive` and
  `render` through the SDK harness. See `docs/reference/runner.md`.
- **server**: the M1 control plane (`mapi-server api|worker|migrate|dev`): FastAPI `/v1` API
  covering every `M1_ENDPOINTS` row plus `/v1/me`, spec diff, `ui/` redirects and `:evaluate`;
  SQLAlchemy 2 + Alembic schema (every row carries `tenant_id`/`project_id`) with procrastinate
  on the same Postgres; dispatcher (token bucket, intent row before submit), level-triggered
  reconciler (executor status is the liveness source of truth, STALLED is a warning, orphan
  adoption by nonce), metadata-only finalizer with server-side copy and EC P-256 signed bundle
  manifests verified at URL minting; HMAC-verified event ingestion from the executor pipe and
  `POST /internal/events`; SSE with `Last-Event-ID` resume over one LISTEN connection per
  process; `local-process` and `docker`/`docker-gvisor` executors; OpenAPI 3.1 export
  (`python -m mapi_server.openapi`).
- **web**: `apps/web`, the Expert-mode web app (React 19, TanStack Router/Query/Table, Tailwind +
  shadcn/ui, Monaco + monaco-yaml with live `:validate` diagnostics, SSE run progress per chain,
  dataset upload via signed URLs, model inspection, scoring) and the Guided-mode shell; a pnpm
  workspace at the repo root.
- **plot-sandbox**: `apps/plot-sandbox`, the separate-origin Vega-Lite/Plotly renderer with a
  strict CSP and a validated `postMessage` protocol.
- **devops**: containers, local orchestration and developer tooling for M1:
  - `images/`: `base-bayes` (with a `worker` target), `base-ml` (no PyMC), `base-forecast`,
    `server` (control plane only, checked with import-linter and package assertions at build
    time), `gcs-shim` and `web` (nginx serving the app and the plot sandbox on separate origins
    with strict CSPs). All are digest-pinned, run as non-root with a read-only root filesystem,
    and have health checks. The runner images bake the catalog JSON and a warmed PyTensor/Numba
    cache.
  - `docker-compose.yml` (postgres 16, gcs-shim, migrate, api, worker with the local-process
    executor, web, plot-sandbox), `compose.override.example.yml` and `.env.example`, which
    documents every `MAPI_*` variable.
  - `scripts/dev.py` (`make dev`): runs the whole stack without Docker (pgserver PostgreSQL, GCS
    shim, migrations, api, worker and both Vite servers) with readiness probes, prefixed logs and
    clean process-group shutdown. Also `stop`, `status`, `migrate`, `db-shell` and `clean`.
  - New make targets: `dev`/`run`, `stop`, `status`, `migrate`, `db-shell`, `seed` (the E33 demo
    through the API), `clean-local`, `docker-build`, `docker-pin`, `compose-up/down/logs`,
    `web-build`, `web-test`, `test-all`, `e2e` and `release-check`.
  - CI: a web job, a build-only docker job for every image, and digest-pin and compose checks.
  - Docs: "Local development" and "Containers and docker compose" (Operations), and
    `deploy/README.md`.
- **client**: `mapi-client`, the typed Python client (`Client`, `AsyncClient`) for every
  `M1_ENDPOINTS` row: DTO-typed resources, RFC 7807 errors as `MapiError` subclasses,
  idempotency keys and safe retries, lazy pagination, SSE with `Last-Event-ID` resume, dataset
  upload through signed URLs, `Prefer: wait` scoring, CRC32C-checked transfers without the API
  token. See `docs/reference/cli.md`.
- **cli**: `mapi` (`new`, `check`, `fit`, `score` locally or `--remote`, `runs`, `datasets`,
  `catalog`, `plugin init|test`, `spec upgrade`, `serve`, `config`), with `--json` output and
  ANSI-stripped untrusted text.
- **spec**: `mapi_spec.jobs`, the run job contract shared by the dispatcher and the runner
  (`IngestJob`, `DescribeJob`, `ScoreJob`, `EvaluateJob`, `DeriveJob`, `RenderJob`, input and
  output object names, `MetricsSidecar`, `DATASET_SCHEMA_ATTR`), and `mapi_spec.views`, the UI
  views the web app reads (`mapi.preview/v1`, `mapi.profile/v1`, `mapi.tree-index/v1`).
- **gcs-shim**: CORS for the web origin (`--cors-origin`, default `$MAPI_WEB_ORIGIN`) so browsers
  can use signed URLs, and a `wrap` hook on `ShimServer`.
- **server**: `mapi-server dev` (and so `mapi serve`) starts an embedded GCS shim when nothing
  listens at `MAPI_GCS_ENDPOINT`; the OpenAPI document publishes the run-event payload schemas.
- **docs**: "CLI and Python client" and "Web app and plot sandbox" reference pages; the run
  protocol documents job payloads, object names and UI views.
- **server**: `GET /v1/runs/{run_id}/plots` (`list_run_plots`) returns the plots a succeeded
  run rendered, sanitized on every read; the client has `runs.plots()`.
- **web**: the score form takes the input dataset and the spec's side tables (defaulting to the
  versions the fit used), shows the score run's own plots and pivots the summary by statistic.
- **devops**: `scripts/seed.py --export DIR` writes the E33 demo tables, score payloads and spec
  as files for the web app.
- **tests**: the M1 exit criterion end to end against the running stack: `tests/e2e`
  (Python client: fit, SSE, signed manifest and bundle generations, the bundle read back with
  the corpus' checks, five E33 score requests, bootstrap deletion, forged events) and
  `apps/web/e2e/m1-flow.e2e.spec.ts` (the same flow in the browser, plots in the sandbox
  iframe); a server test proves the finalizer refuses tampered output of the real runner.
- **docs**: "Your first model in the web app" guide; the local-development page covers the
  first start, `make e2e` and the state under `.local/`.

### Changed
- **server**: the object store is `ShimObjectStore` built from the shim's `control` key file
  (`MAPI_GCS_ROOT`, default `$MAPI_DATA_DIR/gcs`, or `MAPI_GCS_KEY_FILE`); local executions get
  the `exec` token from the same directory unless `MAPI_EXEC_TOKEN` is set. RunSpec payloads and
  input names follow `mapi_spec.jobs` (score data under `dataset`, side tables under
  `side.<name>`, `bundle.manifest` passed to score runs); `evaluate`/`derive` default to the fit
  dataset and `render` gets the bundle's `plots.json`. `metrics.json` is read as
  `MetricsSidecar`.
- **runner**: fit writes `coords.json` and ingest writes `summary.json` (the control plane's
  CoordPlan metadata); `preview.json`, `profile.json` and `ui/tree_index.json` follow
  `mapi_spec.views`. The runner tests run against the real GCS shim.
- **web**: `openapi.json` and `schema.d.ts` are generated from the real server
  (`apps/web/scripts/gen_openapi.py`, checked in CI); the `@e2e` specs can reuse a running
  stack on any ports.
- **devops**: `make dev` starts `mapi-gcs-shim serve --root …` with CORS and passes the resolved
  URLs to the Vite servers; compose mounts the shim volume read-only into `api` and `worker`
  (`MAPI_GCS_ROOT`) so they can load the principal keys.
- **sdk**: zarr zips record their data-variable order (group attribute `_mapi_var_order`,
  removed again on read).
- **devops**: `make e2e` runs the browser `@e2e` suite (it ran the mocked smoke suite).

### Fixed
- **spec**: static score validation binds dataset names to canonical names at every
  `inputs_stage`, as the runtime does (a weekly EUR plan entering at `weekly` was refused).
- **server**: the dry-run and evaluate endpoints declare their `Run` response in OpenAPI.
- **seed**: `make seed` ships E33's raw schema with the data, so the fit keeps
  `market: new_levels` and new-market scoring works.
- **client**: the run event stream decodes the live `progress` and `log` frames (sent without
  an id) as `RunLiveFrame`; it aborted on the first progress frame of a real fit.
- **server**: model lineage records the side-table versions a fit read (it was always empty).
- **sdk**: variables come back from a zarr zip in their written order; plot recipes that
  default to the first variable chose a random one on a bundle read back from storage
  (`mmm.response_curves` failed). The response-curve recipe also prefers variables with a
  `channel` dim.
- **plot-sandbox**: composite Vega-Lite specs (concat, facet) rendered at zero width; they now
  keep their own size and scroll when wider than the frame.
- **plots**: the convergence table's columns no longer overlap; the fan chart draws and
  captions only the intervals a summary holds.
- **web**: a spec opened right after "Save spec" no longer shows unsaved changes (the editor
  used CRLF for models created empty on Windows user agents), so Fit is enabled; the run page's
  result column no longer overflows; result tables show dates as dates.
- **devops**: `make seed` ignores a `SIZE` exported by other toolchains (conda).

## [0.1.0] — M0: contract layer — 2026-10-15

First milestone release: the typed contract layer and the in-process xarray runtime, with no
platform services yet. In summary:

- **spec** (`mapi-spec`): pure-Pydantic contracts (DataSchema, SchemaEffect, ModelSpec, ScoreRequest,
  capabilities, representations, PlotSpec envelope, error codes) with static validation and
  JSON Schema export, frozen as contract v0.1.
- **sdk** (`mapi-sdk`): wire formats, the stage/split/context/replay/inverse pipeline engine,
  CoordPlan, bundles without pickle, the `fit_spec`/`score_bundle` harness, the in-process executor
  and the `mapi_sdk.testing` contract kit.
- **steps** (`mapi-steps`): 28 built-in `core.*` preprocessing steps and five split kinds, each with
  exported static effects.
- **pymc** (`mapi-pymc`) and **sklearn** (`mapi-sklearn`): model adapters where an author writes only
  `build()` or `estimator()`.
- **plots** (`mapi-plots`): JSON-only Vega-Lite/Plotly sanitizer and the `core`, `mmm` and `ml` recipe
  catalogs.
- **plugins**: `mmm.hierarchical_geo`, `mmm.pymc_marketing`, `ts.hsgp_trend`, `ts.gaussian_hmm`,
  `tabular.hierarchical_glm` and `tabular.regressor`.
- **conformance** (`mapi-conformance`): the corpus runner and six fixtures (E02, C08, E24, store
  demand, E33, pymc-marketing), all passing at `--size tiny`.
- **docs**: the MkDocs Material site with tested snippets and generated catalogs.

Every workspace package is versioned 0.1.0. The server, runner, CLI, client and GCS shim packages
are still skeletons.

- Repository scaffold: uv workspace, package skeletons, split Apache-2.0 / AGPLv3+commercial licensing.

### Contract v0.1 (frozen)
- `mapi_spec.CONTRACT_VERSION = "0.1"` names the contract the exported JSON Schemas describe; every
  exported schema carries it as `x-mapi-contract-version`.
- The contract is frozen for the v0.1.x line: `packages/spec/tests/snapshots/contract.json` pins a digest
  of every exported schema and the drift test fails on any change. Additive changes (new optional
  fields, new error codes) regenerate the snapshot (`MAPI_UPDATE_SNAPSHOTS=1`); breaking changes bump
  `CONTRACT_VERSION` first.

### Added
- **`mapi-spec`**: the pure-Pydantic contract layer.
  - DataSchema, DimSpec and VarSpec, with schema templates.
  - SchemaEffect propagation; ModelSpec, ScoreRequest and the Sweep, Backtest, SBC and Campaign wrappers.
  - A strict YAML 1.2 loader, canonical JSON and spec hashes.
  - Static validation: split stage, `LEAKAGE_RISK`, dim policies, recursion and the CoordPlan checks.
  - Capabilities, representation rules, the PlotSpec envelope, stable RFC 7807 error codes, the Executor protocol shapes, JSON Schema export and the `mapi-spec` CLI.
- **`mapi-sdk`**: the xarray runtime.
  - Wire formats: XrJSON, single-object zarr v3 zips, DataTree zips and Arrow IPC.
  - Array-level schema validation; the step contract, pipeline engine and stages.
  - Split and per-group context replay; inverse chains with output-space labels.
  - CoordPlan and coordinate canonicalisation, the representation-aware summariser and metrics.
  - Bundle IO with integrity sidecars and no pickle; the `fit_spec`/`score_bundle` harness with `evaluate_then_refit`.
  - The entry-point registry, the in-process executor, and the `mapi_sdk.testing` contract kit (`check_step`, `check_model`, pytest plugin).
- **`mapi-steps`**: 28 built-in `core.*` steps (reshape, value, feature, formula, adstock and saturation).
  - Each exports a templated SchemaEffect and a shipped catalog JSON.
  - Catalog entries for the five split kinds.
- **`mapi-pymc`**: `PyMCModel`, where an author writes only `build()`.
  - `BuildContext` with `hier_effect` new levels.
  - Engines: nutpie, pymc, MAP and ADVI, plus guarded numpyro, blackjax and pathfinder.
  - Segmented, resumable sampling with checkpoints; CRN counterfactuals.
  - Convergence diagnostics, FrozenPredictor fast scoring and `check_fast_score`.
- **`mapi-sklearn`**: `SklearnModel` with an allow-listed estimator registry (sklearn and xgboost).
  - skops and xgboost-JSON persistence.
  - Point, quantile, bootstrap, conformal and classifier plugins (`sklearn.tabular_*`).
  - Pooled new levels; exact or zeroing contributions.
- **`mapi-plots`**: a JSON-only PlotSpec sanitizer (pinned Vega-Lite 6.4.1 and plotly.js 4.1.1 schemas, an expression allow-list, HTML stripping) and role-bound templates.
  - The standard `core.*` recipe catalog, plus the `mmm.*` and `ml.*` domain packs.
  - Altair and Plotly converters with named Arrow datasets, and the `render_plots` batch entry point.
- **`mapi-plugin-mmm`**: marketing-mix model plugins (entry point `mapi.models: mmm`).
  - `mmm.hierarchical_geo` 1.3.0, the E33 hierarchical geo MMM: adstock over `core.lag_tensor` features, logistic/Hill saturation, channel effects partially pooled across geos, new geos from the hyperprior, additive contributions.
  - Fit-cached derived quantities in original units: contributions, per-draw response-curve grids for Guided sliders, marginal cost/ROI; a decision objective for budgets; fast scoring.
  - `mmm.pymc_marketing` 1.0.0, an adapter around pymc-marketing 1.2's `MMM` (verified on PyMC 6.3) that owns scaling, carry-over and units and never pickles.
  - Declarative priors in pymc-marketing's dict form, typed per parameter so wrong priors fail static validation; a synthetic E33 simulator with known parameters and a parameter-recovery test.
- **`mapi-sdk`**: `outputs.derived` with `cache: fit` is computed on the production fit through the plugin's `derive` hook; `ScoreContext.to_original` maps model-stage arrays to the output space.
- Cross-package conformance suites:
  - the plan's xgboost spec through real steps, models and plots;
  - an E33-shaped `core.*` pipeline in front of a PyMC model.
- **`mapi-plugin-ts`**: `ts.hsgp_trend` (C08: HSGP trend + seasonality whose domain comes from the anticipated coords; `HORIZON_EXCEEDS_FIT` past `max_extension`; sentinel months left out of the likelihood) and `ts.gaussian_hmm` (E24: forward-algorithm HMM over padded tracks with a `terminal_state` group, forecast and filter modes, relabelling postprocess, LOO declared invalid).
- **`mapi-plugin-tabular`**: `tabular.hierarchical_glm` (E02: partially pooled GLM in long layout, hyperprior new groups, group-level covariates, four families) and `tabular.regressor` (plan §6.2 store demand: quantile regression restricted to the registry's quantile-capable estimators).
- **`mapi-conformance`**: the corpus runner `python -m mapi_conformance run [--size tiny|full] [--fixture id]` (validate, fit, score requests, evaluate, contract kit, plots, expectations; summary table and JSON) with the `e02`, `c08`, `e24`, `xgb_store_demand`, `e33` and `pymc_marketing` fixtures; `pytest -m conformance`, `make conformance` (tiny) and `make conformance-full`.
  - Score cases can opt out of side tables (`side_tables: false`, e.g. a weekly EUR plan entering after the FX step).
  - Expectation hooks that declare `ctx` receive a `FixtureContext` (seed, size, registry, side tables and earlier score results).
- Docs: spec format, SDK, built-in steps and plots references; guides for plugins, PyMC, tabular models and plots.
- Docs: built-in models reference, marketing-mix models guide, forecasting and sequential models guide, conformance corpus guide.
- Docs: the MkDocs Material site.
  - A landing page, getting started (install, first model, runs and executors), concepts (labelled data, pipelines and stages, representations, bundles, coord plans, capabilities), the "port a model in 15 minutes" guide, licensing, contributing and roadmap pages for v0.2–v0.4.
  - Generated reference pages: steps, models and plot-recipe catalogs, the ModelSpec/ScoreRequest schema and error codes (`scripts/gen_docs_catalog.py`, run by an MkDocs hook and `make docs-catalog`); API reference via mkdocstrings.
  - Every Python example lives in `docs/snippets/` and is executed by `conformance/tests/test_docs_snippets.py` (`make docs-test`).

### Changed
- The workspace runs numpy 2.4 (pymc-marketing 1.2 requires numpy < 2.5).
- `make typecheck` also covers `plugins/mmm`, `plugins/ts`, `plugins/tabular` and `conformance`.
- Score requests that name a pinned side table now fail validation. Score outputs along time and extend
  dims come back sorted. `validate_spec` emits an INFO issue when the model's target role overrides a
  declared role.
- Plugin-raised error text now appears in `untrusted_text`, not in `detail`.

### Fixed (integration)
- The plan's xgboost example names the real quantile plugin (`sklearn.tabular_quantiles`). A new `ml.feature_importance` recipe covers its plot list.
- `core.fit_vs_actual` and `core.residuals_over_time` compare quantile and interval summaries cell by cell instead of refusing them.
- The contract kit flags step settings whose defaults are missing from the exported JSON Schema (`default_factory`), and finds reverse-bound outputs when checking units.
- Holdout metrics no longer invert the predictions of models that own `inverse_units` a second time.
- Score outputs that declare their own `units` (e.g. state probabilities) no longer go through the target's inverse transforms.
- The contract kit runs its shuffled, subset and new-level checks on long layouts (index variables are remapped) and shifts calendar-frequency time coords by whole periods.
- The in-process executor no longer resolves a score request's side-table refs when the run payload already supplies those tables.
- The contract kit's score requests name the spec's `score_policy: supplied` side tables, so models with supplied tables (E33's FX) pass the scoring checks.
- The E33 and pymc-marketing fixtures use the corpus runner's fixture layout and run in `python -m mapi_conformance run`.

### Fixed (M0 review: spec and SDK)
- Counterfactuals: `score_bundle` takes a baseline `data` payload and a separate `scenario` payload, and
  scores the scenario's interventions against the baseline. A scenario without baseline data, or a
  named scenario that was not supplied, is `SCHEMA_INVALID`.
- `owns` is enforced: catalog steps declare `provides` (`scaling`, `carryover`), and `validate_spec`
  rejects such a step on the target, media or spend variables of a model that owns that
  preprocessing (`CAPABILITY_UNSUPPORTED`).
- Target dependence follows the model template's target roles, even with an inferred raw schema, so
  `RECURSIVE_UNSUPPORTED` fires reliably. The recursion and future-covariate checks measure how far
  the payload extends past the fit, not only `horizon`.
- No target leakage into evaluation lags for dynamic or cell-level splits. `as_of` gap periods stay
  in the holdout with masked targets and are excluded from metrics.
- Pinned side tables always replay the fit-time version; a request that names one fails with
  `SIDE_TABLE_COVERAGE`.
- Score payloads are sorted along ordered/extend dims before the pipeline; duplicate labels are
  `COORD_NOT_ALLOWED`.
- Chained look-back steps (adstock then lag) replay with fit-time history. A score window that starts
  inside the fitted range and loses look-back history gets a WARNING.
- Long-layout per-group context works end to end.
- Inverses: steps declare `additive` inverses, and decompositions along `component` only go through
  additive steps (otherwise they stay at the blocking stage, with a WARNING). Smearing residuals come
  only from target-shaped outputs. The moments `sd` uses the delta method.
- Model-owned outputs are labelled per variable with their units and `output_space`.
- Derived quantities named in `output.vars` come back from the fit cache. An uncached one is refused.
- PyMC CRN counterfactuals give identical draws in untouched cells for Poisson, negative binomial and
  binomial likelihoods (`crn_ppf`).
- Wire and bundle hardening: XrJSON budgets decoded bytes (`max_decoded_bytes`), caps declared dim
  sizes and accepts only ISO-8601 dates. Zarr-zip decoding is bounded by declared shapes, string
  lengths and an expansion ratio. Pickle is detected by opcode stream, in object `.npy` and inside
  archives. The evaluation sub-bundle is verified and never kept stale. Entry points can no longer
  replace registered or built-in plugin ids.
- Spec parsing rejects non-finite floats and reports oversized JSON integers as `SpecParseError`.

### Fixed (M0 review: models and steps)
- `core.formula`: scalar `~` is logical not; overflow and division by zero give inf/nan, and other
  arithmetic errors are reported as `SCHEMA_INVALID` rather than escaping.
- PyMC checkpoints: the codec rebuilds only classes defined in PyMC step-method modules (plus
  `pymc.util` and `mapi_pymc`), at exactly the encoded `module:qualname`.

### Fixed (M0 review: plots, conformance and tooling)
- Plot sanitizer (Plotly): `updatemenus`/`sliders` method `args` are checked in the positional `[attr, value]` form and inside nested frame/trace objects, so external image sources, `href`/`url` paths, tile-map subplots and tile-map trace types can no longer hide there.
- Plot sanitizer (Vega-Lite): `select.toggle` strings and `bind.legend` event filters are checked as expressions, `event['view']` is rejected like `event.view`, and `usermeta.embedOptions` is rejected.
- `from_plotly` strips HTML from (and scans) the strings it moves into Arrow datasets, which the JSON sanitizer never decodes.
- Plugin-originated error text stays out of trusted fields: `render_plots` and the step/model wrappers (`Pipeline`, harness fit/score/derive) move a plugin `MapiError`'s detail into `untrusted_text` and keep its code only when plugin code may raise it (`mapi_spec.errors.from_plugin_error`, `PLUGIN_ERROR_CODES`).
- Contract kit, `check_step`: `effect_matches_output` also compares each dim's kind and frequency and each variable's units; the toy FX and scale steps now set the units they declare.
- Contract kit, `check_model`: new `horizon_exceeds_fit`, `missing_future_covariate`, `recursive_unsupported`, `fast_score_matches_exact` (plugin `check_fast_score` hook, provided by `PyMCModel`), `plots_sanitize` and `outputs_match_manifest` checks; `extend_typed_errors` covers integer extend dims; `output_space_and_units` covers outputs that inherit the target implicitly.
- `OutputDecl.dims_of` (additive) lets a manifest declare settings-dependent data dims; the sklearn models (and so `tabular.regressor`) and `mmm.pymc_marketing` use it, so their static output dims match what `score` returns.

