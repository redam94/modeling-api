"""The read-only dashboard tool surface (v0.16, plan D18 and I-3; v0.17, plan I-5 and D19).

``call_tool`` answers what the HTTP routes answer (the member, client and share read routes
call it), never writes a row or submits a run, and keeps private readouts with their actor.
The layout 2 dashboards are the v0.16 ones moved by the automatic upgrade (the ``forecast``
preset, or the media planner when the catalog says ``mmm``), with hand-written snapshots.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import sources
from mapi_server.services.dashboards.access import (
    DashboardContext,
    actuals_mode,
    effective_inputs,
)
from mapi_server.services.dashboards.inputs import clear_grid_cache
from mapi_server.services.dashboards.readouts import SYSTEM, Actor, new_readout
from mapi_server.services.dashboards.tools import call_tool
from mapi_server.services.dashboards.upgrade import auto_upgrade_v1
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_spec.dashboard_tools import DASHBOARD_TOOL_NAMES, V016_TOOL_NAMES
from mapi_spec.dashboard_v2 import DashboardSpecV2, SourceCatalog, parse_dashboard_spec
from mapi_spec.dashboards import DashboardSpec, ReadoutSnapshot, dashboard_spec_hash
from mapi_spec.errors import MapiError
from sqlalchemy import func, select
from test_dashboards_crud import LAYOUT1, create_published, dashboard_kit

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
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT, **LAYOUT1})


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
    assert set(calls) == set(V016_TOOL_NAMES)
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


# ================================================================================== layout 2


def _kpi(pid: str, title: str, value: float) -> dict[str, Any]:
    tile = {"key": "panel", "label": title, "value": value, "unit": "count", "panel_id": pid}
    return {"kind": "kpi", "panel_id": pid, "title": title, "tile": tile}


def _v2_snap(lead: str, kpis: list[tuple[str, str, float]], **extra: Any) -> ReadoutSnapshot:
    blocks = [_kpi(*k) for k in kpis]
    return ReadoutSnapshot.model_validate(
        {
            "lead": lead,
            "schema_version": 2,
            "preset": "generic",
            "panels": [*blocks, {"kind": "note", "panel_id": "about", "title": "About",
                                 "text": "Plain words."}],
            "tiles": [b["tile"] for b in blocks],
            **extra,
        }
    )  # fmt: skip


DETAIL = {"kind": "error", "n": 6, "mae": 2.0, "rmse": 2.5, "mape": 4.0, "band_hit_rate": 83.0}
HISTORY = [{"seq": 3, "created_at": "2024-07-01T00:00:00Z", "n": 6, "values": {"mape": 4.0}}]
PREDICTION = {
    "rows": [{"inputs": {"horizon": 3}, "outputs": {"y": {"centre": 410.0, "low": 350.0,
                                                          "high": 470.0}}}],
    "band": "outcome",
}  # fmt: skip
BATCH = {
    "n_rows": 3, "n_scored": 3, "outputs": ["y"],
    "stats": {"y": {"mean": 2.0, "min": 1.0, "p50": 2.0, "max": 3.0}},
    "preview": [{"case": "a", "y": 1.0}], "download": {"available": True, "rows": 3,
                                                     "bytes_estimate": 64},
}  # fmt: skip


async def _upgraded(
    ctx: ServerContext,
    kit: dict[str, Any],
    edit: Callable[[dict[str, Any]], dict[str, Any]] = dict,
    catalog_of: Any = None,
) -> DashboardSpecV2:
    """Upgrade ``kit``'s v1 dashboard to layout 2, then ``edit`` the published revision's
    spec (validated again; the tools read what is stored)."""
    report = await auto_upgrade_v1(ctx, catalog_of=catalog_of)
    assert report.published == [kit["dash_id"]]
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, kit["dash_id"])
        assert dash is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        spec = DashboardSpecV2.model_validate(edit(dict(rev.spec)))
        rev.spec, rev.spec_hash = spec.model_dump(mode="json"), dashboard_spec_hash(spec)
    return spec


async def _context(
    ctx: ServerContext, dash_id: str, kind: str, principal_id: str, *, analyst: bool = False
) -> DashboardContext:
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        spec = parse_dashboard_spec(rev.spec)
        mode = await actuals_mode(s, ctx, rev.model_version_id) if spec.actuals.enabled else None
        return DashboardContext(
            tenant_id=dash.tenant_id, project_id=dash.project_id, dashboard_id=dash_id,
            revision_id=rev.id, principal_kind=kind,  # type: ignore[arg-type]
            principal_id=principal_id,
            inputs=effective_inputs(spec, mode, client=kind == "client"), is_analyst=analyst,
        )  # fmt: skip


async def _add(
    ctx: ServerContext, dash_id: str, kind: str, snap: ReadoutSnapshot | None, *,
    visibility: str = "published", actor: Actor = SYSTEM, status: str = "ready",
) -> int:  # fmt: skip
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        trigger = {"prediction": "scenario_rows", "batch": "batch", "actuals": "actuals"}
        row = await new_readout(
            s, dash, rev, kind=kind, trigger=trigger.get(kind, "manual"),  # type: ignore[arg-type]
            visibility=visibility, actor=actor, title=f"{kind} readout",  # type: ignore[arg-type]
            status=status, snapshot=snap,  # type: ignore[arg-type]
        )  # fmt: skip
        return row.seq


@pytest.fixture
async def kit2(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    """A layout 2 dashboard (the ``generic`` preset with scenario rows and batch scoring on)
    with two published refreshes, a published actuals readout, cleo's private prediction and
    batch, a share link's private prediction and a readout still running."""
    clear_grid_cache()
    sources.clear_catalog_cache()
    kit = await dashboard_kit(ctx, client)
    kit["dash_id"] = dash_id = await create_published(kit)
    cleo = kit["users"]["cleo"].id

    def edit(d: dict[str, Any]) -> dict[str, Any]:
        out = [d["subject"]["variable"]]
        return {
            **d, "preset": "generic",
            "scenario_rows": {"enabled": True, "outputs": out,
                              "horizon": {"dim": "t", "max": 12, "default": 3}},
            "batch": {"enabled": True, "outputs": out, "id_column": "case"},
        }  # fmt: skip

    kit["spec"] = await _upgraded(ctx, kit, edit)
    a = _v2_snap("A.", [("next", "Orders next month", 400.0), ("gone", "Old tile", 1.0)])
    b = _v2_snap("B.", [("next", "Orders next month", 430.0), ("new", "New tile", 2.0)])
    actuals = _v2_snap("C.", [("next", "Orders next month", 430.0)],
                       accuracy_detail=DETAIL, accuracy_history=HISTORY)  # fmt: skip
    seqs = kit["seqs"] = {
        "a": await _add(ctx, dash_id, "refresh", a),
        "b": await _add(ctx, dash_id, "refresh", b),
        "actuals": await _add(ctx, dash_id, "actuals", actuals),
        "mine": await _add(
            ctx, dash_id, "prediction", _v2_snap("P.", [], prediction=PREDICTION),
            visibility="private", actor=Actor("client", cleo),
        ),
        "batch": await _add(
            ctx, dash_id, "batch", _v2_snap("Q.", [], batch=BATCH),
            visibility="private", actor=Actor("client", cleo),
        ),
        "theirs": await _add(
            ctx, dash_id, "prediction", _v2_snap("S.", [], prediction=PREDICTION),
            visibility="private", actor=Actor("share", "dss_other"),
        ),
        "running": await _add(ctx, dash_id, "refresh", None, status="running"),
    }  # fmt: skip
    assert len(set(seqs.values())) == len(seqs)
    kit["dctx"] = await _context(ctx, dash_id, "client", cleo)
    return kit


