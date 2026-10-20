# mapi-server

The modeling-api control plane: a FastAPI service (`/v1`), Postgres (SQLAlchemy 2 + Alembic,
procrastinate on the same database) and the orchestrator (dispatcher, reconciler, finalizer,
events, executors). It is JSON-only: it never imports plugin or array code (`mapi_sdk`,
`mapi_runner`, numpy, xarray — enforced by import-linter).

License: dual AGPL-3.0 / commercial (see ../../LICENSE.md).

## Running locally

```bash
mapi-server migrate   # Alembic migrations (incl. the procrastinate schema)
mapi-server dev       # migrate, then API (127.0.0.1:8000) + orchestrator in one process
mapi-server api       # the API only (uvicorn)
mapi-server worker    # procrastinate worker + dispatcher + reconciler + event pump
python -m mapi_server.openapi > apps/web/openapi.json
```

Without `DATABASE_URL` an embedded PostgreSQL 16 (`pgserver`) starts under
`$MAPI_DATA_DIR/pg`. Storage defaults to the GCS shim (`MAPI_OBJECT_STORE=shim`,
`MAPI_GCS_ENDPOINT`); executions run as `mapi-runner` child processes
(`MAPI_EXECUTOR=local-process`, refused when `MAPI_PROFILE=multi-tenant`) or in containers
(`docker`, `docker-gvisor`).

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` / `MAPI_DATABASE_URL` | embedded pgserver | any `postgresql://` URL |
| `MAPI_ENV` | `local` | bucket names `mapi-{env}-*` |
| `MAPI_PROFILE` | `single-tenant` | `multi-tenant` (credentials required; refuses the local-process executor) or `dev` (multi-tenant + the offline development IdP) |
| `MAPI_DATA_DIR` | `.local` | embedded Postgres, signing key, execution work dirs |
| `MAPI_OBJECT_STORE` | `shim` | `shim` (GCS emulator) or `memory` (tests) |
| `MAPI_GCS_ENDPOINT` | `http://127.0.0.1:4443` | GCS shim base URL (also the bootstrap URL base) |
| `MAPI_EXEC_TOKEN` | — | shim *exec* principal token handed to local executions |
| `MAPI_EXECUTOR` | `local-process` | `local-process`, `docker`, `docker-gvisor` |
| `MAPI_RUNNER_COMMAND` | `mapi-runner` | supervisor argv (shell-quoted) |
| `MAPI_DOCKER_IMAGE`, `MAPI_DOCKER_NETWORK` | `mapi/base-bayes:<version>`, `none` | container executor |
| `MAPI_DOCKER_GPUS` | `false` | v0.15: give `GPU`-class run containers one NVIDIA GPU (NVIDIA Container Toolkit) |
| `MAPI_TRACKING_RETENTION_DAYS` | unset | v0.11: days after a run ends that its metric points and `user` log lines are kept |
| `MAPI_CATALOG_PATH` | — | catalog JSON exported at image build; otherwise a `describe` run fills it |
| `MAPI_IMAGE_DIGEST` | `sha256(uv.lock)` | key of the cached catalog |
| `MAPI_UV_LOCK_PATH` | `uv.lock` above the working directory | lock whose sha256 is the image digest (the images set `/opt/mapi/uv.lock`) |
| `MAPI_SIGNING_KEY_PATH` | `$MAPI_DATA_DIR/keys/bundle-signing-p256.pem` | bundle manifest key (KMS emulation); api and worker must share it |
| `MAPI_SIGNING_KEY_CREATE` | `true` | create the key if missing; `migrate` always creates it, compose turns this off for api/worker |
| `MAPI_INTERNAL_TOKEN` | — | bearer required by `POST /internal/events` when set (Pub/Sub OIDC: `MAPI_PUBSUB_PUSH_AUDIENCE`, `MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT`) |
| `MAPI_OIDC_ISSUER`, `MAPI_OIDC_CLIENT_ID`, `MAPI_OIDC_CLIENT_SECRET` | — | browser sign-in (BFF, Authorization Code + PKCE) |
| `MAPI_OPERATOR_EMAILS` | — | verified e-mails that sign in as platform operators |
| `MAPI_SERVER_SECRET` | file next to the signing key | root of the API-token pepper, audit fingerprints and webhook-secret sealing key (api and worker share it) |
| `MAPI_DATABASE_APP_URL`, `MAPI_DATABASE_SYSTEM_URL` | `DATABASE_URL` + `role=` | logins of the RLS-bound request role and the platform role |

Every variable, and how to run multi-tenant: `docs/operations/configuration.md` and
`docs/operations/authentication.md`.

## Layout

