---
description: What modeling-api protects against, which controls each executor isolation level enforces, the threat model in plain terms, and what is verified, what is not yet, and what comes later.
---

# Security model <span class="mapi-badge">v0.4</span>

modeling-api is built to run **models and data from people it does not trust**, from many
organisations on one deployment. Every control that does not need a cloud is in the code and
tested on a laptop; the cloud controls are written (Terraform, the Cloud Run Jobs executor,
Cloud KMS, authenticated Pub/Sub) and tested against fakes, and a short list of cloud
behaviours still waits for a real project. This page says what each control protects against,
which ones a given executor enforces, and where the honest limits are today.

!!! danger "A local stack is not a security boundary"
    `make dev` and Docker Compose run the `dev` profile: anyone who can reach the API can sign
    in as any seeded account through the built-in development identity provider. Every port
    listens on `127.0.0.1` only. Do not expose a local stack to a network you do not trust, and
    do not run untrusted plugin code on it: the `local-process` and plain `docker` executors
    are process isolation, not a multi-tenant boundary.

## What it protects

| Asset | Threat | Main controls |
|---|---|---|
| Each tenant's data | Another tenant, or a bug, reads or changes it | PostgreSQL row-level security pinned per transaction and failing closed; storage keys built by the server from ids; roles checked at every request; operators never read tenant content |
| The control plane | A hostile dataset, bundle or plot exploits a parser in the API | The API parses JSON and YAML only (a strict loader: no anchors or tags, size and depth caps). Arrays, zarr, netCDF, CSV and Parquet are opened only inside a run's sandbox |
| Credentials and other runs | Model code reads or steals what the run holds | A fresh sandbox per run; supervisor/worker split; no secrets in the environment or arguments; a one-time bootstrap object; object-scoped, short-lived signed URLs |
| Results | A run forges outputs, swaps an object later, or fakes progress | Outputs are claims checked against the store's own checksums and generations; write-once bundles; signed bundle manifests checked before every download link; HMAC-signed events |
| The bill | A leaked token, a runaway sweep or abuse runs up compute | vCPU-second quotas per token, tenant and platform; fair-share scheduling; a global spend cap and a kill switch for uploaded code |
| The browser and the user's data | A malicious plot spec runs script in the app | A server-side sanitizer, then rendering only in a sandboxed iframe on a separate origin, with strict CSPs on both origins; sessions in `HttpOnly`, `SameSite=Strict` cookies with CSRF tokens |
| The user's terminal and screen | Log lines or messages with escape codes, HTML or bidi tricks | Untrusted text is a separate field (`untrusted_text`), stripped of ANSI, control and bidi characters, and rendered as text only |
| Long-term trust in a model | Pickle and other code-executing formats | Bundles are zarr zips plus JSON; native models are skops (allow-listed types) or xgboost JSON; pickle is refused on write and on read |

## Threats, in plain terms

