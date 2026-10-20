---
description: Metrics, structured logs and traces of the control plane, the dashboards, alerts and runbooks in ops/, and how to scrape /metrics safely.
---

# Observability <span class="mapi-badge">v0.3</span>

The API and the worker send three signals:

- **Metrics:** Prometheus text at `GET /metrics` on each API process. The worker records the
  series of the orchestrator (dispatch, reconciliation, finalization, pipe event ingestion).
  When `MAPI_WORKER_METRICS_PORT` is set, the worker serves them on that port.
- **Logs:** one JSON object on each line on stderr. Each line has `trace_id` and `span_id`,
  and the request, run and tenant that it is related to.
- **Traces:** OpenTelemetry spans for each request, each outgoing HTTP call, and the
  dispatch and finalization of each run. The server exports spans over OTLP/HTTP only when
  you configure a collector.

!!! note "Run tracking is a different thing"
    These signals tell about the platform. The metrics, params, tags, images and tables that
    a model logs in a run (v0.11 and v0.14) are run data. They go through the run protocol
    into the database and the bundles bucket, not to `/metrics`. Refer to
    [Track metrics, params and tags](../guides/tracking.md) and
    [Compare runs and use experiments](../guides/run-comparison.md). The budgets of the run
    tracking give the warnings `TRACKING_BUDGET` and `MEDIA_BUDGET`
    ([Troubleshooting](troubleshooting.md#warnings-on-a-run)).

Nothing goes out from inside a sandbox. Each execution gets the `traceparent` of the
dispatching span in `MAPI_TRACEPARENT`. Thus you can join the stderr diagnostics of the
supervisor to the trace of the run, and no telemetry goes out of the exec project.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `MAPI_LOG_JSON` | `true` | JSON logs. `false` gives a console format that is easy to read for local work |
| `MAPI_METRICS_ENABLED` | `true` | Serve `GET /metrics` |
| `MAPI_METRICS_TOKEN` | unset | The bearer that a scraper must send. Necessary in the `multi-tenant` profile |
| `MAPI_WORKER_METRICS_PORT`, `MAPI_WORKER_METRICS_HOST` | unset, `127.0.0.1` | The address where the worker serves its own `/metrics` (the same bearer rule) |
| `MAPI_TRACING_ENABLED` | `true` | Make spans (their ids go into the logs also without a collector) |
| `MAPI_OTLP_ENDPOINT` | unset | OTLP/HTTP collector (`OTEL_EXPORTER_OTLP_ENDPOINT` also works). Needs the `otlp` extra of `mapi-server` |

### Scraping `/metrics`

The series contain tenant ids and spend. Thus `/metrics` is not a public route. It
authenticates its own bearer, as the Pub/Sub push endpoint does:

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

In the `multi-tenant` profile, a server without `MAPI_METRICS_TOKEN` refuses all scrapes
(401). The `single-tenant` and `dev` profiles serve `/metrics` without authentication on
their loopback API, if no token is set.

## The metric set

This table comes from `mapi_server.observability.metrics.Metrics`. This is the one registry
that defines all series. Thus the table always agrees with what `/metrics` serves. The
number of label values is limited on purpose. `tenant_id` is the only unlimited label, on the
two counters that have no meaning without it. HTTP series have the route *template* as
label, never the raw path.

<!-- generated: metrics -->

Monitor these series first:

- **`mapi_time_to_running_seconds`**: the M2 target is a p95 below 90 s.
- **`mapi_integrity_failures_total`** and **`mapi_sandbox_violations_total`**: each increase
  is a security signal, not a reliability signal.
- **`mapi_global_spend_micros`** against **`mapi_global_spend_cap_micros`**: the window of the
  kill switch.
- **`mapi_runs_active{state="stalled"}`**: runs that sent no events for some time. The status
  of the executor decides what occurs to them, not the absence of events.

The security suite adds one more series from outside the server,
`mapi_security_probe_unexpected{executor}`, through a textfile collector or a Pushgateway
([Security suite](security-suite.md#run-it)).

Each response has `X-Request-Id`. This is the header of the caller if the caller sent one.
If not, it is the trace id. When you report a problem, give this id. It finds the log lines
and the trace.

## Dashboards, alerts and runbooks

All the files that operators need are in `ops/` at the root of the repository.
`ops/README.md` tells how to import each one. The tables below come from those files.

### Dashboards

A Grafana dashboard and a Cloud Monitoring dashboard show the same series:

<!-- generated: dashboards -->

### Alerts

`ops/alerts/prometheus-rules.yml` has the Prometheus rules, with recording rules such as
`mapi:global_spend_ratio`. `ops/alerts/cloud-monitoring-alerts.yaml` has the same policies
for Cloud Monitoring. A *page* alert wakes a person. A *ticket* alert is for the next working
hour. The thresholds are initial values for the private beta.

<!-- generated: alerts -->

### Runbooks

Each alert names the runbook that solves it:

<!-- generated: runbooks -->

The sandbox-escape runbook is the incident response of the plan for a confirmed escape. It
has these steps:

1. Turn off uploaded code with the [kill switch](../guides/administration.md#kill-switches).
2. Rotate the signing and HMAC keys. This makes all signed URLs that are open now invalid.
3. Put the affected bundles in quarantine.
4. Tell the tenants.

## Tests that guard the signals

| Command | What it checks |
|---|---|
| `make chaos` | A worker that stops during `PROVISIONING` and `RUNNING`, Pub/Sub redelivery and a changed sequence, redelivery with a bad HMAC, and a PostgreSQL restart during a run. Each run comes to one outcome, with one attempt and no duplicate execution |
| `make load-test` | 100 concurrent fits through the real dispatcher, reconciler and finalizer on the fake Cloud Run backend, with injected 429s, lost responses and duplicate pushes. No run is lost, and the p95 time-to-`RUNNING` stays below 90 s |
| `make security-suite` | The sandbox-escape probes (refer to [Security suite](security-suite.md)) |

All three run offline, against the embedded PostgreSQL and the in-memory or shim object
store.
