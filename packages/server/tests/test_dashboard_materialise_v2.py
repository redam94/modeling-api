"""Materialising layout 2 readouts in the dashboard loop (v0.17, plan I-3, I-6, §10).

The dashboards are the v0.16 ones of :mod:`test_dashboards_crud` (the ``trend`` model with a
response grid), moved to layout 2 by the automatic upgrade: the ``forecast`` preset from the
version's own catalog, or the media planner when the catalog says ``mmm``. The readout runs
are completed with hand-written summaries, as in :mod:`test_dashboard_readouts`.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import readouts, sources
from mapi_server.services.dashboards.inputs import clear_grid_cache
from mapi_server.services.dashboards.loop import DashboardLoop
from mapi_server.services.dashboards.readouts import SYSTEM, new_readout
from mapi_server.services.dashboards.upgrade import auto_upgrade_v1
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.integrations import refit_version
from mapi_server_testkit.plots import complete_run
from mapi_spec.dashboard_v2 import DashboardSpecV2, SourceCatalog, parse_dashboard_spec
from mapi_spec.dashboards import ReadoutSnapshot
from sqlalchemy import select
from test_dashboard_readouts import MONTHS, SUMMARY, _fit_metrics, _readout
from test_dashboard_refresh import _GRID_OBJECT, _endpoint_dashboard, _promote, _version
from test_dashboards_crud import LAYOUT1, create_published, dashboard_kit

pytestmark = pytest.mark.integration

MMM_WORDS = re.compile(r"\b(media|channels?|spend|roi|return on)\b", re.IGNORECASE)
PREDICTIVE = {  # the same centres as SUMMARY; the range of one new result is wider
    "tables": [
        {
            "variable": "y",
            "columns": ["t", "mean", "hdi_90_lower", "hdi_90_upper"],
            "dims": ["t"],
            "time_dim": "t",
            "rows": [[t, 400.0 + k, 340.0 + k, 460.0 + k] for k, t in enumerate(MONTHS)],
        }
    ]
}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT, **LAYOUT1})


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    clear_grid_cache()
    sources.clear_catalog_cache()
    return await dashboard_kit(ctx, client)


async def _as_mmm(c: ServerContext, s: Any, mv: m.ModelVersion) -> SourceCatalog:
    cat = await sources.source_catalog(c, s, mv)
    modes = sorted({*cat.score_modes, "counterfactual"})
    return cat.model_copy(update={"family": "mmm", "namespace": "mmm", "score_modes": modes})


async def _run_readout(ctx: ServerContext, dash_id: str, seq: int, summary: Any) -> ReadoutSnapshot:
    """Submit the queued readout ``seq``, finish its run with ``summary`` and materialise."""
    loop = DashboardLoop(ctx)
    assert await loop.submit_queued() >= 1
    running = await _readout(ctx, dash_id, seq)
    assert running.status == "running", running.problem
    await complete_run(
        ctx,
        running.run_ids[0],
        add={"summary.json": (json.dumps(summary).encode(), "application/json")},
    )
    assert await loop.materialise() >= 1
    ready = await _readout(ctx, dash_id, seq)
    assert ready.status == "ready", ready.problem
    return ReadoutSnapshot.model_validate(ready.snapshot)


async def _published_spec(ctx: ServerContext, dash_id: str) -> Any:
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None and dash.published_revision_id is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        return parse_dashboard_spec(rev.spec)


# ============================================================================== media planner


async def test_an_upgraded_planner_shows_the_numbers_of_its_last_v1_readout(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await create_published(kit, whatif={"enabled": True})
    await _fit_metrics(ctx, kit["model"]["mv_id"])
    v1 = await _run_readout(ctx, dash_id, 1, SUMMARY)
    assert v1.schema_version == 1 and v1.panels == []
    report = await auto_upgrade_v1(ctx, catalog_of=_as_mmm)
    assert report.published == [dash_id]
    spec = await _published_spec(ctx, dash_id)
    assert isinstance(spec, DashboardSpecV2) and spec.preset == "mmm_planner"
    v2 = await _run_readout(ctx, dash_id, 2, PREDICTIVE)  # the band is predictive now
    assert v2.schema_version == 2 and v2.preset == "mmm_planner"
    # every number equals the layout 1 readout; only the ranges differ
    assert v2.lead == v1.lead
    assert [(t.key, t.label, t.value, t.unit, t.delta) for t in v2.tiles] == [
        (t.key, t.label, t.value, t.unit, t.delta) for t in v1.tiles
    ]
    assert v2.channels == v1.channels
    assert [p.mean for p in v2.series[0].points] == [p.mean for p in v1.series[0].points]
    expected = {t.key: t for t in v2.tiles}["expected"]
    assert (expected.low, expected.high) == (345.0, 465.0)  # the latest period, predictive
    assert {t.key: t for t in v1.tiles}["expected"].low == 385.0
    # the planner parts read the legacy fields; the fan is a block
    assert [b.panel_id for b in v2.panels] == ["fan"]
    # history keeps the layout 1 readout as it was
    old = await _readout(ctx, dash_id, 1)
    assert ReadoutSnapshot.model_validate(old.snapshot) == v1


# ===================================================================================== layout 2


async def test_an_upgraded_forecast_materialises_its_panels(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await create_published(kit)
    await auto_upgrade_v1(ctx)
    spec = await _published_spec(ctx, dash_id)
    assert isinstance(spec, DashboardSpecV2) and spec.preset == "forecast"
    async with ctx.db.session() as s:
        baseline = (
            await s.execute(
                select(m.DashboardReadout.seq)
                .where(
                    m.DashboardReadout.dashboard_id == dash_id,
                    m.DashboardReadout.status == "queued",
                )
                .order_by(m.DashboardReadout.seq.desc())
                .limit(1)
            )
        ).scalar_one()  # the new layout's baseline (the layout 1 one is still queued)
    summary = json.loads(json.dumps(SUMMARY))
    summary["tables"][0]["variable"] = spec.subject.variable  # the model output (remapped)
    snap = await _run_readout(ctx, dash_id, baseline, summary)
    assert snap.schema_version == 2 and snap.preset == "forecast"
    assert [b.panel_id for b in snap.panels] == [p.id for p in spec.panels]
    assert not MMM_WORDS.search(snap.lead) and snap.channels == []
    assert snap.series and snap.series[0].name == spec.subject.variable
    assert all(t.key == "panel" for t in snap.tiles)
    row = await _readout(ctx, dash_id, baseline)
    assert row.period_end is not None and row.period_end.isoformat() == "2024-06-01"
    # clients read it as the latest readout
    body = (await kit["cleo"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["latest_readout"]["snapshot"]["schema_version"] == 2


async def test_a_layout_2_readout_without_a_readable_summary_fails(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await create_published(kit)
    await auto_upgrade_v1(ctx)
    loop = DashboardLoop(ctx)
    await loop.submit_queued()
    async with ctx.db.session() as s:
        row = (
            await s.execute(
                select(m.DashboardReadout)
                .where(
                    m.DashboardReadout.dashboard_id == dash_id,
                    m.DashboardReadout.status == "running",
                )
                .order_by(m.DashboardReadout.seq.desc())
                .limit(1)
            )
        ).scalar_one()
        seq, run_id = row.seq, row.run_ids[0]
    await complete_run(ctx, run_id, add={})
    await loop.materialise()
    failed = await _readout(ctx, dash_id, seq)
    assert failed.status == "failed" and "summary" in (failed.problem or {}).get("detail", "")


async def test_the_accuracy_history_holds_published_readouts_only(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await create_published(kit)
    await auto_upgrade_v1(ctx)
    detail = {"kind": "error", "n": 6, "mape": 4.0, "mae": 2.0}
    snap = ReadoutSnapshot.model_validate(
        {"lead": "x", "schema_version": 2, "accuracy_detail": detail}
    )
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None and dash.published_revision_id is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        made = {}
        for vis in ("published", "private", "pending_review", "hidden", "published"):
            r = await new_readout(
                s, dash, rev, kind="actuals", trigger="actuals", visibility=vis,  # type: ignore[arg-type]
                actor=SYSTEM, title="Actuals", status="ready", snapshot=snap,
            )  # fmt: skip
            made.setdefault(vis, []).append(r.seq)
        current = await new_readout(
            s, dash, rev, kind="actuals", trigger="actuals", visibility="published",
            actor=SYSTEM, title="Actuals", status="materialising",
        )  # fmt: skip
        history = await readouts._history(s, current, None, None)
    assert [p.seq for p in history] == made["published"]
    assert all(p.values["mape"] == 4.0 and p.n == 6 for p in history)


async def test_a_layout_2_dashboard_follows_its_endpoint(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await _endpoint_dashboard(kit, True)
    await auto_upgrade_v1(ctx)
    assert isinstance(await _published_spec(ctx, dash_id), DashboardSpecV2)
    v2 = await refit_version(ctx, await _version(ctx, kit["model"]["mv_id"]), add=_GRID_OBJECT)
    await _promote(kit, v2.version)
    body = (await kit["ana"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["published_revision"]["model_version_id"] == v2.id, body["problem"]
    assert body["problem"] is None and body["latest_readout"]["trigger"] == "endpoint"
    followed = await _published_spec(ctx, dash_id)
    assert isinstance(followed, DashboardSpecV2) and followed.preset == "forecast"


async def test_old_readouts_stay_layout_1_after_the_upgrade(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await create_published(kit)
    v1 = await _run_readout(ctx, dash_id, 1, SUMMARY)
    await auto_upgrade_v1(ctx)
    async with ctx.db.session() as s:
        old = await s.get(m.DashboardReadout, (dash_id, 1))
        assert old is not None and old.ready_at is not None
        assert old.ready_at <= now() + timedelta(seconds=1)
    assert ReadoutSnapshot.model_validate(old.snapshot) == v1 and v1.schema_version == 1
