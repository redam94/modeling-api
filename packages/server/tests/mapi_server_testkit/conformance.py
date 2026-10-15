"""Executor conformance harness (plan §13 "Executor conformance").

:data:`mapi_spec.conformance_executor.EXECUTOR_CONFORMANCE` is the suite as data; this module
runs each case against a real control plane — the dispatcher, reconciler, event ingestion
and finalizer of an :class:`~mapi_server.orchestrator.Orchestrator` over Postgres — with one
backend at a time:

* ``fake-cloudrun`` — :class:`~mapi_server.executors.cloudrun.CloudRunJobsExecutor` over
  :class:`~mapi_server.executors.fake_cloudrun.FakeCloudRun`; the workload is the synthetic
  supervisor, whose stdout reaches ``/internal/events`` as OIDC-authenticated Pub/Sub pushes;
* ``local-process`` — :class:`~mapi_server.executors.LocalProcessExecutor` running the
  synthetic supervisor as a subprocess (stdout pipe);
* ``docker`` — needs a daemon *and* ``CONFORMANCE_DOCKER_IMAGE`` (an image with the
  synthetic supervisor); skipped otherwise.

Every state change of every run is captured by a Postgres trigger (nothing is sampled), so
the observed trajectory handed to :func:`~mapi_spec.conformance_executor.check_expectation`
is exact. Faults are injected through :class:`FaultyExecutor` (any backend) or the fake's own
fault knobs (Cloud Run only).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
from mapi_server.app import create_app
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.executors import LocalProcessExecutor
from mapi_server.executors.cloudrun import CloudRunJobsExecutor, JobInventory
from mapi_server.executors.fake_cloudrun import FakeCloudRun, FakeLatency, FakeOOM, FakeTask
from mapi_server.orchestrator import Orchestrator
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.queue import InlineFinalizeQueue
from mapi_server.services.runs import load_run, now
from mapi_server.services.runspec import build_runspec
from mapi_server.storage import MemoryObjectStore
from mapi_spec.conformance_executor import ConformanceTarget, ObservedRun
from mapi_spec.executor import ExecState, ExecStatus, Executor, ExecutorCaps, Handle, RunSubmit
from mapi_spec.run import FailureClass, RunState
from sqlalchemy import text, update

from .builders import CATALOG, EXEC_TOKEN, submit_fit, trend_spec
from .pubsub import AUDIENCE, PUSH_SA, SUBSCRIPTION, LocalGoogleIdP, PubSubPusher
from .store_http import serve_store
from .synthetic_runner import GATE_ENV, MODE_LABEL, supervise

__all__ = [
    "INVENTORY",
    "FaultyExecutor",
    "Harness",
    "drop_state_log",
    "harness",
    "install_state_log",
    "trajectory",
]

SYNTHETIC_RUNNER = Path(__file__).resolve().parent / "synthetic_runner.py"
# a US region: the platform tenant's data residency is "us" and placement enforces it
INVENTORY = JobInventory(project="mapi-test-exec", region="us-central1", env="local")
TERMINAL = frozenset(s for s in RunState if s.terminal)

_STATE_LOG = (
    """CREATE TABLE IF NOT EXISTS conformance_states (
        id bigserial PRIMARY KEY, run_id text NOT NULL, state text NOT NULL,
        attempt int NOT NULL, at timestamptz NOT NULL DEFAULT clock_timestamp())""",
    """CREATE OR REPLACE FUNCTION conformance_log_state() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'INSERT' OR NEW.state IS DISTINCT FROM OLD.state
           OR NEW.attempt IS DISTINCT FROM OLD.attempt THEN
            INSERT INTO conformance_states (run_id, state, attempt)
            VALUES (NEW.id, NEW.state, NEW.attempt);
        END IF;
        RETURN NEW;
    END $$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public""",
    "DROP TRIGGER IF EXISTS conformance_states ON runs",
    """CREATE TRIGGER conformance_states AFTER INSERT OR UPDATE ON runs
        FOR EACH ROW EXECUTE FUNCTION conformance_log_state()""",
    "TRUNCATE conformance_states",
)


def _owner(ctx: ServerContext) -> Any:
    """A connection as the schema owner: the runtime roles (``mapi_app``/``mapi_system``)
    may not create tables or triggers. The trigger function is ``SECURITY DEFINER`` so the
    runtime roles' updates can still log through it."""
    return psycopg.AsyncConnection.connect(ctx.db.url, autocommit=True)


