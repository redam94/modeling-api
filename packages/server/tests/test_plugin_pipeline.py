"""v0.19 WP6: the plugin check pipeline end to end on the control plane.

``:complete`` → ``plugin_inspect`` (untrusted, class S, the configured timeout, the quarantine
generation as its only input) → inspection.json → write-once copy into the plugins bucket,
re-hashed → ``plugin_describe`` mounting exactly that copy → describe.json → re-checked as
data → ``plugin_describes`` row → ``kit_passed``. Nothing here opens the "wheel" bytes.
"""

from __future__ import annotations

from typing import Any

import pytest
from _v019_pipeline import (
    NS,
    UPLOADER,
    WHEEL,
    WHEEL_SHA,
    UntrustedFakeExecutor,
    describe,
    finalize,
    inspection,
    load_version,
    runs_of,
    seed_version,
    to_finalizing,
    write_outputs,
)
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.plugins import finalize as plugin_finalize
from mapi_server.services.plugins.pipeline import (
    LABEL_BASE_IMAGE,
    LABEL_REASON,
    LABEL_VERSION,
    start_describe,
    start_inspect,
)
from mapi_server_testkit.builders import runspec_from_bootstrap
from mapi_spec.executor import RunKind
from mapi_spec.jobs import PLUGIN_DESCRIBE_OUTPUT, PLUGIN_INPUT, PLUGIN_INSPECTION_OUTPUT
from mapi_spec.plugins import DESCRIBE_MAX_BYTES
from mapi_spec.run import RunState
from mapi_spec.storage import plugin_key
from sqlalchemy import func, select


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """``(version id, event, recipients)`` of every plugin notification."""
    seen: list[tuple[str, str, str]] = []

    async def record(
        session: Any, ctx: Any, version: m.PluginVersion, event: str, *, to: str, **_: Any
    ) -> None:
        seen.append((version.id, event, to))

    monkeypatch.setattr(plugin_finalize, "notify_plugin_event", record)
    return seen


async def _inspect(ctx: ServerContext, version_id: str) -> m.Run:
    async with ctx.db.session() as s:
        version = await s.get(m.PluginVersion, version_id, with_for_update=True)
        assert version is not None
        run = await start_inspect(s, ctx, version, created_by=UPLOADER)
        s.expunge(run)
        return run


async def _through_inspect(ctx: ServerContext, version_id: str, body: bytes) -> m.Run:
    run = await _inspect(ctx, version_id)
    run, att = await to_finalizing(ctx, run.id)
    await write_outputs(ctx, run, att, {PLUGIN_INSPECTION_OUTPUT: body})
    assert await finalize(ctx, run, att) is RunState.SUCCEEDED
    return run


async def _catalog_rows(ctx: ServerContext) -> int:
    async with ctx.db.session() as s:
        return int((await s.execute(select(func.count()).select_from(m.Catalog))).scalar_one())


async def test_inspect_run_is_untrusted_small_and_reads_the_pinned_quarantine_object(
    ctx: ServerContext,
) -> None:
    _, vid = await seed_version(ctx)
    run = await _inspect(ctx, vid)
    assert run.kind == RunKind.PLUGIN_INSPECT.value
    assert run.untrusted_code and run.overlays == []
    assert run.resource_class == "S" and run.timeout_s == ctx.settings.plugin_inspect_timeout_s
    assert run.state == RunState.QUEUED.value and run.created_by == UPLOADER
    assert run.labels[LABEL_VERSION] == vid
    version = await load_version(ctx, vid)
    assert version.state == "inspecting" and version.inspect_run_id == run.id
    assert run.inputs[PLUGIN_INPUT] == {
        "bucket": ctx.buckets.quarantine,
        "name": version.quarantine_key,
        "generation": version.quarantine_generation,
    }
    assert run.payload["expected_sha256"] == WHEEL_SHA
    assert run.payload["namespace"] == NS and run.payload["dist_name"] == "t-abcdef-acme"
    assert run.payload["limits"]["max_bytes"] == ctx.settings.plugin_upload_max_bytes
    # the RunSpec signs only the quarantine object; nothing is mounted
    ex = UntrustedFakeExecutor()
    await Dispatcher(ctx, ex).dispatch_once()
    (execution,) = ex.executions.values()
    rs = await runspec_from_bootstrap(ctx, execution.handle.nonce)
    assert set(rs.inputs) == {PLUGIN_INPUT} and rs.overlays == []


