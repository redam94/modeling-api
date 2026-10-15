"""Observability for the control plane: structured logs, tracing and Prometheus metrics.

Three independent concerns, each usable on its own:

* :mod:`~mapi_server.observability.logging` — structlog JSON logging over the stdlib
  logging every module already uses, with trace ids and a per-task context;
* :mod:`~mapi_server.observability.tracing` — OpenTelemetry spans (FastAPI, SQLAlchemy,
  httpx) with an optional OTLP exporter;
* :mod:`~mapi_server.observability.metrics` — the Prometheus metric set and ``/metrics``.

:func:`setup_observability` wires all three for a process from :class:`ObsSettings`, and is
called by both the API and the worker entry points. The service code only ever touches the
``record_*`` metric helpers and ``bind_context``; nothing else imports this package, so it
stays an optional cross-cutting layer rather than a dependency of the business logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from mapi_server.observability.logging import (
    bind_context,
    bound_context,
    clear_context,
    configure_logging,
    get_logger,
)
from mapi_server.observability.metrics import METRICS, Metrics
from mapi_server.observability.tracing import (
    configure_tracing,
    instrument_httpx,
    shutdown_tracing,
)

__all__ = [
    "METRICS",
    "Metrics",
    "ObsSettings",
    "bind_context",
    "bound_context",
    "clear_context",
    "configure_logging",
    "get_logger",
    "setup_observability",
    "shutdown_tracing",
]


@dataclass(frozen=True, slots=True)
class ObsSettings:
    """What :func:`setup_observability` needs.

    A subset of :class:`~mapi_server.settings.Settings`, so the observability layer does not
    import the full settings model.
    """

    service_name: str = "mapi-server"
    service_version: str | None = None
    log_level: str = "info"
    log_json: bool = True
    tracing_enabled: bool = True
    otlp_endpoint: str | None = None


def setup_observability(obs: ObsSettings) -> None:
    """Configure logging, tracing and httpx instrumentation for this process (idempotent)."""
    configure_logging(level=obs.log_level, json=obs.log_json)
    if obs.tracing_enabled:
        configure_tracing(
            service_name=obs.service_name,
            service_version=obs.service_version,
            otlp_endpoint=obs.otlp_endpoint,
        )
        instrument_httpx()
