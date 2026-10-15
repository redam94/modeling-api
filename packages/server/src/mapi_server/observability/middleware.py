"""The ``/metrics`` endpoint and the request-scoped observability middleware.

:class:`ObservabilityMiddleware` binds a per-request logging context (request id, method,
path and the matched route template) so every log line and the active span can be joined,
records the request's status and latency to Prometheus against the *route template* (never
the raw path, which would explode label cardinality), and clears the context afterwards. The
request id comes from an ``X-Request-Id`` header when the caller supplies one, otherwise the
active trace id, otherwise a fresh token; it is echoed back in the response.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Match, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mapi_server.observability import logging as obs_logging
from mapi_server.observability import metrics as obs_metrics

__all__ = ["ObservabilityMiddleware", "add_metrics_endpoint", "route_template"]

REQUEST_ID_HEADER = "X-Request-Id"


def _flatten(router: object) -> Iterable[Route]:
    """Every leaf route, descending into FastAPI's ``_IncludedRouter`` wrappers."""
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _flatten(original)
        elif isinstance(route, Route):
            yield route


def route_template(app: FastAPI, request: Request) -> str:
    """The matched route's path template (``/v1/runs/{run_id}``) or ``<unmatched>``.

    Prefers a method+path (``FULL``) match; a path-only (``PARTIAL``) match is the fallback
    so a wrong-method request is still attributed to its route rather than ``<unmatched>``.
    """
    scope = request.scope
    partial: str | None = None
    for route in _flatten(app.router):
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route.path
        if match is Match.PARTIAL and partial is None:
            partial = route.path
    return partial or "<unmatched>"


def _request_id(request: Request) -> str:
    supplied = request.headers.get(REQUEST_ID_HEADER)
    if supplied:
        return supplied[:128]
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:  # pragma: no cover - defensive
        pass
    return uuid.uuid4().hex


class ObservabilityMiddleware:
    """Pure-ASGI middleware: request-id context, structured logging, request metrics."""

    def __init__(self, app: ASGIApp, *, fastapi_app: FastAPI) -> None:
        self.app = app
        self.fastapi_app = fastapi_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        if request.url.path == "/metrics":  # scraped constantly; no per-scrape log/metric
            await self.app(scope, receive, send)
            return
        req_id = _request_id(request)
        route = route_template(self.fastapi_app, request)
        obs_logging.clear_context()
        obs_logging.bind_context(
            request_id=req_id, method=request.method, path=request.url.path, route=route
        )
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = message.setdefault("headers", [])
                headers.append((REQUEST_ID_HEADER.encode(), req_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            obs_metrics.record_http_request(request.method, route, status_code, elapsed)
            obs_logging.clear_context()


def add_metrics_endpoint(app: FastAPI) -> None:
    """Mount ``GET /metrics`` (Prometheus text exposition) on ``app``."""

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return PlainTextResponse(obs_metrics.render(), media_type=obs_metrics.CONTENT_TYPE)
