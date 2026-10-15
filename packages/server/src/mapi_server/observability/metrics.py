"""Prometheus metrics for the control plane (plan §8.8 observability).

Every collector lives on one :class:`~prometheus_client.CollectorRegistry` owned by a
:class:`Metrics` instance, so a test can build a fresh registry without colliding with the
process-wide default (Prometheus refuses a duplicate time series on one registry). The
process uses :data:`METRICS`, a module-level instance on the default registry; service code
reaches it through the thin ``record_*`` helpers at the bottom so call sites never import
Prometheus directly.

The metric set is the one named in the M2 task: runs by state, dispatch latency,
time-to-RUNNING, event-ingestion lag, finalize outcomes, integrity failures, sandbox
violations, quota rejections, cost per tenant and live SSE clients. Label cardinality is
bounded on purpose: ``tenant_id`` is the only unbounded label and appears on just the two
counters (cost, integrity) that are meaningless without it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.registry import Collector

__all__ = [
    "CONTENT_TYPE",
    "DISPATCH_BUCKETS",
    "INGEST_LAG_BUCKETS",
    "METRICS",
    "TTR_BUCKETS",
    "Metrics",
    "record_cost_micros",
    "record_dispatch_latency",
    "record_event_ingested",
    "record_finalize_outcome",
    "record_http_request",
    "record_ingestion_lag",
    "record_integrity_failure",
    "record_quota_rejection",
    "record_run_terminal",
    "record_run_transition",
    "record_sandbox_violation",
    "record_time_to_running",
    "render",
    "set_active_runs",
    "set_metrics",
]

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

# Dispatch and time-to-RUNNING are seconds-to-minutes (p95 TTR target is 90 s, plan §14 M2).
DISPATCH_BUCKETS: Final[Sequence[float]] = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)
TTR_BUCKETS: Final[Sequence[float]] = (1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 300, 600)
# Logging -> Pub/Sub lag is usually sub-second but can spike into tens of seconds.
INGEST_LAG_BUCKETS: Final[Sequence[float]] = (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)


class Metrics:
    """Every control-plane collector, bound to one registry."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        r = self.registry

        self.runs_total = Counter(
            "mapi_runs_total",
            "Runs that reached a terminal state, by kind and terminal state.",
            ["kind", "state"],
            registry=r,
        )
        self.runs_active = Gauge(
            "mapi_runs_active",
            "Runs currently in a non-terminal state (sampled each reconcile pass).",
            ["state"],
            registry=r,
            multiprocess_mode="livesum",
        )
        self.run_transitions_total = Counter(
            "mapi_run_transitions_total",
            "Run state transitions, by target state (every edge, terminal or not).",
            ["state"],
            registry=r,
        )
        self.dispatch_latency = Histogram(
            "mapi_dispatch_latency_seconds",
            "Seconds from a run entering QUEUED to the executor accepting its submission.",
            buckets=DISPATCH_BUCKETS,
            registry=r,
        )
        self.time_to_running = Histogram(
            "mapi_time_to_running_seconds",
            "Seconds from a run entering QUEUED to its first event (RUNNING).",
            buckets=TTR_BUCKETS,
            registry=r,
        )
        self.event_ingestion_lag = Histogram(
            "mapi_event_ingestion_lag_seconds",
            "Seconds between a run event's timestamp and its ingestion.",
            buckets=INGEST_LAG_BUCKETS,
            registry=r,
        )
        self.events_ingested_total = Counter(
            "mapi_events_ingested_total",
            "Run events seen by the ingestor, by result (accepted, duplicate, bad_hmac, ...).",
            ["result"],
            registry=r,
        )
        self.finalize_outcomes_total = Counter(
            "mapi_finalize_outcomes_total",
            "Finalize attempts, by outcome (succeeded, partial, integrity, infra, retry).",
            ["kind", "outcome"],
            registry=r,
        )
        self.integrity_failures_total = Counter(
            "mapi_integrity_failures_total",
            "Output-integrity verification failures at FINALIZING (a security signal).",
            ["tenant_id"],
            registry=r,
        )
        self.sandbox_violations_total = Counter(
            "mapi_sandbox_violations_total",
            "Sandbox violations observed by the control plane, by reason.",
            ["reason"],
            registry=r,
        )
        self.quota_rejections_total = Counter(
            "mapi_quota_rejections_total",
            "Requests denied by a quota or rate limit, by scope and error code.",
            ["scope", "code"],
            registry=r,
        )
        self.cost_micros_total = Counter(
            "mapi_cost_micros_total",
            "Billed compute cost in currency micros, by tenant (cost ledger).",
            ["tenant_id"],
            registry=r,
        )
        self.sse_clients = Gauge(
            "mapi_sse_clients",
            "Live Server-Sent-Events subscribers connected to this process.",
            registry=r,
            multiprocess_mode="livesum",
        )
        self.http_requests_total = Counter(
            "mapi_http_requests_total",
            "HTTP requests handled by the API, by method, route template and status.",
            ["method", "route", "status"],
            registry=r,
        )
        self.http_request_duration = Histogram(
            "mapi_http_request_duration_seconds",
            "HTTP request handling time, by method and route template.",
            ["method", "route"],
            buckets=DISPATCH_BUCKETS,
            registry=r,
        )

    # -------------------------------------------------------------- live callbacks
    def bind_sse_gauge(self, count: Callable[[], float]) -> None:
        """Report the live SSE subscriber count from ``count`` whenever /metrics is scraped."""
        self.sse_clients.set_function(count)

    def register_collector(self, collector: Collector) -> None:
        self.registry.register(collector)

    def render(self) -> bytes:
        return generate_latest(self.registry)


