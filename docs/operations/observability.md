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

Generated from `mapi_server.observability.metrics.Metrics`, the one registry every series is
defined in, so this table cannot drift from what `/metrics` serves. Label cardinality is bounded
on purpose: `tenant_id` is the only unbounded label, on the two counters that are meaningless
without it, and HTTP series are labelled by route *template*, never the raw path.

<!-- generated: metrics -->

What to watch first:

- **`mapi_time_to_running_seconds`**: the M2 target is p95 under 90 s.
- **`mapi_integrity_failures_total`** and **`mapi_sandbox_violations_total`**: any increase is a
  security signal, not a reliability one.
- **`mapi_global_spend_micros`** against **`mapi_global_spend_cap_micros`**: the kill switch's
  window.
- **`mapi_runs_active{state="stalled"}`**: runs whose events went quiet (the executor's status,
  not the silence, decides what happens to them).

The security suite adds one more series from outside the server,
`mapi_security_probe_unexpected{executor}`, through a textfile collector or a Pushgateway
([Security suite](security-suite.md#run-it)).

Every response carries `X-Request-Id`: the caller's own header when it sent one, otherwise the
trace id. Quote it when you report a problem; it finds the log lines and the trace.

## Dashboards, alerts and runbooks

Everything operators need is in `ops/` at the root of the repository; `ops/README.md` explains
how to import each one. The tables below are generated from those files.

### Dashboards

A Grafana dashboard and a Cloud Monitoring dashboard over the same series:

<!-- generated: dashboards -->

### Alerts

`ops/alerts/prometheus-rules.yml` (Prometheus rules, with recording rules such as
`mapi:global_spend_ratio`) and `ops/alerts/cloud-monitoring-alerts.yaml` (the same policies for
Cloud Monitoring). *page* wakes someone; *ticket* is for the next working hour. The thresholds
are starting points for the private beta.

<!-- generated: alerts -->

### Runbooks

Each alert names the runbook that resolves it:

<!-- generated: runbooks -->

The sandbox-escape runbook is the plan's incident response for a confirmed escape: turn off
uploaded code with the [kill switch](../guides/administration.md#kill-switches), rotate the
signing and HMAC keys (which invalidates every outstanding signed URL), quarantine the affected
bundles and notify the tenants.

## Tests that guard the signals

| Command | What it checks |
|---|---|
| `make chaos` | A worker killed during `PROVISIONING` and `RUNNING`, Pub/Sub redelivery and reordering, bad-HMAC redelivery, and a PostgreSQL restart mid-run. Every run converges to one outcome, with one attempt and no duplicate execution |
| `make load-test` | 100 concurrent fits through the real dispatcher, reconciler and finalizer on the fake Cloud Run backend, with injected 429s, lost responses and duplicate pushes. No run is lost, and p95 time-to-`RUNNING` stays under 90 s |
| `make security-suite` | The sandbox-escape probes (see [Security suite](security-suite.md)) |

All three run offline, against the embedded PostgreSQL and the in-memory or shim object store.
