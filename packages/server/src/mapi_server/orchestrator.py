"""The orchestrator process (``mapi-server worker``): dispatcher + reconciler + event pump +
housekeeper + webhook relay + procrastinate worker, all on one event loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from mapi_spec.executor import Executor

from mapi_server.context import ServerContext
from mapi_server.executors import placement_warnings
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.events import EventIngestor
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.housekeeping import Housekeeper
from mapi_server.services.queue import InlineFinalizeQueue, ProcrastinateQueue
from mapi_server.services.reconciler import Reconciler
from mapi_server.services.submit import submit_describe
from mapi_server.services.webhooks.delivery import (
    InlineWebhookQueue,
    WebhookDeliverer,
    WebhookQueue,
    WebhookRelay,
)

__all__ = ["Orchestrator"]

log = logging.getLogger(__name__)


class Orchestrator:
    """Owns the executor and every background loop of a worker process."""

    def __init__(
        self, ctx: ServerContext, executor: Executor, *, use_procrastinate: bool = True
    ) -> None:
        self.ctx = ctx
        self.executor = executor
        self.finalizer = Finalizer(ctx)
        self.deliverer = WebhookDeliverer(ctx)
        self.procrastinate: ProcrastinateQueue | None = None
        webhook_queue: WebhookQueue | None = None
        if ctx.queue is None:
            if use_procrastinate:
                self.procrastinate = ProcrastinateQueue(
                    ctx.db.url, self.finalizer, deliverer=self.deliverer
                )
                ctx.queue = self.procrastinate
                webhook_queue = self.procrastinate
            else:
                ctx.queue = InlineFinalizeQueue(self.finalizer)
        self.webhook_queue: WebhookQueue = webhook_queue or InlineWebhookQueue(self.deliverer)
        self.webhooks = WebhookRelay(ctx, self.webhook_queue)
        self.dispatcher = Dispatcher(ctx, executor)
        self.reconciler = Reconciler(ctx, executor)
        self.ingestor = EventIngestor(ctx)
        self.housekeeper = Housekeeper(ctx)
        self.stop = asyncio.Event()

    async def ensure_catalog(self) -> None:
        """Queue a describe run when no catalog is cached for this image digest."""
        async with self.ctx.db.session() as s:
            if await self.ctx.catalogs.get(s) is None:
                run = await submit_describe(s, self.ctx)
                if run is not None:
                    log.info(
                        "no catalog for %s: queued describe run %s",
                        self.ctx.catalogs.image_digest,
                        run.id,
                    )

    async def warn_unplaceable(self) -> list[str]:
        """Log every active tenant this executor cannot place (residency or tier): their
        runs would be refused at admission (or fail at dispatch)."""
        async with self.ctx.db.session() as s:
            lines = await placement_warnings(s, self.executor.caps())
        for line in lines[:20]:
            log.warning("%s", line)
        if len(lines) > 20:
            log.warning("... and %d more tenants this executor cannot place", len(lines) - 20)
        return lines

    async def run(self) -> None:
        await self.ensure_catalog()
        await self.warn_unplaceable()
        tasks = [
            asyncio.create_task(self.dispatcher.run_forever(self.stop), name="dispatcher"),
            asyncio.create_task(self.reconciler.run_forever(self.stop), name="reconciler"),
            asyncio.create_task(self.ingestor.pump(self.executor.event_source()), name="events"),
            asyncio.create_task(self.housekeeper.run_forever(self.stop), name="housekeeping"),
            asyncio.create_task(self.webhooks.run_forever(self.stop), name="webhooks"),
        ]
        if self.procrastinate is not None:
            tasks.append(
                asyncio.create_task(self.procrastinate.run_worker(wait=True), name="procrastinate")
            )
        stopper = asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t is not stopper and t.exception() is not None:
                    raise t.exception()  # type: ignore[misc]
        finally:
            self.stop.set()
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            stopper.cancel()
            if isinstance(self.webhook_queue, InlineWebhookQueue):
                # let tries in flight record their outcome (a cut-off try is retried later,
                # but its receiver would see the event twice)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.webhook_queue.drain(), 15)
            if self.procrastinate is not None:
                await self.procrastinate.close()
            aclose = getattr(self.executor, "aclose", None)
            if aclose is not None:
                await aclose()
