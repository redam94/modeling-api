"""``GET /v1/catalog/diagnostics`` (tenant admins and operators) and the operator-only
``POST /v1/admin/catalog:describe`` (v0.18)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import (
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_spec.audit import AuditAction
from mapi_spec.executor import RunKind
from mapi_spec.run import RunState
from sqlalchemy import select, update
from test_catalog_schema_safety import hostile_catalog

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

OPS = "ops@platform.test"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    path = tmp_path / "catalog.json"
    data: dict[str, Any] = hostile_catalog()
    data["diagnostics"] = [
        {"code": "load_error", "group": "mapi.models", "entry_point": "acme",
         "distribution": "acme-models", "message": "The entry point could not be loaded.",
         "untrusted_text": "ModuleNotFoundError: No module named 'acme_models'"},
    ]  # fmt: skip
    path.write_text(json.dumps(data))
    return settings.model_copy(
        update={"catalog_path": path, "profile": Profile.MULTI_TENANT, "operator_emails": [OPS]}
    )


async def _people(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    await make_tenant(ctx, "acme", projects=("growth",))
    out: dict[str, Any] = {}
    op = await make_user(ctx, OPS, operator=True)
    out["operator"] = await session_caller(ctx, client, op)
    admin = await make_user(ctx, "admin@acme.test")
    await grant_role(ctx, admin, "acme", None, "admin")
    out["tenant_admin"] = await session_caller(ctx, client, admin)
    out["admin_user"] = admin
    for role in ("editor", "runner", "viewer"):
        u = await make_user(ctx, f"{role}@acme.test")
        await grant_role(ctx, u, "acme", "growth", role)
        out[role] = await session_caller(ctx, client, u, tenant_id="acme", project_id="growth")
    # an admin of one project only is not a tenant admin
    padmin = await make_user(ctx, "padmin@acme.test")
    await grant_role(ctx, padmin, "acme", "growth", "admin")
    out["project_admin"] = await session_caller(
        ctx, client, padmin, tenant_id="acme", project_id="growth"
    )
    return out


async def test_tenant_admins_and_operators_read_the_diagnostics(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    for name in ("operator", "tenant_admin"):
        r = await who[name].get("/v1/catalog/diagnostics")
        assert r.status_code == 200, (name, r.text)
        rows = r.json()
        assert rows[0]["code"] == "load_error"  # the runner's rows first
        assert rows[0]["untrusted_text"].startswith("ModuleNotFoundError")
        assert {d["code"] for d in rows[1:]} == {"schema_unsafe"}
        assert {d["group"] for d in rows[1:]} == {"mapi.models", "mapi.steps", "mapi.plots"}
    for name in ("editor", "runner", "viewer", "project_admin"):
        r = await who[name].get("/v1/catalog/diagnostics")
        assert r.status_code == 403, (name, r.text)
        assert r.json()["code"] == "FORBIDDEN"
    assert (await client.get("/v1/catalog/diagnostics")).status_code == 401


async def test_a_token_needs_the_admin_scope_and_a_tenant_admin_owner(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    admin = who["admin_user"]
    full = await make_token(ctx, admin, "acme", "growth", ["admin"])
    narrow = await make_token(ctx, admin, "acme", "growth", ["models:read"])
    assert (await token_caller(client, full).get("/v1/catalog/diagnostics")).status_code == 200
    assert (await token_caller(client, narrow).get("/v1/catalog/diagnostics")).status_code == 403


async def test_the_catalog_list_does_not_carry_the_diagnostics(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    r = await who["viewer"].get("/v1/catalog")
    assert r.status_code == 200
    assert "diagnostics" not in r.json()


async def test_only_operators_queue_a_describe_run(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    for name in ("tenant_admin", "editor"):
        r = await who[name].post("/v1/admin/catalog:describe")
        assert r.status_code in (403, 404), (name, r.text)
    async with ctx.db.session() as s:
        assert (await s.execute(select(m.Run).where(m.Run.kind == "describe"))).first() is None

    r = await who["operator"].post("/v1/admin/catalog:describe")
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["kind"] == RunKind.DESCRIBE.value
    assert run["state"] == RunState.QUEUED.value
    # a second request while it is pending queues nothing and returns that run
    again = await who["operator"].post("/v1/admin/catalog:describe")
    assert again.status_code == 202 and again.json()["id"] == run["id"]
    async with ctx.db.session() as s:
        runs = list((await s.execute(select(m.Run).where(m.Run.kind == "describe"))).scalars())
        assert [x.payload["image_digest"] for x in runs] == [ctx.catalogs.image_digest]
        audits = list(
            (
                await s.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == AuditAction.CATALOG_DESCRIBE_REQUESTED.value
                    )
                )
            ).scalars()
        )
        assert [a.details["pending"] for a in audits] == [False, True]
        # once it failed, the next request queues a new one
        await s.execute(
            update(m.Run).where(m.Run.id == run["id"]).values(state=RunState.FAILED.value)
        )
    third = await who["operator"].post("/v1/admin/catalog:describe")
    assert third.status_code == 202 and third.json()["id"] != run["id"]
