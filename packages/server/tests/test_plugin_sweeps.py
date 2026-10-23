"""v0.19 WP6 (D5, D12, D15): the plugin housekeeping sweeps.

Ticket expiry, the purge after the retention (objects deleted, rows and audit kept, a
quarantine object the lifecycle already removed counts as purged) and the rekit on a new
base image digest (one describe per version and digest, within the tenant cap; changed
exports send an approved version back to the reviewers; failing kits withdraw it).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from _v019_pipeline import (
    UPLOADER,
    describe,
    finalize,
    load_version,
    runs_of,
    seed_version,
    to_finalizing,
    write_outputs,
)
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.housekeeping import Housekeeper
from mapi_server.services.plugins import finalize as plugin_finalize
from mapi_server.services.plugins.pipeline import LABEL_BASE_IMAGE, LABEL_REASON
from mapi_server.services.plugins.sweeps import PluginSweeps
from mapi_server.services.runs import now
from mapi_spec.api import new_id
from mapi_spec.executor import RunKind
from mapi_spec.jobs import PLUGIN_DESCRIBE_OUTPUT
from mapi_spec.plugins import PluginDescribe, describe_digest
from mapi_spec.run import RunState
from mapi_spec.storage import NotFound, plugin_quarantine_key
from sqlalchemy import select, update


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []

    async def record(
        session: Any, ctx: Any, version: m.PluginVersion, event: str, *, to: str, **_: Any
    ) -> None:
        seen.append((version.id, event, to))

    monkeypatch.setattr(plugin_finalize, "notify_plugin_event", record)
    return seen


@pytest.fixture
def excluded(
    events: list[tuple[str, str, str]], monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, ...]]:
    """The ``exclude`` of every plugin notification (beside ``events``)."""
    seen: list[tuple[str, ...]] = []
    inner = plugin_finalize.notify_plugin_event

    async def record(*args: Any, **kw: Any) -> None:
        seen.append(tuple(kw.get("exclude", ())))
        await inner(*args, **kw)

    monkeypatch.setattr(plugin_finalize, "notify_plugin_event", record)
    return seen


async def _gone(ctx: ServerContext, bucket: str, key: str) -> bool:
    try:
        await ctx.store.stat(bucket, key)
    except NotFound:
        return True
    return False


# ------------------------------------------------------------------------------------ tickets


async def test_expired_tickets_are_retired_and_their_objects_deleted(ctx: ServerContext) -> None:
    pid, _ = await seed_version(ctx, state="kit_passed")
    at = now()
    async with ctx.db.session() as s:
        for i, ago in enumerate((60, -60)):  # one expired a minute ago, one still valid
            uid = new_id("pup")
            key = plugin_quarantine_key("default", uid)
            await ctx.store.put(ctx.buckets.quarantine, key, b"half an upload")
            s.add(
                m.PluginUpload(
                    id=uid,
                    tenant_id="default",
                    plugin_id=pid,
                    project_id="default",
                    created_by=UPLOADER,
                    filename=f"t_abcdef_acme-0.{i}.0-py3-none-any.whl",
                    declared_size=10,
                    object_key=key,
                    expires_at=at - timedelta(seconds=ago),
                )
            )
    assert await PluginSweeps(ctx).expire_tickets(at) == 1
    async with ctx.db.session() as s:
        rows = {r.state: r for r in (await s.execute(select(m.PluginUpload))).scalars()}
    assert set(rows) == {"expired", "pending"}
    assert await _gone(ctx, ctx.buckets.quarantine, rows["expired"].object_key)
    assert not await _gone(ctx, ctx.buckets.quarantine, rows["pending"].object_key)
    assert await PluginSweeps(ctx).expire_tickets(at) == 0


# -------------------------------------------------------------------------------------- purge


async def _retained(ctx: ServerContext, state: str, *, days_ago: int, overlay: bool) -> str:
    _, vid = await seed_version(ctx, state=state, overlay=overlay)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.PluginVersion)
            .where(m.PluginVersion.id == vid)
            .values(purge_after=now() - timedelta(days=days_ago), scope="none")
        )
    return vid


@pytest.mark.parametrize("state", ["inspect_failed", "kit_failed", "rejected", "yanked"])
async def test_purge_deletes_objects_keeps_the_row_and_audits(
    ctx: ServerContext, state: str
) -> None:
    vid = await _retained(ctx, state, days_ago=1, overlay=state != "inspect_failed")
    before = await load_version(ctx, vid)
    assert await PluginSweeps(ctx).purge(now()) == 1
    after = await load_version(ctx, vid)
    assert after.state == "purged" and after.purged_at is not None
    assert await _gone(ctx, ctx.buckets.quarantine, before.quarantine_key)
    if before.overlay_key:
        assert await _gone(ctx, ctx.buckets.plugins, before.overlay_key)
    async with ctx.db.session() as s:
        audits = list(
            (
                await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == "plugin.purge"))
            ).scalars()
        )
    assert [a.resource_id for a in audits] == [vid]
    assert audits[0].details["previous_state"] == state


async def test_purge_tolerates_objects_the_lifecycle_already_deleted(ctx: ServerContext) -> None:
    vid = await _retained(ctx, "inspect_failed", days_ago=1, overlay=False)
    version = await load_version(ctx, vid)
    await ctx.store.delete(ctx.buckets.quarantine, version.quarantine_key)
    assert await PluginSweeps(ctx).purge(now()) == 1
    assert (await load_version(ctx, vid)).state == "purged"


async def test_nothing_is_purged_before_its_time_or_while_usable(ctx: ServerContext) -> None:
    await _retained(ctx, "yanked", days_ago=-1, overlay=True)  # due tomorrow
    _, approved = await seed_version(ctx, state="approved", overlay=True, version="0.2.0")
    assert await PluginSweeps(ctx).purge(now()) == 0
    assert (await load_version(ctx, approved)).state == "approved"


async def test_the_housekeeper_runs_the_plugin_sweeps(ctx: ServerContext) -> None:
    vid = await _retained(ctx, "rejected", days_ago=1, overlay=True)
    await Housekeeper(ctx).sweep_once()
    assert (await load_version(ctx, vid)).state == "purged"


# -------------------------------------------------------------------------------------- rekit


async def test_rekit_queues_one_describe_per_version_and_digest(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    sweeps = PluginSweeps(ctx)
    assert await sweeps.rekit(now()) == 1
    (run,) = await runs_of(ctx, RunKind.PLUGIN_DESCRIBE)
    assert run.labels[LABEL_REASON] == "rekit"
    assert run.labels[LABEL_BASE_IMAGE] == ctx.catalogs.image_digest
    assert run.created_by == UPLOADER and run.untrusted_code
    assert await sweeps.rekit(now()) == 0  # one is pending already
    assert (await load_version(ctx, vid)).state == "approved"


async def test_rekit_skips_versions_described_for_this_digest_and_unusable_ones(
    ctx: ServerContext,
) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    await seed_version(ctx, state="kit_failed", version="0.2.0")
    await seed_version(ctx, state="describing", overlay=True, version="0.3.0")
    async with ctx.db.session() as s:
        s.add(
            m.PluginDescribe(
                version_id=vid,
                base_image_digest=ctx.catalogs.image_digest,
                tenant_id="default",
                run_id="run_x",
                describe_digest="sha256:" + "1" * 64,
                exports={},
                kit={"ok": True, "checks": []},
                ok=True,
            )
        )
    assert await PluginSweeps(ctx).rekit(now()) == 0


async def test_rekit_respects_the_tenant_cap(ctx: ServerContext) -> None:
    for i in range(3):
        await seed_version(ctx, state="approved", overlay=True, version=f"0.{i}.0")
    assert (
        await PluginSweeps(ctx).rekit(now()) == ctx.settings.plugin_pipeline_concurrency_per_tenant
    )


async def _rekit_result(ctx: ServerContext, vid: str, body: bytes) -> m.PluginVersion:
    assert await PluginSweeps(ctx).rekit(now()) == 1
    (drun,) = await runs_of(ctx, RunKind.PLUGIN_DESCRIBE)
    run, att = await to_finalizing(ctx, drun.id)
    await write_outputs(ctx, run, att, {PLUGIN_DESCRIBE_OUTPUT: body})
    assert await finalize(ctx, run, att) is RunState.SUCCEEDED
    return await load_version(ctx, vid)


async def _approve(ctx: ServerContext, vid: str, digest: str) -> None:
    async with ctx.db.session() as s:
        s.add(
            m.PluginApproval(
                id=new_id("pap"),
                tenant_id="default",
                version_id=vid,
                overlay_sha256=(await s.get(m.PluginVersion, vid)).upload_sha256,  # type: ignore[union-attr]
                describe_digest=digest,
                base_image_digest="sha256:" + "ee" * 32,
                decision="approve",
                approver_id="usr_admin",
                uploader_id=UPLOADER,
            )
        )


def _digest(body: bytes) -> str:
    return describe_digest(PluginDescribe.model_validate_json(body).exports)


async def test_rekit_with_the_same_exports_keeps_the_approval(
    ctx: ServerContext, events: list[tuple[str, str, str]]
) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    body = describe(vid)
    await _approve(ctx, vid, _digest(body))
    version = await _rekit_result(ctx, vid, body)
    assert version.state == "approved" and events == []


async def test_rekit_with_changed_exports_goes_back_to_the_reviewers(
    ctx: ServerContext, events: list[tuple[str, str, str]], excluded: list[tuple[str, ...]]
) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    await _approve(ctx, vid, _digest(describe(vid)))
    version = await _rekit_result(ctx, vid, describe(vid, step_name="clip"))
    assert version.state == "awaiting_approval" and version.scope == "private"
    assert events == [(vid, "plugin.version.awaiting_approval", "admins")]
    assert excluded == [(UPLOADER,)]  # review-flow-4: never the uploader (D14)


async def test_rekit_with_failing_kits_withdraws_the_version(
    ctx: ServerContext, events: list[tuple[str, str, str]]
) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    version = await _rekit_result(ctx, vid, describe(vid, kit_ok=False))
    assert version.state == "yanked" and version.yank_reason == "rekit_failed"
    assert version.yanked_at is not None and version.purge_after is not None
    assert events == [(vid, "plugin.version.yanked", "admins_and_uploader")]