async def install_state_log(ctx: ServerContext) -> None:
    """Record every run state change (and attempt bump) in ``conformance_states``."""
    async with await _owner(ctx) as conn:
        for stmt in _STATE_LOG:
            await conn.execute(stmt)


async def drop_state_log(ctx: ServerContext) -> None:
    async with await _owner(ctx) as conn:
        await conn.execute("DROP TRIGGER IF EXISTS conformance_states ON runs")
        await conn.execute("DROP FUNCTION IF EXISTS conformance_log_state()")
        await conn.execute("DROP TABLE IF EXISTS conformance_states")


async def trajectory(ctx: ServerContext, run_id: str) -> list[tuple[RunState, int, Any]]:
    """``(state, attempt, at)`` of every change of ``run_id``, in order."""
    async with await _owner(ctx) as conn:
        cur = await conn.execute(
            "SELECT state, attempt, at FROM conformance_states WHERE run_id = %s ORDER BY id",
            (run_id,),
        )
        rows = await cur.fetchall()
    return [(RunState(r[0]), int(r[1]), r[2]) for r in rows]


@dataclass
class _Live:
    run_id: str
    started: float
    ended: float | None = None


class FaultyExecutor:
    """Wraps any executor: fault injection plus bookkeeping of what it created.

    * ``duplicate_submit`` — every ``submit`` is sent twice with the same nonce;
    * :meth:`unknown_once` — the next ``status`` of a nonce reports ``UNKNOWN`` while the
      real execution keeps running (an infrastructure hiccup);
    * :attr:`inner` can be swapped (an orchestrator restart with a fresh driver).
    """

    def __init__(self, inner: Executor) -> None:
        self.inner = inner
        self.name = inner.name
        self.duplicate_submit = False
        self._unknown: set[str] = set()
        self.nonces_by_run: dict[str, set[str]] = {}
        self.submit_calls = 0
        self._live: dict[str, _Live] = {}

    def caps(self) -> ExecutorCaps:
        return self.inner.caps()

    async def submit(self, rs: RunSubmit, nonce: str) -> Handle:
        self.submit_calls += 1
        handle = await self.inner.submit(rs, nonce)
        if self.duplicate_submit:
            self.submit_calls += 1
            again = await self.inner.submit(rs, nonce)
            if again != handle:
                raise AssertionError(f"duplicate submit returned another execution: {again}")
        self.nonces_by_run.setdefault(rs.run_id, set()).add(nonce)
        self._live.setdefault(nonce, _Live(rs.run_id, time.monotonic()))
        return handle

    async def adopt(self, nonce: str) -> Handle | None:
        return await self.inner.adopt(nonce)

    def unknown_once(self, nonce: str) -> None:
        self._unknown.add(nonce)

    async def status(self, h: Handle) -> ExecStatus:
        st = await self.inner.status(h)
        live = self._live.get(h.nonce)
        if live is not None and live.ended is None and st.state.terminal:
            live.ended = time.monotonic()
        if h.nonce in self._unknown and st.state in (ExecState.PENDING, ExecState.RUNNING):
            self._unknown.discard(h.nonce)
            return ExecStatus(state=ExecState.UNKNOWN, message="injected: status unavailable")
        return st

    async def cancel(self, h: Handle) -> None:
        await self.inner.cancel(h)

    def event_source(self) -> Any:
        return self.inner.event_source()

    async def aclose(self) -> None:
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()

    def executions_created(self, run_id: str) -> int:
        return len(self.nonces_by_run.get(run_id, ()))

    def max_live(self, run_id: str) -> int:
        """Most executions of ``run_id`` live at once, as the platform observed them."""
        spans = [v for v in self._live.values() if v.run_id == run_id]
        points = sorted(
            [(s.started, 1) for s in spans] + [(s.ended or float("inf"), -1) for s in spans]
        )
        live = best = 0
        for _, delta in points:
            live += delta
            best = max(best, live)
        return best


