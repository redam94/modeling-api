---
description: How the platform is put together - the control plane, the run sandbox, storage and integrity, the event path and the integration paths (endpoints, webhooks, embeds) - with diagrams.
---

# Architecture overview <span class="mapi-badge">v0.4</span>

modeling-api has three layers. The project made them in this sequence:

1. A typed runtime for models: the SDK, the steps and the model plugins.
2. A job platform around it: the API, the queue, the executors and the runner.
3. From v0.3, a hosted platform for many tenants.

The rules that keep the last layer safe also apply to the first layers. The control plane never
imports plugin code and never decodes your arrays. Each run occurs in a sandbox. The platform
compares each output with the values that the storage calculated.

```mermaid
flowchart TB
    subgraph clients["Clients"]
        web["Web app<br/>(app origin)"]
        sandbox["Plot sandbox<br/>(separate origin)"]
        cli["mapi CLI · Python client ·<br/>curl · other apps"]
    end
    subgraph control["Control plane: JSON only, never imports plugins"]
        api["API<br/>/v1 · OpenAPI · SSE"]
        orch["Orchestrator<br/>dispatcher · reconciler ·<br/>finalizer · event ingestion"]
        pg[("PostgreSQL<br/>runs · queue · events ·<br/>tracking · registry · catalog")]
    end
    subgraph exec["Run sandbox: one per run"]
        sup["Supervisor<br/>(trusted, holds the run's URLs)"]
        wrk["Worker<br/>(untrusted: SDK + plugin)"]
    end
    store[("Object store<br/>quarantine · bootstrap · runs ·<br/>bundles · data")]

    web -->|"/v1"| api
    cli -->|"/v1"| api
    web -. "postMessage<br/>(validated)" .-> sandbox
    web -->|"signed PUT / GET"| store
    api --> pg
    orch --> pg
    api -->|"metadata, signed URLs"| store
    orch -->|"bootstrap object,<br/>verify, server-side copy"| store
    orch -->|"submit(run id, nonce)"| sup
    sup <-->|"socketpair"| wrk
    sup -->|"signed URLs only"| store
    sup -->|"HMAC-signed events"| orch
```

## The pieces

| Component | Package | What it does | What it never does |
|---|---|---|---|
| **API** | `mapi-server` (`mapi-server api`) | The `/v1` HTTP API: datasets, specs and their static validation, runs, models, scores and plots. Server-sent events. Signed URLs | Import `mapi_sdk`, a plugin, numpy or xarray. An import-linter contract prevents this, also in the server image |
| **Orchestrator** | `mapi-server` (`mapi-server worker`) | The dispatcher, the reconciler, event ingestion and the finalizer. They use a procrastinate queue in the same PostgreSQL | Decode a dataset or a bundle. It reads only object *metadata* and JSON sidecars with a size limit |
| **Executor** | `mapi_server.executors` | Starts a runner for a run: `local-process` (a subprocess), `docker` / `docker-gvisor` (a container), and Cloud Run Jobs in v0.3 | Send data other than the run id and a nonce |
| **Runner** | `mapi-runner` | A trusted *supervisor*. It gets the instructions of the run, starts an untrusted *worker*, uploads outputs and signs events | Let the worker use credentials, the network of the store or its stdout |
| **SDK and plugins** | `mapi-sdk`, `mapi-steps`, `mapi-pymc`, `mapi-sklearn`, `mapi-jax` (v0.15), `mapi-plugin-*` (for example `mapi-plugin-causal`, v0.13) | The runtime for models: pipelines with fitted state, models, coord plans, summaries, tracking and bundles | Run in a location other than the worker (on the platform) or your own process (as a library) |
| **Contracts** | `mapi-spec` | All the shapes that the two sides agree on: specs, schemas, DTOs, the run protocol and error codes | Import numpy, xarray or pandas |
| **Object store** | `mapi-gcs-shim` locally, Cloud Storage later | Buckets with object generations, CRC32C and MD5 that the server calculates, signed URLs and POST policies, and permissions for each principal | — |
| **Web app**, **plot sandbox** | `apps/web`, `apps/plot-sandbox` | The Guided and Expert UI and the Integrations hub. The plot sandbox is the only location where Vega-Lite and Plotly specs render, also for shared embed pages | Render a plot on the origin of the app |

## The control plane

The API and the orchestrator use the same PostgreSQL database. The API writes runs in state
`created`. Then it sets `queued` or `rejected` from static checks. The orchestrator owns all
the subsequent states.

