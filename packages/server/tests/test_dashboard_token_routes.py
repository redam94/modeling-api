"""Where a dashboard read token works (v0.20, plan D12, D14, WP6).

* On every route the generic resolver handles (every non-public, non-internal operation of
  the app) a read token answers ``403 FORBIDDEN``: it is for the MCP endpoint only. On
  public routes the bearer is never read, so the token is simply ignored.
* No API token reaches :data:`mapi_spec.api.SESSION_ONLY_OPERATIONS`.
* :func:`resolve_dashboard_token` (the MCP endpoint's check): the same rules as any API
  token, plus an active tenant and a read-token scope; the owner's access is then checked
  again on every use (:func:`token_context`), so removing a grant stops the token.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import Request
from mapi_server.api import dashboard_read_tokens
from mapi_server.app import create_app, iter_api_routes
from mapi_server.auth.request import access_for, resolve_dashboard_token
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.audit import FAILURE_AUDITS_PER_MINUTE
from mapi_server.services.dashboards.access import DashboardTokenAuth, token_context
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller, make_token, token_caller
from mapi_spec.api import SESSION_ONLY_OPERATIONS, RouteScope
from mapi_spec.errors import MapiError
from mapi_spec.tokens import (
    TOKEN_NEVER,
    ParsedToken,
    generate_token,
    parse_token,
    token_checksum,
)
from sqlalchemy import func, select, update
from test_dashboards_crud import LAYOUT1, dashboard_kit
from test_dashboards_crud_v2 import create_v2

pytestmark = pytest.mark.integration

_PARAM = re.compile(r"\{([a-z_]+)(?::[a-z]+)?\}")
_VALUES = {"tenant_id": "default", "project_id": "default", "version": "1", "seq": "1"}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT, **LAYOUT1})


def _app(ctx: ServerContext) -> Any:
    """The app, with the read-token routes mounted even before ``app.py`` includes them."""
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    ops = {(r.operation_id or r.name) for r, _ in iter_api_routes(app)}
    if "create_dashboard_read_token" not in ops:
        app.include_router(dashboard_read_tokens.router)
    return app


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app(ctx))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
        yield c


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    from mapi_server.services.dashboards import sources

    sources.clear_catalog_cache()
    kit = await dashboard_kit(ctx, client)
    kit["ctx"] = ctx
    kit["client"] = client
    return kit


async def _read_token(kit: dict[str, Any], who: str, dash_id: str) -> tuple[str, str]:
    caller: Caller = kit[who]
    r = await caller.post(f"/v1/dashboards/{dash_id}/read-tokens", json={"name": "mcp"})
    assert r.status_code == 201, r.text
    return str(r.json()["id"]), str(r.json()["secret"])


async def _v2_published(kit: dict[str, Any], name: str = "fc") -> str:
    dash_id, _ = await create_v2(kit, name)
    r = await kit["ana"].post(f"/v1/dashboards/{dash_id}:publish")
    assert r.status_code == 200, r.text
    cid = kit["users"]["cleo"].id
    r = await kit["ana"].put(f"/v1/dashboards/{dash_id}/grants/{cid}")
    assert r.status_code == 200, r.text
    return dash_id


def _request(authorization: str | None) -> Request:
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/mcp",
            "headers": [*headers, (b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 50000),
            "query_string": b"",
        }
    )


async def _resolve(ctx: ServerContext, secret: str | None) -> DashboardTokenAuth:
    return await resolve_dashboard_token(
        ctx, _request(None if secret is None else f"Bearer {secret}")
    )


async def _refused(ctx: ServerContext, secret: str | None, status: int) -> MapiError:
    with pytest.raises(MapiError) as info:
        await _resolve(ctx, secret)
    assert info.value.status == status, (info.value.code, info.value.detail)
    return info.value


# ======================================================================= route enumeration


async def test_a_read_token_is_refused_on_every_resolved_route(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    """Driven by the route table, so new routes are covered: every signed-in, tenant,
    project and platform route answers ``403`` to a dashboard read token (members' and
    clients' tokens alike)."""
    dash = await _v2_published(kit)  # read tokens are made for layout 2 only
    app = _app(ctx)
    for who in ("ana", "cleo"):
        _, secret = await _read_token(kit, who, dash)
        tok = token_caller(client, secret)
        checked = 0
        seen: set[str] = set()
        for route, template in iter_api_routes(app):
            op = route.operation_id or route.name
            if access_for(op).scope in (RouteScope.PUBLIC, RouteScope.INTERNAL):
                continue
            vals = {**_VALUES, "dashboard_id": dash}
            path = _PARAM.sub(lambda mt, v=vals: v.get(mt.group(1), "zz_1"), template)
            for method in sorted(route.methods or ()):
                if method in ("HEAD", "OPTIONS"):
                    continue
                r = await tok.request(method, path, json={})
                assert r.status_code == 403, f"{who} {method} {path} ({op}) -> {r.text}"
                assert r.json()["code"] == "FORBIDDEN", (op, r.text)
                checked += 1
                seen.add(op)
        assert checked > 200
        for op in ("list_dashboards", "get_dashboard", "create_run", "create_token", "get_me",
                   "list_dashboard_read_tokens", "list_shared_readouts"):  # fmt: skip
            assert (op in seen) is (op != "list_shared_readouts"), op


async def test_public_routes_ignore_a_read_token(
    client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    dash = await _v2_published(kit)  # read tokens are made for layout 2 only
    _, secret = await _read_token(kit, "cleo", dash)
    tok = token_caller(client, secret)
    for path in ("/healthz", "/auth/session"):
        plain = await client.get(path)
        if plain.status_code == 404:
            continue
        with_token = await tok.get(path)
        assert with_token.status_code == plain.status_code, path
        assert with_token.json() == plain.json(), path


async def test_api_tokens_never_reach_session_only_operations(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    """An ordinary token that the owner's role and the scope would allow (``admin``) still
    gets ``403`` on every session-only operation the app serves."""
    dash = await _v2_published(kit)  # read tokens are made for layout 2 only
    secret = await make_token(ctx, kit["users"]["ana"], "default", "default", ["admin"])
    tok = token_caller(client, secret)
    vals = {
        **_VALUES,
        "dashboard_id": dash,
        "conversation_id": "cnv_" + "0" * 26,
        "token_id": "tok_" + "0" * 26,
    }
    checked: set[str] = set()
    for route, template in iter_api_routes(_app(ctx)):
        op = route.operation_id or route.name
        if op not in SESSION_ONLY_OPERATIONS:
            continue
        path = _PARAM.sub(lambda mt: vals.get(mt.group(1), "zz_1"), template)
        for method in sorted(route.methods or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            r = await tok.request(method, path, json={"name": "x"})
            assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", (op, r.text)
            checked.add(op)
    assert {
        "list_dashboard_read_tokens",
        "create_dashboard_read_token",
        "revoke_dashboard_read_token",
    } <= checked
    # the same session can do all of them
    r = await kit["ana"].post(f"/v1/dashboards/{dash}/read-tokens", json={"name": "x"})
    assert r.status_code == 201, r.text


def test_token_never_is_unchanged() -> None:
    """v0.20 adds no pair to :data:`TOKEN_NEVER` and takes none away."""
    assert (
        frozenset(
            {
                ("token", "create"),
                ("endpoint", "manage"),
                ("model", "approve"),
                ("dashboard_grant", "create"),
                ("dashboard_share", "create"),
                ("plugin", "approve"),
                ("plugin", "delete"),
                ("plugin_settings", "update"),
            }
        )
        == TOKEN_NEVER
    )


# ==================================================================== resolve_dashboard_token


async def test_resolve_a_valid_read_token(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash = await _v2_published(kit)  # read tokens are made for layout 2 only
    token_id, secret = await _read_token(kit, "cleo", dash)
    async with ctx.db.session() as s:
        assert (await s.get(m.ApiToken, token_id)).last_used_at is None  # type: ignore[union-attr]
    tok = await _resolve(ctx, secret)
    assert tok == DashboardTokenAuth(
        token_id=token_id,
        tenant_id="default",
        project_id="default",
        owner_id=kit["users"]["cleo"].id,
        owner_kind="user",
        dashboard_id=dash,
    )
    async with ctx.db.session() as s:
        row = await s.get(m.ApiToken, token_id)
        assert row is not None and row.last_used_at is not None


async def test_resolve_refuses_bad_and_dead_tokens(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash = await _v2_published(kit)  # read tokens are made for layout 2 only
    token_id, secret = await _read_token(kit, "cleo", dash)
    # missing, other schemes, malformed, unknown
    await _refused(ctx, None, 401)
    for header in ("Basic abc", "Bearer", f"Token {secret}"):
        with pytest.raises(MapiError) as info:
            await resolve_dashboard_token(ctx, _request(header))
        assert info.value.status == 401, header
    await _refused(ctx, "mapi_notatoken", 401)
    await _refused(ctx, "a.b.c", 401)  # an ID token is not a read token
    await _refused(ctx, str(generate_token()), 401)
    # an ordinary, valid API token: MCP takes read tokens and share sessions only
    ordinary = await make_token(ctx, kit["users"]["ana"], "default", "default", ["admin"])
    err = await _refused(ctx, ordinary, 403)
    assert err.code.value == "FORBIDDEN"
    # each dead state answers 401; restoring it brings the token back
    cid = kit["users"]["cleo"].id
    cases: list[tuple[Any, dict[str, Any], dict[str, Any]]] = [
        (
            update(m.ApiToken).where(m.ApiToken.id == token_id),
            {"created_at": now() - timedelta(days=2), "expires_at": now() - timedelta(seconds=1)},
            {"expires_at": now() + timedelta(days=1)},
        ),
        (update(m.User).where(m.User.id == cid), {"status": "disabled"}, {"status": "active"}),
        (
            update(m.Tenant).where(m.Tenant.id == "default"),
            {"status": "suspended"},
            {"status": "active"},
        ),
        (
            update(m.ApiToken).where(m.ApiToken.id == token_id),
            {"revoked_at": now()},
            {"revoked_at": None},
        ),
    ]
    for stmt, dead, alive in cases:
        async with ctx.db.session() as s:
            await s.execute(stmt.values(**dead))
        await _refused(ctx, secret, 401)
        async with ctx.db.session() as s:
            await s.execute(stmt.values(**alive))
        assert (await _resolve(ctx, secret)).token_id == token_id
    # a token whose scope was tampered with (no dashboard column) is not a read token
    async with ctx.db.session() as s:
        await s.execute(
            update(m.ApiToken).where(m.ApiToken.id == token_id).values(dashboard_id=None)
        )
    await _refused(ctx, secret, 403)


async def test_failures_are_audited_with_a_bound(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash = await _v2_published(kit)  # read tokens are made for layout 2 only
    token_id, secret = await _read_token(kit, "cleo", dash)
    parsed = parse_token(secret)
    other = str(generate_token())
    wrong_secret = other.split("_")[2][: len(parsed.secret)]
    wrong = ParsedToken(parsed.prefix, wrong_secret, token_checksum(parsed.prefix, wrong_secret))
    for _ in range(FAILURE_AUDITS_PER_MINUTE + 4):
        await _refused(ctx, str(wrong), 401)
    async with ctx.db.session() as s:
        failures = (
            await s.execute(
                select(func.count()).where(
                    m.AuditEvent.action == "token.auth_failed",
                    m.AuditEvent.resource_id == token_id,
                )
            )
        ).scalar_one()
    assert failures == FAILURE_AUDITS_PER_MINUTE


# ============================================================ access is re-checked on use


async def test_grant_removed_token_dead(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash = await _v2_published(kit)
    _, secret = await _read_token(kit, "cleo", dash)
    tok = await _resolve(ctx, secret)
    dctx = await token_context(ctx, tok)
    assert dctx.dashboard_id == dash and dctx.principal_kind == "client"
    assert dctx.is_analyst is False
    cid = kit["users"]["cleo"].id
    r = await kit["ana"].delete(f"/v1/dashboards/{dash}/grants/{cid}")
    assert r.status_code == 204, r.text
    tok = await _resolve(ctx, secret)  # the token itself is still valid ...
    with pytest.raises(MapiError) as info:
        await token_context(ctx, tok)  # ... but it reads nothing any more
    assert info.value.status == 404
    r = await kit["ana"].put(f"/v1/dashboards/{dash}/grants/{cid}")
    assert r.status_code == 200
    assert (await token_context(ctx, await _resolve(ctx, secret))).dashboard_id == dash


async def test_member_removed_token_dead(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash = await _v2_published(kit)
    _, secret = await _read_token(kit, "vic", dash)
    dctx = await token_context(ctx, await _resolve(ctx, secret))
    assert dctx.principal_kind == "member" and dctx.is_analyst is False
    vid = kit["users"]["vic"].id
    async with ctx.db.session() as s:
        await s.execute(m.Membership.__table__.delete().where(m.Membership.principal_id == vid))
    with pytest.raises(MapiError) as info:
        await token_context(ctx, await _resolve(ctx, secret))
    assert info.value.status == 404
