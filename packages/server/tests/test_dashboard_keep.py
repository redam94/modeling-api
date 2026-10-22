"""Keeping a tried case (v0.17, plan §3.2.5 / Q3): ``POST …/readouts/{seq}:keep``.

A scenario-row readout is private and expires. Its owner (a client here) or an editor may
keep it: it stops expiring and waits for review (``pending_review``), so it never enters
the published history until an editor's ``…:publish``. The dashboards are the layout 2
dashboards of :mod:`test_dashboard_cases` (``cleo`` and ``carl`` are clients, ``vic`` a
viewer, ``eve`` an editor); the runs are queued, never executed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import sources
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import grant_role, make_user, session_caller
from mapi_spec.api import CLIENT_OPERATIONS, ROUTE_ACCESS
from mapi_spec.audit import AuditAction
from mapi_spec.tenancy import Action
from sqlalchemy import select, update
from test_dashboard_cases import HORIZON_ROWS, finish, publish_v2, readout_row
from test_dashboards_crud import dashboard_kit

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    sources.clear_catalog_cache()
    kit = await dashboard_kit(ctx, client)
    carl = await make_user(ctx, "carl@default.test")
    await grant_role(ctx, carl, "default", "default", "client")
    kit["users"]["carl"] = carl
    kit["carl"] = await session_caller(ctx, client, carl, tenant_id="default", project_id="default")
    return kit


async def tried_case(ctx: ServerContext, kit: dict[str, Any], who: str = "cleo") -> tuple[str, int]:
    dash_id = await publish_v2(ctx, kit, scenario_rows=HORIZON_ROWS)
    r = await kit[who].post(
        f"/v1/dashboards/{dash_id}/predictions", json={"rows": [{"horizon": 4}]}
    )
    assert r.status_code == 202, r.text
    seq = int(r.json()["seq"])
    await finish(ctx, dash_id, seq, {"lead": "A case."})
    return dash_id, seq


async def keeps(ctx: ServerContext) -> list[m.AuditEvent]:
    async with ctx.db.session() as s:
        return list(
            (
                await s.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == AuditAction.DASHBOARD_READOUT_KEEP.value
                    )
                )
            ).scalars()
        )


def test_the_contract_lets_clients_keep_their_cases() -> None:
    access = ROUTE_ACCESS["keep_dashboard_readout"]
    assert access.action is Action.RUN and access.name_param == "dashboard_id"
    assert "keep_dashboard_readout" in CLIENT_OPERATIONS


async def test_a_client_keeps_a_case_and_an_editor_publishes_it(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id, seq = await tried_case(ctx, kit)
    base = f"/v1/dashboards/{dash_id}"
    assert (await readout_row(ctx, dash_id, seq)).expires_at is not None
    # nobody else keeps it: another client, a viewer (they see it, they cannot keep it)
    for who in ("carl", "vic"):
        assert (await kit[who].post(f"{base}/readouts/{seq}:keep")).status_code == 404
    r = await kit["cleo"].post(f"{base}/readouts/{seq}:keep")
    assert r.status_code == 200, r.text
    assert r.json()["seq"] == seq and r.json()["visibility"] == "pending_review"
    row = await readout_row(ctx, dash_id, seq)
    assert row.visibility == "pending_review" and row.expires_at is None
    (event,) = await keeps(ctx)
    assert event.resource_id == dash_id and event.actor_id == kit["users"]["cleo"].id
    assert event.details == {"seq": seq, "from": "private", "to": "pending_review"}
    # idempotent (no second audit row)
    again = await kit["cleo"].post(f"{base}/readouts/{seq}:keep")
    assert again.status_code == 200 and again.json()["visibility"] == "pending_review"
    assert len(await keeps(ctx)) == 1
    # still the owner's only: not in anybody else's history until an editor publishes it
    assert (await kit["cleo"].get(f"{base}/readouts/{seq}")).status_code == 200
    assert (await kit["carl"].get(f"{base}/readouts/{seq}")).status_code == 404
    r = await kit["eve"].post(f"{base}/readouts/{seq}:publish")
    assert r.status_code == 200 and r.json()["visibility"] == "published"
    assert (await kit["carl"].get(f"{base}/readouts/{seq}")).status_code == 200
    # R6 (live): a published kept case is in the history, never the clients' Overview
    for who in ("carl", "cleo"):
        view = await kit[who].get(base)
        assert view.status_code == 200, view.text
        latest = view.json().get("latest_readout")
        assert latest is None or latest["seq"] != seq, latest
    # keeping a published case answers with it
    r = await kit["cleo"].post(f"{base}/readouts/{seq}:keep")
    assert r.status_code == 200 and r.json()["visibility"] == "published"


async def test_an_editor_may_keep_anybodys_case(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash_id, seq = await tried_case(ctx, kit)
    r = await kit["eve"].post(f"/v1/dashboards/{dash_id}/readouts/{seq}:keep")
    assert r.status_code == 200 and r.json()["visibility"] == "pending_review"
    (event,) = await keeps(ctx)
    assert event.actor_id == kit["users"]["eve"].id


async def test_only_a_live_tried_case_can_be_kept(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash_id, seq = await tried_case(ctx, kit)
    base = f"/v1/dashboards/{dash_id}"
    # the baseline readout of the publish is not a tried case
    async with ctx.db.session() as s:
        other = (
            (
                await s.execute(
                    select(m.DashboardReadout.seq).where(
                        m.DashboardReadout.dashboard_id == dash_id,
                        m.DashboardReadout.kind != "prediction",
                    )
                )
            )
            .scalars()
            .first()
        )
    assert other is not None
    r = await kit["eve"].post(f"{base}/readouts/{other}:keep")
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    # a failed case
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dash_id, m.DashboardReadout.seq == seq)
            .values(status="failed")
        )
    r = await kit["cleo"].post(f"{base}/readouts/{seq}:keep")
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    # a hidden case (an editor hid it after it was kept)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dash_id, m.DashboardReadout.seq == seq)
            .values(status="ready", visibility="hidden")
        )
    assert (await kit["cleo"].post(f"{base}/readouts/{seq}:keep")).status_code == 404
    r = await kit["eve"].post(f"{base}/readouts/{seq}:keep")
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    assert await keeps(ctx) == []
    # an unknown readout
    assert (await kit["cleo"].post(f"{base}/readouts/999:keep")).status_code == 404
