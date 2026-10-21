"""The dashboard loop (v0.16, plan D10): beside the workflow loop, every ``workflow_tick_s``.

One pass (:meth:`DashboardLoop.once`):

1. submits ``queued`` readouts (baselines, refreshes) as each dashboard's publisher;
2. materialises ``materialising`` readouts (their run finished): reads the score summary,
   the fit metrics and the grid, builds the capped snapshot and sets ``ready``;
3. starts the refit workflow of approved refit requests whose dataset version is ready;
4. deletes private readouts past their ``expires_at`` (saved scenarios: 30 days).

Each step takes at most :data:`BATCH` rows (``FOR UPDATE SKIP LOCKED``), so several worker
processes share the work without doing a row twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Final

from sqlalchemy import delete, select

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import refits
from mapi_server.services.dashboards.inputs import submit_queued
from mapi_server.services.dashboards.readouts import materialise
from mapi_server.services.runs import now

__all__ = ["BATCH", "DashboardLoop"]

log = logging.getLogger(__name__)

BATCH: Final = 20


class DashboardLoop:
    """Submits, materialises and prunes dashboard readouts; advances refit requests."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx

    async def submit_queued(self) -> int:
        async with self.ctx.db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(m.DashboardReadout)
                        .where(m.DashboardReadout.status == "queued")
                        .order_by(m.DashboardReadout.created_at)
                        .limit(BATCH)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for row in rows:
                await submit_queued(self.ctx, s, row)
        return len(rows)

    async def materialise(self) -> int:
        async with self.ctx.db.session() as s:
            keys = [
                (str(dashboard_id), int(seq))
                for dashboard_id, seq in (
                    await s.execute(
                        select(m.DashboardReadout.dashboard_id, m.DashboardReadout.seq)
                        .where(m.DashboardReadout.status == "materialising")
                        .order_by(m.DashboardReadout.created_at)
                        .limit(BATCH)
                    )
                ).all()
            ]
        done = 0
        for dashboard_id, seq in keys:
            try:
                if await materialise(self.ctx, dashboard_id, seq) is not None:
                    done += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("readout %s/%s could not be materialised", dashboard_id, seq)
        return done

    async def prune(self) -> int:
        async with self.ctx.db.session() as s:
            res = await s.execute(
                delete(m.DashboardReadout)
                .where(
                    m.DashboardReadout.visibility == "private",
                    m.DashboardReadout.expires_at.is_not(None),
                    m.DashboardReadout.expires_at < now(),
                    m.DashboardReadout.status.in_(("ready", "failed")),
                )
                .execution_options(synchronize_session=False)
            )
        return int(getattr(res, "rowcount", 0) or 0)

    async def once(self) -> None:
        await self.submit_queued()
        await self.materialise()
        await refits.advance(self.ctx, limit=BATCH)
        await self.prune()

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("dashboard loop started (every %ss)", self.ctx.settings.workflow_tick_s)
        while not stop.is_set():
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dashboard loop pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.ctx.settings.workflow_tick_s)