```mermaid
flowchart LR
    subgraph api["API process"]
        routes["/v1 routes"]
        validate["Static validation<br/>(SchemaEffect JSON,<br/>CoordPlan, sanitizers)"]
        sse["SSE fan-out<br/>(one LISTEN per process)"]
    end
    subgraph worker["Worker process"]
        disp["Dispatcher<br/>token bucket · intent row"]
        rec["Reconciler<br/>every 10 s"]
        ev["Event ingestion<br/>HMAC verify · dedupe"]
        fin["Finalizer<br/>(procrastinate job)"]
    end
    pg[("PostgreSQL")]
    routes --> validate
    routes -->|"runs, specs,<br/>datasets"| pg
    disp -->|"lease queued runs"| pg
    rec -->|"executor status →<br/>state transitions"| pg
    ev -->|"run_progress upsert,<br/>run_events + NOTIFY"| pg
    fin -->|"model versions,<br/>signed manifests"| pg
    pg -->|"NOTIFY"| sse
```

- **Static validation** runs in the API, synchronously, and uses only JSON. It uses the
  exported *schema effect* of each step, and the settings schema and schema template of each
  model, from the catalog. A sandboxed `describe` run of the real runner makes the catalog.
  The platform keeps the catalog in a cache for each image digest. Thus, the API validates
  against exactly the code that the runner will run.
- **The dispatcher** leases queued runs through a token bucket. It writes an *intent row*
  (run, attempt, nonce) and then calls the executor. Thus, the platform can recover from a
  crash between the two operations.
- **The reconciler** is level-triggered. Every 10 seconds, it compares each active run with
  the status from the executor. This status is the source of truth for liveness. If a run
  sends no events, the reconciler only marks it `stalled`. If an execution disappears, the
  reconciler tries it again as a new attempt (in a cost window). The reconciler adopts orphaned
  executions by their nonce.
