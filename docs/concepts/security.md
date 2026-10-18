---
description: What modeling-api protects against, which controls each executor isolation level enforces, the threat model in plain terms, and what is verified, what is not yet, and what comes later.
---

# Security model <span class="mapi-badge">v0.4</span>

modeling-api can run **models and data from persons that it does not trust**, from many
organisations on one deployment. All controls that do not need a cloud are in the code, and we
test them on a laptop. The cloud controls are also written: Terraform, the Cloud Run Jobs
executor, Cloud KMS and authenticated Pub/Sub. We test them against fakes. A short list of
cloud behaviours waits for a real project. This page tells what each control protects
against, which controls each executor enforces, and the limits that apply today.

!!! danger "A local stack is not a security boundary"
    `make dev` and Docker Compose run the `dev` profile. Any person who can get access to the
    API can sign in as any seeded account through the built-in development identity provider.
    All ports listen on `127.0.0.1` only. Do not make a local stack available on a network that
    you do not trust. Do not run untrusted plugin code on a local stack. The `local-process`
    and plain `docker` executors give process isolation, not a multi-tenant boundary.

## What it protects

| Asset | Threat | Main controls |
|---|---|---|
| The data of each tenant | Another tenant, or a bug, reads or changes the data | PostgreSQL row-level security, pinned for each transaction, that fails closed. The server makes the storage keys from ids. The server checks roles at each request. Operators never read tenant content. |
| The control plane | A hostile dataset, bundle or plot uses a parser in the API to attack it | The API parses JSON and YAML only, with a strict loader: no anchors or tags, and limits on size and depth. Only the sandbox of a run opens arrays, zarr, netCDF, CSV and Parquet. |
| Credentials and other runs | Model code reads or steals the items that the run holds | A new sandbox for each run. A supervisor/worker split. No secrets in the environment or the arguments. A one-time bootstrap object. Short-lived signed URLs for one object each. |
| Results | A run forges outputs, replaces an object later, or fakes progress | The control plane compares the output claims with the checksums and generations of the store. Write-once bundles. Signed bundle manifests that the platform checks before each download link. HMAC-signed events. |
| The bill | A leaked token, a runaway sweep or abuse uses too much compute | vCPU-second quotas for each token, tenant and platform. Fair-share scheduling. A global spend cap and a kill switch for uploaded code. |
| The browser and the data of the user | A malicious plot spec runs script in the app | A server-side sanitizer. Then the plot renders only in a sandboxed iframe on a different origin, with strict CSPs on the two origins. Sessions in `HttpOnly`, `SameSite=Strict` cookies with CSRF tokens. |
| The terminal and the screen of the user | Log lines or messages with escape codes, HTML or bidi tricks | Untrusted text is in a separate field (`untrusted_text`). The platform removes ANSI, control and bidi characters from it and shows it as text only. |
| Long-term trust in a model | Pickle and other formats that execute code | Bundles are zarr zips plus JSON. Native models are skops (allow-listed types) or xgboost JSON. The platform refuses pickle on write and on read. |

## Threats, in plain terms

