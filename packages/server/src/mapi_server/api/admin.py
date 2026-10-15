"""Operator endpoints (``/v1/admin``): tenants, quota policies, kill switches, executor
health and the platform audit log.

Only platform operators pass the route checks (``MAPI_OPERATOR_EMAILS``, signed in with a
browser session; API tokens never carry operator powers). These routes read across tenants,
so they use the platform role, always with explicit filters. Every change is audited.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status
from mapi_spec.admin import (
    ExecutorHealth,
    ExecutorStatus,
    ExecutorUpdate,
    KillSwitches,
    KillSwitchUpdate,
)
from mapi_spec.api import Page, TenantAdminUpdate, TenantCreate
from mapi_spec.audit import AuditAction, AuditEvent, AuditOutcome, AuditQuery
from mapi_spec.executor import ExecutorCaps
from mapi_spec.quotas import QuotaPolicy, QuotaPolicyUpsert, QuotaScope
from mapi_spec.run import Actor, FailureClass, RunState, RunTrigger
from mapi_spec.tenancy import ProjectRole, ResourceKind, Tenant, TenantStatus, TrustTier
from pydantic import AwareDatetime, JsonValue
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Limit,
    SystemSession,
    decode_cursor,
    encode_cursor,
)
from mapi_server.api.tenancy import audit_page, tenant_dto
from mapi_server.auth.identity import invite_user
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.audit import write_audit
from mapi_server.services.quotas import (
    DISPATCHED_STATES,
    KILL_SWITCHES_KEY,
    kill_switches,
    policy_dto,
)
from mapi_server.services.runs import apply_transition, now
from mapi_server.services.tenancy import grant

__all__ = ["executor_health", "router"]

router = APIRouter(prefix="/v1/admin", tags=["admin"])

Slug = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")]
STALE_REPORT = timedelta(minutes=2)
"""An executor whose orchestrator has not reported for this long is shown ``down``."""


# ================================================================================== tenants


@router.get("/tenants", response_model=Page[Tenant], operation_id="admin_list_tenants")
async def admin_list_tenants(
    system: SystemSession,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_: Annotated[TenantStatus | None, Query(alias="status")] = None,
    trust_tier: TrustTier | None = None,
) -> Page[Tenant]:
    stmt = select(m.Tenant)
    if status_ is not None:
        stmt = stmt.where(m.Tenant.status == status_.value)
    if trust_tier is not None:
        stmt = stmt.where(m.Tenant.trust_tier == trust_tier.value)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.Tenant.id > str(after))
    rows = list((await system.execute(stmt.order_by(m.Tenant.id).limit(limit + 1))).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[Tenant](
        items=[tenant_dto(t) for t in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.post(
    "/tenants",
    response_model=Tenant,
    status_code=status.HTTP_201_CREATED,
    operation_id="admin_create_tenant",
)
async def admin_create_tenant(
    ctx: Ctx, auth: Auth, system: SystemSession, body: TenantCreate
) -> Tenant:
    """Create a tenant; ``initial_admin_email`` is invited as its tenant admin."""
    if await system.get(m.Tenant, body.id) is not None:
        raise Conflict(f"tenant {body.id} already exists")
    ts = now()
    row = m.Tenant(
        id=body.id,
        name=body.name,
        trust_tier=body.trust_tier.value,
        status=TenantStatus.ACTIVE.value,
        data_residency=body.data_residency,
        created_at=ts,
        updated_at=ts,
    )
    system.add(row)
    await system.flush()
    if body.initial_admin_email is not None:
        user = await invite_user(system, body.initial_admin_email)
        await grant(
            system,
            tenant_id=body.id,
            project_id=None,
            user=user,
            role=ProjectRole.ADMIN,
            created_by=auth.user_id,
        )
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.TENANT_CREATE,
                ResourceKind.TENANT,
                resource_id=body.id,
                tenant_id=body.id,
                details={"trust_tier": body.trust_tier.value, "residency": body.data_residency},
            )
        ],
        session=system,
    )
    return tenant_dto(row)


async def _tenant(system: AsyncSession, tenant_id: str, *, lock: bool = False) -> m.Tenant:
    row = await system.get(m.Tenant, tenant_id, with_for_update=lock)
    if row is None:
        raise NotFoundError("tenant", tenant_id)
    return row


@router.get("/tenants/{tenant_id}", response_model=Tenant, operation_id="admin_get_tenant")
async def admin_get_tenant(system: SystemSession, tenant_id: Slug) -> Tenant:
    return tenant_dto(await _tenant(system, tenant_id))


_STATUS_ACTIONS = {
    TenantStatus.SUSPENDED: AuditAction.TENANT_SUSPEND,
    TenantStatus.ACTIVE: AuditAction.TENANT_REACTIVATE,
    TenantStatus.DELETING: AuditAction.TENANT_DELETE,
}


@router.patch("/tenants/{tenant_id}", response_model=Tenant, operation_id="admin_update_tenant")
async def admin_update_tenant(
    ctx: Ctx, auth: Auth, system: SystemSession, tenant_id: Slug, body: TenantAdminUpdate
) -> Tenant:
    """Change a tenant's name, tier, status or residency. Suspended tenants are read-only;
    ``deleting`` tenants are inaccessible (their data is removed by a separate procedure)."""
    row = await _tenant(system, tenant_id, lock=True)
    action = AuditAction.TENANT_UPDATE
    changed: dict[str, JsonValue] = {}
    if body.name is not None:
        row.name = body.name
        changed["name"] = body.name
    if body.trust_tier is not None:
        row.trust_tier = body.trust_tier.value
        changed["trust_tier"] = body.trust_tier.value
    if body.data_residency is not None:
        row.data_residency = body.data_residency
        changed["data_residency"] = body.data_residency
    if body.status is not None and body.status.value != row.status:
        row.status = body.status.value
        changed["status"] = body.status.value
        action = _STATUS_ACTIONS[body.status]
    if "status_reason" in body.model_fields_set or body.status is not None:
        row.status_reason = body.status_reason
    row.updated_at = now()
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                action,
                ResourceKind.TENANT,
                resource_id=tenant_id,
                tenant_id=tenant_id,
                details={"changed": changed, "reason": body.status_reason},
            )
        ],
        session=system,
    )
    return tenant_dto(row)


# =================================================================================== quotas


@router.get("/quotas", response_model=Page[QuotaPolicy], operation_id="admin_list_quotas")
async def admin_list_quotas(
    system: SystemSession,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    scope: QuotaScope | None = None,
    subject_id: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[QuotaPolicy]:
    """Stored policies (tier defaults apply where none is stored)."""
    stmt = select(m.QuotaPolicy)
    if scope is not None:
        stmt = stmt.where(m.QuotaPolicy.scope == scope.value)
    if subject_id is not None:
        stmt = stmt.where(m.QuotaPolicy.subject_id == subject_id)
    after = decode_cursor(cursor)
    if after is not None:
        a_scope, a_subject = str(after[0]), str(after[1])
        stmt = stmt.where(
            (m.QuotaPolicy.scope > a_scope)
            | ((m.QuotaPolicy.scope == a_scope) & (m.QuotaPolicy.subject_id > a_subject))
        )
    rows = list(
        (
            await system.execute(
                stmt.order_by(m.QuotaPolicy.scope, m.QuotaPolicy.subject_id).limit(limit + 1)
            )
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[QuotaPolicy](
        items=[policy_dto(r) for r in rows],
        next_cursor=encode_cursor([rows[-1].scope, rows[-1].subject_id]) if more and rows else None,
    )


@router.put("/quotas", response_model=QuotaPolicy, operation_id="admin_put_quota")
async def admin_put_quota(
    ctx: Ctx, auth: Auth, system: SystemSession, body: QuotaPolicyUpsert
) -> QuotaPolicy:
    if body.scope is QuotaScope.TENANT and await system.get(m.Tenant, body.subject_id) is None:
        raise NotFoundError("tenant", body.subject_id)
    if body.scope is QuotaScope.TOKEN and await system.get(m.ApiToken, body.subject_id) is None:
        raise NotFoundError("token", body.subject_id)
    ts = now()
    limits: dict[str, JsonValue] = body.limits.model_dump(mode="json", exclude_none=True)
    values = {
        "scope": body.scope.value,
        "subject_id": body.subject_id,
        "limits": limits,
        "note": body.note,
        "updated_at": ts,
        "updated_by": auth.user_id,
    }
    await system.execute(
        insert(m.QuotaPolicy)
        .values(**values)
        .on_conflict_do_update(index_elements=["scope", "subject_id"], set_=values)
    )
    row = await system.get(
        m.QuotaPolicy, (body.scope.value, body.subject_id), populate_existing=True
    )
    assert row is not None
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.QUOTA_UPDATE,
                ResourceKind.QUOTA,
                resource_id=f"{body.scope.value}/{body.subject_id}",
                tenant_id=body.subject_id if body.scope is QuotaScope.TENANT else None,
                details={"limits": limits, "note": body.note},
            )
        ],
        session=system,
    )
    return policy_dto(row)


@router.delete(
    "/quotas/{scope}/{subject_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="admin_delete_quota",
)
async def admin_delete_quota(
    ctx: Ctx,
    auth: Auth,
    system: SystemSession,
    scope: QuotaScope,
    subject_id: Annotated[str, Path(max_length=64)],
) -> Response:
    row = await system.get(m.QuotaPolicy, (scope.value, subject_id))
    if row is None:
        raise NotFoundError("quota policy", f"{scope.value}/{subject_id}")
    await system.delete(row)
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.QUOTA_DELETE,
                ResourceKind.QUOTA,
                resource_id=f"{scope.value}/{subject_id}",
                tenant_id=subject_id if scope is QuotaScope.TENANT else None,
            )
        ],
        session=system,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================================ kill switches


@router.get("/kill-switches", response_model=KillSwitches, operation_id="admin_get_kill_switches")
async def admin_get_kill_switches(system: SystemSession) -> KillSwitches:
    return await kill_switches(system)


@router.put("/kill-switches", response_model=KillSwitches, operation_id="admin_put_kill_switches")
async def admin_put_kill_switches(
    ctx: Ctx, auth: Auth, system: SystemSession, body: KillSwitchUpdate
) -> KillSwitches:
    """Flip kill switches. Enforced at run creation and by the dispatcher at lease time.

    Pausing untrusted execution holds new work only. ``cancel_untrusted_runs`` also cancels
    every unfinished run of tenant-uploaded code (the reconciler then stops their
    executions): the containment step for a suspected sandbox escape.
    """
    await system.execute(
        select(m.PlatformSetting)
        .where(m.PlatformSetting.key == KILL_SWITCHES_KEY)
        .with_for_update()
    )
    current = await kill_switches(system)
    ts = now()
    updated = body.apply(current, now=ts, by=auth.user_id)
    value = updated.model_dump(mode="json")
    await system.execute(
        insert(m.PlatformSetting)
        .values(key=KILL_SWITCHES_KEY, value=value, updated_at=ts)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": ts})
    )
    cancelled = await _cancel_untrusted_runs(system) if body.cancel_untrusted_runs else None
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.KILL_SWITCH_UPDATE,
                ResourceKind.KILL_SWITCH,
                resource_id=KILL_SWITCHES_KEY,
                details={
                    "untrusted_execution_enabled": updated.untrusted_execution_enabled,
                    "global_spend_cap_micros": updated.global_spend_cap_micros,
                    "spend_cap_period": updated.spend_cap_period,
                    "reason": body.reason,
                    **({} if cancelled is None else {"cancelled_runs": len(cancelled)}),
                },
            )
        ],
        session=system,
    )
    return updated


async def _cancel_untrusted_runs(system: AsyncSession) -> list[str]:
    """Move every unfinished run of tenant-uploaded code to ``cancelling`` (all tenants).

    ``finalizing`` runs are left alone: their execution is already over. The reconciler
    cancels the executions and confirms ``cancelled``.
    """
    skip = {s.value for s in RunState if s.terminal} | {
        RunState.FINALIZING.value,
        RunState.CANCELLING.value,
    }
    runs = list(
        (
            await system.execute(
                select(m.Run)
                .where(m.Run.untrusted_code.is_(True), m.Run.state.not_in(skip))
                .order_by(m.Run.id)
                .with_for_update()
            )
        ).scalars()
    )
    for run in runs:
        await apply_transition(system, run, RunTrigger.CANCEL, Actor.API)
    return [r.id for r in runs]


# ================================================================================ executors


async def executor_health(system: AsyncSession, row: m.ExecutorReport) -> ExecutorHealth:
    """One executor: its last report plus live counts from the run table."""
    ts = now()
    caps = ExecutorCaps.model_validate(row.caps)
    running = (
        await system.execute(
            select(func.count()).where(
                m.Run.executor == row.name, m.Run.state.in_(DISPATCHED_STATES)
            )
        )
    ).scalar_one()
    queued = (
        await system.execute(select(func.count()).where(m.Run.state == RunState.QUEUED.value))
    ).scalar_one()
    failures = (
        await system.execute(
            select(func.count()).where(
                m.Run.executor == row.name,
                m.Run.failure_class == FailureClass.INFRA.value,
                m.Run.finished_at >= ts - timedelta(hours=1),
            )
        )
    ).scalar_one()
    if not row.enabled:
        state = ExecutorStatus.DISABLED
    elif ts - row.checked_at > STALE_REPORT:
        state = ExecutorStatus.DOWN
    elif row.throttled_last_hour or failures:
        state = ExecutorStatus.DEGRADED
    else:
        state = ExecutorStatus.OK
    return ExecutorHealth(
        name=row.name,
        enabled=row.enabled,
        status=state,
        caps=caps,
        regions=list(caps.regions),
        allows_public_tenants=caps.allows_public_tenants(),
        conformance=None,
        conformance_passed=False,
        running_executions=int(running),
        queued_runs=int(queued),
        submits_last_minute=row.submits_last_minute,
        submit_rate_limit_per_minute=row.submit_rate_limit_per_minute,
        throttled_last_hour=row.throttled_last_hour,
        failures_last_hour=int(failures),
        last_success_at=row.last_success_at,
        checked_at=row.checked_at,
        message=_health_message(row),
    )


def _health_message(row: m.ExecutorReport) -> str:
    if not row.enabled:
        why = row.disabled_reason or "no reason recorded"
        return f"disabled by an operator: {why}"[:500]
    return row.message or "no executor conformance report has been recorded"


@router.get("/executors", response_model=Page[ExecutorHealth], operation_id="admin_list_executors")
async def admin_list_executors(
    system: SystemSession, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[ExecutorHealth]:
    stmt = select(m.ExecutorReport)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.ExecutorReport.name > str(after))
    rows = list(
        (await system.execute(stmt.order_by(m.ExecutorReport.name).limit(limit + 1))).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[ExecutorHealth](
        items=[await executor_health(system, r) for r in rows],
        next_cursor=encode_cursor(rows[-1].name) if more and rows else None,
    )


@router.get("/executors/{name}", response_model=ExecutorHealth, operation_id="admin_get_executor")
async def admin_get_executor(
    system: SystemSession, name: Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
) -> ExecutorHealth:
    row = await system.get(m.ExecutorReport, name)
    if row is None:
        raise NotFoundError("executor", name)
    return await executor_health(system, row)


@router.put("/executors/{name}", response_model=ExecutorHealth, operation_id="admin_put_executor")
async def admin_put_executor(
    ctx: Ctx,
    auth: Auth,
    system: SystemSession,
    name: Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,63}$")],
    body: ExecutorUpdate,
) -> ExecutorHealth:
    """Enable or disable an executor. Orchestrators lease nothing onto a disabled one (their
    reports never re-enable it); runs already placed on it are not cancelled here."""
    row = await system.get(m.ExecutorReport, name, with_for_update=True)
    if row is None:
        raise NotFoundError("executor", name)
    row.enabled = body.enabled
    row.disabled_reason = None if body.enabled else body.reason
    await system.flush()
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.EXECUTOR_UPDATE,
                ResourceKind.EXECUTOR,
                resource_id=name,
                details={"enabled": body.enabled, "reason": body.reason},
            )
        ],
        session=system,
    )
    return await executor_health(system, row)


# ==================================================================================== audit


@router.get("/audit", response_model=Page[AuditEvent], operation_id="admin_list_audit")
async def admin_list_audit(
    system: SystemSession,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    tenant_id: Annotated[str | None, Query(max_length=64)] = None,
    project_id: Annotated[str | None, Query(max_length=64)] = None,
    actor_id: Annotated[str | None, Query(max_length=128)] = None,
    action: AuditAction | None = None,
    outcome: AuditOutcome | None = None,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
) -> Page[AuditEvent]:
    """The platform audit log (every tenant and platform-level actions), newest first."""
    q = AuditQuery(
        tenant_id=tenant_id, project_id=project_id, actor_id=actor_id, action=action,
        outcome=outcome, since=since, until=until,
    )  # fmt: skip
    return await audit_page(system, q, limit, cursor)
