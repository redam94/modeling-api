"""OpenTelemetry tracing for the control plane (plan §8.7 observability).

``configure_tracing`` installs a :class:`~opentelemetry.sdk.trace.TracerProvider` with a
service-name resource and, when ``MAPI_OTLP_ENDPOINT`` (or the standard
``OTEL_EXPORTER_OTLP_ENDPOINT``) is set and the OTLP/HTTP exporter is installed, a batching
OTLP span exporter. Export is therefore optional: with nothing configured the provider still
runs so spans carry a valid trace id into the logs, but no spans leave the process.

``instrument_app`` adds FastAPI (ASGI), SQLAlchemy and httpx instrumentation. Each is
import-guarded and applied at most once. ``run_span`` opens a span that carries ``run_id``
(and the tenant/attempt) so a run's dispatch, reconcile and finalize work is one trace.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = [
    "configure_tracing",
    "instrument_app",
    "instrument_engine",
    "instrument_httpx",
    "run_span",
    "shutdown_tracing",
]

log = logging.getLogger(__name__)

_PROVIDER: Any = None
_APP_INSTRUMENTED = False
_HTTPX_INSTRUMENTED = False


def _otlp_endpoint(explicit: str | None) -> str | None:
    return (
        explicit
        or os.environ.get("MAPI_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )


def configure_tracing(
    *,
    service_name: str = "mapi-server",
    service_version: str | None = None,
    otlp_endpoint: str | None = None,
    enabled: bool = True,
) -> Any:
    """Install the global tracer provider. Returns it, or ``None`` when disabled.

    Idempotent: a second call with tracing already configured returns the live provider.
    """
    global _PROVIDER
    if not enabled:
        return None
    if _PROVIDER is not None:
        return _PROVIDER

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    attrs: dict[str, Any] = {"service.name": service_name}
    if service_version:
        attrs["service.version"] = service_version
    provider = TracerProvider(resource=Resource.create(attrs))

    endpoint = _otlp_endpoint(otlp_endpoint)
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces"))
            )
            log.info("OpenTelemetry OTLP span export enabled -> %s", endpoint)
        except ImportError:
            log.warning(
                "MAPI_OTLP_ENDPOINT is set but the OTLP/HTTP exporter is not installed "
                "(install mapi-server[otlp]); traces will not be exported"
            )
    trace.set_tracer_provider(provider)
    _PROVIDER = provider
    return provider


def instrument_app(app: FastAPI) -> None:
    """Instrument a FastAPI app for request spans (once per process)."""
    global _APP_INSTRUMENTED
    if _APP_INSTRUMENTED:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:  # pragma: no cover
        return
    FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])
    _APP_INSTRUMENTED = True


def instrument_engine(engine: AsyncEngine) -> None:
    """Instrument a SQLAlchemy engine for query spans (when the versions are compatible).

    The ``opentelemetry-instrumentation-sqlalchemy`` package pins a SQLAlchemy range and logs
    an error (and does nothing) outside it. We check the range ourselves and skip quietly so
    an incompatible SQLAlchemy does not spam the logs; FastAPI and httpx spans still carry the
    trace, and DB calls appear as children of the request/run span regardless.
    """
    try:
        import sqlalchemy
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.instrumentation.sqlalchemy.package import _instruments
        from packaging.requirements import Requirement
        from packaging.version import Version
    except ImportError:  # pragma: no cover
        return
    spec = next((r for r in _instruments if r.startswith("sqlalchemy")), None)
    if spec is not None and Version(sqlalchemy.__version__) not in Requirement(spec).specifier:
        log.debug(
            "skipping SQLAlchemy instrumentation: installed %s is outside %s",
            sqlalchemy.__version__,
            spec,
        )
        return
    # The async engine wraps a sync engine; instrument the sync core.
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


def instrument_httpx() -> None:
    """Instrument httpx clients for outbound-request spans (once per process)."""
    global _HTTPX_INSTRUMENTED
    if _HTTPX_INSTRUMENTED:
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:  # pragma: no cover
        return
    HTTPXClientInstrumentor().instrument()
    _HTTPX_INSTRUMENTED = True


@contextmanager
def run_span(name: str, *, run_id: str, attempt: int | None = None, **attrs: Any) -> Iterator[None]:
    """A span named ``name`` carrying ``mapi.run_id`` (+ attempt and any extra attrs)."""
    from opentelemetry import trace

    tracer = trace.get_tracer("mapi_server")
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("mapi.run_id", run_id)
        if attempt is not None:
            span.set_attribute("mapi.attempt", attempt)
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(f"mapi.{key}", value)
        yield


def shutdown_tracing() -> None:
    """Flush and tear down the provider (tests; process exit)."""
    global _PROVIDER, _APP_INSTRUMENTED, _HTTPX_INSTRUMENTED
    if _PROVIDER is not None:
        with contextlib.suppress(Exception):  # pragma: no cover - best effort
            _PROVIDER.shutdown()
    _PROVIDER = None
    _APP_INSTRUMENTED = False
    _HTTPX_INSTRUMENTED = False
