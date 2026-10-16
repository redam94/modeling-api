"""Tenant deletion (plan §9 "Data lifecycle and privacy"; :mod:`mapi_spec.trust`).

**Request** (``POST /v1/tenants/{id}/deletion``, a tenant admin's signed-in session; the body
repeats the tenant id, and ``MAPI_DELETION_REAUTH_MAX_AGE_S`` can also demand a recent
sign-in): the deletion is ``scheduled`` with a grace period (``MAPI_TENANT_DELETION_GRACE_S``,
7 days). The tenant is suspended at once (read-only: no new runs, no spending) and its
unfinished runs are cancelled; it can still export its data and cancel the deletion.

**Start** (the orchestrator, once the grace period is over): the deletion becomes
``deleting``, irreversibly. The tenant's status becomes ``deleting`` (every request is
refused), its API tokens are revoked, the browser sessions of people who belong to no other
tenant are ended, unfinished runs are cancelled again, and every registered
:class:`~mapi_server.services.trust.TenantRevoker` runs (webhooks, embed links). The
``tenant.deletion.completed`` webhook targets are captured before anything is purged.

**Purge** (a procrastinate job, :class:`TenantPurger`), store by store with progress:

1. ``queue``: waits for the cancelled runs to stop (at most
   ``MAPI_DELETION_RUN_STOP_TIMEOUT_S``), deletes their pending background jobs and
   bootstrap objects;
2. ``database``: every table with a ``tenant_id`` column (discovered from the catalog, so
   tables added later are covered), children before parents, in batches, through the
   tenant's own row-level-security session where the request-path role may delete. Kept:
   ``audit_events``, ``cost_ledger``, this deletion's record and the tenant row itself as a
   tombstone (:data:`mapi_spec.trust.RETAINED_RECORDS`). People who belonged only to this
   tenant are deleted too; sign-up records keep only their keyed client digests (the Sybil
   look-back) and lose the e-mail, name and organisation;
3. ``object_store``: every object under ``t/{tenant}/`` in the quarantine, runs, bundles and
   data buckets, through :class:`~mapi_spec.storage.ObjectStore`. An object a retention lock
   refuses is recorded with the time it may be deleted (``time_created`` + the bundles
   retention, ``MAPI_BUNDLE_RETENTION_DAYS``), and the purge resumes then;
4. ``logs``: not purged. Request and run logs hold ids, not content, and expire with the
   log buckets' retention (documented in the data-deletion guide);
5. ``webhooks``: delivers ``tenant.deletion.completed`` through the configured
   :class:`~mapi_server.services.trust.DeletionNotifier`.

When every store is done the deletion is ``purged`` and the tombstone audit event
``tenant.purged`` is written (by tenant id, without content). A failing store makes the
deletion ``failed``; it is retried with back-off up to ``MAPI_DELETION_MAX_ATTEMPTS`` times,
then waits for an operator (``POST /v1/admin/tenants/{id}/deletion/retry``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction, AuditEvent, AuditOutcome, make_audit_event
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.storage import AccessDenied, BucketPurpose, NotFound, bootstrap_key
from mapi_spec.tenancy import ResourceKind, TenantStatus
from mapi_spec.trust import (
    RETAINED_RECORDS,
    DeletionState,
    DeletionStore,
    StoreProgress,
    TenantDeletion,
    TenantDeletionRequest,
    next_deletion_state,
)
from pydantic import JsonValue
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError, unauthenticated
from mapi_server.services.audit import write_audit
from mapi_server.services.gdpr_export import CANCELLED as EXPORT_CANCELLED
from mapi_server.services.gdpr_export import export_lease
from mapi_server.services.quotas import egress_subject
from mapi_server.services.runs import apply_transition
from mapi_server.services.runs import now as utcnow
from mapi_server.services.runspec import signed_links_ttl
from mapi_server.services.trust import trust_services

__all__ = [
    "DELETION_STORES",
    "OPEN_STATES",
    "RETAINED_TABLES",
    "TenantPurger",
    "cancel_deletion",
    "cancel_unfinished_runs",
    "deletion_dto",
    "latest_deletion",
    "request_deletion",
    "retry_deletion",
]

log = logging.getLogger(__name__)

DELETION_STORES: Final[tuple[DeletionStore, ...]] = (
    "queue",
    "database",
    "object_store",
    "logs",
    "webhooks",
)
OPEN_STATES: Final = (DeletionState.SCHEDULED, DeletionState.DELETING, DeletionState.FAILED)
RETAINED_TABLES: Final = frozenset({"audit_events", "cost_ledger", "tenant_deletions", "tenants"})
"""Tables with a ``tenant_id`` whose rows a purge keeps (see ``RETAINED_RECORDS``)."""
SCHEDULED_PREFIX: Final = "deletion scheduled"
PURGE_BUCKETS: Final = (
    BucketPurpose.QUARANTINE,
    BucketPurpose.RUNS,
    BucketPurpose.BUNDLES,
    BucketPurpose.DATA,
)
"""Buckets with ``t/{tenant}/`` prefixes (the bootstrap bucket is keyed by nonce)."""
BATCH: Final = 5000
RUN_WAIT: Final = timedelta(seconds=30)
LINK_SLACK: Final = timedelta(minutes=5)
"""Added to an attempt's link lifetime: its RunSpec is signed shortly after the attempt row."""
RETRY_BASE: Final = timedelta(minutes=5)
RETRY_CAP: Final = timedelta(hours=6)
MAX_RETAINED_LISTED: Final = 1000
LOGS_DETAIL: Final = (
    "Not purged: platform logs hold ids, not content, and expire with the log buckets' "
    "retention (30 days by default)."
)
_SKIP_CANCEL: Final = frozenset(
    {s.value for s in RunState if s.terminal}
    | {RunState.CANCELLING.value, RunState.FINALIZING.value}
)


