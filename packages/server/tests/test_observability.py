"""Observability: the metric set, JSON logs, trace context and the ``/metrics`` endpoint."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from mapi_server.observability import logging as obs_logging
from mapi_server.observability import metrics as obs_metrics
from mapi_server.observability import tracing as obs_tracing
from mapi_server.observability.metrics import Metrics
from mapi_server.observability.middleware import route_template
from prometheus_client.parser import text_string_to_metric_families


@pytest.fixture
def fresh_metrics() -> Metrics:
    """A throwaway registry so assertions see only this test's observations."""
    previous = obs_metrics.METRICS
    metrics = Metrics()
    obs_metrics.set_metrics(metrics)
    try:
        yield metrics
    finally:
        obs_metrics.set_metrics(previous)


def _sample(text: str, name: str, labels: dict[str, str] | None = None) -> float:
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and (labels is None or labels.items() <= sample.labels.items()):
                return sample.value
    raise AssertionError(f"no sample {name} {labels} in exposition")


def test_metric_helpers_record_to_the_bound_registry(fresh_metrics: Metrics) -> None:
    obs_metrics.record_run_terminal("fit", "succeeded")
    obs_metrics.record_run_terminal("fit", "succeeded")
    obs_metrics.record_dispatch_latency(0.4)
    obs_metrics.record_time_to_running(12.0)
    obs_metrics.record_ingestion_lag(0.2)
    obs_metrics.record_finalize_outcome("fit", "integrity")
    obs_metrics.record_integrity_failure("acme")
    obs_metrics.record_sandbox_violation("bad_hmac")
    obs_metrics.record_quota_rejection("tenant", "RATE_LIMITED")
    obs_metrics.record_cost_micros("acme", 31200)
    obs_metrics.record_http_request("GET", "/v1/runs/{run_id}", 200, 0.01)

    text = fresh_metrics.render().decode()
    assert _sample(text, "mapi_runs_total", {"kind": "fit", "state": "succeeded"}) == 2
    assert _sample(text, "mapi_dispatch_latency_seconds_count") == 1
    assert _sample(text, "mapi_time_to_running_seconds_count") == 1
    assert _sample(text, "mapi_integrity_failures_total", {"tenant_id": "acme"}) == 1
    assert _sample(text, "mapi_sandbox_violations_total", {"reason": "bad_hmac"}) == 1
    assert _sample(text, "mapi_quota_rejections_total", {"code": "RATE_LIMITED"}) == 1
    assert _sample(text, "mapi_cost_micros_total", {"tenant_id": "acme"}) == 31200
    assert _sample(text, "mapi_http_requests_total", {"status": "200"}) == 1


def test_negative_durations_are_clamped(fresh_metrics: Metrics) -> None:
    obs_metrics.record_ingestion_lag(-5.0)  # clock skew must not raise or go negative
    text = fresh_metrics.render().decode()
    assert _sample(text, "mapi_event_ingestion_lag_seconds_sum") == 0.0


def test_active_runs_gauge_sets_every_state(fresh_metrics: Metrics) -> None:
    obs_metrics.set_active_runs({"running": 3}, states=["running", "queued", "finalizing"])
    text = fresh_metrics.render().decode()
    assert _sample(text, "mapi_runs_active", {"state": "running"}) == 3
    assert _sample(text, "mapi_runs_active", {"state": "queued"}) == 0


def test_sse_gauge_reads_the_live_count(fresh_metrics: Metrics) -> None:
    count = 0

    def live() -> float:
        return count

    fresh_metrics.bind_sse_gauge(live)
    count = 4
    assert _sample(fresh_metrics.render().decode(), "mapi_sse_clients") == 4


def test_json_logs_carry_context_and_extras(capsys: pytest.CaptureFixture[str]) -> None:
    obs_logging.configure_logging(level="info", json=True)
    try:
        with obs_logging.bound_context(run_id="run_1", tenant_id="acme"):
            logging.getLogger("mapi_server.test").warning(
                "integrity failure", extra={"security_alert": True}
            )
    finally:
        logging.getLogger().handlers.clear()
    line = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "integrity failure"
    assert record["level"] == "warning"
    assert record["run_id"] == "run_1" and record["tenant_id"] == "acme"
    assert record["security_alert"] is True


def test_trace_context_in_logs_when_a_span_is_active(capsys: pytest.CaptureFixture[str]) -> None:
    obs_tracing.shutdown_tracing()
    obs_tracing.configure_tracing(service_name="test", otlp_endpoint=None)
    obs_logging.configure_logging(level="info", json=True)
    try:
        with obs_tracing.run_span("unit", run_id="run_9"):
            logging.getLogger("mapi_server.test").info("inside")
    finally:
        logging.getLogger().handlers.clear()
        obs_tracing.shutdown_tracing()
    record = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert len(record["trace_id"]) == 32 and len(record["span_id"]) == 16


async def test_metrics_endpoint_and_request_metrics(
    client: httpx.AsyncClient, fresh_metrics: Metrics
) -> None:
    # bind this app's gauge to the fresh registry, then drive one request through it
    assert (await client.get("/healthz")).status_code == 200
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "X-Request-Id" in (await client.get("/healthz")).headers
    text = r.text
    # /healthz was recorded against its route template, not the raw path
    assert _sample(text, "mapi_http_requests_total", {"route": "/healthz", "status": "200"}) >= 1


def test_route_template_uses_the_matched_path() -> None:
    from mapi_server.app import create_app
    from mapi_server.settings import Settings

    app = create_app(Settings(database_url="postgresql://unused/unused"))
    scope = {"type": "http", "method": "GET", "path": "/v1/runs/run_123", "headers": []}
    request = httpx.Request("GET", "http://t/v1/runs/run_123")
    from starlette.requests import Request as StarletteRequest

    assert route_template(app, StarletteRequest(scope)) == "/v1/runs/{run_id}"
    _ = request
