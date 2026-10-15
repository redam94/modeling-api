---
description: Every environment variable the platform reads - the shared topology, mapi-server, the runner, the GCS shim, the CLI and Python client, make dev, the web app, Docker Compose, the images and the tests.
---

# Configuration reference <span class="mapi-badge">v0.2</span>

Every component is configured through **environment variables**; no component reads a config
file except the CLI and the Python client (`~/.config/mapi/config.toml`) and Docker Compose
(`.env`). This page lists every `MAPI_*` variable in the code base, grouped by who reads it.
A test (`conformance/tests/test_docs_platform.py`) scans the source tree and fails when a
variable is missing here, and the full [server settings table](../reference/generated/server-settings.md)
is generated from the code.

!!! tip "The short version"
    For a local stack you only ever need the four addresses, and only when a port is taken:
    `MAPI_API_URL`, `MAPI_GCS_ENDPOINT`, `MAPI_WEB_ORIGIN` and `MAPI_PLOTS_ORIGIN`. Export the
    same values in every shell that runs `make dev`, `make seed`, `make e2e`, `mapi` or the
    Python client. [Troubleshooting](troubleshooting.md#port-conflicts) has an example.

## Topology (every component)

These come from `mapi_spec.localdev.LocalDev`, the one contract the API, worker, runner,
GCS shim, `make dev`, `make seed`, the end-to-end tests and (through `VITE_*`) the web app
share, so they cannot disagree about where things are.

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_ENV` | `local` | Environment name; buckets are `mapi-{env}-quarantine`, `-bootstrap`, `-runs`, `-bundles`, `-data` |
| `DATABASE_URL` (or `MAPI_DATABASE_URL`) | unset | A `postgresql://` URL. Unset: `make dev` and `mapi serve` start an embedded PostgreSQL 16 (`pgserver`) in `$MAPI_DATA_DIR/pg` |
| `MAPI_API_URL` | `http://127.0.0.1:8000` | Where the API listens and is reached. `make dev` derives the API's host and port from it |
| `MAPI_GCS_ENDPOINT` | `http://127.0.0.1:4443` | The object store (the GCS shim). Signed URLs point here |
| `MAPI_WEB_ORIGIN` | `http://127.0.0.1:5173` | The web app's origin. The API and the shim allow it for CORS |
| `MAPI_PLOTS_ORIGIN` | `http://127.0.0.1:5174` | The plot sandbox's origin. **Must differ from `MAPI_WEB_ORIGIN`**: every component refuses a configuration where they are equal |
| `MAPI_DATA_DIR` | `.local` | Local state: `pg/`, `gcs/` (objects and the shim's keys), `keys/` (the bundle-signing key), `executions/`, `dev/` |

## `mapi-server` (API and worker)

`mapi-server api`, `worker`, `migrate` and `dev` read `mapi_server.settings.Settings`: each
field is the variable `MAPI_<FIELD>`. Besides the topology above:

**Identity and API**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_PROFILE` | `single-tenant` | `single-tenant`, `multi-tenant` or `dev` (multi-tenant with the built-in development identity provider). The `local-process` executor refuses `multi-tenant` |
| `MAPI_TENANT_ID`, `MAPI_PROJECT_ID` | `default` | The platform's own tenant and project (the only ones of a single-tenant install; platform runs such as `describe` live there) |
| `MAPI_API_HOST`, `MAPI_API_PORT` | `127.0.0.1`, `8000` | Bind address of `mapi-server api` when started without `--host`/`--port` |
| `MAPI_ALLOWED_HOSTS` | `127.0.0.1,localhost,api` | `Host` header values the API answers (comma-separated; the host of `MAPI_API_URL` is always allowed; `*` turns the check off). The DNS-rebinding guard of the unauthenticated M1 API |
| `MAPI_DEBUG` | `false` | Put tracebacks into problem documents' `untrusted_text`. Never in production |
| `MAPI_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |
| `MAPI_LOG_JSON` | `true` | One JSON object per log line (structlog, with `trace_id`/`span_id` and the request, run and tenant context); `false` gives a human console format |
| `MAPI_METRICS_ENABLED` | `true` | Serve Prometheus metrics at `GET /metrics` (see [Observability](observability.md)) |
| `MAPI_METRICS_TOKEN` | unset | Bearer a scraper must present on `GET /metrics`. Required in the `multi-tenant` profile, which refuses scrapes without it (the series carry tenant ids and spend) |
| `MAPI_WORKER_METRICS_PORT`, `MAPI_WORKER_METRICS_HOST` | unset, `127.0.0.1` | Serve the worker's own `GET /metrics` (dispatcher, reconciler, finalizer series) on this address; same bearer rule as the API |
| `MAPI_TRACING_ENABLED` | `true` | Create OpenTelemetry spans for requests, outgoing HTTP calls and runs |
| `MAPI_OTLP_ENDPOINT` | unset | OTLP/HTTP collector to export spans to (also read from `OTEL_EXPORTER_OTLP_ENDPOINT`); unset creates spans without exporting them |
| `MAPI_INTERNAL_TOKEN` | unset | If set (and Pub/Sub push authentication is not), `POST /internal/events` requires this bearer token. A multi-tenant profile with neither refuses every push |
| `MAPI_PUBSUB_PUSH_AUDIENCE`, `MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT` | unset | Set both to require a Google-signed OIDC token from the push subscription's service account on `POST /internal/events` (see [Authentication](authentication.md#pubsub-push) and the Cloud Run settings below) |
| `MAPI_SSE_HEARTBEAT_S` | `15` | Seconds between `: keepalive` comments on event streams |
| `MAPI_SSE_POLL_S` | `5` | Fallback poll of an event stream when no PostgreSQL `NOTIFY` arrives |
| `MAPI_SSE_REAUTH_S` | `60` | How often an open event stream re-checks its caller's credential (revoked tokens, ended sessions and lost access close it) |
| `MAPI_MAX_SIDECAR_BYTES` | 8 MiB | Largest JSON sidecar (`plots.json`, `diagnostics.json`, …) the control plane will parse |

!!! warning "`MAPI_PROFILE` means two things"
    The CLI and the Python client read `MAPI_PROFILE` as the name of a *config-file profile*;
    the server reads it as the *deployment profile*. Do not export a client profile name in a
    shell that starts `make dev` or `mapi-server`: the server refuses any value other than
    `single-tenant`, `multi-tenant` or `dev`.

**Database**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_DB_POOL_SIZE` | `10` | Request-path connections per process (plus as many overflow connections) |
| `MAPI_DB_SYSTEM_POOL_SIZE` | `5` | Platform-role connections per process (the orchestrator) |
| `MAPI_DB_GATEWAY_POOL_SIZE` | `5` | Gateway-role connections per process (the API and events service) |
| `MAPI_DB_ECHO` | `false` | Log every SQL statement |
| `MAPI_DATABASE_APP_URL` | unset | Dedicated login of the request-path role (row-level security applies). Unset: `DATABASE_URL` with the start-up option `role=$MAPI_DB_APP_ROLE` |
| `MAPI_DATABASE_SYSTEM_URL` | unset | Dedicated login of the platform role, the only one that bypasses row-level security: the orchestrator's dispatcher, reconciler, finalizer and housekeeper. The API never needs it. Unset: `DATABASE_URL` with `role=$MAPI_DB_SYSTEM_ROLE` |
| `MAPI_DATABASE_GATEWAY_URL` | unset | Dedicated login of the gateway role: what the API and events service look up before a tenant is known (sign-in, memberships, operator metadata, pushed events), with row-level security applying. Unset: `DATABASE_URL` with `role=$MAPI_DB_GATEWAY_ROLE` |
| `MAPI_DB_APP_ROLE`, `MAPI_DB_SYSTEM_ROLE`, `MAPI_DB_GATEWAY_ROLE` | `mapi_app`, `mapi_system`, `mapi_gateway` | The three runtime roles `mapi-server migrate` creates and grants (see [Authentication](authentication.md#row-level-security)) |

**Authentication (v0.3)**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_AUTH_REQUIRED` | on, except `single-tenant` | Require a session or API token on every non-public route. Cannot be turned off in a multi-tenant profile |
| `MAPI_SERVER_SECRET` | from `MAPI_SERVER_SECRET_PATH` | Root secret of the API-token pepper and the audit fingerprints. Every API instance must share it; changing it invalidates every token |
| `MAPI_SERVER_SECRET_PATH` | `server-secret` next to the bundle-signing key | File holding the root secret (`0600`, created by `mapi-server migrate`; Compose keeps it on the `keys` volume) |
| `MAPI_COOKIE_SECURE` | on when `MAPI_API_URL` is `https` | `__Host-` prefixed, `Secure` session cookies. Off: `mapi_session`/`mapi_csrf` for plain-HTTP development |
| `MAPI_OIDC_ISSUER` | unset | The OpenID provider (for example `https://accounts.google.com`). Unset outside the `dev` profile: browser sign-in is off, tokens still work |
| `MAPI_OIDC_DISCOVERY_URL` | `<issuer>/.well-known/openid-configuration` | Where the provider's metadata is |
| `MAPI_OIDC_CLIENT_ID`, `MAPI_OIDC_CLIENT_SECRET` | unset | The OAuth client registered for the BFF (the secret is optional for public clients; PKCE is always used) |
| `MAPI_OIDC_REDIRECT_URI` | `$MAPI_API_URL/auth/callback` | The registered redirect URI |
| `MAPI_OIDC_ALGORITHMS` | `RS256,ES256` | Accepted ID-token algorithms (asymmetric only) |
| `MAPI_OIDC_CLI_CLIENT_IDS` | `mapi-cli` | Public OIDC clients of the same issuer whose ID tokens the API accepts as `Authorization: Bearer` (what `mapi login` sends). Comma-separated; empty accepts API tokens only. In the `dev` profile these clients may also use the built-in IdP with a loopback redirect |
| `MAPI_OIDC_EXTRA_PARAMS` | `{}` | Extra authorization parameters as JSON, for example `{"hd": "acme.com"}` |
| `MAPI_OPERATOR_EMAILS` | unset | Verified e-mails that sign in as platform operators (comma-separated) |
| `MAPI_DEFAULT_TENANT_ADMINS` | unset | E-mails invited as admins of the default tenant at start-up (claimed at first sign-in with that verified e-mail) |
| `MAPI_DEVIDP_USERS` | `admin@dev.local,analyst@dev.local,viewer@dev.local` | The `dev` profile's accounts: the first is an operator and tenant admin, the second a runner, the rest viewers |

**Quotas and cost (v0.3)**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_PRICE_TABLE_PATH` | unset | A JSON `PriceTable`; unset: Cloud Run-like list prices (`mapi_spec.cost.DEFAULT_PRICE_TABLE`) |
| `MAPI_ESTIMATE_WALL_S` | fit 300, score 30, ingest 60, … | Per run kind, the wall time (seconds, capped at the run's timeout) a run is charged against vCPU-second quotas until it is metered (JSON) |
| `MAPI_RATE_LIMITS_ENABLED` | `true` | Enforce `requests_per_minute` at token, tenant and global level |
| `MAPI_LEASE_CANDIDATES` | `500` | Queued runs the dispatcher's fair-share planner considers per pass (runs held by a suspended tenant, the kill switches or the spend cap are not among them) |
| `MAPI_LEASE_CANDIDATES_PER_TENANT` | `50` | At most this many of them per tenant, so one tenant's held runs cannot crowd out the others |
| `MAPI_EXECUTOR_REPORT_INTERVAL_S` | `10` | How often each dispatcher reports executor health for `/v1/admin/executors` |
| `MAPI_EXECUTOR_EVIDENCE_MAX_AGE_S` | `259200` (3 days) | How long a recorded conformance or security-suite report lets an executor take `verified` and `public` tenants ([the shared-tier gate](cloud-run.md#the-shared-tier-gate)) |

**Object storage**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_OBJECT_STORE` | `shim` | `shim` (the GCS shim at `MAPI_GCS_ENDPOINT`), `gcs` (real Cloud Storage; needs the `gcp` extra, `MAPI_URL_SIGNER` and `MAPI_URL_UPLOADER`) or `memory` (tests only) |
| `MAPI_GCP_PROJECT` | unset | With `gcs`: the project of the Cloud Storage client (default: the environment's) |
| `MAPI_URL_SIGNER` | unset | With `gcs`: the service account that signs URLs through IAM `signBlob`, so no key file exists anywhere |
| `MAPI_URL_UPLOADER` | unset | With `gcs` (required): the account that signs create-only upload policies, which every run uses for its outputs. It must differ from `MAPI_URL_SIGNER` and may create objects but never delete them: a GCS POST form has no generation precondition, so only a signer without `storage.objects.delete` makes it create-only. The server refuses to start without it |
| `MAPI_GCS_PUBLIC_ENDPOINT` | `MAPI_GCS_ENDPOINT` | The base URL put into signed URLs, when browsers or containers reach the shim under another host name |
| `MAPI_GCS_ROOT` | `$MAPI_DATA_DIR/gcs` | The shim's state directory; the server reads the principals' key files under `_shim/keys/` |
| `MAPI_GCS_KEY_FILE` | `$MAPI_GCS_ROOT/_shim/keys/control.json` | Key file of the control plane's principal |
| `MAPI_EXEC_TOKEN` | from `exec.json` | Bearer token of the shim's `exec` principal, handed to local runs (local executors only, never in the cloud) |

**Executors**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_EXECUTOR` | `local-process` | `local-process`, `docker`, `docker-gvisor` or `cloudrun-jobs` ([Cloud Run Jobs executor](cloud-run.md), with its own settings) |
| `MAPI_RUNNER_COMMAND` | `mapi-runner` next to the interpreter | The supervisor's command line (shell-quoted) for `local-process` |
| `MAPI_EXECUTOR_WORKDIR` | `$MAPI_DATA_DIR/executions` | Per-run scratch of the local executor |
| `MAPI_DOCKER_IMAGE` | `mapi/base-bayes:<version>` | Runner image of the docker executors for runs without a mapped plugin (ingest, describe) |
| `MAPI_DOCKER_IMAGES` | `mmm`, `tabular`: `base-bayes`; `sklearn`: `base-ml`; `ts`: `base-forecast` | Plugin namespace to runner image (JSON), tagged with the workspace version |
| `MAPI_DOCKER_NETWORK` | `none` | The run containers' network: `container:<gcs-shim container>` or an internal network that contains only the shim. `none` is refused while the bootstrap URL is HTTP |
| `MAPI_DOCKER_NETWORK_ISOLATED` | `false` | Declare `MAPI_DOCKER_NETWORK` an internal network holding only the shim, so `docker-gvisor` reports enforced egress and DNS. `verified` and `public` tenants still need a recorded security-suite run with the network probes blocked ([docker](docker.md)) |
| `MAPI_DOCKER_RUNTIME_GVISOR` | `runsc` | The OCI runtime of `docker-gvisor` |
| `MAPI_CANCEL_GRACE_S` | `10` | Seconds between SIGTERM and SIGKILL when a run is cancelled |

**Dispatcher and reconciler**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_DISPATCH_RATE_PER_MINUTE`, `MAPI_DISPATCH_BURST` | `180`, `10` | Token bucket in front of the executor (Cloud Run's `jobs.run` quota) |
| `MAPI_DISPATCH_BATCH`, `MAPI_DISPATCH_POLL_S` | `10`, `1` | Queued runs leased per pass, and the pause between passes |
| `MAPI_DISPATCH_LEASE_S` | `120` | How long a dispatcher's lease on a run lasts; a live dispatcher extends it every third of that while the run waits to be submitted (for at most ten lease lengths per submit) |
| `MAPI_DISPATCH_TIERS` | all | Trust tiers whose runs this orchestrator leases (comma-separated). Several orchestrators that serve different exec pools need disjoint lists, or any of them leases any tenant's runs |
| `MAPI_RECONCILE_INTERVAL_S` | `10` | The reconciler's level-triggered loop |
| `MAPI_STALL_INTERVALS` | `15` | A run is `stalled` (a warning) after this many progress intervals of silence |
| `MAPI_COMPLETE_EVENT_GRACE_S` | `60` | How long to wait for a run's `complete` event after the executor reports an exit |
| `MAPI_LOST_EXECUTION_GRACE_S` | `60` | How long an execution may be missing from the executor before the attempt is lost |
| `MAPI_DEADLINE_GRACE_S` | `60` | Slack after a run's timeout before the reconciler stops it |
| `MAPI_MAX_INFRA_RETRIES` | `2` | Automatic retries after infrastructure failures |
| `MAPI_HOUSEKEEPING_INTERVAL_S` | `300` | Sweep of expired upload intents (their quarantine objects are deleted), expired browser sessions and abandoned sign-ins |
| `MAPI_AUTO_RETRY_WINDOW_S` | `1800` | No automatic retry of a run that has been going for longer than this |
| `MAPI_AUTO_RETRY_COST_FACTOR` | `2` | No automatic retry once a run has cost this multiple of its estimate |
| `MAPI_FINALIZE_MAX_ATTEMPTS` | `5` | Attempts of the finalizer job before a run fails |

**Catalog and signing**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_CATALOG_PATH` | unset | A catalog JSON exported at image build; unset: the worker queues a sandboxed `describe` run |
| `MAPI_IMAGE_DIGEST` | `sha256` of `uv.lock` | The runner image digest the catalog is cached under |
| `MAPI_UV_LOCK_PATH` | found above the working directory | The `uv.lock` whose hash is the local image digest |
| `MAPI_SIGNING_KEY_PATH` | `$MAPI_DATA_DIR/keys/bundle-signing-p256.pem` | The EC P-256 key that signs bundle manifests (standing in for Cloud KMS). The worker signs and the API verifies, so both must load the same file. `mapi-server migrate` creates it |
| `MAPI_SIGNING_TRUSTED_KEY_PATHS` | none | Retired bundle-signing keys (PEM files, public or private; comma-separated) whose signatures still verify, so a key rotation does not break the models the old key signed (`ops/runbooks/rotating-keys.md`) |
| `MAPI_SIGNING_KEY_CREATE` | `true` | Create the key on first use when it is missing. Compose turns it off for `api` and `worker`, so a missing key fails start-up instead of a new key being minted |
| `MAPI_SIGNER` | `local` | `local`: the key file above. `kms`: a Cloud KMS asymmetric key version signs (`gcp` extra) |
| `MAPI_KMS_KEY_VERSION` | unset | With `MAPI_SIGNER=kms`: the `projects/…/cryptoKeyVersions/N` that signs new manifests |
| `MAPI_KMS_TRUSTED_KEY_VERSIONS` | none | Older key versions whose signatures still verify (comma-separated; rotation) |

**Cloud Run Jobs** (`MAPI_EXECUTOR=cloudrun-jobs`; see [Cloud Run Jobs executor](cloud-run.md))

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_CLOUDRUN_PROJECT` | unset (required) | The exec project holding the Job inventory |
| `MAPI_CLOUDRUN_REGION` | `europe-west1` | Region of the inventory |
| `MAPI_DEFAULT_RESIDENCY` | derived | Data residency of the platform tenant, of tenants the migrations adopt and of tenants created without one. Default: the multi-region of `MAPI_CLOUDRUN_REGION` with `cloudrun-jobs` (`eu` for `europe-west1`), else `us` |
| `MAPI_CLOUDRUN_JOB_TEMPLATE` | `mapi-{env}-{base}-{class}` | Job id convention; it must match the Jobs the infrastructure creates |
| `MAPI_CLOUDRUN_PLUGIN_BASES` | `mmm`, `tabular`: `base-bayes`; `sklearn`: `base-ml`; `ts`: `base-forecast` | Plugin namespace to base image family (JSON) |
| `MAPI_CLOUDRUN_DEFAULT_BASE` | `base-bayes` | Base for runs without a mapped plugin (ingest, describe) |
| `MAPI_CLOUDRUN_SUBMITS_PER_MINUTE`, `MAPI_CLOUDRUN_SUBMIT_BURST` | `150`, `10` | Token bucket in front of `jobs.run` per orchestrator (at most 180, the regional quota) |
| `MAPI_CLOUDRUN_MAX_429_RETRIES` | `6` | `429` answers in a row before a submit fails |
| `MAPI_CLOUDRUN_BACKOFF_BASE_S`, `MAPI_CLOUDRUN_BACKOFF_CAP_S` | `1`, `64` | Exponential back-off after a `429` (never shorter than `Retry-After`) |
| `MAPI_CLOUDRUN_TIMEOUT_MARGIN_S` | `120` | Task timeout = run timeout + this margin (the reconciler stops runs at their deadline; this is the backstop) |
| `MAPI_CLOUDRUN_EGRESS_ENFORCED` | `true` | The exec network denies all egress except the restricted Google APIs VIP |
| `MAPI_CLOUDRUN_DNS_ENFORCED` | `false` | The DNS response policy is verified to apply to Jobs (the M0 DNS red-team gate). `verified` and `public` tenants (`verified` is the default tier) are placed only when both are `true` **and** passing suite evidence is recorded ([the shared-tier gate](cloud-run.md#the-shared-tier-gate)); until then their runs are refused at submission |
| `MAPI_CLOUDRUN_ADOPT_LOOKBACK_S` | `93600` | How far back orphan adoption lists executions |

**Pub/Sub push events** (`POST /internal/events`)

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_PUBSUB_PUSH_AUDIENCE` | unset | Expected audience of the push subscription's OIDC token. When set, every push needs a Google-signed token |
| `MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT` | unset | The service account the subscription signs as (set together with the audience) |
| `MAPI_PUBSUB_PUSH_SUBSCRIPTIONS` | any | Accepted subscription names (comma-separated) |
| `MAPI_GOOGLE_JWKS_URL` | `https://www.googleapis.com/oauth2/v3/certs` | Google's OIDC signing keys |

## The runner (per run)

Executors start `mapi-runner` with exactly these variables, and nothing secret in its
arguments. Never set them by hand.

| Variable | Meaning |
|---|---|
| `MAPI_RUN_ID`, `MAPI_NONCE` | The run, and the 256-bit nonce of its one-time bootstrap object |
| `MAPI_BOOTSTRAP_URL` | A template with `{nonce}`: where the supervisor reads (and deletes) the bootstrap object |
| `MAPI_EXEC_TOKEN` | Local executors only: the shim's `exec` principal, which may only get and delete bootstrap objects |
| `MAPI_WORKER_PYTHON` | The worker's interpreter (default: the supervisor's) |
| `MAPI_BASE_IMAGE` | The base image recorded in the run's lineage |
| `MAPI_REQUIRE_ISOLATION` | Set to `1` by the docker executor: the supervisor fails the run with `SANDBOX_VIOLATION` instead of running the worker in dev mode |
| `MAPI_TRACEPARENT` | The W3C `traceparent` of the dispatching span, so the supervisor's stderr diagnostics carry the run's trace id (nothing is exported from the sandbox) |
| `MAPI_LOG_JSON` | `1` (the default) makes the supervisor's stderr diagnostics JSON lines; `0` plain text |

The supervisor passes the untrusted worker only an allow-list of variables
(`mapi_spec.run.WORKER_ENV_ALLOWLIST`: `PATH`, `HOME`, `TMPDIR`, locale and thread-count
variables, `NUMBA_CACHE_DIR`, `PYTENSOR_FLAGS`, `XLA_FLAGS`, …). Every `MAPI_*` is scrubbed.

Inside the runner images, `MAPI_CACHE_SEED` (`/opt/mapi/cache`) is the baked, read-only
PyTensor and Numba cache and `MAPI_CACHE_DIR` (`/tmp/mapi-cache`) the writable symlink farm
the entry point builds over it ([Container images](images.md#compile-caches-and-the-read-only-root-filesystem)).

## The GCS shim

`mapi-gcs-shim serve` takes its defaults from the topology: `MAPI_DATA_DIR` (state in
`$MAPI_DATA_DIR/gcs`), `MAPI_ENV` (bucket names), `MAPI_GCS_ENDPOINT` (host and port) and
`MAPI_WEB_ORIGIN` (the CORS origin). Each has a command-line flag; see the
[GCS shim reference](../reference/gcs-shim.md).

## The CLI and the Python client

| Variable | Meaning |
|---|---|
| `MAPI_URL` | The API (falls back to `MAPI_API_URL`, then the profile, then `http://127.0.0.1:8000`) |
| `MAPI_TOKEN` | An API token (`mapi_…`), sent as `Authorization: Bearer`. It wins over a stored `mapi login` sign-in, and is never sent over plain HTTP to a non-loopback host |
| `MAPI_PROJECT` | The project to act in, `<tenant>/<project>` (v0.3), sent as `X-Mapi-Project` on project-scoped requests. Wins over `mapi use` |
| `MAPI_PROFILE` | The profile of the config file to use (see the warning above) |
| `MAPI_CONFIG` | The config file (default `$XDG_CONFIG_HOME/mapi/config.toml`, else `~/.config/mapi/config.toml`) |
| `MAPI_CREDENTIALS` | Where `mapi login` keeps sign-ins (default `credentials.json` next to the config file; mode `0600`) |
| `MAPI_OIDC_ISSUER` | The identity provider for `mapi login` (https, or plain http on a loopback address for a local development provider) |
| `MAPI_OIDC_CLIENT_ID` | The OIDC client id of the CLI (default `mapi-cli`) |
| `MAPI_OIDC_DISCOVERY_URL` | Where the issuer's discovery document is, when not under the issuer. The `dev` profile's built-in IdP has the issuer `https://dev-idp.mapi.localhost` and serves its document at `<api>/dev/idp` |
| `MAPI_OIDC_CLIENT_SECRET` | Only for identity providers that require one from installed apps (for example Google "desktop" clients); never printed |

Arguments (`--url`, `--token`, `--project`, `--profile`, or `Client(base_url=…, token=…,
project=…)`) win over the environment, which wins over the file. `mapi config show` prints
the result and where each value came from. `mapi login`, `mapi use` and `mapi config set`
rewrite the config file atomically with mode `0600`.

## `make dev`

`scripts/dev.py` reads the topology variables and:

| Variable | Meaning |
|---|---|
| `MAPI_STACK_PROFILE` | The servers' `MAPI_PROFILE`: `dev` (the default: sign-in through the built-in development IdP, the `default` tenant and project, the seeded accounts of `MAPI_DEVIDP_USERS` with `admin@dev.local` as tenant admin and platform operator), `single-tenant` (the credential-less M1 API) or `multi-tenant`. A separate name so that a CLI `MAPI_PROFILE` in your shell never reaches the servers |
| `MAPI_INTERNAL_TOKEN` | Outside the single-tenant profile, a random bearer for `POST /internal/events` is created in `$MAPI_DATA_DIR/dev/internal-token` and given to the servers (the end-to-end suite reads it there) |
| `MAPI_DEV_<NAME>_CMD` | Replace one service's command (shell-quoted). `<NAME>` is `GCS_SHIM`, `MIGRATE`, `API`, `WORKER`, `WEB` or `PLOTS`, e.g. `MAPI_DEV_API_CMD="uvicorn mapi_server.app:app --reload --port 8000"` |
| `NO_COLOR`, `FORCE_COLOR` | Disable or force coloured service prefixes |

`make dev ONLY=api` and `make dev WITHOUT=web,plots` choose services; `make seed SIZE=full`
loads the large demo. These are `make` arguments, not environment variables.

`make seed`, `make e2e` and `python -m mapi_conformance run --stack` sign in to a stack that
requires it: with `MAPI_TOKEN` when set, otherwise through the development IdP as
`MAPI_DEV_ACCOUNT` (default `admin@dev.local`), acting in `MAPI_PROJECT` (default
`default/default`).

## The web app and the plot sandbox

Vite build-time variables. `make dev` sets them from the topology; set them yourself for a
production build.

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_BASE` | `http://127.0.0.1:8000` | The API the app calls (it must allow the app's origin) |
| `VITE_PLOTS_ORIGIN` | `http://127.0.0.1:5174` | The plot sandbox; must differ from the app origin |
| `VITE_STORAGE_ORIGINS` | `http://127.0.0.1:4443` | Origins of signed storage URLs, allowed by the app's CSP `connect-src` |
| `VITE_APP_ORIGINS` (sandbox) | `http://127.0.0.1:5173,http://localhost:5173` | Origins allowed to frame the sandbox and talk to it |
| `VITE_AUTH_BASE` | `VITE_API_BASE` | Where the sign-in routes (`/auth/*`) live, when they are not on the API base |

The web app's Playwright smoke suite serves the app on 5173 and the sandbox on 5174. When those
ports are taken, set `MAPI_SMOKE_WEB_PORT` and `MAPI_SMOKE_PLOTS_PORT`; the suite then builds the
app for that sandbox origin and exports it to the specs as `MAPI_SMOKE_PLOTS_ORIGIN`.

The sandbox's `index.html` carries a `%MAPI_CSP%` placeholder that its Vite build replaces
with the Content-Security-Policy; it is not a variable you set.

## Docker Compose

Compose reads `.env` in the repository root (copy `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_PASSWORD` | **required** | The database password; compose refuses to start without it |
| `MAPI_VERSION` | the workspace version | The `mapi/*` image tag (`make compose-up` sets it) |
| `MAPI_ENV` | `local` | As above |
| `MAPI_STACK_PROFILE` | `dev` | The servers' `MAPI_PROFILE`, as for `make dev` |
| `MAPI_EXECUTOR` | `local-process` | The worker's executor |
| `MAPI_WORKER_TMPFS`, `MAPI_WORKER_MEMORY`, `MAPI_WORKER_CPUS` | `4g`, `12g`, `4` | The worker's `/tmp` (the scratch of every local-process run), memory limit and CPUs |
| `DOCKER_GID` | `0` | With the docker executor override: the group that owns the Docker socket |

## The `mapi/web` image (nginx)

Rendered into the nginx configuration when the container starts (only `MAPI_*` variables
are substituted).

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_WEB_ORIGIN`, `MAPI_PLOTS_ORIGIN`, `MAPI_API_URL`, `MAPI_GCS_ENDPOINT` | the local defaults | Origins written into the CSP and `frame-ancestors` headers |
| `MAPI_API_UPSTREAM` | `http://api:8000` | Where `/v1/` is proxied (a URL without a path) |
| `MAPI_WEB_SERVER_NAMES` | `127.0.0.1 localhost` | Host names the app server answers (space-separated); any other `Host` gets no response. Keep in step with `MAPI_ALLOWED_HOSTS` |
| `MAPI_WEB_SCRIPT_SRC_EXTRA` | empty | Extra `script-src` sources for the app. Widening the CSP needs a review |
| `NGINX_ENVSUBST_TEMPLATE_DIR` | `/etc/nginx/templates/all` | `…/web` or `…/plots` serves only one of the two apps |

## Image build arguments

`docker build --build-arg NAME=value` (the Makefile passes `VERSION`, `VCS_REF` and
`BUILD_DATE`). The defaults define what each image is; change them only to build a variant.

| Argument | Meaning |
|---|---|
| `MAPI_PACKAGES` | The workspace packages installed into the image (`mapi-runner mapi-steps mapi-pymc …`) |
| `MAPI_WORKER_PACKAGES` | What the `worker` target adds to `base-bayes` (`mapi-plugin-ts mapi-server[docker]`) |
| `MAPI_NO_INSTALL` | Distributions never installed (the 300 MB `nvidia-nccl-cu13`) |
| `MAPI_APT_PACKAGES` | System packages of the runtime stage (`tini`, and `g++` for PyTensor) |
| `MAPI_ASSERT_PRESENT`, `MAPI_ASSERT_ABSENT` | Distributions that must, and must not, be installed; the build fails otherwise |
| `MAPI_IMAGE_NAME`, `MAPI_DESCRIPTION` | OCI labels, and the name the catalog export records |
| `PYTHON_IMAGE` (and the other base images) | Base images, pinned by tag *and* digest (`make docker-pin`) |

## Deploying to Google Cloud

The deploy scripts under `deploy/gcp/` take their settings as flags (`--help` on each). One
variable changes where they keep local files:

| Variable | Meaning |
|---|---|
| `MAPI_DEPLOY_OUT` | Local scratch of `deploy/gcp/*.sh`: images files, saved plans, Terraform outputs (default `deploy/gcp/.out`, one directory per environment; never committed) |
| `MAPI_PUSHGATEWAY_URL` | Repository secret of the nightly workflow: the Pushgateway the security jobs push `mapi_security_probe_unexpected` to (unset: no push; `ops/README.md`) |

The variables Terraform sets on the Cloud Run services (the deployment contract with
`mapi-server`) are listed in [Deploy to Google Cloud](gcp-deployment.md#environment-of-the-cloud-run-workloads).

## Tests

| Variable | Meaning |
|---|---|
| `MAPI_E2E` | Set by `pnpm --filter @mapi/web e2e`: run the Playwright `@e2e` suite against a running stack |
| `MAPI_DEV_ACCOUNT` | The development-IdP account `make seed` and the end-to-end suites sign in as (default `admin@dev.local`) |
| `MAPI_E2E_ACCOUNTS` | The two development-IdP accounts the M2 tenancy suite makes tenant admins (default `analyst@dev.local,viewer@dev.local`) |
| `MAPI_E2E_UI_ACCOUNT` | The tenant admin of the M2 browser flow and the fit burst (default `analyst@dev.local`) |
| `MAPI_E2E_BURST_RUNS` | Opt-in: how many concurrent real fits `tests/e2e/test_m2_burst_stack.py` submits ([M2 verification](m2-verification.md)) |
| `MAPI_UPDATE_SNAPSHOTS` | `1` regenerates the contract snapshot of `mapi_spec` |
| `MAPI_HYPOTHESIS_EXAMPLES` | Examples per property in the wire-format round-trip tests (default 60) |

## Documentation

| Variable | Meaning |
|---|---|
| `MAPI_DOCS_SPEC` | The spec `scripts/docs_screenshots.mjs` (`make docs-screenshots`) opens and fits, default `eu-geo-mmm`. The script also reads `MAPI_WEB_ORIGIN` |
