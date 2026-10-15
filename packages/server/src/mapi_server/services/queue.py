"""Background jobs on procrastinate (same Postgres as the control plane).

The reconciler hands every run that reaches ``FINALIZING`` to :class:`ProcrastinateQueue`;
the ``finalize_run`` task runs :meth:`Finalizer.finalize` with retries (storage hiccups),
serialised per run by a procrastinate ``lock``. :class:`InlineFinalizeQueue` runs the same
finalizer in-process (single-process ``dev`` mode and tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import procrastinate
from procrastinate import RetryStrategy
from procrastinate.exceptions import AlreadyEnqueued

from mapi_server.services.finalizer import Finalizer

__all__ = ["FINALIZE_QUEUE", "InlineFinalizeQueue", "ProcrastinateQueue"]

log = logging.getLogger(__name__)

FINALIZE_QUEUE = "finalize"


class ProcrastinateQueue:
    """Defers ``finalize_run`` jobs and runs the procrastinate worker."""

    def __init__(self, conninfo: str, finalizer: Finalizer) -> None:
        self._finalizer = finalizer
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
        self._opened = False

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

    async def defer_finalize(self, run_id: str, attempt: int) -> None:
        await self.open()
        lock = f"finalize:{run_id}"
        if await self._active(lock):
            return
        with contextlib.suppress(AlreadyEnqueued):
            await self._task.configure(lock=lock, queueing_lock=f"{lock}:{attempt}").defer_async(
                run_id=run_id, attempt=attempt
            )

    async def run_worker(self, *, wait: bool = True) -> None:
        """Run the procrastinate worker on the finalize queue (``wait=False`` drains once)."""
        await self.open()
        await self.app.run_worker_async(
            queues=[FINALIZE_QUEUE],
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
