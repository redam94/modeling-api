"""Tenants, projects and memberships: start-up provisioning and membership changes.

* :func:`ensure_platform_tenant` keeps the configured default tenant/project (the platform's
  own ``internal`` tenant; the only one in the single-tenant profile) in place;
* :func:`seed_dev_accounts` invites the development IdP's accounts (``dev`` profile);
* :func:`invite_admins` invites ``MAPI_DEFAULT_TENANT_ADMINS`` as tenant admins;
* :func:`add_member` / :func:`change_role` / :func:`remove_member` implement the membership
  endpoints, including "the last admin cannot be removed or demoted".
"""

from __future__ import annotations

from collections.abc import Sequence

from mapi_spec.api import MembershipCreate, new_id
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import DEFAULT_RESIDENCY, Capability, PrincipalKind, ProjectRole
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.identity import LOCAL_ISSUER, LOCAL_USER_ID, invite_user
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.runs import now

__all__ = [
    "add_member",
    "change_role",
    "ensure_local_admin",
    "ensure_platform_tenant",
    "grant",
    "invite_admins",
    "remove_member",
    "seed_dev_accounts",
]


async def ensure_platform_tenant(
    s: AsyncSession, tenant_id: str, project_id: str, *, residency: str = DEFAULT_RESIDENCY
) -> None:
    """Create the default tenant (``internal``, in ``residency``: the deployment's default,
    :meth:`~mapi_server.settings.Settings.effective_default_residency`) and project if
    missing (idempotent: an existing tenant keeps its residency)."""
    await s.execute(
        insert(m.Tenant)
        .values(
            id=tenant_id,
            name=tenant_id.capitalize(),
            trust_tier="internal",
            status="active",
            data_residency=residency,
        )
        .on_conflict_do_nothing()
    )
    await s.execute(
        insert(m.Project)
        .values(
            tenant_id=tenant_id,
            id=project_id,
            project_id=project_id,
            name=f"{project_id.capitalize()} project",
        )
        .on_conflict_do_nothing()
    )


async def grant(
    s: AsyncSession,
    *,
    tenant_id: str,
    project_id: str | None,
    user: m.User,
    role: ProjectRole,
    created_by: str | None = None,
) -> bool:
    """Insert a membership unless one exists for that scope (returns whether it inserted)."""
    exists = (
        await s.execute(
            select(m.Membership.id).where(
                m.Membership.tenant_id == tenant_id,
                func.coalesce(m.Membership.project_id, "") == (project_id or ""),
                m.Membership.principal_id == user.id,
            )
        )
    ).first()
    if exists is not None:
        return False
    s.add(
        m.Membership(
            id=new_id("mem"),
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id=user.id,
            principal_kind=PrincipalKind.USER.value,
            role=role.value,
            email=user.email,
            display_name=user.name,
            created_at=now(),
            created_by=created_by,
        )
    )
    await s.flush()
    return True


async def ensure_local_admin(s: AsyncSession, tenant_id: str) -> None:
    """Single-tenant profile: the local administrator (credential-less loopback requests,
    and the owner of tokens they mint) with a tenant-wide admin grant."""
    await s.execute(
        insert(m.User)
        .values(
            id=LOCAL_USER_ID,
            issuer=LOCAL_ISSUER,
            subject="local",
            email_verified=False,
            name="Local administrator",
            operator=True,
            status="active",
        )
        .on_conflict_do_nothing()
    )
    user = await s.get(m.User, LOCAL_USER_ID)
    assert user is not None
    await grant(s, tenant_id=tenant_id, project_id=None, user=user, role=ProjectRole.ADMIN)


async def seed_dev_accounts(
    s: AsyncSession, emails: Sequence[str], *, tenant_id: str, project_id: str
) -> None:
    """Invite the dev IdP's accounts: #1 tenant admin, #2 project runner, the rest viewers."""
    for i, email in enumerate(e.strip().lower() for e in emails if e.strip()):
        user = await invite_user(s, email)
        if i == 0:
            await grant(s, tenant_id=tenant_id, project_id=None, user=user, role=ProjectRole.ADMIN)
        else:
            role = ProjectRole.RUNNER if i == 1 else ProjectRole.VIEWER
            await grant(s, tenant_id=tenant_id, project_id=project_id, user=user, role=role)


