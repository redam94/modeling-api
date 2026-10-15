# ops/ — dashboards, alerts and runbooks

Operational assets for running the modeling-api control plane (plan §8.8, §14 M2). Everything
here is driven by the metrics the server exposes at **`GET /metrics`**
(`mapi_server.observability.metrics`) and the structured JSON logs and OpenTelemetry traces
it emits (`mapi_server.observability.{logging,tracing}`).

```
ops/
├─ dashboards/
│  ├─ grafana-mapi-overview.json              Grafana dashboard (import; pick a Prometheus DS)
│  └─ cloud-monitoring-mapi-overview.json     Cloud Monitoring dashboard (gcloud / Terraform)
├─ alerts/
│  ├─ prometheus-rules.yml                    Prometheus recording + alerting rules
│  └─ cloud-monitoring-alerts.yaml            Cloud Monitoring alert policies (multi-doc YAML)
└─ runbooks/
   ├─ incident-response.md                    General triage flow; links the specific runbooks
   ├─ sandbox-escape.md                        Kill switch → rotate keys → quarantine → notify
   ├─ stuck-runs.md                            STALLED, QUEUED backlog, jobs.run 429
   ├─ db-failover.md                           Postgres outage / Cloud SQL failover
   └─ rotating-keys.md                         Bundle-signing + HMAC / exec token rotation
```

## Metrics the dashboards and alerts use

| Metric | Type | Meaning |
|---|---|---|
| `mapi_runs_total{kind,state}` | counter | Runs that reached a terminal state |
| `mapi_runs_active{state}` | gauge | Runs currently in each non-terminal state |
| `mapi_run_transitions_total{state}` | counter | Every state transition by target |
| `mapi_dispatch_latency_seconds` | histogram | QUEUED → executor accepted the submission |
| `mapi_time_to_running_seconds` | histogram | QUEUED → first event (RUNNING); SLO p95 < 90 s |
| `mapi_event_ingestion_lag_seconds` | histogram | Event timestamp → ingestion |
| `mapi_events_ingested_total{result}` | counter | Ingest outcomes (accepted/duplicate/bad_hmac/…) |
| `mapi_finalize_outcomes_total{kind,outcome}` | counter | succeeded / partial / integrity / infra |
| `mapi_integrity_failures_total{tenant_id}` | counter | Output-integrity failures (security signal) |
| `mapi_sandbox_violations_total{reason}` | counter | Forged events / HMAC / identity mismatch |
| `mapi_quota_rejections_total{scope,code}` | counter | QUOTA_EXCEEDED / RATE_LIMITED denials |
| `mapi_executor_submit_throttled_total{executor}` | counter | 429s from the executor backend (Cloud Run jobs.run), retried ones included; orchestrator |
| `mapi_cost_micros_total{tenant_id}` | counter | Billed compute cost in currency micros |
| `mapi_global_spend_micros` / `mapi_global_spend_cap_micros` | gauge | Spend vs the global cap |
| `mapi_sse_clients` | gauge | Live SSE subscribers on this process |
| `mapi_http_requests_total{method,route,status}` | counter | API requests (route *template*, not raw path) |
| `mapi_http_request_duration_seconds{method,route}` | histogram | API latency |

Two series referenced by the alerts are produced outside the server process:

- `mapi_security_probe_unexpected{executor}` — the nightly security suite
  (`python -m mapi_security run`, `security/README.md`) pushes this to a Pushgateway; a
  non-zero value means a probe's outcome disagreed with the expected matrix on an enforcing
  executor.
- `serviceruntime.googleapis.com/api/request_count` (resource `consumed_api`, service
  `run.googleapis.com`, method `…RunJob`, `response_code=429`) — GCP's own count of the
  Admin API's 429s to jobs.run, used by the Cloud Monitoring alert next to the server's
  `mapi_executor_submit_throttled_total`.

## Applying

**Grafana:** Dashboards → Import → upload `dashboards/grafana-mapi-overview.json`, select your
Prometheus data source. Scrape the API and worker `/metrics` (annotate pods/services, or add a
static scrape target).

**Prometheus alerts:** add `alerts/prometheus-rules.yml` to `rule_files:` (or convert to a
`PrometheusRule` CRD). Point `runbook` annotations at your rendered copy of `ops/runbooks/`.

**Cloud Monitoring:** import the dashboard with
`gcloud monitoring dashboards create --config-from-file=dashboards/cloud-monitoring-mapi-overview.json`
and each alert policy with
`gcloud alpha monitoring policies create --policy-from-file=…` (the YAML is multi-document —
split it, or manage via `google_monitoring_alert_policy` in Terraform). Requires Managed
Service for Prometheus scraping the `mapi_*` series. Replace `${PROJECT_ID}` and notification
channel ids first.

## Enabling the signals in the server

All on by default; tune via `MAPI_*` env (see `mapi_server.settings`):

- `MAPI_METRICS_ENABLED` — mount `/metrics` (default on).
- `MAPI_TRACING_ENABLED` — install the OpenTelemetry tracer provider (default on).
- `MAPI_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_ENDPOINT`) — OTLP/HTTP collector for span
  export; unset means spans are created (trace ids reach the logs) but not exported. Install
  the exporter with the `otlp` extra (`mapi-server[otlp]`).
- `MAPI_LOG_JSON` — JSON logs (default on); off gives a human console renderer for local dev.

Thresholds in the alert files are starting points for the private beta; tune them against the
§14 M2 exit criteria (100 concurrent fits without loss, p95 time-to-RUNNING < 90 s, security
suite nightly green).
