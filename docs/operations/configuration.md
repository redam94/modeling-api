---
description: Every environment variable the platform reads - the shared topology, mapi-server, the runner, the GCS shim, the CLI and Python client, make dev, the web app, Docker Compose, the images and the tests.
---

# Configuration reference <span class="mapi-badge">v0.3</span>

You configure each component with **environment variables**. Only three components read a
config file: the CLI and the Python client (`~/.config/mapi/config.toml`), and Docker Compose
(`.env`). This page lists each `MAPI_*` variable in the code base, in groups for the
component that reads it. A test (`conformance/tests/test_docs_platform.py`) looks for the
variables in the source tree. If a variable is not on this page, the test fails.

Each docs build makes two tables from the code:

- The [server settings](../reference/generated/server-settings.md): each `mapi-server` field
  with its type and default.
- The [index of environment variables](../reference/generated/environment-variables.md): each
  `MAPI_*` name in the code, the images, the Terraform and the deploy scripts. The index tells
  which files use the name, and which section below explains it.

!!! info "New in v0.3"
    Sign-in and tenancy (`MAPI_PROFILE`, `MAPI_OIDC_*`, `MAPI_OPERATOR_EMAILS`,
    `MAPI_SERVER_SECRET`, the two database roles), quotas and cost (`MAPI_PRICE_TABLE_PATH`,
    `MAPI_RATE_LIMITS_ENABLED`), the Cloud Run Jobs executor (`MAPI_CLOUDRUN_*`), Cloud Storage
    and KMS (`MAPI_OBJECT_STORE=gcs`, `MAPI_SIGNER=kms`), Pub/Sub push authentication and
    observability (`MAPI_METRICS_TOKEN`, `MAPI_OTLP_ENDPOINT`). On Google Cloud, Terraform sets
    them ([the deployment contract](gcp-deployment.md#environment-of-the-cloud-run-workloads)).

!!! tip "The short version"
    For a local stack, you need only the four addresses, and only when a different process
    uses a port: `MAPI_API_URL`, `MAPI_GCS_ENDPOINT`, `MAPI_WEB_ORIGIN` and
    `MAPI_PLOTS_ORIGIN`. Export the same values in each shell that runs `make dev`,
    `make seed`, `make e2e`, `mapi` or the Python client.
    [Troubleshooting](troubleshooting.md#port-conflicts) has an example.

## Topology (every component)

These variables come from `mapi_spec.localdev.LocalDev`. This is the one contract that these
components share: the API, worker, runner, GCS shim, `make dev`, `make seed`, the end-to-end
tests and (through `VITE_*`) the web app. Thus all components agree about the location of
each service.

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_ENV` | `local` | Environment name. The buckets are `mapi-{env}-quarantine`, `-bootstrap`, `-runs`, `-bundles`, `-data` |
| `DATABASE_URL` (or `MAPI_DATABASE_URL`) | unset | A `postgresql://` URL. If unset, `make dev` and `mapi serve` start an embedded PostgreSQL 16 (`pgserver`) in `$MAPI_DATA_DIR/pg` |
| `MAPI_API_URL` | `http://127.0.0.1:8000` | The address where the API listens and where clients connect. `make dev` gets the host and port of the API from it |
| `MAPI_GCS_ENDPOINT` | `http://127.0.0.1:4443` | The object store (the GCS shim). Signed URLs point here |
| `MAPI_WEB_ORIGIN` | `http://127.0.0.1:5173` | The origin of the web app. The API and the shim permit it for CORS |
| `MAPI_PLOTS_ORIGIN` | `http://127.0.0.1:5174` | The origin of the plot sandbox. **Must be different from `MAPI_WEB_ORIGIN`**. All components refuse a configuration where they are equal |
| `MAPI_DATA_DIR` | `.local` | Local state: `pg/`, `gcs/` (objects and the keys of the shim), `keys/` (the bundle-signing key), `executions/`, `dev/` |

## `mapi-server` (API and worker)

`mapi-server api`, `worker`, `migrate` and `dev` read `mapi_server.settings.Settings`. Each
field is the variable `MAPI_<FIELD>`. These are the variables in addition to the topology
above:

**Identity and API**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_PROFILE` | `single-tenant` | `single-tenant`, `multi-tenant` or `dev` (multi-tenant with the built-in development identity provider). The `local-process` executor refuses `multi-tenant` |
| `MAPI_TENANT_ID`, `MAPI_PROJECT_ID` | `default` | The tenant and project of the platform. They are the only tenant and project of a single-tenant installation. Platform runs, for example `describe`, are in them |
| `MAPI_API_HOST`, `MAPI_API_PORT` | `127.0.0.1`, `8000` | The bind address of `mapi-server api` when you start it without `--host`/`--port` |
| `MAPI_ALLOWED_HOSTS` | `127.0.0.1,localhost,api` | The `Host` header values that the API answers (comma-separated). The host of `MAPI_API_URL` is always permitted. `*` turns the check off. This is the DNS-rebinding guard of the unauthenticated M1 API |
| `MAPI_TRUSTED_PROXIES` | unset | The addresses or CIDR ranges of the proxies in front of the API, comma-separated. Examples are the load balancer, the Cloud Run front end and the `/v1/` proxy of the web container. For a request from one of these proxies, the client address is the right-most `X-Forwarded-For` hop that is not a trusted proxy. The limits for each address (sign-up, verification, embed views), the Sybil `same_ip` check and the audit fingerprints use this address. `none`: clients connect directly, and the server ignores `X-Forwarded-For`. **Necessary in the `multi-tenant` profile**. `make dev` sets `none` for its multi-tenant stack |
| `MAPI_DEBUG` | `false` | Put tracebacks into the `untrusted_text` of problem documents. Never use this in production |
| `MAPI_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |
| `MAPI_LOG_JSON` | `true` | One JSON object for each log line (structlog, with `trace_id`/`span_id` and the context of the request, run and tenant). `false` gives a console format for persons |
| `MAPI_METRICS_ENABLED` | `true` | Serve Prometheus metrics at `GET /metrics` (refer to [Observability](observability.md)) |
| `MAPI_METRICS_TOKEN` | unset | The bearer that a scraper must send on `GET /metrics`. Necessary in the `multi-tenant` profile, which refuses scrapes without it (the series contain tenant ids and spend) |
| `MAPI_WORKER_METRICS_PORT`, `MAPI_WORKER_METRICS_HOST` | unset, `127.0.0.1` | Serve the `GET /metrics` of the worker (dispatcher, reconciler, finalizer series) on this address. The same bearer rule as the API applies |
| `MAPI_TRACING_ENABLED` | `true` | Make OpenTelemetry spans for requests, outgoing HTTP calls and runs |
| `MAPI_OTLP_ENDPOINT` | unset | The OTLP/HTTP collector that gets the spans. The server also reads it from `OTEL_EXPORTER_OTLP_ENDPOINT`. If unset, the server makes spans but does not export them |
| `MAPI_INTERNAL_TOKEN` | unset | If set (and Pub/Sub push authentication is not set), `POST /internal/events` needs this bearer token. A multi-tenant profile with neither refuses all pushes |
| `MAPI_PUBSUB_PUSH_AUDIENCE`, `MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT` | unset | Set the two to make `POST /internal/events` need an OIDC token that Google signed for the service account of the push subscription. Refer to [Authentication](authentication.md#pubsub-push) and the Cloud Run settings below |
| `MAPI_SSE_HEARTBEAT_S` | `15` | The seconds between `: keepalive` comments on event streams |
| `MAPI_SSE_POLL_S` | `5` | The fallback poll of an event stream when no PostgreSQL `NOTIFY` comes |
| `MAPI_SSE_REAUTH_S` | `60` | How frequently an open event stream does a check of the credential of its caller again. Revoked tokens, ended sessions and lost access close the stream |
| `MAPI_MAX_SIDECAR_BYTES` | 8 MiB | The largest JSON sidecar (`plots.json`, `diagnostics.json`, …) that the control plane parses |

!!! warning "`MAPI_PROFILE` means two things"
    Do not export a client profile name in a shell that starts `make dev` or `mapi-server`.
    The server refuses all values other than `single-tenant`, `multi-tenant` or `dev`. The CLI
    and the Python client read `MAPI_PROFILE` as the name of a *config-file profile*. The
    server reads it as the *deployment profile*.

**Database**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_DB_POOL_SIZE` | `10` | Request-path connections for each process (and the same number of overflow connections) |
| `MAPI_DB_SYSTEM_POOL_SIZE` | `5` | Platform-role connections for each process (the orchestrator) |
| `MAPI_DB_GATEWAY_POOL_SIZE` | `5` | Gateway-role connections for each process (the API and events service) |
| `MAPI_DB_ECHO` | `false` | Log each SQL statement |
| `MAPI_DATABASE_APP_URL` | unset | The dedicated login of the request-path role (row-level security applies). If unset: `DATABASE_URL` with the start-up option `role=$MAPI_DB_APP_ROLE` |
| `MAPI_DATABASE_SYSTEM_URL` | unset | The dedicated login of the platform role. It is the only role that bypasses row-level security. The dispatcher, reconciler, finalizer and housekeeper of the orchestrator use it. The API never needs it. If unset: `DATABASE_URL` with `role=$MAPI_DB_SYSTEM_ROLE` |
| `MAPI_DATABASE_GATEWAY_URL` | unset | The dedicated login of the gateway role. The API and events service use it for look-ups before they know the tenant (sign-in, memberships, operator metadata, pushed events). Row-level security applies. If unset: `DATABASE_URL` with `role=$MAPI_DB_GATEWAY_ROLE` |
| `MAPI_DB_APP_ROLE`, `MAPI_DB_SYSTEM_ROLE`, `MAPI_DB_GATEWAY_ROLE` | `mapi_app`, `mapi_system`, `mapi_gateway` | The three runtime roles that `mapi-server migrate` makes and grants (refer to [Authentication](authentication.md#row-level-security)) |

**Authentication (v0.3)**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_AUTH_REQUIRED` | on, but not for `single-tenant` | Make all routes that are not public need a session or API token. You cannot turn it off in a multi-tenant profile |
| `MAPI_SERVER_SECRET` | from `MAPI_SERVER_SECRET_PATH` | The root secret of the API-token pepper, the audit fingerprints and the key that seals webhook secrets. All API and worker instances must share it. If you change it, all tokens and webhook secrets become invalid |
| `MAPI_SERVER_SECRET_PATH` | `server-secret` next to the bundle-signing key | The file that holds the root secret (`0600`). `mapi-server migrate` makes it. Compose keeps it on the `keys` volume |
| `MAPI_COOKIE_SECURE` | on when `MAPI_API_URL` is `https` | Session cookies with the `__Host-` prefix and `Secure`. Off: `mapi_session`/`mapi_csrf` for development with plain HTTP |
| `MAPI_OIDC_ISSUER` | unset | The OpenID provider (for example `https://accounts.google.com`). If unset outside the `dev` profile, browser sign-in is off, but tokens still work |
| `MAPI_OIDC_DISCOVERY_URL` | `<issuer>/.well-known/openid-configuration` | The location of the metadata of the provider |
| `MAPI_OIDC_CLIENT_ID`, `MAPI_OIDC_CLIENT_SECRET` | unset | The OAuth client that you registered for the BFF. The secret is optional for public clients. The server always uses PKCE |
| `MAPI_OIDC_REDIRECT_URI` | `$MAPI_API_URL/auth/callback` | The registered redirect URI |
| `MAPI_OIDC_ALGORITHMS` | `RS256,ES256` | The ID-token algorithms that the server accepts (only asymmetric) |
| `MAPI_OIDC_CLI_CLIENT_IDS` | `mapi-cli` | Public OIDC clients of the same issuer. The API accepts their ID tokens as `Authorization: Bearer` (what `mapi login` sends). Comma-separated. If empty, the API accepts only API tokens. In the `dev` profile, these clients can also use the built-in IdP with a loopback redirect |
| `MAPI_OIDC_EXTRA_PARAMS` | `{}` | More authorization parameters as JSON, for example `{"hd": "acme.com"}` |
| `MAPI_OPERATOR_EMAILS` | unset | The verified e-mails that sign in as platform operators (comma-separated) |
| `MAPI_DEFAULT_TENANT_ADMINS` | unset | The e-mails that the server invites as admins of the default tenant at start-up. The user gets the invitation at the first sign-in with that verified e-mail |
| `MAPI_DEVIDP_USERS` | `admin@dev.local,analyst@dev.local,viewer@dev.local` | The accounts of the `dev` profile. The first is an operator and tenant admin, the second is a runner, and the other accounts are viewers |

**Quotas and cost (v0.3)**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_PRICE_TABLE_PATH` | unset | A JSON `PriceTable`. If unset: list prices like Cloud Run prices (`mapi_spec.cost.DEFAULT_PRICE_TABLE`) |
| `MAPI_ESTIMATE_WALL_S` | fit 300, score 30, ingest 60, … | For each run kind, the wall time in seconds that the server charges against vCPU-second quotas until it meters the run (JSON). The maximum is the timeout of the run |
| `MAPI_RATE_LIMITS_ENABLED` | `true` | Enforce `requests_per_minute` at token, tenant and global level |
| `MAPI_LEASE_CANDIDATES` | `500` | The queued runs that the fair-share planner of the dispatcher looks at in each pass. This does not include runs that are held by a suspended tenant, the kill switches or the spend cap |
| `MAPI_LEASE_CANDIDATES_PER_TENANT` | `50` | The maximum number of these runs for each tenant. Thus the held runs of one tenant cannot push out the runs of other tenants |
| `MAPI_EXECUTOR_REPORT_INTERVAL_S` | `10` | How frequently each dispatcher reports executor health for `/v1/admin/executors` |
| `MAPI_EXECUTOR_EVIDENCE_MAX_AGE_S` | `259200` (3 days) | How long a recorded conformance or security-suite report lets an executor take `verified` and `public` tenants ([the shared-tier gate](cloud-run.md#the-shared-tier-gate)) |

**Plot rendering and embed links (v0.4)**

Refer to [Sharing plots](../guides/sharing-plots.md).

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_RENDER_TIMEOUT_S` | `300` | The wall-clock limit of a `POST /v1/plots:render` run. The sandbox stops the run after this time |
| `MAPI_INSPECT_TIMEOUT_S` | `300` | The wall-clock limit of an upload inspect run (`POST /v1/datasets/{dataset_id}/uploads/{upload_id}/inspect`). The run reads only the metadata of the file |
| `MAPI_RENDER_PLOTLY_ENABLED` | `false` | Accept Plotly plots for static rendering. This needs kaleido and a Chrome or Chromium in the runner image. The base images do not include them. While this setting is off, the server refuses Plotly renders with `MODE_NOT_SUPPORTED` |
| `MAPI_EMBED_RATE_PER_MINUTE` | `300` | The public views in each minute for one embed link. More views get `429 RATE_LIMITED` |
| `MAPI_EMBED_IP_RATE_PER_MINUTE` | `120` | The public embed requests in each minute from one client address, valid or not valid. This makes token guesses slower |
| `MAPI_EMBED_CACHE_S` | `60` | How long an API instance uses the resolved plot of an embed again. The server still does a check of expiry and revocation on each request. Responses are `Cache-Control: private, no-cache`. Thus a revoked link stops on the next load |
| `MAPI_EMBED_IMAGE_URL_TTL_S` | `900` | The life of the signed URL of the PNG fallback of an embed |

**Object storage**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_OBJECT_STORE` | `shim` | `shim` (the GCS shim at `MAPI_GCS_ENDPOINT`), `gcs` (real Cloud Storage. It needs the `gcp` extra, `MAPI_URL_SIGNER` and `MAPI_URL_UPLOADER`) or `memory` (only for tests) |
| `MAPI_GCP_PROJECT` | unset | With `gcs`: the project of the Cloud Storage client (default: the project of the environment) |
| `MAPI_URL_SIGNER` | unset | With `gcs`: the service account that signs URLs through IAM `signBlob`. Thus no key file is necessary |
| `MAPI_URL_UPLOADER` | unset | With `gcs` (necessary): the account that signs create-only upload policies. Each run uses these policies for its outputs. It must be different from `MAPI_URL_SIGNER`. It can make objects, but it must never delete them. A GCS POST form has no generation precondition. Thus only a signer without `storage.objects.delete` makes the form create-only. The server does not start without this account |
| `MAPI_GCS_PUBLIC_ENDPOINT` | `MAPI_GCS_ENDPOINT` | The base URL in signed URLs, when browsers or containers connect to the shim with a different host name |
| `MAPI_GCS_ROOT` | `$MAPI_DATA_DIR/gcs` | The state directory of the shim. The server reads the key files of the principals under `_shim/keys/` |
| `MAPI_GCS_KEY_FILE` | `$MAPI_GCS_ROOT/_shim/keys/control.json` | The key file of the principal of the control plane |
| `MAPI_EXEC_TOKEN` | from `exec.json` | The bearer token of the `exec` principal of the shim. The server gives it to local runs (only local executors, never in the cloud) |

**Executors**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_EXECUTOR` | `local-process` | `local-process`, `docker`, `docker-gvisor` or `cloudrun-jobs` ([Cloud Run Jobs executor](cloud-run.md), with its own settings) |
| `MAPI_RUNNER_COMMAND` | `mapi-runner` next to the interpreter | The command line of the supervisor (shell-quoted) for `local-process` |
| `MAPI_EXECUTOR_WORKDIR` | `$MAPI_DATA_DIR/executions` | The scratch directory of the local executor for each run |
| `MAPI_DOCKER_IMAGE` | `mapi/base-bayes:<version>` | The runner image of the docker executors for runs without a mapped plugin (ingest, describe) |
| `MAPI_DOCKER_IMAGES` | `mmm`, `sci`, `tabular`: `base-bayes`; `sklearn`: `base-ml`; `ts`: `base-forecast` | Plugin namespace to runner image (JSON), with the tag of the workspace version |
| `MAPI_DOCKER_NETWORK` | `none` | The network of the run containers: `container:<gcs-shim container>`, or an internal network that contains only the shim. The server refuses `none` while the bootstrap URL is HTTP |
| `MAPI_DOCKER_NETWORK_ISOLATED` | `false` | Declare that `MAPI_DOCKER_NETWORK` is an internal network with only the shim. Then `docker-gvisor` reports enforced egress and DNS. `verified` and `public` tenants also need a recorded security-suite run in which the network probes were stopped ([docker](docker.md)) |
| `MAPI_DOCKER_RUNTIME_GVISOR` | `runsc` | The OCI runtime of `docker-gvisor` |
| `MAPI_CANCEL_GRACE_S` | `10` | The seconds between SIGTERM and SIGKILL when you cancel a run |

**Dispatcher and reconciler**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_DISPATCH_RATE_PER_MINUTE`, `MAPI_DISPATCH_BURST` | `180`, `10` | The token bucket in front of the executor (the `jobs.run` quota of Cloud Run) |
| `MAPI_DISPATCH_BATCH`, `MAPI_DISPATCH_POLL_S` | `10`, `1` | The queued runs that the dispatcher leases in each pass, and the pause between passes |
| `MAPI_DISPATCH_LEASE_S` | `120` | How long the lease of a dispatcher on a run lasts. While the run waits for submission, a live dispatcher extends the lease after each third of this time. The maximum is ten lease lengths for each submit |
| `MAPI_DISPATCH_TIERS` | all | The trust tiers whose runs this orchestrator leases (comma-separated). If many orchestrators serve different exec pools, they need lists with no common tiers. If not, each orchestrator leases the runs of all tenants |
| `MAPI_RECONCILE_INTERVAL_S` | `10` | The level-triggered loop of the reconciler |
| `MAPI_STALL_INTERVALS` | `15` | A run is `stalled` (a warning) after this number of progress intervals without events |
| `MAPI_COMPLETE_EVENT_GRACE_S` | `60` | How long to wait for the `complete` event of a run after the executor reports an exit |
| `MAPI_LOST_EXECUTION_GRACE_S` | `60` | How long an execution can be missing from the executor before the attempt is lost |
| `MAPI_DEADLINE_GRACE_S` | `60` | The extra time after the timeout of a run before the reconciler stops it |
| `MAPI_MAX_INFRA_RETRIES` | `2` | The automatic retries after infrastructure failures |
| `MAPI_HOUSEKEEPING_INTERVAL_S` | `300` | The sweep of expired upload intents (the server deletes their quarantine objects), expired browser sessions and abandoned sign-ins |
| `MAPI_AUTO_RETRY_WINDOW_S` | `1800` | No automatic retry of a run that has continued for longer than this time |
| `MAPI_AUTO_RETRY_COST_FACTOR` | `2` | No automatic retry when the cost of a run is this multiple of its estimate |
| `MAPI_FINALIZE_MAX_ATTEMPTS` | `5` | The attempts of the finalizer job before a run fails |

**Catalog and signing**

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_CATALOG_PATH` | unset | A catalog JSON that the image build exports. If unset, the worker puts a sandboxed `describe` run in the queue |
| `MAPI_IMAGE_DIGEST` | `sha256` of `uv.lock` | The digest of the runner image. The catalog cache uses it as the key |
| `MAPI_UV_LOCK_PATH` | found above the working directory | The `uv.lock` whose hash is the image digest. When it is unset, the digest of a source checkout also hashes `packages/*/src` and `plugins/*/src`, so `make dev` makes a new catalog after a code change |
| `MAPI_SIGNING_KEY_PATH` | `$MAPI_DATA_DIR/keys/bundle-signing-p256.pem` | The EC P-256 key that signs bundle manifests (in place of Cloud KMS). The worker signs and the API verifies, thus the two must load the same file. `mapi-server migrate` makes it |
| `MAPI_SIGNING_TRUSTED_KEY_PATHS` | none | Old bundle-signing keys (PEM files, public or private, comma-separated) whose signatures still verify. Thus a key rotation does not break the models that the old key signed (`ops/runbooks/rotating-keys.md`) |
| `MAPI_SIGNING_KEY_CREATE` | `true` | Make the key at the first use if it is missing. Compose turns it off for `api` and `worker`. Thus, if the key is missing, start-up fails, and the server does not mint a new key |
| `MAPI_SIGNER` | `local` | `local`: the key file above. `kms`: a Cloud KMS asymmetric key version signs (`gcp` extra) |
| `MAPI_KMS_KEY_VERSION` | unset | With `MAPI_SIGNER=kms`: the `projects/…/cryptoKeyVersions/N` that signs new manifests |
| `MAPI_KMS_TRUSTED_KEY_VERSIONS` | none | Older key versions whose signatures still verify (comma-separated, for rotation) |

**Cloud Run Jobs** (`MAPI_EXECUTOR=cloudrun-jobs`; refer to [Cloud Run Jobs executor](cloud-run.md))

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_CLOUDRUN_PROJECT` | unset (necessary) | The exec project that holds the Job inventory |
| `MAPI_CLOUDRUN_REGION` | `europe-west1` | The region of the inventory |
| `MAPI_DEFAULT_RESIDENCY` | derived | The data residency of the platform tenant, of tenants that the migrations adopt, and of tenants that you make without a residency. Default: with `cloudrun-jobs`, the multi-region of `MAPI_CLOUDRUN_REGION` (`eu` for `europe-west1`). If not, `us` |
| `MAPI_CLOUDRUN_JOB_TEMPLATE` | `mapi-{env}-{base}-{class}` | The rule for Job ids. It must agree with the Jobs that the infrastructure makes |
| `MAPI_CLOUDRUN_PLUGIN_BASES` | `mmm`, `sci`, `tabular`: `base-bayes`; `sklearn`: `base-ml`; `ts`: `base-forecast` | Plugin namespace to base image family (JSON) |
| `MAPI_CLOUDRUN_DEFAULT_BASE` | `base-bayes` | The base for runs without a mapped plugin (ingest, describe) |
| `MAPI_CLOUDRUN_SUBMITS_PER_MINUTE`, `MAPI_CLOUDRUN_SUBMIT_BURST` | `150`, `10` | The token bucket in front of `jobs.run` for each orchestrator (maximum 180, the regional quota) |
| `MAPI_CLOUDRUN_MAX_429_RETRIES` | `6` | The number of `429` answers in sequence before a submit fails |
| `MAPI_CLOUDRUN_BACKOFF_BASE_S`, `MAPI_CLOUDRUN_BACKOFF_CAP_S` | `1`, `64` | The exponential back-off after a `429` (never shorter than `Retry-After`) |
| `MAPI_CLOUDRUN_TIMEOUT_MARGIN_S` | `120` | Task timeout = run timeout + this margin. The reconciler stops runs at their deadline. This margin is the backstop |
| `MAPI_CLOUDRUN_EGRESS_ENFORCED` | `true` | The exec network denies all egress, but not the restricted Google APIs VIP |
| `MAPI_CLOUDRUN_DNS_ENFORCED` | `false` | We verified that the DNS response policy applies to Jobs (the M0 DNS red-team gate). The dispatcher puts `verified` and `public` tenants on the executor only when the two settings are `true` **and** passing suite evidence is on record ([the shared-tier gate](cloud-run.md#the-shared-tier-gate)). `verified` is the default tier. Until then, the server refuses their runs at submission |
| `MAPI_CLOUDRUN_ADOPT_LOOKBACK_S` | `93600` | How far back orphan adoption lists executions |

**Webhooks (v0.4)** (refer to [Endpoints and webhooks](../guides/endpoints-and-webhooks.md))

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_WEBHOOK_POLL_INTERVAL_S` | `2` | How frequently the relay of the worker looks for due deliveries. The relay gives them to the `webhooks` procrastinate queue |
| `MAPI_WEBHOOK_MAX_ATTEMPTS` | `8` | The tries for each delivery (1 to 8). Retries wait 1 min, 5 min, 30 min, 2 h, 6 h, 12 h and 24 h, plus a jitter of up to 10% |
| `MAPI_WEBHOOK_AUTO_DISABLE_AFTER` | `20` | Disable a webhook after this number of consecutive events that the server could not send. A `410 Gone` disables it immediately. The audit log records `webhook.disabled` |
| `MAPI_WEBHOOK_MAX_PER_PROJECT` | `25` | The maximum number of webhooks for one project (more get `409`) |
| `MAPI_WEBHOOK_RETENTION_DAYS` | `30` | The days that the server keeps events and their delivery logs |
| `MAPI_WEBHOOK_CA_FILE` | unset | An extra PEM CA bundle that the server trusts for receivers (a private CA). The server always uses the system trust store |
| `MAPI_WEBHOOK_ALLOW_HTTP` | `false` | Only for development: accept `http://` URLs for the hosts in `MAPI_WEBHOOK_DEV_ALLOWLIST`. Refused in the `multi-tenant` profile |
| `MAPI_WEBHOOK_DEV_ALLOWLIST` | none | Only for development: host names (comma-separated, optionally `host:port`) that the SSRF address and port checks do not apply to, for example a receiver on `localhost`. Refused in the `multi-tenant` profile |

The server seals webhook signing secrets with a key from `MAPI_SERVER_SECRET`. If you change
the server secret, you cannot use the webhook secrets. Rotate them after the change.

**Workflows (v0.5)** (refer to [Workflows](../guides/workflows.md))

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_WORKFLOW_TICK_S` | `1` | How frequently (seconds) the workflow loop of the orchestrator moves active workflow runs forward, and fires due schedules and debounced triggers |
| `MAPI_WORKFLOW_MAX_ACTIVE_RUNS` | `20` | The workflow runs that one project can have at the same time, with sub-workflow runs. More runs get `429`. A `subworkflow.run` node over the limit fails with `QUOTA_EXCEEDED` |
| `MAPI_WORKFLOW_MAX_SCHEDULES` | `20` | The active cron schedules for each project |
| `MAPI_WORKFLOW_MAX_TRIGGERS` | `20` | The active dataset-version triggers for each project |
| `MAPI_WORKFLOW_NODE_TIMEOUT_S` | `1800` | The timeout of the child run of a node when the node sets no timeout |
| `MAPI_WORKFLOW_MAX_JSON_OUTPUT_BYTES` | `1048576` | The largest JSON output (tables, metrics, reports, plots) that a node can return. A larger output fails the node (`INTEGRITY_FAILURE`) |

**Pub/Sub push events** (`POST /internal/events`)

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_PUBSUB_PUSH_AUDIENCE` | unset | The expected audience of the OIDC token of the push subscription. When set, each push needs a token that Google signed |
| `MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT` | unset | The service account that the subscription signs as (set it together with the audience) |
| `MAPI_PUBSUB_PUSH_SUBSCRIPTIONS` | any | The accepted subscription names (comma-separated) |
| `MAPI_GOOGLE_JWKS_URL` | `https://www.googleapis.com/oauth2/v3/certs` | The OIDC signing keys of Google |

**Sign-up, e-mail and abuse controls (v0.4)** (refer to [Sign-up and abuse controls](signup-and-abuse.md))

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_SIGNUP_ENABLED` | on in `dev` and `multi-tenant` when a mailer works, off in `single-tenant` | Self-service sign-up at `POST /auth/signup`. If you set it on without a mailer that works, the server does not start |
| `MAPI_MAILER` | `outbox` in `dev`, `smtp` in `multi-tenant`, `console` in other profiles | `console` (log the message), `outbox` (JSON files that you read at `GET /dev/outbox` in `dev`), `smtp` or `none`. `multi-tenant` refuses `console` and `outbox` while sign-up is on, because each person who reads the logs or the disk could verify all addresses |
| `MAPI_MAIL_FROM` | `modeling-api <no-reply@mapi.localhost>` | The `From:` header |
| `MAPI_OUTBOX_DIR` | `$MAPI_DATA_DIR/outbox` | The directory where the outbox mailer writes (mode `0600`, the newest 200 messages stay) |
| `MAPI_SMTP_HOST`, `MAPI_SMTP_PORT` | unset, `587` | The SMTP server. Without a host, the `smtp` mailer is off, and sign-up is also off |
| `MAPI_SMTP_TLS` | `starttls` | `starttls` (necessary, not opportunistic: the server refuses an SMTP server without it) or `ssl` (implicit TLS, port 465). The server verifies the certificate against the system trust store |
| `MAPI_SMTP_USERNAME`, `MAPI_SMTP_PASSWORD` | unset | SMTP authentication (keep the password in a secret store) |
| `MAPI_SMTP_TIMEOUT_S` | `10` | The connection and command timeout |
| `MAPI_SIGNUP_VERIFY_URL` | `$MAPI_WEB_ORIGIN/verify-email?token={token}` | The link that the server sends in the e-mail for a sign-up. The server replaces `{token}` |
| `MAPI_SIGNUP_BLOCKED_DOMAINS` | none | The e-mail domains that the server refuses, in addition to the built-in disposable list (comma-separated, subdomains included) |
| `MAPI_SIGNUP_BLOCKLIST_PATH` | unset | A file of disposable domains, one on each line (`#` comments), for example a public list that a group keeps current |
| `MAPI_SIGNUP_PER_IP_PER_HOUR` | `5` | The sign-ups that the server accepts for each client address in each hour |
| `MAPI_SIGNUP_PER_DOMAIN_PER_DAY` | `25` | The verified sign-ups for each e-mail domain in each day. Unverified sign-ups do not count. The shared free-mail domains are exempt |
| `MAPI_SIGNUP_GLOBAL_PER_HOUR` | `500` | The verified sign-ups in each hour on the whole platform (unverified sign-ups do not count) |
| `MAPI_SIGNUP_SHARED_EMAIL_DOMAINS` | built-in list (`gmail.com`, `outlook.com`, ...) | Free-mail domains that persons with no relation share (comma-separated, replaces the built-in list). These domains have no cap for each domain and no `domain_burst` flag |
| `MAPI_VERIFY_PER_IP_PER_MINUTE` | `20` | The e-mail verification attempts for each client address in each minute |
| `MAPI_SIGNUP_SYBIL_WINDOW_DAYS` | `30` | The look-back of the Sybil heuristics |
| `MAPI_SIGNUP_SYBIL_THRESHOLD` | `3` | The workspaces from one client address, browser or e-mail in the window. After this number, the server flags the next workspace for review. The server flags a domain at two times this number in one hour |
| `MAPI_SIGNUP_FLAGGED_SUSPEND` | `false` | Suspend the workspace of a flagged sign-up until an operator approves it. Default: the workspace works and waits in the review queue |
| `MAPI_SIGNUP_RETENTION_DAYS` | `30` | The server deletes unverified sign-ups, with their e-mail, after this number of days |
| `MAPI_PUBLIC_CODE_UPLOAD_ENABLED` | `false` | Cut line B (planned): let identity-verified `public` tenants run uploaded code. Off: public tenants run only built-in models and declarative specs |

**Tenant deletion and GDPR export (v0.4)** (refer to [Deleting data](../guides/data-deletion.md))

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_TENANT_DELETION_GRACE_S` | `604800` (7 days) | The grace period of a deletion. During this period, the tenant is suspended and can cancel the deletion |
| `MAPI_DELETION_REAUTH_MAX_AGE_S` | unset | To request a deletion, also need a sign-in that is not older than this number of seconds. If unset, only the confirmation phrase is necessary |
| `MAPI_DELETION_RUN_STOP_TIMEOUT_S` | `3600` | How long a purge waits for the cancelled runs of the tenant to stop before it continues |
| `MAPI_DELETION_MAX_ATTEMPTS` | `8` | The automatic retries of a failed purge, with back-off from 5 minutes to 6 hours. After this, an operator retries |
| `MAPI_BUNDLE_RETENTION_DAYS` | `30` | The retention period of the bundles bucket (Terraform `bundle_retention_days`). It tells when a purge retries the objects that a retention lock kept |
| `MAPI_LIFECYCLE_INTERVAL_S` | `15` | The interval of the deletion and export scheduler of the orchestrator |
| `MAPI_EXPORT_MAX_INLINE_BYTES` | 256 MiB | The dataset bytes in an export archive. The manifest lists the remaining bytes |
| `MAPI_EXPORT_RETENTION_S` | `604800` (7 days) | How long the server keeps an export archive |
| `MAPI_EXPORT_LINK_TTL_S` | `900` | The life of the signed download link of an export (a new link on each `GET`) |
| `MAPI_EXPORTS_PER_DAY` | `3` | The exports that a tenant can request in each day (one at a time) |
| `MAPI_EXPORT_MAX_METADATA_BYTES` | 256 MiB | The metadata rows in an export archive. The manifest lists the tables that are not complete |
| `MAPI_EXPORT_BUILD_TIMEOUT_S` | `3600` | The maximum time of an export build. If a build is still `running` five minutes after this time (its process stopped), the server builds it again one time. Then the build fails |

## The runner (per run)

Executors start `mapi-runner` with exactly these variables. Its arguments contain nothing
secret. Never set these variables manually.

| Variable | Meaning |
|---|---|
| `MAPI_RUN_ID`, `MAPI_NONCE` | The run, and the 256-bit nonce of its one-time bootstrap object |
| `MAPI_BOOTSTRAP_URL` | A template with `{nonce}`: the location where the supervisor reads (and deletes) the bootstrap object |
| `MAPI_EXEC_TOKEN` | Only local executors: the `exec` principal of the shim. It can only get and delete bootstrap objects |
| `MAPI_WORKER_PYTHON` | The interpreter of the worker (default: the interpreter of the supervisor) |
| `MAPI_BASE_IMAGE` | The base image in the lineage of the run |
| `MAPI_REQUIRE_ISOLATION` | The docker executor sets it to `1`. The supervisor then fails the run with `SANDBOX_VIOLATION`, and it does not run the worker in dev mode |
| `MAPI_TRACEPARENT` | The W3C `traceparent` of the dispatching span. Thus the stderr diagnostics of the supervisor contain the trace id of the run. Nothing goes out of the sandbox |
| `MAPI_LOG_JSON` | `1` (the default) makes the stderr diagnostics of the supervisor JSON lines. `0` makes plain text |

The supervisor gives the untrusted worker only an allow-list of variables
(`mapi_spec.run.WORKER_ENV_ALLOWLIST`: `PATH`, `HOME`, `TMPDIR`, locale and thread-count
variables, `NUMBA_CACHE_DIR`, `PYTENSOR_FLAGS`, `XLA_FLAGS`, …). The supervisor removes all
`MAPI_*` variables.

In the runner images, `MAPI_CACHE_SEED` (`/opt/mapi/cache`) is the read-only PyTensor and
Numba cache that the image build makes. `MAPI_CACHE_DIR` (`/tmp/mapi-cache`) is the writable
symlink farm that the entry point builds over it
([Container images](images.md#compile-caches-and-the-read-only-root-filesystem)).

## The GCS shim

`mapi-gcs-shim serve` gets its defaults from the topology: `MAPI_DATA_DIR` (state in
`$MAPI_DATA_DIR/gcs`), `MAPI_ENV` (bucket names), `MAPI_GCS_ENDPOINT` (host and port) and
`MAPI_WEB_ORIGIN` (the CORS origin). Each has a command-line flag. Refer to the
[GCS shim reference](../reference/gcs-shim.md). If you set `MAPI_BUNDLE_RETENTION_DAYS`, it
is also the locked retention of the bundles bucket in days when the shim makes its state.
`0` means no retention. If unset, the bucket is write-once, for about 100 years.

## The CLI and the Python client

| Variable | Meaning |
|---|---|
| `MAPI_URL` | The API. If unset, the client uses `MAPI_API_URL`, then the profile, then `http://127.0.0.1:8000` |
| `MAPI_TOKEN` | An API token (`mapi_…`), sent as `Authorization: Bearer`. It has priority over a stored `mapi login` sign-in. The client never sends it over plain HTTP to a host that is not loopback |
| `MAPI_PROJECT` | The project to act in, `<tenant>/<project>` (v0.3). The client sends it as `X-Mapi-Project` on project-scoped requests. It has priority over `mapi use` |
| `MAPI_PROFILE` | The profile of the config file to use (refer to the warning above) |
| `MAPI_CONFIG` | The config file (default `$XDG_CONFIG_HOME/mapi/config.toml`, if not set `~/.config/mapi/config.toml`) |
| `MAPI_CREDENTIALS` | The file where `mapi login` keeps sign-ins (default `credentials.json` next to the config file, mode `0600`) |
| `MAPI_OIDC_ISSUER` | The identity provider for `mapi login` (https, or plain http on a loopback address for a local development provider) |
| `MAPI_OIDC_CLIENT_ID` | The OIDC client id of the CLI (default `mapi-cli`) |
| `MAPI_OIDC_DISCOVERY_URL` | The location of the discovery document of the issuer, when it is not under the issuer. The built-in IdP of the `dev` profile has the issuer `https://dev-idp.mapi.localhost`. It serves its document at `<api>/dev/idp` |
| `MAPI_OIDC_CLIENT_SECRET` | Only for identity providers that need a secret from installed apps (for example Google "desktop" clients). The CLI never prints it |

Arguments (`--url`, `--token`, `--project`, `--profile`, or `Client(base_url=…, token=…,
project=…)`) have priority over the environment. The environment has priority over the file.
`mapi config show` prints the result and the source of each value. `mapi login`, `mapi use`
and `mapi config set` write the config file again atomically with mode `0600`.

## `make dev`

`scripts/dev.py` reads the topology variables and these variables:

| Variable | Meaning |
|---|---|
| `MAPI_STACK_PROFILE` | The `MAPI_PROFILE` of the servers. `dev` is the default: sign-in through the built-in development IdP, the `default` tenant and project, and the seeded accounts of `MAPI_DEVIDP_USERS`, with `admin@dev.local` as tenant admin and platform operator. The other values are `single-tenant` (the M1 API without credentials) or `multi-tenant`. The name is different, so a CLI `MAPI_PROFILE` in your shell never goes to the servers |
| `MAPI_INTERNAL_TOKEN` | Outside the single-tenant profile, the script makes a random bearer for `POST /internal/events` in `$MAPI_DATA_DIR/dev/internal-token`. It gives the bearer to the servers. The end-to-end suite reads it there |
| `MAPI_WEBHOOK_CA_FILE`, `MAPI_WEBHOOK_DEV_ALLOWLIST` | If you do not set them, the `dev` profile writes a temporary CA and a certificate for `127.0.0.1` and `localhost` to `$MAPI_DATA_DIR/dev/webhook-tls/` (`ca.pem`, `cert.pem`, `key.pem`). It trusts the CA and puts the two hosts on the allow-list. Thus webhooks can get to an HTTPS receiver on your machine (the receiver of the M3 end-to-end suite) |
| `MAPI_DEV_<NAME>_CMD` | Replace the command of one service (shell-quoted). `<NAME>` is `GCS_SHIM`, `MIGRATE`, `API`, `WORKER`, `WEB` or `PLOTS`, for example `MAPI_DEV_API_CMD="uvicorn mapi_server.app:app --reload --port 8000"` |
| `NO_COLOR`, `FORCE_COLOR` | Disable or force coloured service prefixes |

`make dev ONLY=api` and `make dev WITHOUT=web,plots` select services. `make seed SIZE=full`
loads the large demo. These are `make` arguments, not environment variables.

`make seed`, `make e2e` and `python -m mapi_conformance run --stack` sign in to a stack that
needs sign-in. If `MAPI_TOKEN` is set, they use it. If not, they sign in through the
development IdP as `MAPI_DEV_ACCOUNT` (default `admin@dev.local`). They act in `MAPI_PROJECT`
(default `default/default`).

## The web app and the plot sandbox

These are Vite build-time variables. `make dev` sets them from the topology. For a production
build, set them yourself.

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_BASE` | `http://127.0.0.1:8000` | The API that the app calls (it must permit the origin of the app) |
| `VITE_PLOTS_ORIGIN` | `http://127.0.0.1:5174` | The plot sandbox. It must be different from the app origin |
| `VITE_STORAGE_ORIGINS` | `http://127.0.0.1:4443` | The origins of signed storage URLs. The CSP `connect-src` of the app permits them |
| `VITE_APP_ORIGINS` (sandbox) | `http://127.0.0.1:5173,http://localhost:5173` | The origins that can frame the sandbox and send messages to it |
| `VITE_AUTH_BASE` | `VITE_API_BASE` | The location of the sign-in routes (`/auth/*`), when they are not on the API base |
| `VITE_TERMS_URL` | `/terms` | The terms of service that sign-up links to (an `http(s)` URL or a path on the origin of the app). The default is the plain-language summary in the app |
| `VITE_ACCEPTABLE_USE_URL` | `/acceptable-use` | The acceptable-use policy that sign-up links to, in the same way. A public installation must publish its own versions of the two documents |

The Playwright smoke suite of the web app serves the app on 5173 and the sandbox on 5174. If
other processes use these ports, set `MAPI_SMOKE_WEB_PORT` and `MAPI_SMOKE_PLOTS_PORT`. The
suite then builds the app for that sandbox origin. It exports the origin to the specs as
`MAPI_SMOKE_PLOTS_ORIGIN`.

The `index.html` of the sandbox has a `%MAPI_CSP%` placeholder. Its Vite build replaces the
placeholder with the Content-Security-Policy. It is not a variable that you set.

## Docker Compose

Compose reads `.env` in the repository root (copy `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_PASSWORD` | **necessary** | The database password. Compose does not start without it |
| `MAPI_VERSION` | the workspace version | The `mapi/*` image tag (`make compose-up` sets it) |
| `MAPI_ENV` | `local` | The same as above |
| `MAPI_STACK_PROFILE` | `dev` | The `MAPI_PROFILE` of the servers, the same as for `make dev` |
| `MAPI_EXECUTOR` | `local-process` | The executor of the worker |
| `MAPI_WORKER_TMPFS`, `MAPI_WORKER_MEMORY`, `MAPI_WORKER_CPUS` | `4g`, `12g`, `4` | The `/tmp` of the worker (the scratch of each local-process run), the memory limit and the CPUs |
| `DOCKER_GID` | `0` | With the docker executor override: the group that owns the Docker socket |

## The `mapi/web` image (nginx)

The container puts these variables into the nginx configuration when it starts. It replaces
only `MAPI_*` variables.

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_WEB_ORIGIN`, `MAPI_PLOTS_ORIGIN`, `MAPI_API_URL`, `MAPI_GCS_ENDPOINT` | the local defaults | The origins in the CSP and `frame-ancestors` headers |
| `MAPI_API_UPSTREAM` | `http://api:8000` | The location where nginx sends `/v1/` (a URL without a path) |
| `MAPI_WEB_SERVER_NAMES` | `127.0.0.1 localhost` | The host names that the app server answers (space-separated). All other `Host` values get no response. Keep it in step with `MAPI_ALLOWED_HOSTS` |
| `MAPI_WEB_SCRIPT_SRC_EXTRA` | empty | Extra `script-src` sources for the app. A wider CSP needs a review |
| `NGINX_ENVSUBST_TEMPLATE_DIR` | `/etc/nginx/templates/all` | `…/web` or `…/plots` serves only one of the two apps |

## Image build arguments

Use `docker build --build-arg NAME=value`. The Makefile gives `VERSION`, `VCS_REF` and
`BUILD_DATE`. The defaults define each image. Change them only to build a variant.

| Argument | Meaning |
|---|---|
| `MAPI_PACKAGES` | The workspace packages that the build installs into the image (`mapi-runner mapi-steps mapi-pymc …`) |
| `MAPI_WORKER_PACKAGES` | The packages that the `worker` target adds to `base-bayes` (`mapi-plugin-ts mapi-server[docker]`) |
| `MAPI_NO_INSTALL` | The distributions that the build never installs (the 300 MB `nvidia-nccl-cu13`) |
| `MAPI_APT_PACKAGES` | The system packages of the runtime stage (`tini`, and `g++` for PyTensor) |
| `MAPI_ASSERT_PRESENT`, `MAPI_ASSERT_ABSENT` | The distributions that must be installed, and that must not be installed. If not, the build fails |
| `MAPI_IMAGE_NAME`, `MAPI_DESCRIPTION` | OCI labels, and the name that the catalog export records |
| `PYTHON_IMAGE` (and the other base images) | Base images, pinned by tag *and* digest (`make docker-pin`) |

## Deploying to Google Cloud

The deploy scripts under `deploy/gcp/` get their settings as flags (`--help` on each). One
variable changes the location of their local files:

| Variable | Meaning |
|---|---|
| `MAPI_DEPLOY_OUT` | The local scratch directory of `deploy/gcp/*.sh`: images files, saved plans and Terraform outputs. The default is `deploy/gcp/.out`, with one directory for each environment. Never commit it |
| `MAPI_PUSHGATEWAY_URL` | A repository secret of the nightly workflow: the Pushgateway that gets `mapi_security_probe_unexpected` from the security jobs. If unset, the jobs do not push (`ops/README.md`) |

[Deploy to Google Cloud](gcp-deployment.md#environment-of-the-cloud-run-workloads) lists the
variables that Terraform sets on the Cloud Run services (the deployment contract with
`mapi-server`).

## Tests

| Variable | Meaning |
|---|---|
| `MAPI_E2E` | `pnpm --filter @mapi/web e2e` sets it: run the Playwright `@e2e` suite against a stack that runs |
| `MAPI_DEV_ACCOUNT` | The development-IdP account that `make seed` and the end-to-end suites sign in as (default `admin@dev.local`) |
| `MAPI_E2E_ACCOUNTS` | The two development-IdP accounts that the M2 tenancy suite makes tenant admins (default `analyst@dev.local,viewer@dev.local`) |
| `MAPI_E2E_UI_ACCOUNT` | The tenant admin of the M2 browser flow and the fit burst (default `analyst@dev.local`) |
| `MAPI_E2E_BURST_RUNS` | Opt-in: the number of concurrent real fits that `tests/e2e/test_m2_burst_stack.py` submits ([M2 verification](m2-verification.md)) |
| `MAPI_E2E_SIGNUP_ACCOUNT` | The address that the M3 trust-lifecycle suite (`tests/e2e/test_m3_trust_stack.py`) uses to sign up. It must be a development-IdP account, so that it can sign in (default `viewer@dev.local`) |
| `MAPI_E2E_GUIDED_ACCOUNT` | The address that the M3 journey (`tests/e2e/test_m3_guided_stack.py`) uses to sign up. It is a development-IdP account (default `viewer@dev.local`) |
| `MAPI_E2E_KEEP` | `1` keeps the project that the M3.5 journeys make (`tests/e2e/test_m35_stack.py`, `apps/web/e2e/m35-live.e2e.spec.ts`). It also keeps the project of the forecasts-with-actuals journey (`tests/e2e/test_forecast_actuals_stack.py`). Thus you can look at the project in the app ([M3.5 verification](m35-verification.md)) |
| `MAPI_E2E_WEBHOOK_TLS_DIR` | The location where the webhook receiver of the M3 journey finds `cert.pem` and `key.pem` (default `$MAPI_DATA_DIR/dev/webhook-tls`) |
| `MAPI_UPDATE_SNAPSHOTS` | `1` makes the contract snapshot of `mapi_spec` and the Guided catalog fixture of the server tests again (`make snapshots`) |
| `MAPI_HYPOTHESIS_EXAMPLES` | The examples for each property in the wire-format round-trip tests (default 60) |
| `MAPI_PYMC_CHALLENGES_DATA` | The `data/` directory of a local copy of pymc-challenges. The `from_challenges` loaders of `mapi-plugin-science` read the real data files there, and the tests that use them run. If it is not set, these tests are skipped |
| `MAPI_E21_DRAWS` | The draws for each chain in the real-data sociality fit of `sci.network_latent_space` (`plugins/science/tests/test_network.py`, default 300) |

## Documentation

| Variable | Meaning |
|---|---|
| `MAPI_DOCS_SPEC` | The spec that `scripts/docs_screenshots.mjs` (`make docs-screenshots`) opens and fits, default `eu-geo-mmm`. The script also reads `MAPI_WEB_ORIGIN` |
| `MAPI_DOCS_PROJECT` | The project that `scripts/docs_screenshots_admin.mjs` (`make docs-screenshots-admin`) selects after it captures the project switcher, default `Default project`. The script also reads `MAPI_WEB_ORIGIN`, `MAPI_API_URL` and `MAPI_DEV_ACCOUNT` |
| `MAPI_SCIENCE_DATA` | The folder of the three exported datasets (`scotland_lip.nc`, `heat_plate_e51.nc` and `lazega_coworkers.nc`) that `scripts/docs_screenshots_science.mjs` (`make docs-screenshots-science`) uploads, default `.local/science`. The script also reads `MAPI_WEB_ORIGIN`, `MAPI_API_URL` and `MAPI_DEV_ACCOUNT` |
| `MAPI_GUIDED_SCREENSHOTS` | Set to `1` to run `apps/web/e2e/guided-screenshots.spec.ts`. This spec captures the screenshots of the Guided tutorial from the app in mock mode. `make docs-screenshots-guided` sets it, with `MAPI_SMOKE_WEB_PORT`/`MAPI_SMOKE_PLOTS_PORT` at the defaults 15173/15174. If not set, the smoke suite skips it |
| `MAPI_EXPLORER_SCREENSHOTS` | Set to `1` to run `apps/web/e2e/explorer-screenshots.spec.ts`. This spec captures the explorer, About-card and budget-plan screenshots of the docs from the app in mock mode. `make docs-screenshots-explorers` sets it, with the same port defaults. If not set, the smoke suite skips it |
| `MAPI_WORKFLOW_SCREENSHOTS` | Set to `1` to run `apps/web/e2e/workflow-screenshots.spec.ts`. This spec captures the screenshots of the docs for workflows (gallery, editor, run monitor, schedules, Guided recipe) and governance (policies, policy editor, review queue, validation, evidence), from the app in mock mode. `make docs-screenshots-workflows` sets it, with `MAPI_SMOKE_WEB_PORT`/`MAPI_SMOKE_PLOTS_PORT` at the defaults 15183/15184. If not set, the smoke suite skips it |
| `MAPI_SHOTS_DIR` | A directory. When set, the workflow and governance smoke specs (`apps/web/e2e/workflows.spec.ts`, `governance.spec.ts`) also save full-page screenshots of their screens there |
| `MAPI_EMBED_VIEW` | The `EmbedView` JSON that `scripts/docs_screenshots_tutorials.mjs` (`make docs-screenshots-tutorials`) serves to the embed page of the plot sandbox. `scripts/docs_embed_view.py` writes it (default `.local/embed-view.json`). The script also reads `MAPI_WEB_ORIGIN` and `MAPI_PLOTS_ORIGIN` (default ports 15173/15174) |
| `MAPI_EMBED_API` | The API origin where the embed page asks for the view in that script. Playwright answers (default `http://127.0.0.1:8000`, the `VITE_API_URL` of the sandbox dev server). No process must listen there |
