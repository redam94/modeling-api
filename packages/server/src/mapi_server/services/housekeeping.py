"""Periodic clean-up of rows that would otherwise only ever be cleaned lazily (or never).

The orchestrator runs :meth:`Housekeeper.run_forever` every ``housekeeping_interval_s``:

* **upload intents** whose signed PUT expired while still ``pending`` become ``expired``
  and whatever reached the quarantine bucket is deleted, so an abandoned upload (browser
  closed, CLI killed) never holds storage quota or quarantine space for long. Quota usage
  already ignores them once their PUT has expired (:func:`~mapi_server.services.quotas.
  quota_usage`); this makes the row say so.

* **browser sessions** past their absolute or idle expiry, and sign-ins abandoned between
  ``/auth/login`` and ``/auth/callback``, are deleted (a request with an expired cookie
  deletes its own row; this catches the cookies that never come back);
* **rate counters** of past windows (each subject's own old windows are only deleted when
  it is counted again, and failure counters of unknown token prefixes never are);
* **run tracking points** (``MAPI_TRACKING_RETENTION_DAYS``, off by default): metric points
  and ``user`` log lines of runs that finished longer ago than the retention are deleted, at
  most :data:`TRACKING_PRUNE_BATCH` rows per table and pass. Metric summaries, params, tags,
  names and notes stay, so finished runs can still be searched and compared.

It runs on the platform role and touches nothing a request could still be using: an
expired PUT can no longer be completed, ``create_dataset_version`` refuses an upload
whose intent expired, and an expired session is refused anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from mapi_spec.storage import NotFound
from mapi_spec.tracking import SCALAR_STEP
from sqlalchemy import delete, text, update

from mapi_server.auth.identity import sweep_expired_logins
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import now as utcnow

__all__ = ["TRACKING_PRUNE_BATCH", "Housekeeper"]

log = logging.getLogger(__name__)

TRACKING_PRUNE_BATCH = 10_000
"""Rows of one table the tracking retention deletes per pass (a backlog takes more passes)."""

_PRUNE_SQL = {
    # the point of a metric without a step is its only value: it stays
    "run_metrics": f"AND step <> {SCALAR_STEP}",
    "run_logs": "AND stream = 'user'",
}


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
        tracking = await self.prune_tracking(now=at)
        async with self.ctx.db.session() as s:
            logins = await sweep_expired_logins(s, at=at)
            # fixed one-minute windows: anything older than the previous one is dead weight
            # (failure counters of random token prefixes are never touched again)
            stale = await s.execute(
                delete(m.RateCounter).where(m.RateCounter.window_start < at - timedelta(minutes=2))
            )
        return uploads + logins + tracking + int(getattr(stale, "rowcount", 0) or 0)

    async def prune_tracking(self, *, now: datetime) -> int:
        """Delete metric points (not the one point of a metric without a step) and user log
        lines of runs finished before the retention. The summaries stay."""
        days = self.ctx.settings.tracking_retention_days
        if days is None:
            return 0
        cutoff = now - timedelta(days=days)
        deleted = 0
        async with self.ctx.db.session() as s:
            for table, extra in _PRUNE_SQL.items():
                # one bounded batch per table and pass (ctid: no primary-key tuple needed)
                res = await s.execute(
                    text(
                        f"DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM {table} "
                        "WHERE run_id IN (SELECT id FROM runs WHERE finished_at < :cutoff) "
                        f"{extra} LIMIT :n)"
                    ),
                    {"cutoff": cutoff, "n": TRACKING_PRUNE_BATCH},
                )
                deleted += int(getattr(res, "rowcount", 0) or 0)
        if deleted:
            log.info("pruned %d run tracking row(s) past the retention", deleted)
        return deleted

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
