"""A restarted orchestrator re-attaches to executions that are still running (plan §8.3)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.executors import LocalProcessExecutor
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.reconciler import Reconciler
from mapi_server.services.runs import load_run
from mapi_server_testkit.builders import queued_fit
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.executor import ExecState, Handle
from mapi_spec.run import RunState

TEMPLATE = "http://127.0.0.1:9/storage/v1/b/mapi-local-bootstrap/o/{nonce}"


def _sleeper(tmp_path: Path) -> list[str]:
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(30)\n")
    return [sys.executable, str(script)]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_restarted_reconciler_adopts_a_live_local_process(
    ctx: ServerContext, tmp_path: Path
) -> None:
    cmd = _sleeper(tmp_path)
    first = LocalProcessExecutor(
        command=cmd, bootstrap_url_template=TEMPLATE, workdir=tmp_path / "w", cancel_grace_s=1
    )
    second: LocalProcessExecutor | None = None
    try:
        run_id = await queued_fit(ctx)
        await Dispatcher(ctx, first).dispatch_once()
        async with ctx.db.session() as s:
            att = await s.get(m.RunAttempt, (run_id, 1))
            assert att is not None and att.handle is not None
            handle = Handle.model_validate(att.handle)
        pid = int(str(handle.external_id).removeprefix("pid:"))
        # the orchestrator restarts: a fresh executor over the same workdir
        second = LocalProcessExecutor(
            command=cmd, bootstrap_url_template=TEMPLATE, workdir=tmp_path / "w", cancel_grace_s=1
        )
        reconciler = Reconciler(ctx, second)
        for _ in range(2):
            await reconciler.reconcile_run(run_id)
        async with ctx.db.session() as s:
            run = await load_run(s, None, run_id)
        assert run.state == RunState.PROVISIONING.value and run.attempt == 1, run.state
        assert _pid_alive(pid), "the old supervisor must not be orphaned by a re-run"
        assert (await second.status(handle)).state is ExecState.RUNNING
        # and the adopted execution can still be cancelled through the new executor
        await second.cancel(handle)
        for _ in range(100):
            if (await second.status(handle)).state is not ExecState.RUNNING:
                break
            await asyncio.sleep(0.05)
        assert (await second.status(handle)).state is not ExecState.RUNNING
    finally:
        await first.aclose()
        if second is not None:
            await second.aclose()


async def test_reconciler_reattaches_each_attempt_once_per_process(ctx: ServerContext) -> None:
    executor = FakeExecutor()
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, executor).dispatch_once()
    reconciler = Reconciler(ctx, executor)
    before = executor.adopt_calls
    for _ in range(3):
        await reconciler.reconcile_run(run_id)
    assert executor.adopt_calls == before + 1  # re-attach (log follower, pid) only once


async def test_lost_execution_is_retried_only_once_it_is_gone(ctx: ServerContext) -> None:
    executor = FakeExecutor()
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, executor).dispatch_once()
    ex = executor.only()
    running = ex.status
    # a transient UNKNOWN from the status API; after the cancel request it is seen running
    ex.scripted = [ex.status.model_copy(update={"state": ExecState.UNKNOWN}), running]
    ex.refuse_cancel = True
    reconciler = Reconciler(ctx, executor)
    await reconciler.reconcile_run(run_id)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
    assert run.state == RunState.PROVISIONING.value and run.attempt == 1
    assert ex.cancelled == 1  # it was asked to stop before any new attempt
    # gone for real: cancelled, then a new attempt
    ex.refuse_cancel = False
    ex.status = ex.status.model_copy(update={"state": ExecState.UNKNOWN})
    await reconciler.reconcile_run(run_id)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
    assert run.state == RunState.QUEUED.value and run.attempt == 2
