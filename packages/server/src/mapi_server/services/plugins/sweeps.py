"""Housekeeping of uploaded plugins (v0.19, D5, D8, D12, D15).

:meth:`PluginSweeps.sweep` runs on the housekeeper's cadence, on the platform role:

* **tickets** (:meth:`~PluginSweeps.expire_tickets`): an upload ticket still ``pending``
  after it expired becomes ``expired`` and its quarantine object (if any arrived) is deleted;
* **stuck checks** (:meth:`~PluginSweeps.reconcile`): a version still ``inspecting`` or
  ``describing`` whose check run is over without a result ends its check (the finalizer
  and the dispatcher normally do this at once; this catches an end they missed);
* **rekit** (:meth:`~PluginSweeps.rekit`, D12): a mountable version with no describe result
  for the current base image digest gets one ``plugin_describe`` run, within the tenant's
  pipeline concurrency cap;
* **purge** (:meth:`~PluginSweeps.purge`, D15): a refused, failed, rejected or withdrawn
  version past its ``purge_after`` loses its objects (quarantine copy and overlay copy; an
  object the quarantine lifecycle already deleted counts as purged) and becomes ``purged``.
  The row and its audit trail stay.

The base image digest is the control plane's catalog key (``Settings.resolve_image_digest``),
not a docker image id.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta
from typing import Final

from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.errors import ErrorCode, MapiError, Problem
from mapi_spec.executor import PLUGIN_KINDS, RunKind
from mapi_spec.plugins import MOUNTABLE_STATES
from mapi_spec.run import RunState
from mapi_spec.storage import NotFound
from mapi_spec.tenancy import PrincipalKind, ResourceKind
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.audit import add_audit
from mapi_server.services.plugins.finalize import fail_check, plugin_run_ended
from mapi_server.services.plugins.pipeline import (
    LABEL_REASON,
    LABEL_VERSION,
    describe_pending,
    start_describe,
)
from mapi_server.services.plugins.store import set_state

__all__ = ["PURGEABLE_STATES", "SWEEP_BATCH", "PluginSweeps"]

log = logging.getLogger(__name__)

SWEEP_BATCH: Final = 200
"""Rows one sweep looks at per pass (a backlog takes more passes)."""

PURGEABLE_STATES: Final = ("inspect_failed", "kit_failed", "rejected", "yanked")
"""States whose objects the purge deletes once ``purge_after`` has passed."""

_LIVE_STATES: Final = tuple(s.value for s in RunState if not s.terminal)
_PLUGIN_KINDS: Final = tuple(k.value for k in PLUGIN_KINDS)


class PluginSweeps:
    """The plugin sweeps of one housekeeping pass (see the module docstring)."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx

    async def sweep(self, now: datetime) -> int:
        """Run every plugin sweep once; returns how many rows they changed. One failing
        sweep does not stop the others (it is logged)."""
        changed = 0
        for name in ("expire_tickets", "reconcile", "rekit", "purge"):
            try:
                changed += await getattr(self, name)(now)
            except Exception:
                log.exception("plugin sweep %s failed", name)
        return changed

    # ------------------------------------------------------------------------- tickets
    async def expire_tickets(self, now: datetime) -> int:
        """``pending`` tickets past ``expires_at`` → ``expired`` (their object is deleted)."""
        gone: list[str] = []
        async with self.ctx.db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(m.PluginUpload)
                        .where(m.PluginUpload.state == "pending", m.PluginUpload.expires_at < now)
                        .order_by(m.PluginUpload.expires_at)
                        .limit(SWEEP_BATCH)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for row in rows:
                row.state = "expired"
                gone.append(row.object_key)
        for key in gone:
            with contextlib.suppress(NotFound):
                await self.ctx.store.delete(self.ctx.buckets.quarantine, key)
        if rows:
            log.info("expired %d plugin upload ticket(s)", len(rows))
        return len(rows)

    # ---------------------------------------------------------------------- stuck checks
    async def reconcile(self, now: datetime) -> int:
        """End the check of versions whose check run ended without a result (or never got
        one, after twice the describe timeout)."""
        stale = now - timedelta(seconds=2 * self.ctx.settings.plugin_describe_timeout_s)
        ended = 0
        async with self.ctx.db.session() as s:
            versions = list(
                (
                    await s.execute(
                        select(m.PluginVersion)
                        .where(m.PluginVersion.state.in_(("inspecting", "describing")))
                        .order_by(m.PluginVersion.updated_at)
                        .limit(SWEEP_BATCH)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for version in versions:
                run = await self._check_run(s, version)
                try:
                    async with s.begin_nested():
                        if run is None:
                            if version.updated_at >= stale:
                                continue
                            await fail_check(
                                s,
                                self.ctx,
                                version,
                                Problem.from_code(
                                    ErrorCode.INFRA_ERROR,
                                    "The platform lost the check run. Raise the version number "
                                    "and upload it again.",
                                ),
                            )
                        elif RunState(run.state).terminal and run.state != "succeeded":
                            await plugin_run_ended(s, self.ctx, run)
                        else:
                            continue
                except MapiError:
                    log.exception("could not end the check of plugin version %s", version.id)
                    continue
                ended += 1
        return ended

    async def _check_run(self, s: AsyncSession, version: m.PluginVersion) -> m.Run | None:
        """The run checking ``version`` now (its inspect run, or its newest upload describe)."""
        if version.state == "inspecting":
            if version.inspect_run_id is None:
                return None
            return await s.get(m.Run, version.inspect_run_id)
        return (
            await s.execute(
                select(m.Run)
                .where(
                    m.Run.tenant_id == version.tenant_id,
                    m.Run.kind == RunKind.PLUGIN_DESCRIBE.value,
                    m.Run.labels[LABEL_VERSION].astext == version.id,
                    m.Run.labels[LABEL_REASON].astext == "upload",
                )
                .order_by(m.Run.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    # ----------------------------------------------------------------------------- rekit
    async def rekit(self, now: datetime) -> int:
        """Queue a ``plugin_describe`` for every mountable version with no result for the
        current base image digest (one per version and digest; within each tenant's
        pipeline concurrency cap, counting the checks already queued or running)."""
        digest = self.ctx.catalogs.image_digest
        cap = self.ctx.settings.plugin_pipeline_concurrency_per_tenant
        queued = 0
        async with self.ctx.db.session() as s:
            described = select(m.PluginDescribe.version_id).where(
                m.PluginDescribe.version_id == m.PluginVersion.id,
                m.PluginDescribe.base_image_digest == digest,
            )
            versions = list(
                (
                    await s.execute(
                        select(m.PluginVersion)
                        .where(
                            m.PluginVersion.state.in_(sorted(MOUNTABLE_STATES)),
                            m.PluginVersion.overlay_key.is_not(None),
                            ~described.exists(),
                        )
                        .order_by(m.PluginVersion.updated_at)
                        .limit(SWEEP_BATCH)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            if not versions:
                return 0
            busy = {
                str(t): int(n)
                for t, n in (
                    await s.execute(
                        select(m.Run.tenant_id, func.count())
                        .where(m.Run.kind.in_(_PLUGIN_KINDS), m.Run.state.in_(_LIVE_STATES))
                        .group_by(m.Run.tenant_id)
                    )
                ).all()
            }
            for version in versions:
                if busy.get(version.tenant_id, 0) >= cap:
                    continue
                if await describe_pending(s, version.id, digest) is not None:
                    continue
                try:
                    async with s.begin_nested():
                        await start_describe(
                            s, self.ctx, version, created_by=version.uploaded_by, reason="rekit"
                        )
                except MapiError as exc:
                    log.warning(
                        "rekit of plugin version %s was not queued: %s", version.id, exc.detail
                    )
                    continue
                busy[version.tenant_id] = busy.get(version.tenant_id, 0) + 1
                queued += 1
        if queued:
            log.info("queued %d plugin rekit describe run(s) for %s", queued, digest)
        return queued

    # ----------------------------------------------------------------------------- purge
    async def purge(self, now: datetime) -> int:
        """Delete the objects of versions past ``purge_after`` and mark them ``purged``."""
        store, buckets = self.ctx.store, self.ctx.buckets
        purged = 0
        async with self.ctx.db.session() as s:
            versions = list(
                (
                    await s.execute(
                        select(m.PluginVersion)
                        .where(
                            m.PluginVersion.state.in_(PURGEABLE_STATES),
                            m.PluginVersion.purged_at.is_(None),
                            m.PluginVersion.purge_after.is_not(None),
                            m.PluginVersion.purge_after <= now,
                        )
                        .order_by(m.PluginVersion.purge_after)
                        .limit(SWEEP_BATCH)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for version in versions:
                # the quarantine lifecycle (7 days) may have deleted the upload already
                with contextlib.suppress(NotFound):
                    await store.delete(buckets.quarantine, version.quarantine_key)
                if version.overlay_key is not None:
                    with contextlib.suppress(NotFound):
                        await store.delete(buckets.plugins, version.overlay_key)
                previous = version.state
                set_state(version, "purged", now=now)
                await add_audit(
                    s,
                    [
                        make_audit_event(
                            action=AuditAction.PLUGIN_PURGE,
                            resource_kind=ResourceKind.PLUGIN,
                            outcome=AuditOutcome.SUCCESS,
                            ts=now,
                            fingerprint_key=b"",  # no client data to fingerprint
                            tenant_id=version.tenant_id,
                            project_id=version.project_id,
                            actor_kind=PrincipalKind.SERVICE,
                            actor_id="svc_housekeeper",
                            resource_id=version.id,
                            details={
                                "plugin_id": version.plugin_id,
                                "plugin_version_id": version.id,
                                "previous_state": previous,
                                "upload_sha256": version.upload_sha256,
                            },
                        )
                    ],
                )
                purged += 1
        if purged:
            log.info("purged the objects of %d plugin version(s)", purged)
        return purged
