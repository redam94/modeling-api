# Cloud Run Jobs executor

The `cloudrun-jobs` executor runs each attempt as a new Cloud Run Jobs (gen2) execution. Each
run gets a microVM, which the executor makes from a small, fixed set of Jobs. This executor is
the primary backend of the hosted platform ([v0.3](../roadmap/cloud.md)).

!!! warning "Tested against fakes only"
    The executor, the Pub/Sub push endpoint, the KMS signer and the Cloud Storage driver use
    the real Google client libraries. But we tested them **only** against test doubles:
    an in-memory Cloud Run API (`FakeCloudRun`), local OIDC keys, a KMS double with local
    keys and a fake storage client. Items with the mark **[verify M0]** depend on Cloud Run
    behaviour that we must still confirm in a real project.

## Install and select it

```bash
pip install 'mapi-server[gcp]'      # google-cloud-run, -storage, -pubsub, -kms
export MAPI_EXECUTOR=cloudrun-jobs
export MAPI_CLOUDRUN_PROJECT=mapi-prod-exec-paid
export MAPI_CLOUDRUN_REGION=europe-west1
```

The orchestrator (`mapi-server worker`) authenticates with Application Default Credentials.
On Cloud Run, these are the credentials of its own service account. The service account needs
`run.jobs.run`, `run.executions.get`, `run.executions.list`, `run.executions.cancel` and
`run.tasks.list` on the exec project. Use roles/run.developer, or a custom role with only
these permissions.

## The Job inventory

Execution overrides can change only environment variables, the task count and the timeout.
They can never change the image, CPU or memory. Thus the platform deploys one Job for each
base image and resource class, and it selects one Job for each attempt:

| | S (2 vCPU, 8 GiB) | M (4 vCPU, 16 GiB) | L (8 vCPU, 32 GiB) |
|---|---|---|---|
| `base-bayes` (`mmm.*`, `sci.*`, `tabular.*`, ingest, describe) | `mapi-prod-base-bayes-s` | `mapi-prod-base-bayes-m` | `mapi-prod-base-bayes-l` |
| `base-ml` (`sklearn.*`) | `mapi-prod-base-ml-s` | `mapi-prod-base-ml-m` | `mapi-prod-base-ml-l` |
| `base-forecast` (`ts.*`, `causal.*`) | `mapi-prod-base-forecast-s` | `mapi-prod-base-forecast-m` | `mapi-prod-base-forecast-l` |
| `base-dl` (`dl.*`, v0.15) | `mapi-prod-base-dl-s` | `mapi-prod-base-dl-m` | `mapi-prod-base-dl-l` |

