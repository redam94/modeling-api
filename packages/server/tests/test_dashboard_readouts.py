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
from mapi_server.services.dashboards.readouts import (
    ChannelFacts,
    build_snapshot,
    grid_facts,
    kpi_series,
    merge_facts,
    whatif_result,
)
from mapi_server.services.runs import apply_transition, now
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.plots import complete_run
from mapi_spec.api import ScoreSummary
from mapi_spec.dashboards import DashboardSpec, ReadoutSnapshot
from mapi_spec.run import Actor, RunTrigger
from mapi_spec.views import ResponseGridView
from mapi_spec.whatif import WhatIfScenario, evaluate_scenario
from pydantic import ValidationError
from sqlalchemy import func, select, update
from test_dashboards_crud import LAYOUT1, create_published, dashboard_kit, dashboard_spec

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
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT, **LAYOUT1})


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
    # spend from the grid per week (52 000 over 52 weeks), the return from the fit metrics;
    # 1/40 is the breakeven
    assert tv.spend == 1000.0 and tv.roi == 0.04 and tv.zone == "headroom"
    assert tv.contribution == pytest.approx(975.0 / 52)  # the grid's mean total, per week
    assert snap.series[0].points[-1].t == "2024-06-01" and snap.source_run_ids == ready.run_ids
    assert ready.period_end is not None and ready.period_end.isoformat() == "2024-06-01"
    # clients see it as the latest readout
    body = (await kit["cleo"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["latest_readout"]["seq"] == 1 and body["latest_readout"]["snapshot"]["lead"]


async def _baseline_done(kit: dict[str, Any], ctx: ServerContext, summary: Any) -> str:
    """The baseline readout's run finished with ``summary``; returns its run id."""
    loop = DashboardLoop(ctx)
    assert await loop.submit_queued() == 1
    running = await _readout(ctx, kit["dash_id"], 1)
    await complete_run(
        ctx,
        running.run_ids[0],
        add={"summary.json": (json.dumps(summary).encode(), "application/json")},
    )
    assert (await _readout(ctx, kit["dash_id"], 1)).status == "materialising"
    return running.run_ids[0]


async def test_a_transient_storage_error_is_retried_not_failed(
    kit: dict[str, Any], ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_server.services.dashboards.readouts import MAX_MATERIALISE_ATTEMPTS
    from mapi_spec.storage import StorageError

    await _baseline_done(kit, ctx, SUMMARY)
    real_get = ctx.store.get
    calls = {"n": 0}

    async def flaky(bucket: str, name: str, **kw: Any) -> bytes:
        if name.endswith("summary.json"):
            calls["n"] += 1
            raise StorageError("503 from the object store", bucket=bucket, name=name)
        return await real_get(bucket, name, **kw)

    monkeypatch.setattr(ctx.store, "get", flaky)
    loop = DashboardLoop(ctx)
    for k in range(1, MAX_MATERIALISE_ATTEMPTS):
        assert await loop.materialise() == 0
        row = await _readout(ctx, kit["dash_id"], 1)
        assert row.status == "materialising" and row.attempts == k  # never failed for it
    # it recovers when the store does
    monkeypatch.setattr(ctx.store, "get", real_get)
    assert await loop.materialise() == 1
    assert (await _readout(ctx, kit["dash_id"], 1)).status == "ready"
    assert calls["n"] == MAX_MATERIALISE_ATTEMPTS - 1


async def test_a_readout_that_keeps_raising_fails_after_the_cap(
    kit: dict[str, Any], ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_server.services.dashboards import readouts
    from mapi_server.services.dashboards.readouts import MAX_MATERIALISE_ATTEMPTS

    await _baseline_done(kit, ctx, SUMMARY)

    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(readouts, "build_snapshot", boom)
    loop = DashboardLoop(ctx)
    for _ in range(MAX_MATERIALISE_ATTEMPTS):
        await loop.materialise()
    row = await _readout(ctx, kit["dash_id"], 1)
    assert row.status == "failed" and row.problem is not None
    assert row.problem["code"] == "INTERNAL_ERROR"


async def test_overflowing_and_ragged_summaries_still_materialise(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    """Two cells of 1e308 overflow to inf when summed (JSONB refuses ``Infinity``, so the
    commit failed on every pass); a ragged row has no mean column. Both store as nulls."""
    summary = {
        "tables": [
            {
                "variable": "y",
                "columns": ["t", "geo", "mean", "hdi_90_lower", "hdi_90_upper"],
                "dims": ["t", "geo"],
                "time_dim": "t",
                "rows": [
                    ["2024-01-01", "de", 1e308, 1e308, 1e308],
                    ["2024-01-01", "fr", 1e308, 1e308, 1e308],
                    ["2024-02-01", "de", 400.0, 380.0, 420.0],
                    ["2024-02-01", "fr"],
                ],
            }
        ]
    }
    await _baseline_done(kit, ctx, summary)
    assert await DashboardLoop(ctx).materialise() == 1
    ready = await _readout(ctx, kit["dash_id"], 1)
    assert ready.status == "ready", ready.problem
    assert "Infinity" not in json.dumps(ready.snapshot)
    snap = ReadoutSnapshot.model_validate(ready.snapshot)
    first = snap.series[0].points[0]
    assert first.t == "2024-01-01" and first.mean is None


async def test_uploaded_actuals_use_the_stored_time_dim(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    """The KPI is (geo, t) and the summary names no time dim: the actuals are still keyed
    by period (the contract's time dim, stored with the upload), never by geo."""
    summary = {"tables": [{**SUMMARY["tables"][0], "time_dim": None}]}
    await _baseline_done(kit, ctx, summary)
    upload = {
        "coords": {"geo": {"dims": ["geo"], "data": ["de", "fr"]},
                   "t": {"dims": ["t"], "data": ["2024-05-01", "2024-06-01"]}},
        "data_vars": {"y": {"dims": ["geo", "t"], "data": [[200.0, 210.0], [220.0, 215.25]]}},
    }  # fmt: skip
    meta = await ctx.store.put(
        ctx.buckets.quarantine, "t/default/inline/actuals.json", json.dumps(upload).encode()
    )
    ref = {"bucket": ctx.buckets.quarantine, "name": "t/default/inline/actuals.json",
           "generation": meta.generation, "kpi_column": "y", "time_dim": "t"}  # fmt: skip
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == kit["dash_id"], m.DashboardReadout.seq == 1)
            .values(kind="actuals", trigger="actuals", input_ref=ref)
        )
    assert await DashboardLoop(ctx).materialise() == 1
    ready = await _readout(ctx, kit["dash_id"], 1)
    snap = ReadoutSnapshot.model_validate(ready.snapshot)
    points = {p.t: p.actual for p in snap.series[0].points}
    assert points["2024-05-01"] == 420.0 and points["2024-06-01"] == 425.25
    assert "de" not in points and "fr" not in points


async def test_an_unreadable_upload_never_becomes_a_ready_comparison(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    await _baseline_done(kit, ctx, SUMMARY)
    ref = {"bucket": ctx.buckets.quarantine, "name": "t/default/inline/gone.json",
           "generation": 1, "kpi_column": "y", "time_dim": "t"}  # fmt: skip
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == kit["dash_id"], m.DashboardReadout.seq == 1)
            .values(kind="actuals", trigger="actuals", input_ref=ref)
        )
    await DashboardLoop(ctx).materialise()
    row = await _readout(ctx, kit["dash_id"], 1)
    assert row.status == "failed" and row.problem is not None


async def test_a_version_that_ran_uploaded_code_never_materialises(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    await _baseline_done(kit, ctx, SUMMARY)
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, kit["model"]["mv_id"])
        assert mv is not None
        mv.lineage = {**(mv.lineage or {}), "overlay_hash": "sha256:" + "1" * 64}
    await DashboardLoop(ctx).materialise()
    row = await _readout(ctx, kit["dash_id"], 1)
    assert row.status == "failed" and row.problem is not None
    assert row.problem["code"] == "CAPABILITY_UNSUPPORTED"


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


async def test_viewers_get_a_generic_problem_without_the_runs_text(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    """A client's own failed readout: the code and a fixed explanation, never the detail or
    the ``untrusted_text`` the publisher's model code wrote. Analysts keep the full one."""
    base = f"/v1/dashboards/{kit['dash_id']}"
    r = await kit["cleo"].post(f"{base}/scenarios", json={"multipliers": {"tv": 0.8}})
    seq = r.json()["seq"]
    leak = "Traceback: /srv/models/zq-secret/model.py line 3, host db.internal"
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == kit["dash_id"], m.DashboardReadout.seq == seq)
            .values(status="failed", snapshot=None, problem={
                "type": "about:blank", "title": "Run failed", "status": 500,
                "code": "USER_CODE_ERROR", "detail": "ValueError at /srv/models/zq-secret",
                "untrusted_text": leak, "instance": "/v1/runs/run_x",
                "errors": [{"code": "USER_CODE_ERROR", "pointer": "/x", "message": leak}],
            })
        )  # fmt: skip
    mine = (await kit["cleo"].get(f"{base}/readouts/{seq}")).json()
    assert mine["status"] == "failed" and mine["problem"]["code"] == "USER_CODE_ERROR"
    assert "zq-secret" not in str(mine) and not mine["problem"].get("untrusted_text")
    assert not mine["problem"].get("errors") and not mine["problem"].get("instance")
    full = (await kit["vic"].get(f"{base}/readouts/{seq}")).json()
    assert full["problem"]["untrusted_text"] == leak


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
    # the pruned readout was the newest: its seq is never handed out again
    r = await kit["cleo"].post(
        f"/v1/dashboards/{kit['dash_id']}/scenarios", json={"multipliers": {"tv": 0.9}}
    )
    assert r.json()["seq"] == seq + 1


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


# 60 weeks: the grid holds window totals, the readout shows one week
GRID_60: dict[str, Any] = {
    "format": "mapi.response-grid/v1",
    "kpi": "y",
    "periods": 60,
    "channels": ["tv", "search"],
    "multipliers": [0.5, 1.0, 1.5],
    "draws": 2,
    "contribution": [[[6000.0, 9000.0, 10800.0], [1800.0, 3000.0, 3600.0]],
                     [[5400.0, 8400.0, 10200.0], [2400.0, 3600.0, 4200.0]]],
    "spend": [[60000.0, 120000.0, 180000.0], [12000.0, 24000.0, 36000.0]],
    "current_spend": [120000.0, 24000.0],
    "baseline": [12000.0, 12600.0],
}  # fmt: skip


def test_grid_facts_are_per_period_and_commensurate_with_the_kpi() -> None:
    grid = ResponseGridView.model_validate(GRID_60)
    facts = grid_facts(grid)
    # per week = window total / 60; the return is a ratio, so the window cancels
    assert facts["tv"].contribution == pytest.approx(8700.0 / 60)
    assert facts["tv"].spend == pytest.approx(120000.0 / 60)
    assert facts["search"].contribution == pytest.approx(3300.0 / 60)
    assert facts["tv"].roi == pytest.approx(8700.0 / 120000.0)
    assert facts["tv"].roi_low == pytest.approx(8400.0 / 120000.0)
    assert facts["tv"].mroi == pytest.approx((10500.0 - 8700.0) / 60000.0)
    # the KPI of one week is the model's per-week expectation: the channels fit inside it
    kpi_week = (12300.0 + 8700.0 + 3300.0) / 60  # mean baseline + contributions, per week
    points = kpi_series(
        ScoreSummary.model_validate(
            {"tables": [{"variable": "y", "columns": ["t", "mean"], "dims": ["t"],
                         "rows": [["2024-01-01", kpi_week]]}]}
        ),
        "y",
    )  # fmt: skip
    snap = build_snapshot(_spec(), kind="refresh", points=points, facts=merge_facts({}, facts))
    total = sum(c.contribution or 0.0 for c in snap.channels)
    assert 0 < total < kpi_week
    tiles = {t.key: t for t in snap.tiles}
    assert tiles["return_on_media"].value == pytest.approx(12000.0 / 144000.0)
    # the fit's return times the grid's spend is per week too
    merged = merge_facts({"tv": ChannelFacts(roi=0.05)}, {"tv": ChannelFacts(spend=2000.0)})
    assert merged["tv"].contribution == pytest.approx(100.0)


def test_grid_whatifs_answer_per_period() -> None:
    grid = ResponseGridView.model_validate(GRID_60)
    plan = {"tv": 1.5, "search": 0.5}
    res = whatif_result(_spec(), grid, plan)
    window = evaluate_scenario(grid, WhatIfScenario(multipliers=plan))
    assert res.delta_kpi == pytest.approx(window.kpi_delta.mean / 60)
    tiles = {t.key: t for t in res.tiles}
    assert window.kpi is not None
    assert tiles["expected"].value == pytest.approx(window.kpi.mean / 60)
    assert tiles["expected"].caption is not None and "A week on average over 60 weeks" in (
        tiles["expected"].caption
    )
    tv = next(c for c in res.channels if c.name == "tv")
    assert tv.contribution == pytest.approx(window.channels["tv"].contribution.mean / 60)
    assert tv.spend == pytest.approx(180000.0 / 60)
    assert res.lead.endswith("a week than the current plan.")
