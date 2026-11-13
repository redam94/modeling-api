"""Who can do what in the library (v0.18 D2, I-4): project items for editors, tenant items
for tenant admins (create, revise, archive, promote), viewers read only, tenant items are
read from every project of the tenant, other tenants see nothing, and clients reach no
library route."""

from __future__ import annotations

import copy
import functools
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
from mapi_spec.api import CLIENT_OPERATIONS, V018_ENDPOINTS
from mapi_spec.library import LibraryItem
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

URL = "/v1/library"
PRESET: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "PrepPreset",
    "metadata": {"name": "log-sales", "title": "Log sales"},
    "steps": [{"id": "log", "use": "core.log1p@^1", "with": {"vars": ["sales"]}}],
}


@functools.cache
def _checkout_catalog() -> str:
    registry = pytest.importorskip("mapi_sdk.registry")
    text: str = registry.default_registry().catalog().model_dump_json()
    return text


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    path = tmp_path / "checkout_catalog.json"
    path.write_text(_checkout_catalog())
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT, "catalog_path": path})


async def _people(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    await make_tenant(ctx, "acme", projects=("growth", "retail"))
    await make_tenant(ctx, "other", projects=("main",))
    out: dict[str, Any] = {}
    for role, project in (("viewer", "growth"), ("editor", "growth"), ("retail_editor", "retail")):
        u = await make_user(ctx, f"{role}@acme.test")
        await grant_role(ctx, u, "acme", project, role.removeprefix("retail_"))
        out[role] = await session_caller(ctx, client, u, tenant_id="acme", project_id=project)
        out[f"{role}_user"] = u
    admin = await make_user(ctx, "admin@acme.test")
    await grant_role(ctx, admin, "acme", None, "admin")
    out["admin"] = await session_caller(ctx, client, admin, tenant_id="acme", project_id="growth")
    out["admin_user"] = admin
    cl = await make_user(ctx, "client@acme.test")
    await grant_role(ctx, cl, "acme", "growth", "client")
    out["client"] = await session_caller(ctx, client, cl, tenant_id="acme", project_id="growth")
    outsider = await make_user(ctx, "admin@other.test")
    await grant_role(ctx, outsider, "other", None, "admin")
    out["outsider"] = await session_caller(
        ctx, client, outsider, tenant_id="other", project_id="main"
    )
    return out


def _doc(**metadata: Any) -> dict[str, Any]:
    doc = copy.deepcopy(PRESET)
    doc["metadata"].update(metadata)
    return doc


async def _create(caller: Any, doc: dict[str, Any], scope: str = "project") -> LibraryItem:
    r = await caller.post(URL, json={"document": doc, "scope": scope})
    assert r.status_code == 201, r.text
    return LibraryItem.model_validate(r.json())


async def test_viewers_read_only(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    who = await _people(ctx, client)
    item = await _create(who["editor"], PRESET)
    v = who["viewer"]
    assert (await v.get(URL)).json()["items"][0]["id"] == item.id
    assert (await v.get(f"{URL}/{item.id}")).status_code == 200
    assert (await v.get(f"{URL}/{item.id}/revisions/1")).status_code == 200
    for method, path, body in (
        ("POST", URL, {"document": _doc(name="v")}),
        ("POST", f"{URL}/{item.id}/revisions", {"document": PRESET}),
        ("POST", f"{URL}/{item.id}:archive", None),
        ("POST", f"{URL}/{item.id}:unarchive", None),
        ("POST", f"{URL}/{item.id}:promote", None),
    ):
        r = await v.request(method, path, json=body)
        assert r.status_code == 403, (path, r.text)
        assert r.json()["code"] == "FORBIDDEN"


async def test_tenant_items_need_a_tenant_admin(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    r = await who["editor"].post(URL, json={"document": PRESET, "scope": "tenant"})
    assert r.status_code == 403
    shared = await _create(who["admin"], PRESET, scope="tenant")
    assert (shared.scope, shared.project_id) == ("tenant", None)

    # every project of the tenant reads it ...
    retail = who["retail_editor"]
    ids = [i["id"] for i in (await retail.get(URL)).json()["items"]]
    assert ids == [shared.id]
    assert (await retail.get(URL, params={"scope": "project"})).json()["items"] == []
    assert (await who["viewer"].get(f"{URL}/{shared.id}")).status_code == 200
    # ... but only tenant admins change it
    for path, body in (
        (f"{URL}/{shared.id}/revisions", {"document": PRESET}),
        (f"{URL}/{shared.id}:archive", None),
    ):
        r = await retail.post(path, json=body)
        assert r.status_code == 403, (path, r.text)
    r = await who["admin"].post(f"{URL}/{shared.id}/revisions", json={"document": PRESET})
    assert r.status_code == 201
    assert (await who["admin"].post(f"{URL}/{shared.id}:archive")).status_code == 200
    async with ctx.db.session() as s:
        kinds = (
            await s.execute(
                select(m.AuditEvent.resource_kind).where(m.AuditEvent.resource_id == shared.id)
            )
        ).scalars()
        assert set(kinds) == {"tenant_library"}


async def test_project_items_stay_in_their_project(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    item = await _create(who["editor"], PRESET)
    # the same name in another project is another item
    other = await _create(who["retail_editor"], PRESET)
    assert other.id != item.id
    retail = who["retail_editor"]
    assert [i["id"] for i in (await retail.get(URL)).json()["items"]] == [other.id]
    for path in (f"{URL}/{item.id}", f"{URL}/{item.id}/revisions"):
        assert (await retail.get(path)).status_code == 404
    r = await retail.post(f"{URL}/{item.id}:use", json={})
    assert r.status_code == 404
    # the tenant admin reads it in its project
    assert (await who["admin"].get(f"{URL}/{item.id}")).status_code == 200


async def test_promote_copies_the_latest_revision(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    item = await _create(who["editor"], PRESET)
    revised = _doc(title="Log sales (v2)")
    r = await who["editor"].post(f"{URL}/{item.id}/revisions", json={"document": revised})
    assert r.status_code == 201

    assert (await who["editor"].post(f"{URL}/{item.id}:promote")).status_code == 403
    r = await who["admin"].post(f"{URL}/{item.id}:promote")
    assert r.status_code == 201, r.text
    shared = LibraryItem.model_validate(r.json())
    assert (shared.scope, shared.latest_revision, shared.title) == ("tenant", 1, "Log sales (v2)")
    rev = (await who["admin"].get(f"{URL}/{shared.id}/revisions/1")).json()
    assert rev["document"] == revised and rev["valid"] is True
    assert rev["message"] == f"promoted from {item.id} r2"
    # the project item is unchanged; promoting again (or a tenant item) conflicts
    assert (await who["editor"].get(f"{URL}/{item.id}")).json()["scope"] == "project"
    assert (await who["admin"].post(f"{URL}/{item.id}:promote")).status_code == 409
    assert (await who["admin"].post(f"{URL}/{shared.id}:promote")).status_code == 409
    # the retail project sees the promoted item, not the growth one
    ids = [i["id"] for i in (await who["retail_editor"].get(URL)).json()["items"]]
    assert ids == [shared.id]
    async with ctx.db.session() as s:
        ev = (
            await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == "library.promote"))
        ).scalar_one()
    assert ev.resource_id == shared.id and ev.details["from"] == item.id


async def test_other_tenants_see_nothing(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    who = await _people(ctx, client)
    item = await _create(who["editor"], PRESET)
    shared = await _create(who["admin"], _doc(name="shared"), scope="tenant")
    out = who["outsider"]
    assert (await out.get(URL)).json()["items"] == []
    for it in (item, shared):
        assert (await out.get(f"{URL}/{it.id}")).status_code == 404
        assert (await out.post(f"{URL}/{it.id}:archive")).status_code == 404
        assert (await out.post(f"{URL}/{it.id}:use", json={})).status_code == 404
    # nor through the other tenant's header
    stray = who["outsider"].in_project("acme", "growth")
    assert (await stray.get(URL)).status_code == 404


async def test_clients_reach_no_library_route(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """Driven by the route table: every library operation is outside
    :data:`CLIENT_OPERATIONS`, and the client gate answers as if it did not exist."""
    who = await _people(ctx, client)
    item = await _create(who["editor"], PRESET)
    ops = [e for e in V018_ENDPOINTS if e.path.startswith("/v1/library")]
    assert len(ops) == 12
    for e in ops:
        assert e.operation_id not in CLIENT_OPERATIONS
        path = e.path.replace("{item_id}", item.id).replace("{revision}", "1")
        body = {"document": PRESET} if e.method == "POST" else None
        r = await who["client"].request(e.method, path, json=body)
        assert r.status_code in (403, 404), (e.operation_id, r.status_code)
        assert r.status_code == 404  # the v0.16 client gate: "not found", never a hint


async def test_tokens_read_and_write_with_their_scopes(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    item = await _create(who["editor"], PRESET)
    head = {"X-Mapi-Project": "acme/growth"}
    reader = token_caller(
        client,
        await make_token(ctx, who["editor_user"], "acme", "growth", ["models:read"]),
        **head,
    )
    assert (await reader.get(f"{URL}/{item.id}")).status_code == 200
    assert (await reader.post(URL, json={"document": _doc(name="tok")})).status_code == 403