This table gives the threat model of the plan as a user asks about it. **Enforced** means
that the control is in the code and that tests cover it on each change. **[verify M0]** means
that the control is built but depends on a cloud behaviour. We did not yet confirm this
behaviour against a real Cloud Run Jobs execution (see
[the checklist](../operations/gcp-deployment.md#before-you-start-the-verify-m0-checklist)).

| If a person tries to… | What stops them | In v0.4 |
|---|---|---|
| Break out of the sandbox into the machine | A new microVM for each run. The model code runs as its own non-root user with `no_new_privs`, on a read-only root filesystem. | Built for Cloud Run Jobs. The uid split there is **[verify M0]**. Local executors are not boundaries. |
| Steal the credentials of the run | No secret in the environment or the arguments. The run reads its instructions from a one-time object. The supervisor deletes the object before model code starts. The only credential can get and delete that object, and it can do nothing else. | **Enforced**. Locally, the storage emulator enforces the same permissions. |
| Send your data out over the network | Deny-all egress, only the address of the Google restricted APIs, and a VPC Service Controls perimeter | Terraform is written and tested offline. **[verify M0]** |
| Send your data out over DNS | A DNS response policy that answers nothing outside Google APIs | **[verify M0]**. Until it passes, the platform does not put `verified` and `public` tenants on Cloud Run. |
| Crash or take control of the API with a crafted file | The API parses only JSON and YAML. A sandbox opens all other files. | **Enforced** |
| Make the sandbox read other files or load code with a crafted HDF5 file (v0.8) | The [HDF5 guard](#uploads-and-the-hdf5-guard) refuses links to other files, external storage, virtual datasets and filter plugins before a reader opens the file. | **Enforced** |
| Replace or replay the files of a model | The control plane compares the outputs with the hashes and generations of the store. It copies them to a location where runs cannot write, and signs them. It checks the signature before each download link. | **Enforced**. We tested Cloud KMS signing against a fake. |
| Fake progress or results | Only the supervisor writes events. It signs and numbers each event. The status from the executor decides if a run is alive. | **Enforced** |
| Read the work of another organisation | Row-level security on all tables, which fails closed. The server checks ids against the tenant of the caller. A test uses the same pooled connections for two tenants. | **Enforced** |
| Use a leaked token | Tokens have a scope and one project. They expire and can have rate limits and budgets. You can revoke them immediately. They never have operator powers. The platform audits failed uses. | **Enforced** |
| Use too much compute, or stop the work of all other users | Quotas in vCPU-seconds for each token, tenant and platform. Interactive work goes before batch work. A global spend cap. A switch that stops all uploaded code. | **Enforced** |
| Run script in your browser through a plot | A sanitizer, then a sandboxed iframe on a different origin with a strict CSP | **Enforced** |
| Take control of the runner with a malicious package | The supervisor runs in its own environment (`python -I -S`). The platform checks package uploads for `.pth` and `sitecustomize`. | Supervisor isolation is **enforced**. Uploaded packages come with the overlay pipeline (v0.6). |
| Put a hostile item into the supply chain | Hash-locked Python, digest-pinned images, a frozen JavaScript lockfile without install scripts, and keyless CI identities | **Enforced** in the builds. Enforcement of signed images (Binary Authorization) comes with public code upload. |
| Use a webhook to attack your network (SSRF) | Static URL checks. One DNS resolution, with a check of each address. A connection pinned to the checked address. No redirects. Limits on time and size. The platform refuses private, loopback and link-local addresses and unusual ports. | **Enforced**. Unit tests cover the delivery to public hosts. The `multi-tenant` profile refuses the development allow-list. |
| Forge or replay a webhook delivery to your receiver | An HMAC-SHA256 signature over a timestamp and the raw body. Each webhook has its own secret. The platform shows the secret one time and keeps it sealed. Receivers refuse old timestamps. Envelopes contain ids and states, never data, links or secrets. | **Enforced** (`mapi_client.webhooks.verify`) |
| Use an embed link to see more than one chart | A tokenised link to one saved plot of a platform recipe, stored as a keyed hash. The public view reads no credential and contains only the data of that plot. It has rate limits for each link and each address. It stops on revocation or expiry. | **Enforced** |
| Sign up many workspaces to get free compute | E-mail verification. The platform refuses disposable domains. Sign-up limits for each address, each domain and the platform. Operators review Sybil flags. `public` workspaces run only built-ins and declarative specs (cut line A). They run only on executors that enforce egress and DNS. | **Enforced**. Vendor identity verification and a public run on such an executor wait for a deployment. |
| Keep the data of a tenant after the tenant leaves | After a grace period, tenant deletion purges all tables and object prefixes. It sends a notification by signed webhook. A GDPR export with the credentials redacted. | **Enforced**. The platform keeps only the pseudonymised audit log and the cost ledger. |
| Misuse the MCP server | Curated tools with delimited untrusted text | Not applicable yet. The MCP server comes in v0.6. |

The [security suite](../operations/security-suite.md) contains the probes that check these
rows as hostile code, for each executor.

## What each executor enforces

Each executor declares its **isolation level**. It also declares if it enforces egress and
DNS. Before each run, the dispatcher compares these values with the
[trust tier](../guides/administration.md#trust-tiers) of the tenant. This check is in the
code. No tenant setting can make the check less strict.

| Executor | Isolation | Egress / DNS enforced | Tenants it can run | The security suite must show |
|---|---|---|---|---|
| `inproc` | `NONE` | No | Only your own notebook (never through the API) | — |
| `local-process` | `PROCESS` | No | `internal`, `self-hosted-trusted`. It does not start under `MAPI_PROFILE=multi-tenant`. | Platform probes BLOCKED. Sandbox probes can be ALLOWED. |
| `docker` (runc) | `PROCESS` | Only with `MAPI_DOCKER_NETWORK=none` | `internal`, `self-hosted-trusted` | The same as above |
| `docker-gvisor` | `GVISOR` when the supervisor can move the worker to its own uid. If not, `PROCESS`. | Only with `MAPI_DOCKER_NETWORK=none`. This needs an HTTPS bootstrap URL, thus not with the local shim. | All tiers, when egress and DNS are enforced | All probes BLOCKED |
| `cloudrun-jobs` | `MICROVM` | Egress yes (`MAPI_CLOUDRUN_EGRESS_ENFORCED`). DNS only when `MAPI_CLOUDRUN_DNS_ENFORCED=true`, after the M0 DNS check. | `internal`, `self-hosted-trusted`. `verified` and `public` when DNS is enforced. | All probes BLOCKED, run in an execution (not connected yet) |

All executors share these items, for all isolation levels: the supervisor/worker protocol, the
scrubbed worker environment, limits on logs and outputs, HMAC-signed events, and the
verification of each output by the control plane. Only the stronger levels add a boundary
between the model code and the machine that it runs on.

## Local development vs production

| Control | `make dev` | Docker Compose | Google Cloud (v0.3) |
|---|---|---|---|
| Authentication | The development IdP: any seeded account, no password. `127.0.0.1` only. | The same. Ports are published on `127.0.0.1` only. | OIDC for persons (Google or a different provider). Scoped API tokens with budgets for programs. |
| Tenancy | Many tenants and row-level security, the same as in production | The same | The same, on Cloud SQL with a separate database login for each role |
| Run isolation | A separate worker process with a scrubbed environment, **as your own user** (`DEV_MODE_ISOLATION`) | `local-process`: as above, in a hardened container. `docker` executor: one container for each run, read-only root, `no-new-privileges`. The supervisor has only the capabilities that the uid split needs. The worker runs as uid 10001 with no capabilities (`MAPI_REQUIRE_ISOLATION`). | A new Cloud Run Jobs microVM for each run. The worker runs under its own uid with `no_new_privs`. Read-only root. |
| Network egress from runs | Not restricted | `docker` executor: the network namespace of the GCS shim, or an internal network that contains only the shim | Deny-all egress, DNS included, in a VPC Service Controls perimeter |
| Object storage permissions | The GCS shim enforces permissions for each principal. The credentials of runs can only get and delete their bootstrap object. | The same | Cloud Storage IAM, with the same layout and principals |
| Bundle signatures | EC P-256 with a local key in `.local/keys/` | EC P-256 with one key on the `keys` volume. `migrate` makes the key. It is mounted read-only into `api` and `worker`. | Cloud KMS, with the same signed bytes |
| Event integrity | HMAC for each event, read from the stdout pipe of the supervisor | The same | HMAC for each event, through Cloud Logging and Pub/Sub push. The events service checks an OIDC token. |
| Web app CSP | The Vite **dev server does not send the CSP of the app**, because hot reload needs inline scripts. The CSP of the plot sandbox is enforced. | nginx enforces it, with Trusted Types in report-only mode. | Enforced |
| Plot isolation | Separate origin (port 5174), `sandbox="allow-scripts"` | Separate origin (port 5174) | Separate domain (`plots.<domain>`) |
| Quotas and cost limits | Tier defaults, quotas, the cost ledger and kill switches all apply. | The same, plus limits on worker memory, CPU and `/tmp` | The same, with the price table of the deployment |

## How the controls work

### The run sandbox

Each run is a pair of processes. An executor starts them and gives them **only** the run id
and a 256-bit nonce. The trusted supervisor uses the nonce to get its instructions from a
one-time bootstrap object. The instructions are signed URLs for its inputs and outputs, an
event HMAC key, and limits. The supervisor **deletes the object before any model code
starts**. Its storage credential can get and delete bootstrap objects only. It cannot list
them.

The worker runs the SDK and the plugin. It gets an allow-listed environment with all `MAPI_*`
variables removed, and its own session. If the supervisor starts as root, the worker also gets
a separate uid with `no_new_privs` and no core dumps. The worker can talk only to the
supervisor, in schema-checked frames.

On a laptop, the supervisor is not root. Thus, the worker runs as you, and the run reports
`DEV_MODE_ISOLATION`. All other controls continue to apply: the split, the scrubbed
environment, signed events, limits and output verification. But the boundary between your
account and the model code is only as strong as the process separation of Python. Think of a
local run as the same as a run of the plugin in a notebook.

### Uploads and the HDF5 guard <span class="mapi-badge">v0.8</span> { #uploads-and-the-hdf5-guard }

Only an ingest or inspect run in the sandbox opens an upload. The API never parses it. HDF5
(and NetCDF-4, which is HDF5) has features that can make a reader open other files, load
code or do work without a limit. Thus, before xarray or h5py opens an HDF5 file, the runner
walks the file with the low-level API of h5py. The walk never follows a link. It refuses the
file with `INGEST_UNSAFE_FILE` when the file has:

- external links, user-defined links, or soft links that do not resolve, make a loop, use
  `..` or point to a group;
- groups in a loop of hard links;
- datasets with external raw storage, or virtual datasets (their bytes are in other files);
- filters other than deflate, shuffle, fletcher32, scaleoffset, nbit and lzf (no szip, no
  plugins);
- a user block larger than 64 KiB. The guard looks for the HDF5 superblock at each offset
  where libhdf5 looks for it. Thus a file that starts with other bytes is still checked;
- too many objects or attributes, groups deeper than 32 levels, arrays with too many dims or
  attribute values that are too large;
- arrays that declare more bytes than the budget of the run;
- data types whose size has no limit (variable-length sequences, compound, array, reference
  and opaque types).

The supervisor also points `HDF5_PLUGIN_PATH` at an empty directory in the environment of the
worker. The inspect run uses the same guard and reads only metadata. Refer to
[Tree datasets, HDF5 and multi-file versions](../guides/tree-data.md#security-the-hdf5-guard).

### Outputs are claims

A run can write only under the prefix of its own attempt, through a POST policy with a size
range. When the run finishes, the control plane does not read its outputs. It lists them. It
compares the **size, CRC32C, MD5 and generation that the store calculated** with the signed
manifest of the run. If an object is missing, changed or extra, the run fails with
`INTEGRITY_FAILURE`, and the platform sends an alert.

The platform copies verified objects server-side into a write-once bundles bucket. The runs
cannot get access to this bucket. The platform signs a bundle manifest that binds the tenant,
the run, the hashes and the generation of each destination. **The platform mints download
links only after it verifies that signature.** Each link is pinned to an object generation and
expires after 15 minutes. Thus, if you delete the signing key (`.local/keys/`), you cannot
download the existing model versions. This is intentional.

### Events and liveness

Only the supervisor writes the real stdout. Each event line has a sequence number and an HMAC
under the key of the run. The control plane verifies the HMAC in constant time. It removes
duplicates on `(run, attempt, seq)`. It drops forged lines and counts them as sandbox
violations. Events never decide if a run is alive. The status from the executor decides this.
Thus, a run cannot stay "running" because it sends messages, and it cannot cause its own stop
because it becomes quiet.

### Plots

Plot specs come from recipes and plugins, and later from users. Vega and Plotly are large
JavaScript libraries. Thus, the platform uses these controls:

1. The server sanitizes each spec when a run finishes and again at each read. It uses pinned
   Vega-Lite and Plotly schemas. It permits no URL data, no `href` encodings, no external
   images, no tile maps and no prototype keys. It reduces Plotly HTML to `b`, `i`, `br`,
   `sub` and `sup`. It also has limits on size and depth.
2. Each plot renders in an `<iframe sandbox="allow-scripts">` (no `allow-same-origin`) from
   the origin of the plot sandbox. The CSP has `default-src 'none'` and scripts from its own
   origin only. `connect-src` is limited to its own topojson, and `frame-ancestors` is set to
   the app.
3. Vega runs with the expression interpreter (no `eval`). It has a loader that refuses all
   URLs and a text-only tooltip. Plotly uses its strict bundle.
4. The app and the sandbox use a small, validated `postMessage` protocol. The app accepts
   messages only from the window of that iframe, with an opaque origin.

One risk remains. A sandboxed frame can navigate itself and leak the data that the app sent
into it. We accept this risk only for the own data of a viewer. Shared and embedded plots
(v0.4) render only platform recipes.

### Untrusted text

Log lines, tracebacks and plugin messages go in `untrusted_text` or `LogLine.line`, never in
`detail`. The runner removes ANSI, control and bidi characters. The web app shows them as text
(ESLint forbids `dangerouslySetInnerHTML`). The platform treats Monaco hovers as untrusted.
The CLI removes escape sequences before it prints.

### Supply chain

- `uv.lock` locks the Python dependencies with hashes. The build makes container images from
  the lock with `--frozen`. Base images are pinned by digest (`make docker-pin-check`).
- Build-time guards make an image fail if it contains prohibited items. The control-plane
  image must not contain numpy, xarray, PyMC, the SDK or a plugin. The import-linter
  contracts run against the installed packages in the server image build.
- The web image installs JavaScript dependencies with `--frozen-lockfile --ignore-scripts`.
- The OpenAPI explorer of this documentation site loads a vendored, hash-pinned Redoc bundle.
  It sends no request outside the site.

## Known gaps in v0.4

- **Cloud behaviours that we did not verify yet.** This is all items marked [verify M0]
  above. The most important item is that the DNS response policy applies to Cloud Run Jobs.
  Until we confirm it, the executor declares DNS as not enforced. The platform does not put
  shared tiers on it.
- **The nightly security suite does not run in the cloud yet.** The probes run locally and
  against gVisor. The base images do not contain them, and Terraform defines no probe Job
  ([Security suite](../operations/security-suite.md#where-the-probes-run)).
- **The platform does not store executor conformance reports.** Thus, the admin console shows
  all executors as *conformance not passed*. You can turn on an executor without a stored
  report ([M2 verification](../operations/m2-verification.md#known-gaps-not-cloud-specific)).
  The platform enforces placement by trust tier in all cases.
- **The browser sign-in does not accept the tokens of Identity Platform yet.** Use Google or a
  different OpenID provider ([Authentication](../guides/authentication.md#identity-platform)).
- **Public workspaces need an enforcing executor.** Sign-up makes `public` workspaces. These
  run only on `docker-gvisor`, or on Cloud Run Jobs when we verify its DNS enforcement. A
  deployment with neither executor cannot run their work. Neither executor ran in the M3
  verification ([what remains](../operations/m3-verification.md#what-remains-unverified)).
- **Identity verification has no vendor integration yet.** Operators verify tenants manually
  (audited), behind the `IdentityVerifier` protocol.
- **We tested webhook deliveries to the internet** only through the development allow-list.
  Unit tests cover the SSRF policy for public hosts.
- The built-in `/docs` and `/redoc` pages of the API (the FastAPI defaults) load Swagger UI and
  Redoc from a public CDN. Use the [OpenAPI explorer](../reference/openapi.md) or
  `/openapi.json` instead.
- The Vite dev server does not enforce the CSP of the app (see the table).
- With the `local-process` executor, run code runs as the uid of the orchestrator. Thus, it
  can read the control key of the shim and the bundle-signing key. The orchestrator is
  non-dumpable and logs a warning at start-up. Run only trusted plugin code in this mode. See
  [Docker Compose](../operations/docker.md#executor-choice-local-process-vs-docker).

## What comes next

Plugin upload for allow-listed teams comes in [v0.6](../roadmap/expert-plugins.md). The
platform builds the plugins into hash-pinned overlays, and the contract kit checks them on
upload. [Public code upload](../roadmap/index.md#after-v06) lets all tenants upload their own
plugins. It comes only after these items are available:

- A dedicated overlay build pipeline with a curated package mirror
- A separate execution project for public tenants
- Binary Authorization on base images
- Abuse controls (verification before any code runs)
- A rehearsed kill-switch drill and an external penetration test
- The security suite green in the cloud for two weeks