async def test_layout_2_tools_equal_the_route_results(
    kit2: dict[str, Any], ctx: ServerContext
) -> None:
    dctx: DashboardContext = kit2["dctx"]
    seqs = kit2["seqs"]
    base = f"/v1/dashboards/{kit2['dash_id']}"
    cleo = kit2["cleo"]
    for name, args, path in (
        ("get_dashboard", {}, base),
        ("get_latest_readout", {}, f"{base}/readouts/{seqs['actuals']}"),
        ("list_readouts", {"limit": 20}, f"{base}/readouts"),
        ("get_readout", {"seq": seqs["mine"]}, f"{base}/readouts/{seqs['mine']}"),
    ):
        response = await cleo.get(path)
        assert response.status_code == 200, response.text
        result = await call_tool(ctx, dctx, name, args)
        assert result.model_dump(mode="json") == response.json(), name
    view = (await cleo.get(base)).json()
    assert view["layout_version"] == 2 and view["scenario_form"] and view["batch_form"]
    panels = (await call_tool(ctx, dctx, "list_panels", {})).model_dump(mode="json")
    assert panels["panels"] == view["panels_meta"]
    assert [p["id"] for p in panels["panels"]] == [p.id for p in kit2["spec"].panels]
    inputs = (await call_tool(ctx, dctx, "list_inputs", {})).model_dump(mode="json")
    assert inputs["inputs"] == view["inputs"]
    assert inputs["scenario_form"] == view["scenario_form"]
    assert inputs["batch_form"] == view["batch_form"]
    assert inputs["exact_runs_per_day"] == kit2["spec"].limits.exact_runs_per_day
    assert inputs["batch_runs_per_day"] == kit2["spec"].limits.batch_runs_per_day

    async def snap(seq: int) -> dict[str, Any]:
        r = await cleo.get(f"{base}/readouts/{seq}")
        assert r.status_code == 200, r.text
        snapshot: dict[str, Any] = r.json()["snapshot"]
        return snapshot

    got = await call_tool(ctx, dctx, "get_panel", {"panel_id": "next", "seq": seqs["a"]})
    assert got.model_dump(mode="json") == {
        "seq": seqs["a"],
        "panel": (await snap(seqs["a"]))["panels"][0],
    }
    acc = (await call_tool(ctx, dctx, "get_accuracy", {"seq": seqs["actuals"]})).model_dump(
        mode="json"
    )
    s_acc = await snap(seqs["actuals"])
    assert acc["detail"] == s_acc["accuracy_detail"] and acc["detail"]["mape"] == 4.0
    assert acc["history"] == s_acc["accuracy_history"] and acc["seq"] == seqs["actuals"]
    pred = (await call_tool(ctx, dctx, "get_prediction", {"seq": seqs["mine"]})).model_dump(
        mode="json"
    )
    assert pred["prediction"] == (await snap(seqs["mine"]))["prediction"]
    assert pred["readout"]["seq"] == seqs["mine"] and pred["readout"]["visibility"] == "private"
    batch = (await call_tool(ctx, dctx, "get_batch_summary", {"seq": seqs["batch"]})).model_dump(
        mode="json"
    )
    assert batch["summary"] == (await snap(seqs["batch"]))["batch"]
    # Random identifiers (e.g. ``run_id``) can spell "csv" or "http" by chance; leave them out.
    scrubbed = {
        **batch,
        "readout": {k: v for k, v in batch["readout"].items() if not k.endswith("id")},
    }
    assert "csv" not in str(scrubbed).lower() and "http" not in str(scrubbed)


