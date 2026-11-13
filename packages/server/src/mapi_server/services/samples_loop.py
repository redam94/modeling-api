"""The sample loop (v0.18 D20): beside the dashboard loop, every ``workflow_tick_s``.

One pass (:meth:`SampleLoop.once`) takes at most :data:`BATCH` samples that are on their way
(``provisioning``, ``ingesting``, ``fitting``; the ones untouched longest first) and moves
each one on with :func:`~mapi_server.services.samples.advance`, in its own transaction under
the tenant's row-level security. Rows are locked ``FOR UPDATE SKIP LOCKED``, so several
worker processes share the work without doing a sample twice, and a sample whose step
raises something unexpected is logged and tried again on the next pass (a refusal or a
failed run is not unexpected: ``advance`` records it on the sample).

With ``MAPI_SAMPLES_ENABLED=false`` the loop does nothing (rows wait as they are).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Final

from sqlalchemy import select

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.samples import LOOP_STATES, advance

__all__ = ["BATCH", "SampleLoop"]

log = logging.getLogger(__name__)

BATCH: Final = 20


class SampleLoop:
    """Moves installed samples from ``provisioning`` to ``ready`` (or ``failed``)."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx

    async def due(self) -> list[tuple[str, str]]:
        """``(tenant_id, sample_id)`` of the samples on their way, oldest change first."""
        async with self.ctx.db.session() as s:
            rows = (
                await s.execute(
                    select(m.TenantSample.tenant_id, m.TenantSample.sample_id)
                    .where(m.TenantSample.state.in_(LOOP_STATES))
                    .order_by(m.TenantSample.updated_at)
                    .limit(BATCH)
                )
            ).all()
        return [(str(tenant_id), str(sample_id)) for tenant_id, sample_id in rows]

    async def step(self, tenant_id: str, sample_id: str) -> bool:
        """Advance one sample (skipped when another worker holds it). Returns whether it
        changed."""
        async with self.ctx.db.tenant_session(tenant_id) as s:
            row = (
                await s.execute(
                    select(m.TenantSample)
                    .where(
                        m.TenantSample.tenant_id == tenant_id,
                        m.TenantSample.sample_id == sample_id,
                        m.TenantSample.state.in_(LOOP_STATES),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            return await advance(self.ctx, s, row)

    async def once(self) -> int:
        """One pass; returns how many samples changed."""
        if not self.ctx.settings.samples_enabled:
            return 0
        changed = 0
        for tenant_id, sample_id in await self.due():
            try:
                changed += await self.step(tenant_id, sample_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sample %s of tenant %s could not be advanced", sample_id, tenant_id)
        return changed

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("sample loop started (every %ss)", self.ctx.settings.workflow_tick_s)
        while not stop.is_set():
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sample loop pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.ctx.settings.workflow_tick_s)
