"""The automatic layout 1 → layout 2 upgrade at start-up (v0.17, plan §10.1).

The dashboards are the v0.16 ones of :mod:`test_dashboards_crud`: the trend model
(``ts.hsgp_trend``) with placeholder channels ``tv`` and ``search``, so the upgrade picks the
``forecast`` preset. The media planner case feeds the same version's catalog as a media mix
model (family ``mmm``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import inputs, sources, upgrade
from mapi_server.services.dashboards.upgrade import (
    ADVISORY_LOCK_KEY,
    UpgradeReport,
    auto_upgrade_v1,
    run_auto_upgrade,
)
from mapi_server.settings import Settings
from mapi_server_testkit.identity import Caller
from mapi_spec.audit import SYSTEM_ACTOR_ID
from mapi_spec.dashboard_v2 import UPGRADE_NOTE, SourceCatalog, parse_dashboard_spec
from mapi_spec.dashboards import UPGRADE_NEEDS_REVIEW, DashboardSpec
from mapi_spec.errors import ErrorCode, MapiError
from sqlalchemy import select, text
from test_dashboards_crud import LAYOUT1, create_published, dashboard_kit, dashboard_spec

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update=LAYOUT1)  # layout 1 dashboards are created here too


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    sources.clear_catalog_cache()
    return await dashboard_kit(ctx, client)


async def revisions(ctx: ServerContext, dash_id: str) -> list[m.DashboardRevision]:
    async with ctx.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(m.DashboardRevision)
                    .where(m.DashboardRevision.dashboard_id == dash_id)
                    .order_by(m.DashboardRevision.seq)
                )
            ).scalars()
        )
        s.expunge_all()
    return rows


async def dashboard(ctx: ServerContext, dash_id: str) -> m.Dashboard:
    async with ctx.db.session() as s:
        row = await s.get(m.Dashboard, dash_id)
        assert row is not None
        s.expunge(row)
    return row


async def readouts(ctx: ServerContext, dash_id: str) -> list[m.DashboardReadout]:
    async with ctx.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(m.DashboardReadout)
                    .where(m.DashboardReadout.dashboard_id == dash_id)
                    .order_by(m.DashboardReadout.seq)
                )
            ).scalars()
        )
        s.expunge_all()
    return rows


async def upgraded_audits(ctx: ServerContext, dash_id: str) -> list[m.AuditEvent]:
    async with ctx.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == "dashboard.upgraded",
                        m.AuditEvent.resource_id == dash_id,
                    )
                )
            ).scalars()
        )
        s.expunge_all()
    return rows


def _layout(spec: dict[str, Any]) -> int:
    return 2 if spec.get("layout_version") == 2 else 1


# =================================================================================== upgrades


async def test_a_published_forecast_moves_to_the_forecast_preset(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await create_published(kit)
    before = await dashboard(ctx, dash_id)
    report = await auto_upgrade_v1(ctx)
    assert report.ran and report.published == [dash_id] and report.drafts == [dash_id]
    assert report.needs_review == [] and report.retry == []
    old, new = await revisions(ctx, dash_id)
    assert _layout(old.spec) == 1 and _layout(new.spec) == 2  # the old row is untouched
    spec = parse_dashboard_spec(new.spec)
    assert not isinstance(spec, DashboardSpec) and spec.preset == "forecast"
    assert spec.title == "Weekly media results" and spec.client_name == "Acme Retail"
    assert (new.model_version_id, new.model_ref) == (old.model_version_id, old.model_ref)
    assert (new.endpoint_alias, new.endpoint_revision) == (old.endpoint_alias, None)
    assert new.created_by == SYSTEM_ACTOR_ID and new.seq == 2
    after = await dashboard(ctx, dash_id)
    assert after.published_revision_id == new.id and after.run_as == before.run_as
    assert _layout(after.draft_spec) == 2 and after.draft_etag != before.draft_etag
    assert after.problem is None and after.status == "published"
    first, baseline = await readouts(ctx, dash_id)
    assert first.revision_id == old.id and first.kind == "baseline"  # history stays layout 1
    assert (baseline.kind, baseline.trigger, baseline.status) == ("baseline", "publish", "queued")
    assert baseline.revision_id == new.id and baseline.actor_kind == "system"
    assert baseline.spec_hash == new.spec_hash
    (audit,) = await upgraded_audits(ctx, dash_id)
    assert audit.outcome == "success" and audit.actor_kind == "service"
    assert audit.actor_id == SYSTEM_ACTOR_ID
    assert audit.details["from_revision"] == 1 and audit.details["to_revision"] == 2
    assert audit.details["draft"] is True and audit.details["preset"] == "forecast"
    # clients see the new layout with the one-time note
    body = (await kit["cleo"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["layout_version"] == 2 and body["upgrade_note"].startswith(UPGRADE_NOTE)


async def test_the_pass_is_idempotent(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash_id = await create_published(kit)
    await auto_upgrade_v1(ctx)
    etag = (await dashboard(ctx, dash_id)).draft_etag
    again = await auto_upgrade_v1(ctx)
    assert again.ran and again.published == [] and again.drafts == []
    assert len(await revisions(ctx, dash_id)) == 2
    assert (await dashboard(ctx, dash_id)).draft_etag == etag
    assert len(await upgraded_audits(ctx, dash_id)) == 1


async def test_a_draft_only_dashboard_gets_a_layout_2_draft(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    eve: Caller = kit["eve"]
    r = await eve.post("/v1/dashboards", json={"name": "wip", "spec": dashboard_spec()})
    dash_id = r.json()["id"]
    report = await auto_upgrade_v1(ctx)
    assert report.drafts == [dash_id] and report.published == []
    row = await dashboard(ctx, dash_id)
    assert row.status == "draft" and _layout(row.draft_spec) == 2
    assert await revisions(ctx, dash_id) == []
    (audit,) = await upgraded_audits(ctx, dash_id)
    assert audit.details["draft"] is True and audit.details["to_revision"] is None


async def test_an_editors_layout_2_draft_is_kept(ctx: ServerContext, kit: dict[str, Any]) -> None:
    ana: Caller = kit["ana"]
    dash_id = await create_published(kit)
    etag = (await ana.get(f"/v1/dashboards/{dash_id}")).json()["draft_etag"]
    r = await ana.post(f"/v1/dashboards/{dash_id}:upgrade", headers={"If-Match": etag})
    assert r.status_code == 200, r.text
    draft = r.json()["dashboard"]["draft_spec"]
    draft_etag = r.json()["dashboard"]["draft_etag"]
    report = await auto_upgrade_v1(ctx)
    assert report.published == [dash_id] and report.drafts == []
    row = await dashboard(ctx, dash_id)
    assert row.draft_spec == draft and row.draft_etag == draft_etag


async def test_a_media_mix_model_becomes_the_planner(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await create_published(kit, whatif={"enabled": True})

    async def as_mmm(c: ServerContext, s: Any, mv: m.ModelVersion) -> SourceCatalog:
        cat = await sources.source_catalog(c, s, mv)
        modes = sorted({*cat.score_modes, "counterfactual"})
        return cat.model_copy(update={"family": "mmm", "namespace": "mmm", "score_modes": modes})

    report = await auto_upgrade_v1(ctx, catalog_of=as_mmm)
    assert report.published == [dash_id], report
    _, new = await revisions(ctx, dash_id)
    spec = parse_dashboard_spec(new.spec)
    assert not isinstance(spec, DashboardSpec)
    assert spec.preset == "mmm_planner" and spec.mmm is not None
    v1 = DashboardSpec.model_validate(dashboard_spec(whatif={"enabled": True}))
    assert [c.name for c in spec.mmm.channels] == [c.name for c in v1.channels]
    assert spec.mmm.tiles == v1.tiles and spec.mmm.whatif == v1.whatif
    # the planner reads the KPI as layout 1 does (the same numbers)
    assert spec.subject.variable == v1.kpi.variable == "y"
    assert spec.subject.value_per_unit == v1.kpi.value_per_unit
    # the what-if inputs (grid, evaluate, scenarios) read the planner as its layout 1 spec
    planner = inputs._planner_spec(new)
    assert isinstance(planner, DashboardSpec)
    assert planner.channels == v1.channels and planner.whatif == v1.whatif
    assert planner.kpi.variable == v1.kpi.variable and planner.tiles == v1.tiles
    assert planner.limits.exact_runs_per_day == spec.limits.exact_runs_per_day


async def test_the_upgraded_planner_baseline_keeps_its_layout_1_centres(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    """Plan §10.1-2: the planner's baseline is a predictive run (the band) and an expected
    run (the layout 1 centres); the readout waits for both."""
    from mapi_server.services.dashboards.loop import DashboardLoop
    from mapi_server.services.dashboards.readouts import run_terminal
    from mapi_spec.run import RunState

    dash_id = await create_published(kit, whatif={"enabled": True})

    async def as_mmm(c: ServerContext, s: Any, mv: m.ModelVersion) -> SourceCatalog:
        cat = await sources.source_catalog(c, s, mv)
        modes = sorted({*cat.score_modes, "counterfactual"})
        return cat.model_copy(update={"family": "mmm", "namespace": "mmm", "score_modes": modes})

    assert (await auto_upgrade_v1(ctx, catalog_of=as_mmm)).published == [dash_id]
    assert await DashboardLoop(ctx).submit_queued() >= 1
    row = (await readouts(ctx, dash_id))[-1]  # the layout 2 baseline (after the layout 1 one)
    assert row.status == "running" and len(row.run_ids) == 2
    async with ctx.db.session() as s:
        runs = [await s.get(m.Run, r) for r in row.run_ids]
        kinds = [r.payload["request"]["output"].get("kind", "expected") for r in runs if r]
        assert kinds == ["predictive", "expected"]
    for i, rid in enumerate(row.run_ids):
        async with ctx.db.session() as s:
            run = await s.get(m.Run, rid)
            assert run is not None
            run.state = RunState.SUCCEEDED.value
            await run_terminal(s, run, RunState.SUCCEEDED)
        status = next(r.status for r in await readouts(ctx, dash_id) if r.seq == row.seq)
        assert status == ("running" if i == 0 else "materialising")


async def test_a_non_planner_layout_2_spec_has_no_media_planner_inputs(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await create_published(kit)
    report = await auto_upgrade_v1(ctx)
    assert report.published == [dash_id], report
    _, new = await revisions(ctx, dash_id)
    with pytest.raises(MapiError) as err:
        inputs._planner_spec(new)
    assert err.value.code is ErrorCode.DASHBOARD_INPUT_DISABLED


# ==================================================================================== refusals


async def test_a_blocked_upgrade_keeps_layout_1_and_flags_the_dashboard(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    dash_id = await create_published(kit)

    def boom(spec: Any, catalog: Any) -> Any:
        raise ValueError("no preset fits")

    monkeypatch.setattr(upgrade, "upgrade_v1", boom)
    report = await auto_upgrade_v1(ctx)
    assert report.needs_review == [dash_id] and report.published == []
    row = await dashboard(ctx, dash_id)
    assert row.problem == UPGRADE_NEEDS_REVIEW and _layout(row.draft_spec) == 1
    assert len(await revisions(ctx, dash_id)) == 1
    (audit,) = await upgraded_audits(ctx, dash_id)
    assert audit.outcome == "error" and audit.code == "SCHEMA_INVALID"
    assert audit.details["issues"] == [{"code": "SCHEMA_INVALID", "pointer": ""}]
    # the builder's banner: the analyst DTO carries the blocking issues
    body = (await kit["eve"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["problem"] == UPGRADE_NEEDS_REVIEW
    assert body["upgrade_issues"] and "no preset fits" in body["upgrade_issues"][0]["message"]
    # the next start tries again (audited once), and once it works the flag goes
    await auto_upgrade_v1(ctx)
    assert len(await upgraded_audits(ctx, dash_id)) == 1
    monkeypatch.undo()
    report = await auto_upgrade_v1(ctx)
    assert report.published == [dash_id]
    assert (await dashboard(ctx, dash_id)).problem is None


async def _broken_draft(ctx: ServerContext, dash_id: str) -> None:
    """An unfinished layout 1 draft whose source no longer resolves."""
    async with ctx.db.session() as s:
        row = await s.get(m.Dashboard, dash_id)
        assert row is not None
        row.draft_spec = {**dashboard_spec(), "source": {"kind": "model_version",
                                                         "model_id": "nope"}}  # fmt: skip


async def test_a_broken_draft_never_holds_back_the_published_revision(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await create_published(kit)
    await _broken_draft(ctx, dash_id)
    report = await auto_upgrade_v1(ctx)
    assert report.published == [dash_id] and report.needs_review == [dash_id]
    _, new = await revisions(ctx, dash_id)
    assert _layout(new.spec) == 2  # clients get the new layout
    row = await dashboard(ctx, dash_id)
    assert _layout(row.draft_spec) == 1 and row.problem == UPGRADE_NEEDS_REVIEW  # kept, flagged
    # the banner names what stops the draft (the published part only had notes)
    body = (await kit["eve"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["upgrade_issues"], body
    assert all(i["severity"] == "error" for i in body["upgrade_issues"])
    assert any(i["pointer"].startswith("/source") for i in body["upgrade_issues"])
    # idempotent: the next start publishes nothing new and audits the flag once
    again = await auto_upgrade_v1(ctx)
    assert again.published == [] and len(await revisions(ctx, dash_id)) == 2


async def test_the_review_lists_the_drafts_issues_beside_a_published_v1(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_spec.errors import Issue, Severity

    dash_id = await create_published(kit)
    await _broken_draft(ctx, dash_id)
    real = upgrade.convert

    async def notes_only(c: Any, s: Any, spec: Any, mv: Any, **kw: Any) -> Any:
        got = await real(c, s, spec, mv, **kw)
        if mv is None:
            return got
        info = Issue(code=ErrorCode.BINDING_INVALID, severity=Severity.INFO, pointer="/x",
                     message="accuracy shows after actuals are uploaded")  # fmt: skip
        return upgrade.Conversion(None, [], [info, *got.issues])  # blocked, infos only

    monkeypatch.setattr(upgrade, "convert", notes_only)
    async with ctx.db.session() as s:
        row = await s.get(m.Dashboard, dash_id)
        assert row is not None
        row.problem = UPGRADE_NEEDS_REVIEW
        issues = await upgrade.review_issues(ctx, s, row)
    assert issues and all(i.severity is Severity.ERROR for i in issues)
    assert any(i.pointer.startswith("/source") for i in issues)


async def test_the_flag_keeps_other_problems_and_survives_a_promotion(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    dash_id = await create_published(kit)
    async with ctx.db.session() as s:
        row = await s.get(m.Dashboard, dash_id)
        assert row is not None
        row.problem = "new_version_available"

    def boom(spec: Any, catalog: Any) -> Any:
        raise ValueError("no preset fits")

    monkeypatch.setattr(upgrade, "upgrade_v1", boom)
    assert (await auto_upgrade_v1(ctx)).needs_review == [dash_id]
    assert (await dashboard(ctx, dash_id)).problem == "new_version_available"
    assert len(await upgraded_audits(ctx, dash_id)) == 1
    await auto_upgrade_v1(ctx)  # audited once even though the flag is not the problem
    assert len(await upgraded_audits(ctx, dash_id)) == 1


async def test_the_pass_waits_for_the_image_catalog(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """At a release the catalog of the new images is described after start-up: without it
    the pass would pick ``generic`` for good, so the dashboard waits (retry)."""
    dash_id = await create_published(kit)
    assert ctx.catalogs is not None

    async def none(session: Any) -> None:
        return None

    monkeypatch.setattr(ctx.catalogs, "get", none)
    report = await auto_upgrade_v1(ctx)
    assert report.retry == [dash_id] and report.published == [] and report.needs_review == []
    assert len(await revisions(ctx, dash_id)) == 1
    monkeypatch.undo()
    report = await auto_upgrade_v1(ctx)
    assert report.published == [dash_id]
    _, new = await revisions(ctx, dash_id)
    assert new.spec["preset"] == "forecast"


async def test_the_loop_retries_a_pass_that_left_dashboards_for_later(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_server.services.dashboards import loop as loop_mod

    loop = loop_mod.DashboardLoop(ctx)
    loop.upgraded = UpgradeReport(ran=True, retry=["dsh_x"])
    seen: list[int] = []

    async def again(c: Any) -> UpgradeReport:
        seen.append(1)
        return UpgradeReport(ran=True)

    monkeypatch.setattr(loop_mod, "run_auto_upgrade", again)
    assert await loop.retry_upgrade(force=True) is not None and seen == [1]
    assert await loop.retry_upgrade(force=True) is None  # nothing left to retry


async def test_pictures_of_a_publisher_who_lost_run_rights_never_hang_the_pass(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_server.services.dashboards import assets

    dash_id = await create_published(kit)
    async with ctx.db.session() as s:
        await s.execute(
            text("UPDATE memberships SET role = 'viewer' WHERE principal_id = :u"),
            {"u": kit["users"]["ana"].id},
        )
    queued: list[str] = []

    async def queue(octx: Any, session: Any, rev: Any, spec: Any) -> None:
        queued.append(rev.id)

    monkeypatch.setattr(assets, "picture_panels", lambda spec: ["plot"])
    monkeypatch.setattr(assets, "queue_pictures", queue)
    report = await asyncio.wait_for(auto_upgrade_v1(ctx), 20)
    assert report.published == [dash_id] and queued == []
    assert (await dashboard(ctx, dash_id)).live_inputs != "paused"  # no write under the lock


async def test_an_unreadable_catalog_needs_review(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash_id = await create_published(kit)

    async def unreadable(c: ServerContext, s: Any, mv: m.ModelVersion) -> SourceCatalog:
        raise MapiError("no input contract", code=ErrorCode.CAPABILITY_UNSUPPORTED)

    report = await auto_upgrade_v1(ctx, catalog_of=unreadable)
    assert report.needs_review == [dash_id]
    assert (await dashboard(ctx, dash_id)).problem == UPGRADE_NEEDS_REVIEW


async def test_a_transient_error_changes_nothing(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash_id = await create_published(kit)

    async def flaky(c: ServerContext, s: Any, mv: m.ModelVersion) -> SourceCatalog:
        raise OSError("storage is away")

    report = await auto_upgrade_v1(ctx, catalog_of=flaky)
    assert report.retry == [dash_id] and report.needs_review == []
    row = await dashboard(ctx, dash_id)
    assert row.problem is None and _layout(row.draft_spec) == 1
    assert await upgraded_audits(ctx, dash_id) == []
    assert (await auto_upgrade_v1(ctx)).published == [dash_id]


async def test_archived_dashboards_are_untouched(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash_id = await create_published(kit)
    assert (await kit["ana"].post(f"/v1/dashboards/{dash_id}:archive")).status_code == 200
    report = await auto_upgrade_v1(ctx)
    assert report.ran and report.published == [] and report.drafts == []
    assert len(await revisions(ctx, dash_id)) == 1
    assert _layout((await dashboard(ctx, dash_id)).draft_spec) == 1


async def test_the_setting_turns_the_pass_off(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    dash_id = await create_published(kit)
    assert not (await auto_upgrade_v1(ctx, enabled=False)).ran
    monkeypatch.setattr(upgrade, "auto_upgrade_enabled", lambda c: False)
    assert not (await auto_upgrade_v1(ctx)).ran
    assert not (await run_auto_upgrade(ctx)).ran
    assert len(await revisions(ctx, dash_id)) == 1


# ================================================================================= concurrency


async def test_a_worker_holding_the_lock_makes_the_others_skip(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await create_published(kit)
    async with ctx.db.session() as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        skipped = await auto_upgrade_v1(ctx)
    assert not skipped.ran and len(await revisions(ctx, dash_id)) == 1


async def test_concurrent_workers_upgrade_once(ctx: ServerContext, kit: dict[str, Any]) -> None:
    ids = [await create_published(kit, name=f"acme-{i}", grant=False) for i in range(3)]
    reports = await asyncio.gather(*(auto_upgrade_v1(ctx) for _ in range(3)))
    done = sorted(d for r in reports for d in r.published)
    assert done == sorted(ids)
    for dash_id in ids:
        assert len(await revisions(ctx, dash_id)) == 2
        assert len(await upgraded_audits(ctx, dash_id)) == 1


async def test_the_start_up_hook_never_raises(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(c: ServerContext, **kw: Any) -> UpgradeReport:
        raise RuntimeError("database is away")

    monkeypatch.setattr(upgrade, "auto_upgrade_v1", broken)
    assert await run_auto_upgrade(ctx) == UpgradeReport()
