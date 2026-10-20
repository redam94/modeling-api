"""Periodic clean-up of rows that would otherwise only ever be cleaned lazily (or never).

The orchestrator runs :meth:`Housekeeper.run_forever` every ``housekeeping_interval_s``:

* **upload intents** whose signed PUT expired while still ``pending``: an intent whose file
  arrived becomes ``uploaded`` (a resumable collection upload uses it hours later; it keeps
  counting against the storage quota), the others become ``expired``, so an abandoned upload
  (browser closed, CLI killed) never holds storage quota for long. Quota usage counts every
  ``pending`` intent until this sweep retires it (:func:`~mapi_server.services.quotas.
  quota_usage`): a file that arrived must not stop counting when its PUT expires. An
  ``uploaded`` file that no version used for :data:`UPLOAD_KEEP` becomes ``expired`` too,
  and the objects of expired intents are deleted from quarantine.

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
expired PUT can no longer be started, ``create_dataset_version`` refuses an upload whose
intent expired, and an expired session is refused anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from mapi_spec.storage import NotFound
from mapi_spec.tracking import SCALAR_STEP
from sqlalchemy import delete, select, text

from mapi_server.auth.identity import sweep_expired_logins
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import now as utcnow

__all__ = ["TRACKING_PRUNE_BATCH", "UPLOAD_EXPIRE_BATCH", "UPLOAD_KEEP", "Housekeeper"]

log = logging.getLogger(__name__)

UPLOAD_KEEP = timedelta(days=7)
"""How long an arrived upload that no dataset version used is kept after its PUT expired."""

UPLOAD_EXPIRE_BATCH = 500
"""Upload intents one pass checks (each needs a metadata read of its object)."""

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
        """Retire pending intents whose PUT expired (``uploaded`` when their file arrived,
        else ``expired``) and arrived files unused for :data:`UPLOAD_KEEP` (``expired``);
        deletes the objects of the expired ones."""
        gone: list[tuple[str, str]] = []
        due = select(m.Upload).where(
            ((m.Upload.status == "pending") & (m.Upload.expires_at < now))
            | ((m.Upload.status == "uploaded") & (m.Upload.expires_at < now - UPLOAD_KEEP))
        )
        async with self.ctx.db.session() as s:
            rows = list(
                (
                    await s.execute(
                        due.order_by(m.Upload.expires_at)
                        .limit(UPLOAD_EXPIRE_BATCH)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for row in rows:
                if row.status == "pending" and await self._arrived(row):
                    row.status = "uploaded"
                    continue
                row.status = "expired"
                gone.append((row.bucket, row.object_name))
        for bucket, name in gone:
            with contextlib.suppress(NotFound):
                await self.ctx.store.delete(bucket, name)
        if gone:
            log.info("expired %d abandoned upload intent(s)", len(gone))
        return len(rows)

    async def _arrived(self, row: m.Upload) -> bool:
        """Whether the file of ``row`` is in quarantine (a signed PUT that expired cannot
        start again, so an object there arrived in time)."""
        try:
            meta = await self.ctx.store.stat(row.bucket, row.object_name)
        except NotFound:
            return False
        return meta.size <= row.size_bytes
