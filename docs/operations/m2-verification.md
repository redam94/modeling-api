---
description: What was verified of the M2 exit criteria (cloud and multi-tenant core) on a laptop without Docker or a cloud project, the measured numbers, and the exact steps that remain for a real Google Cloud project.
---

# M2 verification <span class="mapi-badge">v0.3</span>

Milestone M2 (cloud and multi-tenant core) has five exit criteria:

1. 100 concurrent fits without loss.
2. Chaos and executor conformance green.
3. The security suite green each night.
4. p95 time-to-RUNNING below 90 s.
5. The cost of each run shown in the UI.

This page records how much of each criterion we verified **locally**. It also tells what
still needs a real Google Cloud project. The local run had no Docker daemon and no cloud
credentials:

- Cloud Storage was the GCS shim.
- Postgres was the embedded `pgserver`.
- The Cloud Run Jobs executor ran against its in-memory fake Admin API (`FakeCloudRun`).
- Locally signed OIDC tokens simulated the Pub/Sub pushes.
- We validated the Terraform, but we did not apply it.

We did the verification on 2026-10-15 at commit `feat/m2-e2e` (v0.3 development), on an
Apple-silicon laptop (8 cores, 16 GB, macOS).

## Summary

| Exit criterion | Verified locally | Result | Still needs real GCP |
|---|---|---|---|
| 100 concurrent fits without loss | `make load-test`: 100 fits through the real dispatcher, reconciler, finalizer and events path on the Cloud Run executor (fake Admin API) and the GCS shim. A second mode keeps all 100 executions live at the same time. A burst of 32 *real* fits ran on the live dev stack. | 100/100 succeeded, with one execution and one attempt for each run. 32/32 real fits succeeded, with one attempt, ledger entry and model version for each run. | The same burst on Cloud Run Jobs (real cold starts, real `jobs.run` quota) |
| Chaos green | `make chaos` | 5/5 passed | Cloud SQL failover. Pub/Sub redelivery from the real subscription |
| Executor conformance green | 12 cases on `fake-cloudrun` and on `local-process`, and the gate | 24 passed, 3 skipped (documented capabilities that `local-process` does not have), 0 failed | The 12 cases against the real Cloud Run Admin API (there is no live target yet, refer to the text below) |
| Security suite nightly green | `make security-suite EXECUTOR=local-process`. The nightly workflow is written. | PASS: all 17 outcomes are acceptable for `PROCESS` | Probes in a gVisor container (needs Docker) and in a Cloud Run Jobs execution (`MICROVM`) |
| p95 time-to-RUNNING < 90 s | Measured in the load test, and on the live stack | Fake Cloud Run: p95 **45.9–46.5 s** (100 fits). Live local stack: p95 **3.0 s** (16 real fits) and **8.9 s** (32) | The real cold start of a base image of about 2.5 GB **[verify M0]** adds more time (refer to the warning below) |
| Per-run cost shown in the UI | Playwright against the live stack: the run page, the runs list and the usage page show the metered cost | `$0.0003` metered on the run page and in the list. The usage totals agree with the ledger | The price table against real Cloud Run billing |

M2 also adds multi-tenancy. We verified these items from end to end on the live stack:

- Two users in two tenants, signed in through the development identity provider.
- The API and row-level security refuse cross-tenant access.
- Token scopes.
- Quotas.
- The spend-cap kill switch, which holds runs at dispatch.
- The cost of each run.
- The audit trail.

