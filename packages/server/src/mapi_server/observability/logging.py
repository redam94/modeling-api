"""Structured JSON logging (plan §8.7 observability).

The control plane logs through the stdlib :mod:`logging` module (``log = getLogger(__name__)``
everywhere), so rather than rewrite every call site we route stdlib records through a
structlog :class:`~structlog.stdlib.ProcessorFormatter`. The result is one JSON object per
line carrying the level, logger, message, any ``extra=`` fields, the bound request/run
context, and — when a span is active — the OpenTelemetry ``trace_id`` and ``span_id`` so a
log line can be joined to its trace.

``configure_logging`` is idempotent and safe to call from the API process, the worker
process and tests. ``bind_context``/``clear_context`` manage the per-task context variables
(request id, run id, tenant) that every log line in that task then carries; the request
middleware and the orchestrator loops set them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

__all__ = [
    "bind_context",
    "bound_context",
    "clear_context",
    "configure_logging",
    "get_logger",
]

_CONFIGURED = False


def add_trace_context(_logger: WrappedLogger, _name: str, event: EventDict) -> EventDict:
    """Add ``trace_id``/``span_id`` (hex) from the active OpenTelemetry span, if any."""
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - otel always installed in this workspace
        return event
    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is not None and ctx.is_valid:
        event["trace_id"] = format(ctx.trace_id, "032x")
        event["span_id"] = format(ctx.span_id, "016x")
    return event


_STD_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def add_record_extras(_logger: WrappedLogger, _name: str, event: EventDict) -> EventDict:
    """Copy ``extra=`` fields from a stdlib :class:`~logging.LogRecord` into the event."""
    record = event.get("_record")
    if record is None:
        return event
    for key, value in record.__dict__.items():
        if key not in _STD_RECORD_ATTRS and not key.startswith("_") and key not in event:
            event[key] = value
    return event


def _shared_processors() -> list[structlog.types.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        add_record_extras,
        add_trace_context,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def configure_logging(*, level: str = "info", json: bool = True) -> None:
    """Install the JSON (or console) formatter on the root logger. Idempotent."""
    global _CONFIGURED
    numeric = getattr(logging, level.upper(), logging.INFO)
    shared = _shared_processors()
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    root = logging.getLogger()
    # Replace any handler we (or logging.basicConfig) installed before; keep this idempotent.
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.handlers = [handler]
    root.setLevel(numeric)
    # uvicorn installs its own handlers on these loggers; let them propagate to our root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    _CONFIGURED = True


def get_logger(name: str | None = None) -> Any:
    """A structlog logger (``structlog.get_logger``) for code that wants structured kwargs."""
    return structlog.get_logger(name)


def bind_context(**kwargs: object) -> None:
    """Bind key/values onto every log line emitted by the current task."""
    structlog.contextvars.bind_contextvars(**{k: v for k, v in kwargs.items() if v is not None})


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


@contextmanager
def bound_context(**kwargs: object) -> Iterator[None]:
    """Bind context for the duration of a ``with`` block, then restore the previous values."""
    tokens = structlog.contextvars.bind_contextvars(
        **{k: v for k, v in kwargs.items() if v is not None}
    )
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
