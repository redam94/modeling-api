"""Backend throttling at dispatch: a ``429`` from ``jobs.run`` means nothing was created, so
it never counts against a run's infrastructure retries; and a lease never expires while its
dispatcher is still waiting to submit (back-off, a slow executor, earlier runs of the batch)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.executors.cloudrun import CloudRunJobsExecutor, CloudRunRateLimited
from mapi_server.executors.fake_cloudrun import FakeCloudRun
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.runs import now
from mapi_server_testkit.builders import queued_fit, submit_fit, trend_spec
from mapi_server_testkit.conformance import INVENTORY
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.executor import Handle, RunSubmit
from mapi_spec.run import RunState

pytestmark = pytest.mark.integration


class Throttled(FakeCloudRun):
    """``jobs.run`` answers 429 until ``open`` is set."""

    open = False

    async def run_job(self, job: str, **kw: Any) -> Any:
        if not self.open:
            raise CloudRunRateLimited("429 Too Many Requests", retry_after=3600.0)
        return await super().run_job(job, **kw)


class FakeTime:
    """A clock that only advances when the executor sleeps (back-off without waiting)."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


async def _run(ctx: ServerContext, run_id: str) -> m.Run:
    async with ctx.db.session() as s:
        run = await s.get(m.Run, run_id)
        assert run is not None
        return run


async def test_rate_limited_submits_never_use_up_infra_retries(ctx: ServerContext) -> None:
    ctx.settings.max_infra_retries = 1
    api = Throttled.for_inventory(INVENTORY)
    fake = FakeTime()
    ex = CloudRunJobsExecutor(
        api, INVENTORY, clock=fake.clock, sleep=fake.sleep, max_rate_limit_retries=1
    )
    dispatcher = Dispatcher(ctx, ex)
    run_id = await queued_fit(ctx)
    for _ in range(4):  # a sustained jobs.run throttling storm
        dispatcher._backoff_until = 0.0
        assert await dispatcher.dispatch_once() == 1
        run = await _run(ctx, run_id)
        assert run.state == RunState.QUEUED.value, run.problem
        assert run.infra_retries == 0
    # Retry-After is honoured but capped, so one throttled answer cannot park the queue
    assert fake.slept and max(fake.slept) <= ex._backoff_cap * 1.25
    api.open = True
    dispatcher._backoff_until = 0.0
    assert await dispatcher.dispatch_once() == 1
    run = await _run(ctx, run_id)
    assert run.state == RunState.PROVISIONING.value and run.infra_retries == 0


@dataclass
class SlowExecutor(FakeExecutor):
    """``submit`` waits for ``release`` (an executor backing off inside ``submit``)."""

    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def submit(self, rs: RunSubmit, nonce: str) -> Handle:
        await self.release.wait()
        return await super().submit(rs, nonce)


async def test_leases_are_renewed_while_the_batch_is_being_submitted(
    ctx: ServerContext,
) -> None:
    ctx.settings.dispatch_lease_s = 0.3
    ex = SlowExecutor()
    dispatcher = Dispatcher(ctx, ex)
    spec = await trend_spec(ctx)
    first = await submit_fit(ctx, spec)
    second = await submit_fit(ctx, spec)
    task = asyncio.create_task(dispatcher.dispatch_once())
    try:
        await asyncio.sleep(1.2)  # four lease lengths: the first submit is still in flight
        for run_id in (first, second):
            run = await _run(ctx, run_id)
            assert run.state == RunState.DISPATCHING.value
            assert run.lease_expires_at is not None and run.lease_expires_at > now(), run_id
    finally:
        ex.release.set()
        assert await asyncio.wait_for(task, 10) == 2
    for run_id in (first, second):
        run = await _run(ctx, run_id)
        assert run.state == RunState.PROVISIONING.value and run.lease_expires_at is None
