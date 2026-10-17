---
description: What was verified of the M2 exit criteria (cloud and multi-tenant core) on a laptop without Docker or a cloud project, the measured numbers, and the exact steps that remain for a real Google Cloud project.
---

# M2 verification <span class="mapi-badge">v0.3</span>

Milestone M2 (cloud and multi-tenant core) has five exit criteria:

1. 100 concurrent fits without loss;
2. chaos and executor conformance green;
3. the security suite green nightly;
4. p95 time-to-RUNNING under 90 s;
5. per-run cost shown in the UI.

This page records how far each one was verified **locally** and what still needs a real Google
Cloud project. The local run had no Docker daemon and no cloud credentials:

- Cloud Storage was the GCS shim;
- Postgres was the embedded `pgserver`;
- the Cloud Run Jobs executor ran against its in-memory fake Admin API (`FakeCloudRun`);
- Pub/Sub pushes were simulated with locally signed OIDC tokens;
- Terraform was validated, never applied.

Verified on 2026-10-15 at commit `feat/m2-e2e` (v0.3 development), on an Apple-silicon laptop
(8 cores, 16 GB, macOS).

## Summary

| Exit criterion | Verified locally | Result | Still needs real GCP |
|---|---|---|---|
| 100 concurrent fits without loss | `make load-test`: 100 fits through the real dispatcher, reconciler, finalizer and events path on the Cloud Run executor (fake Admin API) and the GCS shim. A second mode keeps all 100 executions live at once. A burst of 32 *real* fits ran on the live dev stack. | 100/100 succeeded, one execution and one attempt per run. 32/32 real fits succeeded, with one attempt, ledger entry and model version per run. | The same burst on Cloud Run Jobs (real cold starts, real `jobs.run` quota) |
| Chaos green | `make chaos` | 5/5 passed | Cloud SQL failover; Pub/Sub redelivery from the real subscription |
| Executor conformance green | 12 cases on `fake-cloudrun` and on `local-process`, plus the gate | 24 passed, 3 skipped (documented capabilities `local-process` lacks), 0 failed | The 12 cases against the real Cloud Run Admin API (no live target exists yet, see below) |
| Security suite nightly green | `make security-suite EXECUTOR=local-process`. The nightly workflow is written. | PASS: all 17 outcomes acceptable for `PROCESS` | Probes inside a gVisor container (needs Docker) and inside a Cloud Run Jobs execution (`MICROVM`) |
| p95 time-to-RUNNING < 90 s | Measured in the load test, and on the live stack | Fake Cloud Run: p95 **45.9–46.5 s** (100 fits). Live local stack: p95 **3.0 s** (16 real fits) and **8.9 s** (32) | Real cold start of a ~2.5 GB base image **[verify M0]** comes on top (see the warning below) |
| Per-run cost shown in the UI | Playwright against the live stack: the run page, the runs list and the usage page show the metered cost | `$0.0003` metered on the run page and in the list; usage totals match the ledger | The price table against real Cloud Run billing |

Multi-tenancy, which the M2 scope adds, was also verified end to end on the live stack:

- two users in two tenants, signed in through the development identity provider;
- cross-tenant access refused at the API and by row-level security;
- token scopes;
- quotas;
- the spend-cap kill switch, held at dispatch;
- per-run cost;
- the audit trail.