async def test_happy_path_reaches_kit_passed_without_touching_catalogs(
    ctx: ServerContext, events: list[tuple[str, str, str]]
) -> None:
    before = await _catalog_rows(ctx)
    _, vid = await seed_version(ctx)
    await _through_inspect(ctx, vid, inspection())
    version = await load_version(ctx, vid)
    assert version.state == "describing" and version.problem is None
    assert version.inspection is not None and version.sbom is not None
    # write-once copy into the plugins bucket, same bytes
    key = plugin_key("default", version.plugin_id, vid)
    assert version.overlay_key == key and version.overlay_generation is not None
    assert await ctx.store.get(ctx.buckets.plugins, key) == WHEEL
    # the describe run mounts exactly this version
    (drun,) = await runs_of(ctx, RunKind.PLUGIN_DESCRIBE)
    assert drun.untrusted_code and drun.resource_class == "M"
    assert drun.timeout_s == ctx.settings.plugin_describe_timeout_s
    assert drun.overlays == [{"plugin_version_id": vid, "namespace": NS, "sha256": WHEEL_SHA}]
    assert drun.labels[LABEL_REASON] == "upload"
    assert drun.labels[LABEL_BASE_IMAGE] == ctx.catalogs.image_digest
    assert drun.payload["data_check_timeout_s"] == ctx.settings.plugin_data_check_timeout_s
    ex = UntrustedFakeExecutor()
    run, att = await to_finalizing(ctx, drun.id, ex)
    (execution,) = ex.executions.values()
    rs = await runspec_from_bootstrap(ctx, execution.handle.nonce)
    (mount,) = rs.overlays
    assert mount.pin.plugin_version_id == vid and mount.size == len(WHEEL)
    assert f"/{ctx.buckets.plugins}/" in str(mount.get.url) and rs.inputs == {}
    await write_outputs(ctx, run, att, {PLUGIN_DESCRIBE_OUTPUT: describe(vid)})
    assert await finalize(ctx, run, att) is RunState.SUCCEEDED
    version = await load_version(ctx, vid)
    assert version.state == "kit_passed" and version.scope == "private"
    async with ctx.db.session() as s:
        row = await s.get(m.PluginDescribe, (vid, ctx.catalogs.image_digest))
    assert row is not None and row.ok and row.run_id == drun.id
    assert [st["id"] for st in row.exports["steps"]] == [f"{NS}.winsorize"]
    assert set(row.plot_examples) == {f"{NS}.weekly_total"}
    assert row.describe_digest.startswith("sha256:")
    # G4: a plugin describe never reaches the platform-wide catalogs table
    assert await _catalog_rows(ctx) == before
    assert (vid, "plugin.version.kit_passed", "uploader") in events


async def test_asked_for_approval_at_upload_goes_on_to_the_reviewers(
    ctx: ServerContext, events: list[tuple[str, str, str]]
) -> None:
    _, vid = await seed_version(ctx, requested=True)
    await _through_inspect(ctx, vid, inspection())
    (drun,) = await runs_of(ctx, RunKind.PLUGIN_DESCRIBE)
    run, att = await to_finalizing(ctx, drun.id)
    await write_outputs(ctx, run, att, {PLUGIN_DESCRIBE_OUTPUT: describe(vid)})
    await finalize(ctx, run, att)
    assert (await load_version(ctx, vid)).state == "awaiting_approval"
    assert (vid, "plugin.version.awaiting_approval", "admins") in events


@pytest.mark.parametrize(
    ("body", "check"),
    [
        (
            inspection(
                problems=[
                    {"check": "pth_file", "path": "acme.pth", "message": "A .pth file runs code."}
                ]
            ),
            "pth_file",
        ),
        (inspection(sha="sha256:" + "0" * 64), "sha_mismatch"),
        (inspection(dist="mapi-plugin-mmm"), "dist_name"),
        (inspection(top_level=[NS, "json"]), "top_level_name"),
        (b"{not json", "json"),
        (b"[" * 5000 + b"]" * 5000, "depth"),
        (b'{"ok": true}', "schema"),
    ],
)
async def test_inspect_problems_refuse_the_version_with_plain_reasons(
    ctx: ServerContext, events: list[tuple[str, str, str]], body: bytes, check: str
) -> None:
    _, vid = await seed_version(ctx)
    await _through_inspect(ctx, vid, body)
    version = await load_version(ctx, vid)
    assert version.state == "inspect_failed" and version.purge_after is not None
    assert version.problem is not None and version.problem["code"] == "PLUGIN_REFUSED"
    checks = [e["context"]["check"] for e in version.problem["errors"]]
    assert check in checks
    assert all(e["context"]["stage"] == "inspect" for e in version.problem["errors"])
    assert version.overlay_key is None
    assert await runs_of(ctx, RunKind.PLUGIN_DESCRIBE) == []
    stored = ctx.store.snapshot()  # type: ignore[attr-defined]
    assert not [k for k in stored if k[0] == ctx.buckets.plugins]
    assert (vid, "plugin.version.kit_failed", "uploader") in events


