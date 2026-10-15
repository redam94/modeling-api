# Runbook: stuck runs (STALLED, stuck in a state, jobs.run 429)

Triggered by **MapiStalledRateHigh**, **MapiTimeToRunningSlow**, **MapiJobsRun429**,
**MapiNoProgressIngested**, or tenant reports that a run "isn't moving".

## Key principle (plan §8.3, §8.7)

**The executor status API is the source of truth for liveness, not event silence.** Event
silence only raises `STALLED`, which is a *warning* — a healthy run whose Logging→Pub/Sub path
is lagging must never be killed. So "stuck" almost always means one of:

1. the **event path** is lagging or down (progress not ingested), or
2. the **dispatch path** is throttled/backed up (runs sit in QUEUED), or
3. a real execution problem the reconciler will converge on its own.

Find out which from the dashboard **Runs** and **Events & finalize** rows before touching
anything.

## A. Many runs STALLED, but executions are alive

Symptom: `mapi_runs_active{state="stalled"}` high; `mapi_events_ingested_total{result="accepted"}`
low or flat.

1. Confirm the executions are actually running: the reconciler polls `executor.status` every
   `reconcile_interval_s`. In the logs, a run reported STALLED should still show the executor
   as RUNNING. If so, this is an **event-path** problem, not a run problem.
2. Check the path end to end:
   - **Cloud:** Logging sink (filter `jsonPayload.mapi_event`) → Pub/Sub subscription backlog
     (`num_undelivered_messages`) → the `events` Cloud Run service (push endpoint 5xx?) →
     Postgres. A Pub/Sub backlog or a failing push endpoint is the usual cause.
   - **Local/compose:** the orchestrator's event pump reads the supervisor stdout pipe; check
     the worker process is alive and the orchestrator log for ingest errors.
3. Do **not** cancel the runs. Once the event path recovers, dedupe on `(run, attempt, seq)`
   makes replay safe and STALLED clears on the next accepted event.
4. If a run is genuinely dead (executor says gone), the reconciler cancels-then-retries once
   the execution is confirmed gone; let it. If it is wedged, see section C.

## B. Runs pile up in QUEUED / slow time-to-RUNNING

Symptom: `mapi_runs_active{state="queued"}` growing; dispatch-latency or time-to-RUNNING p95
rising; possibly **MapiJobsRun429**.

1. Is the dispatcher running and leasing? Check the dispatcher log ("dispatcher … started")
   and that leases are being taken (`DISPATCHING` transitions).
2. **429s on jobs.run:** the token bucket keeps us under 180/min/region (§8.3). Sustained
   429s mean a region is saturated:
   - reduce pressure: lower `MAPI_DISPATCH_RATE_PER_MINUTE` so we back off cleanly;
   - spread load: shard across exec projects or regions at ~70% of quota (§8.8);
   - batch homogeneous children as one execution with `taskCount` (already done for sweeps).
3. **Cold starts:** large images (2–4 GB) make time-to-RUNNING spike. Confirm base images are
   warm/pinned; this is expected after a deploy and should settle.
4. **Placement refusals:** if runs fail straight to `INFRA_ERROR` at lease, the executor caps
   don't satisfy the tenant's trust tier (public needs isolation≥GVISOR, egress+DNS enforced).
   Check the enabled executors.

## C. A single run wedged in a non-terminal state

1. Look up the run and its attempt: state, `lease_owner`/`lease_expires_at`, `handle`,
   `violations`, `exec_message`.
2. If a lease is stale (owner gone, lease expired) the reconciler adopts or retries by nonce —
   give it one or two reconcile intervals.
3. If the executor reports the execution gone but the run didn't move, check the reconciler
   log for the specific run (`reconciling run … failed`). Fix the underlying error (storage
   reachability, etc.); the loop is level-triggered and idempotent, so it will converge once
   the dependency is healthy.
4. Last resort: cancel the run via the API (`POST /v1/runs/{id}:cancel`). This is safe — the
   reconciler confirms the execution is gone before CANCELLED, and a PARTIAL bundle is kept if
   the supervisor flushed checkpoints.

## D. FINALIZING not completing

A finalize job retries up to `finalize_max_attempts`, then fails the run as `infra`. If many
runs are stuck in FINALIZING, the object store or Postgres is likely unreachable — see
[database failover](db-failover.md) and check the store's `/readyz`.

## Escalate

If dispatch/events are healthy and runs still don't converge, escalate to the on-call
engineer with: the run ids, their states, the attempt rows, and the reconciler logs.