async def invite_admins(s: AsyncSession, emails: Sequence[str], *, tenant_id: str) -> None:
    for email in (e.strip().lower() for e in emails if e.strip()):
        user = await invite_user(s, email)
        await grant(s, tenant_id=tenant_id, project_id=None, user=user, role=ProjectRole.ADMIN)


# ======================================================================== membership changes


async def _principal(
    system: AsyncSession, body: MembershipCreate
) -> tuple[str, str, str | None, str | None]:
    """``(principal_id, kind, email, display_name)`` for a new membership."""
    if body.email is not None:
        user = await invite_user(system, body.email)
        return user.id, PrincipalKind.USER.value, user.email, user.name
    assert body.principal_id is not None
    if body.principal_id.startswith("sa_"):
        return body.principal_id, PrincipalKind.SERVICE_ACCOUNT.value, None, None
    found = await system.get(m.User, body.principal_id)
    if found is None:
        raise NotFoundError("principal", body.principal_id)
    return found.id, PrincipalKind.USER.value, found.email, found.name


async def add_member(
    session: AsyncSession,
    system: AsyncSession,
    *,
    tenant_id: str,
    project_id: str | None,
    body: MembershipCreate,
    created_by: str | None,
) -> m.Membership:
    """Grant ``body.role`` (409 if the principal already has a grant at this scope)."""
    principal_id, kind, email, name = await _principal(system, body)
    existing = await _find(session, tenant_id, project_id, principal_id)
    if existing is not None:
        raise Conflict(f"{principal_id} already has a role here; change it instead")
    row = m.Membership(
        id=new_id("mem"),
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id=principal_id,
        principal_kind=kind,
        role=body.role.value,
        capabilities=sorted({c.value for c in body.capabilities}),
        email=email,
        display_name=name,
        created_at=now(),
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def _find(
    session: AsyncSession, tenant_id: str, project_id: str | None, principal_id: str
) -> m.Membership | None:
    return (
        await session.execute(
            select(m.Membership)
            .where(
                m.Membership.tenant_id == tenant_id,
                func.coalesce(m.Membership.project_id, "") == (project_id or ""),
                m.Membership.principal_id == principal_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _admins_left(
    session: AsyncSession, tenant_id: str, project_id: str | None, excluding: str
) -> int:
    """Admins that remain at this scope without ``excluding`` (tenant admins count for every
    project of the tenant)."""
    stmt = select(func.count()).where(
        m.Membership.tenant_id == tenant_id,
        m.Membership.role == ProjectRole.ADMIN.value,
        m.Membership.id != excluding,
    )
    if project_id is None:
        stmt = stmt.where(m.Membership.project_id.is_(None))
    else:
        stmt = stmt.where(
            (m.Membership.project_id.is_(None)) | (m.Membership.project_id == project_id)
        )
    return int((await session.execute(stmt)).scalar_one())


async def change_role(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str | None,
    principal_id: str,
    role: ProjectRole | None,
    capabilities: list[Capability] | None = None,
) -> m.Membership:
    """Change a member's role and/or (M3.5) capabilities (``reviewer``)."""
    row = await _find(session, tenant_id, project_id, principal_id)
    if row is None:
        raise NotFoundError("member", principal_id)
    if capabilities is not None:
        row.capabilities = sorted({c.value for c in capabilities})
    if role is None:
        await session.flush()
        return row
    if (
        row.role == ProjectRole.ADMIN.value
        and role is not ProjectRole.ADMIN
        and await _admins_left(session, tenant_id, project_id, row.id) == 0
    ):
        raise MapiError("the last admin cannot be demoted", code=ErrorCode.CONFLICT)
    row.role = role.value
    await session.flush()
    return row


async def remove_member(
    session: AsyncSession, *, tenant_id: str, project_id: str | None, principal_id: str
) -> m.Membership:
    row = await _find(session, tenant_id, project_id, principal_id)
    if row is None:
        raise NotFoundError("member", principal_id)
    if (
        row.role == ProjectRole.ADMIN.value
        and await _admins_left(session, tenant_id, project_id, row.id) == 0
    ):
        raise MapiError("the last admin cannot be removed", code=ErrorCode.CONFLICT)
    await session.delete(row)
    await session.flush()
    return row