# ===================================================================================== DTO


def _progress(row: m.TenantDeletion) -> list[StoreProgress]:
    stored = {p.get("store"): p for p in (row.stores or []) if isinstance(p, dict)}
    out = []
    for store in DELETION_STORES:
        doc = stored.get(store)
        out.append(
            StoreProgress.model_validate(doc)
            if doc
            else StoreProgress(store=store, state="pending")
        )
    return out


def deletion_dto(row: m.TenantDeletion) -> TenantDeletion:
    return TenantDeletion(
        id=row.id,
        tenant_id=row.tenant_id,
        state=DeletionState(row.state),
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        purge_after=row.purge_after,
        started_at=row.started_at,
        completed_at=row.completed_at,
        cancelled_at=row.cancelled_at,
        stores=_progress(row),
        retained=dict(RETAINED_RECORDS),
    )


def _set_store(
    row: m.TenantDeletion,
    store: DeletionStore,
    state: str,
    *,
    at: datetime,
    total: int | None = None,
    done: int | None = None,
    detail: str | None = None,
) -> None:
    progress = {p.store: p for p in _progress(row)}
    old = progress[store]
    progress[store] = StoreProgress(
        store=store,
        state=state,
        items_total=total if total is not None else old.items_total,
        items_done=done if done is not None else old.items_done,
        detail=detail[:500] if detail else None,
        updated_at=at,
    )
    row.stores = [progress[s].model_dump(mode="json") for s in DELETION_STORES]


def _store_state(row: m.TenantDeletion, store: DeletionStore) -> str:
    return next(p.state for p in _progress(row) if p.store == store)


# ================================================================================= request