- **The finalizer** changes a finished execution into a model version (see
  [Storage and integrity](#storage-and-integrity)).
- **Events**: the orchestrator verifies them and removes duplicates on `(run, attempt, seq)`.
  It keeps one `run_progress` row for each run. It also keeps `run_events` for state changes,
  warnings, diagnostics and metrics. From v0.11, it writes the `metrics`, `params`, `tags`
  and (v0.14) `media` events to the tracking tables, not to `run_events` (see
  [Run tracking](run-tracking.md)). A PostgreSQL `NOTIFY` starts the SSE streams of each API
  process again.

The [Run protocol](../reference/run-protocol.md#run-states) gives the state machine, all the
transitions and the owner of each transition.

## The run sandbox

Each run gets a new pair of processes: a trusted supervisor and an untrusted worker. This
applies to all types of run (`ingest`, `describe`, `validate`, `fit`, `score`).

```mermaid
sequenceDiagram
    participant D as Dispatcher
    participant S as Object store
    participant X as Executor
    participant R as Supervisor
    participant W as Worker (SDK + plugin)
    D->>S: PUT bootstrap/{nonce}: RunSpec (signed URLs, HMAC key, limits)
    D->>X: submit(MAPI_RUN_ID, MAPI_NONCE)
    X->>R: start (no secrets in argv or env)
    R->>S: GET bootstrap/{nonce}, then DELETE it
    R->>S: GET inputs (signed, generation-pinned, CRC32C-checked)
    R->>W: spawn: scrubbed env, separate uid (where possible), python -I
    W-->>R: progress, logs, results (framed JSON over a socketpair)
    R-->>D: stdout: one HMAC-signed event per line
    R->>S: POST outputs (V4 POST policy on the attempt prefix)
    R->>S: PUT manifest.json (signed claim of every object)
    R-->>D: "complete" event, exit 0
```

- **No credentials in the environment.** The executor sends only `MAPI_RUN_ID` and
  `MAPI_NONCE`. The signed URLs of the run and its event HMAC key are in a one-time
  *bootstrap* object. The supervisor reads and deletes this object before model code starts.
- **The worker is untrusted.** It runs the SDK harness and the plugin with an allow-listed
  environment (with all `MAPI_*` removed), in its own session. When the supervisor runs as
  root (containers, Cloud Run), the worker runs under uid 10001 with `no_new_privs`. It
  communicates only with the supervisor, through length-prefixed JSON frames. Each frame must
  agree with its schema.
- **Only the supervisor writes events.** Its stdout has one JSON event on each line. Each event
  has a sequence number and an HMAC. All text that the worker prints becomes a `log` event. The
  `log` event has a size limit and has no control characters.
- **Limits everywhere.** There are limits for log lines (4 KB) and total logs (20 MB), event
  size, output objects and bytes, and a scratch budget for each resource class. If a run goes
  above a limit, the result is `SANDBOX_VIOLATION` or `RESOURCE_EXHAUSTED`. The platform never
  cuts results silently.
- **Checkpoints.** On SIGTERM or near the deadline, a PyMC fit writes its last segment, and
  the supervisor uploads a `partial` manifest. The run can continue from that point.

On a laptop, the worker cannot change the user, so runs report `DEV_MODE_ISOLATION`. The
process split, the scrubbed environment, the event signatures and the output verification
continue to apply. But the worker runs as you. See the [security model](security.md).

## Storage and integrity

```mermaid
flowchart LR
    subgraph buckets["Buckets: mapi-{env}-*"]
        q["quarantine<br/>uploads, 7-day lifecycle"]
        b["bootstrap<br/>one-time RunSpecs"]
        r["runs<br/>attempt scratch"]
        bun["bundles<br/>write-once, retention lock"]
        d["data<br/>content-addressed datasets"]
    end
    browser(("browser /<br/>client")) -->|"signed PUT"| q
    q -->|"signed GET"| ingest["ingest run"]
    ingest -->|"POST policy"| r
    r -->|"verify, then<br/>server-side copy"| d
    fit["fit / score run"] -->|"POST policy"| r
    r -->|"verify, then<br/>server-side copy"| bun
    bun -->|"signed GET, minted after<br/>the manifest signature checks"| client(("client"))
```

| Bucket | Who writes | Who reads |
|---|---|---|
| `quarantine` | Browsers and clients, through a signed PUT for one object | Ingest runs, through a signed GET |
| `bootstrap` | The dispatcher | The supervisor, with a principal that can only *get and delete* there (no list) |
| `runs` | Runs, through a POST policy with a limit to their attempt prefix and a size range | The finalizer (metadata) |
| `bundles` | Only the finalizer, with a server-side copy. Write-once under a retention lock. From v0.14, also the server, with a create-only copy of each verified media item of a run (`t/{tenant}/run-media/`) | Clients and runs, through signed GETs that the platform makes after verification |
| `data` | The finalizer, with a server-side copy of ingest outputs. From v0.15, also the image packs of collections, one time for each tenant (`t/{tenant}/blobs/packs/`) | Runs, through signed GETs. A run reads a pack in ranges. |

**Outputs are claims.** When a run ends, the finalizer lists the attempt prefix. It reads the
metadata of each object: the size, CRC32C, MD5 and *generation* that the store calculated. It
compares these values with the signed manifest of the run. It refuses missing, changed or
unexpected objects (`INTEGRITY_FAILURE`). It validates the small JSON sidecars under size
limits and sanitizes the plots. Then it copies the verified generations into the bundles
bucket.

Then the finalizer signs a **bundle manifest**. The manifest binds the tenant, the run, the
spec hash, the data hash, the catalog digest and each destination generation. Locally, an EC
P-256 key replaces Cloud KMS. The platform makes each subsequent download link only after it
checks that signature. Thus, it can never serve an object that was replaced. Bundles are zarr
zips and JSON. The platform does not use pickle.

## The event path

```mermaid
flowchart LR
    W["Worker"] -->|"frames"| S["Supervisor"]
    S -->|"stdout:<br/>{mapi_event, hmac}"| P{{"local executor:<br/>stdout pipe"}}
    S -.->|"v0.3: Cloud Logging<br/>→ Pub/Sub push"| I["POST /internal/events"]
    P --> V["Verify HMAC ·<br/>dedupe (run, attempt, seq)"]
    I --> V
    V -->|"progress: upsert"| RP[("run_progress")]
    V -->|"state, warning, diagnostic,<br/>metric, complete, error"| RE[("run_events")]
    RE -->|"NOTIFY"| SSE["SSE streams<br/>Last-Event-ID resume"]
    RP --> SSE
    SSE --> C(("web app ·<br/>CLI · client"))
```

The tracking events of v0.11 (`metrics`, `params`, `tags`) and v0.14 (`media`) take the same
path to the verification. Then the orchestrator writes them to the tracking tables, and the
SSE stream of the run sends a `tracking` frame. For a `media` event, the server first
downloads the object, verifies it and copies it to the bundles bucket.

Locally, the executor reads events directly from the stdout of the supervisor. In the cloud,
the same lines go through Cloud Logging and Pub/Sub to `POST /internal/events`. The
verification is the same. The platform combines progress into one event every two or three
seconds and keeps it as one row. Thus, a long fit does not write thousands of rows. The
platform keeps only state changes, warnings, diagnostics, checkpoints, metrics and terminal
events. It sends these events again to a client that connects again.

## Data in PostgreSQL

All tables have forced row-level security with a policy for each tenant that fails closed.
The schema migrations are in `mapi_server/migrations`. The releases v0.11 to v0.15 add these
migrations:

| Migration | Release | Adds |
|---|---|---|
| `0015_run_tracking` | v0.11 | `run_metrics` (each metric point), `run_metric_summary` (one row for each series, for search and sort), `run_params`, `run_tags`, and the columns `runs.name`, `runs.note`, `run_logs.level` and `run_logs.fields` |
| `0016_experiments_media` | v0.14 | `experiments`, `run_views` (saved views of the runs table), `run_media` (the images and tables of a run, with the bucket, object and generation of the verified copy), and the column `runs.experiment_id` |
| `0017_collections` | v0.15 | The layout `collection` for `dataset_versions.layout`, and the column `dataset_versions.collection` (the `CollectionInfo` of a collection version) |

The rows of the tracking tables and of `run_media` go with their run. The request path can
add and change these rows, but it cannot delete metrics, summaries, params or media. The
gateway role (the lookups before the API knows the tenant) gets no access to the new
tables. An older server cannot read a collection version. Thus the downgrade of
`0017_collections` refuses while collection versions exist.

## Runner images

The worker runs in a runner image. Each image contains the runner, the SDK, the built-in
steps, the plot recipes and some model plugins. The image of a run comes from the model of
its spec. The control plane validates each spec against the catalog of that image.

| Image | Models |
|---|---|
| `mapi/base-bayes` | `mapi-pymc`, the `mmm.*`, `tabular.*` and `sci.*` models |
| `mapi/base-ml` | `mapi-sklearn` with LightGBM and CatBoost (v0.14). No PyMC. |
| `mapi/base-forecast` | The `ts.*` models and, from v0.13, the `causal.*` models and pymc-extras |
| `mapi/base-dl` <span class="mapi-badge">v0.15</span> | The `dl.*` models on JAX, Flax and Optax (`mapi-jax[cuda12]`). No PyMC and no scikit-learn. |

`mapi/base-bayes` and `mapi/base-ml` also contain `mapi-jax` for the CPU. Thus the step
`dl.embed` can make image features in front of their models. The `base-dl` image uses its
GPU only in a run of the class `GPU` ([Quotas and cost](quotas-and-cost.md#the-gpu-class)).
For the full contents, see [Container images](../operations/images.md).

## The integration paths (v0.4)

Three paths send the results of a model out of the platform. No path gives more data to the
outside world than it needs. An endpoint call runs in a sandbox, as all scores do. A webhook
sends only ids and states. An embed link shows one chart.

```mermaid
flowchart LR
    subgraph out["Outside"]
        app["Sheets · BI ·<br/>your app"]
        rcv["Your webhook<br/>receiver"]
        viewer["A viewer's browser<br/>(Notion, Looker Studio, …)"]
    end
    subgraph cp["Control plane"]
        ep["POST /v1/endpoints/{alias}/score<br/>score:&lt;alias&gt; token · input contract ·<br/>CoordPlan · rate limit and budget"]
        outbox[("events outbox<br/>written in the same<br/>transaction as the change")]
        relay["Worker relay<br/>retries · SSRF guard ·<br/>HMAC signature"]
        embed["GET /v1/embeds/{token}<br/>no credentials ·<br/>one platform-recipe plot"]
    end
    sb["Score run<br/>(fresh sandbox)"]
    bun[("Verified bundles")]
    page["Plot sandbox origin<br/>/embed/&lt;token&gt;"]

    app -->|"inline XrJSON"| ep --> sb --> bun
    ep -->|"signed links to the outputs"| app
    sb -. "score.completed" .-> outbox
    outbox --> relay -->|"Mapi-Signature"| rcv
    viewer --> page -->|"fetch, CSP-limited"| embed
    embed -->|"data inlined from the<br/>verified bundle"| bun
```

- **Endpoints** freeze the input contract of the model version at promotion. Thus, the
  platform checks a call statically (dims, coordinates, future covariates) before it starts a
  run. The platform keeps the payload as a run input and decodes it only in the sandbox.
  Promotion goes through the [scorecard gate](integrations.md#promotion-gate).
- **Webhooks**: the platform writes them to an outbox in the same transaction as the change
  that they report (a finished run, a model version that is ready, a promotion). Then the
  worker delivers them through the [SSRF guard](integrations.md#ssrf-protection). The worker
  signs, retries and logs each delivery.
- **Embeds**: the platform serves them to a page on the origin of the plot sandbox. This page
  can get only the public embed view. The API puts the data of the plot from the verified
  bundle into the page, and does not decode it
  ([Sharing plots](../guides/sharing-plots.md#the-embed-page)).

## Where to read more

- [Run protocol](../reference/run-protocol.md): states, bootstrap, the wire format of events,
  exit codes, manifests and the storage layout.
- [Runner](../reference/runner.md): the guarantees of the supervisor.
- [GCS shim](../reference/gcs-shim.md): the local object store, its buckets and principals.
- [Security model](security.md): the controls on a laptop, and the controls that production
  adds.
- [HTTP API](../reference/http-api.md) and the [OpenAPI explorer](../reference/openapi.md).
