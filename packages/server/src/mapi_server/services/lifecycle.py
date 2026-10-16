"""The trust-lifecycle scheduler and its background jobs (orchestrator process).

The API never touches the job queue: it only writes rows (a scheduled deletion, a queued
export). :class:`LifecycleScheduler` runs in the orchestrator every
``MAPI_LIFECYCLE_INTERVAL_S`` and

* defers a ``mapi.tenant_purge`` job for every deletion with work to do (grace period over,
  waiting runs, a retention lock that expired, a failed store whose back-off elapsed);
* defers a ``mapi.tenant_export`` job for every queued GDPR export;
* deletes expired export archives and stale sign-ups.

With procrastinate (:class:`ProcrastinateLifecycleJobs`) the jobs run on the ``lifecycle``
queue of the orchestrator's worker, one at a time per deletion or export (procrastinate
``lock``), deduplicated while queued (``queueing_lock``), and re-queued when a dead worker
left them in ``doing``. :class:`InlineLifecycleJobs` runs them as tasks of this process
(single-process ``dev`` mode and tests). Both job bodies are idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any, Final, Protocol

from procrastinate import RetryStrategy
from procrastinate.exceptions import AlreadyEnqueued

from mapi_server.context import ServerContext
from mapi_server.services.deletion import TenantPurger
from mapi_server.services.gdpr_export import ExportBuilder, expire_exports
from mapi_server.services.queue import STALLED_WORKER_S, ProcrastinateQueue
from mapi_server.services.runs import now as utcnow
from mapi_server.services.signup import sweep_signups

__all__ = [
    "LIFECYCLE_QUEUE",
    "InlineLifecycleJobs",
    "LifecycleJobs",
    "LifecycleScheduler",
    "ProcrastinateLifecycleJobs",
]

log = logging.getLogger(__name__)

LIFECYCLE_QUEUE: Final = "lifecycle"


class LifecycleJobs(Protocol):
    async def defer_purge(self, deletion_id: str) -> None: ...

    async def defer_export(self, export_id: str) -> None: ...


class InlineLifecycleJobs:
    """Runs purges and exports as background tasks of this process (one per id at a time)."""

    def __init__(self, ctx: ServerContext) -> None:
        self.purger = TenantPurger(ctx)
        self.builder = ExportBuilder(ctx)
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def _spawn(self, key: str, coro: Any) -> None:
        task = self._tasks.get(key)
        if task is not None and not task.done():
            coro.close()
            return
        self._tasks[key] = asyncio.create_task(self._guard(key, coro))

    @staticmethod
    async def _guard(key: str, coro: Any) -> None:
        try:
            await coro
        except Exception:  # the scheduler retries on its next tick
            log.exception("lifecycle job %s failed", key)

    async def defer_purge(self, deletion_id: str) -> None:
        self._spawn(f"purge:{deletion_id}", self.purger.run(deletion_id))

    async def defer_export(self, export_id: str) -> None:
        self._spawn(f"export:{export_id}", self.builder.run(export_id))

    async def drain(self) -> None:
        while pending := [t for t in self._tasks.values() if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)


class ProcrastinateLifecycleJobs:
    """The jobs on the orchestrator's procrastinate app (queue ``lifecycle``)."""

    def __init__(self, queue: ProcrastinateQueue, ctx: ServerContext) -> None:
        self.queue = queue
        purger, builder = TenantPurger(ctx), ExportBuilder(ctx)
        retry = RetryStrategy(max_attempts=3, wait=5, exponential_wait=2)

        @queue.app.task(
            name="mapi.tenant_purge", queue=LIFECYCLE_QUEUE, retry=retry, pass_context=False
        )
        async def tenant_purge(deletion_id: str) -> None:
            state = await purger.run(deletion_id)
            log.info("tenant purge %s → %s", deletion_id, state.value)

        @queue.app.task(
            name="mapi.tenant_export", queue=LIFECYCLE_QUEUE, retry=retry, pass_context=False
        )
        async def tenant_export(export_id: str) -> None:
            state = await builder.run(export_id)
            log.info("tenant export %s → %s", export_id, state)

        self._purge: Any = tenant_purge
        self._export: Any = tenant_export
        if LIFECYCLE_QUEUE not in queue.queues:
            queue.queues.append(LIFECYCLE_QUEUE)

    async def _recover(self) -> None:
        manager = self.queue.app.job_manager
        for job in await manager.get_stalled_jobs(
            queue=LIFECYCLE_QUEUE, seconds_since_heartbeat=STALLED_WORKER_S
        ):
            log.warning("re-queueing stalled lifecycle job %s (%s)", job.id, job.lock)
            await manager.retry_job(job)

    async def _defer(self, task: Any, lock: str, **kwargs: str) -> None:
        await self.queue.open()
        await self._recover()
        with contextlib.suppress(AlreadyEnqueued):
            await task.configure(lock=lock, queueing_lock=lock).defer_async(**kwargs)

    async def defer_purge(self, deletion_id: str) -> None:
        await self._defer(self._purge, f"purge:{deletion_id}", deletion_id=deletion_id)

    async def defer_export(self, export_id: str) -> None:
        await self._defer(self._export, f"export:{export_id}", export_id=export_id)


class LifecycleScheduler:
    """Finds due deletions and queued exports and defers their jobs; sweeps expired rows."""

    def __init__(self, ctx: ServerContext, jobs: LifecycleJobs) -> None:
        self.ctx = ctx
        self.jobs = jobs
        self.purger = TenantPurger(ctx)
        self.builder = ExportBuilder(ctx)

    async def run_forever(self, stop: asyncio.Event) -> None:
        interval = self.ctx.settings.lifecycle_interval_s
        log.info("lifecycle scheduler started (every %ss)", interval)
        while not stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("lifecycle pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)

    async def tick(self, *, at: datetime | None = None) -> dict[str, int]:
        ts = at or utcnow()
        deletions = await self.purger.due(at=ts)
        for deletion_id in deletions:
            await self.jobs.defer_purge(deletion_id)
        exports = await self.builder.due()
        for export_id in exports:
            await self.jobs.defer_export(export_id)
        expired = await expire_exports(self.ctx, at=ts)
        async with self.ctx.db.session() as s:
            swept = await sweep_signups(s, self.ctx, at=ts)
        return {"purges": len(deletions), "exports": len(exports), "expired": expired,
                "signups_swept": swept}  # fmt: skip