async def latest_deletion(session: AsyncSession, tenant_id: str) -> m.TenantDeletion | None:
    return (
        await session.execute(
            select(m.TenantDeletion)
            .where(m.TenantDeletion.tenant_id == tenant_id)
            .order_by(m.TenantDeletion.requested_at.desc(), m.TenantDeletion.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def cancel_unfinished_runs(session: AsyncSession, tenant_id: str) -> list[str]:
    """Move the tenant's unfinished runs to ``cancelling`` (the reconciler stops their
    executions); ``finalizing`` runs are already over and finish on their own."""
    runs = (
        await session.execute(
            select(m.Run)
            .where(m.Run.tenant_id == tenant_id, m.Run.state.not_in(_SKIP_CANCEL))
            .order_by(m.Run.id)
            .with_for_update()
        )
    ).scalars()
    cancelled = []
    for run in list(runs):
        await apply_transition(session, run, RunTrigger.CANCEL, Actor.API)
        cancelled.append(run.id)
    return cancelled


async def _check_reauth(ctx: ServerContext, auth: RequestAuth) -> None:
    max_age = ctx.settings.deletion_reauth_max_age_s
    if max_age is None or auth.session_key is None:
        return
    async with ctx.db.session() as s:
        sess = await s.get(m.WebSession, auth.session_key)
    if sess is None or utcnow() - sess.created_at > timedelta(seconds=max_age):
        raise unauthenticated("sign in again to confirm the deletion of this tenant")


async def request_deletion(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    tenant_id: str,
    body: TenantDeletionRequest,
) -> m.TenantDeletion:
    """Schedule the deletion (tenant session of ``tenant_id``)."""
    if body.confirm != tenant_id:
        raise MapiError(
            f"type the tenant id ({tenant_id}) to confirm the deletion",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/confirm",
        )
    if tenant_id == ctx.platform_scope.tenant_id:
        raise Conflict("the platform's own tenant cannot be deleted")
    await _check_reauth(ctx, auth)
    tenant = await session.get(m.Tenant, tenant_id, with_for_update=True)
    if tenant is None:
        raise NotFoundError("tenant", tenant_id)
    open_row = (
        await session.execute(
            select(m.TenantDeletion.id).where(
                m.TenantDeletion.tenant_id == tenant_id,
                m.TenantDeletion.state.in_([s.value for s in OPEN_STATES]),
            )
        )
    ).first()
    if open_row is not None:
        raise Conflict(f"a deletion of tenant {tenant_id} is already in progress")
    at = utcnow()
    purge_after = at + timedelta(seconds=ctx.settings.tenant_deletion_grace_s)
    row = m.TenantDeletion(
        id=new_id("del"),
        tenant_id=tenant_id,
        state=DeletionState.SCHEDULED.value,
        previous_status=tenant.status,
        reason=body.reason,
        requested_by=auth.principal_id,
        requested_at=at,
        purge_after=purge_after,
        stores=[],
        retained_objects=[],
        notify=[],
        attempts=0,
        next_attempt_at=purge_after,
    )
    session.add(row)
    tenant.status = TenantStatus.SUSPENDED.value
    tenant.status_reason = f"{SCHEDULED_PREFIX}: purge after {purge_after.isoformat()}"
    tenant.updated_at = at
    await session.flush()
    cancelled = await cancel_unfinished_runs(session, tenant_id)
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.TENANT_DELETION_REQUEST,
                ResourceKind.TENANT_DELETION,
                resource_id=row.id,
                tenant_id=tenant_id,
                details={
                    "purge_after": purge_after.isoformat(),
                    "cancelled_runs": len(cancelled),
                    "reason": body.reason,
                },
            )
        ],
        session=session,
    )
    return row


async def cancel_deletion(
    session: AsyncSession, ctx: ServerContext, auth: RequestAuth, tenant_id: str
) -> m.TenantDeletion:
    """Cancel a scheduled deletion; the tenant gets back the status it had before (unless an
    operator changed it in the meantime)."""
    row = (
        await session.execute(
            select(m.TenantDeletion)
            .where(
                m.TenantDeletion.tenant_id == tenant_id,
                m.TenantDeletion.state.in_([s.value for s in OPEN_STATES]),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("deletion of tenant", tenant_id)
    state = next_deletion_state(DeletionState(row.state), "cancel")  # CONFLICT once started
    at = utcnow()
    row.state = state.value
    row.cancelled_at = at
    row.next_attempt_at = None
    tenant = await session.get(m.Tenant, tenant_id, with_for_update=True)
    if tenant is not None and (tenant.status_reason or "").startswith(SCHEDULED_PREFIX):
        tenant.status = row.previous_status
        tenant.status_reason = None
        tenant.updated_at = at
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.TENANT_DELETION_CANCEL,
                ResourceKind.TENANT_DELETION,
                resource_id=row.id,
                tenant_id=tenant_id,
            )
        ],
        session=session,
    )
    return row


async def retry_deletion(
    session: AsyncSession, ctx: ServerContext, auth: RequestAuth, tenant_id: str
) -> m.TenantDeletion:
    """An operator restarts a failed purge (gateway role; audited)."""
    row = (
        await session.execute(
            select(m.TenantDeletion)
            .where(
                m.TenantDeletion.tenant_id == tenant_id,
                m.TenantDeletion.state.in_([s.value for s in OPEN_STATES]),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("deletion of tenant", tenant_id)
    row.state = next_deletion_state(DeletionState(row.state), "retry").value
    row.attempts = 0
    row.next_attempt_at = utcnow()
    row.last_error = None
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.TENANT_DELETION_RETRY,
                ResourceKind.TENANT_DELETION,
                resource_id=row.id,
                tenant_id=tenant_id,
            )
        ],
        session=session,
    )
    return row