async def test_compare_readouts_gives_panel_deltas(
    kit2: dict[str, Any], ctx: ServerContext
) -> None:
    seqs = kit2["seqs"]
    cmp = (
        await call_tool(
            ctx, kit2["dctx"], "compare_readouts", {"a_seq": seqs["a"], "b_seq": seqs["b"]}
        )
    ).model_dump()
    assert cmp["panel_deltas"] == [
        {"panel_id": "next", "label": "Orders next month", "a": 400.0, "b": 430.0, "delta": 30.0},
        {"panel_id": "new", "label": "New tile", "a": None, "b": 2.0, "delta": None},
        {"panel_id": "gone", "label": "Old tile", "a": 1.0, "b": None, "delta": None},
    ]
    assert cmp["tiles"] == [] and cmp["channels"] == []  # panel tiles are compared as panels
    assert (
        cmp["lead"]
        == f"Orders next month went up by 30 from readout {seqs['a']} to readout {seqs['b']}."
    )
    same = (
        await call_tool(ctx, kit2["dctx"], "compare_readouts",
                        {"a_seq": seqs["b"], "b_seq": seqs["actuals"]})
    ).model_dump()  # fmt: skip
    assert same["lead"].startswith("Orders next month is the same in readouts")


async def test_media_tools_refuse_dashboards_without_the_planner(
    kit2: dict[str, Any], ctx: ServerContext
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = [
        ("get_channel", {"name": "tv"}),
        ("evaluate_whatif", {"multipliers": {"tv": 0.8}}),
    ]
    for name, args in calls:
        with pytest.raises(MapiError) as err:
            await call_tool(ctx, kit2["dctx"], name, args)
        assert err.value.code.value == "DASHBOARD_INPUT_DISABLED", name


async def test_panels_and_accuracy_default_to_the_latest_published_readout(
    kit2: dict[str, Any], ctx: ServerContext
) -> None:
    dctx: DashboardContext = kit2["dctx"]
    seqs = kit2["seqs"]
    latest = (await call_tool(ctx, dctx, "get_panel", {"panel_id": "next"})).model_dump()
    assert latest["seq"] == seqs["actuals"] and latest["panel"]["tile"]["value"] == 430.0
    acc = (await call_tool(ctx, dctx, "get_accuracy", {})).model_dump()
    assert acc["seq"] == seqs["actuals"] and acc["detail"]["kind"] == "error"
    none = (await call_tool(ctx, dctx, "get_accuracy", {"seq": seqs["a"]})).model_dump()
    assert none["detail"] is None and none["history"] == []
    for args, code in (
        ({"panel_id": "nope"}, "NOT_FOUND"),
        ({"panel_id": "next", "seq": seqs["running"]}, "NOT_READY"),
        ({"panel_id": "next", "seq": seqs["theirs"]}, "NOT_FOUND"),  # not visible to cleo
    ):
        with pytest.raises(MapiError) as err:
            await call_tool(ctx, dctx, "get_panel", args)
        assert err.value.code.value == code, args


async def test_private_predictions_and_batches_stay_with_their_actor(
    kit2: dict[str, Any], ctx: ServerContext
) -> None:
    seqs = kit2["seqs"]
    dash_id = kit2["dash_id"]
    cleo: DashboardContext = kit2["dctx"]
    share = await _context(ctx, dash_id, "share", "dss_other")
    stranger = await _context(ctx, dash_id, "share", "dss_x")
    analyst = await _context(ctx, dash_id, "member", kit2["users"]["vic"].id, analyst=True)
    assert (await call_tool(ctx, share, "get_prediction", {"seq": seqs["theirs"]})).model_dump()[
        "prediction"
    ]["band"] == "outcome"
    denied = [
        (stranger, "get_prediction", seqs["mine"]),
        (stranger, "get_prediction", seqs["theirs"]),
        (stranger, "get_batch_summary", seqs["batch"]),
        (share, "get_prediction", seqs["mine"]),
        (cleo, "get_prediction", seqs["theirs"]),
        (analyst, "get_prediction", seqs["mine"]),  # analysts read it with get_readout
        (analyst, "get_batch_summary", seqs["batch"]),
        (cleo, "get_prediction", seqs["batch"]),  # the wrong kind
        (cleo, "get_batch_summary", seqs["mine"]),
        (cleo, "get_prediction", seqs["a"]),
    ]
    for who, name, seq in denied:
        with pytest.raises(MapiError) as err:
            await call_tool(ctx, who, name, {"seq": seq})
        assert err.value.code.value == "NOT_FOUND", (who.principal_id, name, seq)
    listed = (await call_tool(ctx, stranger, "list_readouts", {})).model_dump()["items"]
    assert not {seqs["mine"], seqs["batch"], seqs["theirs"]} & {i["seq"] for i in listed}
    analyst_view = await call_tool(ctx, analyst, "get_readout", {"seq": seqs["mine"]})
    assert analyst_view.model_dump()["seq"] == seqs["mine"]


async def test_every_layout_2_tool_is_read_only(kit2: dict[str, Any], ctx: ServerContext) -> None:
    dctx: DashboardContext = kit2["dctx"]
    seqs = kit2["seqs"]
    calls: dict[str, dict[str, Any]] = {
        "get_dashboard": {},
        "get_latest_readout": {},
        "list_readouts": {},
        "get_readout": {"seq": seqs["a"]},
        "compare_readouts": {"a_seq": seqs["a"], "b_seq": seqs["b"]},
        "explain_metric": {"metric": "prediction"},
        "list_panels": {},
        "get_panel": {"panel_id": "next"},
        "get_accuracy": {},
        "list_inputs": {},
        "get_prediction": {"seq": seqs["mine"]},
        "get_batch_summary": {"seq": seqs["batch"]},
    }
    media = {"get_channel", "evaluate_whatif"}  # refused here (no media planner)
    assert set(calls) | media == set(DASHBOARD_TOOL_NAMES)
    before = await _counts(ctx)
    for name, args in calls.items():
        await call_tool(ctx, dctx, name, args)
    assert await _counts(ctx) == before


async def test_layout_1_dashboards_have_no_panels(kit: dict[str, Any], ctx: ServerContext) -> None:
    dctx: DashboardContext = kit["dctx"]
    assert (await call_tool(ctx, dctx, "list_panels", {})).model_dump() == {"panels": []}
    with pytest.raises(MapiError) as err:
        await call_tool(ctx, dctx, "get_panel", {"panel_id": "next"})
    assert err.value.code.value == "NOT_FOUND"
    inputs = (await call_tool(ctx, dctx, "list_inputs", {})).model_dump()
    assert inputs["scenario_form"] is None and inputs["batch_runs_per_day"] is None
    assert inputs["exact_runs_per_day"] == 20
    assert (await call_tool(ctx, dctx, "get_accuracy", {})).model_dump()["seq"] is None
    async with ctx.db.session() as s:  # a v0.16 actuals readout reads as error metrics
        row = await s.get(m.DashboardReadout, (kit["dash_id"], 2))
        assert row is not None
        row.snapshot = {**SNAP_B, "accuracy": {"mape": 5.0, "bias": 1.0, "n": 4}}
    acc = (await call_tool(ctx, dctx, "get_accuracy", {"seq": 2})).model_dump()
    assert acc["detail"]["kind"] == "error" and acc["detail"]["mape"] == 5.0
    assert acc["detail"]["n"] == 4 and acc["history"] == []


async def _as_mmm(c: ServerContext, s: Any, mv: m.ModelVersion) -> SourceCatalog:
    cat = await sources.source_catalog(c, s, mv)
    modes = sorted({*cat.score_modes, "counterfactual"})
    return cat.model_copy(update={"family": "mmm", "namespace": "mmm", "score_modes": modes})


async def test_an_upgraded_planner_keeps_its_channel_tools(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    clear_grid_cache()
    sources.clear_catalog_cache()
    kit = await dashboard_kit(ctx, client)
    kit["dash_id"] = dash_id = await create_published(kit, whatif={"enabled": True})
    spec = await _upgraded(ctx, kit, catalog_of=_as_mmm)
    assert spec.preset == "mmm_planner"
    a = ReadoutSnapshot.model_validate({**SNAP_A, "schema_version": 2, "preset": "mmm_planner"})
    b = ReadoutSnapshot.model_validate({**SNAP_B, "schema_version": 2, "preset": "mmm_planner"})
    sa = await _add(ctx, dash_id, "refresh", a)
    sb = await _add(ctx, dash_id, "refresh", b)
    dctx = await _context(ctx, dash_id, "client", kit["users"]["cleo"].id)
    cmp = (await call_tool(ctx, dctx, "compare_readouts", {"a_seq": sa, "b_seq": sb})).model_dump()
    assert cmp["tiles"][0]["delta"] == 30.0 and cmp["channels"][0]["zone_b"] == "optimal"
    assert cmp["lead"] == f"Readout {sb} expects 30 more orders than readout {sa}."
    assert cmp["panel_deltas"] == []
    hist = (await call_tool(ctx, dctx, "get_channel", {"name": "tv"})).model_dump()
    assert [p["seq"] for p in hist["points"]] == [sa, sb]