METRICS: Metrics = Metrics()
"""The process-wide metrics on the Prometheus default-style registry.

Reassigned only by :func:`set_metrics` (tests), so not ``Final``.
"""


def set_metrics(metrics: Metrics) -> None:
    """Point the module-level helpers at ``metrics`` (tests use a throwaway registry)."""
    global METRICS
    METRICS = metrics


def render() -> bytes:
    """The current /metrics exposition for :data:`METRICS`."""
    return METRICS.render()


# ---------------------------------------------------------------------------- helpers
# These read ``METRICS`` at call time so :func:`set_metrics` (tests) is honoured, and they
# never raise into a hot path: a metrics error must not fail a run or a request.


def record_run_transition(state: str) -> None:
    METRICS.run_transitions_total.labels(state=state).inc()


def record_run_terminal(kind: str, state: str) -> None:
    METRICS.runs_total.labels(kind=kind, state=state).inc()


def set_active_runs(counts: dict[str, int], *, states: Sequence[str]) -> None:
    """Set ``mapi_runs_active`` for every known ``state`` (0 when absent from ``counts``)."""
    for state in states:
        METRICS.runs_active.labels(state=state).set(counts.get(state, 0))


def record_dispatch_latency(seconds: float) -> None:
    METRICS.dispatch_latency.observe(max(0.0, seconds))


def record_time_to_running(seconds: float) -> None:
    METRICS.time_to_running.observe(max(0.0, seconds))


def record_ingestion_lag(seconds: float) -> None:
    METRICS.event_ingestion_lag.observe(max(0.0, seconds))


def record_event_ingested(result: str) -> None:
    METRICS.events_ingested_total.labels(result=result).inc()


def record_finalize_outcome(kind: str, outcome: str) -> None:
    METRICS.finalize_outcomes_total.labels(kind=kind, outcome=outcome).inc()


def record_integrity_failure(tenant_id: str) -> None:
    METRICS.integrity_failures_total.labels(tenant_id=tenant_id).inc()


def record_sandbox_violation(reason: str) -> None:
    METRICS.sandbox_violations_total.labels(reason=reason).inc()


def record_quota_rejection(scope: str, code: str) -> None:
    METRICS.quota_rejections_total.labels(scope=scope, code=code).inc()


def record_cost_micros(tenant_id: str, micros: float) -> None:
    if micros > 0:
        METRICS.cost_micros_total.labels(tenant_id=tenant_id).inc(micros)


def record_http_request(method: str, route: str, status: int, seconds: float) -> None:
    METRICS.http_requests_total.labels(method=method, route=route, status=str(status)).inc()
    METRICS.http_request_duration.labels(method=method, route=route).observe(max(0.0, seconds))
