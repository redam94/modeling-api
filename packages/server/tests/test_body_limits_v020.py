"""Every v0.20 body is capped before it is parsed (plan I-3, WP5): chat turns (64 KiB),
chat settings (4 KiB), read tokens (2 KiB) and MCP messages (64 KiB). Driven by
:data:`mapi_spec.api.V020_ENDPOINTS`, so a new write route without a cap fails here. The app
also serves every I-3 route with the declared method, path and operation id.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mapi_server.api.problems import JsonBodyLimit
from mapi_server.app import V020_BODY_LIMITS, create_app, iter_api_routes
from mapi_server.context import ServerContext
from mapi_spec.api import ROUTE_ACCESS, V020_ENDPOINTS

pytestmark = pytest.mark.integration

KIB = 1024
_PARAM = re.compile(r"\{([^}:]+)\}")
_SAMPLE = {
    "dashboard_id": "dsh_" + "0" * 26,
    "conversation_id": "cnv_" + "0" * 26,
    "token_id": "tok_" + "0" * 26,
}
WRITES = ("POST", "PUT", "PATCH")
PLANNED = {
    "post_dashboard_chat": 64 * KIB,
    "post_shared_chat": 64 * KIB,
    "put_dashboard_chat_settings": 4 * KIB,
    "create_dashboard_read_token": 2 * KIB,
    "mcp_endpoint": 64 * KIB,
}
I3_OPERATIONS = {
    "get_dashboard_chat", "post_dashboard_chat", "get_dashboard_chat_settings",
    "put_dashboard_chat_settings", "list_dashboard_chat_transcripts",
    "get_dashboard_chat_transcript", "delete_dashboard_chat_transcript", "get_shared_chat",
    "post_shared_chat", "list_dashboard_read_tokens", "create_dashboard_read_token",
    "revoke_dashboard_read_token", "mcp_endpoint",
}  # fmt: skip


def _caps(app: FastAPI, method: str, path: str) -> list[int]:
    out = []
    for mw in app.user_middleware:
        if mw.cls is JsonBodyLimit:
            kwargs: dict[str, Any] = dict(mw.kwargs)
            methods = kwargs.get("methods", ("POST",))
            if method in methods and any(re.fullmatch(p, path) for p in kwargs["paths"]):
                out.append(int(kwargs["max_bytes"]))
    return out


def _path(template: str) -> str:
    return _PARAM.sub(lambda mt: _SAMPLE[mt.group(1)], template)


def test_the_table_is_the_i3_table() -> None:
    assert {e.operation_id for e in V020_ENDPOINTS} == I3_OPERATIONS
    assert set(ROUTE_ACCESS) >= I3_OPERATIONS


def test_every_v020_write_route_is_capped_as_planned(ctx: ServerContext) -> None:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    writes = [e for e in V020_ENDPOINTS if e.method in WRITES]
    assert {e.operation_id for e in writes} == set(PLANNED)
    for e in writes:
        caps = _caps(app, e.method, _path(e.path))
        assert caps, f"{e.method} {e.path} ({e.operation_id}) has no JsonBodyLimit"
        assert min(caps) == PLANNED[e.operation_id], (e.operation_id, caps)
    # the table holds nothing else, and every row covers a route
    for paths, _max, methods in V020_BODY_LIMITS:
        assert any(
            e.method in methods and any(re.fullmatch(p, _path(e.path)) for p in paths)
            for e in writes
        ), paths


def test_the_app_serves_every_i3_route(ctx: ServerContext) -> None:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    served = {
        (method, path, route.operation_id)
        for route, path in iter_api_routes(app)
        for method in (route.methods or ())
    }
    for e in V020_ENDPOINTS:
        assert (e.method, e.path, e.operation_id) in served, e
    doc = app.openapi()
    for e in V020_ENDPOINTS:
        op = doc["paths"][e.path][e.method.lower()]
        assert op["operationId"] == e.operation_id


async def test_oversized_bodies_are_refused_before_parsing(client: httpx.AsyncClient) -> None:
    """One byte over each cap: ``413`` before the credential check (no credential is sent)."""
    for e in V020_ENDPOINTS:
        if e.method not in WRITES:
            continue
        cap = PLANNED[e.operation_id]
        head = b'{"pad": "'
        body = head + b"x" * (cap + 1 - len(head) - 2) + b'"}'
        assert len(body) == cap + 1
        r = await client.request(
            e.method, _path(e.path), content=body, headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 413, (e.operation_id, r.status_code, r.text)
        assert r.json()["code"] == "PAYLOAD_TOO_LARGE"


async def test_a_body_at_the_chat_cap_reaches_the_credential_check(
    client: httpx.AsyncClient,
) -> None:
    head = b'{"pad": "'
    body = head + b"x" * (64 * KIB - len(head) - 2) + b'"}'
    assert len(body) == 64 * KIB
    r = await client.post(
        "/v1/shared/chat", content=body, headers={"Content-Type": "application/json"}
    )
    assert r.status_code in (401, 422), r.text  # past the cap: validation or the session check


async def test_a_deep_chat_body_is_refused(client: httpx.AsyncClient) -> None:
    body = b"[" * 200 + b"]" * 200
    r = await client.post(
        "/v1/shared/chat", content=body, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID", r.text
