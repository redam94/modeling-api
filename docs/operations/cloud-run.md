# Cloud Run Jobs executor

The `cloudrun-jobs` executor runs every attempt as a fresh Cloud Run Jobs (gen2) execution: a
microVM per run, created from a small, fixed set of Jobs. It is the primary backend of the
hosted platform ([v0.3](../roadmap/cloud.md)).

!!! warning "Tested against fakes only"
    The executor, the Pub/Sub push endpoint, the KMS signer and the Cloud Storage driver are
    built on the real Google client libraries but have **only** been exercised against test
    doubles: an in-memory Cloud Run API (`FakeCloudRun`), locally generated OIDC keys, a KMS
    double backed by local keys and a fake storage client. Items marked **[verify M0]** depend
    on Cloud Run behaviour that still has to be confirmed in a real project.

## Install and select it

```bash
pip install 'mapi-server[gcp]'      # google-cloud-run, -storage, -pubsub, -kms
export MAPI_EXECUTOR=cloudrun-jobs
export MAPI_CLOUDRUN_PROJECT=mapi-prod-exec-paid
export MAPI_CLOUDRUN_REGION=europe-west1
```

The orchestrator (`mapi-server worker`) authenticates with Application Default Credentials:
on Cloud Run, its own service account, which needs `run.jobs.run`, `run.executions.get`,
`run.executions.list`, `run.executions.cancel` and `run.tasks.list` on the exec project
(roles/run.developer, or a custom role with just these).

## The Job inventory

Execution overrides can change only environment variables, the task count and the timeout —
never the image, CPU or memory. The platform therefore deploys one Job per base image and
resource class, and picks one per attempt:

| | S (2 vCPU, 8 GiB) | M (4 vCPU, 16 GiB) | L (8 vCPU, 32 GiB) |
|---|---|---|---|
| `base-bayes` (`mmm.*`, `tabular.*`, ingest, describe) | `mapi-prod-base-bayes-s` | `mapi-prod-base-bayes-m` | `mapi-prod-base-bayes-l` |
| `base-ml` (`sklearn.*`) | `mapi-prod-base-ml-s` | `mapi-prod-base-ml-m` | `mapi-prod-base-ml-l` |
| `base-forecast` (`ts.*`) | `mapi-prod-base-forecast-s` | `mapi-prod-base-forecast-m` | `mapi-prod-base-forecast-l` |

The names follow `MAPI_CLOUDRUN_JOB_TEMPLATE` (default `mapi-{env}-{base}-{class}`); the
infrastructure must create exactly these Jobs, each with:

- `max-retries = 0` (the platform decides about retries);
- the execution service account (get and delete on the bootstrap bucket, nothing else);
- the static environment `MAPI_BOOTSTRAP_URL=gs://mapi-{env}-bootstrap/{nonce}`;
- the deny-all network and the restricted Google APIs VIP.

XL runs belong to the GKE executor (M6); the dispatcher refuses them for `cloudrun-jobs`.

## What one attempt looks like

1. The dispatcher writes the intent row `(run, attempt, nonce)` and the bootstrap object
   `bootstrap/<nonce>` (the RunSpec with signed URLs and the HMAC key).
2. `jobs.run` on the chosen Job with **only** these overrides: `MAPI_RUN_ID`, `MAPI_NONCE`,
   `taskCount` and the task timeout (the run's timeout plus `MAPI_CLOUDRUN_TIMEOUT_MARGIN_S`,
   so the supervisor can flush checkpoints before the backstop). Nothing goes in `args`.
3. The operation's metadata names the execution; the execution name is stored as the attempt's
   handle and the operation name is logged.
4. The supervisor reads and deletes its bootstrap object, runs the worker and prints
   HMAC-signed events, which travel Cloud Logging → Pub/Sub → `POST /internal/events`.
5. The reconciler polls `executions.get` (and `tasks.list` once the execution finished, for
   the exit codes). Event silence only marks a run `stalled`; the executor's status decides
   whether it is alive.

| Execution outcome | Reported as | Run outcome |
|---|---|---|
| All tasks succeeded | `succeeded`, exit 0 | finalized after the `complete` event |
| Supervisor exit 16 (checkpoints flushed) | `failed` or `cancelled`, exit 16 | finalized as `partial` |
| Exit 137, or "memory limit exceeded" | `failed`, exit 137 | `failed` (`RESOURCE_EXHAUSTED`), retry one class up |
| Task timeout | `failed`, exit 143 | `failed` (`TIMEOUT`) |
| Cancelled through `executions.cancel` | `cancelled` | `cancelled`, or `partial` if it flushed |
| Execution not found | `unknown` | cancelled and confirmed gone, then retried as an infrastructure failure |

### Orphans and duplicates

Cloud Run chooses execution names, so an execution is identified by its nonce. If the
dispatcher dies after `jobs.run` but before storing the handle, the reconciler calls
`adopt(nonce)`: it lists the recent executions of every Job in the inventory and matches the
`MAPI_NONCE` override in the execution template **[verify M0]**. If two executions carry the
same nonce, the oldest is kept and the others are cancelled; a duplicate that starts anyway
finds no bootstrap object (the first one deleted it) and exits without doing any work.