!!! warning "p95 time-to-RUNNING on real Cloud Run"
    For a burst of 100 fits, almost all of the 46 s p95 comes from the submit rate.
    `MAPI_CLOUDRUN_SUBMITS_PER_MINUTE=150` divides 100 submissions over about 40 s. The
    regional `jobs.run` quota is 180 a minute (**[verify M0]**). The fake provisions in
    0.3–1.5 s. On Cloud Run, the cold start of each execution adds to that time.

    The p95 cold start of a base image can be more than about 40 s. Then a cold burst of 100
    fits is longer than the 90 s budget. This is true also when each fit starts quickly. Measure
    the cold start first (refer to [What remains for real GCP](#what-remains-for-real-gcp)).
    Homogeneous fan-out (sweeps, SBC) uses one execution with `taskCount`, and thus it does
    not have this limit. Independent fits cannot do this.

## How each item was verified

### Load: 100 concurrent fits

```bash
make load-test                                    # default: fits of 0.2–3 s
LOAD_TEST_FIT_MS=40000-60000 make load-test       # 40–60 s fits: all 100 live at once
```

`packages/server/tests/test_load_cloudrun.py` submits 100 fits. They go through the
production rate limits: the token bucket of the dispatcher and the 150 a minute of the
executor. The backend makes errors on purpose:

- It answers the first `jobs.run` calls with 429.
- It loses two responses. This makes orphans that the executor must adopt. The executor must
  not submit them again.

A synthetic supervisor reads its bootstrap object. It writes its outputs through signed URLs
on the real GCS shim. Its events come as OIDC-authenticated Pub/Sub pushes, with random lag
and 5 % duplicates.

| Mode | Runs | p50 / p95 / max time-to-RUNNING | Most executions live at the same time | Pushes (duplicates) | Wall |
|---|---|---|---|---|---|
| default | 100/100 succeeded | 28.95 / **46.45** / 48.53 s | 9 | 525 (24) | 50.5 s |
| `LOAD_TEST_FIT_MS=40000-60000` | 100/100 succeeded | 28.47 / **45.93** / 48.09 s | **100** | 525 (24) | 105.8 s |

The two modes asserted exactly one execution and one attempt for each run (no double
dispatch). They also asserted that each run succeeded (no loss).

**Real fits on the live stack.** This test is opt-in, because each fit is a pair of processes
on the host:

```bash
make dev   # with the MAPI_* ports below
MAPI_E2E_BURST_RUNS=32 uv run pytest -q -s -m e2e tests/e2e/test_m2_burst_stack.py
```

| Burst | Result | p50 / p95 / max time-to-RUNNING | p95 queued → finished |
|---|---|---|---|
| 16 real store-demand fits | 16/16 succeeded | 1.32 / **3.0** / 3.23 s | 38.3 s |
| 32 real store-demand fits | 32/32 succeeded | 3.66 / **8.91** / 9.61 s | 82.0 s |

For each run, the test made sure of two things. The run has exactly one row in each of
`run_attempts`, `cost_ledger` and `model_versions` (counted directly in Postgres). The run
registered a different model version.

### Chaos

```bash
make chaos
```

5/5 passed:

- The test stopped the worker during PROVISIONING.
- The test stopped the worker during RUNNING.
- Event redelivery and a changed sequence of events.
- A redelivered event with a bad HMAC. This event never moves the run forward.
- A Postgres restart during a run.

Each case came to the correct terminal state, with no lost or duplicated attempt.

### Executor conformance

```bash
uv run pytest -q -rs packages/server/tests/test_executor_conformance.py
```

24 passed, 3 skipped. The 12 cases are:

- Submit.
- Orphan adoption after a dispatcher crash.
- Safety of a duplicate submit.
- Cancel during a run.
- Timeout.
- STALLED without a false LOST.
- Confirmed cancel before retry.
- Checkpoint resume.
- OOM classification.
- Fan-out with `taskCount`.
- Generation verification.
- 429 back-off.

All cases passed on `fake-cloudrun`. The gate test
(`test_conformance_report_gates_the_executor`) passed for the two backends.

On `local-process`, the suite skipped three cases. They need capabilities that this executor
does not declare: `memory_limit`, `fanout` and `remote_control_api`. The gate needs exactly
those skips.

The suite did not select the 12 cases of the `docker` backend, because there is no Docker
daemon.

### Security suite

```bash
make security-suite EXECUTOR=local-process
```

Result: `PASS: all 17 probe outcomes acceptable for this executor`. The tests of the suite
also passed (9). The outcomes agree with the [expected matrix](security-suite.md) for
`PROCESS` isolation. For this isolation, the suite accepts the two outcomes for the isolation
probes. But the executor must stop all platform probes:

| Probe | Category | Outcome |
|---|---|---|
| `metadata_token`, `http_egress`, `dns_egress` | isolation | blocked (no metadata server or egress on this host) |
| `write_outside_scratch`, `reuse_foreign_url`, `pth_injection`, `fork_bomb`, `memory_bomb`, `disk_bomb`, `log_flood` | isolation | allowed (accepted: `PROCESS` is not a security boundary) |
| `read_proc1_environ`, `read_supervisor_mem` | isolation | skipped (no procfs on macOS) |
| `forged_stdout_event`, `tampered_manifest`, `redos_schema`, `plot_xss_exfil`, `reuse_foreign_manifest` | platform | **blocked** |

The probes remove what they make. The suite removed the `.pth` file and the file that it
wrote outside scratch.

We repaired two defects in the suite:

- The docker and gVisor runner ran `python -m mapi_security` in the runner image, but the
  runner image never contains the suite. Now the suite is mounted read-only into the probe
  container.
- `--executor cloud` probed the host where it ran. Now it does not run outside a Cloud Run
  Jobs execution.

`.github/workflows/nightly.yml` schedules these jobs each night:

- Chaos.
- Executor conformance.
- The two load-test modes.
- The security suite on `local-process`.
- The security suite in gVisor (it installs `runsc` and builds the worker image).

We did a check of the workflow as YAML here. It has not run on GitHub yet.

### Multi-tenancy through the live stack

```bash
make e2e    # API suites (M1 + M2), then the browser @e2e suites
```

The M2 API suite, `tests/e2e/test_m2_tenancy_stack.py` (11 tests, about 2 minutes), does
these steps:

1. The platform operator makes two `internal` tenants. Each tenant has an invited admin, the
   dev accounts `analyst@dev.local` and `viewer@dev.local`.
2. Each admin signs in, which accepts the invitation, and makes project `growth`.
3. Each admin uploads data under the same dataset names. Each admin fits the scikit-learn
   store-demand model of the corpus and scores it.

On that state, the suite proves these items.

**Sign-in**

- The BFF flow works: `/auth/login`, the IdP, then `/auth/callback`, with PKCE S256.
- A replayed callback gets 401.
- A cookie mutation without `X-CSRF-Token`, or from a foreign origin, gets 403.
- Logout stops the session.
- ID-token bearers work.
- API tokens work and are bound to their project. After revocation, they do not work (401).

**API isolation.** The other tenant always gets 404 or 403, never a 2xx or a 5xx:

- On 18 project-scoped reads: datasets and versions, the preview, the data artifact, specs,
  runs, logs, run plots, models, versions, artifacts, `ui/` views and plots.
- On the SSE stream.
- On 10 writes: cancel, retry, resume, spec revision, dry-run, score, evaluate, fit, upload
  and new version.
- On tenant routes: tenant, projects, members, audit and usage.
- When the caller names the project of the other tenant explicitly.

Lists never show the rows of the other tenant. The same `dataset://growth/...` ref resolves
to the data of each tenant. The lineage dataset hashes are different.

**Row-level security, directly in Postgres** (as `mapi_app`)

- `FORCE ROW LEVEL SECURITY` is on for each table that we examined.
- A new connection without a tenant fails closed.
- With `app.tenant_id` set, each table shows the rows of the caller and none of the rows of
  the other tenant.
- The caller cannot see or update the run of the other tenant by id.
- If you move a row to a different tenant, this violates the policy.
- The setting is local to the transaction. After commit, a pooled connection sees nothing.

**Scopes**

- A `models:read` token can read. It gets 403 on fit, score and token minting, and on the
  project of a different tenant.
- A `runs:create` token can fit. The audit log shows that token as the source of the run.

**Quotas.** These two limits answer 429 `QUOTA_EXCEEDED`. They have no effect on the other
tenant:

- A tenant cap of `vcpu_seconds_per_day=1`.
- A token `vcpu_seconds_budget=1`.

**Kill switch.** This test pauses the worker for a short time (`SIGSTOP`). Thus a run is
always first in the queue. Then:

- The operator makes the tenant `verified` and sets `global_spend_cap_micros=1`.
- The queued run stays `queued` across many dispatcher polls. Thus the dispatcher holds it.
- New runs of that tenant get 429 with the spend-cap message.
- The dispatcher continues to admit and dispatch an `internal` tenant.
- When the tenant is `internal` again, the dispatcher leases the held run, and the run
  succeeds.

**Cost.** For each fit and score:

- `Run.cost` is metered (`estimated=false`, one attempt billed, `amount_micros > 0`, a price
  version, and vCPU- and GiB-seconds).
- There is one `actual` ledger row for each attempt, in the correct tenant.
- `GET /v1/usage` and the tenant usage are equal to the ledger or more.

**Audit**

- The tenant log has `project.create`, `run.create`, `token.create` and `token.revoke`.
- The platform log has `tenant.create`, `quota.update`, `kill_switch.update` and
  `auth.login`.
- The other tenant cannot read the log.

### The browser flow

```bash
MAPI_E2E=1 pnpm --filter @mapi/web exec playwright test e2e/m2-flow.e2e.spec.ts
```

Chromium runs the M2 browser flow, `apps/web/e2e/m2-flow.e2e.spec.ts` (4 tests):

1. Sign in through the account chooser of the IdP.
2. Use the tenant switcher. Change to the seeded tenant. The selection stays after a reload.
3. Fit from the spec editor and wait for `succeeded`. The run page shows
   `Metered · price table … · 1 attempt billed` and `cost $0.0003`. The runs list and the
   usage page also show the cost.
4. Mint a `models:read` token, then revoke it. Before revocation, the API refuses a fit with
   it (403). After revocation, the token gets 401.
5. In the admin console, do these steps:
    1. Make the tenant `verified`.
    2. Set a spend cap.
    3. The next fit of the tenant admin gets the spend-cap message.
    4. Remove the cap (type `REMOVE`).
    5. Find `kill_switch.update` and `tenant.update` in the audit log.

`tests/e2e/m2_browser_seed.py` seeds the tenant, and puts the stack back after the test. We
examined the screenshots in `apps/web/e2e/screenshots/m2-*.png` (git ignores them). They
caused these fixes:

- **Costs below one cent showed as `$0.00`.** A 3 s ingest cost $0.000047. The spend-cap
  badge for a 1-micro cap showed "$0.00 per day". Now amounts below $0.0001 keep two
  significant digits.
- **Session usage showed as "-" in "By token".** The group key of the server is `-`. Now the
  page shows the label "Signed-in users".
- **Memory showed "0 GiB·h".** Now it shows GiB-seconds below one hour.
- **A finished run without output showed "No output captured yet".**
- **The project switcher cut tenant and project names short**, also on wide screens.

### Infrastructure

```bash
make tf-validate
```

87 passed, 1 skipped: `shellcheck` is not installed. The tests include:

- `terraform fmt`.
- `validate` of each environment, with `-backend=false`.
- Module tests with mocked providers.
- The HCL invariants (for example digest-pinned images, the deny-all egress, the env-only
  overrides).
- The deploy scripts and the deploy workflow.

## Local stack used

A different process used the default port of the API. Thus the stack ran on other ports,
with a new data directory and two more dev accounts:

```bash
export MAPI_API_URL=http://127.0.0.1:18000 MAPI_GCS_ENDPOINT=http://127.0.0.1:14443 \
       MAPI_WEB_ORIGIN=http://127.0.0.1:15173 MAPI_PLOTS_ORIGIN=http://127.0.0.1:15174 \
       MAPI_DATA_DIR=$PWD/.local/m2e2e VITE_API_BASE=http://127.0.0.1:18000
make dev                     # dev profile: dev IdP, the default tenant, admin@dev.local as operator
make e2e                     # in another shell, with the same exports
```

`MAPI_E2E_ACCOUNTS` selects the two tenant admins of the API suite (default
`analyst@dev.local,viewer@dev.local`). `MAPI_E2E_UI_ACCOUNT` selects the tenant admin of the
browser flow (default `analyst@dev.local`). The two suites need a stack in the `dev` profile.
If not, they skip.

## Full verification set

| Check | Command | Result |
|---|---|---|
| Python suite | `make test` | 4141 passed, 11 skipped (optional cloud SDKs not installed, Linux-only `prctl`, Debian `cp`, `shellcheck`, the three documented `local-process` conformance skips), 45 deselected (`docker`, `cloud`, `e2e`) |
| Lint and import contracts | `make lint` | ruff and format clean (569 files). Import contracts: 4 kept, 0 broken |
| Type checks | `make typecheck` | mypy clean on each package (`--strict` for spec, gcs-shim, runner, server, client and cli) |
| Conformance corpus (tiny) | `make conformance` | 7/7 fixtures passed (e02, c08, e24, xgb and sklearn store demand, e33 and the model-owns-units MMM fixture) in 77 s |
| Web: lint, typecheck, unit, smoke, build | `make web-check` | web: 146 Vitest tests. Plot sandbox: 60. Playwright smoke: 26. The two production builds are OK |
| Docs | `make docs-build` (MkDocs `--strict`) | built with no warnings |
| End to end, M1 and M2, API and browser | `make e2e` | API suites: 28 passed, 1 skipped (the opt-in burst): M1 E33 stack, corpus on the stack, the docs walkthroughs, M2 tenancy. Browser `@e2e` suites: 14 passed: the stack smoke tests, the M1 flow and the M2 flow |

## Known gaps (not cloud-specific)

- **An operator records executor evidence manually.** Shared tiers need stored conformance
  and security-suite reports ([the shared-tier gate](cloud-run.md#the-shared-tier-gate)). But
  no job sends the nightly results to `PUT /v1/admin/executors/{name}/evidence` yet.
- **The nightly workflow has not run yet.** `.github/workflows/nightly.yml` has no remote to
  run on here. Its gVisor job (which installs `runsc` on the runner) is not tested.

## What remains for real GCP

All items below need a Google Cloud project. Use a temporary `dev` environment. Build it as
[Google Cloud deployment](gcp-deployment.md) tells. The values below are placeholders from
`infra/terraform/envs/dev/terraform.tfvars.example`:

```bash
export ENV=dev REGION=europe-west4
export CONTROL=acme-mapi-dev-control EXEC=acme-mapi-dev-exec DATA=acme-mapi-dev-data
export EXEC_SA=mapi-$ENV-exec@$EXEC.iam.gserviceaccount.com
export NET=mapi-$ENV-exec-paid SUBNET=mapi-$ENV-exec-paid-jobs
```

### 1. Stand the environment up

```bash
deploy/gcp/bootstrap.sh --env $ENV --region $REGION --control-project $CONTROL \
  --data-project $DATA --build-project acme-mapi-dev-build --exec-project paid=$EXEC \
  --state-project acme-mapi-dev-build
deploy/gcp/deploy.sh --env $ENV --foundation
deploy/gcp/build-push.sh --env $ENV
deploy/gcp/deploy.sh --env $ENV            # migrations, apply, smoke test
```

### 2. Work through the M0 checklist

The [M0 verification checklist](security-hardening.md#m0-verification-checklist) lists each
**[verify M0]** item, with its pass condition. The checklist has four areas:

- **DNS response policy:** the Direct VPC resolver obeys it, with root wildcard and bypass
  rules.
- **Network:** egress is really denied, and VPC-SC applies on Jobs.
- **Sandbox:** gen2 uid split with seccomp, `no_new_privs` and ptrace denial. Env-only
  overrides. Ephemeral disk and the in-memory filesystem limits. Bootstrap read-then-delete.
- **Platform:**
    - Quotas: 1,000 Jobs and 1,000 running executions in each region, 180 `jobs.run` a
      minute.
    - The Logging → Pub/Sub path and its labels.
    - Push to an internal service.
    - Pooled connections.
    - `BYPASSRLS` on Cloud SQL.
    - `runWithOverrides` without `actAs`.
    - Image indexes.

### 3. Cold start and p95 time-to-RUNNING

Measure one cold execution for each base image. Overrides change only env. Thus this
measures exactly what the dispatcher does:

```bash
for base in base-bayes base-ml base-forecast; do
  gcloud run jobs execute mapi-$ENV-$base-s --project $EXEC --region $REGION \
    --update-env-vars MAPI_RUN_ID=coldstart,MAPI_NONCE=none --wait --format='value(name)'
done
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name:mapi-$ENV-" \
  --project $EXEC --freshness 1h --format='table(timestamp,resource.labels.job_name,textPayload)'
```

The supervisor stops immediately, because it finds no bootstrap object. The cold start is the
time from the `createTime` of the execution to its first log line. Then run the burst
through the deployed API with a token that has the `admin` scope:

```bash
MAPI_API_URL=https://api.$ENV.example.com MAPI_TOKEN=<token> MAPI_PROJECT=<tenant>/<project> \
MAPI_E2E_BURST_RUNS=100 uv run pytest -q -s -m e2e tests/e2e/test_m2_burst_stack.py
```

The test asserts no loss or duplication (through the API) and a p95 time-to-RUNNING below
90 s. It prints the percentiles.

### 4. Security suite inside a Cloud Run Jobs execution

`--executor cloud` runs only in a Job execution. The runner images do not contain the suite.
Thus build a probe image on the deployed base, and run it as its own Job. Use the same
network and identity as the inventory Jobs:

```bash
REPO=$(terraform -chdir=infra/terraform/envs/$ENV output -raw image_repository)   # …/mapi-$ENV-images
IMAGE=$REPO/base-bayes@sha256:<digest from deploy/gcp/.out/$ENV/images.tfvars.json>
PROBE=$REPO/security-probe:$(git rev-parse --short HEAD)
printf 'FROM %s\nCOPY security/src /opt/mapi-security\nENV PYTHONPATH=/opt/mapi-security\n' "$IMAGE" \
  | docker build --platform linux/amd64 -t "$PROBE" -f - . && docker push "$PROBE"
gcloud run jobs create mapi-$ENV-security-probe --project $EXEC --region $REGION \
  --image "$PROBE" --command python --args=-m,mapi_security,run,--executor,cloud \
  --service-account $EXEC_SA --network $NET --subnet $SUBNET --network-tags mapi-exec \
  --vpc-egress all-traffic --execution-environment gen2 --max-retries 0 --task-timeout 15m
gcloud run jobs execute mapi-$ENV-security-probe --project $EXEC --region $REGION --wait
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=mapi-$ENV-security-probe" \
  --project $EXEC --freshness 1h --format='value(textPayload)'
```

Pass: `PASS: all 17 probe outcomes acceptable for this executor`. On `MICROVM`, each
isolation probe must be BLOCKED or SKIPPED. The DNS probe passes only if the authoritative
query log of the zone also never sees the name of the probe (checklist, network section).
After the test, delete the probe Job. To run this each night, add the probe image to
`build-push.sh` and the Job to `infra/terraform/modules/exec`. We have not done this yet.

### 5. Executor conformance against the real Admin API

There is no live conformance target yet.
`packages/server/tests/mapi_server_testkit/conformance.py` operates only `fake-cloudrun`,
`local-process` and `docker`. A `cloudrun-live` target needs a harness that does these
actions:

- It builds `CloudRunJobsExecutor` from settings (`MAPI_CLOUDRUN_PROJECT=$EXEC`, the dev
  inventory).
- It causes the faults through real means. It stops an execution with
  `gcloud run jobs executions cancel`. It causes an OOM with a run that is larger than class
  S. It causes a 429 when it sets `MAPI_CLOUDRUN_SUBMITS_PER_MINUTE` above the quota.
- It marks itself `cloud`.

Until then, run the chaos cases manually against the deployed stack:

```bash
# orphan adoption: scale the orchestrator worker pool (manual scaling) to 0 right after a
# submit, then back (`gcloud beta run worker-pools update --help` names the flag of your gcloud)
gcloud beta run worker-pools update mapi-$ENV-orchestrator --project $CONTROL --region $REGION --scaling 0
gcloud beta run worker-pools update mapi-$ENV-orchestrator --project $CONTROL --region $REGION --scaling 1
# Cloud SQL failover (regional/HA instance)
gcloud sql instances failover mapi-$ENV-pg --project $CONTROL
```

After each case, each run that the case affects must come to a terminal state, with exactly
one attempt for each execution.

### 6. Cost

Compare the `cost_ledger` of one day with the billing export for the exec project. If list
prices or discounts are different, adjust the price table (`MAPI_PRICE_TABLE_PATH`).
