"""v0.19 WP6 (D9, D17, T19.26): the RunSpec mounts exactly the run's pins.

Each pin becomes one generation-pinned signed GET to the inspected copy in the plugins
bucket, resolved in the run's own tenant; a pin that does not resolve fails the run with
``PLUGIN_UNAVAILABLE`` (never a silent run without its code). Runs without pins keep a
RunSpec without the field.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from _v019_pipeline import (
    NS,
    WHEEL,
    WHEEL_SHA,
    UntrustedFakeExecutor,
    pinned_fit,
    queued_fit_once,
    seed_version,
)
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.runs import load_run
from mapi_server.services.runspec import (
    OVERLAY_URL_MARGIN,
    build_runspec,
    resolve_overlays,
    run_pins,
)
from mapi_server_testkit.builders import runspec_from_bootstrap
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import RunState
from sqlalchemy import update


async def _dispatched(ctx: ServerContext, run_id: str) -> tuple[m.Run, UntrustedFakeExecutor]:
    ex = UntrustedFakeExecutor()
    await Dispatcher(ctx, ex).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        s.expunge(run)
    return run, ex


async def test_a_pinned_run_mounts_its_pins_only(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    _, unused = await seed_version(ctx, state="approved", overlay=True, short="beta")
    run_id = await pinned_fit(ctx, vid)
    run, ex = await _dispatched(ctx, run_id)
    assert run.state == RunState.PROVISIONING.value
    (execution,) = ex.executions.values()
    rs = await runspec_from_bootstrap(ctx, execution.handle.nonce)
    (mount,) = rs.overlays
    assert mount.pin.plugin_version_id == vid and mount.pin.sha256 == WHEEL_SHA
    assert mount.pin.namespace == NS and mount.size == len(WHEEL)
    url = str(mount.get.url)
    assert f"/{ctx.buckets.plugins}/" in url and vid in url and unused not in url
    async with ctx.db.session() as s:
        version = await s.get(m.PluginVersion, vid)
    assert version is not None and mount.get.generation == version.overlay_generation
    # TTL: the run's timeout plus ten minutes (D17), not the 30-minute input margin
    ttl = mount.get.expires_at - rs.created_at
    assert ttl <= timedelta(seconds=run.timeout_s) + OVERLAY_URL_MARGIN + timedelta(seconds=5)
    # the "execution" can fetch exactly those bytes
    assert await ctx.store.get(ctx.buckets.plugins, version.overlay_key or "") == WHEEL


async def test_runs_without_pins_have_no_overlays_field(ctx: ServerContext) -> None:
    run_id = await queued_fit_once(ctx)
    _run, ex = await _dispatched(ctx, run_id)
    (execution,) = ex.executions.values()
    raw = await ctx.store.get(ctx.buckets.bootstrap, execution.handle.nonce)
    assert b'"overlays"' not in raw


async def test_a_pin_of_another_tenant_never_resolves(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    run_id = await pinned_fit(ctx, vid)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        s.expunge(run)
    run.tenant_id = "someone-else"
    async with ctx.db.session() as s:
        with pytest.raises(MapiError) as err:
            await resolve_overlays(s, run, plugins_bucket=ctx.buckets.plugins)
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE


@pytest.mark.parametrize("case", ["sha", "namespace", "no_copy", "purged"])
async def test_a_pin_that_does_not_match_the_version_fails_the_run(
    ctx: ServerContext, case: str
) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    run_id = await pinned_fit(ctx, vid)
    async with ctx.db.session() as s:
        if case == "sha":
            # the gate refuses it first (PLUGIN_UNAVAILABLE): same outcome, earlier
            await s.execute(
                update(m.Run)
                .where(m.Run.id == run_id)
                .values(
                    overlays=[
                        {"plugin_version_id": vid, "namespace": NS, "sha256": "sha256:" + "0" * 64}
                    ]
                )
            )
        elif case == "namespace":
            await s.execute(
                update(m.Run)
                .where(m.Run.id == run_id)
                .values(
                    overlays=[
                        {
                            "plugin_version_id": vid,
                            "namespace": "t_abcdef_beta",
                            "sha256": WHEEL_SHA,
                        }
                    ]
                )
            )
        elif case == "no_copy":
            await s.execute(
                update(m.PluginVersion)
                .where(m.PluginVersion.id == vid)
                .values(overlay_key=None, overlay_generation=None)
            )
    if case == "purged":
        async with ctx.db.session() as s:
            run = await load_run(s, None, run_id)
            s.expunge(run)
        async with ctx.db.session() as s:
            await s.execute(
                update(m.PluginVersion)
                .where(m.PluginVersion.id == vid)
                .values(overlay_key=None, overlay_generation=None)
            )
            with pytest.raises(MapiError) as err:
                await resolve_overlays(s, run, plugins_bucket=ctx.buckets.plugins)
        assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE
        return
    run, ex = await _dispatched(ctx, run_id)
    assert run.state == RunState.FAILED.value and not ex.executions
    assert run.problem is not None and run.problem["code"] == "PLUGIN_UNAVAILABLE"


async def test_build_runspec_refuses_pins_without_resolved_objects(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    run_id = await pinned_fit(ctx, vid)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        s.expunge(run)
    assert [p.plugin_version_id for p in run_pins(run)] == [vid]
    attempt = m.RunAttempt(
        run_id=run.id, attempt=1, nonce="0" * 64, hmac_key="k" * 64, deadline=run.created_at
    )
    with pytest.raises(MapiError) as err:
        await build_runspec(ctx.store, run=run, attempt=attempt, runs_bucket=ctx.buckets.runs)
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE


def test_malformed_pins_fail_closed() -> None:
    run = m.Run(overlays=[{"plugin_version_id": "nope"}])
    with pytest.raises(MapiError) as err:
        run_pins(run)
    assert err.value.code is ErrorCode.INTEGRITY_FAILURE