| Module | Role |
|---|---|
| `mapi_server.app` | app factory, service container lifecycle |
| `mapi_server.auth` | per-request authentication and authorization (`ROUTE_ACCESS`, tokens, sessions, CSRF, rate limits), identities, OIDC relying party |
| `mapi_server.devidp` | the offline OpenID provider of the `dev` profile |
| `mapi_server.services.tenancy`, `.quotas`, `.cost`, `.audit` | memberships, quota admission and kill switches, the cost ledger, the audit log |
| `mapi_server.api.*` | routers: meta/catalog, auth (BFF), tenancy, tokens, usage, admin, datasets (v0.15: upload sessions, `blobs/missing`), specs, runs (SSE), tracking (v0.11), media, compare, experiments and run views (v0.14), models/plots, endpoints, webhooks, workflows, governance, explore, internal |
| `mapi_server.services.endpoints` | endpoints and aliases: the promotion gate, revisions and rollback, input-contract checks of score calls, inline payloads as run inputs |
| `mapi_server.services.webhooks` | the transactional outbox, the SSRF guard (resolve, check, pin), sealed secrets, signed deliveries with retries and auto-disable, the relay loop |
| `mapi_server.services.runs` | the only writer of `runs.state` (via `mapi_spec.run.transition`) |
| `mapi_server.services.dispatcher` | fair-share lease (priority classes, caps, kill switches) → intent row → bootstrap RunSpec → `executor.submit` |
| `mapi_server.services.reconciler` | level-triggered liveness, exit classification, cancel, retries |
| `mapi_server.services.finalizer` | metadata-only verification, server-side copy, signed manifests |
| `mapi_server.services.events` | HMAC verification, `(run, attempt, seq)` dedupe, progress upsert |
| `mapi_server.services.notify` | `NOTIFY` on commit + one `LISTEN` connection with fan-out |
| `mapi_server.services.workflows` | saved workflows and revisions, runs that the JSON-only engine of `mapi_workflows` drives (each data node is a sandboxed child run), schedules and triggers. The `plot` outputs of nodes go through the plot sanitizer when they are collected (`PLOT_UNSAFE` fails the node) and again when they are served |
| `mapi_server.services.tracking`, `.run_search` | v0.11 run tracking: metric series and points, params, tags, notes, the `GET /v1/runs` search grammar and LTTB downsampling |
| `mapi_server.services.media`, `.compare`, `.experiments`, `.run_views` | v0.14: run media (re-verified, copied create-only to the bundles bucket), `GET /v1/run-compare` and `/v1/run-keys`, experiments and saved views |
| `mapi_server.services.housekeeping` | expired uploads (v0.15: an arrived upload stays usable; unused for 7 days it expires), sessions, rate counters and tracking retention |
| `mapi_server.executors` | `LocalProcessExecutor`, `DockerExecutor` (v0.15: `GPU` device requests), `CloudRunJobsExecutor` (v0.15: the `base-dl` GPU Job), placement checks |
| `mapi_server.storage` | store factory; `MemoryObjectStore` for tests |

## Runner contract details owned by the server

`RunSpec.spec` and the input/output object names follow `mapi_spec.jobs`, the contract the
runner reads: for `fit`/`validate` the resolved `ModelSpec` (`use:` pinned to exact versions,
`data.dataset` pinned to `sha256:`); for `score` the `ScoreRequest` (model pinned to
`model://project/name@vN`); for `ingest` an `IngestJob`; for `describe` a `DescribeJob`; for
`evaluate`/`derive`/`render` the run's `params` validated as `EvaluateJob`/`DeriveJob`/
`RenderJob`. Inputs: `dataset` (fit data, or a score request's `data`), `scenario`,
`side.<name>`, `upload`, `bundle`, `bundle.manifest`, `plots`, and for score runs submitted
through an endpoint with inline XrJSON `inline.data` / `inline.scenario` (the
`XrJsonDataset` JSON, in the quarantine bucket). `evaluate` and `derive` default to the
dataset version the model was fitted on.

Outputs the finalizer reads (relative to the attempt prefix): `fit` → `bundle.zarr.zip`
(required) plus optional `diagnostics.json` (`list[Diagnostic]`), `metrics.json`
(`MetricsSidecar`: finite `values` are stored), `plots.json` (`list[PlotSpec]`, sanitized;
unsafe plots are dropped with a warning), `coords.json` (`{stage: {dim: CoordSummary}}` for
score-time CoordPlan checks) and `ui/*`; `ingest` → `data.zarr.zip`, `schema.json`
(`DataSchema`) and optional `summary.json` (`DataSummary`), `profile.json`, `preview.json`;
`describe` → `catalog.json` (`Catalog`).

**Object store.** `MAPI_OBJECT_STORE=shim` (default) uses `mapi_gcs_shim.ShimObjectStore` as the
shim's `control` principal, loading its key file from `MAPI_GCS_ROOT` (default
`$MAPI_DATA_DIR/gcs`) or `MAPI_GCS_KEY_FILE`; local executions get the `exec` token from the
same directory unless `MAPI_EXEC_TOKEN` is set. Start the shim first (`make dev` does).

## Tenancy and row-level security

Request handlers use a **tenant session** (`Database.tenant_session`, role `mapi_app`): every
transaction is pinned with `set_config('app.tenant_id', …, true)` and every tenant table has
`FORCE ROW LEVEL SECURITY` with a fail-closed policy (migration `0003_tenancy`). The
dispatcher, reconciler, finalizer, events service, authentication lookups and operator
endpoints use `Database.session()` (role `mapi_system`, `BYPASSRLS`). A new tenant table
must be added to `db.models.TENANT_TABLES` and get its policy in a migration; a new route
must have an entry in `mapi_spec.api.ROUTE_ACCESS` (or `auth.request.SERVER_ROUTE_ACCESS`)
or the app refuses to start.

## Tests

`uv run pytest packages/server -q` — an embedded Postgres is started once per session
(`DATABASE_URL` overrides it). Most end-to-end tests drive real subprocesses through
`LocalProcessExecutor` with a protocol-following stand-in supervisor
(`tests/mapi_server_testkit/fake_runner.py`) and an HTTP front for the in-memory store, so
failure paths (OOM, tampering, bad HMAC, partial, cancel) are deterministic.
`test_real_runner.py` runs the whole stack for real: the GCS shim, `ShimObjectStore`, the
orchestrator with `LocalProcessExecutor` and the real `mapi-runner` (describe → CSV ingest →
xgboost fit → signed artifact download → score → evaluate). Docker tests are marked `docker`
and skip without a daemon.