# =================================================================================== purge


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class TenantPurger:
    """Starts due deletions and purges their stores (orchestrator; platform role)."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------------- scheduling
    async def due(self, *, at: datetime | None = None) -> list[str]:
        """Ids of deletions with work to do now."""
        ts = at or utcnow()
        async with self.ctx.db.session() as s:
            rows = (
                await s.execute(
                    select(m.TenantDeletion.id, m.TenantDeletion.state, m.TenantDeletion.attempts)
                    .where(
                        m.TenantDeletion.state.in_([s.value for s in OPEN_STATES]),
                        (m.TenantDeletion.next_attempt_at.is_(None))
                        | (m.TenantDeletion.next_attempt_at <= ts),
                    )
                    .order_by(m.TenantDeletion.requested_at)
                )
            ).all()
        limit = self.ctx.settings.deletion_max_attempts
        return [r.id for r in rows if r.state != DeletionState.FAILED.value or r.attempts < limit]

    async def run(self, deletion_id: str, *, at: datetime | None = None) -> DeletionState:
        """One pass over a deletion: start it if due, then purge stores until one has to
        wait (runs stopping, a retention lock) or fails. Idempotent and safe to repeat."""
        ts = at or utcnow()
        async with self.ctx.db.session() as s:
            row = await s.get(m.TenantDeletion, deletion_id, with_for_update=True)
            if row is None:
                raise NotFoundError("deletion", deletion_id)
            state = DeletionState(row.state)
            if state is DeletionState.SCHEDULED:
                if row.purge_after > ts:
                    return state
                await self._start(s, row, ts)
            elif state is DeletionState.FAILED:
                if row.attempts >= self.ctx.settings.deletion_max_attempts:
                    return state
                row.state = next_deletion_state(state, "retry").value
            elif state is not DeletionState.DELETING:
                return state
            tenant_id = row.tenant_id
        for store in DELETION_STORES:
            async with self.ctx.db.session() as s:
                row = await s.get(m.TenantDeletion, deletion_id, with_for_update=True)
                assert row is not None
                if _store_state(row, store) == "done":
                    continue
            try:
                finished = await self._purge_store(deletion_id, tenant_id, store, ts)
            except Exception as exc:
                log.exception("purging %s of tenant %s failed", store, tenant_id)
                await self._failed(deletion_id, store, exc, ts)
                return DeletionState.FAILED
            if not finished:
                return DeletionState.DELETING
        return await self._complete(deletion_id, ts)

    # ----------------------------------------------------------------------------- start
    async def _start(self, s: AsyncSession, row: m.TenantDeletion, at: datetime) -> None:
        tenant_id = row.tenant_id
        row.state = next_deletion_state(DeletionState(row.state), "start").value
        row.started_at = at
        row.next_attempt_at = None
        row.stores = [
            StoreProgress(store=st, state="pending", updated_at=at).model_dump(mode="json")
            for st in DELETION_STORES
        ]
        tenant = await s.get(m.Tenant, tenant_id, with_for_update=True)
        if tenant is not None:
            tenant.status = TenantStatus.DELETING.value
            tenant.status_reason = f"deleting since {at.isoformat()}"
            tenant.updated_at = at
        tokens = await s.execute(
            update(m.ApiToken)
            .where(m.ApiToken.tenant_id == tenant_id, m.ApiToken.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        sole = await self._sole_members(s, tenant_id)
        sessions = await s.execute(delete(m.WebSession).where(m.WebSession.user_id.in_(sole)))
        cancelled = await cancel_unfinished_runs(s, tenant_id)
        # an export requested in the grace period must not write its archive after the purge
        exports = await s.execute(
            update(m.TenantExport)
            .where(
                m.TenantExport.tenant_id == tenant_id,
                m.TenantExport.state.in_(("queued", "running")),
            )
            .values(state="failed", error=EXPORT_CANCELLED)
        )
        trust = trust_services(self.ctx)
        revoked: dict[str, JsonValue] = {
            "tokens": int(getattr(tokens, "rowcount", 0) or 0),
            "sessions": int(getattr(sessions, "rowcount", 0) or 0),
            "runs": len(cancelled),
            "exports": int(getattr(exports, "rowcount", 0) or 0),
        }
        for revoker in trust.revokers:
            revoked[revoker.name] = await revoker.revoke(s, tenant_id, now=at)
        row.notify = list(await trust.notifier.capture(s, tenant_id))
        await self._audit(
            s, AuditAction.TENANT_DELETE, tenant_id, row.id, at,
            details={"revoked": revoked, "deletion_id": row.id},
        )  # fmt: skip
        log.warning("tenant %s: deletion %s started (%s)", tenant_id, row.id, revoked)

    async def _sole_members(self, s: AsyncSession, tenant_id: str) -> list[str]:
        """Users whose only memberships are in ``tenant_id`` (never operators)."""
        mine = select(m.Membership.principal_id).where(
            m.Membership.tenant_id == tenant_id, m.Membership.principal_kind == "user"
        )
        elsewhere = select(m.Membership.principal_id).where(m.Membership.tenant_id != tenant_id)
        operators = {e.strip().lower() for e in self.ctx.settings.effective_operator_emails()}
        rows = (
            await s.execute(
                select(m.User.id, m.User.email, m.User.operator).where(
                    m.User.id.in_(mine), m.User.id.not_in(elsewhere)
                )
            )
        ).all()
        return [
            r.id
            for r in rows
            if not r.operator and (r.email or "").strip().lower() not in operators
        ]

    # ---------------------------------------------------------------------------- stores
    async def _purge_store(
        self, deletion_id: str, tenant_id: str, store: DeletionStore, at: datetime
    ) -> bool:
        if store == "queue":
            return await self._purge_queue(deletion_id, tenant_id, at)
        if store == "database":
            return await self._purge_database(deletion_id, tenant_id, at)
        if store == "object_store":
            return await self._purge_objects(deletion_id, tenant_id, at)
        if store == "logs":
            await self._update(deletion_id, store, "done", at, total=0, done=0, detail=LOGS_DETAIL)
            return True
        return await self._notify(deletion_id, tenant_id, at)

    async def _update(
        self,
        deletion_id: str,
        store: DeletionStore,
        state: str,
        at: datetime,
        *,
        total: int | None = None,
        done: int | None = None,
        detail: str | None = None,
        next_attempt_at: datetime | None = None,
        retained: list[Any] | None = None,
    ) -> None:
        async with self.ctx.db.session() as s:
            row = await s.get(m.TenantDeletion, deletion_id, with_for_update=True)
            assert row is not None
            _set_store(row, store, state, at=at, total=total, done=done, detail=detail)
            row.next_attempt_at = next_attempt_at
            if retained is not None:
                row.retained_objects = retained

    async def _purge_queue(self, deletion_id: str, tenant_id: str, at: datetime) -> bool:
        async with self.ctx.db.session() as s:
            row = await s.get(m.TenantDeletion, deletion_id)
            assert row is not None
            started = row.started_at or at
            # anything submitted since the start (a retry racing the start) is cancelled too
            await cancel_unfinished_runs(s, tenant_id)
            unfinished = (
                await s.execute(
                    select(func.count()).where(
                        m.Run.tenant_id == tenant_id,
                        m.Run.state.not_in([st.value for st in RunState if st.terminal]),
                    )
                )
            ).scalar_one()
            # cancelled exports whose build may still upload an archive (within its lease)
            building = (
                await s.execute(
                    select(func.count()).where(
                        m.TenantExport.tenant_id == tenant_id,
                        m.TenantExport.error == EXPORT_CANCELLED,
                        m.TenantExport.started_at > at - export_lease(self.ctx.settings),
                    )
                )
            ).scalar_one()
        if building:
            await self._update(
                deletion_id, "queue", "running", at,
                detail=f"waiting for {building} cancelled export build(s) to end",
                next_attempt_at=at + RUN_WAIT,
            )  # fmt: skip
            return False
        timeout = timedelta(seconds=self.ctx.settings.deletion_run_stop_timeout_s)
        if unfinished and at - started < timeout:
            await self._update(
                deletion_id, "queue", "running", at,
                detail=f"waiting for {unfinished} cancelled run(s) to stop",
                next_attempt_at=at + RUN_WAIT,
            )  # fmt: skip
            return False
        if unfinished:
            # A run that ignored its cancel may still be executing, and its attempts' signed
            # upload policies write under t/{tenant}/: purge nothing until they expired.
            links_expire = await self._upload_links_expire(tenant_id)
            if links_expire is not None and at < links_expire:
                await self._update(
                    deletion_id, "queue", "running", at,
                    detail=f"{unfinished} run(s) have not stopped; the purge resumes when "
                    f"their upload links expire ({links_expire.isoformat(timespec='minutes')})",
                    next_attempt_at=links_expire,
                )  # fmt: skip
                return False
        async with self.ctx.db.session() as s:
            run_ids = list(
                (await s.execute(select(m.Run.id).where(m.Run.tenant_id == tenant_id))).scalars()
            )
            nonces = list(
                (
                    await s.execute(
                        select(m.RunAttempt.nonce).where(m.RunAttempt.tenant_id == tenant_id)
                    )
                ).scalars()
            )
            jobs = 0
            if run_ids:
                res = await s.execute(
                    text(
                        "DELETE FROM procrastinate_jobs WHERE status = 'todo' "
                        "AND args->>'run_id' = ANY(:ids)"
                    ),
                    {"ids": run_ids},
                )
                jobs = int(getattr(res, "rowcount", 0) or 0)
        removed = 0
        for nonce in nonces:
            try:
                await self.ctx.store.delete(self.ctx.buckets.bootstrap, bootstrap_key(nonce))
                removed += 1
            except (NotFound, ValueError):
                continue
        detail = None
        if unfinished:
            detail = f"{unfinished} run(s) had not stopped after {int(timeout.total_seconds())} s"
        await self._update(
            deletion_id, "queue", "done", at, total=jobs + len(nonces), done=jobs + removed,
            detail=detail,
        )  # fmt: skip
        return True

    async def _upload_links_expire(self, tenant_id: str) -> datetime | None:
        """When the last signed link of the tenant's unfinished runs' attempts expires (their
        RunSpec is built when the attempt is dispatched; plus :data:`LINK_SLACK`)."""
        async with self.ctx.db.session() as s:
            rows = (
                await s.execute(
                    select(m.RunAttempt.created_at, m.Run.timeout_s)
                    .join(m.Run, m.Run.id == m.RunAttempt.run_id)
                    .where(
                        m.Run.tenant_id == tenant_id,
                        m.Run.state.not_in([st.value for st in RunState if st.terminal]),
                    )
                )
            ).all()
        ends = [
            created + signed_links_ttl(int(timeout_s)) + LINK_SLACK for created, timeout_s in rows
        ]
        return max(ends) if ends else None

    async def _tables(self, s: AsyncSession) -> list[str]:
        """Tables with a ``tenant_id`` column, children before parents (foreign keys)."""
        names = (
            set(
                (
                    await s.execute(
                        text(
                            "SELECT c.table_name FROM information_schema.columns c "
                            "JOIN information_schema.tables t ON t.table_schema = c.table_schema "
                            "AND t.table_name = c.table_name "
                            "WHERE c.table_schema = current_schema() "
                            "AND c.column_name = 'tenant_id' "
                            "AND t.table_type = 'BASE TABLE'"
                        )
                    )
                ).scalars()
            )
            - RETAINED_TABLES
        )
        edges = (
            await s.execute(
                text(
                    "SELECT cl.relname AS child, pr.relname AS parent FROM pg_constraint k "
                    "JOIN pg_class cl ON cl.oid = k.conrelid "
                    "JOIN pg_class pr ON pr.oid = k.confrelid "
                    "JOIN pg_namespace n ON n.oid = cl.relnamespace "
                    "WHERE k.contype = 'f' AND n.nspname = current_schema()"
                )
            )
        ).all()
        parents: dict[str, set[str]] = {n: set() for n in names}
        for child, parent in edges:
            if child in names and parent in names and child != parent:
                parents[child].add(parent)
        # Kahn's algorithm on the reversed graph: a table goes once nothing references it
        children: dict[str, set[str]] = {n: set() for n in names}
        for child, ps in parents.items():
            for p in ps:
                children[p].add(child)
        order: list[str] = []
        pending = dict(children)
        while pending:
            ready = sorted(t for t, cs in pending.items() if not (cs & pending.keys()))
            if not ready:  # a cycle: delete the rest in name order and let cascades work
                ready = sorted(pending)
            for t in ready:
                order.append(t)
                del pending[t]
        return order

    async def _app_may_delete(self, s: AsyncSession, table: str) -> bool:
        role = self.ctx.db.app_role
        return bool(
            (
                await s.execute(
                    text("SELECT has_table_privilege(:role, :table, 'DELETE')"),
                    {"role": role, "table": table},
                )
            ).scalar_one()
        )

    async def _purge_database(self, deletion_id: str, tenant_id: str, at: datetime) -> bool:
        async with self.ctx.db.session() as s:
            tables = await self._tables(s)
            counts: dict[str, int] = {}
            for table in tables:
                n = (
                    await s.execute(
                        text(f"SELECT count(*) FROM {_quote(table)} WHERE tenant_id = :t"),
                        {"t": tenant_id},
                    )
                ).scalar_one()
                counts[table] = int(n)
            through_rls = {t: await self._app_may_delete(s, t) for t in tables}
            sole = await self._sole_members(s, tenant_id)
            tokens = list(
                (
                    await s.execute(select(m.ApiToken.id).where(m.ApiToken.tenant_id == tenant_id))
                ).scalars()
            )
        total = sum(counts.values()) + len(sole)
        done = 0
        await self._update(deletion_id, "database", "running", at, total=total, done=0)
        for table in tables:
            if not counts[table]:
                continue
            stmt = text(
                f"DELETE FROM {_quote(table)} WHERE ctid = ANY(ARRAY("
                f"SELECT ctid FROM {_quote(table)} WHERE tenant_id = :t LIMIT {BATCH}))"
            )
            while True:
                session = (
                    self.ctx.db.tenant_session(tenant_id)
                    if through_rls[table]
                    else self.ctx.db.session()
                )
                async with session as s:
                    res = await s.execute(stmt, {"t": tenant_id})
                    n = int(getattr(res, "rowcount", 0) or 0)
                done += n
                if n < BATCH:
                    break
            await self._update(deletion_id, "database", "running", at, total=total, done=done)
        async with self.ctx.db.session() as s:
            subjects = [
                f"tenant:{tenant_id}",
                egress_subject(tenant_id),
                *(f"token:{t}" for t in tokens),
            ]
            await s.execute(delete(m.RateCounter).where(m.RateCounter.subject.in_(subjects)))
            await s.execute(
                delete(m.QuotaPolicy).where(
                    ((m.QuotaPolicy.scope == "tenant") & (m.QuotaPolicy.subject_id == tenant_id))
                    | ((m.QuotaPolicy.scope == "token") & (m.QuotaPolicy.subject_id.in_(tokens)))
                )
            )
            await s.execute(
                update(m.Signup)
                .where(m.Signup.created_tenant_id == tenant_id)
                .values(email="erased@" + m.Signup.email_domain, name="", organisation=None)
            )
            users = await s.execute(delete(m.User).where(m.User.id.in_(sole)))
            done += int(getattr(users, "rowcount", 0) or 0)
            tenant = await s.get(m.Tenant, tenant_id, with_for_update=True)
            if tenant is not None:  # the tombstone: the id stays taken, nothing else remains
                tenant.name = tenant_id
                tenant.updated_at = at
        await self._update(
            deletion_id, "database", "done", at, total=total, done=done,
            detail=f"{len([t for t in tables if counts[t]])} table(s); audit and cost records "
            "kept (see retained)",
        )  # fmt: skip
        return True

    def _retry_after(self, bucket: str, created: datetime, at: datetime) -> datetime:
        if bucket == self.ctx.buckets.bundles:
            when = created + timedelta(days=self.ctx.settings.bundle_retention_days)
            return max(when, at + timedelta(minutes=5))
        return at + timedelta(days=1)

    async def _purge_objects(self, deletion_id: str, tenant_id: str, at: datetime) -> bool:
        store = self.ctx.store
        prefix = f"t/{tenant_id}/"
        deleted = 0
        retained: list[dict[str, Any]] = []
        retained_n = 0
        earliest: datetime | None = None
        for purpose in PURGE_BUCKETS:
            bucket = self.ctx.buckets.all()[purpose]
            token: str | None = None
            while True:
                try:
                    page = await store.list(bucket, prefix=prefix, page_token=token)
                except NotFound:  # the bucket does not exist in this deployment
                    break
                for obj in page.items:
                    try:
                        await store.delete(bucket, obj.name)
                        deleted += 1
                    except NotFound:
                        continue
                    except AccessDenied:
                        when = self._retry_after(bucket, obj.time_created, at)
                        earliest = when if earliest is None else min(earliest, when)
                        retained_n += 1
                        if len(retained) < MAX_RETAINED_LISTED:
                            retained.append(
                                {
                                    "bucket": bucket,
                                    "name": obj.name,
                                    "retry_after": when.isoformat(),
                                }
                            )
                token = page.next_page_token
                if not token:
                    break
            await self._update(
                deletion_id, "object_store", "running", at, total=deleted + retained_n,
                done=deleted,
            )  # fmt: skip
        if retained_n:
            assert earliest is not None
            await self._update(
                deletion_id, "object_store", "running", at, total=deleted + retained_n,
                done=deleted, retained=retained, next_attempt_at=earliest,
                detail=f"{retained_n} object(s) are under a retention lock until "
                f"{earliest.date().isoformat()}; the purge resumes then",
            )  # fmt: skip
            return False
        await self._update(
            deletion_id, "object_store", "done", at, total=deleted, done=deleted, retained=[]
        )
        return True

    async def _notify(self, deletion_id: str, tenant_id: str, at: datetime) -> bool:
        async with self.ctx.db.session() as s:
            row = await s.get(m.TenantDeletion, deletion_id)
            assert row is not None
            captured: Sequence[JsonValue] = list(row.notify or [])
        notifier = trust_services(self.ctx).notifier
        sent = await notifier.deliver(tenant_id, deletion_id, captured, now=at)
        detail = None if notifier.name != "none" else "no webhook service is configured"
        async with self.ctx.db.session() as s:
            row = await s.get(m.TenantDeletion, deletion_id, with_for_update=True)
            assert row is not None
            row.notify = []
            _set_store(row, "webhooks", "done", at=at, total=sent, done=sent, detail=detail)
        return True

    # ---------------------------------------------------------------------------- outcome
    async def _failed(
        self, deletion_id: str, store: DeletionStore, exc: Exception, at: datetime
    ) -> None:
        async with self.ctx.db.session() as s:
            row = await s.get(m.TenantDeletion, deletion_id, with_for_update=True)
            assert row is not None
            row.state = next_deletion_state(DeletionState(row.state), "store_failed").value
            row.attempts += 1
            delay = min(RETRY_BASE * (2 ** (row.attempts - 1)), RETRY_CAP)
            row.next_attempt_at = at + delay
            row.last_error = f"{store}: {type(exc).__name__}"[:500]
            limit = self.ctx.settings.deletion_max_attempts
            detail = f"failed ({type(exc).__name__}); attempt {row.attempts} of {limit}"
            _set_store(row, store, "failed", at=at, detail=detail)

    async def _complete(self, deletion_id: str, at: datetime) -> DeletionState:
        async with self.ctx.db.session() as s:
            row = await s.get(m.TenantDeletion, deletion_id, with_for_update=True)
            assert row is not None
            if row.state == DeletionState.PURGED.value:
                return DeletionState.PURGED
            row.state = next_deletion_state(DeletionState(row.state), "all_done").value
            row.completed_at = at
            row.next_attempt_at = None
            tenant = await s.get(m.Tenant, row.tenant_id, with_for_update=True)
            if tenant is not None:
                tenant.status_reason = f"deleted on {at.date().isoformat()}"
            counts: dict[str, JsonValue] = {p.store: p.items_done for p in _progress(row)}
            await self._audit(
                s, AuditAction.TENANT_PURGED, row.tenant_id, row.id, at,
                details={"deletion_id": row.id, "items": counts},
            )  # fmt: skip
            log.warning("tenant %s purged (deletion %s)", row.tenant_id, row.id)
            return DeletionState.PURGED

    async def _audit(
        self,
        s: AsyncSession,
        action: AuditAction,
        tenant_id: str,
        resource_id: str,
        at: datetime,
        *,
        details: dict[str, JsonValue],
    ) -> None:
        ev: AuditEvent = make_audit_event(
            action=action,
            resource_kind=ResourceKind.TENANT_DELETION,
            outcome=AuditOutcome.SUCCESS,
            ts=at,
            fingerprint_key=self.ctx.secrets.audit_key,
            tenant_id=tenant_id,
            resource_id=resource_id,
            details=details,
        )
        await write_audit(self.ctx.db, [ev], session=s)