async def test_a_tampered_overlay_copy_is_refused(ctx: ServerContext) -> None:
    """The copy is re-hashed: if the plugins-bucket object is not the uploaded bytes (here:
    a different object already sits at the write-once key) the version is refused."""
    pid, vid = await seed_version(ctx)
    await ctx.store.put(ctx.buckets.plugins, plugin_key("default", pid, vid), b"evil bytes")
    await _through_inspect(ctx, vid, inspection())
    version = await load_version(ctx, vid)
    assert version.state == "inspect_failed" and version.overlay_key is None
    assert [e["context"]["check"] for e in version.problem["errors"]] == ["sha_mismatch"]


async def _describe_result(ctx: ServerContext, vid: str, body: bytes) -> m.PluginVersion:
    await _through_inspect(ctx, vid, inspection())
    (drun,) = await runs_of(ctx, RunKind.PLUGIN_DESCRIBE)
    run, att = await to_finalizing(ctx, drun.id)
    await write_outputs(ctx, run, att, {PLUGIN_DESCRIBE_OUTPUT: body})
    assert await finalize(ctx, run, att) is RunState.SUCCEEDED  # never a 500 / run failure
    return await load_version(ctx, vid)


@pytest.mark.parametrize(
    "case", ["too_big", "too_deep", "not_json", "schema", "other_version", "kit_fail", "confusable"]
)
async def test_bad_describe_results_fail_the_kit_never_the_finalizer(
    ctx: ServerContext, events: list[tuple[str, str, str]], case: str
) -> None:
    _, vid = await seed_version(ctx)
    body = {
        "too_big": b'{"x": "' + b"a" * (DESCRIBE_MAX_BYTES + 10) + b'"}',
        "too_deep": b"[" * 5000 + b"]" * 5000,
        "not_json": b"\xff\xfe nope",
        "schema": b'{"plugin_version_id": "nope"}',
        "other_version": describe("plv_01jbz3q2x8m9v7k5h4g3f2e1d9"),
        "kit_fail": describe(vid, kit_ok=False),
        "confusable": describe(vid, title="Sampler  Convergence"),
    }[case]
    before = await _catalog_rows(ctx)
    version = await _describe_result(ctx, vid, body)
    assert version.state == "kit_failed" and version.problem is not None
    assert version.problem["code"] == "PLUGIN_REFUSED"
    assert await _catalog_rows(ctx) == before
    assert (vid, "plugin.version.kit_failed", "uploader") in events


async def test_unsafe_recipe_examples_are_dropped_and_block_kit_passed(
    ctx: ServerContext,
) -> None:
    from _v019_pipeline import UNSAFE_PLOT

    _, vid = await seed_version(ctx)
    unsafe = {**UNSAFE_PLOT, "id": f"{NS}.weekly_total"}
    body = describe(vid, examples={f"{NS}.weekly_total": unsafe})
    version = await _describe_result(ctx, vid, body)
    assert version.state == "kit_failed"
    async with ctx.db.session() as s:
        row = await s.get(m.PluginDescribe, (vid, ctx.catalogs.image_digest))
    assert row is not None and not row.ok and row.plot_examples == {}
    assert "example_unsafe" in {i["context"]["check"] for i in row.issues}


async def test_at_most_two_checks_per_tenant_are_dispatched(ctx: ServerContext) -> None:
    ids = [(await seed_version(ctx, version=f"0.1.{i}"))[1] for i in range(3)]
    for vid in ids:
        await _inspect(ctx, vid)
    ex = UntrustedFakeExecutor()
    await Dispatcher(ctx, ex).dispatch_once()
    assert len(ex.executions) == ctx.settings.plugin_pipeline_concurrency_per_tenant == 2
    queued = [r for r in await runs_of(ctx, RunKind.PLUGIN_INSPECT) if r.state == "queued"]
    assert len(queued) == 1
    await Dispatcher(ctx, ex).dispatch_once()  # still capped while two are in flight
    assert len(ex.executions) == 2


async def test_rekit_describe_keeps_the_state_until_its_result(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    async with ctx.db.session() as s:
        version = await s.get(m.PluginVersion, vid, with_for_update=True)
        assert version is not None
        run = await start_describe(s, ctx, version, created_by=UPLOADER, reason="rekit")
        assert version.state == "approved"
        s.expunge(run)
    assert run.labels[LABEL_REASON] == "rekit" and run.untrusted_code
