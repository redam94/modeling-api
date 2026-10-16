# Changelog

All notable changes are documented here. The project follows [Semantic Versioning](https://semver.org/);
milestone releases are tagged on `main`.

## [Unreleased]

### Added
- **web**: the Integrations hub (`/integrations`). Guided cards for Google Sheets (the
  endpoint's Apps Script and a `score:<alias>` token), a BI embed link and a webhook; endpoints
  and aliases with the promotion dialog (the version's scorecard, the expected gate, the
  server's reasons on `409` and the audited admin override), history and rollback, the frozen
  input contract with **Try it** (validate or score the example call), snippets for Python,
  TypeScript, curl, R, Apps Script and Atlas plus an Open-in-notebook cell; webhooks with an
  event picker, the secret shown once, rotation, a test send, the delivery log (status,
  attempts, response code) with replays and the auto-disabled notice; saved plots with
  PNG/SVG/PDF downloads (Plotly disabled with the server's reason on `MODE_NOT_SUPPORTED`) and
  expiring embed links with allowed sites and revocation; a **Share** button in the plot
  gallery; `/tokens?preset=score:<alias>`.
- **web**: the trust pages. Public `/signup` (disposable-address and terms errors at their
  fields, the "check your inbox" state, the development outbox link) and `/verify-email`;
  Settings > Data for organisation admins (GDPR export and download, tenant deletion with the
  typed confirmation, grace period, cancel and per-store progress); a banner for scheduled
  deletions; in the admin console, the sign-up review queue and per-tenant identity
  verification and deletion status with a retry.
- **docs**: The Integrations hub guide (with screenshots) and the Integrations section of the
  web app guide; `make docs-screenshots-integrations`.
- **spec**: the M3 contracts for Guided mode and integrations, shared by the server, runner,
  web app and client components (contract v0.1, additive only).
  - `mapi_spec.guided`: four guided templates (`geo_mmm`, `weekly_forecast`,
    `store_demand`, `customer_choice` - the last needs a built-in choice model that is not
    shipped yet), plain-language questions, role detection from column profiles, drafts
    (ingest mapping + `ModelSpec` + YAML) and the BayesInsight `model_def.json` import.
  - `mapi_spec.ingest`: ingest presets (`mff`, `meridian`, `bayesinsight`), wide variables
    and the `mff` format; `IngestMapping` moves here (still exported by `mapi_spec.api`).
  - `mapi_spec.calibration`: business priors (ROI or share ranges with a credibility), lift
    tests, the `ModelSpec.calibration` block and the sandboxed `translate_priors` run kind.
  - `mapi_spec.scorecard`: the check catalog, items with check ids, categories and docs
    anchors, and the promotion gate (pass → allowed, warn → admin override with a reason,
    fail → blocked); `Scorecard` moves here (still exported by `mapi_spec.api`).
  - `mapi_spec.whatif` and `ResponseGridView`: per-draw response grids, the slider
    interpolation contract, "Run exact" and forecast/counterfactual presets;
    `ScoreRequest.future_covariates`.
  - `mapi_spec.endpoints`, `mapi_spec.xrjson`, `mapi_spec.snippets`: endpoints and aliases,
    score calls with inline XrJSON, input-schema compatibility, example payloads and pure
    snippet generators (Python, TypeScript, curl, R, Apps Script, Atlas).
  - `mapi_spec.webhooks`: events, envelope, `Mapi-Signature` signing and verification,
    retries and the SSRF policy; `mapi_spec.sharing`: saved plots, renders (PNG, SVG, PDF)
    and embed links for platform recipes; `mapi_spec.trust`: sign-up, e-mail and identity
    verification, tenant deletion and GDPR export.
  - `mapi_spec.api.M3_ENDPOINTS` (45 operations) with their access rules; new permissions
    (`webhook`, `embed`, `tenant_deletion`, `tenant_export`, plot delete, endpoint
    `manage` for gate overrides), scopes (`endpoints:promote`, `webhooks:manage`), audit
    actions, problem codes and `Project.guided_locked`.
- **docs**: Guided mode and Integrations concept pages; the v0.4 section of the HTTP API
  reference.
- **server**: the Guided data path. `GET /v1/guided/templates` (paged) and
  `/{template_id}`; `POST /v1/guided/detect-roles` from browser profiles or a stored dataset
  version; `POST /v1/guided/drafts` and `POST /v1/guided/import/bayesinsight`, validated
  statically against the catalog with the schema the draft's mapping promises. Every
  operation documents its RFC 7807 problems in the OpenAPI document.
  - Migration `0007_guided`: `projects.guided_locked` and `dataset_versions.columns`.
  - The persona lock is persisted, audited (`project.guided_lock`) and enforced: in a locked
    project, members below admin store Guided specs only, and only their `basic` fields may
    change (`403 FORBIDDEN` with the pointers otherwise).
  - The finalizer validates and stores an ingest's `columns.json`, which detection reads and
    `…/artifacts/columns.json` serves.
- **runner**: sandboxed ingest for Guided mode: wide variables (one column per channel or
  product), the Meridian preset, MFF tables mapped like a CSV after the pivot,
  `MffOptions.combine`, and the BayesInsight preset (`allowed_*` values, period range,
  necessary variables). Caps on rows, columns, cell and name length, and decompression (gzip
  CSV streamed under a budget, Parquet, zip and netCDF by their declared sizes) are checked
  before a parser allocates; every refusal is a plain message with a suggested action.
  Ingest writes `columns.json`, the profile of every uploaded column.
- **plugins**: `choice.multinomial_logit` in `mapi-plugin-tabular`, a conditional logit
  with household product effects and Bayesian-bootstrap draws, so the `customer_choice`
  template is real. The `e84_lite` conformance fixture replaces the corpus deferral.
- **spec**: `mapi_spec.bayesinsight.import_bayesinsight` maps `model_def.json` onto
  `mmm.hierarchical_geo` - adstock, Hill saturation and pooling with BayesInsight's own
  priors where they have equivalents, controls, seasonality, trends - and reports every
  feature without one as an `IMPORT_UNSUPPORTED` warning (`BayesInsightImport.target`
  keeps the `mmm.bayesinsight` draft). Role detection gains `ColumnProfile.monotonic` and
  `origin`, `profiles_from_dataset`, `draft_schema` and `guided_lock_issues`;
  `GuidedAnswers.format` accepts `mff`; `IngestJob` gains the ingest caps.
- **docs**: the "From a spreadsheet to a Guided draft" guide; the Guided mode
  concept page, the built-in models and the conformance guide describe the served parts.

- **mmm**: `mmm.hierarchical_geo` declares `business_priors` and `lift_tests` calibration.
  - Business priors (ROI or contribution-share ranges at a credibility, a baseline share)
    become per-channel `LogNormal` `beta_channel` priors (and an `intercept` prior) through a
    vectorised location/scale bisection over Monte Carlo draws of the other priors, with a
    prior-predictive preview in business units and the prior mass inside each stated range
    (`mapi_plugin_mmm.calibration`; property tests check the round trip).
  - Lift tests add `kpi_delta ~ Normal(implied lift, se)` over the test window; the extra
    spend is carried over through the lag tensor and the implied lift is kept as
    `lift_tests__model`.
  - `response_curves` carries the baseline (intercepts and controls), closing the expected KPI.
  - `mmm.pymc_marketing` takes lift tests through pymc-marketing's `add_lift_test_measurements`.
- **runner**: `translate_priors` runs (`priors.json` and the `mmm.prior_beliefs` /
  `core.prior_predictive` plots); every fit writes `scorecard.json` (the whole check catalog,
  or the guided template's, each measured or skipped with a reason) and the what-if grid
  view `ui/grids/response_curves.json`; forecast and counterfactual score runs write
  `ui/guided/forecast.json` and `ui/guided/counterfactual.json`, and a forecast's fan chart
  shows the fitted history with the split line. Lift tests are checked against the raw data
  before a fit.
- **server**: `POST /v1/guided/prior-translations` and `GET …/{run_id}` (static checks of the
  beliefs and lift tests against the dataset summary, a pinned `translate_priors` run, its
  verified `priors.json`). The finalizer stores the runner's scorecard on the model version
  and validates `priors.json`; spec validation checks a spec's lift tests against the dataset.
- **sdk**: `FitContext.calibration` and `FitContext.to_original` (the fitted scales);
  `prepare_model_stage` for runs that build but do not fit a model; the optional
  `translate_priors` plugin hook. **sdk-pymc**: `PyMCModel.prior_overlap_vars` keeps cheap
  prior draws for the scorecard's new `plausibility.prior_overlap` check.
- **sdk/runner/server**: forecasts extend the covariates their payload lacks
  (`ScoreRequest.future_covariates`: `carry_last`, `seasonal_naive`, `zero`) from the fitted
  history (`mapi_sdk.future`); the server passes the fit dataset as the score run's `history`
  input. `mapi_server.services.scorecards.promotion_decision` derives the promotion gate from
  a stored model version.
- **plots**: `mmm.prior_beliefs` (each channel's prior ROI or share against the stated range).
- **docs**: Guided modelling (the translation math, the lift-test likelihood, how each
  scorecard check is measured, the what-if, forecast and counterfactual views).
- **server**: endpoints and aliases (`/v1/endpoints`, migration `0008_endpoints_webhooks`).
  - `PUT /v1/endpoints/{alias}` promotes a model version through the promotion gate: the
    sanity scorecard, the endpoint's evaluation policy (`evaluate_then_refit` by default, so
    endpoints serve versions refitted on all data after their holdout evaluation) and
    input-schema breaks. A `warn` needs a signed-in project admin's override with a reason
    (audited `endpoint.promote_override`; no API token can override); a `fail` blocks.
    `expected_revision` guards concurrent promotions.
  - History, rollback (audited with its reason) and delete; `GET …/schema` with an example
    payload, `POST …/validate`, and `GET …/snippets` rendered with the server's base URL.
  - `POST /v1/endpoints/{alias}/score`: inline XrJSON payloads are checked against the
    endpoint's input contract and the CoordPlan rules, stored as run inputs and decoded only
    in the sandbox; `Prefer: wait`, `Idempotency-Key` (repeat the call to collect a result),
    `score:<alias>` tokens, and the per-token rate limit and vCPU budget. Bodies are capped
    (`413`) and depth-checked before parsing.
- **server**: webhooks (`/v1/webhooks`).
  - CRUD with the signing secret shown once and stored sealed (AES-GCM under a key derived
    from the server secret); rotation keeps the old secret signing for 24 hours.
  - A transactional outbox: `run.succeeded`, `run.failed`, `model.version.ready`,
    `score.completed` and `endpoint.promoted` are written in the transaction of the change;
    `emit_tenant_event` serves `tenant.deletion.completed`.
  - Deliveries from the worker on a `webhooks` procrastinate queue: signed
    (`Mapi-Signature`), retried with exponential backoff and jitter, auto-disabled after
    `MAPI_WEBHOOK_AUTO_DISABLE_AFTER` consecutive failed events or a `410` (audited), with a
    delivery log and replays.
  - The SSRF guard: static URL checks, one resolution with every address checked, a pinned
    connection with the original name as SNI and `Host`, no redirects, time and size caps;
    `MAPI_WEBHOOK_ALLOW_HTTP` and `MAPI_WEBHOOK_DEV_ALLOWLIST` for development only.
  - Metrics for promotions, endpoint calls, events, delivery tries, SSRF refusals and
    disabled webhooks; every new operation documents its RFC 7807 problems in OpenAPI.
- **docs**: the Endpoints and webhooks guide and the webhook settings.
- **server**: saved plots, static rendering and embed links (plan §10.3 items 4-5).
  - `POST/GET/DELETE /v1/plots[/{plot_id}]`: a model version's plot (after its signed
    manifest verifies) or a user-authored PlotSpec, sanitized on save and on every read.
    User-authored plots are recorded with `author` provenance whatever they claim.
  - `POST /v1/plots:render` (`Prefer: wait`, `Idempotency-Key`) and
    `GET /v1/plots/renders/{run_id}`: a `render` run of one sanitized plot. The API writes
    its `plots.json` input and pins each bundle dataset as a `plotdata.<n>` input. Plotly is
    refused with `MODE_NOT_SUPPORTED` unless `MAPI_RENDER_PLOTLY_ENABLED`.
  - `POST/GET/DELETE /v1/plots/{plot_id}/embeds[/{embed_id}]`: tokenised links for platform
    recipes only (`EMBED_NOT_ALLOWED` otherwise), stored as keyed hashes, with expiry,
    revocation, view counts and audit events. A PNG fallback is rendered when the caller may
    start render runs, and copied write-once into the bundles bucket on first use.
  - The public `GET /v1/embeds/{token}`: no credential is read (cookies and `Authorization`
    are dropped), CORS for the plot-sandbox origin only, rate limits per link and per
    address, every dataset inlined from the verified bundle without decoding it.
  - Migration `0009_plots_embeds`: `saved_plots` and `plot_embeds` with forced row-level
    security; the gateway role may read only the embed lookup columns.
  - Every new operation documents its RFC 7807 problems in the OpenAPI document.
- **runner**: `render` runs draw PNG, SVG and PDF with vl-convert (pinned in the new
  `mapi-plots[render]` extra) with network access off, refuse anything that could fetch a
  resource, read `plotdata.<n>` inputs, apply `width`/`height` and cap images at 32 MiB.
  Plotly needs kaleido and a system Chrome (`MODE_NOT_SUPPORTED` otherwise; no browser is
  ever downloaded).
- **plot-sandbox**: the embed page `/embed/<token>`. It fetches the embed view with no
  credentials under its own CSP (`connect-src` adds only the API's `/v1/embeds/` path),
  validates and decodes it, honours the link's allowed origins and offers the PNG fallback.
  The `mapi/web` image serves it with the API origin filled in at start-up.
- **docs**: the "Sharing plots" guide and the new settings in the configuration reference.
- **spec**: the sign-up review queue (`SignupReview`, `SignupReviewDecision`, the
  `SYBIL_FLAGS` catalog), `GET /v1/admin/signups`, `POST /v1/admin/signups/{signup_id}/review`
  and `POST /v1/admin/tenants/{tenant_id}/deletion/retry`; the `auth.signup_flagged`,
  `auth.signup_review` and `tenant.deletion_retry` audit actions; the deletion record and
  platform logs in `RETAINED_RECORDS`.
- **server**: the trust lifecycle (migration `0010_trust_lifecycle`).
  - Self-service sign-up (`POST /auth/signup`, `POST /auth/verify-email`): single-use e-mail
    tokens stored as HMACs (24 h), the same answer for new and known addresses, a mailer
    protocol (SMTP with required TLS, a file outbox read at `GET /dev/outbox` in the `dev`
    profile, console), the disposable-domain blocklist plus configured domains and files,
    per-address, per-domain and platform-wide sign-up limits, and Sybil heuristics (same
    address, browser cookie, e-mail, domain burst) that flag sign-ups for the operators'
    review queue. Verification creates a `public` tenant, its `default` project and the admin
    grant.
  - Identity verification (`GET /v1/tenants/{id}/verification`, operators'
    `PUT /v1/admin/tenants/{id}/verification`, an `IdentityVerifier` provider protocol with a
    fake) and cut line A at run admission: uploaded code needs a trusted tier or identity
    verification (`IDENTITY_VERIFICATION_REQUIRED`); public tenants run built-ins and
    declarative specs only.
  - Tenant deletion: a confirmed request suspends the tenant and cancels its runs; after the
    grace period the orchestrator makes it `deleting` (tokens revoked, sessions ended,
    revoker hooks) and a procrastinate job purges the queue, every tenant table, every
    `t/{tenant}/` object prefix (resuming after retention locks), then notifies and writes
    the `tenant.purged` tombstone; progress per store; automatic and operator retries.
  - GDPR export: a zip of every tenant record (credentials redacted), dataset objects and
    bundle manifests, with a short-lived signed link.
- **infra**: a Cloud Armor policy on every load-balancer backend (OWASP CRS sqli, xss, lfi,
  rce and scanner rules in preview-then-enforce, per-IP rate bans on sign-up, `/auth/*`,
  embeds and endpoint scoring, Adaptive Protection), and an explicit retention for the control
  project's `_Default` log bucket (tenant deletion does not purge logs).
- **docs**: Sign-up and abuse controls, Deleting and exporting data, and a Cloud Armor
  section in Security hardening.
- **integration**: the M3 parts work together.
  - Tenant deletion revokes the tenant's embed links and disables its webhooks when it starts,
    purges every M3 table (endpoints and their history, webhooks, events and deliveries,
    saved plots, embeds, column profiles) and then delivers a signed
    `tenant.deletion.completed` to the webhooks that subscribed to it, through the same
    SSRF-guarded sender as every delivery (`mapi_server.services.tenant_hooks`).
  - Webhook events come from the finalizer's terminal transitions (`run.*`,
    `model.version.ready` with the runner's scorecard), promotions and rollbacks
    (`endpoint.promoted`), score runs (`score.completed`) and deletions.
  - Endpoint promotion gates on the scorecard the runner measured at fit
    (`services.scorecards.promotion_decision`, the single gate derivation).
  - Sign-up workspaces are `public` tier: built-ins and declarative specs only, on executors
    with enforced egress and DNS (cut line A).
- **server**: `POST /v1/guided/requests:compile` turns a what-if scenario, a forecast or an
  intervention preset into its `ScoreRequest`, checked against the model version's score
  metadata ("Run exact").
- **runner**: score runs read an endpoint call's inline XrJSON payloads (`inline.data`,
  `inline.scenario`) with the wire-format caps and the model's dim types.
- **mmm**: `mmm.bayesinsight`, a BayesInsight `model_def.json` as a built-in model (Q8):
  Normal, log-normal, Poisson, negative binomial and zero-inflated likelihoods; coefficient
  and intercept priors in the KPI's own units; Hill, linear and S-shaped curves before
  delayed, geometric, Weibull or no carry-over; enforced sign constraints; fixed and
  partially pooled effects per row id (new levels pooled); Fourier seasons and local spline
  trends. Contributions sum to the expected KPI (a log-mean Divisia split for log links);
  lift tests observe the incremental KPI under the model's own likelihood. It passes the
  contract kit; `mapi_plugin_mmm.bayesinsight_examples` simulates a project with known
  parameters.
- **spec**: `mapi_spec.bayesinsight_model` types `model_def.json` as the plugin runs it (the
  `mmm.bayesinsight` settings); `import_bayesinsight(target="mmm.bayesinsight")` now drafts a
  runnable spec - every row id a panel dim, the KPI bound to `y`, lagged media - falling back
  to BayesInsight's defaults with `IMPORT_UNSUPPORTED` warnings (and `info` issues for
  faithful but surprising behaviour).
- **mmm**: business-prior translation for `mmm.pymc_marketing` (1.1.0): ROI and share ranges
  become per-channel log-normal `saturation_beta` priors and a baseline share an `intercept`
  prior (narrowing the control and seasonality priors when needed), solved on prior draws of
  pymc-marketing's own model; `settings.priors` hands them to pymc-marketing's
  `model_config`; a `translate_priors` hook previews them and a fit without a translation
  run translates them itself.
- **sdk**: categorical holdout metrics - `accuracy`, `accuracy_naive`, `accuracy_lift`,
  `log_score` and `brier` - for a text target scored as one probability per category.
- **spec/runner**: the `holdout.hit_rate` scorecard check (the holdout hit rate against
  always guessing the most popular product, in plain words) for the `customer_choice`
  template.
- **conformance**: the `guided_mmm` fixture (the `geo_mmm` Guided draft with business
  priors and a lift test, true to its simulation); `e84_lite` evaluates the categorical
  metrics; the dedicated BayesInsight import ingests, validates and fits end to end.
- **docs**: the built-in models reference covers `mmm.bayesinsight` and every plugin's
  calibration; Guided modelling covers business priors for the pymc-marketing adapter and
  the BayesInsight lift likelihood.
- **dev**: `make outbox` (`dev.py outbox`) prints the dev mailer's newest e-mails and
  verification links; `make dev` sets `MAPI_OUTBOX_DIR` and the plot sandbox's `VITE_API_URL`;
  compose keeps the outbox on a volume. `make api-types` and `make snapshots` regenerate the
  web API types and the snapshots.
- **web**: Guided mode, the task flow for non-technical analysts (plan §11 Guided 1-7),
  replacing the placeholder page.
  - "What do you want to learn?": the templates as cards, and the analyses in progress. A
    session is a stored spec (`metadata.labels.guided`); every step has its own URL
    (`/guided/<spec>/<step>`) and is rebuilt from server state, so it can be resumed or shared.
  - Your data: the upload (CSV, Parquet, MFF; Google Meridian tables recognised) is profiled in
    the browser and only the column profiles go to role detection; questions with suggested
    answers, confidence and reasons; a live preview with a sparkline per chosen series; plain
    validation messages; then the dataset, the signed upload, the sandboxed ingest with the
    draft's mapping and the spec.
  - What you believe: ROI or contribution-share ranges per channel with a confidence, the
    baseline share and lift tests, a `translate_priors` preview (prior ROI/share
    distributions and a KPI fan in the plot sandbox) and the translated priors applied when
    they match the beliefs saved.
  - Fit the model: plain-language phases, progress, time left and cancel, live over SSE.
  - Results: headline numbers, ROI and share per channel with 90% ranges, the fit and
    response-curve plots, and the sanity scorecard grouped by question with what to do.
  - What if…: per-channel spend sliders answered instantly from the per-draw response grid
    (a TypeScript port of `mapi_spec.whatif.evaluate_scenario`, checked against golden
    cases the spec tests export), "Run exact" next to the instant answer, scenario presets
    (channel off, budget × k) and a forecast fan chart with its split line.
  - Share: promotion to an endpoint with the gate explained (a warning needs a project
    admin's reason), an embed link for a platform chart, and the way to the Integrations hub.
  - "View as YAML" on every step (read-only Monaco) and "Edit in Expert" with
    "Back to Guided mode"; settings Expert mode changed are kept and marked "advanced
    settings applied". The Guided/Expert choice is remembered per user, and in a project
    locked to Guided mode members below admin see Guided screens only.
  - Mock-mode handlers for every Guided call (`src/mocks/guided.ts`), Vitest unit and route
    tests and the Playwright smoke spec `e2e/guided.spec.ts` (the whole flow, the lock, the
    persona and a 375 px phone).
- **spec**: `tests/snapshots/whatif_golden.json`, golden what-if cases shared with the web
  app (`make snapshots` regenerates it).
- **docs**: "Your first analysis in Guided mode", a step-by-step tutorial with screenshots
  (`make docs-screenshots-guided` recaptures them from mock mode).
- **client**: the v0.4 Python client. Every operation of `ENDPOINTS` has a request builder
  (the coverage test now checks the whole v0.4 table), sync and async.
  - `api.guided` (templates, role detection, drafts, the BayesInsight import, "Run exact"
    compilation, `preview_priors`), `api.endpoints` (promote, history, rollback, schema,
    validate, keyed `Prefer: wait` scoring, snippets), `api.webhooks` (secrets returned once;
    deliveries, replay, rotation), saved plots, renders (waited for, CRC32C-checked download)
    and embeds on `api.plots`, `api.signup`/`api.verify_email` (sent without credentials),
    verification, deletion and the GDPR export (size-checked atomic download) on
    `api.tenants`, and the sign-up review queue on `api.admin`.
  - `Client().endpoint(alias)` (`RemoteEndpoint`): `schema()`, `validate(ds)` and
    `score(ds) -> xr.Dataset` with inline XrJSON (`mapi_client.xrjson`, the SDK wire format);
    the generated Python snippet runs as-is.
  - `mapi_client.webhooks.verify(headers, body, secret)` for receivers.
  - `mapi_client.atlas.RemoteModel`: Atlas's model interface, structurally (Atlas is never
    imported at import time): KPI at the mean or as draws, batching along an independent dim
    into one score request, an LRU cache, backoff on `RATE_LIMITED`; `register_with_atlas()`;
    the `atlas` extra; `make test-atlas` runs the tests with Atlas installed in a throwaway
    overlay.
- **cli**: `mapi endpoints ls|get|history|promote|rollback|schema|score|snippets`,
  `mapi webhooks ls|get|create|rotate|deliveries|replay|rm`,
  `mapi plots render|embed|embeds|revoke`, `mapi guided templates|detect|draft` (CSV files are
  profiled locally), `mapi priors preview`, `mapi tenant status|export|delete`, `mapi signup`
  and `mapi verify-email`. Promotions, rollbacks, rotations, deletions and revocations ask
  first; webhook secrets and embed links are printed once, alone on stdout.
- **docs**: the v0.4 sections of the Python client and command-line guides, "Optimise a budget
  with Atlas" and "Receiving webhooks" (FastAPI and Flask), with offline, tested snippets.

- **integration (web)**: Guided mode and the Integrations hub work as one.
  - The Share step links into the hub: the promoted endpoint's snippets, a `score:<alias>`
    token, the chart link under Plots and links, and the hub itself (typed routes). A project
    locked to Guided mode keeps Integrations in the navigation.
  - Both use the same endpoint and saved-plot queries and the same promotion-gate preview (the
    port of `promotion_gate`), so a promotion or link made in one shows in the other at once;
    in mock mode they share one endpoint, history, plot and embed state.
  - Every snippet the app shows and copies is the server's (`mapi_spec.snippets`), the notebook
    cell included; the mock's fixture endpoint, example call and snippets are generated from
    `mapi_spec` (`apps/web/scripts/gen_integration_fixtures.py`, checked by `make web-check`
    and CI, rewritten by `make snapshots`), and a test checks each tab against them.
- **spec**: a `notebook` snippet language: a Jupyter or marimo cell that scores the endpoint
  with `Client().endpoint(alias).score(...)` and shows an `xarray.Dataset`; the client's tests
  run it as generated.

- **e2e**: the M3 exit criteria through the running stack. `tests/e2e/test_m3_guided_stack.py`
  (in `make e2e`) follows one analyst from sign-up and e-mail verification through the
  `geo_mmm` Guided flow (CSV and MFF), business priors and a lift test, the scorecard and its
  gate, what-if vs "Run exact", forecast and channel-off, an endpoint scored with a
  `score:<alias>` token, signed webhooks to a local HTTPS receiver with a replay, an embed
  link and its revocation, Atlas optimising a budget through `RemoteModel` against the live
  endpoint, the GDPR export and a tenant purge checked in Postgres and the GCS shim.
  `apps/web/e2e/m3-guided.e2e.spec.ts` is the timed "unaided tester" script (upload → report
  → scenario) and the Integrations hub flow. `make e2e` overlays Atlas from `ATLAS_DIR` when
  it is there.
- **dev**: the `dev` profile trusts a local webhook receiver: a throwaway CA and certificate
  for `127.0.0.1`/`localhost` in `$MAPI_DATA_DIR/dev/webhook-tls/`, with
  `MAPI_WEBHOOK_CA_FILE` and `MAPI_WEBHOOK_DEV_ALLOWLIST` set unless given.
- **gcs-shim**: `--bundle-retention-days` (default `$MAPI_BUNDLE_RETENTION_DAYS`) chooses the
  bundles bucket's locked retention when a root is created (`0`: none), so a test stack's
  tenant deletions can finish.
- **web**: "Fit again, more carefully" on the Guided report when a convergence check warns or
  fails (twice the tuning and draws and a higher `target_accept`, up to a ceiling); a blocked
  Share step links to it.
- **spec**: `mapi_spec.guided.freq_plain`: role detection explains a detected date frequency
  in words ("weekly, on Sundays").
- **docs**: M3 verification: what was verified of the M3 exit criteria, the timings, the
  defects found, and the protocol and observation sheet for the five non-technical testers.

### Changed
- **spec**: the `geo_mmm` template (1.1.0) fits with 1000 tuning steps and draws and
  `target_accept` 0.95: the 500-draw fit failed the scorecard's convergence checks on two
  years of weekly data, which Guided users cannot act on, so the analysis could never go live.
- **web**: in What you believe, channels start without a belief (unticked); ticking one starts
  from the sensible range. Untouched channels no longer become ROI beliefs.
- **server**: `GET /v1/embeds/{token}` answers `Cache-Control: private, no-cache` (was
  `public, max-age=60`), so a revoked link stops rendering on the next load.
- **spec**: `mapi_spec.api.ENDPOINTS` is the v0.4 surface (M1 + M2 + M3, every row served);
  `V03_ENDPOINTS` keeps the v0.3 table and `ALL_ENDPOINTS` is an alias of `ENDPOINTS`.
- **server**: the M3 migrations run in one chain after `0006_executor_disable`:
  `0007_guided`, `0008_endpoints_webhooks`, `0009_plots_embeds`, `0010_trust_lifecycle`.
- **spec**: the `customer_choice` template (1.1.0) targets the built-in
  `choice.multinomial_logit`; wide column labels drop the question's keywords
  (`price_cola` → `cola`); a draft refuses answers that would give two columns the same
  dataset name.
- **spec**: the `customer_choice` template (1.2.0) evaluates the categorical metrics and
  runs `holdout.hit_rate`; `BayesInsightImport(target="mmm.bayesinsight")` drafts the
  shipped plugin's settings (`variables` maps analytic names to variables; the KPI is `y`)
  instead of keeping `model_def` verbatim.
- **server**: `POST /v1/guided/import/bayesinsight` validates `mmm.bayesinsight` drafts
  against the catalog too.
- **conformance**: the corpus runner compares fit-time and evaluate-run metrics only when it
  rebuilt the holdout (a `last_per_group` split is scored in sample).

### Fixed
- **server**: "Run exact" failed on every live stack: `POST /v1/guided/requests:compile` left
  what-ifs and presets over the fit window without data. The compiled request now names the
  fit dataset version, and a score request without data, scenario or horizon is refused with
  `422 SCHEMA_INVALID` before a run starts.
- **web**: the Guided report showed MAPE in percent as a fraction ("83 %" for 0.83 %); it
  reads the scorecard's measured value. The beliefs preview no longer prints impossible
  baseline shares ("between -164 % and 271 %"); Share a chart offers result charts before
  the sampler's diagnostics.
- **plots**: prediction plots name the predicted quantity as the dataset does (`revenue`, not
  `y_hat`).
- **client**: `datasets.upload` accepts an `mff` mapping for a `.csv` MFF table.
- **server**: an unresolvable webhook host is explained in plain words (no `gaierror`).
- **web**: the New token dialog's budget period showed "pe" instead of "per day"/"per month":
  native selects no longer shrink below their width in a flex row (filters, the cap window,
  the intervention editor and the upload mapper had the same risk).
- **spec**: `index` dims extend contiguously (their coordinates have ordinals), so scoring an
  `index` dim with an `extend` policy no longer fails with `NON_CONTIGUOUS_EXTENSION`.
- **sdk**: cell-level splits treat an empty text target as missing and hold text targets out
  with `''`, so a choice panel's chosen product can be held out.
- **runner**: the `holdout.mape` scorecard check read `mapi_sdk`'s MAPE, which is in percent,
  against thresholds in fractions, so a 1.4% holdout error showed as 140% and blocked the
  promotion of almost every model; percent metrics are now divided by 100 first.
- **runner**: inline endpoint payloads named like the endpoint's input contract (canonical
  names such as `time`) failed to score when the dataset's own name differed (`week`); the
  score run renames them to the dataset's names before typing coordinates. The snapshot's
  documentation says which names a payload uses.
- **spec**: an endpoint's example call (`GET …/schema`, the snippets, `mapi endpoints score
  --example`) asked quantile models for a `mean` they cannot serve; it now asks for the
  representation's default summary.

### Fixed (M3 review: integrations and trust)
- **server**: every per-address control saw the proxy's address, not the caller's, so the
  sign-up, verification and embed-view limits and the Sybil `same_ip` check applied to the
  whole platform behind a load balancer. New `MAPI_TRUSTED_PROXIES` (addresses or CIDR
  ranges): a request from a trusted proxy takes the right-most untrusted `X-Forwarded-For`
  hop as the client; any other peer's header is ignored. The multi-tenant profile refuses to
  start without it (`none` when clients connect directly); Terraform sets Cloud Run's and the
  load balancer's hops and `make dev` sets `none` for its multi-tenant stack.
- **server**: a promoter, an `endpoints:promote` token included, could switch off the
  schema-change and train-only gates by sending a laxer `evaluation_policy` with the
  promotion. Relaxing the policy (`schema_change` toward `allow`, `fit_mode` to `train_only`)
  now needs a signed-in endpoint admin, like an override (`403 FORBIDDEN` otherwise, audited
  as a denial), and the promotion's audit event records the policy before and after
  (`mapi_spec.endpoints.policy_relaxations`).
- **server**: the per-domain (25 a day) and platform (500 an hour) sign-up caps counted
  unverified sign-ups, so made-up addresses could lock a whole e-mail domain, or everyone,
  out of sign-up. They now count verified sign-ups only (the per-address limit still counts
  every request), and large free-mail domains (`MAPI_SIGNUP_SHARED_EMAIL_DOMAINS`, default
  `mapi_spec.trust.SHARED_EMAIL_DOMAINS`) have no per-domain cap and never trip the
  `domain_burst` Sybil flag.
- **server**: the plan's draw cap (Q2: 300, 500 on paid tiers) was enforced only by the
  Python client, so any caller could ask a model or endpoint for 10,000 draws. Score
  requests asking for more than the tenant tier's `MAX_SCORE_DRAWS` (`public` 300, other
  tiers 500) are refused with `QUOTA_EXCEEDED` at `/output/draws`, and the score job carries
  the cap (`ScoreJob.max_draws`: score runs now store a `{request, max_draws}` payload) so the
  runner enforces it too.
- **server**: a `score:<alias>` token could score any dataset of the project by
  `dataset://` reference (and probe which exist through `…/validate`) without dataset read
  access. Callers without it now send inline data or name the endpoint's own fit dataset;
  any other reference, existing or not, gets the same `403 FORBIDDEN`.

## [0.3.0] — M2: cloud & multi-tenant core — 2026-10-15

The platform is now multi-tenant and ready for Google Cloud. Many teams can share one
deployment: each tenant has its own projects, members, API tokens, quotas, cost and audit
trail, and PostgreSQL row-level security keeps their data apart. Runs can execute as Cloud Run
Jobs, and Terraform describes every Google Cloud project. Nothing has been applied to real
Google Cloud yet: cloud code is tested against fakes, and the Terraform is validated and tested
against mocked providers. In summary:

- **tenancy and auth**: tenants with trust tiers and data residency, projects with ordered
  roles (viewer < editor < runner < admin), browser sign-in through a backend-for-frontend
  OIDC flow (with a dev identity provider for local work), `mapi login` for the CLI, and
  checksummed, scoped, project-bound API tokens.
- **isolation**: forced row-level security on every tenant table. The API runs as roles
  without the bypass (`mapi_app` for tenant data, `mapi_gateway` for sign-in and operator
  lookups); only the orchestrator holds the platform role. The audit log and the cost ledger
  are append-only.
- **quotas and cost**: token, tenant and global quotas with weighted fair-share leasing and
  429 back-off; an integer-micros cost ledger with per-run cost, usage reports and cost by
  tenant for operators; a global spend cap.
- **operations**: kill switches (pause or cancel uploaded code, the spend cap), executors that
  operators can disable, a shared-tier gate that needs recorded conformance and security-suite
  evidence, audit events for every change, Prometheus metrics, dashboards, alerts and
  runbooks, plus a security probe suite and chaos tests.
- **cloud**: a Cloud Run Jobs executor (orphan adoption, fan-out, 429 handling), Pub/Sub push
  events, a Cloud KMS bundle signer, Terraform for the control, data and exec projects, and
  deploy and rollback scripts.
- **web, CLI and client**: tenant switcher, members, tokens, usage and an operator console
  (tenants, quotas, cost, kill switches, executors, audit); `mapi admin`, `mapi tokens` and
  `mapi usage`; client methods for every new endpoint.
- **docs**: Administration and Authentication guides with tested walkthroughs, a security
  model, deployment to Google Cloud with the [verify M0] checklist, and reference tables
  generated from the code.

Every workspace package and both web apps are versioned 0.3.0. Contract v0.1 gains only
additive changes. Upgrading from 0.2 runs migrations 0003 to 0006, which adopt existing rows
into a platform tenant.

### Added
- **spec**: the M2 contracts. They cover tenants, trust tiers and placement, ordered project
  roles (viewer < editor < runner < admin) with one permission matrix, and `AuthContext` as
  the single authorization decision. They also add checksummed API tokens stored as HMACs,
  scopes, BFF OIDC helpers (PKCE, cookies, safe `return_to`) and Pub/Sub push claims;
  token/tenant/global quotas with fair-share leasing and 429 back-off; integer-micros cost
  pricing and the ledger (`RunCost` moves to `mapi_spec.cost`); audit events; kill switches
  and executor health; and the 12-case executor conformance suite as data. `mapi_spec.api`
  gains the 40 M2 endpoints and `ROUTE_ACCESS`, the scope and permission of every operation
  (all 44 M1 ones included). Contract v0.1 gains only additive changes.
- **server**: authentication on every route.
  - Bearer API tokens, browser sessions through an OIDC backend-for-frontend (authorization
    code + PKCE, HttpOnly SameSite=Strict cookie, CSRF token), and ID tokens of the `mapi`
    CLI (`MAPI_OIDC_CLI_CLIENT_IDS`).
  - A credential-less local administrator in the single-tenant profile, and a `dev` profile
    with a built-in identity provider and seeded accounts.
  - Tenants, projects, members and invitations.
  - Row-level security: `FORCE ROW LEVEL SECURITY` on every tenant table, `set_config(…, true)`
    per transaction, failing closed, with separate app and platform roles.
  - Quotas and rate limits at admission and at dispatch.
  - A cost ledger written whenever an attempt ends, for every executor.
  - The audit log, the operator endpoints (tenants, quota policies, kill switches, executor
    health, audit) and usage reports.
- **server**: the `cloudrun-jobs` executor for a fixed inventory of 3 bases × S/M/L.
  - Env-only overrides, orphan adoption by nonce, and a jobs.run token bucket with a shared
    429 cool-down.
  - Exit-code classification (OOM, timeout, partial) and `taskCount` fan-out plumbing.
  - Tested against a deterministic `FakeCloudRun`.
  - Pub/Sub push authentication: a Google-signed OIDC token, a JWKS cache, a subscription
    allow-list and decoding of the log entry.
  - A Cloud KMS manifest signer, alongside the local one.
  - `MAPI_OBJECT_STORE=gcs`: Cloud Storage with keyless IAM `signBlob` URLs and integrity
    checks on every write.
- **server**: observability.
  - structlog JSON logs carrying the trace and span ids.
  - OpenTelemetry spans, with an optional OTLP exporter.
  - The Prometheus metric set at `GET /metrics`, behind a bearer (`MAPI_METRICS_TOKEN`, which
    the multi-tenant profile requires), plus the worker's own endpoint
    (`MAPI_WORKER_METRICS_PORT`).
  - Request ids on every response.
- **tests**: the executor conformance suite, run against fake Cloud Run and local-process. It
  also checks that every attempt that ran is in the cost ledger.
  - A 100-fit load test on the fake backend (`make load-test`).
  - Chaos tests (`make chaos`).
  - A route-table test that every non-public route needs credentials and that no M1 or M2
    route serves another tenant's project.
  - End-to-end M2 suites against a running dev stack, run by `make e2e`. Two tenants are
    created through the dev IdP. The suites cover sign-in, cross-tenant refusal at the API
    and by row-level security in Postgres, token scopes, `QUOTA_EXCEEDED`, the spend-cap kill
    switch held at dispatch, per-run cost and the audit trail
    (`tests/e2e/test_m2_tenancy_stack.py`). They also include a browser flow: sign-in, tenant
    switch, fit with metered cost, tokens, and the admin kill switch
    (`apps/web/e2e/m2-flow.e2e.spec.ts`).
  - An opt-in burst of concurrent real fits, against the dev stack or a deployment
    (`MAPI_E2E_BURST_RUNS`).
  - A nightly workflow: chaos, executor conformance, both load-test modes, and the security
    suite on local-process and inside gVisor.
- **infra**: Terraform modules control, exec, data, build, perimeter and stack, plus the dev,
  staging and prod environments.
  - The exec projects get zero egress, a restricted VIP, a DNS response policy and VPC-SC.
  - Cloud SQL with three login roles, generated secrets written only through write-only
    arguments, a Pub/Sub push to an internal-only events service, and KMS.
  - Deploy scripts (`deploy/gcp`) and a manual Deploy workflow.
  - `make tf-validate` runs fmt, validate and the mocked-provider module tests; nothing is
    ever applied.
- **ops** and **security**: Grafana and Cloud Monitoring dashboards, alert rules and runbooks
  (`ops/`). A sandbox-escape probe suite with an expected-outcome matrix per isolation level,
  and a deliberately malicious plugin that is never installed (`security/`, run with
  `make security-suite`).
- **web**: sign-in, a tenant and project switcher, and pages for API tokens, members, and usage
  and cost. Per-run cost, and plain-language quota and permission errors with a Retry-After
  countdown. An operator console for tenants, quota policies, kill switches, executor health
  and the audit log.
- **client** and **cli**:
  - `mapi login` (browser or device code, refreshed and stored with mode 0600, or
    `--with-token`), `logout`, `whoami` and `use`, plus `X-Mapi-Project` selection.
  - Token, usage and admin commands and resources.
  - 429 handling with `RateLimited`/`QuotaExceeded` and Retry-After.
  - `--discovery-url` for providers whose discovery document lives elsewhere (the dev IdP).
  - `mapi_client.oidc.dev_idp_login` for scripts.
- **docs**: tenancy and auth, and quotas and cost concepts; the v0.3 HTTP API; the
  authentication, Cloud Run, Google Cloud deployment, security hardening, observability and
  security-suite operations pages; and every new variable in the configuration reference.
  - The M2 verification record (`docs/operations/m2-verification.md`): what was proven
    locally, the measured numbers, and the exact steps left for a real Google Cloud project.
- **server**: `GET /v1/admin/usage` (operators) returns cost by tenant for a UTC day or
  month, summed from the cost ledger with the biggest spender first. It also returns the
  current spend against the global spend cap (`AdminUsageReport`, `TenantCost` and
  `GlobalSpend` in `mapi_spec.api`). The Python client exposes it as `admin.usage()`.
- **web**: the admin console has a **Cost** tab with cost by tenant per period and each
  tenant's share of spend. The global spend-cap card shows current spend against the cap on a
  meter that warns at 70% and 90%.
- **docs**: the v0.3 site.
  - New **Administration** and **Authentication** guides, each driven by a walkthrough that
    `make e2e` runs against a dev stack (`docs/snippets/admin_tour.sh`, `auth_tour.sh`).
  - Generated from the code at every build (`scripts/gen_docs_platform.py`): the roles,
    token-scope and trust-tier matrices (`mapi_spec.tenancy.PERMISSIONS`), the metric set,
    the alerts, dashboards and runbooks of `ops/`, and an index of every `MAPI_*` variable.
    Pages embed generated tables with `<!-- generated: NAME -->`.
  - The landing page tells the multi-tenant story, with screenshots of the new screens
    (`make docs-screenshots-admin`); the security model shows what each executor enforces
    and the threat model in plain terms; Deploying to Google Cloud starts with the
    [verify M0] checklist and ends at the first tenant; the roadmap moves to M3.

### Changed
- **devops**: `make dev` and compose run the servers in the `dev` profile by default
  (`MAPI_STACK_PROFILE`): sign in as `admin@dev.local` (tenant admin and operator) or one of the
  other seeded accounts. `make seed`, `make e2e` and `mapi_conformance run --stack` sign in by
  themselves. `MAPI_STACK_PROFILE=single-tenant` restores the credential-less API.
- **server**: every v0.2 route is project-scoped. Callers name the project with
  `X-Mapi-Project: <tenant>/<project>`, a token's binding, or the single-tenant default.
- **web**: mutations send the session's CSRF token (`X-CSRF-Token`). `openapi.json` and the
  TypeScript types are generated from the server alone.
- **client**: QUOTA_EXCEEDED is no longer retried by default (`RetryPolicy.max_quota_wait`).
  RATE_LIMITED has its own retry budget and is retried for every method.

### Fixed
- **server**: the start-up check that every route has an access rule now covers routes of
  included routers (FastAPI 0.143 keeps them as branches).
- **server**: two finalizations of the same model at once no longer deadlock, and a retried
  finalization reuses its stored signed manifest.
- **server**: `/auth/callback` sends the browser back to the web origin, and a single-tenant
  server answers `/auth/session` as the local administrator.
- **web**: costs below $0.0001 keep two significant digits instead of showing `$0.00`.
  - Usage lists session runs as "Signed-in users" (the server's group key is `-`).
  - Memory under an hour is shown in GiB-seconds.
  - A finished run without output no longer says "yet".
  - The project switcher no longer truncates names on wide screens.
- **web**: a live log tail no longer trips the tenant's rate limit. Before, it sent up to 10
  requests every 500 ms while catching up. It now fetches one page per poll, at least 2 s
  apart (5 s once caught up), and waits out `Retry-After` on 429.
- **security**: the docker and gVisor probe container gets the suite through a read-only
  mount; before, every isolation probe ended in ERROR on a host with Docker.
  - `--executor cloud` refuses to run outside a Cloud Run Jobs execution.

### Fixed (M2 review: spec, server and auth)
- **server**: an API token can no longer administer its tenant. Tokens are bound to one
  project, so tenant members, project create and delete, the tenant audit log and tenant usage
  now need a signed-in session (`403` for any token). A token may still read its own tenant
  and list its own project (`mapi_spec.api.TOKEN_TENANT_ROUTES`).
- **server**: signing in again no longer re-activates a disabled account. `/auth/callback`
  refuses it with the generic `401` and audits `auth.login_failed`; only an invitation becomes
  active at its first sign-in.
- **server**: operator status is decided on every request from `MAPI_OPERATOR_EMAILS` (and
  the dev profile's first account). Before, a demoted operator kept operator powers through
  `mapi login` ID tokens indefinitely and through browser sessions until they expired.
- **spec/server**: an API token can no longer mint tokens, whatever its scopes
  (`mapi_spec.tokens.TOKEN_NEVER`). A child token dropped its parent's expiry, vCPU budget and
  rate limit, and outlived its revocation. `POST /v1/tokens` needs a signed-in session.
- **spec/server**: `score:<target>` tokens can score. They authorized nothing, because
  `:score` checks `run` on the model and the scope covered only endpoints, so integrations
  fell back to broader `runs:create` tokens. `score:<model id>` now covers `:score` on that
  model and `score:*` on every model of the project (`RouteAccess.name_param`). The web
  tokens page, the CLI help and the docs say so.
- **server**: concurrent uploads can no longer overrun the storage quota. Storage admission
  now locks the tenant row like run admission does, so six concurrent 800-byte intents against
  a 1000-byte quota admit exactly one.
- **server**: abandoned upload intents no longer hold storage quota forever. Usage counts a
  pending intent only while its signed PUT is valid, and a new housekeeper loop in the worker
  (`MAPI_HOUSEKEEPING_INTERVAL_S`, default 300 s) marks expired intents `expired` and deletes
  whatever reached quarantine.
- **server**: expired browser sessions are deleted. The delete in `load_session` was rolled
  back with the request's 401, and nothing else removed `web_sessions` rows. The row is now
  deleted in its own transaction after the 401, and the housekeeper sweeps expired sessions
  and abandoned sign-ins.
- **server**: a FINALIZING run no longer counts its vCPU estimate on top of its metered
  attempt. Quota usage counts an active run's estimate only while its current attempt has no
  `actual` ledger entry, so runs near a daily or monthly limit are not refused by mistake.
- **server**: concurrent submissions from different tenants can no longer exceed a global
  `concurrent_runs` or vCPU limit. Run admission takes a transaction-scoped advisory lock
  after the tenant row when the global policy sets such a limit.
- **server**: the global spend cap counts work in flight. The ledger only grows when runs
  finish, so every queued run used to be admitted and dispatched against the same figure.
  Admission now adds the estimates of admitted, unmetered runs, and the dispatcher adds the
  estimates of started runs and of each run it leases in the same pass.
- **server**: held runs can no longer fill the dispatcher's window of queued runs and stall
  fair share for every tenant. Runs of suspended tenants, uploaded code paused by the kill
  switch and non-internal work past the spend cap are left out in SQL, and each tenant takes
  at most `MAPI_LEASE_CANDIDATES_PER_TENANT` (50) of the window.
- **server**: a retry resumes from the newest checkpoints of *any* earlier attempt. Before, it
  only looked at the attempt just before it, so an attempt that died before its first segment
  threw away all earlier progress. Automatic infra retries are also capped by cost
  (`MAPI_AUTO_RETRY_COST_FACTOR`, default 2× the estimate), as plan §8.3 asks, and no longer
  by count alone.
- **server**: an unknown token prefix and a wrong secret for a known prefix get the same
  answer (`the API token is invalid`), so the API no longer tells which prefixes exist.
  Unauthenticated failures that anyone can cause (`token.auth_failed`, `auth.login_failed`)
  are audited at most five times per token prefix or client and minute, so they can no longer
  flood a tenant's audit log. The housekeeper sweeps old rate counters.
- **server/web**: run event streams re-check the caller's credential every
  `MAPI_SSE_REAUTH_S` (60 s). A revoked token, an ended session or a lost membership no
  longer keeps receiving a 24-hour run's progress and logs: the stream closes and the
  reconnect is refused. The web client stops on 401 (and raises the session-expired prompt)
  or 403, where it used to retry forever.
- **server**: the audit log and the cost ledger are append-only (migration
  `0004_append_only_records`). Both runtime roles lose `UPDATE` and `DELETE` on them, and a
  trigger refuses any change to an existing row, so a request-path bug or injection can no
  longer rewrite a tenant's audit trail or billing history.
- **server/infra**: the default data residency follows the deployment. It used to be `us`
  everywhere, so on the documented EU deployment every run of the platform tenant and of
  tenants created with defaults failed as `INFRA_ERROR`. `MAPI_DEFAULT_RESIDENCY` defaults
  to the multi-region of the Cloud Run region, and Terraform sets it from the dispatch pool's
  region (`default_residency` overrides it). It applies to the platform tenant, to rows the
  tenancy migration adopts and to `POST /v1/admin/tenants` without `data_residency`.
  Submissions that no reporting executor can place are refused at admission with a clear
  `403` instead of being queued, and the worker warns at start-up about such tenants.
- **server/infra**: the API and events services no longer hold the RLS-bypassing database
  role, which plan §9 reserves for the dispatcher, reconciler and finalizer. Their platform
  lookups (sign-in, memberships, operator metadata, routing pushed events) now run as a new
  `mapi_gateway` role (migration `0005_gateway_role`, `MAPI_DATABASE_GATEWAY_URL`,
  `MAPI_DB_GATEWAY_ROLE`). It has no `BYPASSRLS`, gets explicit policies on identity tables
  and column privileges on a few run and ledger metadata columns, and cannot read datasets,
  specs, models, plots, attempts, events or logs. Every API request uses it automatically.
  Pushed events are written through tenant sessions. Terraform adds the `mapi_gateway` login
  and its `db-url-gateway` secret, and only the orchestrator can read `db-url-orchestrator`.
### Fixed (M2 review: executors, cloud and infrastructure)
- **infra**: the deployed stack can run work at all. Terraform creates a create-only
  `url-uploader` account (`MAPI_URL_UPLOADER`), without which every run failed at dispatch;
  the server refuses `MAPI_OBJECT_STORE=gcs` without a distinct uploader.
- **infra**: checkpoint policies may overwrite their own checkpoints (conditional
  `storage.objects.delete` under `.../checkpoints/` only), so PARTIAL bundles and resume see
  every segment on real GCS.
- **server**: verified and public tenants are placed only on an executor with passing,
  unexpired conformance and security-suite reports for its current caps
  (`PUT /v1/admin/executors/{name}/evidence`, `MAPI_EXECUTOR_EVIDENCE_MAX_AGE_S`); the admin
  console, CLI and web show the real state. Runs that no executor can place are refused at
  submission (503) instead of failing at lease, and the docs say that verified tenants need
  DNS enforcement verified first.
- **server**: `docker-gvisor` can qualify for shared tiers on a network declared isolated
  (`MAPI_DOCKER_NETWORK_ISOLATED`).
- **server**: Cloud Run 429s requeue a run without using up an infrastructure retry, leases
  are renewed while a batch is submitted (for at most ten lease lengths per submit), and
  `Retry-After` is capped. The executor's per-process bookkeeping is bounded.
- **sdk**: zip artifacts (skops) are no longer rejected as pickles depending on the second
  they were written.
- **infra**: user log and progress events are excluded from the exec projects' `_Default`
  log bucket; the API identity only reads the quarantine bucket.
- **deploy**: the deploy workflow is valid (no `runner` context in job env; CI runs
  actionlint); `rollback.sh --previous` picks the release before the deployed one (records
  are named after their images, rollbacks are marked); dry runs no longer claim what they did
  not do.

### Fixed (M2 review: CLI, operations and docs)
- **server**: incident containment can now stop running tenant code. `PUT /v1/admin/executors/{name}`
  (audited `executor.update`, reason required; `mapi admin executors disable|enable`, a
  Disable button in Admin → Executors) disables an executor: orchestrators lease nothing onto
  it and their periodic reports no longer reset `enabled` to true. The kill-switch update
  takes `cancel_untrusted_runs` (with `untrusted_execution_enabled=false`; `mapi admin
  kill-switches set --cancel-untrusted-runs`, "Cancel running uploaded code" in the web app),
  which moves every unfinished run of uploaded code to `cancelling`. The sandbox-escape and
  incident-response runbooks use both.
- **ops**: the jobs.run 429 alarm (plan §8.3) watches Cloud Run's 429s. The orchestrator
  exports `mapi_executor_submit_throttled_total{executor}` (every 429, retried ones
  included), and `MapiJobsRun429` and the dashboard use it instead of the API's own 429s to
  tenants on `POST /v1/runs`. The Cloud Monitoring policy uses
  `serviceruntime.googleapis.com/api/request_count` (RunJob, `response_code=429`). Its old
  filter named a metric that Cloud Run jobs do not export, and it counted every 4xx.
- **ops**: the plan's alert on running executions at 70 % of Cloud Run's 1,000 (§8.8)
  exists. The orchestrator's report exports `mapi_executor_running_executions{executor}` from
  the run table; `MapiRunningExecutions70` and a Cloud Monitoring policy on
  `run.googleapis.com/job/running_executions` fire above 700.
- **infra**: on GCP the `mapi_*` series reach Cloud Monitoring.
  - The orchestrator serves its own `/metrics` on `127.0.0.1:9464`. Before, the dispatch,
    reconcile and finalize series had no endpoint in the cloud.
  - api, events and the orchestrator run a collector sidecar (`metrics_collector_image`, an
    OpenTelemetry Collector pinned by digest). It scrapes `/metrics` over loopback with the
    metrics bearer and writes to Managed Service for Prometheus.
  - Without an image Terraform warns that the alerts and dashboard stay empty.
  - The nightly security jobs push `mapi_security_probe_unexpected` to the Pushgateway at
    `MAPI_PUSHGATEWAY_URL`.
  - `gcp-deployment.md` has a Metrics section.
- **server**: rotating the local bundle-signing key no longer breaks every existing model.
  `MAPI_SIGNING_TRUSTED_KEY_PATHS` lists retired keys (public or private PEM) that still
  verify. Terraform gains `control.kms_trusted_key_versions` (`MAPI_KMS_TRUSTED_KEY_VERSIONS`
  on every server workload). The key-rotation runbook now gives the real local and Cloud KMS
  procedures and the compromise path. It no longer tells GCP operators to rotate a
  Pub/Sub "internal token": pushes there are OIDC-authenticated.
- **docs**: data residency is enforced only on executors that declare regions (Cloud Run
  Jobs). The self-hosted executors are not checked, and the tenancy page now says so instead
  of promising a region match everywhere.
- **server**: `MAPI_DISPATCH_TIERS` restricts an orchestrator to some trust tiers. Two
  orchestrators serving different exec pools can no longer lease each other's runs (leasing
  was not pool-aware, so public-tier code could land in the paid pool).
  `gcp-deployment.md` no longer suggests a second orchestrator without it, and says that one
  environment deploys one pool until M5.
- **ops**: three Prometheus rule fixes.
  - `MapiQuota70` is now `MapiQuotaRejections`. It fires on rejections, not at 70 %, and it
    aggregates `by (scope)`, so its summary names the scope.
  - `MapiNoProgressIngested` also fires when no event was ever accepted (`or vector(0)`).
  - Clearing the spend cap now resets `mapi_global_spend_micros` and
    `mapi_global_spend_cap_micros` to 0, both at once and at every admission. The spend
    ratio ignores a cap of 0, so `MapiGlobalSpendCap90` stops firing.
- **cli**: `mapi logout --revoke` revokes the token in the project it is bound to (it was
  refused with 403 when another project was selected) and forgets the local credentials even
  when the revocation fails, then exits 1.
- **security**: the report writers create their directory, so `make security-suite` (and both
  nightly security jobs) no longer fail on a fresh checkout without `.local/`.

### Fixed (integration)
- **server**: two review branches each added a database role for the API without the
  row-level-security bypass. One is kept (`0005_gateway_role`, column-level grants, used for
  every API request); `MAPI_DB_PLATFORM` from the other is gone. The executor-disable
  migration is renumbered `0006_executor_disable` and lets the gateway change an executor's
  `enabled` and `disabled_reason`.
- **server**: an executor's `enabled` flag belongs to operators. Missing shared-tier evidence
  no longer turns it off; it shows in `shared_tiers_enabled` and the executor's message.
- **server**: a submission that no executor can place gets one answer per cause. A residency
  or resource class no executor serves is refused with `403`. A trust tier that no executor
  can take yet (enforcement not verified, no suite evidence) gets `503`.
- **server**: as the gateway role, the kill switch's `cancel_untrusted_runs` cancels each
  tenant's runs in a session of that tenant. The operators' usage report can read the ledger
  columns it sums.
- **tests**: the end-to-end spend-cap drills use a self-hosted-trusted tenant, which the local
  executor can still place.

### Known gaps
- Nothing has run against real Google Cloud. The Cloud Run, Pub/Sub, KMS and Cloud SQL code
  paths, the IAM conditions, the logging exclusion, the metrics collector sidecar and the
  Cloud Monitoring filters are checked with fakes and mocked providers only.
- No job posts the nightly conformance and security-suite results to the evidence endpoint
  yet. Until an operator records them (and the M0 DNS check passes), `verified` and `public`
  tenants cannot run on any executor.
- There is no endpoint to disable a user account. `disabled` is set in the database, and
  sign-in keeps it.
- The gateway role still reads some tables across tenants for the operator console, quota
  counters and sign-in.
- A live log tail polls the logs endpoint instead of using the run's event stream.
- The API's built-in `/docs` and `/redoc` pages load assets from a public CDN.

## [0.2.0] — M1: local vertical slice — 2026-10-15

The platform now runs end to end on one machine. From the Expert web app (or the CLI, the Python
client or plain HTTP) you can upload data, write and validate a spec, fit an E33-style
hierarchical geo MMM, inspect it, and score it: an 8-week forecast, a new market, a weekly EUR
plan and counterfactuals. Real Cloud Run and multi-tenancy come in M2 (v0.3.0). In summary:

- **server** (`mapi-server`): a single-tenant FastAPI control plane on PostgreSQL with
  procrastinate. It includes a dispatcher, a level-triggered reconciler, a metadata-only
  finalizer with signed bundle manifests, HMAC-verified run events, SSE with resume, and
  `local-process` and `docker` executors. It never imports plugin code or array libraries.
- **runner** (`mapi-runner`): a trusted supervisor and an untrusted worker harness for ingest,
  describe, validate, fit, score, evaluate, derive and render runs. It produces signed events,
  checkpoints, partial manifests and the `ui/` views the web app reads.
- **gcs-shim** (`mapi-gcs-shim`): a local Cloud Storage emulator. It provides generations,
  server-side CRC32C/MD5, V4 signed URLs and POST policies, per-principal permissions and a
  write-once bundles bucket.
- **web** and **plot-sandbox**: the Expert web app (YAML editor with live validation, runs with
  live progress and logs, model inspection, scoring) and a separate-origin, CSP-locked plot
  renderer.
- **client** and **cli**: a typed sync/async Python client for every v1 endpoint, and `mapi`
  for local and remote work.
- **devops**: `make dev` (the whole stack without Docker), digest-pinned images, docker
  compose, `make seed`, `make e2e` and `make release-check`.
- **conformance**: the corpus also runs through the live stack (`run --stack URL`), and a
  sklearn conformal fixture is added (7 fixtures in all).
- **docs**: a 10-minute quickstart, guides to the web app, CLI, client and HTTP API, a
  deployment section, an architecture overview and a security model.

Every workspace package and both web apps are versioned 0.2.0. Contract v0.1 gains only
additive changes.

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
- **docs**: the M1 documentation set. A landing page with screenshots of the running platform
  (`docs/assets/screenshots/`, captured by `scripts/docs_screenshots.mjs` and shrunk by
  `scripts/optimise_screenshots.py`, `make docs-screenshots`); a validated "zero to first
  model in 10 minutes" quickstart; guides to every page of the Expert web app, the CLI, the
  Python client and the HTTP API; an OpenAPI explorer page (a vendored, hash-pinned Redoc
  bundle over the committed `openapi.json`, `scripts/vendor_redoc.py`); a Deployment section
  (local, Docker Compose, container images, a configuration reference naming every `MAPI_*`
  variable, troubleshooting); an architecture overview and a security model; an updated
  roadmap (M2 cloud and multi-tenant, M3 Guided mode). `scripts/gen_docs_reference.py` (run by
  `make docs-build` and the MkDocs hook) generates the CLI reference from the Typer app, the
  server settings table from `Settings`, and copies the OpenAPI document.
  `conformance/tests/test_docs_platform.py` runs the CLI walkthrough, checks the generated
  pages, the configuration reference, the Redoc pin and the screenshots, and (under `make e2e`)
  runs the client, HTTP and remote-CLI walkthroughs against the live stack.

### Changed
- **docs**: anchors are validated in strict builds; mermaid is pinned to 11.17.2 instead of
  Material's moving `mermaid@11`.
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
- **devops**: the compose `api` and `worker` containers start. The `mapi/server` and
  `mapi/worker` images bake `uv.lock` and set `MAPI_UV_LOCK_PATH`, so both resolve the same
  image digest. Before, neither could resolve one and both failed at start-up.
- **devops**: compose keeps the bundle-signing key on a `keys` volume that `migrate` writes and
  `api`/`worker` mount read-only (`MAPI_SIGNING_KEY_PATH`, `MAPI_SIGNING_KEY_CREATE=false`).
  Each container used to mint its own key on its tmpfs, so the API rejected every bundle the
  worker signed, and a restart orphaned every model. `mapi-server migrate` now creates the key,
  and concurrent first starts converge on one key.
- **server**: `mapi-server api|worker|dev` make themselves non-dumpable on Linux, so
  local-process runs (same uid) cannot read their environment or memory. The worker's start-up
  warning and the operations docs state what such runs can still read.
- **server**: a `Prefer: wait` long-poll stops waiting when its client disconnects.
- **client**: `models.score()` extends the read timeout of each long-poll by its `wait` and
  treats a read timeout as "still running", so scores slower than about 90 s no longer raise
  `TransportError`. `stream_events()` counts only connections that deliver nothing towards
  `max_reconnects`, so it follows long fits behind proxies that cut connections.
- **cli**: run progress and state messages are never parsed as rich markup (no terminal
  hyperlink injection, no `MarkupError` aborting `mapi fit`).
- **devops**: `.env.example` documents every `mapi-server` setting, and the devops test now
  derives the list from `Settings`.

### Fixed (M1 review: spec, runner, shim and plots)
- **runner**: fit runs write `ui/preprocess.json` (`mapi_spec.views.PreprocessView`,
  `mapi.preprocess/v1`): each step's resolved plugin, learned JSON and small numeric state
  arrays, so the web app's Preprocessing tab is no longer empty.
- **runner**: an `INFRA_ERROR` reported by the worker fails the run as a user error. Only
  failures the supervisor sees itself are retried automatically as infrastructure failures.
- **runner**: every string the worker sends has ANSI, control and bidi characters stripped. The
  CLI's `strip_control` also removes the bidi ranges.
- **server**, **gcs-shim**: the artifacts upload policy is create-only
  (`post_policy(create_only=True)` signs `x-goog-if-generation-match: 0`, and the shim answers
  412 for an existing key).
- **plots**: recipes draw from `mapi_plots.palette`, whose dark variants keep at least 3:1
  contrast, and the plot sandbox swaps them in dark mode. The decomposition legend is titled
  "component" when baseline or controls join the channels. Faceted fans and MMM curves give
  each panel its own axes, in coordinate order.
- **conformance**: new `sklearn_store_demand` fixture. Fixtures not yet ported are listed in
  `DEFERRED` with reasons (`list --deferred`). `run --stack URL` drives fixtures through the API.

### Fixed (M1 review: server)
- `GET /v1/runs/{id}/plots` and score results are scoped to the caller's tenant and project.
- Runner JSON is parsed with a nesting limit, and deep nesting is reported as
  `INTEGRITY_FAILURE`.
- Bundle inputs are verified again at dispatch and on `:retry`.
- The API refuses unexpected `Host` headers (`MAPI_ALLOWED_HOSTS`), nginx answers only
  `MAPI_WEB_SERVER_NAMES` and the GCS shim checks hosts too, which guards against DNS
  rebinding.
- Ingest and spec validation enforce the per-resource-class data caps (`PAYLOAD_TOO_LARGE`).
- An omitted fit seed is pinned to `0` at submission, so lineage records the seed actually
  used.
- Run lifecycle:
  - A cancelled checkpointed fit ends `PARTIAL` and can be resumed (new
    `CANCELLING --complete--> FINALIZING` edge).
  - `:cancel` on a finalizing run is a no-op.
  - A run stopped at its deadline fails as `TIMEOUT` or finalizes as `PARTIAL`.
  - An exit counts only after its event backlog is ingested.
  - A restarted reconciler re-adopts live executions.
  - Stalled finalize jobs are re-queued.
  - Executor cancels no longer block the reconcile pass.
- SSE reads the run state before its events, and a run never shows another attempt's progress.
- Docker executor:
  - Each plugin namespace gets its own versioned `mapi/base-*` image (`MAPI_DOCKER_IMAGES`).
  - Run containers join the shim's network namespace; `none` is refused.
  - The supervisor and the worker run under separate uids (10001 for the worker), and
    `MAPI_REQUIRE_ISOLATION=1` fails a run closed rather than falling back to dev mode.
- The import-linter contract also forbids plugin packages, plot recipes and columnar libraries
  in `mapi_server`.
- Docs: invalid submissions are refused without a run, and `REJECTED` is reserved.

### Fixed (M1 review: web)
- Plots:
  - The plot host accepts a sandbox `exported` reply only for an export the app requested.
  - Downloading a score output saves a checksum-verified file instead of leaving the app.
- Run pages:
  - Live `progress` and `log` SSE frames, which the server sends without an id, are delivered.
  - The live log view follows the tail.
- Performance: Monaco and Arrow are no longer in first paint, and routes are lazy-loaded. A
  build-time budget keeps first-paint JavaScript under 640 KiB (it was 4.26 MB; now about
  515 KB).
- Spec editor:
  - It never replaces unsaved edits when a newer revision appears.
  - Save sends the revision the text came from as `parent_revision`.
- Accessibility:
  - No sideways scroll at phone width, and the compact nav shows every page.
  - Status colours and form borders meet WCAG AA.
  - Score form errors are marked on their fields and announced.
- The Score page resets its form and results when the version changes, and shows a loading
  state while the version loads.

### Fixed (integration)
- **tests**: a devops docstring no longer names a fake `MAPI_` variable that the configuration
  reference test picked up.
- **web**: `openapi.json` and `schema.d.ts` are regenerated (the cancel endpoint documents the
  finalizing no-op).
- **docs**: pages written before the review fixes landed now describe the shared compose
  signing key, `ui/preprocess.json`, the docker executor's network and images, and the new
  `MAPI_ALLOWED_HOSTS`, `MAPI_WEB_SERVER_NAMES`, `MAPI_DOCKER_IMAGES`,
  `MAPI_SIGNING_KEY_CREATE` and `MAPI_REQUIRE_ISOLATION` settings.

### Known gaps
- The docker executor, the compose stack and the new compose smoke test have not run against
  a real Docker daemon yet.
- Under the `local-process` executor, run code shares the orchestrator's uid and can read its
  key files. The uid split is M2.
- `MAPI_PROFILE` means a config profile to the CLI and client, but the deployment profile to
  the server.
- The API's built-in `/docs` and `/redoc` pages load assets from a public CDN.
- Conformance fixtures E13, E84-lite, E82-slim and E45 are deferred: they need new plugins or
  run kinds.

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