!!! warning "p95 time-to-RUNNING on real Cloud Run"
    For a burst of 100 fits, the 46 s p95 comes almost entirely from the submit rate:
    `MAPI_CLOUDRUN_SUBMITS_PER_MINUTE=150` (the regional `jobs.run` quota is 180 a minute,
    **[verify M0]**) spreads 100 submissions over about 40 s. The fake provisions in
    0.3–1.5 s. On Cloud Run, every execution's cold start adds to that. If the p95 cold start
    of a base image is over about 40 s, a cold 100-fit burst misses the 90 s budget even
    though every single fit starts quickly. Measure the cold start first (see
    [What remains for real GCP](#what-remains-for-real-gcp)). Homogeneous fan-out (sweeps,
    SBC) avoids the limit by using one execution with `taskCount`; independent fits cannot.

## How each item was verified

### Load: 100 concurrent fits

```bash
make load-test                                    # default: fits of 0.2–3 s
LOAD_TEST_FIT_MS=40000-60000 make load-test       # 40–60 s fits: all 100 live at once
```

`packages/server/tests/test_load_cloudrun.py` submits 100 fits. They go through the
production rate limits: the dispatcher's token bucket and the executor's 150 a minute. The
backend misbehaves on purpose:

- it answers the first `jobs.run` calls with 429;
- it loses two responses, which leaves orphans the executor must adopt instead of
  resubmitting.

A synthetic supervisor reads its bootstrap object and writes its outputs through signed URLs
on the real GCS shim. Its events arrive as OIDC-authenticated Pub/Sub pushes, with random lag
and 5 % duplicates.

| Mode | Runs | p50 / p95 / max time-to-RUNNING | Most executions live at once | Pushes (duplicates) | Wall |
|---|---|---|---|---|---|
| default | 100/100 succeeded | 28.95 / **46.45** / 48.53 s | 9 | 525 (24) | 50.5 s |
| `LOAD_TEST_FIT_MS=40000-60000` | 100/100 succeeded | 28.47 / **45.93** / 48.09 s | **100** | 525 (24) | 105.8 s |

Both modes asserted exactly one execution and one attempt per run (no double dispatch), and
that every run succeeded (no loss).

**Real fits on the live stack.** This is opt-in, because each fit is a pair of processes on
the host:

```bash
make dev   # with the MAPI_* ports below
MAPI_E2E_BURST_RUNS=32 uv run pytest -q -s -m e2e tests/e2e/test_m2_burst_stack.py
```

| Burst | Result | p50 / p95 / max time-to-RUNNING | p95 queued → finished |
|---|---|---|---|
| 16 real store-demand fits | 16/16 succeeded | 1.32 / **3.0** / 3.23 s | 38.3 s |
| 32 real store-demand fits | 32/32 succeeded | 3.66 / **8.91** / 9.61 s | 82.0 s |

For each run, the test checked that it has exactly one row in each of `run_attempts`,
`cost_ledger` and `model_versions` (counted straight in Postgres), and that it registered a
distinct model version.

### Chaos

```bash
make chaos
```

5/5 passed:

- the worker killed during PROVISIONING;
- the worker killed during RUNNING;
- event redelivery and reordering;
- a redelivered event with a bad HMAC that never advances the run;
- a Postgres restart mid-run.

Each converged to the correct terminal state with no lost or duplicated attempt.

### Executor conformance

```bash
uv run pytest -q -rs packages/server/tests/test_executor_conformance.py
```

24 passed, 3 skipped. The 12 cases are:

- submit;
- orphan adoption after a dispatcher crash;
- duplicate-submit safety;
- cancel mid-run;
- timeout;
- STALLED without a false LOST;
- confirmed cancel before retry;
- checkpoint resume;
- OOM classification;
- fan-out with `taskCount`;
- generation verification;
- 429 back-off.

Every case passed on `fake-cloudrun`. The gate test (`test_conformance_report_gates_the_executor`)
passed for both backends.

On `local-process`, three cases were skipped. They need capabilities that executor does not
declare: `memory_limit`, `fanout` and `remote_control_api`. The gate requires exactly those
skips.

The 12 cases of the `docker` backend were deselected, because there is no Docker daemon.

### Security suite

```bash
make security-suite EXECUTOR=local-process
```

Result: `PASS: all 17 probe outcomes acceptable for this executor`, and the suite's own
tests passed (9). The outcomes match the [expected matrix](security-suite.md) for `PROCESS`
isolation, where either outcome is accepted for the isolation probes but every platform probe
must be blocked:

| Probe | Category | Outcome |
|---|---|---|
| `metadata_token`, `http_egress`, `dns_egress` | isolation | blocked (no metadata server or egress on this host) |
| `write_outside_scratch`, `reuse_foreign_url`, `pth_injection`, `fork_bomb`, `memory_bomb`, `disk_bomb`, `log_flood` | isolation | allowed (accepted: `PROCESS` is not a security boundary) |
| `read_proc1_environ`, `read_supervisor_mem` | isolation | skipped (no procfs on macOS) |
| `forged_stdout_event`, `tampered_manifest`, `redos_schema`, `plot_xss_exfil`, `reuse_foreign_manifest` | platform | **blocked** |

The probes clean up after themselves: the `.pth` file and the file written outside scratch
were removed.

Two defects in the suite were fixed along the way:

- The docker and gVisor runner ran `python -m mapi_security` in the runner image, which never
  contains the suite. The suite is now mounted read-only into the probe container.
- `--executor cloud` used to probe whatever host ran it. It now refuses to run outside a
  Cloud Run Jobs execution.

`.github/workflows/nightly.yml` schedules the following jobs every night:

- chaos;
- executor conformance;
- both load-test modes;
- the security suite on `local-process`;
- the security suite inside gVisor (it installs `runsc` and builds the worker image).

The workflow was checked as YAML here; it has not run on GitHub yet.

### Multi-tenancy through the live stack

```bash
make e2e    # API suites (M1 + M2), then the browser @e2e suites
```

The M2 API suite, `tests/e2e/test_m2_tenancy_stack.py` (11 tests, about 2 minutes), runs
the following steps:

1. The platform operator creates two `internal` tenants. Each has an invited admin, the dev
   accounts `analyst@dev.local` and `viewer@dev.local`.
2. Each admin signs in, which claims the invitation, and creates project `growth`.
3. Each admin uploads data under the same dataset names, fits the corpus's scikit-learn
   store-demand model and scores it.

On that state, the suite proves the following.

**Sign-in**

- The BFF flow works: `/auth/login`, the IdP, then `/auth/callback`, with PKCE S256.
- A replayed callback gets 401.
- A cookie mutation without `X-CSRF-Token`, or from a foreign origin, gets 403.
- Logout ends the session.
- ID-token bearers work.
- API tokens work, are bound to their project, and are dead (401) after revocation.

**API isolation.** The other tenant always gets 404 or 403, never a 2xx or a 5xx:

- on 18 project-scoped reads: datasets and versions, the preview, the data artifact, specs,
  runs, logs, run plots, models, versions, artifacts, `ui/` views and plots;
- on the SSE stream;
- on 10 writes: cancel, retry, resume, spec revision, dry-run, score, evaluate, fit, upload
  and new version;
- on tenant routes: tenant, projects, members, audit and usage;
- when the caller names the other tenant's project explicitly.

Lists never show the other tenant's rows. The same `dataset://growth/...` ref resolves to
each tenant's own data: the lineage dataset hashes differ.

**Row-level security, straight in Postgres** (as `mapi_app`)

- `FORCE ROW LEVEL SECURITY` is on for every table checked.
- A fresh connection without a tenant fails closed.
- With `app.tenant_id` set, each table shows the caller's own rows and none of the other
  tenant's.
- The other tenant's run is neither visible nor updatable by id.
- Moving a row to another tenant violates the policy.
- The setting is transaction-local: after commit, a pooled connection sees nothing.

**Scopes**

- A `models:read` token can read but gets 403 on fit, score and token minting, and on another
  tenant's project.
- A `runs:create` token fits, and the audit log attributes the run to that token.

**Quotas.** Both answer 429 `QUOTA_EXCEEDED`, and the other tenant is unaffected:

- a tenant cap of `vcpu_seconds_per_day=1`;
- a token's `vcpu_seconds_budget=1`.

**Kill switch.** This test briefly pauses the worker (`SIGSTOP`) so a run is
deterministically queued first. Then:

- the operator makes the tenant `verified` and sets `global_spend_cap_micros=1`;
- the queued run stays `queued` across several dispatcher polls, so it is held at dispatch;
- new runs of that tenant get 429 with the spend-cap message;
- an `internal` tenant keeps being admitted and dispatched;
- once the tenant is `internal` again, the held run is leased and succeeds.

**Cost.** For every fit and score:

- `Run.cost` is metered (`estimated=false`, one attempt billed, `amount_micros > 0`, a price
  version, and vCPU- and GiB-seconds);
- there is one `actual` ledger row per attempt, in the right tenant;
- `GET /v1/usage` and the tenant usage add up to at least the ledger.

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

1. Sign-in through the IdP's account chooser.
2. The tenant switcher: switch to the seeded tenant; the choice survives a reload.
3. Fit from the spec editor and wait for `succeeded`. The run page shows
   `Metered · price table … · 1 attempt billed` and `cost $0.0003`; the runs list and the
   usage page show the cost too.
4. Mint a `models:read` token, then revoke it. Before revocation, the API refuses it a fit
   (403); after revocation, the token gets 401.
5. Admin console:
    1. make the tenant `verified`;
    2. set a spend cap;
    3. the tenant admin's next fit is refused with the spend-cap message;
    4. remove the cap (typing `REMOVE`);
    5. find `kill_switch.update` and `tenant.update` in the audit log.

`tests/e2e/m2_browser_seed.py` seeds the tenant and puts the stack back afterwards. The
screenshots in `apps/web/e2e/screenshots/m2-*.png` (git-ignored) were reviewed. They led to
these fixes:

- **Sub-cent costs showed as `$0.00`.** A 3 s ingest cost $0.000047, and the spend-cap badge
  for a 1-micro cap read "$0.00 per day". Amounts below $0.0001 now keep two significant
  digits.
- **Session usage was listed as "-" in "By token".** The server's group key is `-`; the page
  now labels it "Signed-in users".
- **Memory showed "0 GiB·h".** It is now in GiB-seconds under an hour.
- **A finished run without output said "No output captured yet".**
- **The project switcher truncated tenant and project names** even on wide screens.

### Infrastructure

```bash
make tf-validate
```

87 passed, 1 skipped: `shellcheck` is not installed. This covers:

- `terraform fmt`;
- `validate` of every environment, with `-backend=false`;
- module tests with mocked providers;
- the HCL invariants (for example digest-pinned images, the deny-all egress, the env-only
  overrides);
- the deploy scripts and the deploy workflow.

## Local stack used

The API's default port was taken, so the stack ran on other ports, with a fresh data
directory and two extra dev accounts:

```bash
export MAPI_API_URL=http://127.0.0.1:18000 MAPI_GCS_ENDPOINT=http://127.0.0.1:14443 \
       MAPI_WEB_ORIGIN=http://127.0.0.1:15173 MAPI_PLOTS_ORIGIN=http://127.0.0.1:15174 \
       MAPI_DATA_DIR=$PWD/.local/m2e2e VITE_API_BASE=http://127.0.0.1:18000
make dev                     # dev profile: dev IdP, the default tenant, admin@dev.local as operator
make e2e                     # in another shell, with the same exports
```

`MAPI_E2E_ACCOUNTS` selects the two tenant admins of the API suite (default
`analyst@dev.local,viewer@dev.local`). `MAPI_E2E_UI_ACCOUNT` selects the browser flow's
tenant admin (default `analyst@dev.local`). Both suites need a stack in the `dev` profile,
and skip otherwise.

## Full verification set

| Check | Command | Result |
|---|---|---|
| Python suite | `make test` | 4141 passed, 11 skipped (optional cloud SDKs not installed, Linux-only `prctl`, Debian `cp`, `shellcheck`, the three documented `local-process` conformance skips), 45 deselected (`docker`, `cloud`, `e2e`) |
| Lint and import contracts | `make lint` | ruff and format clean (569 files); import contracts 4 kept, 0 broken |
| Type checks | `make typecheck` | mypy clean on every package (`--strict` for spec, gcs-shim, runner, server, client and cli) |
| Conformance corpus (tiny) | `make conformance` | 7/7 fixtures passed (e02, c08, e24, xgb and sklearn store demand, e33 and the model-owns-units MMM fixture) in 77 s |
| Web: lint, typecheck, unit, smoke, build | `make web-check` | web: 146 Vitest tests; plot sandbox: 60; Playwright smoke: 26; both production builds OK |
| Docs | `make docs-build` (MkDocs `--strict`) | built with no warnings |
| End to end, M1 and M2, API and browser | `make e2e` | API suites 28 passed, 1 skipped (the opt-in burst): M1 E33 stack, corpus on the stack, the docs walkthroughs, M2 tenancy. Browser `@e2e` suites 14 passed: the stack smoke tests, the M1 flow and the M2 flow |

## Known gaps (not cloud-specific)

- **Executor evidence is recorded by hand.** Shared tiers are gated on stored conformance and
  security-suite reports ([the shared-tier gate](cloud-run.md#the-shared-tier-gate)), but no
  job posts the nightly results to `PUT /v1/admin/executors/{name}/evidence` yet: an operator
  records them.
- **The nightly workflow has not run yet.** `.github/workflows/nightly.yml` has no remote
  to run on here, and its gVisor job (installing `runsc` on the runner) is untested.

## What remains for real GCP

Everything below needs a Google Cloud project. Use a throw-away `dev` environment, built as
described in [Google Cloud deployment](gcp-deployment.md). The values below are placeholders
taken from `infra/terraform/envs/dev/terraform.tfvars.example`:

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

Every **[verify M0]** item is listed, with its pass condition, in the
[M0 verification checklist](security-hardening.md#m0-verification-checklist). The checklist
covers four areas:

- **DNS response policy:** honoured by the Direct VPC resolver, with root wildcard and
  bypass rules.
- **Network:** egress really denied, VPC-SC on Jobs.
- **Sandbox:** gen2 uid split with seccomp, `no_new_privs` and ptrace denial; env-only
  overrides; ephemeral disk and the in-memory filesystem limits; bootstrap read-then-delete.
- **Platform:**
    - quotas: 1,000 Jobs and 1,000 running executions per region, 180 `jobs.run` a minute;
    - the Logging → Pub/Sub path and its labels;
    - push to an internal service;
    - pooled connections;
    - `BYPASSRLS` on Cloud SQL;
    - `runWithOverrides` without `actAs`;
    - image indexes.

### 3. Cold start and p95 time-to-RUNNING

Measure one cold execution per base image. Overrides only change env, so this measures exactly
what the dispatcher does:

```bash
for base in base-bayes base-ml base-forecast; do
  gcloud run jobs execute mapi-$ENV-$base-s --project $EXEC --region $REGION \
    --update-env-vars MAPI_RUN_ID=coldstart,MAPI_NONCE=none --wait --format='value(name)'
done
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name:mapi-$ENV-" \
  --project $EXEC --freshness 1h --format='table(timestamp,resource.labels.job_name,textPayload)'
```

The supervisor exits at once (it finds no bootstrap object); the time from the execution's
`createTime` to its first log line is the cold start. Then run the burst through the
deployed API with a token that has the `admin` scope:

```bash
MAPI_API_URL=https://api.$ENV.example.com MAPI_TOKEN=<token> MAPI_PROJECT=<tenant>/<project> \
MAPI_E2E_BURST_RUNS=100 uv run pytest -q -s -m e2e tests/e2e/test_m2_burst_stack.py
```

It asserts no loss or duplication (through the API) and p95 time-to-RUNNING under 90 s, and
prints the percentiles.

### 4. Security suite inside a Cloud Run Jobs execution

`--executor cloud` only runs inside a Job execution. The runner images do not ship the suite,
so build a probe image on top of the deployed base and run it as its own Job, with the same
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

Pass: `PASS: all 17 probe outcomes acceptable for this executor`. On `MICROVM`, every
isolation probe must be BLOCKED or SKIPPED. The DNS probe passes only if the zone's
authoritative query log also never sees the probe's name (checklist, network section).
Delete the probe Job afterwards. Making this nightly means adding the probe image to
`build-push.sh` and the Job to `infra/terraform/modules/exec`. That is not done yet.

### 5. Executor conformance against the real Admin API

There is no live conformance target yet. `packages/server/tests/mapi_server_testkit/conformance.py`
drives `fake-cloudrun`, `local-process` and `docker` only. A `cloudrun-live` target needs a
harness that:

- builds `CloudRunJobsExecutor` from settings (`MAPI_CLOUDRUN_PROJECT=$EXEC`, the dev
  inventory);
- injects the faults through real means: kill an execution with
  `gcloud run jobs executions cancel`; an OOM with a run that exceeds class S; a 429 by
  lowering `MAPI_CLOUDRUN_SUBMITS_PER_MINUTE` above the quota;
- marks itself `cloud`.

Until then, run the chaos cases by hand against the deployed stack:

```bash
# orphan adoption: scale the orchestrator worker pool (manual scaling) to 0 right after a
# submit, then back (`gcloud beta run worker-pools update --help` names the flag of your gcloud)
gcloud beta run worker-pools update mapi-$ENV-orchestrator --project $CONTROL --region $REGION --scaling 0
gcloud beta run worker-pools update mapi-$ENV-orchestrator --project $CONTROL --region $REGION --scaling 1
# Cloud SQL failover (regional/HA instance)
gcloud sql instances failover mapi-$ENV-pg --project $CONTROL
```

After each one, every affected run must reach a terminal state with exactly one attempt per
execution.

### 6. Cost

Compare a day's `cost_ledger` with the billing export for the exec project. Then adjust the
price table (`MAPI_PRICE_TABLE_PATH`) if list prices or discounts differ.
