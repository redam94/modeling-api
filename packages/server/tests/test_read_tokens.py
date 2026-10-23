"""Dashboard read tokens (v0.20, plan D12-D13, WP6): mint, list and revoke.

``cleo`` is a client of ``default/default`` with a grant on the published dashboard;
``ana`` (admin), ``eve`` (editor) and ``vic`` (viewer) are members. The server forces each
token's scope (``dashboard:<id>``), owner and lifetime; the secret is returned once; the
routes take a web session only; the generic ``/v1/tokens`` routes neither make nor show
these tokens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.api import dashboard_read_tokens
from mapi_server.app import create_app, iter_api_routes
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller, make_token, token_caller
from mapi_spec.chat import ReadToken, ReadTokenCreated
from mapi_spec.tokens import dashboard_scope
from sqlalchemy import select, update
from test_dashboards_crud import LAYOUT1, create_published, dashboard_kit, dashboard_spec

pytestmark = pytest.mark.integration


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
    kit = await dashboard_kit(ctx, client)
    kit["dash"] = await create_published(kit)
    kit["ctx"] = ctx
    return kit


def _url(dash_id: str, token_id: str | None = None) -> str:
    base = f"/v1/dashboards/{dash_id}/read-tokens"
    return base if token_id is None else f"{base}/{token_id}"


async def _mint(caller: Caller, dash_id: str, name: str = "Claude Desktop", **kw: Any) -> Any:
    return await caller.post(_url(dash_id), json={"name": name, **kw})


async def _audits(ctx: ServerContext, action: str) -> list[m.AuditEvent]:
    async with ctx.db.session() as s:
        stmt = select(m.AuditEvent).where(m.AuditEvent.action == action)
        return list((await s.execute(stmt)).scalars())


# ===================================================================================== mint


async def test_a_client_mints_a_token_for_a_granted_dashboard(kit: dict[str, Any]) -> None:
    ctx: ServerContext = kit["ctx"]
    cleo: Caller = kit["cleo"]
    dash = kit["dash"]
    r = await _mint(cleo, dash, expires_in_days=7)
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    created = ReadTokenCreated.model_validate(r.json())
    cid = kit["users"]["cleo"].id
    assert created.dashboard_id == dash and created.owner_id == cid
    assert created.owner_label == "Cleo"
    assert created.secret.startswith(f"mapi_{created.prefix}_")
    lifetime = created.expires_at - created.created_at
    assert lifetime == timedelta(days=7)
    async with ctx.db.session() as s:
        row = await s.get(m.ApiToken, created.id)
        assert row is not None
        # the server sets the scope, the owner and the dashboard; only the HMAC is stored
        assert row.scopes == [dashboard_scope(dash)] and row.dashboard_id == dash
        assert (row.owner_id, row.owner_kind, row.project_id) == (cid, "user", "default")
        assert created.secret.split("_")[2] not in str(row.__dict__)
    # listed without the secret
    r = await cleo.get(_url(dash))
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()] == [created.id]
    assert created.secret not in r.text and "secret" not in r.json()[0]
    ReadToken.model_validate(r.json()[0])
    (audit,) = await _audits(ctx, "dashboard.read_token_create")
    assert audit.resource_id == created.id and audit.actor_id == cid
    assert created.secret not in str(audit.details) and audit.details["dashboard_id"] == dash


async def test_mint_is_refused_where_the_caller_cannot_read(kit: dict[str, Any]) -> None:
    cleo: Caller = kit["cleo"]
    ungranted = await create_published(kit, "other", grant=False)
    for dash in (ungranted, "dsh_" + "0" * 26):
        r = await _mint(cleo, dash)
        assert r.status_code == 404, (dash, r.text)
    # a draft: an editor sees it, but there is nothing published to read yet
    r = await kit["eve"].post("/v1/dashboards", json={"name": "wip", "spec": dashboard_spec()})
    draft = r.json()["id"]
    r = await _mint(kit["eve"], draft)
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_NOT_PUBLISHED"
    # the request cannot name a scope or an owner (extra fields are refused)
    r = await _mint(cleo, kit["dash"], scopes=["admin"])
    assert r.status_code == 422, r.text
    r = await _mint(cleo, kit["dash"], owner_id=kit["users"]["ana"].id)
    assert r.status_code == 422, r.text


async def test_mint_and_management_need_a_web_session(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    dash = kit["dash"]
    r = await _mint(kit["ana"], dash)
    assert r.status_code == 201, r.text
    token_id = r.json()["id"]
    admin = token_caller(
        client,
        await make_token(ctx, kit["users"]["ana"], "default", "default", ["admin"]),
    )
    for method, url in (
        ("POST", _url(dash)),
        ("GET", _url(dash)),
        ("DELETE", _url(dash, token_id)),
    ):
        r = await admin.request(method, url, json={"name": "x"})
        assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", (method, r.text)
    assert len(await _audits(ctx, "dashboard.read_token_create")) == 1


async def test_lifetime_is_capped(ctx: ServerContext, kit: dict[str, Any]) -> None:
    cleo: Caller = kit["cleo"]
    r = await _mint(cleo, kit["dash"], expires_in_days=91)
    assert r.status_code == 422, r.text
    r = await _mint(cleo, kit["dash"], expires_in_days=0)
    assert r.status_code == 422, r.text
    r = await _mint(cleo, kit["dash"])
    assert r.status_code == 201, r.text
    created = ReadTokenCreated.model_validate(r.json())
    assert created.expires_at - created.created_at == timedelta(days=30)  # the default
    # an operator may lower the ceiling below 90 days
    ctx.settings = ctx.settings.model_copy(update={"dashboard_read_token_max_days": 10})
    r = await _mint(cleo, kit["dash"], expires_in_days=11)
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID", r.text
    r = await _mint(cleo, kit["dash"], expires_in_days=10)
    assert r.status_code == 201, r.text


async def test_at_most_five_active_tokens_per_user_and_dashboard(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    cleo: Caller = kit["cleo"]
    dash = kit["dash"]
    ids = []
    for i in range(5):
        r = await _mint(cleo, dash, f"t{i}")
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    r = await _mint(cleo, dash, "t5")
    assert r.status_code == 409 and r.json()["code"] == "READ_TOKEN_LIMIT", r.text
    # another user and another dashboard have their own count
    assert (await _mint(kit["vic"], dash)).status_code == 201
    other = await create_published(kit, "other")
    assert (await _mint(cleo, other)).status_code == 201
    # a revoked or expired token no longer counts
    assert (await cleo.delete(_url(dash, ids[0]))).status_code == 204
    async with ctx.db.session() as s:
        await s.execute(
            update(m.ApiToken)
            .where(m.ApiToken.id == ids[1])
            .values(expires_at=now() - timedelta(seconds=1))
        )
    assert (await _mint(cleo, dash, "again")).status_code == 201
    assert (await _mint(cleo, dash, "and again")).status_code == 201
    r = await _mint(cleo, dash, "too many")
    assert r.status_code == 409 and r.json()["code"] == "READ_TOKEN_LIMIT"
    # listing shows what stopped working too (newest first), never a secret
    listed = (await cleo.get(_url(dash))).json()
    assert len(listed) == 7 and {t["owner_id"] for t in listed} == {kit["users"]["cleo"].id}
    revoked = [t for t in listed if t["id"] == ids[0]]
    assert revoked and revoked[0]["revoked_at"] is not None


# ============================================================================= list, revoke


async def test_revoke_own_and_editors_revoke_any(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash = kit["dash"]
    cleo, vic, eve = kit["cleo"], kit["vic"], kit["eve"]
    c1 = (await _mint(cleo, dash, "c1")).json()["id"]
    c2 = (await _mint(cleo, dash, "c2")).json()["id"]
    v1 = (await _mint(vic, dash, "v1")).json()["id"]
    # each owner lists their own tokens; another owner's token does not exist for them
    assert [t["id"] for t in (await cleo.get(_url(dash))).json()] == sorted([c1, c2], reverse=True)
    assert [t["id"] for t in (await vic.get(_url(dash))).json()] == [v1]
    r = await vic.delete(_url(dash, c1))
    assert r.status_code == 404, r.text
    r = await cleo.delete(_url(dash, v1))
    assert r.status_code == 404, r.text
    # every owner's tokens: editors only
    r = await vic.get(_url(dash) + "?all=true")
    assert r.status_code == 403, r.text
    r = await cleo.get(_url(dash) + "?all=true")
    assert r.status_code == 403, r.text
    r = await eve.get(_url(dash) + "?all=true")
    assert r.status_code == 200, r.text
    assert {t["id"] for t in r.json()} == {c1, c2, v1}
    labels = {t["id"]: t["owner_label"] for t in r.json()}
    assert labels[v1] == "Vic" and labels[c1] == "Cleo"
    # revoke own (idempotent), and an editor revokes anyone's
    assert (await cleo.delete(_url(dash, c1))).status_code == 204
    assert (await cleo.delete(_url(dash, c1))).status_code == 204
    assert (await eve.delete(_url(dash, c2))).status_code == 204
    assert (await eve.delete(_url(dash, v1))).status_code == 204
    async with ctx.db.session() as s:
        rows = (
            await s.execute(select(m.ApiToken).where(m.ApiToken.id.in_([c1, c2, v1])))
        ).scalars()
        assert all(r.revoked_at is not None for r in rows)
    audits = await _audits(ctx, "dashboard.read_token_revoke")
    assert sorted(a.resource_id or "" for a in audits) == sorted([c1, c2, v1])
    eve_id = kit["users"]["eve"].id
    assert {a.actor_id for a in audits if a.resource_id in (c2, v1)} == {eve_id}
    # a token of another dashboard is not found under this one
    other = await create_published(kit, "other")
    o1 = (await _mint(cleo, other, "o1")).json()["id"]
    assert (await eve.delete(_url(dash, o1))).status_code == 404
    assert (await cleo.delete(_url(other, o1))).status_code == 204


async def test_generic_token_routes_neither_make_nor_show_read_tokens(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    ana: Caller = kit["ana"]
    dash = kit["dash"]
    rt = (await _mint(ana, dash)).json()["id"]
    r = await ana.post("/v1/tokens", json={"name": "sneaky", "scopes": [dashboard_scope(dash)]})
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID", r.text
    r = await ana.post(
        "/v1/tokens", json={"name": "mixed", "scopes": [dashboard_scope(dash), "models:read"]}
    )
    assert r.status_code == 422, r.text
    r = await ana.post("/v1/tokens", json={"name": "ci", "scopes": ["models:read"]})
    assert r.status_code == 201, r.text
    ordinary = r.json()["token"]["id"]
    for query in ("", "?all=true"):
        listed = (await ana.get("/v1/tokens" + query)).json()["items"]
        assert [t["id"] for t in listed] == [ordinary], query
    assert (await ana.get(f"/v1/tokens/{rt}")).status_code == 404
    assert (await ana.delete(f"/v1/tokens/{rt}")).status_code == 404
    async with ctx.db.session() as s:
        row = await s.get(m.ApiToken, rt)
        assert row is not None and row.revoked_at is None


async def test_deleting_the_dashboard_deletes_its_tokens(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash = kit["dash"]
    rt = (await _mint(kit["cleo"], dash)).json()["id"]
    async with ctx.db.session() as s:
        await s.execute(m.Dashboard.__table__.delete().where(m.Dashboard.id == dash))
    async with ctx.db.session() as s:
        assert await s.get(m.ApiToken, rt) is None
