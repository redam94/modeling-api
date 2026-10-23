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


async def test_metrics_endpoint_requires_its_token_when_configured() -> None:
    from fastapi import FastAPI
    from mapi_server.observability.middleware import add_metrics_endpoint

    async def get(app: FastAPI, auth: str | None = None) -> int:
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": auth} if auth else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return (await c.get("/metrics", headers=headers)).status_code

    guarded = FastAPI()
    add_metrics_endpoint(guarded, token="scrape-s3cret")
    assert await get(guarded) == 401
    assert await get(guarded, "Bearer nope") == 401
    assert await get(guarded, "Bearer scrape-s3cret") == 200
    closed = FastAPI()  # multi-tenant production without a token: fail closed
    add_metrics_endpoint(closed, require_token=True)
    assert await get(closed, "Bearer anything") == 401
    open_ = FastAPI()
    add_metrics_endpoint(open_)
    assert await get(open_) == 200


def test_route_template_uses_the_matched_path() -> None:
    from mapi_server.app import create_app
    from mapi_server.settings import Settings

    app = create_app(Settings(database_url="postgresql://unused/unused"))
    scope = {"type": "http", "method": "GET", "path": "/v1/runs/run_123", "headers": []}
    request = httpx.Request("GET", "http://t/v1/runs/run_123")
    from starlette.requests import Request as StarletteRequest

    assert route_template(app, StarletteRequest(scope)) == "/v1/runs/{run_id}"
    _ = request


async def test_worker_serves_its_own_metrics_on_request() -> None:
    """The orchestrator's series (dispatch latency, finalize outcomes, ...) live in the
    worker process: MAPI_WORKER_METRICS_PORT serves them there, with the same bearer rule."""
    import asyncio
    import socket

    from mapi_server.__main__ import _worker_metrics_server
    from mapi_server.settings import Profile, Settings

    base = Settings(database_url="postgresql://unused/unused")
    assert _worker_metrics_server(base) is None  # off unless a port is set
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = base.model_copy(
        update={
            "worker_metrics_port": port,
            "profile": Profile.MULTI_TENANT,
            "metrics_token": "scrape",
        }
    )
    server = _worker_metrics_server(settings)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            assert (await c.get("/metrics")).status_code == 401
            r = await c.get("/metrics", headers={"Authorization": "Bearer scrape"})
            assert r.status_code == 200 and "mapi_dispatch_latency_seconds" in r.text
    finally:
        server.should_exit = True
        await task


async def test_mcp_request_spans_never_carry_the_query_string() -> None:
    """The residual of mcp-5: a read token in ``/v1/mcp?token=`` never reaches a span."""
    from fastapi import FastAPI
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = FastAPI()

    @app.post("/v1/mcp")
    async def mcp() -> dict[str, str]:
        return {}

    @app.post("/v1/other")
    async def other() -> dict[str, str]:
        return {}

    secret = "mapi_abcdefgh_" + "x" * 39
    obs_tracing.shutdown_tracing()
    obs_tracing.instrument_app(app, tracer_provider=provider)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.post(f"/v1/mcp?token={secret}&x=Bearer%20{secret}")
            await c.post("/v1/other?page=2")
    finally:
        obs_tracing.shutdown_tracing()
    spans = exporter.get_finished_spans()
    mcp_spans = [s for s in spans if "/v1/mcp" in str(dict(s.attributes or {}))]
    assert mcp_spans
    for span in spans:
        assert secret not in str(dict(span.attributes or {})), span.name
    # other routes keep their query
    assert any("page=2" in str(dict(s.attributes or {})) for s in spans)


def test_scrub_mcp_query_rewrites_the_stable_attributes_too() -> None:
    from opentelemetry.sdk.trace import TracerProvider

    span = TracerProvider().get_tracer("t").start_span(
        "x", attributes={"url.query": "token=mapi_x", "http.url": "http://h/v1/mcp?token=mapi_x",
                         "http.target": "/v1/mcp?token=mapi_x", "url.path": "/v1/mcp"},
    )  # fmt: skip
    obs_tracing.scrub_mcp_query(span, {"type": "http", "path": "/v1/mcp", "query_string": b"t"})
    attrs = dict(span.attributes or {})
    assert "mapi_x" not in str(attrs) and attrs["url.query"] == "REDACTED"
    assert attrs["http.url"] == "http://h/v1/mcp?REDACTED"
    span.end()


async def test_mcp_query_never_reaches_the_access_log() -> None:
    """v0.20 live: ``POST /v1/mcp?token=mapi_…`` was refused (401) but uvicorn's access log
    printed the whole query, read token included (plan D19). The filter that the app
    attaches must survive uvicorn's own logging ``dictConfig`` (as in ``mapi-server api``);
    other paths keep their query."""
    import asyncio
    import socket

    import uvicorn
    from fastapi import FastAPI

    app = FastAPI()

    @app.post("/v1/mcp")
    async def mcp() -> dict[str, str]:
        return {}

    @app.post("/v1/other")
    async def other() -> dict[str, str]:
        return {}

    obs_logging.redact_access_log()  # the app factory does this before uvicorn.run
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off"))
    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    access = logging.getLogger("uvicorn.access")
    capture = _Capture()
    access.addHandler(capture)
    task = asyncio.create_task(server.serve())
    secret = "mapi_abcdefgh_" + "x" * 39
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            await c.post(f"/v1/mcp?token={secret}&key={secret}")
            await c.post("/v1/mcp/?x=Bearer%20" + secret)
            await c.post("/v1/other?page=2")
    finally:
        server.should_exit = True
        await task
        access.removeHandler(capture)
    assert any("/v1/mcp?REDACTED" in line for line in lines), lines
    assert all(secret not in line for line in lines), lines
    assert any("/v1/other?page=2" in line for line in lines), lines


def test_mcp_query_redactor_leaves_other_records_alone() -> None:
    f = obs_logging.McpQueryRedactor()
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s %s", ("a", "b"), None)
    assert f.filter(rec) and rec.args == ("a", "b")
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "POST", "/v1/mcpx?token=a", "1.1", 401), None,
    )  # fmt: skip
    assert f.filter(rec) and rec.args[2] == "/v1/mcpx?token=a"
