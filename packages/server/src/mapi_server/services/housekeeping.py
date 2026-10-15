"""Periodic clean-up of rows that would otherwise only ever be cleaned lazily (or never).

The orchestrator runs :meth:`Housekeeper.run_forever` every ``housekeeping_interval_s``:

* **upload intents** whose signed PUT expired while still ``pending`` become ``expired``
  and whatever reached the quarantine bucket is deleted, so an abandoned upload (browser
  closed, CLI killed) never holds storage quota or quarantine space for long. Quota usage
  already ignores them once their PUT has expired (:func:`~mapi_server.services.quotas.
  quota_usage`); this makes the row say so.

* **browser sessions** past their absolute or idle expiry, and sign-ins abandoned between
  ``/auth/login`` and ``/auth/callback``, are deleted (a request with an expired cookie
  deletes its own row; this catches the cookies that never come back).

It runs on the platform role and touches nothing a request could still be using: an
expired PUT can no longer be completed, ``create_dataset_version`` refuses an upload
whose intent expired, and an expired session is refused anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from mapi_spec.storage import NotFound
from sqlalchemy import update

from mapi_server.auth.identity import sweep_expired_logins
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import now as utcnow

__all__ = ["Housekeeper"]

log = logging.getLogger(__name__)


class Housekeeper:
    """Sweeps expired upload intents, sessions and sign-ins on a slow cadence."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx

    async def run_forever(self, stop: asyncio.Event) -> None:
        interval = self.ctx.settings.housekeeping_interval_s
        log.info("housekeeper started (every %ss)", interval)
        while not stop.is_set():
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("housekeeping pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)

    async def sweep_once(self, *, now: datetime | None = None) -> int:
        """One pass; returns how many rows it retired."""
        at = now or utcnow()
        uploads = await self.expire_uploads(now=at)
        async with self.ctx.db.session() as s:
            logins = await sweep_expired_logins(s, at=at)
        return uploads + logins

    async def expire_uploads(self, *, now: datetime) -> int:
        """Mark pending intents whose PUT expired ``expired`` and delete their objects."""
        async with self.ctx.db.session() as s:
            rows = (
                await s.execute(
                    update(m.Upload)
                    .where(m.Upload.status == "pending", m.Upload.expires_at < now)
                    .values(status="expired")
                    .returning(m.Upload.bucket, m.Upload.object_name)
                )
            ).all()
        for bucket, name in rows:
            with contextlib.suppress(NotFound):
                await self.ctx.store.delete(bucket, name)
        if rows:
            log.info("expired %d abandoned upload intent(s)", len(rows))
        return len(rows)
