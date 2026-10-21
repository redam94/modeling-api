"""The ``client`` role (v0.16, plan D4): granted, published dashboards and nothing else.

``cleo`` is a client of ``default/default``; ``mia`` is a client there too but a viewer of
``default/other`` (a mixed user). Every operation outside
:data:`mapi_spec.api.CLIENT_OPERATIONS` answers ``404`` to a client, whatever its route
scope, before any handler runs.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from mapi_server.app import create_app, iter_api_routes
from mapi_server.auth.request import access_for
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import (
    Caller,
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_spec.api import ALL_ENDPOINTS, CLIENT_OPERATIONS, RouteScope
from sqlalchemy import func, select, update
from test_dashboards_crud import create_published, dashboard_kit, dashboard_spec

pytestmark = pytest.mark.integration

_PARAM = re.compile(r"\{([a-z_]+)(?::[a-z]+)?\}")
_VALUES = {"tenant_id": "default", "project_id": "default", "version": "1", "seq": "1"}
_DECLS = {e.operation_id: e for e in ALL_ENDPOINTS}


def _path(op: str, **values: str) -> tuple[str, str]:
    decl = _DECLS[op]
    vals = {**_VALUES, **values}
    return decl.method, _PARAM.sub(lambda mt: vals.get(mt.group(1), "zz_1"), decl.path)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return {**await dashboard_kit(ctx, client), "ctx": ctx}


async def test_a_client_sees_only_granted_published_dashboards(kit: dict[str, Any]) -> None:
    granted = await create_published(kit, "granted")
    other = await create_published(kit, "other", grant=False)
    r = await kit["eve"].post("/v1/dashboards", json={"name": "wip", "spec": dashboard_spec()})
    draft = r.json()["id"]
    cid = kit["users"]["cleo"].id
    await kit["ana"].put(f"/v1/dashboards/{draft}/grants/{cid}")  # granted, never published
    cleo: Caller = kit["cleo"]
    r = await cleo.get("/v1/dashboards")
    assert r.status_code == 200, r.text
    assert [d["id"] for d in r.json()["items"]] == [granted]
    r = await cleo.get(f"/v1/dashboards/{granted}")
    assert r.status_code == 200, r.text
    body = r.json()
    # the client view: the published revision, no draft, no people, no model internals
    assert body["title"] == "Weekly media results" and body["kpi"]["variable"] == "y"
    for key in ("draft_spec", "run_as", "created_by", "published_revision", "name"):
        assert key not in body
    assert body["inputs"]["whatif"] is True and body["inputs"]["actuals"] is False
    assert "update" not in body["tabs"]
    for dash_id in (other, draft, "dsh_" + "0" * 26):
        r = await cleo.get(f"/v1/dashboards/{dash_id}")
        assert r.status_code == 404, (dash_id, r.text)
    # management stays with the analysts (and answers as if it did not exist)
    for op in ("list_dashboard_revisions", "list_dashboard_grants", "list_dashboard_shares",
               "publish_dashboard", "update_dashboard_draft", "create_dashboard",
               "list_dashboard_refit_requests", "approve_dashboard_refit_request"):  # fmt: skip
        method, path = _path(op, dashboard_id=granted, request_id="drr_" + "0" * 26)
        r = await cleo.request(method, path, json={})
        assert r.status_code == 404, (op, r.status_code, r.text)


async def test_a_client_reaches_no_other_route(kit: dict[str, Any]) -> None:
    cleo: Caller = kit["cleo"]
    for op in ("list_projects", "get_catalog", "list_runs", "score_model", "create_token",
               "list_workflow_templates", "list_tenant_projects", "list_tenants",
               "get_project", "list_members", "list_guided_templates"):  # fmt: skip
        method, path = _path(op, model_id="trend")
        r = await cleo.request(method, path, json={})
        assert r.status_code == 404, (op, path, r.status_code, r.text)
    r = await cleo.get("/v1/me")
    assert r.status_code == 200, r.text
    roles = [p["role"] for t in r.json()["tenants"] for p in t["projects"]]
    assert roles == ["client"]
    # the tenant as a client sees it: id, name and status, nothing the operator wrote
    async with kit["ctx"].db.session() as s:
        await s.execute(
            update(m.Tenant).where(m.Tenant.id == "default").values(status_reason="billing note")
        )
    (tenant,) = [t["tenant"] for t in (await cleo.get("/v1/me")).json()["tenants"]]
    assert set(tenant) == {"id", "name", "status"}, tenant
    (full,) = [t["tenant"] for t in (await kit["vic"].get("/v1/me")).json()["tenants"]]
    assert full["trust_tier"] and full["status_reason"] == "billing note"


async def test_every_route_outside_the_allow_list_is_404_for_a_client(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    """Driven by the route table, so new routes are covered: a client gets 404 on every
    signed-in, tenant, project and platform route that is not a client operation."""
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    cleo: Caller = kit["cleo"]
    checked = 0
    for route, template in iter_api_routes(app):
        op = route.operation_id or route.name
        access = access_for(op)
        if access.scope in (RouteScope.PUBLIC, RouteScope.INTERNAL) or op in CLIENT_OPERATIONS:
            continue
        path = _PARAM.sub(lambda mt: _VALUES.get(mt.group(1), "zz_1"), template)
        for method in sorted(route.methods or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            r = await cleo.request(method, path, json={})
            assert r.status_code == 404, f"{method} {path} ({op}) -> {r.status_code} {r.text}"
            checked += 1
    assert checked > 150


async def test_a_mixed_user_keeps_their_viewer_routes(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    await make_tenant(ctx, "default", projects=("other",))
    mia = await make_user(ctx, "mia@default.test")
    await grant_role(ctx, mia, "default", "default", "client")
    await grant_role(ctx, mia, "default", "other", "viewer")
    in_default = await session_caller(ctx, client, mia, tenant_id="default", project_id="default")
    in_other = in_default.in_project("default", "other")
    assert (await in_default.get("/v1/runs")).status_code == 404
    assert (await in_other.get("/v1/runs")).status_code == 200
    assert (await in_default.get("/v1/projects")).status_code == 200  # not a client everywhere
    assert (await in_default.get("/v1/dashboards")).status_code == 200


async def test_a_client_cannot_mint_tokens_and_old_tokens_act_as_client(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    cleo: Caller = kit["cleo"]
    r = await cleo.post("/v1/tokens", json={"name": "x", "scopes": ["models:read"]})
    assert r.status_code == 404
    async with ctx.db.session() as s:
        denied = (
            await s.execute(
                select(func.count()).where(
                    m.AuditEvent.action == "token.create", m.AuditEvent.outcome == "denied"
                )
            )
        ).scalar_one()
    assert denied == 1  # refusals of privileged operations are audited
    # a token minted while its owner was a viewer acts with the owner's role now: client
    secret = await make_token(ctx, kit["users"]["cleo"], "default", "default", ["admin"])
    tok = token_caller(client, secret)
    assert (await tok.get("/v1/runs")).status_code == 404
    assert (await tok.get("/v1/dashboards")).status_code == 200


async def test_client_grants_are_project_scoped(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    ana: Caller = kit["ana"]
    newbie = {"email": "new@client.test", "role": "client"}
    r = await ana.post("/v1/tenants/default/members", json=newbie)
    assert r.status_code == 422 and r.json()["code"] == "INVALID_ROLE_SCOPE"
    r = await ana.post("/v1/tenants/default/projects/default/members", json=newbie)
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "client"
    # a tenant-wide grant cannot become client either
    eve_id = kit["users"]["eve"].id
    await grant_role(ctx, kit["users"]["vic"], "default", None, "viewer")
    vic_id = kit["users"]["vic"].id
    r = await ana.patch(f"/v1/tenants/default/members/{vic_id}", json={"role": "client"})
    assert r.status_code == 422 and r.json()["code"] == "INVALID_ROLE_SCOPE"
    r = await ana.patch(
        f"/v1/tenants/default/projects/default/members/{eve_id}", json={"role": "client"}
    )
    assert r.status_code == 200 and r.json()["role"] == "client"
