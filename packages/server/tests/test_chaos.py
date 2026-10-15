"""Chaos tests (plan §13): the control plane converges to correct terminal states despite
worker death (with and without retries), event redelivery/reordering, a Postgres restart
under the live loops, ``jobs.run`` throttling storms and a submit that hangs after the
execution was created — without loss or duplicates.

Marked ``slow`` (and ``integration``): they spawn real subprocesses and, for the Postgres
case, a dedicated embedded server they stop and start. They exercise the invariants the plan
relies on: the dispatcher's intent row + ``adopt`` by nonce means one attempt never spawns two
executions; the events path dedupes on ``(run, attempt, seq)`` so redelivery and reordering
are safe; and the reconciler is level-triggered and idempotent so it reconverges after an
outage.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import libpq_url
from mapi_server.db import models as m
from mapi_server.db.migrate import upgrade
from mapi_server.executors import LocalProcessExecutor
from mapi_server.executors.cloudrun import CloudRunJobsExecutor
from mapi_server.orchestrator import Orchestrator
from mapi_server.services.events import EventIngestor
from mapi_server.services.runs import load_run
from mapi_server_testkit.builders import CATALOG, EXEC_TOKEN, queued_fit, signed_event
from mapi_server_testkit.conformance import INVENTORY, harness
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.store_http import serve_store
from mapi_spec.run import (
    CompletePayload,
    ProgressPayload,
    RunnerPhase,
    RunState,
    StatePayload,
)
from sqlalchemy import func, select

pytestmark = [pytest.mark.slow, pytest.mark.integration]

FAKE_RUNNER = Path(__file__).resolve().parent / "mapi_server_testkit" / "fake_runner.py"


async def _until(check: Callable[[], Awaitable[bool]], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached in time")


@contextlib.asynccontextmanager
async def _orchestrator(ctx: ServerContext, mode: str) -> AsyncIterator[LocalProcessExecutor]:
    """A running orchestrator driving the stand-in supervisor in ``mode``."""
    ctx.settings.complete_event_grace_s = 3
    async with serve_store(
        ctx.store,  # type: ignore[arg-type]
        exec_token=EXEC_TOKEN,
        bootstrap_bucket=ctx.buckets.bootstrap,
    ) as url:
        executor = LocalProcessExecutor(
            command=[sys.executable, str(FAKE_RUNNER), mode],
            bootstrap_url_template=f"{url}/storage/v1/b/{ctx.buckets.bootstrap}/o/{{nonce}}",
            workdir=ctx.settings.workdir,
            exec_token=EXEC_TOKEN,
            extra_env={"FAKE_RUNNER_CATALOG": str(CATALOG)},
            cancel_grace_s=2,
        )
        orch = Orchestrator(ctx, executor, use_procrastinate=False)
        task = asyncio.create_task(orch.run())
        try:
            yield executor
        finally:
            orch.stop.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, 20)


async def _supervisor_pid(ctx: ServerContext, run_id: str) -> int:
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None and att.external_id is not None
    return int(str(att.external_id).removeprefix("pid:"))


def _wait_dead(pid: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} did not exit")


async def _terminal(ctx: ServerContext, run_id: str, timeout: float = 40) -> m.Run:
    out: dict[str, m.Run] = {}

    async def done() -> bool:
        async with ctx.db.session() as s:
            run = await load_run(s, None, run_id)
            s.expunge(run)
        out["run"] = run
        return RunState(run.state).terminal

    await _until(done, timeout)
    return out["run"]


async def _one_execution(ctx: ServerContext, run_id: str) -> None:
    """No duplicate executions were created for this run (one attempt row per attempt)."""
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        attempts = (
            (await s.execute(select(m.RunAttempt).where(m.RunAttempt.run_id == run_id)))
            .scalars()
            .all()
        )
        external_ids = [a.external_id for a in attempts if a.external_id is not None]
        # each attempt leased exactly one nonce; no two attempts share an external id
        assert len(external_ids) == len(set(external_ids)), external_ids
        assert len(attempts) == run.attempt


# --------------------------------------------------------------------------- worker death


async def test_worker_killed_during_provisioning_converges(ctx: ServerContext) -> None:
    """SIGKILL before any event (PROVISIONING) → a terminal failure, no orphan, no duplicate."""
    ctx.settings.max_infra_retries = 0
    ctx.settings.lost_execution_grace_s = 0.0
    run_id = await queued_fit(ctx)
    async with _orchestrator(ctx, "silent_hang"):

        async def provisioning() -> bool:
            async with ctx.db.session() as s:
                run = await load_run(s, None, run_id)
            return run.state == RunState.PROVISIONING.value

        await _until(provisioning)
        pid = await _supervisor_pid(ctx, run_id)
        os.kill(pid, signal.SIGKILL)
        _wait_dead(pid)
        run = await _terminal(ctx, run_id)
    assert run.state == RunState.FAILED.value, run.state
    await _one_execution(ctx, run_id)
    # no bundle/model registered from a killed run
    async with ctx.db.session() as s:
        models = (await s.execute(select(func.count()).select_from(m.ModelVersion))).scalar_one()
    assert models == 0


async def test_worker_killed_during_running_converges(ctx: ServerContext) -> None:
    """SIGKILL after the run is RUNNING → the reconciler classifies the dead execution and the
    run reaches a terminal state exactly once (no stuck RUNNING, no duplicate attempt row)."""
    ctx.settings.max_infra_retries = 0
    ctx.settings.lost_execution_grace_s = 0.0
    run_id = await queued_fit(ctx)
    async with _orchestrator(ctx, "hang"):

        async def running() -> bool:
            async with ctx.db.session() as s:
                run = await load_run(s, None, run_id)
            return run.state in (RunState.RUNNING.value, RunState.STALLED.value)

        await _until(running)
        pid = await _supervisor_pid(ctx, run_id)
        os.kill(pid, signal.SIGKILL)
        _wait_dead(pid)
        run = await _terminal(ctx, run_id)
    assert run.state == RunState.FAILED.value, run.state
    assert run.finished_at is not None
    await _one_execution(ctx, run_id)


# --------------------------------------------------------- Pub/Sub redelivery & reordering


async def test_event_redelivery_and_reordering(ctx: ServerContext) -> None:
    """Out-of-order, duplicated event delivery converges without loss or duplicate rows."""
    executor = FakeExecutor()
    run_id = await queued_fit(ctx)
    from mapi_server.services.dispatcher import Dispatcher

    await Dispatcher(ctx, executor).dispatch_once()
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        s.expunge(att)

    ingestor = EventIngestor(ctx)
    events = [
        signed_event(att, 0, StatePayload(phase=RunnerPhase.BOOTSTRAP)),
        signed_event(att, 1, ProgressPayload(phase=RunnerPhase.FIT, fraction=0.3)),
        signed_event(att, 2, ProgressPayload(phase=RunnerPhase.FIT, fraction=0.7)),
    ]
    # reordered and duplicated: the newest progress (seq 2) arrives before seq 1, and events
    # are redelivered. Progress coalescing keeps only the newest sample, so the stale seq-1
    # progress that arrives after seq 2 is (correctly) dropped as a duplicate — no rollback.
    delivery = [events[2], events[0], events[1], events[0], events[2], events[1]]
    results = [await ingestor.ingest(e) for e in delivery]
    accepted = sum(r.value == "accepted" for r in results)
    duplicates = sum(r.value == "duplicate" for r in results)
    # accepted: the seq-2 progress (newest) and the seq-0 state event. Everything else — the
    # redeliveries and the out-of-order older seq-1 progress — is deduped.
    assert accepted == 2 and duplicates == 4, results

    # the first accepted event moved the run to RUNNING; progress holds the newest seq (2)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        progress = await s.get(m.RunProgress, run_id)
        # exactly one persisted runner event per seq (no duplicates)
        rows = (
            (
                await s.execute(
                    select(m.RunEvent.seq).where(
                        m.RunEvent.run_id == run_id, m.RunEvent.source == "runner"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert run.state == RunState.RUNNING.value
    assert progress is not None and progress.seq == 2 and progress.fraction == pytest.approx(0.7)
    # only state events are appended to run_events (progress upserts a single row)
    assert rows == [0]

    # a late duplicate of an old progress event must not roll the progress back
    assert (await ingestor.ingest(events[1])).value == "duplicate"
    async with ctx.db.session() as s:
        progress = await s.get(m.RunProgress, run_id)
    assert progress is not None and progress.seq == 2

    # the complete event arrives (possibly redelivered) → recorded exactly once
    complete = signed_event(
        att,
        3,
        CompletePayload(
            status="complete",
            manifest_name="manifest.json",
            manifest_crc32c="AAAAAA==",
            objects=1,
            total_bytes=10,
        ),
    )
    assert (await ingestor.ingest(complete)).value == "accepted"
    assert (await ingestor.ingest(complete)).value == "duplicate"
    async with ctx.db.session() as s:
        att2 = await s.get(m.RunAttempt, (run_id, 1))
        assert att2 is not None and att2.complete_payload is not None


async def test_bad_hmac_redelivery_never_advances_the_run(ctx: ServerContext) -> None:
    """A forged/replayed event with a bad HMAC is dropped every time (counted, never applied)."""
    from mapi_spec.run import new_hmac_key

    executor = FakeExecutor()
    run_id = await queued_fit(ctx)
    from mapi_server.services.dispatcher import Dispatcher

    await Dispatcher(ctx, executor).dispatch_once()
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        s.expunge(att)
    forged = signed_event(att, 0, StatePayload(phase=RunnerPhase.FIT), key=new_hmac_key())
    ingestor = EventIngestor(ctx)
    for _ in range(3):
        assert (await ingestor.ingest(forged)).value == "bad_hmac"
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        att2 = await s.get(m.RunAttempt, (run_id, 1))
    assert run.state == RunState.PROVISIONING.value  # never advanced to RUNNING
    assert att2 is not None and att2.violations == 3


# ----------------------------------------------------------------- Postgres restart mid-run


async def test_postgres_restart_mid_run_converges(tmp_path: Path) -> None:
    """Stop and start the database under a live run while the orchestrator's loops keep
    running on the same pools (no engine swap, no hand-driven reconciler): the loops ride out
    the outage, the pools reconnect, and the run converges with no duplicate attempt."""
    pgserver = pytest.importorskip("pgserver")
    from mapi_server.app import build_context, shutdown, startup
    from mapi_server.services.signing import BundleSigner
    from mapi_server.settings import ObjectStoreKind, Settings
    from mapi_server.storage import MemoryObjectStore

    pgdata = tmp_path / "pg"
    srv = pgserver.get_server(str(pgdata), cleanup_mode="stop")
    settings = Settings(
        database_url=srv.get_uri(),
        object_store=ObjectStoreKind.MEMORY,
        data_dir=tmp_path,
        image_digest="sha256:" + "ab" * 32,
        catalog_path=CATALOG,
        gcs_endpoint="http://store.invalid",
        exec_token=EXEC_TOKEN,
        reconcile_interval_s=0.05,
        dispatch_poll_s=0.05,
        lost_execution_grace_s=0.0,
        max_infra_retries=0,
    )
    upgrade(settings.database_url)
    store = MemoryObjectStore(
        base_url=settings.gcs_endpoint, buckets=settings.buckets.all().values()
    )
    ctx = build_context(settings, store=store, signer=BundleSigner.generate())
    await startup(ctx, listen=False)
    executor = FakeExecutor()

    # record finalize hand-offs instead of running the finalizer (no real outputs here): the
    # convergence we assert is that the live reconciler moves the run past RUNNING.
    class _RecordingQueue:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def defer_finalize(self, run_id: str, attempt: int) -> None:
            self.calls.append((run_id, attempt))

    queue = _RecordingQueue()
    ctx.queue = queue
    orch = Orchestrator(ctx, executor, use_procrastinate=False)
    loops = asyncio.create_task(orch.run())

    async def state(run_id: str) -> str:
        async with ctx.db.session() as s:
            return (await load_run(s, None, run_id)).state

    async def ingest(event: Any) -> str:
        """Pushes are redelivered until they succeed (Pub/Sub retries a failed push)."""
        for _ in range(100):
            with contextlib.suppress(Exception):
                return (await orch.ingestor.ingest(event)).value
            await asyncio.sleep(0.1)
        raise AssertionError("the event was never accepted")

    try:
        run_id = await queued_fit(ctx)

        async def dispatched() -> bool:
            return await state(run_id) == RunState.PROVISIONING.value

        await _until(dispatched)
        async with ctx.db.session() as s:
            att = await s.get(m.RunAttempt, (run_id, 1))
            assert att is not None
            s.expunge(att)
        assert await ingest(signed_event(att, 0, StatePayload(phase=RunnerPhase.FIT))) == (
            "accepted"
        )

        # --- chaos: stop Postgres under the running loops, let them fail, start it again ---
        import pgserver.postgres_server as ps

        ps.pg_ctl(["-m", "fast", "stop"], pgdata=pgdata)
        _wait_pg_down(settings.database_url)
        await asyncio.sleep(1.0)  # the dispatcher and reconciler passes fail meanwhile
        assert not loops.done(), "the orchestrator must survive a database outage"
        # the execution finishes while the database is down
        executor.finish(att.nonce, exit_code=0)
        srv.ensure_postgres_running()
        _wait_pg_up(settings.database_url)

        complete = signed_event(
            att,
            1,
            CompletePayload(
                status="complete",
                manifest_name="manifest.json",
                manifest_crc32c="AAAAAA==",
                objects=0,
                total_bytes=0,
            ),
        )
        assert await ingest(complete) == "accepted"

        async def finalizing() -> bool:
            with contextlib.suppress(Exception):
                return await state(run_id) == RunState.FINALIZING.value and bool(queue.calls)
            return False

        await _until(finalizing)
        assert not loops.done()
        async with ctx.db.session() as s:
            attempts = (
                await s.execute(
                    select(func.count())
                    .select_from(m.RunAttempt)
                    .where(m.RunAttempt.run_id == run_id)
                )
            ).scalar_one()
        assert attempts == 1
        assert queue.calls == [(run_id, 1)], "handed off exactly once"
        assert executor.submit_calls == 1, "no duplicate execution"
    finally:
        orch.stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(loops, 20)
        await shutdown(ctx)
        srv.cleanup()


def _wait_pg_down(url: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(libpq_url(url), connect_timeout=1):
                pass
        except psycopg.OperationalError:
            return
        time.sleep(0.1)
    raise AssertionError("Postgres did not go down")


def _wait_pg_up(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(libpq_url(url), connect_timeout=1):
                return
        except psycopg.OperationalError:
            time.sleep(0.1)
    raise AssertionError("Postgres did not come back up")


# ------------------------------------------------------ retries, throttling, stuck submits

TERMINAL = frozenset(s for s in RunState if s.terminal)


async def _billed_attempts(ctx: ServerContext, run_id: str) -> list[int]:
    async with ctx.db.session() as s:
        return sorted(
            (
                await s.execute(
                    select(m.CostLedgerEntry.attempt).where(
                        m.CostLedgerEntry.run_id == run_id, m.CostLedgerEntry.kind == "actual"
                    )
                )
            ).scalars()
        )


async def test_lost_worker_is_retried_to_success(ctx: ServerContext, tmp_path: Path) -> None:
    """The host of a running execution dies (no exit code; Cloud Run forgets it) with retries
    enabled: exactly one new attempt, never two executions live at once, the run SUCCEEDS,
    and the lost attempt is billed too."""
    async with harness("fake-cloudrun", ctx, tmp_path) as h:
        assert h.fake is not None
        ctx.settings.max_infra_retries = 1
        run_id = await h.fit("hang_first")  # attempt 1 hangs, attempt 2 succeeds
        await h.start()
        await h.wait(run_id, {RunState.RUNNING})
        att = await h.attempt(run_id)
        assert att is not None and att.external_id
        h.fake.lose_execution(att.external_id)
        run = await h.wait(run_id, TERMINAL, timeout=90)
        assert run.state == RunState.SUCCEEDED.value, run.problem
        assert run.attempt == 2 and run.infra_retries == 1
        assert h.executions_created(run_id) == 2 and h.max_live(run_id) == 1
        assert await _billed_attempts(ctx, run_id) == [1, 2]
        await _one_execution(ctx, run_id)


async def test_sigkill_is_never_retried_blindly(ctx: ServerContext, tmp_path: Path) -> None:
    """A SIGKILLed supervisor looks exactly like the OOM killer: even with retries enabled
    it is not retried automatically (that would OOM again); the run fails as
    RESOURCE_EXHAUSTED with the offer to retry one class up, after one execution."""
    async with harness("local-process", ctx, tmp_path) as h:
        ctx.settings.max_infra_retries = 2
        run_id = await h.fit("hang_first")
        await h.start()
        await h.wait(run_id, {RunState.RUNNING})
        pid = await _supervisor_pid(ctx, run_id)
        os.kill(pid, signal.SIGKILL)
        _wait_dead(pid)
        run = await h.wait(run_id, TERMINAL, timeout=90)
        assert run.state == RunState.FAILED.value
        assert run.failure_class == "resource_exhausted" and run.attempt == 1
        assert "retry at class" in str((run.problem or {}).get("detail"))
        assert await _billed_attempts(ctx, run_id) == [1]


async def test_jobs_run_throttling_storm_never_fails_runs(
    ctx: ServerContext, tmp_path: Path
) -> None:
    """jobs.run answers 429 for longer than the executor retries: the run is requeued
    (without an infrastructure retry budget at all) and succeeds once the storm passes,
    with exactly one execution."""
    async with harness("fake-cloudrun", ctx, tmp_path) as h:
        assert h.fake is not None
        h.executor.inner = CloudRunJobsExecutor(
            h.fake,
            INVENTORY,
            max_rate_limit_retries=1,
            backoff_base_s=0.05,
            submits_per_minute=180,
            burst=50,
            jitter=lambda: 0.0,
            adopt_refresh_s=0.0,
        )
        ctx.settings.max_infra_retries = 0
        h.fake.rate_limit(5)  # two exhausted submits, then one that gets through
        run_id = await h.fit("ok")
        await h.start()
        run = await h.wait(run_id, TERMINAL, timeout=90)
        assert run.state == RunState.SUCCEEDED.value, run.problem
        assert run.infra_retries == 0 and run.attempt == 3
        assert h.executions_created(run_id) == 1


async def test_a_stuck_submit_lets_the_lease_lapse_and_is_adopted(
    ctx: ServerContext, tmp_path: Path
) -> None:
    """jobs.run created the execution but the dispatcher never gets the answer back (a hung
    call): its lease stops being renewed after MAX_SUBMIT_LEASES, the reconciler adopts the
    execution by nonce, and the run succeeds with one execution. When the stale call finally
    returns, nothing is created or rolled back."""
    async with harness("fake-cloudrun", ctx, tmp_path) as h:
        assert h.fake is not None
        ctx.settings.dispatch_lease_s = 0.2
        original = h.fake.run_job
        release = asyncio.Event()

        async def stuck(job: str, **kw: Any) -> Any:
            result = await original(job, **kw)
            await release.wait()
            return result

        h.fake.run_job = stuck  # type: ignore[method-assign]
        run_id = await h.fit("ok")
        await h.start()
        try:
            run = await h.wait(run_id, TERMINAL, timeout=60)
        finally:
            release.set()
        assert run.state == RunState.SUCCEEDED.value, run.problem
        assert run.attempt == 1 and h.executions_created(run_id) == 1
        await asyncio.sleep(0.3)  # the stale submit returns and is recorded as a no-op
        assert (await h.run(run_id)).state == RunState.SUCCEEDED.value
        assert h.executions_created(run_id) == 1