A `jobs.run` call that fails ambiguously (a timeout or a 5xx after the request was sent) is
never retried blindly: the next submit of that nonce adopts first.

### Rate limits

`jobs.run` is limited per project and region (180 calls a minute **[verify M0]**). Each
orchestrator keeps a token bucket at `MAPI_CLOUDRUN_SUBMITS_PER_MINUTE` (default 150 — split
the quota between orchestrator replicas). A `429` answer means nothing was created: the
executor puts every submitter into a shared cool-down (exponential back-off from
`MAPI_CLOUDRUN_BACKOFF_BASE_S`, at least the server's `Retry-After`), counts the answer as an
alarm (`ALARM cloudrun-jobs: jobs.run answered 429` in the log, `throttled_last_hour` in the
executor health) and retries the same nonce.

### Fan-out with `taskCount`

Homogeneous children of a fan-out parent (same kind, class, base and timeout) can run as the
tasks of **one** execution (`RunSubmit.task_count`). Task *i* reads its own bootstrap object
at a nonce derived from the execution nonce and `CLOUD_RUN_TASK_INDEX`
(`mapi_server.executors.tasks.task_nonce`), numbers its events from `task_seq(i, 0)` so the
event dedupe keeps every task's events, and writes its outputs under `.../a{n}/tasks/{i}/`.
The execution succeeds only when every task does; a cancel stops all of them. Fan-out parents
(`Sweep`, `SBC`, `Backtest`) arrive in v0.5; the executor side is ready.

## Events: authenticated Pub/Sub push

A Cloud Logging sink with the filter `jsonPayload.mapi_event:*` publishes the supervisors'
event lines to a topic; a push subscription with OIDC authentication delivers them to
`POST /internal/events`. Configure the expected token:

```bash
export MAPI_PUBSUB_PUSH_AUDIENCE=https://api.example.com/internal/events
export MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT=mapi-events-push@mapi-prod-control.iam.gserviceaccount.com
export MAPI_PUBSUB_PUSH_SUBSCRIPTIONS=projects/mapi-prod-control/subscriptions/mapi-run-events-push
```

Every push is then checked in this order, before anything in the body is read:

1. the bearer token's RS256 signature against Google's JWKS (cached per `Cache-Control`; an
   unknown key id triggers at most one refetch every 30 s; cached keys ride out a short JWKS
   outage);
2. its claims: issuer Google, our audience, the push service account with a verified e-mail,
   not expired (60 s leeway);
3. the subscription name against `MAPI_PUBSUB_PUSH_SUBSCRIPTIONS`.

Failures answer `401`/`403` and are retried by Pub/Sub (configure a dead-letter topic). The
message is then decoded — base64 Cloud Logging entry, `jsonPayload.mapi_event`, the
`run.googleapis.com/execution_name` label — and the event is HMAC-verified with its attempt
key, deduplicated on `(run, attempt, seq)` and coalesced into the run's progress row. A forged
or replayed event is acknowledged (`200`, counted as a sandbox violation) and changes nothing.

In the `multi-tenant` profile the endpoint refuses every request until push authentication is
configured (or, for a local setup, `MAPI_INTERNAL_TOKEN` is set).

## Bundle signatures with Cloud KMS

```bash
export MAPI_SIGNER=kms
export MAPI_KMS_KEY_VERSION=projects/mapi-prod-control/locations/europe-west1/keyRings/mapi/cryptoKeys/bundles/cryptoKeyVersions/2
export MAPI_KMS_TRUSTED_KEY_VERSIONS=projects/.../cryptoKeys/bundles/cryptoKeyVersions/1
```

Only the SHA-256 digest of a manifest is sent to `asymmetricSign` (`EC_SIGN_P256_SHA256` or
`RSA_SIGN_PSS_*_SHA256`). The response is checked for `verified_digest_crc32c`, the key version
name and `signature_crc32c`, and the signature is verified locally before it is stored. The
public keys of the active and trusted versions are loaded at startup, so verifying a manifest
before minting URLs is local and never waits on KMS. To rotate, create a new version, point
`MAPI_KMS_KEY_VERSION` at it and keep the old one in `MAPI_KMS_TRUSTED_KEY_VERSIONS` until
no model version signed with it is in use.

## Cloud Storage

`GcsObjectStore.for_cloud_run(...)` signs V4 URLs and POST policies through the IAM
Credentials `signBlob` API with a refreshing token (the control-plane service account), and
create-only POST policies as an uploader account that can create but not delete objects —
GCS POST forms take no generation precondition, so an account that cannot delete cannot
overwrite. The URLs are byte-identical to `google-cloud-storage`'s own V4 signer. Every write
is checked against the CRC32C and MD5 that Cloud Storage computed, and copies pin the source
generation and compare the result. See [GCS shim](../reference/gcs-shim.md#real-cloud-storage).

## Executor conformance and load

An executor takes shared tiers only after it passes the executor conformance suite
(`mapi_spec.conformance_executor`) and the [security suite](security-suite.md): submit, orphan adoption after a dispatcher crash,
duplicate submit, cancel mid-run, timeout, stalled-not-lost, confirmed cancel before retry,
checkpoint resume, OOM classification, fan-out, generation verification and 429 back-off.

```bash
uv run pytest packages/server/tests/test_executor_conformance.py      # fake-cloudrun, local-process
uv run pytest packages/server/tests/test_executor_conformance.py -m docker   # needs a daemon
uv run pytest packages/server/tests/test_load_cloudrun.py -s           # 100 fits; prints metrics
```

Each case runs the real dispatcher, reconciler, event ingestion and finalizer and checks the
exact sequence of run states (recorded by a database trigger). The load test pushes 100
concurrent fits through `cloudrun-jobs` over the fake backend, with outputs on the GCS shim,
events as lagging, duplicated Pub/Sub pushes, three `429`s and two lost `jobs.run` responses.
At the default 150 submits a minute it measures a p95 time-to-`running` of about 46 s with no
lost run and exactly one execution per run. Real Cloud Run cold starts come on top.

### The shared-tier gate

The gate is enforced in code. `verified` and `public` tenants are placed on an executor only
when both hold:

1. its caps allow it: isolation of at least gVisor, with egress and DNS enforced;
2. a passing executor-conformance report **and** a passing security-suite report were recorded
   for exactly the caps it currently reports, and neither is older than
   `MAPI_EXECUTOR_EVIDENCE_MAX_AGE_S` (3 days by default, so the nightly suites keep it open).

A security-suite report only counts as evidence of enforced egress and DNS when the
`http_egress`, `dns_egress` and `metadata_token` probes ran and were blocked (skipped is not
evidence). Record results with `PUT /v1/admin/executors/{name}/evidence` (operators only,
audited), with the target the suites ran against:

```bash
curl -X PUT "$API/v1/admin/executors/cloudrun-jobs/evidence" -H 'content-type: application/json' \
  -d '{"target": {"executor": "cloudrun-jobs", "caps": <caps from GET /v1/admin/executors>,
                  "remote_control_api": true, "enforces_memory_limit": true},
       "conformance": <ConformanceReport>, "security": <mapi-security --json output>}'
```

Until then the dispatcher leaves `verified` and `public` runs queued (it never fails them for
missing evidence), and `GET /v1/admin/executors` shows the executor as `disabled` with the
reason, plus `conformance_passed`, `security_passed` and `shared_tiers_enabled`. Evidence
recorded for other caps (a changed region list, a flipped `MAPI_CLOUDRUN_DNS_ENFORCED`) no
longer counts. `internal` and `self_hosted_trusted` tenants are not gated.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `MAPI_CLOUDRUN_PROJECT` | — | Exec project holding the Job inventory (required) |
| `MAPI_CLOUDRUN_REGION` | `europe-west1` | Region of the inventory |
| `MAPI_CLOUDRUN_JOB_TEMPLATE` | `mapi-{env}-{base}-{class}` | Job id convention |
| `MAPI_CLOUDRUN_PLUGIN_BASES` | `mmm`, `tabular`: `base-bayes`; `sklearn`: `base-ml`; `ts`: `base-forecast` | Plugin namespace to base (JSON) |
| `MAPI_CLOUDRUN_DEFAULT_BASE` | `base-bayes` | Base for runs without a plugin |
| `MAPI_CLOUDRUN_SUBMITS_PER_MINUTE`, `MAPI_CLOUDRUN_SUBMIT_BURST` | `150`, `10` | Token bucket in front of `jobs.run` (at most 180) |
| `MAPI_CLOUDRUN_MAX_429_RETRIES` | `6` | `429` answers in a row before a submit fails |
| `MAPI_CLOUDRUN_BACKOFF_BASE_S`, `MAPI_CLOUDRUN_BACKOFF_CAP_S` | `1`, `64` | Exponential back-off after a `429` |
| `MAPI_CLOUDRUN_TIMEOUT_MARGIN_S` | `120` | Task timeout = run timeout + margin |
| `MAPI_CLOUDRUN_EGRESS_ENFORCED` | `true` | The exec network denies all egress |
| `MAPI_CLOUDRUN_DNS_ENFORCED` | `false` | The DNS response policy is verified for Jobs **[verify M0]**; public tenants need both |
| `MAPI_CLOUDRUN_ADOPT_LOOKBACK_S` | `93600` | How far back orphan adoption looks |
| `MAPI_PUBSUB_PUSH_AUDIENCE`, `MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT` | — | Expected push token (set both) |
| `MAPI_PUBSUB_PUSH_SUBSCRIPTIONS` | any | Accepted subscriptions (comma-separated) |
| `MAPI_GOOGLE_JWKS_URL` | Google's | Where the OIDC signing keys come from |
| `MAPI_SIGNER` | `local` | `local` (PEM key file) or `kms` |
| `MAPI_KMS_KEY_VERSION`, `MAPI_KMS_TRUSTED_KEY_VERSIONS` | — | Active and still-trusted key versions |
