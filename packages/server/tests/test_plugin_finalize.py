"""v0.19 WP6 (D8): a version never stays ``inspecting``/``describing`` once its check run is
over, whatever way the run ended (one test per terminal state and per run kind)."""

from __future__ import annotations

from typing import Any

import pytest
from _v019_pipeline import (
    UPLOADER,
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
from mapi_server.services.plugins.pipeline import start_describe, start_inspect
from mapi_server.services.plugins.sweeps import PluginSweeps
from mapi_server.services.runs import apply_transition, load_run, now
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.executor import RunKind
from mapi_spec.jobs import PLUGIN_DESCRIBE_OUTPUT, PLUGIN_INSPECTION_OUTPUT
from mapi_spec.run import Actor, RunState, RunTrigger


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def record(session: Any, ctx: Any, version: Any, event: str, **_: Any) -> None:
        seen.append(event)

    monkeypatch.setattr(plugin_finalize, "notify_plugin_event", record)
    return seen


async def _checking(ctx: ServerContext, kind: RunKind) -> tuple[str, str]:
    """A version in ``inspecting`` (or ``describing``) with its queued check run."""
    if kind is RunKind.PLUGIN_INSPECT:
        _, vid = await seed_version(ctx)
    else:
        _, vid = await seed_version(ctx, state="inspecting", overlay=True)
    async with ctx.db.session() as s:
        version = await s.get(m.PluginVersion, vid, with_for_update=True)
        assert version is not None
        if kind is RunKind.PLUGIN_INSPECT:
            run = await start_inspect(s, ctx, version, created_by=UPLOADER)
        else:
            run = await start_describe(s, ctx, version, created_by=UPLOADER, reason="upload")
        return vid, run.id


async def _end_by_reconciler(ctx: ServerContext, run_id: str, trigger: RunTrigger) -> None:
    await Dispatcher(ctx, UntrustedFakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        if trigger is RunTrigger.CANCEL:
            await apply_transition(s, run, RunTrigger.CANCEL, Actor.API)
            await apply_transition(s, run, RunTrigger.CANCEL_CONFIRMED, Actor.RECONCILER)
        else:
            await apply_transition(s, run, trigger, Actor.RECONCILER)


KINDS = [RunKind.PLUGIN_INSPECT, RunKind.PLUGIN_DESCRIBE]
FAILED_STATE = {RunKind.PLUGIN_INSPECT: "inspect_failed", RunKind.PLUGIN_DESCRIBE: "kit_failed"}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("trigger", "code"),
    [
        (RunTrigger.FAIL_USER, "PLUGIN_REFUSED"),
        (RunTrigger.FAIL_TIMEOUT, "TIMEOUT"),
        (RunTrigger.FAIL_SANDBOX, "PLUGIN_REFUSED"),
        (RunTrigger.FAIL_RESOURCE_EXHAUSTED, "PLUGIN_REFUSED"),
        (RunTrigger.CANCEL, "CANCELLED"),
    ],
)
async def test_every_execution_end_ends_the_check(
    ctx: ServerContext, kind: RunKind, trigger: RunTrigger, code: str
) -> None:
    vid, run_id = await _checking(ctx, kind)
    await _end_by_reconciler(ctx, run_id, trigger)
    # the dispatcher's report cadence (and the housekeeper) end the check
    assert await PluginSweeps(ctx).reconcile(now()) == 1
    version = await load_version(ctx, vid)
    assert version.state == FAILED_STATE[kind]
    assert version.problem is not None and version.problem["code"] == code
    assert version.purge_after is not None
    assert await PluginSweeps(ctx).reconcile(now()) == 0  # once only


@pytest.mark.parametrize("kind", KINDS)
async def test_an_integrity_failure_ends_the_check(ctx: ServerContext, kind: RunKind) -> None:
    vid, run_id = await _checking(ctx, kind)
    run, att = await to_finalizing(ctx, run_id)
    name = PLUGIN_INSPECTION_OUTPUT if kind is RunKind.PLUGIN_INSPECT else PLUGIN_DESCRIBE_OUTPUT
    body = inspection() if kind is RunKind.PLUGIN_INSPECT else describe(vid)
    # a forged manifest: the overlay hashes do not match the pins (or claim one for inspect)
    await write_outputs(ctx, run, att, {name: body}, overlay_hashes=["sha256:" + "9" * 64])
    assert await finalize(ctx, run, att) is RunState.FAILED
    version = await load_version(ctx, vid)
    assert version.state == FAILED_STATE[kind]
    assert version.problem is not None and version.problem["code"] == "INTEGRITY_FAILURE"


@pytest.mark.parametrize("kind", KINDS)
async def test_a_refusal_at_dispatch_ends_the_check(ctx: ServerContext, kind: RunKind) -> None:
    vid, run_id = await _checking(ctx, kind)
    ex = FakeExecutor()
    if ex.caps().runs_untrusted_code:  # pragma: no cover - a kit that allows it
        ex = UntrustedFakeExecutor(runs_untrusted=False)
    await Dispatcher(ctx, ex).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
    assert run.state == RunState.FAILED.value
    version = await load_version(ctx, vid)
    assert version.state == FAILED_STATE[kind]
    assert version.problem is not None
    assert version.problem["code"] == "UNTRUSTED_PLACEMENT_UNAVAILABLE"


async def test_a_lost_check_run_ends_the_check_after_a_while(ctx: ServerContext) -> None:
    from datetime import timedelta

    _, vid = await seed_version(ctx, state="inspecting")
    sweeps = PluginSweeps(ctx)
    assert await sweeps.reconcile(now()) == 0  # not stale yet
    later = now() + timedelta(seconds=3 * ctx.settings.plugin_describe_timeout_s)
    assert await sweeps.reconcile(later) == 1
    version = await load_version(ctx, vid)
    assert version.state == "inspect_failed" and version.problem["code"] == "INFRA_ERROR"


async def test_a_held_check_stays_checking(ctx: ServerContext) -> None:
    """A run merely held (kill switch, concurrency cap) is not over: the version waits."""
    vid, _ = await _checking(ctx, RunKind.PLUGIN_INSPECT)
    assert await PluginSweeps(ctx).reconcile(now()) == 0
    assert (await load_version(ctx, vid)).state == "inspecting"
    assert [r.state for r in await runs_of(ctx, RunKind.PLUGIN_INSPECT)] == ["queued"]


async def test_a_cancelled_check_asks_for_a_new_version_number(ctx: ServerContext) -> None:
    """review-flow-1: a version number is never uploaded twice, so a check the platform
    cancelled (or lost) asks for a new version number, not for the same file again."""
    from mapi_server.services.plugins.store import _version_exists

    vid, run_id = await _checking(ctx, RunKind.PLUGIN_INSPECT)
    await _end_by_reconciler(ctx, run_id, RunTrigger.CANCEL)
    assert await PluginSweeps(ctx).reconcile(now()) == 1
    version = await load_version(ctx, vid)
    assert version.state == "inspect_failed" and version.problem is not None
    assert "Raise the version number" in version.problem["detail"]
    assert "same file" not in version.problem["detail"]
    async with ctx.db.session() as s:  # pushing the same wheel again is refused (409)
        assert await _version_exists(s, version.plugin_id, version.version)
