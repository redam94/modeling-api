# Runbook: incident response (control plane)

The general flow for any control-plane incident. Specific incidents have their own runbook:
[sandbox escape](sandbox-escape.md), [stuck runs](stuck-runs.md),
[database failover](db-failover.md), [rotating keys](rotating-keys.md).

## 1. Declare and classify

- **SEV1** — data loss, a confirmed sandbox escape, cross-tenant data exposure, or the API
  down for all tenants. Page immediately; open a bridge.
- **SEV2** — degraded SLO (p95 time-to-RUNNING > 90 s for 30 min), one tenant blocked, or an
  alert that auto-recovery did not clear.
- **SEV3** — single-run failures, cosmetic, or a noisy alert.

Record start time, the alert that fired, and the owner in the incident channel.

## 2. Orient with the dashboards

Open the Grafana dashboard **modeling-api — control plane**
(`ops/dashboards/grafana-mapi-overview.json`) or its Cloud Monitoring twin. Read, in order:

1. **Fleet overview** — are runs flowing? Is anything STALLED? Spend vs cap? Integrity?
2. **Runs / Dispatch** — is the backlog in QUEUED (dispatch problem) or RUNNING (execution
   problem)?
3. **Events & finalize** — if `mapi_events_ingested_total{result="accepted"}` is flat while
   runs are live, the **event path** (Pub/Sub push → events service → Postgres) is the
   suspect, not the runs.
4. **Security & abuse** — sandbox violations, quota rejections, cost per tenant.

Logs are JSON; filter by `run_id`, `tenant_id` or `request_id` (echoed as `X-Request-Id`).
A log line with a span carries `trace_id`/`span_id` — pivot to the trace in your backend.

## 3. Stabilise

Pick the smallest lever that stops the bleeding:

| Symptom | Lever |
|---|---|
| Denial-of-wallet / runaway spend | Set the global spend cap, or flip the kill switch (step 4). |
| One tenant abusing compute | Tighten that tenant's quota policy (`PUT /v1/admin/quotas`); suspend the tenant if needed. |
| jobs.run 429s | Lower `MAPI_DISPATCH_RATE_PER_MINUTE`; shard across exec projects/regions (§8.8). |
| Event path down | See [stuck runs](stuck-runs.md); the executor status API is the source of truth, so healthy runs are not lost — only progress lags. |
| Suspected escape / integrity failure | Go straight to [sandbox escape](sandbox-escape.md). |

### Kill switches (plan §8.8, §9)

Two global switches deny with `QUOTA_EXCEEDED` at the global scope (no new error codes):

- `untrusted_execution_enabled = false` — pauses all uploaded/untrusted code paths.
- `global_spend_cap_micros` — denies new spend once exceeded.

Set them through the admin API (`PUT /v1/admin/kill-switches`, operator scope; a reason is
required and is audited). The switch takes effect at the next quota check: new runs that need
untrusted execution are denied and queued ones are held, but **in-flight runs continue**
unless the same update sets `cancel_untrusted_runs=true`, which also cancels every unfinished
run of uploaded code (use it for a suspected escape, not for spend).

To stop placing runs on one executor (any trust level), disable it:
`PUT /v1/admin/executors/{name}` with `{"enabled": false, "reason": ...}`
(`mapi admin executors disable <name>`). Orchestrator reports never re-enable it; enable it
again the same way.

## 4. Communicate

- Post a status update every 30 min (SEV1/2) with: what is known, what is being done, ETA.
- For tenant-visible impact, prepare a customer note (see sandbox-escape for the template).

## 5. Resolve and verify

- Confirm the firing alert has cleared and the dashboards are nominal for 15 min.
- Re-enable anything you disabled (kill switch, lowered rate) deliberately, one at a time.

## 6. Postmortem (SEV1/SEV2)

Blameless, within 3 business days: timeline, impact, root cause, what detected it, what the
fix was, and action items with owners. File follow-ups to close detection or recovery gaps.