@dataclass
class Harness:
    """One backend under a running control plane (see module doc)."""

    name: str
    ctx: ServerContext
    executor: FaultyExecutor
    target: ConformanceTarget
    api: httpx.AsyncClient
    gate_dir: Path
    fake: FakeCloudRun | None = None
    make_driver: Callable[[], Executor] | None = None
    orch: Orchestrator | None = None
    spec_id: str = ""
    _task: asyncio.Task[None] | None = None
    notes: list[str] = field(default_factory=list)

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.orch = Orchestrator(self.ctx, self.executor, use_procrastinate=False)
        self._task = asyncio.create_task(self.orch.run(), name=f"orchestrator-{self.name}")

    async def stop(self) -> None:
        if self.orch is not None and self._task is not None:
            self.orch.stop.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 30)

    def restart_driver(self) -> None:
        """A fresh executor driver over the same backend (an orchestrator restart)."""
        if self.make_driver is not None:
            self.executor.inner = self.make_driver()

    # ------------------------------------------------------------------- runs
    async def fit(
        self,
        mode: str = "ok",
        *,
        timeout_s: int | None = None,
        checkpointable: bool | None = None,
        labels: dict[str, str] | None = None,
    ) -> str:
        return await submit_fit(
            self.ctx,
            self.spec_id,
            labels={MODE_LABEL: mode, **(labels or {})},
            timeout_s=timeout_s,
            checkpointable=checkpointable,
        )

    async def run(self, run_id: str) -> m.Run:
        async with self.ctx.db.session() as s:
            return await load_run(s, None, run_id)

    async def attempt(self, run_id: str, attempt: int = 1) -> m.RunAttempt | None:
        async with self.ctx.db.session() as s:
            return await s.get(m.RunAttempt, (run_id, attempt))

    async def wait(
        self, run_id: str, states: frozenset[RunState] | set[RunState], timeout: float = 60
    ) -> m.Run:
        deadline = time.monotonic() + timeout
        while True:
            run = await self.run(run_id)
            if RunState(run.state) in states:
                return run
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"{self.name}: run {run_id} is {run.state}, not in {sorted(states)}"
                )
            await asyncio.sleep(0.02)

    async def until(self, check: Callable[[], Awaitable[bool]], timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while not await check():
            if time.monotonic() > deadline:
                raise AssertionError(f"{self.name}: condition not reached in {timeout}s")
            await asyncio.sleep(0.02)

    def open_gate(self, run_id: str) -> None:
        (self.gate_dir / run_id).touch()

    async def backdate_events(self, run_id: str, seconds: float = 3600) -> None:
        """Pretend the run has been silent for ``seconds`` (no event, no start)."""
        async with self.ctx.db.session() as s:
            await s.execute(
                update(m.Run)
                .where(m.Run.id == run_id)
                .values(
                    last_event_at=now() - timedelta(seconds=seconds),
                    started_at=now() - timedelta(seconds=seconds),
                )
            )

    # ------------------------------------------------------------- observing
    def executions_created(self, run_id: str) -> int:
        if self.fake is not None:
            return len(self.fake.executions_for_run(run_id))
        return self.executor.executions_created(run_id)

    def max_live(self, run_id: str) -> int:
        if self.fake is not None:
            return self.fake.max_live_by_run[run_id]
        return self.executor.max_live(run_id)

    async def observe(self, run_id: str) -> ObservedRun:
        run = await self.run(run_id)
        states: list[RunState] = []
        for state, _, _ in await trajectory(self.ctx, run_id):
            if not states or states[-1] is not state:
                states.append(state)
        problem = run.problem or {}
        return ObservedRun(
            states=tuple(states),
            failure_class=FailureClass(run.failure_class) if run.failure_class else None,
            attempts=run.attempt,
            executions_created=self.executions_created(run_id),
            max_live_executions=self.max_live(run_id),
            offered_next_class="retry at class" in str(problem.get("detail", "")),
        )


def _api_client(ctx: ServerContext) -> httpx.AsyncClient:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    )


