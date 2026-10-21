"""Dashboard readouts (v0.16, plan D10, D11, §3.5): the run-terminal hook, the dashboard loop
and the materialiser, visibility and the history.

Publishing queues a ``baseline`` readout; the loop submits it as the publisher, the fake
executor and the real finalizer finish the run with a ``summary.json``, the terminal
transition flips the readout to ``materialising`` - in a project with no webhooks - and the
loop builds the snapshot from the summary, the fit metrics and the grid.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards.inputs import clear_grid_cache
from mapi_server.services.dashboards.loop import DashboardLoop
from mapi_server.services.dashboards.readouts import ChannelFacts, build_snapshot, kpi_series
from mapi_server.services.runs import apply_transition, now
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.plots import complete_run
from mapi_spec.api import ScoreSummary
from mapi_spec.dashboards import DashboardSpec, ReadoutSnapshot
from mapi_spec.run import Actor, RunTrigger
from pydantic import ValidationError
from sqlalchemy import func, select
from test_dashboards_crud import create_published, dashboard_kit, dashboard_spec

pytestmark = pytest.mark.integration

MONTHS = [f"2024-{k:02d}-01" for k in range(1, 7)]
SUMMARY = {
    "tables": [
        {
            "variable": "y",
            "columns": ["t", "mean", "hdi_90_lower", "hdi_90_upper"],
            "dims": ["t"],
            "time_dim": "t",
            "rows": [[t, 400.0 + k, 380.0 + k, 420.0 + k] for k, t in enumerate(MONTHS)],
        }
    ]
}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    clear_grid_cache()
    kit = await dashboard_kit(ctx, client)
    kit["dash_id"] = await create_published(kit)
    return kit


async def _readout(ctx: ServerContext, dash_id: str, seq: int) -> m.DashboardReadout:
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (dash_id, seq))
        assert row is not None
        s.expunge(row)
        return row


async def _fit_metrics(ctx: ServerContext, mv_id: str) -> None:
    """The fit run logged per-channel metrics (as the MMM plugins do)."""
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, mv_id)
        assert mv is not None
        ts = now()
        for ch, roi in (("tv", 0.04), ("search", 0.01)):
            for key, value in (("roi", roi), ("mroi", roi)):
                s.add(
                    m.RunMetricSummary(
                        tenant_id=mv.tenant_id, project_id=mv.project_id, run_id=mv.run_id,
                        key=key, labels_key=f"channel={ch}", labels={"channel": ch},
                        attempt=1, count=1, first_ts=ts, last_ts=ts, last=value,
                        last_step=0, min=value, max=value,
                    )
                )  # fmt: skip


async def test_the_baseline_runs_finishes_and_materialises_without_webhooks(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = kit["dash_id"]
    await _fit_metrics(ctx, kit["model"]["mv_id"])
    queued = await _readout(ctx, dash_id, 1)
    assert (queued.kind, queued.status, queued.trigger) == ("baseline", "queued", "publish")
    loop = DashboardLoop(ctx)
    assert await loop.submit_queued() == 1
    running = await _readout(ctx, dash_id, 1)
    assert running.status == "running" and len(running.run_ids) == 1
    async with ctx.db.session() as s:
        run = await s.get(m.Run, running.run_ids[0])
        assert run is not None and run.kind == "score"
        assert run.created_by == kit["users"]["ana"].id
        assert run.payload["request"]["mode"] == "predict"
        assert (await s.execute(select(func.count()).select_from(m.Webhook))).scalar_one() == 0
    await complete_run(
        ctx,
        running.run_ids[0],
        add={"summary.json": (json.dumps(SUMMARY).encode(), "application/json")},
    )
    assert (await _readout(ctx, dash_id, 1)).status == "materialising"
    assert await loop.materialise() == 1
    ready = await _readout(ctx, dash_id, 1)
    assert ready.status == "ready" and ready.ready_at is not None
    snap = ReadoutSnapshot.model_validate(ready.snapshot)
    assert snap.lead == "The model expects 405 orders this week."
    tiles = {t.key: t for t in snap.tiles}
    assert tiles["expected"].value == 405.0 and tiles["expected"].low == 385.0
    assert "return_on_media" in tiles and "in_range_spend" in tiles
    tv = next(c for c in snap.channels if c.name == "tv")
    # spend from the grid (52 000), the return from the fit metrics; 1/40 is the breakeven
    assert tv.spend == 52000.0 and tv.roi == 0.04 and tv.zone == "headroom"
    assert snap.series[0].points[-1].t == "2024-06-01" and snap.source_run_ids == ready.run_ids
    assert ready.period_end is not None and ready.period_end.isoformat() == "2024-06-01"
    # clients see it as the latest readout
    body = (await kit["cleo"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["latest_readout"]["seq"] == 1 and body["latest_readout"]["snapshot"]["lead"]


async def test_a_failed_run_fails_its_readout_only_analysts_see(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = kit["dash_id"]
    await DashboardLoop(ctx).submit_queued()
    row = await _readout(ctx, dash_id, 1)
    async with ctx.db.session() as s:
        run = await s.get(m.Run, row.run_ids[0], with_for_update=True)
        assert run is not None
        await apply_transition(s, run, RunTrigger.CANCEL, Actor.API)
        await apply_transition(s, run, RunTrigger.CANCEL_CONFIRMED, Actor.RECONCILER)
    failed = await _readout(ctx, dash_id, 1)
    assert failed.status == "failed" and failed.problem is not None
    base = f"/v1/dashboards/{dash_id}/readouts"
    assert (await kit["vic"].get(f"{base}/1")).json()["status"] == "failed"
    assert (await kit["cleo"].get(f"{base}/1")).status_code == 404
    assert (await kit["cleo"].get(base)).json()["items"] == []


async def test_a_paused_owner_fails_the_queued_baseline(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    from sqlalchemy import update

    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == kit["users"]["ana"].id)
            .values(role="viewer")
        )
    await DashboardLoop(ctx).submit_queued()
    row = await _readout(ctx, kit["dash_id"], 1)
    assert row.status == "failed" and row.problem is not None
    assert row.problem["code"] == "DASHBOARD_LIVE_PAUSED" and row.run_ids == []


async def test_sequence_numbers_stay_unique_under_concurrency(kit: dict[str, Any]) -> None:
    base = f"/v1/dashboards/{kit['dash_id']}/scenarios"
    plans = [{"multipliers": {"tv": 0.6 + k / 10}} for k in range(6)]
    answers = await asyncio.gather(*(kit["cleo"].post(base, json=p) for p in plans))
    seqs = sorted(r.json()["seq"] for r in answers)
    assert seqs == list(range(2, 8))  # 1 is the baseline


async def test_readouts_page_back_newest_first(kit: dict[str, Any]) -> None:
    base = f"/v1/dashboards/{kit['dash_id']}"
    for k in range(3):
        await kit["cleo"].post(f"{base}/scenarios", json={"multipliers": {"tv": 0.7 + k / 10}})
    page = (await kit["cleo"].get(f"{base}/readouts", params={"limit": 2})).json()
    assert [i["seq"] for i in page["items"]] == [4, 3] and page["next_before_seq"] == 3
    more = (
        await kit["cleo"].get(f"{base}/readouts", params={"before_seq": 3, "kind": "scenario"})
    ).json()
    assert [i["seq"] for i in more["items"]] == [2] and more["next_before_seq"] is None
    # analysts can filter by visibility; the queued baseline is theirs to see
    queued = (await kit["vic"].get(f"{base}/readouts", params={"visibility": "published"})).json()
    assert [i["seq"] for i in queued["items"]] == [1]


async def test_private_readouts_stay_with_their_share_link(
    kit: dict[str, Any], client: httpx.AsyncClient
) -> None:
    from mapi_spec.dashboards import SHARE_SESSION_HEADER

    sessions = []
    for label in ("a", "b"):
        r = await kit["ana"].post(
            f"/v1/dashboards/{kit['dash_id']}/shares",
            json={"label": label, "allowed_inputs": ["whatif"]},
        )
        token = r.json()["token"]
        s = await client.post("/v1/share-sessions", json={"token": token})
        sessions.append({SHARE_SESSION_HEADER: s.json()["session"]})
    a, b = sessions
    r = await client.post("/v1/shared/scenarios", json={"multipliers": {"tv": 0.8}}, headers=a)
    assert r.status_code == 200, r.text
    seq = r.json()["seq"]
    assert (await client.get(f"/v1/shared/readouts/{seq}", headers=a)).status_code == 200
    assert (await client.get(f"/v1/shared/readouts/{seq}", headers=b)).status_code == 404
    listed = (await client.get("/v1/shared/readouts", headers=b)).json()["items"]
    assert seq not in [i["seq"] for i in listed]
    # nor do members other than the analysts see it
    assert (
        await kit["cleo"].get(f"/v1/dashboards/{kit['dash_id']}/readouts/{seq}")
    ).status_code == 404
    assert (
        await kit["vic"].get(f"/v1/dashboards/{kit['dash_id']}/readouts/{seq}")
    ).status_code == 200


async def test_review_publishes_and_hides(kit: dict[str, Any], ctx: ServerContext) -> None:
    dash_id = kit["dash_id"]
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (dash_id, 1))
        assert row is not None
        row.status, row.visibility, row.ready_at = "ready", "pending_review", now()
        row.snapshot = {"lead": "Pending."}
    base = f"/v1/dashboards/{dash_id}/readouts/1"
    assert (await kit["cleo"].get(base)).status_code == 404
    assert (await kit["vic"].post(f"{base}:publish")).status_code == 403  # editors review
    r = await kit["eve"].post(f"{base}:publish")
    assert r.status_code == 200 and r.json()["visibility"] == "published"
    assert (await kit["cleo"].get(base)).json()["snapshot"]["lead"] == "Pending."
    r = await kit["eve"].post(f"{base}:hide")
    assert r.status_code == 200 and r.json()["visibility"] == "hidden"
    assert (await kit["cleo"].get(base)).status_code == 404
    async with ctx.db.session() as s:
        actions = (
            await s.execute(
                select(m.AuditEvent.action).where(m.AuditEvent.action == "dashboard.readout_review")
            )
        ).scalars()
        assert len(list(actions)) == 2


async def test_private_readouts_expire(kit: dict[str, Any], ctx: ServerContext) -> None:
    from datetime import timedelta

    r = await kit["cleo"].post(
        f"/v1/dashboards/{kit['dash_id']}/scenarios", json={"multipliers": {"tv": 0.8}}
    )
    seq = r.json()["seq"]
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (kit["dash_id"], seq))
        assert row is not None and row.expires_at is not None
        lived = row.expires_at.timestamp() - row.created_at.timestamp()
        assert abs(lived - timedelta(days=30).total_seconds()) < 60
        row.expires_at = now() - timedelta(seconds=1)
    assert await DashboardLoop(ctx).prune() == 1
    async with ctx.db.session() as s:
        assert await s.get(m.DashboardReadout, (kit["dash_id"], seq)) is None


# ============================================================================== pure builders


def _spec(**over: Any) -> DashboardSpec:
    return DashboardSpec.model_validate(dashboard_spec(**over))


def test_the_snapshot_compares_actuals_with_the_expectation() -> None:
    summary = ScoreSummary.model_validate(SUMMARY)
    points = kpi_series(summary, "y", {"2024-05-01": 420.0, "2024-06-01": 425.25})
    spec = _spec(tiles=["expected", "vs_expected", "accuracy", "index"])
    snap = build_snapshot(spec, kind="actuals", points=points, facts={})
    assert (
        snap.lead == "The result was 425 orders this week. This is 5% more than the model expects."
    )
    tiles = {t.key: t for t in snap.tiles}
    assert tiles["vs_expected"].delta == pytest.approx(0.05)
    assert tiles["vs_expected"].delta_dir == "alert"  # above the 90 % range (385 to 425)
    assert tiles["index"].value == pytest.approx(105.0)
    assert snap.accuracy is not None and snap.accuracy.n == 2
    assert [t.key for t in snap.tiles] == ["expected", "vs_expected", "accuracy", "index"]


def test_long_series_are_thinned_and_oversized_snapshots_refused() -> None:
    rows = [[f"t{k:05d}", float(k), None, None] for k in range(3000)]
    summary = ScoreSummary.model_validate(
        {"tables": [{"variable": "y", "columns": ["t", "mean", "q5", "q95"], "dims": ["t"],
                     "rows": rows}]}
    )  # fmt: skip
    snap = build_snapshot(_spec(), kind="refresh", points=kpi_series(summary, "y"), facts={})
    assert len(snap.series[0].points) == 520
    assert snap.series[0].points[0].t == "t00000" and snap.series[0].points[-1].t == "t02999"
    huge = {f"k{i}": "x" * 5000 for i in range(60)}
    with pytest.raises(ValidationError, match="max 262144"):
        build_snapshot(_spec(), kind="refresh", points=[], facts={}, technical=huge)


def test_channel_zones_use_the_breakeven_of_the_kpi() -> None:
    spec = _spec()  # orders worth 40 each: breakeven return 0.025 orders per unit of spend
    facts = {
        "tv": ChannelFacts(spend=100.0, contribution=5.0, roi=0.05, mroi=0.02),
        "search": ChannelFacts(spend=100.0, contribution=1.0, roi=0.01, mroi=0.005),
    }
    snap = build_snapshot(spec, kind="refresh", points=[], facts=facts)
    zones = {c.name: c.zone for c in snap.channels}
    assert zones == {"tv": "over", "search": "no_payback"}
    tiles = {t.key: t for t in snap.tiles}
    assert tiles["in_range_spend"].value == 0.0
    assert tiles["return_on_media"].value == pytest.approx(0.03)
    assert snap.lead == "This readout has no result for the latest week."
