"""v0.19 WP6 (D6, D9, D15): where uploaded code may run, checked again at every lease.

* executors report ``runs_untrusted_code`` (docker, docker-gvisor, Cloud Run; local-process
  only with the development allowance, then also ``untrusted_dev_only``);
* the dispatcher fails an untrusted run on an executor that may not run uploaded code
  (``UNTRUSTED_PLACEMENT_UNAVAILABLE``), and in the multi-tenant profile on anything weaker
  than gVisor with enforced egress and DNS;
* the overlay pins are re-checked at lease: a version yanked (or made private) between
  admission and dispatch fails the run with ``PLUGIN_UNAVAILABLE``;
* the kill switch still only holds untrusted runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from _v019_pipeline import (
    NS,
    TEAMMATE,
    UPLOADER,
    WHEEL_SHA,
    UntrustedFakeExecutor,
    load_version,
    pinned_fit,
    queued_fit_once,
    seed_version,
)
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.executors import CloudRunJobsExecutor, DockerExecutor, LocalProcessExecutor
from mapi_server.executors.cloudrun import JobInventory
from mapi_server.executors.fake_cloudrun import FakeCloudRun
from mapi_server.services.dispatcher import DEV_MODE_ISOLATION, Dispatcher
from mapi_server.services.runs import load_run
from mapi_server.settings import Profile
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.executor import Isolation
from mapi_spec.run import RunState
from sqlalchemy import select, update

_pinned_fit = pinned_fit


async def _run(ctx: ServerContext, run_id: str) -> m.Run:
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        s.expunge(run)
        return run


# ------------------------------------------------------------------------------ executor caps


def test_local_process_runs_uploaded_code_only_with_the_dev_allowance(tmp_path: Path) -> None:
    kw = {
        "command": ["true"],
        "bootstrap_url_template": "http://127.0.0.1:14443/b/{nonce}",
        "workdir": tmp_path,
    }
    caps = LocalProcessExecutor(**kw).caps()  # type: ignore[arg-type]
    assert not caps.runs_untrusted_code and not caps.untrusted_dev_only
    dev = LocalProcessExecutor(**kw, allow_untrusted=True).caps()  # type: ignore[arg-type]
    assert dev.runs_untrusted_code and dev.untrusted_dev_only
    assert dev.isolation is Isolation.PROCESS


def test_docker_and_cloud_run_may_run_uploaded_code() -> None:
    docker = DockerExecutor(
        image="mapi/base-bayes:0.16.0", bootstrap_url_template="http://shim/b/{nonce}"
    )
    assert docker.caps().runs_untrusted_code and not docker.caps().untrusted_dev_only
    inv = JobInventory(project="mapi-test", region="europe-west1", env="prod")
    cloud = CloudRunJobsExecutor(FakeCloudRun.for_inventory(inv), inv).caps()
    assert cloud.runs_untrusted_code and not cloud.untrusted_dev_only


# ---------------------------------------------------------------------------- dispatch checks


async def test_an_executor_without_untrusted_support_fails_the_run(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    run_id = await _pinned_fit(ctx, vid)
    ex = FakeExecutor()
    if ex.caps().runs_untrusted_code:  # pragma: no cover - a test kit that allows it
        ex = UntrustedFakeExecutor(runs_untrusted=False)
    await Dispatcher(ctx, ex).dispatch_once()
    run = await _run(ctx, run_id)
    assert run.state == RunState.FAILED.value and not ex.executions
    assert run.problem is not None and run.problem["code"] == "UNTRUSTED_PLACEMENT_UNAVAILABLE"


async def test_multi_tenant_needs_gvisor_with_egress_and_dns(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    monkeypatch.setattr(ctx.settings, "profile", Profile.MULTI_TENANT)
    weak = await _pinned_fit(ctx, vid)
    await Dispatcher(ctx, UntrustedFakeExecutor()).dispatch_once()  # docker-like: PROCESS
    run = await _run(ctx, weak)
    assert run.problem is not None and run.problem["code"] == "UNTRUSTED_PLACEMENT_UNAVAILABLE"
    strong = await _pinned_fit(ctx, vid)
    ex = UntrustedFakeExecutor(isolation=Isolation.GVISOR, egress=True)
    await Dispatcher(ctx, ex).dispatch_once()
    assert (await _run(ctx, strong)).state == RunState.PROVISIONING.value


async def test_a_version_yanked_after_admission_fails_the_run_at_lease(
    ctx: ServerContext,
) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    run_id = await _pinned_fit(ctx, vid)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.PluginVersion)
            .where(m.PluginVersion.id == vid)
            .values(
                state="yanked",
                scope="none",
                yanked_at=datetime.now(UTC),
                yank_reason="security",
            )
        )
    ex = UntrustedFakeExecutor()
    await Dispatcher(ctx, ex).dispatch_once()
    run = await _run(ctx, run_id)
    assert run.state == RunState.FAILED.value and not ex.executions
    assert run.problem is not None and run.problem["code"] == "PLUGIN_UNAVAILABLE"
    assert "withdrawn" in run.problem["detail"]


async def test_a_private_version_runs_only_for_its_uploader(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="kit_passed", overlay=True)
    mine = await _pinned_fit(ctx, vid, created_by=UPLOADER)
    theirs = await _pinned_fit(ctx, vid, created_by=TEAMMATE)
    ex = UntrustedFakeExecutor()
    await Dispatcher(ctx, ex).dispatch_once()
    assert (await _run(ctx, mine)).state == RunState.PROVISIONING.value
    other = await _run(ctx, theirs)
    assert other.state == RunState.FAILED.value
    assert other.problem is not None and other.problem["code"] == "PLUGIN_UNAVAILABLE"


async def test_the_kill_switch_holds_untrusted_runs(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    run_id = await _pinned_fit(ctx, vid)
    r = await client.put(
        "/v1/admin/kill-switches", json={"untrusted_execution_enabled": False, "reason": "drill"}
    )
    assert r.status_code == 200
    # web+cli+docs-3: the pause is visible (the web and the CLI say "Paused by the platform")
    assert (await client.get("/v1/me")).json()["plugins_paused"] is True
    ex = UntrustedFakeExecutor()
    await Dispatcher(ctx, ex).dispatch_once()
    assert (await _run(ctx, run_id)).state == RunState.QUEUED.value and not ex.executions
    await client.put(
        "/v1/admin/kill-switches", json={"untrusted_execution_enabled": True, "reason": "drill"}
    )
    assert (await client.get("/v1/me")).json()["plugins_paused"] is False
    await Dispatcher(ctx, ex).dispatch_once()
    assert (await _run(ctx, run_id)).state == RunState.PROVISIONING.value


async def test_dev_only_executors_mark_every_untrusted_run(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    run_id = await _pinned_fit(ctx, vid)
    trusted = await queued_fit_once(ctx)
    await Dispatcher(ctx, UntrustedFakeExecutor(dev_only=True)).dispatch_once()
    async with ctx.db.session() as s:
        events = list(
            (await s.execute(select(m.RunEvent).where(m.RunEvent.type == "warning"))).scalars()
        )
    assert [(e.run_id, e.payload["code"]) for e in events] == [(run_id, DEV_MODE_ISOLATION)]
    assert "without a sandbox" in events[0].payload["message"]
    assert trusted != run_id


async def test_a_describe_run_may_mount_the_version_it_checks(ctx: ServerContext) -> None:
    """``describing`` is not mountable for anyone else; the describe run of that very
    version is the exception (and only for its own pin)."""
    _, vid = await seed_version(ctx, state="describing", overlay=True)
    from mapi_server.services.plugins.pipeline import describe_pin_problem
    from mapi_spec.plugins import OverlayPin

    pin = OverlayPin(plugin_version_id=vid, namespace=NS, sha256=WHEEL_SHA)
    async with ctx.db.session() as s:
        run = m.Run(tenant_id="default", payload={"plugin_version_id": vid}, created_by=UPLOADER)
        assert await describe_pin_problem(s, run, [pin]) is None
        other = m.Run(tenant_id="default", payload={"plugin_version_id": "plv_x"})
        assert await describe_pin_problem(s, other, [pin]) is not None
        foreign = m.Run(tenant_id="acme", payload={"plugin_version_id": vid}, created_by=UPLOADER)
        assert await describe_pin_problem(s, foreign, [pin]) is not None
        wrong = OverlayPin(plugin_version_id=vid, namespace=NS, sha256="sha256:" + "0" * 64)
        assert await describe_pin_problem(s, run, [wrong]) is not None
    assert (await load_version(ctx, vid)).state == "describing"


async def test_a_describe_run_mounts_its_version_only_for_the_uploader(
    ctx: ServerContext,
) -> None:
    """trust-1: a describe run that acts for anyone but the uploader (a teammate's retry of
    a rekit run) may not mount the version, approved or not."""
    from mapi_server.services.plugins.pipeline import describe_pin_problem
    from mapi_spec.plugins import OverlayPin

    for i, state in enumerate(("describing", "approved", "kit_passed")):
        _, vid = await seed_version(ctx, state=state, overlay=True, version=f"0.{i + 1}.0")
        pin = OverlayPin(plugin_version_id=vid, namespace=NS, sha256=WHEEL_SHA)
        async with ctx.db.session() as s:
            theirs = m.Run(
                tenant_id="default", payload={"plugin_version_id": vid}, created_by=TEAMMATE
            )
            assert await describe_pin_problem(s, theirs, [pin]) is not None
            nobody = m.Run(tenant_id="default", payload={"plugin_version_id": vid})
            assert await describe_pin_problem(s, nobody, [pin]) is not None
            assert await describe_pin_problem(s, theirs, [pin], created_by=UPLOADER) is None


@pytest.mark.parametrize("kind", ["plugin_inspect", "plugin_describe"])
async def test_plugin_check_runs_are_not_retried_one_by_one(ctx: ServerContext, kind: str) -> None:
    """trust-1: a stand-alone retry of a plugin check run is refused (409), so nobody can
    re-run (or, on a rekit, yank) someone else's upload."""
    from mapi_server.errors import Conflict
    from mapi_server.services.submit import _refuse_orchestrated_retry

    old = m.Run(tenant_id="default", kind=kind, state=RunState.FAILED.value, created_by=UPLOADER)
    async with ctx.db.session() as s:
        with pytest.raises(Conflict) as err:
            await _refuse_orchestrated_retry(s, old)
    assert "upload the plugin again" in err.value.detail
