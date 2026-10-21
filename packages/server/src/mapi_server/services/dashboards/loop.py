"""The dashboard loop (v0.16, plan D10): beside the workflow loop, every ``workflow_tick_s``.

One pass (:meth:`DashboardLoop.once`):

1. submits ``queued`` readouts (baselines, refreshes) as each dashboard's publisher;
2. materialises ``materialising`` readouts (their run finished): reads the score summary,
   the fit metrics and the grid, builds the capped snapshot and sets ``ready``. A pass that
   raises (a transient storage error, anything unexpected) counts an attempt; after
   :data:`~mapi_server.services.dashboards.readouts.MAX_MATERIALISE_ATTEMPTS` the readout
   fails, and readouts with fewer attempts go first;
3. starts the refit workflow of approved refit requests whose dataset version is ready;
4. deletes private readouts past their ``expires_at`` (saved scenarios: 30 days).

Each step takes at most :data:`BATCH` rows (``FOR UPDATE SKIP LOCKED``), so several worker
processes share the work without doing a row twice.

Start-up (v0.17, plan §10.1): before the first pass, :meth:`DashboardLoop.start_up` runs the
automatic layout 1 → layout 2 upgrade once per process
(:func:`~mapi_server.services.dashboards.upgrade.run_auto_upgrade`: idempotent, under a
Postgres advisory lock so concurrent workers upgrade each dashboard once, off with
``MAPI_DASHBOARD_AUTO_UPGRADE=false``). It never raises: a failed pass is logged and the
next start tries again. Dashboards a pass left for later (a transient error, or the image
catalog of a new release not read yet) are tried again every :data:`UPGRADE_RETRY_S`
seconds (:meth:`DashboardLoop.retry_upgrade`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Final

from sqlalchemy import delete, select

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import refits
from mapi_server.services.dashboards.inputs import submit_queued
from mapi_server.services.dashboards.readouts import (
    materialise,
    record_failed_attempt,
    settle_pictures,
)
from mapi_server.services.dashboards.upgrade import UpgradeReport, run_auto_upgrade
from mapi_server.services.runs import now

__all__ = ["BATCH", "DashboardLoop"]

log = logging.getLogger(__name__)

BATCH: Final = 20
UPGRADE_RETRY_S: Final = 60.0
"""How long the loop waits before it runs an upgrade pass that left dashboards again."""
MAX_UPGRADE_RETRIES: Final = 60


class DashboardLoop:
    """Submits, materialises and prunes dashboard readouts; advances refit requests."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx
        self.upgraded: UpgradeReport | None = None
        self._retries = 0
        self._next_retry = 0.0

    async def start_up(self) -> UpgradeReport:
        """The once-per-process start-up work: the automatic layout 2 upgrade (a no-op when
        ``dashboard_auto_upgrade`` is off or another worker holds the lock)."""
        if self.upgraded is None:
            self.upgraded = await run_auto_upgrade(self.ctx)
            self._next_retry = time.monotonic() + UPGRADE_RETRY_S
        return self.upgraded

    async def retry_upgrade(self, *, force: bool = False) -> UpgradeReport | None:
        """Run the upgrade pass again while the last one left dashboards for later (a
        transient error, the image catalog of a new release not read yet): every
        :data:`UPGRADE_RETRY_S` seconds, at most :data:`MAX_UPGRADE_RETRIES` times per
        process. Returns the new report, or ``None`` when nothing was due."""
        last = self.upgraded
        if last is None or not last.retry or self._retries >= MAX_UPGRADE_RETRIES:
            return None
        if not force and time.monotonic() < self._next_retry:
            return None
        self._retries += 1
        self._next_retry = time.monotonic() + UPGRADE_RETRY_S
        self.upgraded = await run_auto_upgrade(self.ctx)
        return self.upgraded

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
                        # rows that keep failing go last, so they never starve new ones
                        .order_by(m.DashboardReadout.attempts, m.DashboardReadout.created_at)
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
            except Exception as exc:
                log.exception("readout %s/%s could not be materialised", dashboard_id, seq)
                try:
                    await record_failed_attempt(self.ctx, dashboard_id, seq, exc)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("readout %s/%s: the attempt was not recorded", dashboard_id, seq)
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
        await settle_pictures(self.ctx, limit=BATCH)
        await refits.advance(self.ctx, limit=BATCH)
        await self.prune()

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("dashboard loop started (every %ss)", self.ctx.settings.workflow_tick_s)
        await self.start_up()
        while not stop.is_set():
            try:
                await self.retry_upgrade()
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dashboard loop pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.ctx.settings.workflow_tick_s)
