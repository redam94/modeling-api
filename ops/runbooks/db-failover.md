# Runbook: database (Postgres) failover / outage

Triggered by **MapiNoProgressIngested**, `/readyz` reporting `postgres: fail`, a Cloud SQL
failover event, or widespread 5xx. Postgres is the one stateful service (plan §8.2): runs,
the queue (procrastinate), `run_progress`, `run_events`, lineage and the cost ledger all live
there.

## What stays safe during a DB outage

- **Executions keep running.** The supervisor/worker in each execution do not talk to
  Postgres; they upload outputs via signed URLs and emit events to Logging/Pub/Sub. A fit
  in progress is not lost by a control-plane DB blip.
- **Events are durable in transit.** Pub/Sub retains undelivered messages; the events service
  will catch up once the DB is back. Dedupe on `(run, attempt, seq)` makes redelivery safe.
- **The reconciler is level-triggered and idempotent.** When the DB returns it re-reads state
  and converges — adopting orphans by nonce, resuming FINALIZING, etc.

So the priority is **restore the DB and let the loops converge**, not to intervene per run.

## 1. Confirm and classify

- Hit `/readyz` on the API and worker — which dependency is `fail`?
- Cloud SQL: check the instance state and whether an HA failover already happened
  (primary → standby). Managed failover usually completes in ~60–120 s.
- Is it connectivity (pooler/network) or the instance itself?

## 2. If HA failover is available (cloud)

1. Trigger/confirm the failover to the standby (Cloud SQL does this automatically for HA
   instances; a manual `gcloud sql instances failover` is the backstop).
2. Connections drop; the app uses `pool_pre_ping=True`, so stale connections are discarded and
   re-established against the new primary. No app restart is normally needed.
3. Watch `mapi_events_ingested_total{result="accepted"}` resume and the QUEUED backlog drain.

## 3. If the instance is down with no HA (or local/compose)

- **Compose / local (`pgserver`):** restart the database process; the app reconnects via
  pre-ping. For the embedded `pgserver`, `make stop` then `make dev` brings it back under the
  same data directory.
- **Cloud, no standby:** restore from the latest automated backup / PITR to a new instance,
  then repoint `DATABASE_URL` (or the Cloud SQL connection) and roll the API + worker.

## 4. Protect Postgres from the reconnect stampede

When the DB returns, every API/worker instance reconnects and the dispatcher resumes at once.

- Per-service connection budgets are set by `MAPI_DB_POOL_SIZE`; keep the sum under the
  instance's `max_connections` (use the managed pooler in cloud).
- If the pooler is the bottleneck, bring workers back before API replicas so the queue drains
  before user traffic resumes.

## 5. Verify convergence

- `/readyz` is `ok` on API and worker.
- QUEUED backlog drains; no runs stuck in DISPATCHING with expired leases after a few
  reconcile intervals.
- `run_progress` updates resume (dashboard **Events & finalize**).
- Spot-check that no run was double-submitted: the intent row `(run, attempt, nonce)` plus
  `executor.adopt` by nonce prevents duplicate executions even across the outage.

## 6. Data integrity note

Because the dispatcher writes the intent row **before** calling the executor and the reconciler
adopts by nonce, a crash mid-dispatch cannot create two executions for one attempt. If a PITR
restore rewinds past some submissions, the reconciler will find the live executions by nonce
and adopt them; executions with no matching attempt row are cancelled as orphans.
