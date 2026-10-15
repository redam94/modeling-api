---
description: Metrics, structured logs and traces of the control plane, the dashboards, alerts and runbooks in ops/, and how to scrape /metrics safely.
---

# Observability <span class="mapi-badge">v0.3</span>

The API and the worker emit three signals:

- **Metrics:** Prometheus text at `GET /metrics` on every API process. The worker records
  the orchestrator's series (dispatch, reconciliation, finalization, pipe event ingestion) and
  serves them on `MAPI_WORKER_METRICS_PORT` when that is set.
- **Logs:** one JSON object per line on stderr. Each line carries `trace_id` and `span_id`,
  plus the request, run and tenant it belongs to.
- **Traces:** OpenTelemetry spans for every request, every outgoing HTTP call and every run's
  dispatch and finalization. Spans are exported over OTLP/HTTP only when a collector is
  configured.

Nothing is exported from inside a sandbox. Each execution gets the dispatching span's
`traceparent` in `MAPI_TRACEPARENT`, so the supervisor's own stderr diagnostics can be joined
to the run's trace without any telemetry leaving the exec project.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `MAPI_LOG_JSON` | `true` | JSON logs. `false` gives a readable console format for local work |
| `MAPI_METRICS_ENABLED` | `true` | Serve `GET /metrics` |
| `MAPI_METRICS_TOKEN` | unset | Bearer a scraper must present. Required in the `multi-tenant` profile |
| `MAPI_WORKER_METRICS_PORT`, `MAPI_WORKER_METRICS_HOST` | unset, `127.0.0.1` | Where the worker serves its own `/metrics` (same bearer rule) |
| `MAPI_TRACING_ENABLED` | `true` | Create spans (their ids reach the logs even without a collector) |
| `MAPI_OTLP_ENDPOINT` | unset | OTLP/HTTP collector (`OTEL_EXPORTER_OTLP_ENDPOINT` also works). Needs the `otlp` extra of `mapi-server` |

### Scraping `/metrics`

The series carry tenant ids and spend, so `/metrics` is not a public route. It authenticates
its own bearer, like the Pub/Sub push endpoint does:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: mapi-api
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/mapi-metrics-token
    static_configs:
      - targets: ["api.internal:8000", "worker.internal:9464"]
```

In the `multi-tenant` profile a server without `MAPI_METRICS_TOKEN` refuses every scrape
(401). The `single-tenant` and `dev` profiles serve `/metrics` openly on their loopback API
unless a token is set.

## The metric set

| Metric | Type | Meaning |
|---|---|---|
| `mapi_runs_total{kind,state}` | counter | Runs that reached a terminal state |
| `mapi_runs_active{state}` | gauge | Runs in each non-terminal state (set by the reconciler) |
| `mapi_run_transitions_total{state}` | counter | Every state transition, by target state |
| `mapi_dispatch_latency_seconds` | histogram | `QUEUED` until the executor accepted the submission |
| `mapi_time_to_running_seconds` | histogram | `QUEUED` until the first event (`RUNNING`); M2 target p95 < 90 s |
| `mapi_event_ingestion_lag_seconds` | histogram | Event timestamp until ingestion |
| `mapi_events_ingested_total{result}` | counter | Ingest outcomes (`accepted`, `duplicate`, `bad_hmac`, ...) |
| `mapi_finalize_outcomes_total{kind,outcome}` | counter | `succeeded`, `partial`, `integrity`, `infra` |
| `mapi_integrity_failures_total{tenant_id}` | counter | Outputs that did not match the platform's record (a security signal) |
| `mapi_sandbox_violations_total{reason}` | counter | Forged events, bad HMACs, execution-label mismatches |
| `mapi_quota_rejections_total{scope,code}` | counter | `QUOTA_EXCEEDED` and `RATE_LIMITED` denials, by token, tenant or global level |
| `mapi_executor_running_executions{executor}` | gauge | Runs holding a live execution on the executor, from the run table (every orchestrator reports the same fleet-wide value: aggregate with `max`). `MapiRunningExecutions70` alerts at 700, 70 % of Cloud Run's 1,000 per project and region |
| `mapi_executor_submit_throttled_total{executor}` | counter | `429` answers from the executor backend to a submission (Cloud Run `jobs.run`), retried ones included. Recorded by the orchestrator. The API's own `429`s to tenants are not counted here |
| `mapi_cost_micros_total{tenant_id}` | counter | Metered compute cost (currency micros) as ledger entries are written |
| `mapi_global_spend_micros`, `mapi_global_spend_cap_micros` | gauge | Spend in the kill switches' cap window, and the cap |
| `mapi_sse_clients` | gauge | Live event-stream subscribers on this process |
| `mapi_http_requests_total{method,route,status}` | counter | Requests, labelled by route *template* (never the raw path) |
| `mapi_http_request_duration_seconds{method,route}` | histogram | Request latency |

Every response carries `X-Request-Id`: the caller's own header when it sent one, otherwise the
trace id. Quote it when you report a problem; it finds the log lines and the trace.

## Dashboards, alerts and runbooks

Everything operators need is in `ops/` at the root of the repository:

- `ops/dashboards/`: a Grafana dashboard and a Cloud Monitoring dashboard over the series
  above.
- `ops/alerts/`: Prometheus rules and Cloud Monitoring policies. They cover jobs.run 429s, any
  integrity failure, a failed security probe, spend at 70 % and 90 % of the cap, Cloud Run
  running executions at 70 % of the quota, a rising
  `STALLED` rate, quota use at 70 %, slow time-to-`RUNNING` and a silent event path.
- `ops/runbooks/`: incident response, a confirmed sandbox escape (kill switch, rotate keys,
  quarantine, notify), stuck runs, database failover, and rotating the HMAC and signing keys.

`ops/README.md` explains how to import each one. The thresholds are starting points for the
private beta.

## Tests that guard the signals

| Command | What it checks |
|---|---|
| `make chaos` | A worker killed during `PROVISIONING` and `RUNNING`, Pub/Sub redelivery and reordering, bad-HMAC redelivery, and a PostgreSQL restart mid-run. Every run converges to one outcome, with one attempt and no duplicate execution |
| `make load-test` | 100 concurrent fits through the real dispatcher, reconciler and finalizer on the fake Cloud Run backend, with injected 429s, lost responses and duplicate pushes. No run is lost, and p95 time-to-`RUNNING` stays under 90 s |
| `make security-suite` | The sandbox-escape probes (see [Security suite](security-suite.md)) |

All three run offline, against the embedded PostgreSQL and the in-memory or shim object store.
