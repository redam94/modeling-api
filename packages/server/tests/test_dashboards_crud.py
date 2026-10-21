"""Client dashboards (v0.16): drafts, validation, publishing, archiving and grants.

The model is a fitted trend model of the platform tenant ``default`` with a response grid
(``ui/grids/response_curves.json``) over two channels. ``ana`` administers the tenant,
``eve`` edits the project, ``vic`` views it and ``cleo`` is a client of it. The helpers at the
top (:func:`dashboard_kit`, :func:`create_published`) are shared with the client-role and
share-link tests.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import load_run
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.builders import queued_fit, simulate_outputs
from mapi_server_testkit.identity import Caller, grant_role, make_user, session_caller
from mapi_server_testkit.plots import _to_finalizing
from mapi_spec.run import RunState
from sqlalchemy import select, update

pytestmark = pytest.mark.integration

GRID: dict[str, Any] = {
    "format": "mapi.response-grid/v1",
    "kpi": "y",
    "periods": 52,
    "channels": ["tv", "search"],
    "multipliers": [0.5, 1.0, 1.5],
    "draws": 2,
    "contribution": [[[700.0, 1000.0, 1150.0], [300.0, 500.0, 620.0]],
                     [[650.0, 950.0, 1100.0], [320.0, 520.0, 650.0]]],
    "spend": [[26000.0, 52000.0, 78000.0], [9000.0, 18000.0, 27000.0]],
    "current_spend": [52000.0, 18000.0],
    "baseline": [2600.0, 2700.0],
}  # fmt: skip


def dashboard_spec(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "title": "Weekly media results",
        "client_name": "Acme Retail",
        "source": {"kind": "model_version", "model_id": "trend"},
        "kpi": {"variable": "y", "label": "Orders", "unit": "count", "value_per_unit": 40.0},
        "channels": [{"name": "tv", "label": "TV"}, {"name": "search", "label": "Search"}],
        "tabs": ["this_period", "trends", "scenarios", "history"],
    }
    spec.update(over)
    return spec


async def model_with_grid(ctx: ServerContext) -> dict[str, Any]:
    """A ready version of the ``trend`` model whose bundle holds the response grid."""
    run_id = await queued_fit(ctx)
    run, att = await _to_finalizing(ctx, run_id)
    grid = json.dumps(GRID).encode()
    await simulate_outputs(
        ctx, run, att, add={"ui/grids/response_curves.json": (grid, "application/json")}
    )
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    async with ctx.db.session() as s:
        done = await load_run(s, None, run.id)
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
        assert mv is not None
    return {"model_id": mv.model_id, "version": mv.version, "mv_id": mv.id}


async def dashboard_kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    """Users of ``default/default`` (admin, editor, viewer, client) and a model with a grid."""
    model = await model_with_grid(ctx)
    users: dict[str, Any] = {}
    callers: dict[str, Caller] = {}
    for name, project, role in (
        ("ana", None, "admin"),
        ("eve", "default", "editor"),
        ("vic", "default", "viewer"),
        ("cleo", "default", "client"),
    ):
        user = await make_user(ctx, f"{name}@default.test")
        await grant_role(ctx, user, "default", project, role)
        users[name] = user
        callers[name] = await session_caller(
            ctx, client, user, tenant_id="default", project_id="default"
        )
    return {"model": model, "users": users, **callers}


async def create_published(
    kit: dict[str, Any], name: str = "acme-weekly", grant: bool = True, **spec: Any
) -> str:
    """A dashboard published by ``ana`` (and granted to ``cleo``); returns its id."""
    ana: Caller = kit["ana"]
    r = await ana.post("/v1/dashboards", json={"name": name, "spec": dashboard_spec(**spec)})
    assert r.status_code == 201, r.text
    dash_id = str(r.json()["id"])
    r = await ana.post(f"/v1/dashboards/{dash_id}:publish")
    assert r.status_code == 200, r.text
    if grant:
        cleo_id = kit["users"]["cleo"].id
        r = await ana.put(f"/v1/dashboards/{dash_id}/grants/{cleo_id}")
        assert r.status_code == 200, r.text
    return dash_id


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return await dashboard_kit(ctx, client)


# ==================================================================================== drafts


async def test_create_read_and_update_a_draft_with_its_etag(kit: dict[str, Any]) -> None:
    eve: Caller = kit["eve"]
    r = await eve.post("/v1/dashboards", json={"name": "acme", "spec": dashboard_spec()})
    assert r.status_code == 201, r.text
    body = r.json()
    dash_id, etag = body["id"], body["draft_etag"]
    assert body["status"] == "draft" and body["published_revision"] is None
    assert r.headers["etag"] == etag
    r = await eve.get(f"/v1/dashboards/{dash_id}")
    assert r.status_code == 200 and r.json()["draft_spec"]["title"] == "Weekly media results"
    # the name is unique in the project
    r = await eve.post("/v1/dashboards", json={"name": "acme", "spec": dashboard_spec()})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    new = {"spec": dashboard_spec(title="Monthly results")}
    r = await eve.put(f"/v1/dashboards/{dash_id}/draft", json=new)
    assert r.status_code == 412 and r.json()["code"] == "PRECONDITION_FAILED"
    r = await eve.put(f"/v1/dashboards/{dash_id}/draft", json=new, headers={"If-Match": '"9"'})
    assert r.status_code == 412 and r.json()["code"] == "DASHBOARD_SPEC_STALE"
    r = await eve.put(f"/v1/dashboards/{dash_id}/draft", json=new, headers={"If-Match": etag})
    assert r.status_code == 200, r.text
    assert r.json()["draft_spec"]["title"] == "Monthly results"
    assert r.json()["draft_etag"] != etag
    # the old etag is stale now
    r = await eve.put(f"/v1/dashboards/{dash_id}/draft", json=new, headers={"If-Match": etag})
    assert r.json()["code"] == "DASHBOARD_SPEC_STALE"


async def test_specs_are_plain_text(kit: dict[str, Any]) -> None:
    r = await kit["eve"].post(
        "/v1/dashboards", json={"name": "x", "spec": dashboard_spec(note="<b>bold</b>")}
    )
    assert r.status_code == 422


async def test_viewers_read_published_dashboards_but_not_drafts(kit: dict[str, Any]) -> None:
    published = await create_published(kit, grant=False)
    r = await kit["eve"].post("/v1/dashboards", json={"name": "wip", "spec": dashboard_spec()})
    draft = r.json()["id"]
    vic: Caller = kit["vic"]
    assert (await vic.get(f"/v1/dashboards/{published}")).status_code == 200
    assert (await vic.get(f"/v1/dashboards/{draft}")).status_code == 404
    ids = [d["id"] for d in (await vic.get("/v1/dashboards")).json()["items"]]
    assert ids == [published]
    ids = {d["id"] for d in (await kit["eve"].get("/v1/dashboards")).json()["items"]}
    assert ids == {published, draft}
    # viewers cannot write
    r = await vic.post("/v1/dashboards", json={"name": "v", "spec": dashboard_spec()})
    assert r.status_code == 403


# ================================================================================ validation


async def test_validate_points_at_what_to_fix(kit: dict[str, Any]) -> None:
    eve: Caller = kit["eve"]
    channels = [{"name": "tv", "label": "TV"}, {"name": "radio", "label": "Radio"}]
    spec = dashboard_spec(
        channels=channels, kpi={"variable": "sales", "label": "S", "unit": "ratio"}
    )
    r = await eve.post("/v1/dashboards", json={"name": "bad", "spec": spec})
    dash_id = r.json()["id"]
    r = await eve.post(f"/v1/dashboards/{dash_id}:validate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    pointers = {i["pointer"] for i in body["issues"]}
    assert {"/channels/1/name", "/kpi/variable"} <= pointers
    missing = dashboard_spec(source={"kind": "model_version", "model_id": "nope"})
    r = await eve.post("/v1/dashboards", json={"name": "nope", "spec": missing})
    r = await eve.post(f"/v1/dashboards/{r.json()['id']}:validate")
    assert r.json()["issues"][0]["code"] == "REF_UNRESOLVED"
    r = await eve.post("/v1/dashboards", json={"name": "good", "spec": dashboard_spec()})
    r = await eve.post(f"/v1/dashboards/{r.json()['id']}:validate")
    assert r.json() == {"ok": True, "issues": []}


async def test_versions_that_ran_uploaded_code_are_refused(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    async with ctx.db.session() as s:
        await s.execute(
            update(m.ModelVersion)
            .where(m.ModelVersion.id == kit["model"]["mv_id"])
            .values(lineage={"overlay_hash": "sha256:" + "ab" * 32})
        )
    ana: Caller = kit["ana"]
    r = await ana.post("/v1/dashboards", json={"name": "x", "spec": dashboard_spec()})
    r = await ana.post(f"/v1/dashboards/{r.json()['id']}:publish")
    assert r.status_code == 422, r.text
    assert "uploaded plugin code" in r.json()["detail"]


# =================================================================================== publish


async def test_publish_needs_runner_and_pins_the_version(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    eve: Caller = kit["eve"]
    r = await eve.post("/v1/dashboards", json={"name": "acme", "spec": dashboard_spec()})
    dash_id = r.json()["id"]
    r = await eve.post(f"/v1/dashboards/{dash_id}:publish")
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    r = await kit["ana"].post(f"/v1/dashboards/{dash_id}:publish")
    assert r.status_code == 200, r.text
    body = r.json()
    rev = body["published_revision"]
    assert body["status"] == "published"
    assert body["run_as"] == kit["users"]["ana"].id
    assert rev["seq"] == 1 and rev["model_version_id"] == kit["model"]["mv_id"]
    assert rev["model_ref"] == f"model://default/trend@v{kit['model']['version']}"
    assert rev["spec_hash"].startswith("sha256:")
    # the baseline readout is queued for the dashboard loop
    assert body["latest_readout"]["kind"] == "baseline"
    assert body["latest_readout"]["status"] == "queued"
    r = await kit["ana"].post(f"/v1/dashboards/{dash_id}:publish")
    assert r.json()["published_revision"]["seq"] == 2
    r = await kit["vic"].get(f"/v1/dashboards/{dash_id}/revisions")
    assert [x["seq"] for x in r.json()["items"]] == [2, 1]
    async with ctx.db.session() as s:
        rows = (
            await s.execute(
                select(m.DashboardReadout.seq, m.DashboardReadout.visibility).where(
                    m.DashboardReadout.dashboard_id == dash_id
                )
            )
        ).all()
        audit = (
            await s.execute(select(m.AuditEvent.action).where(m.AuditEvent.resource_id == dash_id))
        ).scalars()
        actions = sorted(audit)
    assert sorted(rows) == [(1, "published"), (2, "published")]
    assert actions == ["dashboard.create", "dashboard.publish", "dashboard.publish"]


async def test_archive_hides_the_dashboard_and_revokes_its_links(kit: dict[str, Any]) -> None:
    dash_id = await create_published(kit)
    ana: Caller = kit["ana"]
    r = await ana.post(f"/v1/dashboards/{dash_id}/shares", json={})
    assert r.status_code == 201, r.text
    assert (await kit["cleo"].get(f"/v1/dashboards/{dash_id}")).status_code == 200
    r = await ana.post(f"/v1/dashboards/{dash_id}:archive")
    assert r.status_code == 200 and r.json()["status"] == "archived"
    assert (await ana.post(f"/v1/dashboards/{dash_id}:archive")).status_code == 200
    assert (await kit["cleo"].get(f"/v1/dashboards/{dash_id}")).status_code == 404
    shares = (await ana.get(f"/v1/dashboards/{dash_id}/shares")).json()["items"]
    assert [s["status"] for s in shares] == ["revoked"]
    r = await ana.put(
        f"/v1/dashboards/{dash_id}/draft",
        json={"spec": dashboard_spec()},
        headers={"If-Match": '"1"'},
    )
    assert r.status_code == 409


# ==================================================================================== grants


async def test_grants_name_members_of_the_project(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash_id = await create_published(kit, grant=False)
    eve: Caller = kit["eve"]
    cleo = kit["users"]["cleo"]
    stranger = await make_user(ctx, "sam@elsewhere.test")
    r = await eve.put(f"/v1/dashboards/{dash_id}/grants/{stranger.id}")
    assert r.status_code == 404
    for _ in range(2):  # idempotent
        r = await eve.put(f"/v1/dashboards/{dash_id}/grants/{cleo.id}")
        assert r.status_code == 200, r.text
        assert r.json()["email"] == "cleo@default.test"
        assert r.json()["granted_by"] == kit["users"]["eve"].id
    r = await eve.get(f"/v1/dashboards/{dash_id}/grants")
    assert [g["user_id"] for g in r.json()["items"]] == [cleo.id]
    assert (await kit["vic"].get(f"/v1/dashboards/{dash_id}/grants")).status_code == 403
    for _ in range(2):
        r = await eve.delete(f"/v1/dashboards/{dash_id}/grants/{cleo.id}")
        assert r.status_code == 204
    assert (await eve.get(f"/v1/dashboards/{dash_id}/grants")).json()["items"] == []
    async with ctx.db.session() as s:
        actions = sorted(
            (
                await s.execute(
                    select(m.AuditEvent.action).where(
                        m.AuditEvent.resource_id == dash_id,
                        m.AuditEvent.action.in_(("dashboard.grant", "dashboard.ungrant")),
                    )
                )
            ).scalars()
        )
    assert actions == ["dashboard.grant", "dashboard.ungrant"]


async def test_grants_end_with_the_client_membership(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    """Removing a client (or giving them another role) deletes their grants, audited, so
    inviting the same user again as a client restores nothing by itself."""
    dash_id = await create_published(kit)
    other = await create_published(kit, name="acme-monthly")
    cleo = kit["users"]["cleo"]
    members = f"/v1/tenants/default/projects/default/members/{cleo.id}"
    assert len((await kit["cleo"].get("/v1/dashboards")).json()["items"]) == 2
    r = await kit["ana"].delete(members)
    assert r.status_code == 204, r.text
    await grant_role(ctx, cleo, "default", "default", "client")  # invited again
    again = await session_caller(ctx, client, cleo, tenant_id="default", project_id="default")
    assert (await again.get("/v1/dashboards")).json()["items"] == []
    assert (await again.get(f"/v1/dashboards/{dash_id}")).status_code == 404
    # client -> viewer -> client: the same
    r = await kit["ana"].put(f"/v1/dashboards/{dash_id}/grants/{cleo.id}")
    assert r.status_code == 200, r.text
    r = await kit["ana"].patch(members, json={"role": "viewer"})
    assert r.status_code == 200, r.text
    r = await kit["ana"].patch(members, json={"role": "client"})
    assert r.status_code == 200, r.text
    assert (await again.get("/v1/dashboards")).json()["items"] == []
    async with ctx.db.session() as s:
        ungrants = sorted(
            (e.resource_id, (e.details or {}).get("reason"))
            for e in (
                await s.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "dashboard.ungrant")
                )
            ).scalars()
        )
    assert ungrants == sorted(
        [(dash_id, "member_removed"), (other, "member_removed"), (dash_id, "role_changed")]
    )