From v0.15, the inventory also has one GPU Job: `mapi-prod-base-dl-gpu` (see
[GPU jobs](#gpu-jobs)). Only a GPU base (`base-dl`) has a GPU Job. A spec of the GPU class
for a model of another namespace is refused at placement: "only dl models run on the GPU
class".

The names use `MAPI_CLOUDRUN_JOB_TEMPLATE` (default `mapi-{env}-{base}-{class}`). The
infrastructure must make exactly these Jobs. Each Job must have:

- `max-retries = 0` (the platform decides about retries).
- The execution service account (get and delete on the bootstrap bucket, and no other
  permissions).
- The static environment `MAPI_BOOTSTRAP_URL=gs://mapi-{env}-bootstrap/{nonce}`.
- The deny-all network and the restricted Google APIs VIP.

XL runs are for the GKE executor (planned). The dispatcher refuses XL runs for `cloudrun-jobs`.

### GPU jobs <span class="mapi-badge">v0.15</span> { #gpu-jobs }

!!! warning "Not verified"
    The GPU Job is written to the Cloud Run documentation, but it is not applied or tested
    against GCP. The `base-dl` CUDA image is not tested on a GPU. Do a test with a small run
    before you use it.

The resource class `GPU` runs on the Job `{base-dl, GPU}`:

| Field | Value |
|---|---|
| Image | `base-dl` (`mapi-jax[cuda12]`: `jax[cuda12]` from pip wheels on python-slim, no NVIDIA base image) |
| Resources | `cpu = "8"`, `memory = "32Gi"`, `nvidia.com/gpu = "1"` |
| Node selector | `accelerator = "nvidia-l4"` |
| Zonal redundancy | off (`gpu_zonal_redundancy_disabled = true`) |
| Other fields | The same service account, network, environment and `max_retries = 0` as the CPU Jobs |

To make the Job:

1. Select a region that has Cloud Run GPUs (L4) and request the GPU quota for the project.
2. Build and push the `base-dl` image. It is about 3 GB, because of the CUDA wheels.
3. In `infra/terraform/envs/<env>/main.tf`, set `gpu_jobs = true` (the default is
   `false`). The `stack` module gives it to the `exec` module of each pool. The image
   `images["base-dl"]` comes from `images.tfvars.json`, which `deploy/gcp/build-push.sh`
   writes.
4. Apply. The `exec` module then adds `GPU` to the classes of the inventory. Thus Terraform
   sets `MAPI_CLOUDRUN_GPU_JOBS=true` on the orchestrator. Without this setting, the
   executor does not offer the GPU class.
5. Submit a small `dl.cnn_classifier` run of the class `GPU`.
6. Open the run. Make sure that the param `dl.backend` is `gpu` and that the run has no
   `GPU_UNAVAILABLE` warning.

The cost of a GPU attempt adds `accelerator_seconds` (wall time × 1) at the price of the
class `GPU` in the price table (the default is the Cloud Run L4 list price, **[verify]**).

## What one attempt looks like

1. The dispatcher writes the intent row `(run, attempt, nonce)` and the bootstrap object
   `bootstrap/<nonce>`. The bootstrap object is the RunSpec with signed URLs and the HMAC
   key.
2. The dispatcher calls `jobs.run` on the selected Job with **only** these overrides:
    - `MAPI_RUN_ID` and `MAPI_NONCE`.
    - `taskCount`.
    - The task timeout. This is the timeout of the run plus
      `MAPI_CLOUDRUN_TIMEOUT_MARGIN_S`. Thus the supervisor can flush checkpoints before the
      backstop.

    Nothing goes in `args`.
3. The metadata of the operation names the execution. The dispatcher keeps the execution
   name as the handle of the attempt, and it logs the operation name.
4. The supervisor reads and deletes its bootstrap object. It runs the worker and prints
   events with HMAC signatures. The events go from Cloud Logging to Pub/Sub to
   `POST /internal/events`.
5. The reconciler polls `executions.get`. After the execution is complete, it also polls
   `tasks.list` for the exit codes. If there are no events, the reconciler only marks the run
   `stalled`. The status from the executor decides if the run is alive.

| Execution outcome | Reported as | Run outcome |
|---|---|---|
| All tasks succeeded | `succeeded`, exit 0 | finalized after the `complete` event |
| Supervisor exit 16 (checkpoints flushed) | `failed` or `cancelled`, exit 16 | finalized as `partial` |
| Exit 137, or "memory limit exceeded" | `failed`, exit 137 | `failed` (`RESOURCE_EXHAUSTED`), retry with the next larger class |
| Task timeout | `failed`, exit 143 | `failed` (`TIMEOUT`) |
| Cancelled through `executions.cancel` | `cancelled` | `cancelled`, or `partial` if it flushed |
| Execution not found | `unknown` | cancelled and confirmed gone, then retried as an infrastructure failure |

### Orphans and duplicates

Cloud Run selects the execution names. Thus the nonce identifies an execution. The dispatcher
can stop after `jobs.run` but before it keeps the handle. If this occurs, the reconciler calls
`adopt(nonce)`. It lists the recent executions of each Job in the inventory. Then it finds
the `MAPI_NONCE` override in the execution template **[verify M0]**.

If two executions have the same nonce, the reconciler keeps the oldest and cancels the
others. A duplicate that starts finds no bootstrap object, because the first execution
deleted it. The duplicate then stops and does no work.

A `jobs.run` call can fail in a way that is not clear, for example a timeout or a 5xx after
the dispatcher sent the request. The dispatcher never retries this call without a check. The
next submit of that nonce adopts first.

### Rate limits

`jobs.run` has a limit for each project and region (180 calls a minute **[verify M0]**).
Each orchestrator keeps a token bucket at `MAPI_CLOUDRUN_SUBMITS_PER_MINUTE` (default 150).
Divide the quota between the orchestrator replicas.

A `429` answer means that Cloud Run made nothing. The executor then does these steps:

1. It puts all submitters into a shared cool-down. This is an exponential back-off from
   `MAPI_CLOUDRUN_BACKOFF_BASE_S`. It is at least the `Retry-After` of the server, but never
   more than `MAPI_CLOUDRUN_BACKOFF_CAP_S`.
2. It counts the answer as an alarm. The alarm shows in these locations:
    - `ALARM cloudrun-jobs: jobs.run answered 429` in the log.
    - `throttled_last_hour` in the executor health.
    - `mapi_executor_submit_throttled_total{executor}` at `/metrics`. The `MapiJobsRun429`
      alert monitors this metric.
3. It retries the same nonce.

After `MAPI_CLOUDRUN_MAX_429_RETRIES` answers in sequence, the run goes back to the queue for
a new attempt. This does not use one of its `MAPI_MAX_INFRA_RETRIES`. Thus a large quantity
of throttled calls makes runs late, but it never makes them fail.

While a dispatcher works through a batch, it extends the leases of the runs that it has not
submitted yet. Thus the reconciler never takes a run whose submit is only slow. If one submit
stops for more than ten lease lengths, the renewals stop. The leases then expire. The
reconciler adopts what `jobs.run` made (by nonce), or it retries the attempt.

### Fan-out with `taskCount`

Homogeneous children of a fan-out parent can run as the tasks of **one** execution
(`RunSubmit.task_count`). Homogeneous children have the same kind, class, base and timeout.
Each task *i* does these actions:

- It reads its own bootstrap object at a nonce. The nonce comes from the execution nonce and
  `CLOUD_RUN_TASK_INDEX` (`mapi_server.executors.tasks.task_nonce`).
- It gives numbers to its events from `task_seq(i, 0)`. Thus the event dedupe keeps the
  events of all tasks.
- It writes its outputs under `.../a{n}/tasks/{i}/`.

The execution succeeds only when all tasks succeed. A cancel stops all of them. Fan-out
parents (`SBC`, `Backtest`) can use tasks. The executor side is ready. v0.14 retired the
`Sweep` document: a sweep is a workflow with a `search.grid` node.

## Events: authenticated Pub/Sub push

A Cloud Logging sink with the filter `jsonPayload.mapi_event:*` publishes the event lines of
the supervisors to a topic. A push subscription with OIDC authentication sends them to
`POST /internal/events`. Configure the expected token:

```bash
export MAPI_PUBSUB_PUSH_AUDIENCE=https://api.example.com/internal/events
export MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT=mapi-events-push@mapi-prod-control.iam.gserviceaccount.com
export MAPI_PUBSUB_PUSH_SUBSCRIPTIONS=projects/mapi-prod-control/subscriptions/mapi-run-events-push
```

The server then does a check of each push in this sequence, before it reads the body:

1. The RS256 signature of the bearer token, against the JWKS of Google. The server keeps the
   JWKS in a cache, as `Cache-Control` tells. An unknown key id causes a maximum of one new
   fetch each 30 s. If the JWKS is not available for a short time, the cached keys continue
   to work.
2. The claims of the token. The issuer is Google, and the audience is ours. The push service
   account has a verified e-mail. The token is not expired (60 s leeway).
3. The subscription name, against `MAPI_PUBSUB_PUSH_SUBSCRIPTIONS`.

If a check fails, the server answers `401`/`403`, and Pub/Sub retries. Configure a
dead-letter topic. The server then decodes the message: the base64 Cloud Logging entry,
`jsonPayload.mapi_event` and the `run.googleapis.com/execution_name` label. The server
verifies the HMAC of the event with the key of its attempt. It removes duplicates on
`(run, attempt, seq)` and merges the event into the progress row of the run.

If an event is forged or replayed, the server acknowledges it (`200`) and changes nothing. It
counts the event as a sandbox violation.

In the `multi-tenant` profile, the endpoint refuses all requests until you configure push
authentication. For a local setup, you can set `MAPI_INTERNAL_TOKEN` instead.

## Bundle signatures with Cloud KMS

```bash
export MAPI_SIGNER=kms
export MAPI_KMS_KEY_VERSION=projects/mapi-prod-control/locations/europe-west1/keyRings/mapi/cryptoKeys/bundles/cryptoKeyVersions/2
export MAPI_KMS_TRUSTED_KEY_VERSIONS=projects/.../cryptoKeys/bundles/cryptoKeyVersions/1
```

The server sends only the SHA-256 digest of a manifest to `asymmetricSign`
(`EC_SIGN_P256_SHA256` or `RSA_SIGN_PSS_*_SHA256`). It does a check of the response for
`verified_digest_crc32c`, the key version name and `signature_crc32c`. It verifies the
signature locally before it keeps it. At startup, the server loads the public keys of the
active and trusted versions. Thus, before the server mints URLs, it verifies a manifest
locally and never waits for KMS.

To rotate the key, do these steps:

1. Make a new key version.
2. Set `MAPI_KMS_KEY_VERSION` to the new version.
3. Keep the old version in `MAPI_KMS_TRUSTED_KEY_VERSIONS` until no model version that it
   signed is in use.

With Terraform, these settings are `control.kms_key_version` and
`control.kms_trusted_key_versions` (`ops/runbooks/rotating-keys.md`).

## Cloud Storage

`GcsObjectStore.for_cloud_run(...)` signs V4 URLs and POST policies through the IAM
Credentials `signBlob` API. It uses a token that it refreshes (the control-plane service
account). It signs create-only POST policies as an uploader account. This account can make
objects, but it cannot delete them. GCS POST forms do not accept a generation precondition.
Thus an account that cannot delete cannot overwrite.

The URLs are byte-identical to the URLs from the V4 signer of `google-cloud-storage`. The
store does a check of each write against the CRC32C and MD5 that Cloud Storage calculated.
Copies pin the source generation and compare the result. Refer to
[GCS shim](../reference/gcs-shim.md#real-cloud-storage).

## Executor conformance and load

An executor gets shared tiers only after it passes two suites. These are the executor
conformance suite (`mapi_spec.conformance_executor`) and the
[security suite](security-suite.md). The conformance suite has these cases:

- Submit, and orphan adoption after a dispatcher crash.
- Duplicate submit, and cancel during a run.
- Timeout, and stalled-not-lost.
- Confirmed cancel before retry, and checkpoint resume.
- OOM classification and fan-out.
- Generation verification and 429 back-off.

```bash
uv run pytest packages/server/tests/test_executor_conformance.py      # fake-cloudrun, local-process
uv run pytest packages/server/tests/test_executor_conformance.py -m docker   # needs a daemon
uv run pytest packages/server/tests/test_load_cloudrun.py -s           # 100 fits; prints metrics
```

Each case runs the real dispatcher, reconciler, event ingestion and finalizer. It does a
check of the exact sequence of run states, which a database trigger records. The load test
sends 100 concurrent fits through `cloudrun-jobs` over the fake backend. It has these
conditions:

- The outputs are on the GCS shim.
- The events come as Pub/Sub pushes that are late and duplicated.
- There are three `429` answers and two lost `jobs.run` responses.

At the default of 150 submits a minute, the test measures a p95 time-to-`running` of about
46 s. No run is lost, and each run has exactly one execution. Real Cloud Run cold starts add
more time.

### The shared-tier gate

The code enforces the gate. The dispatcher puts `verified` and `public` tenants on an
executor only when these two conditions are true:

1. The caps of the executor permit it: an isolation of gVisor or higher, with egress and DNS
   enforced.
2. A passing executor-conformance report **and** a passing security-suite report are on
   record. They must be for exactly the caps that the executor reports now. Neither report
   can be older than `MAPI_EXECUTOR_EVIDENCE_MAX_AGE_S`. The default is 3 days, so the
   nightly suites keep the gate open.

A security-suite report is evidence of enforced egress and DNS only under one condition. The
`http_egress`, `dns_egress` and `metadata_token` probes must run, and the executor must stop
them. A skipped probe is not evidence. Record results with
`PUT /v1/admin/executors/{name}/evidence` (only operators, with an audit record). Include
the target of the suites:

```bash
curl -X PUT "$API/v1/admin/executors/cloudrun-jobs/evidence" -H 'content-type: application/json' \
  -d '{"target": {"executor": "cloudrun-jobs", "caps": <caps from GET /v1/admin/executors>,
                  "remote_control_api": true, "enforces_memory_limit": true},
       "conformance": <ConformanceReport>, "security": <mapi-security --json output>}'
```

Until you record the evidence, the dispatcher keeps `verified` and `public` runs in the
queue. It never makes them fail because evidence is missing. `GET /v1/admin/executors` shows
`shared_tiers_enabled: false`, with the reason in `message`. It also shows
`conformance_passed` and `security_passed`.

The executor stays enabled for the trusted tiers. `enabled` and the `disabled` status are the
switch of the operators (`PUT /v1/admin/executors/{name}`, incident containment). They are
never the switch of the evidence gate. Evidence for other caps does not count, for example
after a change to the region list or to `MAPI_CLOUDRUN_DNS_ENFORCED`. The gate does not apply
to `internal` and `self_hosted_trusted` tenants.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_CLOUDRUN_PROJECT` | — | Exec project that holds the Job inventory (necessary) |
| `MAPI_CLOUDRUN_REGION` | `europe-west1` | Region of the inventory |
| `MAPI_CLOUDRUN_JOB_TEMPLATE` | `mapi-{env}-{base}-{class}` | Rule for Job ids |
| `MAPI_CLOUDRUN_PLUGIN_BASES` | `mmm`, `sci`, `tabular`: `base-bayes`; `sklearn`: `base-ml`; `ts`, `causal`: `base-forecast`; `dl`: `base-dl` (v0.15) | Plugin namespace to base (JSON) |
| `MAPI_CLOUDRUN_DEFAULT_BASE` | `base-bayes` | Base for runs without a plugin |
| `MAPI_CLOUDRUN_GPU_JOBS` | `false` | v0.15: the inventory has the GPU Job of `base-dl`. Runs of the class `GPU` go to it |
| `MAPI_CLOUDRUN_SUBMITS_PER_MINUTE`, `MAPI_CLOUDRUN_SUBMIT_BURST` | `150`, `10` | Token bucket in front of `jobs.run` (maximum 180) |
| `MAPI_CLOUDRUN_MAX_429_RETRIES` | `6` | Number of `429` answers in sequence before a submit fails |
| `MAPI_CLOUDRUN_BACKOFF_BASE_S`, `MAPI_CLOUDRUN_BACKOFF_CAP_S` | `1`, `64` | Exponential back-off after a `429` |
| `MAPI_CLOUDRUN_TIMEOUT_MARGIN_S` | `120` | Task timeout = run timeout + margin |
| `MAPI_CLOUDRUN_EGRESS_ENFORCED` | `true` | The exec network denies all egress |
| `MAPI_CLOUDRUN_DNS_ENFORCED` | `false` | The DNS response policy is verified for Jobs **[verify M0]**. `verified` and `public` tenants need the two settings, and also [recorded evidence](#the-shared-tier-gate) |
| `MAPI_CLOUDRUN_ADOPT_LOOKBACK_S` | `93600` | How far back orphan adoption looks |
| `MAPI_PUBSUB_PUSH_AUDIENCE`, `MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT` | — | Expected push token (set the two) |
| `MAPI_PUBSUB_PUSH_SUBSCRIPTIONS` | any | Accepted subscriptions (comma-separated) |
| `MAPI_GOOGLE_JWKS_URL` | Google's | The source of the OIDC signing keys |
| `MAPI_SIGNER` | `local` | `local` (PEM key file) or `kms` |
| `MAPI_KMS_KEY_VERSION`, `MAPI_KMS_TRUSTED_KEY_VERSIONS` | — | Active key versions and key versions that are still trusted |