def _tune(ctx: ServerContext) -> None:
    s = ctx.settings
    s.complete_event_grace_s = 5.0
    s.lost_execution_grace_s = 0.0
    s.reconcile_interval_s = 0.05
    s.dispatch_poll_s = 0.05
    s.deadline_grace_s = 0.0
    s.max_infra_retries = 2


@contextlib.asynccontextmanager
async def fake_cloudrun_harness(ctx: ServerContext, tmp: Path) -> AsyncIterator[Harness]:
    _tune(ctx)
    store = ctx.store
    assert isinstance(store, MemoryObjectStore)
    idp = LocalGoogleIdP()
    from mapi_server.services.pubsub import PushVerifier
    from mapi_spec.auth import PushAuthExpectation

    ctx.settings.pubsub_push_audience = AUDIENCE
    ctx.settings.pubsub_push_service_account = PUSH_SA
    ctx.settings.pubsub_push_subscriptions = [SUBSCRIPTION]
    ctx.push_verifier = PushVerifier(
        PushAuthExpectation(audience=AUDIENCE, service_account_email=PUSH_SA),
        idp.jwks_cache(),
        subscriptions=[SUBSCRIPTION],
    )
    gate = tmp / "gates"
    gate.mkdir(parents=True, exist_ok=True)
    async with (
        serve_store(store, exec_token=EXEC_TOKEN, bootstrap_bucket=ctx.buckets.bootstrap) as url,
        _api_client(ctx) as api,
        _api_client(ctx) as pushes,
        httpx.AsyncClient(timeout=30) as http,
    ):
        pusher = PubSubPusher(pushes, idp)

        async def workload(task: FakeTask) -> int:
            code = await supervise(task.env, emit=task.print, sigterm=task.sigterm, http=http)
            if code == 137:
                raise FakeOOM  # the container's memory limit, as Cloud Run reports it
            return code

        fake = FakeCloudRun.for_inventory(
            INVENTORY,
            job_env={
                "MAPI_BOOTSTRAP_URL": f"{url}/storage/v1/b/{ctx.buckets.bootstrap}/o/{{nonce}}",
                "MAPI_EXEC_TOKEN": EXEC_TOKEN,  # the shim's exec principal (cloud: the job SA)
                GATE_ENV: str(gate),
            },
            workload=workload,
            log_sink=pusher.push,
            latency=FakeLatency(provision_s=0.02, provision_jitter_s=0.03),
            cancel_grace_s=0.5,
        )

        def driver() -> CloudRunJobsExecutor:
            return CloudRunJobsExecutor(
                fake,
                INVENTORY,
                submits_per_minute=180,
                burst=50,
                backoff_base_s=0.1,
                jitter=lambda: 0.0,
                adopt_refresh_s=0.0,
                cancel_resend_s=1.0,
            )

        executor = FaultyExecutor(driver())
        h = Harness(
            name="fake-cloudrun",
            ctx=ctx,
            executor=executor,
            target=ConformanceTarget(
                executor="cloudrun-jobs",
                caps=executor.caps(),
                remote_control_api=True,
                enforces_memory_limit=True,
            ),
            api=api,
            gate_dir=gate,
            fake=fake,
            make_driver=driver,
        )
        h.spec_id = await trend_spec(ctx)
        try:
            yield h
        finally:
            await h.stop()
            await fake.aclose()


