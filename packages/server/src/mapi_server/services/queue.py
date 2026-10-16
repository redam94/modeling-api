"""Background jobs on procrastinate (same Postgres as the control plane).

The reconciler hands every run that reaches ``FINALIZING`` to :class:`ProcrastinateQueue`;
the ``finalize_run`` task runs :meth:`Finalizer.finalize` with retries (storage hiccups),
serialised per run by a procrastinate ``lock``. Jobs a dead worker left in ``doing`` are
re-queued whenever the reconciler re-defers the run. :class:`InlineFinalizeQueue` runs the same
finalizer in-process (single-process ``dev`` mode and tests).

Webhook deliveries (M3) run on the ``webhooks`` queue: the relay
(:class:`~mapi_server.services.webhooks.delivery.WebhookRelay`) defers one
``deliver_webhook`` job per due try, de-duplicated by a queueing lock per delivery and
attempt. Retries are scheduled in the delivery row (``next_attempt_at``), not by
procrastinate, so a lost job is simply deferred again by the next relay pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import procrastinate
from procrastinate import RetryStrategy
from procrastinate.exceptions import AlreadyEnqueued

from mapi_server.services.finalizer import Finalizer

if TYPE_CHECKING:
    from mapi_server.services.webhooks.delivery import WebhookDeliverer

__all__ = ["FINALIZE_QUEUE", "WEBHOOK_QUEUE", "InlineFinalizeQueue", "ProcrastinateQueue"]

log = logging.getLogger(__name__)

FINALIZE_QUEUE = "finalize"
WEBHOOK_QUEUE = "webhooks"
STALLED_WORKER_S = 30.0
"""A worker that missed heartbeats this long is dead (procrastinate's own default)."""


class ProcrastinateQueue:
    """Defers ``finalize_run`` jobs and runs the procrastinate worker."""

    def __init__(
        self,
        conninfo: str,
        finalizer: Finalizer,
        *,
        deliverer: WebhookDeliverer | None = None,
    ) -> None:
        self._finalizer = finalizer
        self._deliverer = deliverer
        self.app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(conninfo=conninfo, min_size=1, max_size=4)
        )

        @self.app.task(
            name="mapi.finalize_run",
            queue=FINALIZE_QUEUE,
            retry=RetryStrategy(max_attempts=5, wait=2, exponential_wait=2),
            pass_context=False,
        )
        async def finalize_run(run_id: str, attempt: int) -> None:
            state = await self._finalizer.finalize(run_id, attempt)
            log.info("finalized %s/%s → %s", run_id, attempt, state)

        self._task: Any = finalize_run

        @self.app.task(
            name="mapi.deliver_webhook",
            queue=WEBHOOK_QUEUE,
            pass_context=False,
        )
        async def deliver_webhook(delivery_id: str, attempt: int) -> None:
            if self._deliverer is None:  # pragma: no cover - a worker without webhooks
                raise RuntimeError("this worker does not deliver webhooks")
            outcome = await self._deliverer.deliver(delivery_id)
            log.debug("webhook delivery %s/%s → %s", delivery_id, attempt, outcome)

        self._deliver_task: Any = deliver_webhook
        self._opened = False
        self.queues: list[str] = [FINALIZE_QUEUE] + (
            [WEBHOOK_QUEUE] if deliverer is not None else []
        )
        """Queues the worker serves (finalize, webhooks with a deliverer; other components add
        theirs, e.g. ``lifecycle``)."""

    async def open(self) -> None:
        if not self._opened:
            await self.app.open_async()
            self._opened = True

    async def close(self) -> None:
        if self._opened:
            await self.app.close_async()
            self._opened = False

    async def _active(self, lock: str) -> bool:
        connector = self.app.connector
        row = await connector.execute_query_one_async(
            "SELECT count(*) AS n FROM procrastinate_jobs "
            "WHERE lock = %(lock)s AND status IN ('todo', 'doing')",
            lock=lock,
        )
        return bool(row and row["n"])

    async def recover_stalled(self, lock: str | None = None) -> int:
        """Re-queue finalize jobs left in ``doing`` by a dead worker (optionally one lock).

        procrastinate only prunes a dead worker's row (``worker_id`` becomes NULL); its job
        stays ``doing`` forever and, through its ``lock``, blocks every later job of the run.
        A job counts as stalled when its worker is gone or has missed heartbeats for
        ``STALLED_WORKER_S``. The finalizer is idempotent, so a retry is safe.
        """
        manager = self.app.job_manager
        stalled = await manager.get_stalled_jobs(
            queue=FINALIZE_QUEUE, seconds_since_heartbeat=STALLED_WORKER_S
        )
        n = 0
        for job in stalled:
            if lock is not None and job.lock != lock:
                continue
            log.warning("re-queueing stalled finalize job %s (%s)", job.id, job.lock)
            await manager.retry_job(job)
            n += 1
        return n

    async def defer_finalize(self, run_id: str, attempt: int) -> None:
        await self.open()
        lock = f"finalize:{run_id}"
        await self.recover_stalled(lock)
        if await self._active(lock):
            return
        with contextlib.suppress(AlreadyEnqueued):
            await self._task.configure(lock=lock, queueing_lock=f"{lock}:{attempt}").defer_async(
                run_id=run_id, attempt=attempt
            )

    async def defer_delivery(self, delivery_id: str, attempt: int) -> None:
        """Hand one webhook delivery try to the worker (once per delivery and attempt)."""
        await self.open()
        lock = f"webhook:{delivery_id}:{attempt}"
        with contextlib.suppress(AlreadyEnqueued):
            await self._deliver_task.configure(queueing_lock=lock).defer_async(
                delivery_id=delivery_id, attempt=attempt
            )

    async def run_worker(self, *, wait: bool = True) -> None:
        """Run the procrastinate worker on :attr:`queues` (``wait=False`` drains once)."""
        await self.open()
        await self.app.run_worker_async(
            queues=list(self.queues),
            wait=wait,
            concurrency=4,
            install_signal_handlers=False,
            listen_notify=True,
        )


class InlineFinalizeQueue:
    """Runs the finalizer in a background task of this process (one per run at a time)."""

    def __init__(self, finalizer: Finalizer) -> None:
        self._finalizer = finalizer
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def defer_finalize(self, run_id: str, attempt: int) -> None:
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            return
        self._tasks[run_id] = asyncio.create_task(self._run(run_id, attempt))

    async def _run(self, run_id: str, attempt: int) -> None:
        try:
            await self._finalizer.finalize(run_id, attempt)
        except Exception:  # the reconciler re-defers on its next pass
            log.exception("inline finalization of %s/%s failed", run_id, attempt)

    async def drain(self) -> None:
        """Wait for every pending finalization (tests and shutdown)."""
        while pending := [t for t in self._tasks.values() if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
