"""The read-only dashboard tool surface (v0.16, plan D18 and I-3).

``call_tool`` answers what the HTTP routes answer (the member, client and share read routes
call it), never writes a row or submits a run, and keeps private readouts with their actor.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards.access import DashboardContext, effective_inputs
from mapi_server.services.dashboards.inputs import clear_grid_cache
from mapi_server.services.dashboards.tools import call_tool
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_spec.dashboard_tools import DASHBOARD_TOOL_NAMES
from mapi_spec.dashboards import DashboardSpec
from mapi_spec.errors import MapiError
from sqlalchemy import func, select
from test_dashboards_crud import create_published, dashboard_kit

pytestmark = pytest.mark.integration

SNAP_A = {
    "lead": "A.",
    "tiles": [{"key": "expected", "label": "Expected orders", "value": 400.0, "unit": "count"}],
    "channels": [{"name": "tv", "label": "TV", "spend": 100.0, "roi": 0.04, "zone": "headroom"}],
}
SNAP_B = {
    "lead": "B.",
    "tiles": [{"key": "expected", "label": "Expected orders", "value": 430.0, "unit": "count"}],
    "channels": [{"name": "tv", "label": "TV", "spend": 120.0, "roi": 0.03, "zone": "optimal"}],
}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    clear_grid_cache()
    kit = await dashboard_kit(ctx, client)
    dash_id = await create_published(kit)
    kit["dash_id"] = dash_id
    async with ctx.db.session() as s:  # two ready published readouts: the baseline and one
        base = await s.get(m.DashboardReadout, (dash_id, 1))
        assert base is not None
        base.status, base.ready_at, base.snapshot = "ready", now(), SNAP_A
        s.add(
            m.DashboardReadout(
                tenant_id=base.tenant_id, project_id=base.project_id, dashboard_id=dash_id,
                seq=2, revision_id=base.revision_id, kind="refresh", trigger="manual",
                visibility="published", status="ready", title="Refreshed", actor_kind="system",
                model_version_id=base.model_version_id, model_ref=base.model_ref,
                spec_hash=base.spec_hash, run_ids=[], snapshot=SNAP_B, created_at=now(),
                ready_at=now(),
            )
        )  # fmt: skip
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        kit["dctx"] = DashboardContext(
            tenant_id=dash.tenant_id,
            project_id=dash.project_id,
            dashboard_id=dash_id,
            revision_id=rev.id,
            principal_kind="client",
            principal_id=kit["users"]["cleo"].id,
            inputs=effective_inputs(DashboardSpec.model_validate(rev.spec), None),
            is_analyst=False,
        )
    return kit


async def _counts(ctx: ServerContext) -> tuple[int, int, int]:
    async with ctx.db.session() as s:
        return (
            int(
                (await s.execute(select(func.count()).select_from(m.DashboardReadout))).scalar_one()
            ),
            int((await s.execute(select(func.count()).select_from(m.Run))).scalar_one()),
            int((await s.execute(select(func.count()).select_from(m.AuditEvent))).scalar_one()),
        )


async def test_tool_results_equal_the_route_results(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dctx: DashboardContext = kit["dctx"]
    base = f"/v1/dashboards/{kit['dash_id']}"
    cleo = kit["cleo"]
    pairs: list[tuple[str, dict[str, Any], Any]] = [
        ("get_dashboard", {}, cleo.get(base)),
        ("list_readouts", {"limit": 20}, cleo.get(f"{base}/readouts")),
        ("get_readout", {"seq": 2}, cleo.get(f"{base}/readouts/2")),
        (
            "evaluate_whatif",
            {"multipliers": {"tv": 0.8}},
            cleo.post(f"{base}/whatif:evaluate", json={"multipliers": {"tv": 0.8}}),
        ),
    ]
    for name, args, request in pairs:
        result = await call_tool(ctx, dctx, name, args)
        response = await request
        assert response.status_code == 200, response.text
        assert result.model_dump(mode="json") == response.json(), name


async def test_every_tool_is_read_only(kit: dict[str, Any], ctx: ServerContext) -> None:
    dctx: DashboardContext = kit["dctx"]
    calls: dict[str, dict[str, Any]] = {
        "get_dashboard": {},
        "get_latest_readout": {},
        "list_readouts": {"kind": "refresh"},
        "get_readout": {"seq": 1},
        "compare_readouts": {"a_seq": 1, "b_seq": 2},
        "get_channel": {"name": "tv", "last_n": 5},
        "explain_metric": {"metric": "return_on_media"},
        "evaluate_whatif": {"multipliers": {"search": 1.2}},
    }
    assert set(calls) == set(DASHBOARD_TOOL_NAMES)
    before = await _counts(ctx)
    for name, args in calls.items():
        await call_tool(ctx, dctx, name, args)
    assert await _counts(ctx) == before


async def test_compare_channel_latest_and_explain(kit: dict[str, Any], ctx: ServerContext) -> None:
    dctx: DashboardContext = kit["dctx"]
    cmp = (await call_tool(ctx, dctx, "compare_readouts", {"a_seq": 1, "b_seq": 2})).model_dump()
    assert cmp["same_model"] is True
    assert cmp["tiles"] == [
        {"key": "expected", "label": "Expected orders", "a": 400.0, "b": 430.0, "delta": 30.0}
    ]
    assert cmp["channels"][0]["roi_a"] == 0.04 and cmp["channels"][0]["zone_b"] == "optimal"
    assert cmp["lead"] == "Readout 2 expects 30 more orders than readout 1."
    hist = (await call_tool(ctx, dctx, "get_channel", {"name": "tv"})).model_dump()
    assert [p["seq"] for p in hist["points"]] == [1, 2] and hist["points"][1]["spend"] == 120.0
    latest = (await call_tool(ctx, dctx, "get_latest_readout", {})).model_dump()
    assert latest["seq"] == 2 and latest["snapshot"]["lead"] == "B."
    text = (await call_tool(ctx, dctx, "explain_metric", {"metric": "headroom"})).model_dump()
    assert text["text"].startswith("More room")
    with pytest.raises(MapiError) as bad:
        await call_tool(ctx, dctx, "get_channel", {"name": "radio"})
    assert bad.value.code.value == "NOT_FOUND"


async def test_tools_refuse_unknown_names_and_bad_arguments(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    for name, args in (("start_run", {}), ("get_readout", {"seq": 0}), ("list_readouts", {"x": 1})):
        with pytest.raises(MapiError) as err:
            await call_tool(ctx, kit["dctx"], name, args)
        assert err.value.code.value == "SCHEMA_INVALID"


async def test_private_readouts_are_seen_only_by_their_actor(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    r = await kit["cleo"].post(
        f"/v1/dashboards/{kit['dash_id']}/scenarios", json={"multipliers": {"tv": 0.8}}
    )
    seq = r.json()["seq"]
    mine: DashboardContext = kit["dctx"]
    other = DashboardContext(
        **{**_fields(mine), "principal_kind": "share", "principal_id": "dss_x"}
    )
    analyst = DashboardContext(**{**_fields(mine), "principal_kind": "member", "is_analyst": True,
                                  "principal_id": kit["users"]["vic"].id})  # fmt: skip
    assert (await call_tool(ctx, mine, "get_readout", {"seq": seq})).model_dump()["seq"] == seq
    assert (await call_tool(ctx, analyst, "get_readout", {"seq": seq})).model_dump()["seq"] == seq
    with pytest.raises(MapiError) as hidden:
        await call_tool(ctx, other, "get_readout", {"seq": seq})
    assert hidden.value.code.value == "NOT_FOUND"
    listed = (await call_tool(ctx, other, "list_readouts", {})).model_dump()["items"]
    assert seq not in [i["seq"] for i in listed]


def _fields(d: DashboardContext) -> dict[str, Any]:
    return {f: getattr(d, f) for f in DashboardContext.__slots__}