@contextlib.asynccontextmanager
async def local_process_harness(ctx: ServerContext, tmp: Path) -> AsyncIterator[Harness]:
    _tune(ctx)
    store = ctx.store
    assert isinstance(store, MemoryObjectStore)
    gate = tmp / "gates"
    gate.mkdir(parents=True, exist_ok=True)
    async with (
        serve_store(store, exec_token=EXEC_TOKEN, bootstrap_bucket=ctx.buckets.bootstrap) as url,
        _api_client(ctx) as api,
    ):
        inner = LocalProcessExecutor(
            command=[sys.executable, str(SYNTHETIC_RUNNER)],
            bootstrap_url_template=f"{url}/storage/v1/b/{ctx.buckets.bootstrap}/o/{{nonce}}",
            workdir=ctx.settings.workdir,
            exec_token=EXEC_TOKEN,
            extra_env={"FAKE_RUNNER_CATALOG": str(CATALOG), GATE_ENV: str(gate)},
            cancel_grace_s=2,
        )
        executor = FaultyExecutor(inner)
        h = Harness(
            name="local-process",
            ctx=ctx,
            executor=executor,
            target=ConformanceTarget(executor="local-process", caps=executor.caps()),
            api=api,
            gate_dir=gate,
        )
        h.spec_id = await trend_spec(ctx)
        try:
            yield h
        finally:
            await h.stop()
            await inner.aclose()


@contextlib.asynccontextmanager
async def docker_harness(ctx: ServerContext, tmp: Path) -> AsyncIterator[Harness]:
    """``docker`` needs a daemon and an image carrying the synthetic supervisor."""
    import pytest
    from mapi_server.executors import DockerExecutor, docker_available

    image = os.environ.get("CONFORMANCE_DOCKER_IMAGE")
    if not docker_available() or not image:
        pytest.skip("docker conformance needs a daemon and CONFORMANCE_DOCKER_IMAGE")
    _tune(ctx)
    store = ctx.store
    assert isinstance(store, MemoryObjectStore)
    gate = tmp / "gates"
    gate.mkdir(parents=True, exist_ok=True)
    async with (
        serve_store(store, exec_token=EXEC_TOKEN, bootstrap_bucket=ctx.buckets.bootstrap) as url,
        _api_client(ctx) as api,
    ):
        inner = DockerExecutor(
            image=image,
            bootstrap_url_template=f"{url}/storage/v1/b/{ctx.buckets.bootstrap}/o/{{nonce}}",
            exec_token=EXEC_TOKEN,
            command=["python", "/opt/mapi-testkit/synthetic_runner.py"],
            network="host",
        )
        executor = FaultyExecutor(inner)
        h = Harness(
            name="docker",
            ctx=ctx,
            executor=executor,
            target=ConformanceTarget(
                executor="docker", caps=executor.caps(), enforces_memory_limit=True
            ),
            api=api,
            gate_dir=gate,
        )
        h.spec_id = await trend_spec(ctx)
        try:
            yield h
        finally:
            await h.stop()


HARNESSES = {
    "fake-cloudrun": fake_cloudrun_harness,
    "local-process": local_process_harness,
    "docker": docker_harness,
}


def harness(
    name: str, ctx: ServerContext, tmp: Path
) -> contextlib.AbstractAsyncContextManager[Harness]:
    return HARNESSES[name](ctx, tmp)


# helpers for the fan-out case (executor level: fan-out parents arrive in M4)
async def lease_for_fanout(h: Harness, run_id: str) -> tuple[m.Run, m.RunAttempt]:
    """Lease ``run_id`` (intent row + nonce) without dispatching it."""
    leased = await Dispatcher(h.ctx, h.executor).lease()
    assert (run_id, 1) in leased, leased
    async with h.ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        s.expunge_all()
    return run, att


async def write_task_bootstraps(h: Harness, run: m.Run, att: m.RunAttempt, count: int) -> None:
    """One bootstrap RunSpec per task, each at its task nonce (what M4's dispatcher writes)."""
    from mapi_server.executors.tasks import task_nonce
    from mapi_spec.storage import bootstrap_key

    base = await build_runspec(h.ctx.store, run=run, attempt=att, runs_bucket=h.ctx.buckets.runs)
    for i in range(count):
        nonce = task_nonce(att.nonce, i, count)
        child = base.model_copy(
            update={"nonce": nonce, "labels": {**base.labels, "test.mapi/child": f"child-{i}"}}
        )
        await h.ctx.store.put(
            h.ctx.buckets.bootstrap,
            bootstrap_key(nonce),
            child.model_dump_json().encode(),
            content_type="application/json",
            if_generation_match=0,
        )


def inline_queue(ctx: ServerContext) -> InlineFinalizeQueue:
    return InlineFinalizeQueue(Finalizer(ctx))