The plan's threat model, as a user would ask about it. **Enforced** means in the code and
covered by tests on every change; **[verify M0]** means the control is built but depends on a
cloud behaviour not yet confirmed against a real Cloud Run Jobs execution (see
[the checklist](../operations/gcp-deployment.md#before-you-start-the-verify-m0-checklist)).

| If someone tries to… | What stops them | In v0.4 |
|---|---|---|
| Break out of the sandbox into the machine | A fresh microVM per run; the model code runs as its own non-root user with `no_new_privs`, on a read-only root filesystem | Built for Cloud Run Jobs; the uid split there is **[verify M0]**. Local executors are not boundaries |
| Steal the run's credentials | Nothing secret in the environment or arguments; the run's instructions are read from a one-time object and deleted before model code starts; the only credential can get and delete that object, nothing else | **Enforced** (locally the storage emulator enforces the same permissions) |
| Send your data out over the network | Deny-all egress, only Google's restricted APIs address, a VPC Service Controls perimeter | Terraform written and tested offline; **[verify M0]** |
| Send your data out over DNS | A DNS response policy that answers nothing outside Google APIs | **[verify M0]**. Until it passes, `verified` and `public` tenants are not placed on Cloud Run at all |
| Crash or take over the API with a crafted file | The API only parses JSON and YAML; everything else is opened inside a sandbox | **Enforced** |
| Swap or replay a model's files | Outputs checked against the store's own hashes and generations, copied where runs cannot write, and signed; the signature is checked before every download link | **Enforced** (Cloud KMS signing tested against a fake) |
| Fake progress or results | Only the supervisor writes events, each signed and numbered; the executor's own status decides whether a run is alive | **Enforced** |
| Read another organisation's work | Row-level security on every table, failing closed; ids checked against the caller's tenant; a test reuses pooled connections across two tenants | **Enforced** |
| Use a leaked token | Tokens are scoped, bound to one project, expire, can carry rate limits and budgets, are revoked at once, and never carry operator powers; failed uses are audited | **Enforced** |
| Run up a huge bill, or starve everyone else | Quotas in vCPU-seconds per token, tenant and platform; interactive work ahead of batch; a global spend cap; a switch that stops all uploaded code | **Enforced** |
| Run script in your browser through a plot | A sanitizer, then a sandboxed iframe on another origin with a strict CSP | **Enforced** |
| Hijack the runner with a malicious package | The supervisor runs in its own environment (`python -I -S`); package uploads are checked for `.pth` and `sitecustomize` | Supervisor isolation **enforced**; uploaded packages arrive with the overlay pipeline (v0.6) |
| Slip something into the supply chain | Hash-locked Python, digest-pinned images, a frozen JavaScript lockfile without install scripts, keyless CI identities | **Enforced** in the builds; signed-image enforcement (Binary Authorization) comes with public code upload |
| Turn a webhook against your network (SSRF) | Static URL checks, one DNS resolution with every address checked, a connection pinned to the checked address, no redirects, time and size caps; private, loopback and link-local addresses and odd ports refused | **Enforced** (to public hosts covered by unit tests; the development allow-list is refused in the `multi-tenant` profile) |
| Forge or replay a webhook delivery to your receiver | An HMAC-SHA256 signature over a timestamp and the raw body under a per-webhook secret shown once and stored sealed; receivers refuse stale timestamps; envelopes carry ids and states, never data, links or secrets | **Enforced** (`mapi_client.webhooks.verify`) |
| Use an embed link to see more than one chart | A tokenised link to one saved plot of a platform recipe, stored as a keyed hash; the public view reads no credential, inlines only that plot's data, is rate-limited per link and per address, and stops on revocation or expiry | **Enforced** |
| Sign up many workspaces to farm free compute | E-mail verification, disposable domains refused, per-address, per-domain and platform-wide sign-up limits, Sybil flags reviewed by operators; `public` workspaces run built-ins and declarative specs only (cut line A), and only on executors that enforce egress and DNS | **Enforced**; vendor identity verification and a public run on such an executor await a deployment |
| Keep a tenant's data after it leaves | Tenant deletion purges every table and object prefix after a grace period and notifies by signed webhook; a GDPR export with credentials redacted | **Enforced**; only the pseudonymised audit log and the cost ledger are retained |
| Misuse the MCP server | Curated tools with delimited untrusted text | Not applicable yet: the MCP server comes in v0.6 |

The probes that check these rows as hostile code, executor by executor, are the
[security suite](../operations/security-suite.md).

## What each executor enforces

Each executor declares its **isolation level** and whether it enforces egress and DNS. The
dispatcher compares that with the tenant's [trust tier](../guides/administration.md#trust-tiers)
before every run, in code: no tenant setting can lower the bar.

| Executor | Isolation | Egress / DNS enforced | Tenants it may run | Security suite must show |
|---|---|---|---|---|
| `inproc` | `NONE` | No | Your own notebook only (never through the API) | — |
| `local-process` | `PROCESS` | No | `internal`, `self-hosted-trusted`. Refuses to start under `MAPI_PROFILE=multi-tenant` | Platform probes BLOCKED; sandbox probes may be ALLOWED |
| `docker` (runc) | `PROCESS` | Only with `MAPI_DOCKER_NETWORK=none` | `internal`, `self-hosted-trusted` | As above |
| `docker-gvisor` | `GVISOR` when the supervisor can drop the worker to its own uid, else `PROCESS` | Only with `MAPI_DOCKER_NETWORK=none` (needs an HTTPS bootstrap URL, so not with the local shim) | Every tier once egress and DNS are enforced | Every probe BLOCKED |
| `cloudrun-jobs` | `MICROVM` | Egress yes (`MAPI_CLOUDRUN_EGRESS_ENFORCED`); DNS only once `MAPI_CLOUDRUN_DNS_ENFORCED=true`, after the M0 DNS check | `internal`, `self-hosted-trusted`; `verified` and `public` once DNS is enforced | Every probe BLOCKED, run inside an execution (not wired up yet) |

What every executor shares, whatever its isolation: the supervisor/worker protocol, the
scrubbed worker environment, capped logs and outputs, HMAC-signed events, and verification of
every output by the control plane. What only the stronger levels add is a boundary between the
model code and the machine it runs on.

## Local development vs production

| Control | `make dev` | Docker Compose | Google Cloud (v0.3) |
|---|---|---|---|
| Authentication | The development IdP: any seeded account, no password; `127.0.0.1` only | Same; ports published on `127.0.0.1` only | OIDC for people (Google or another provider), scoped API tokens with budgets for programs |
| Tenancy | Multiple tenants and row-level security, exactly as in production | Same | Same, on Cloud SQL with separate database logins per role |
| Run isolation | A separate worker process with a scrubbed environment, **as your own user** (`DEV_MODE_ISOLATION`) | `local-process`: as above, inside a hardened container. `docker` executor: one container per run, read-only root, `no-new-privileges`, the supervisor with only the capabilities the uid split needs and the worker as uid 10001 with none (`MAPI_REQUIRE_ISOLATION`) | A fresh Cloud Run Jobs microVM per run; the worker under its own uid with `no_new_privs`; read-only root |
| Network egress from runs | Not restricted | `docker` executor: the GCS shim's network namespace, or an internal network containing only the shim | Deny-all egress, including DNS, inside a VPC Service Controls perimeter |
| Object storage permissions | The GCS shim enforces per-principal permissions: runs' credentials can only get and delete their bootstrap object | Same | Cloud Storage IAM; the same layout and principals |
| Bundle signatures | EC P-256 with a local key in `.local/keys/` | EC P-256 with one key on the `keys` volume, created by `migrate` and mounted read-only into `api` and `worker` | Cloud KMS, same signed bytes |
| Event integrity | HMAC per event, read from the supervisor's stdout pipe | Same | HMAC per event, through Cloud Logging and Pub/Sub push with an OIDC token checked by the events service |
| Web app CSP | The Vite **dev server does not send the app's CSP** (hot reload needs inline scripts); the plot sandbox's CSP is enforced | Enforced by nginx, with Trusted Types in report-only mode | Enforced |
| Plot isolation | Separate origin (port 5174), `sandbox="allow-scripts"` | Separate origin (port 5174) | Separate domain (`plots.<domain>`) |
| Quotas and cost limits | Tier defaults, quotas, the cost ledger and kill switches all apply | Same, plus worker memory, CPU and `/tmp` limits | Same, priced with the deployment's price table |

## How the controls work

### The run sandbox

Every run is a pair of processes started by an executor that passes **only** the run id and a
256-bit nonce. The trusted supervisor uses the nonce to fetch its instructions (signed URLs for
its inputs and outputs, an event HMAC key, limits) from a one-time bootstrap object, and
**deletes it before any model code starts**; the storage credential it used can get and
delete bootstrap objects and do nothing else, not even list them. The worker that runs the SDK
and the plugin gets an allow-listed environment with every `MAPI_*` variable removed, its own
session, and (where the supervisor starts as root) a separate uid with `no_new_privs` and no
core dumps. It can only talk to the supervisor, in schema-checked frames.

On a laptop the supervisor is not root, so the worker runs as you: the run reports
`DEV_MODE_ISOLATION`. Everything else (the split, the scrubbed environment, signed events,
caps, output verification) still applies, but the boundary between your account and the
model code is only as strong as Python's process separation. Treat local runs like running
the plugin in a notebook.

### Outputs are claims

A run can only write under its own attempt prefix, through a POST policy with a size range.
When it finishes, the control plane does not read its outputs; it lists them and compares the
**size, CRC32C, MD5 and generation the store computed** with the run's signed manifest. Any
missing, changed or extra object fails the run with `INTEGRITY_FAILURE` and raises an alert.
Verified objects are copied server-side into a write-once bundles bucket the runs cannot
reach, and a bundle manifest binding the tenant, run, hashes and every destination generation
is signed. **Download links are minted only after that signature verifies**, and each link is
pinned to an object generation and expires after 15 minutes. Deleting the signing key
(`.local/keys/`) therefore makes existing model versions undownloadable, by design.

### Events and liveness

Only the supervisor writes the real stdout. Each event line carries a sequence number and an
HMAC under the run's key; the control plane verifies it in constant time, deduplicates on
`(run, attempt, seq)` and drops forged lines (counting them as sandbox violations). Events
never decide whether a run is alive: the executor's own status does, so a run cannot keep
itself "running" by talking, or get itself killed by going quiet.

### Plots

Plot specs come from recipes, plugins and, later, users, and Vega and Plotly are large
JavaScript libraries. So:

1. The server sanitizes every spec when a run finishes and again on every read: pinned
   Vega-Lite and Plotly schemas, no URL data, no `href` encodings, no external images, no
   tile maps, no prototype keys, Plotly HTML reduced to `b`, `i`, `br`, `sub`, `sup`, and size
   and depth caps.
2. Every plot renders in an `<iframe sandbox="allow-scripts">` (no `allow-same-origin`) from
   the plot sandbox's origin, with `default-src 'none'`, scripts from its own origin only,
   `connect-src` limited to its own topojson, and `frame-ancestors` set to the app.
3. Vega runs with the expression interpreter (no `eval`), a loader that rejects every URL and
   a text-only tooltip; Plotly uses its strict bundle.
4. The app and the sandbox exchange a small, validated `postMessage` protocol; the app accepts
   messages only from that iframe's window with an opaque origin.

The remaining risk (a sandboxed frame could navigate itself and leak the data posted into it)
is accepted only for a viewer's own data; shared and embedded plots (v0.4) render platform
recipes only.

### Untrusted text

Log lines, tracebacks and plugin messages travel in `untrusted_text` or `LogLine.line`, never
in `detail`. The runner strips ANSI, control and bidi characters; the web app renders them as
text (ESLint forbids `dangerouslySetInnerHTML`); Monaco hovers are untrusted; the CLI strips
escape sequences before printing.

### Supply chain

- Python dependencies are locked with hashes in `uv.lock`; container images are built from the
  lock with `--frozen`, and base images are pinned by digest (`make docker-pin-check`).
- Build-time guards fail an image that contains what it must not: no numpy, xarray, PyMC,
  SDK or plugin in the control-plane image, and the import-linter contracts run against the
  installed packages inside the server image build.
- The web image installs JavaScript dependencies with `--frozen-lockfile --ignore-scripts`.
- This documentation site's OpenAPI explorer loads a vendored, hash-pinned Redoc bundle; it
  makes no request outside the site.

## Known gaps in v0.4

- **Cloud behaviours not yet verified.** Everything marked [verify M0] above, chiefly that the
  DNS response policy applies to Cloud Run Jobs. Until it is confirmed, the executor declares
  DNS as not enforced and shared tiers are not placed on it.
- **The nightly security suite does not run in the cloud yet.** The probes run locally and
  against gVisor; the base images do not ship them and Terraform defines no probe Job
  ([Security suite](../operations/security-suite.md#where-the-probes-run)).
- **Executor conformance reports are not stored**, so the admin console shows every executor
  as *conformance not passed* and enabling an executor is not gated on a stored report
  ([M2 verification](../operations/m2-verification.md#known-gaps-not-cloud-specific)).
  Placement by trust tier is enforced regardless.
- **Identity Platform's own tokens** are not accepted by the browser sign-in yet; use Google
  or another OpenID provider ([Authentication](../guides/authentication.md#identity-platform)).
- **Public workspaces need an enforcing executor.** Sign-up creates `public` workspaces, which
  run only on `docker-gvisor` or on Cloud Run Jobs once its DNS enforcement is verified; a
  deployment with neither cannot run their work. Neither ran in the M3
  verification ([what remains](../operations/m3-verification.md#what-remains-unverified)).
- **Identity verification has no vendor integration yet**: operators verify tenants by hand
  (audited), behind the `IdentityVerifier` protocol.
- **Webhook deliveries to the internet** were exercised only through the development
  allow-list; the SSRF policy for public hosts is covered by unit tests.
- The API's built-in `/docs` and `/redoc` pages (FastAPI's defaults) load Swagger UI and Redoc
  from a public CDN. Use the [OpenAPI explorer](../reference/openapi.md) or `/openapi.json`
  instead.
- The app's CSP is not enforced by the Vite dev server (see the table).
- Under the `local-process` executor, run code runs as the orchestrator's own uid, so it can
  read the shim's control key and the bundle-signing key. The orchestrator is non-dumpable and
  logs a warning at start-up, but run only trusted plugin code in this mode; see
  [Docker Compose](../operations/docker.md#executor-choice-local-process-vs-docker).

## What comes next

Plugin upload for allow-listed teams comes in [v0.6](../roadmap/expert-plugins.md), built into
hash-pinned overlays and checked by the contract kit on upload.
[Public code upload](../roadmap/index.md#after-v06) (every tenant uploading their own plugins) comes
only after a dedicated overlay build pipeline with a curated package mirror, a separate
execution project for public tenants, Binary Authorization on base images, abuse controls
(verification before any code runs), a rehearsed kill-switch drill and an external penetration
test, with the security suite green in the cloud for two weeks.
