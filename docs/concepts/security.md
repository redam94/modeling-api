---
description: What modeling-api protects against, which controls are enforced on a laptop today, and what production adds in later releases.
---

# Security model <span class="mapi-badge">v0.2</span>

modeling-api is built to run **models and data from people it does not trust**, eventually
from the public internet. v0.2 is a single-tenant, local slice of that platform, but the
controls that do not depend on the cloud are already in the code and tested, so the design is
exercised from the first release. This page says what each control protects against and how
much of it you get in each setting.

!!! danger "v0.2 has no authentication"
    The API, the web app and the object store listen on `127.0.0.1`, and Docker Compose
    publishes its ports on `127.0.0.1` only. Anyone who can reach those ports can do anything.
    Do not expose a v0.2 stack to a network you do not trust, and do not run untrusted plugin
    code on it: local and compose runs are not a multi-tenant boundary.

## What it protects

| Asset | Threat | Main controls |
|---|---|---|
| The control plane | A hostile dataset, bundle or plot exploits a parser in the API | The API parses JSON and YAML only (a strict loader: no anchors or tags, size and depth caps). Arrays, zarr, netCDF, CSV and Parquet are opened only inside a run's sandbox |
| Credentials and other runs | Model code reads or steals what the run holds | Supervisor/worker split; no secrets in the environment or arguments; a one-time bootstrap object; object-scoped, short-lived signed URLs |
| Results | A run forges outputs, swaps an object later, or fakes progress | Outputs are claims checked against the store's own checksums and generations; write-once bundles; signed bundle manifests checked before every download link; HMAC-signed events |
| The browser and the user's data | A malicious plot spec runs script in the app | A server-side sanitizer, then rendering only in a sandboxed iframe on a separate origin, with strict CSPs on both origins |
| The user's terminal and screen | Log lines or messages with escape codes, HTML or bidi tricks | Untrusted text is a separate field (`untrusted_text`), stripped of ANSI, control and bidi characters, and rendered as text only |
| Long-term trust in a model | Pickle and other code-executing formats | Bundles are zarr zips plus JSON; native models are skops (allow-listed types) or xgboost JSON; pickle is refused on write and on read |

## Local development vs production

| Control | `make dev` (v0.2) | Docker Compose (v0.2) | Production (v0.3 and later) |
|---|---|---|---|
| Authentication | None; `127.0.0.1` only | None; ports published on `127.0.0.1` only | OIDC for people, scoped API tokens with budgets for programs |
| Tenancy | One tenant and project (`default`); every row and storage key already carries them | Same | Multiple tenants; PostgreSQL row-level security on every table |
| Run isolation | A separate worker process with a scrubbed environment, **as your own user** (`DEV_MODE_ISOLATION`) | `local-process`: as above, inside a hardened container. `docker` executor: one container per run, read-only root, `no-new-privileges`, the supervisor with only the capabilities the uid split needs and the worker as uid 10001 with none (`MAPI_REQUIRE_ISOLATION`) | A fresh Cloud Run Jobs microVM per run; the worker under its own uid with `no_new_privs`; read-only root |
| Network egress from runs | Not restricted | `docker` executor: the GCS shim's network namespace, or an internal network containing only the shim | Deny-all egress, including DNS, inside a VPC Service Controls perimeter |
| Object storage permissions | The GCS shim enforces per-principal permissions: runs' credentials can only get and delete their bootstrap object | Same | Cloud Storage IAM; the same layout and principals |
| Bundle signatures | EC P-256 with a local key in `.local/keys/` | EC P-256 with one key on the `keys` volume, created by `migrate` and mounted read-only into `api` and `worker` | Cloud KMS, same signed bytes |
| Event integrity | HMAC per event, read from the supervisor's stdout pipe | Same | HMAC per event, through Cloud Logging and Pub/Sub with OIDC push |
| Web app CSP | The Vite **dev server does not send the app's CSP** (hot reload needs inline scripts); the plot sandbox's CSP is enforced | Enforced by nginx, with Trusted Types in report-only mode | Enforced |
| Plot isolation | Separate origin (port 5174), `sandbox="allow-scripts"` | Separate origin (port 5174) | Separate domain (`plots.<domain>`) |
| Quotas and cost limits | None | Worker memory, CPU and `/tmp` limits | Per-tenant and per-token vCPU-second quotas, priority classes, a global kill switch |

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

!!! note "Known gaps in v0.2"
    - The API's built-in `/docs` and `/redoc` pages (FastAPI's defaults) load Swagger UI and
      Redoc from a public CDN. Use the [OpenAPI explorer](../reference/openapi.md) or
      `/openapi.json` instead.
    - The app's CSP is not enforced by the Vite dev server (see the table).
    - Local and compose runs have no egress restriction unless you use the `docker` executor.
    - Under the `local-process` executor, run code runs as the orchestrator's own uid, so it can
      read the shim's control key and the bundle-signing key. The orchestrator is non-dumpable
      and logs a warning at start-up, but run only trusted plugin code in this mode; see
      [Docker Compose](../operations/docker.md#executor-choice-local-process-vs-docker).

## What production adds

[v0.3](../roadmap/cloud.md) moves runs to Cloud Run Jobs: a fresh microVM per run, a separate
execution project with deny-all egress (DNS included) and VPC Service Controls, a bootstrap
bucket the execution service account can only get from and delete in, Cloud KMS signatures,
OIDC, scoped tokens, row-level security, quotas and a kill switch.
[Public code upload](../roadmap/index.md#after-v04) comes only after a dedicated overlay
build pipeline, abuse controls and an external penetration test.
