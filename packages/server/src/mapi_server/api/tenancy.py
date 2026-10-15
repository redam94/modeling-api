"""Identity and tenancy: ``/v1/me``, tenants, projects, members and the tenant audit log.

Tenant-level routes take ``{tenant_id}`` from the path and run in a tenant session (row-level
security); the caller's grants were resolved by :func:`mapi_server.auth.request.authenticate`.
Cross-tenant views of the caller's own memberships (``/v1/me``, ``/v1/tenants``) read
through the platform role with explicit principal filters.
"""

from __future__ import annotations

import base64
import binascii
from collections import defaultdict
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status
from mapi_spec.api import (
    Me,
    MembershipCreate,
    MembershipUpdate,
    MeProject,
    MeTenant,
    Page,
    ProjectCreate,
    ProjectUpdate,
    TenantUpdate,
)
from mapi_spec.audit import AuditAction, AuditEvent, AuditOutcome, AuditQuery
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import (
    Membership,
    ProjectRole,
    ResourceKind,
    Tenant,
    TenantStatus,
    TrustTier,
)
from mapi_spec.tenancy import Project as ProjectDTO
from pydantic import AwareDatetime
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Limit,
    Session,
    SystemSession,
    decode_cursor,
    encode_cursor,
)
from mapi_server.auth.identity import local_principal, membership_dto
from mapi_server.auth.request import Credential, RequestAuth
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.audit import audit_dto, audit_query, write_audit
from mapi_server.services.runs import now
from mapi_server.services.tenancy import add_member, change_role, remove_member

__all__ = ["project_dto", "router", "tenant_dto", "visible_projects"]

router = APIRouter(tags=["tenants"])

Slug = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")]
PrincipalId = Annotated[str, Path(pattern=r"^(?:usr|sa)_[0-9a-z]{26}$")]


def tenant_dto(t: m.Tenant) -> Tenant:
    return Tenant(
        id=t.id,
        name=t.name,
        trust_tier=TrustTier(t.trust_tier),
        status=TenantStatus(t.status),
        status_reason=t.status_reason,
        data_residency=t.data_residency,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def project_dto(p: m.Project) -> ProjectDTO:
    return ProjectDTO(
        id=p.id, tenant_id=p.tenant_id, name=p.name, description=p.description,
        created_at=p.created_at,
    )  # fmt: skip


def _role_at(
    memberships: tuple[Membership, ...], tenant_id: str, project_id: str | None
) -> ProjectRole | None:
    best: ProjectRole | None = None
    for mm in memberships:
        if mm.tenant_id != tenant_id or (mm.project_id not in (None, project_id)):
            continue
        if best is None or mm.role > best:
            best = mm.role
    return best


async def visible_projects(
    s: AsyncSession, auth: RequestAuth, tenant_ids: list[str] | None = None
) -> list[tuple[m.Project, ProjectRole | None]]:
    """Projects the caller may see, with the caller's effective role in each (operators see
    every project of the tenants asked for, with role ``None`` where they hold none)."""
    by_tenant: dict[str, set[str | None]] = defaultdict(set)
    for mm in auth.memberships:
        by_tenant[mm.tenant_id].add(mm.project_id)
    wanted = tenant_ids if tenant_ids is not None else sorted(by_tenant)
    out: list[tuple[m.Project, ProjectRole | None]] = []
    for tenant_id in wanted:
        grants = by_tenant.get(tenant_id, set())
        stmt = select(m.Project).where(m.Project.tenant_id == tenant_id)
        if None not in grants and not auth.operator:
            if not grants:
                continue
            stmt = stmt.where(m.Project.id.in_([g for g in grants if g is not None]))
        if auth.token is not None:  # a token sees its own project only
            stmt = stmt.where(m.Project.id == auth.token.project_id)
        for p in (await s.execute(stmt.order_by(m.Project.id))).scalars():
            out.append((p, _role_at(auth.memberships, tenant_id, p.id)))
    return out


def _page_cursor(after: str | None) -> str | None:
    value = decode_cursor(after)
    return None if value is None else str(value)


# ===================================================================================== me


@router.get("/v1/me", response_model=Me, tags=["identity"], operation_id="get_me")
async def get_me(ctx: Ctx, auth: Auth, system: SystemSession) -> Me:
    """Who is calling, and the tenants, projects and roles they can act in. For a token:
    only its own project (its scopes narrow every role shown)."""
    principal = auth.principal or local_principal()
    tenant_ids = sorted({mm.tenant_id for mm in auth.memberships})
    if auth.token is not None:
        tenant_ids = [auth.token.tenant_id]
    tenants: list[MeTenant] = []
    for tenant_id in tenant_ids:
        tenant = await system.get(m.Tenant, tenant_id)
        if tenant is None or not TenantStatus(tenant.status).allows_reads:
            continue
        projects: list[MeProject] = []
        for p, role in await visible_projects(system, auth, [tenant_id]):
            if auth.token is not None and p.id != auth.token.project_id:
                continue
            if role is None:
                continue
            projects.append(MeProject(tenant_id=tenant_id, project_id=p.id, name=p.name, role=role))
        tenants.append(
            MeTenant(
                tenant=tenant_dto(tenant),
                role=_role_at(auth.memberships, tenant_id, None),
                projects=projects,
            )
        )
    token = None
    if auth.token is not None:
        token = auth.token.to_public(now())
    kind = "token" if auth.credential is Credential.TOKEN else "session"
    return Me(
        principal=principal,
        auth=kind,
        operator=auth.operator,
        tenants=tenants,
        token=token,
    )


# ================================================================================ tenants


@router.get("/v1/tenants", response_model=Page[Tenant], operation_id="list_tenants")
async def list_tenants(
    auth: Auth, system: SystemSession, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[Tenant]:
    """Tenants the caller belongs to (operators: use ``/v1/admin/tenants`` for all)."""
    ids = sorted({mm.tenant_id for mm in auth.memberships})
    after = _page_cursor(cursor)
    if after is not None:
        ids = [i for i in ids if i > after]
    rows = list(
        (
            await system.execute(
                select(m.Tenant)
                .where(m.Tenant.id.in_(ids), m.Tenant.status != TenantStatus.DELETING.value)
                .order_by(m.Tenant.id)
                .limit(limit + 1)
            )
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[Tenant](
        items=[tenant_dto(t) for t in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


async def _tenant(session: AsyncSession, tenant_id: str, *, lock: bool = False) -> m.Tenant:
    stmt = select(m.Tenant).where(m.Tenant.id == tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("tenant", tenant_id)
    return row


@router.get("/v1/tenants/{tenant_id}", response_model=Tenant, operation_id="get_tenant")
async def get_tenant(session: Session, tenant_id: Slug) -> Tenant:
    return tenant_dto(await _tenant(session, tenant_id))


@router.patch("/v1/tenants/{tenant_id}", response_model=Tenant, operation_id="update_tenant")
async def update_tenant(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: Slug, body: TenantUpdate
) -> Tenant:
    tenant = await _tenant(session, tenant_id, lock=True)
    tenant.name = body.name
    tenant.updated_at = now()
    await write_audit(
        ctx.db,
        [auth.audit(ctx, AuditAction.TENANT_UPDATE, ResourceKind.TENANT, resource_id=tenant_id,
                    details={"name": body.name})],
        session=session,
    )  # fmt: skip
    return tenant_dto(tenant)


# =============================================================================== projects


@router.get(
    "/v1/tenants/{tenant_id}/projects",
    response_model=Page[ProjectDTO],
    tags=["projects"],
    operation_id="list_tenant_projects",
)
async def list_tenant_projects(
    auth: Auth,
    session: Session,
    tenant_id: Slug,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[ProjectDTO]:
    rows = [p for p, _ in await visible_projects(session, auth, [tenant_id])]
    after = _page_cursor(cursor)
    if after is not None:
        rows = [p for p in rows if p.id > after]
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[ProjectDTO](
        items=[project_dto(p) for p in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.post(
    "/v1/tenants/{tenant_id}/projects",
    response_model=ProjectDTO,
    status_code=status.HTTP_201_CREATED,
    tags=["projects"],
    operation_id="create_project",
)
async def create_project(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: Slug, body: ProjectCreate
) -> ProjectDTO:
    if await session.get(m.Project, (tenant_id, body.id)) is not None:
        raise Conflict(f"project {tenant_id}/{body.id} already exists")
    row = m.Project(
        tenant_id=tenant_id,
        id=body.id,
        project_id=body.id,
        name=body.name,
        description=body.description,
        created_at=now(),
    )
    session.add(row)
    await session.flush()
    await write_audit(
        ctx.db,
        [auth.audit(ctx, AuditAction.PROJECT_CREATE, ResourceKind.PROJECT, resource_id=body.id,
                    project_id=body.id)],
        session=session,
    )  # fmt: skip
    return project_dto(row)


async def _project(session: AsyncSession, tenant_id: str, project_id: str) -> m.Project:
    row = await session.get(m.Project, (tenant_id, project_id), with_for_update=True)
    if row is None:
        raise NotFoundError("project", f"{tenant_id}/{project_id}")
    return row


@router.patch(
    "/v1/tenants/{tenant_id}/projects/{project_id}",
    response_model=ProjectDTO,
    tags=["projects"],
    operation_id="update_project",
)
async def update_project(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    tenant_id: Slug,
    project_id: Slug,
    body: ProjectUpdate,
) -> ProjectDTO:
    row = await _project(session, tenant_id, project_id)
    if "name" in body.model_fields_set and body.name is not None:
        row.name = body.name
    if "description" in body.model_fields_set:
        row.description = body.description
    await write_audit(
        ctx.db,
        [auth.audit(ctx, AuditAction.PROJECT_UPDATE, ResourceKind.PROJECT, resource_id=project_id,
                    details={"fields": ", ".join(sorted(body.model_fields_set))})],
        session=session,
    )  # fmt: skip
    return project_dto(row)


_PROJECT_DATA = (m.Dataset, m.Spec, m.Run, m.Model)


@router.delete(
    "/v1/tenants/{tenant_id}/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["projects"],
    operation_id="delete_project",
)
async def delete_project(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: Slug, project_id: Slug
) -> Response:
    """Delete an empty project (its memberships and tokens go with it). 409 while it holds
    datasets, specs, runs or models, and for the platform's default project."""
    row = await _project(session, tenant_id, project_id)
    if (tenant_id, project_id) == (ctx.platform_scope.tenant_id, ctx.platform_scope.project_id):
        raise Conflict("the platform's default project cannot be deleted")
    for model in _PROJECT_DATA:
        holds = (
            await session.execute(
                select(exists().where(model.tenant_id == tenant_id, model.project_id == project_id))
            )
        ).scalar_one()
        if holds:
            raise Conflict(f"project {tenant_id}/{project_id} still holds {model.__tablename__}")
    await session.delete(row)
    await session.flush()
    await write_audit(
        ctx.db,
        [auth.audit(ctx, AuditAction.PROJECT_DELETE, ResourceKind.PROJECT, resource_id=project_id,
                    project_id=project_id)],
        session=session,
    )  # fmt: skip
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ================================================================================ members


async def _members_page(
    session: AsyncSession,
    tenant_id: str,
    project_id: str | None,
    *,
    include_tenant_wide: bool,
    limit: int,
    cursor: str | None,
) -> Page[Membership]:
    stmt = select(m.Membership).where(m.Membership.tenant_id == tenant_id)
    if project_id is None:
        stmt = stmt.where(m.Membership.project_id.is_(None))
    elif include_tenant_wide:
        stmt = stmt.where(
            (m.Membership.project_id.is_(None)) | (m.Membership.project_id == project_id)
        )
    else:
        stmt = stmt.where(m.Membership.project_id == project_id)
    after = _page_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.Membership.id > after)
    rows = list((await session.execute(stmt.order_by(m.Membership.id).limit(limit + 1))).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[Membership](
        items=[membership_dto(r) for r in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.get(
    "/v1/tenants/{tenant_id}/members",
    response_model=Page[Membership],
    operation_id="list_tenant_members",
)
async def list_tenant_members(
    session: Session, tenant_id: Slug, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[Membership]:
    return await _members_page(
        session, tenant_id, None, include_tenant_wide=True, limit=limit, cursor=cursor
    )


@router.post(
    "/v1/tenants/{tenant_id}/members",
    response_model=Membership,
    status_code=status.HTTP_201_CREATED,
    operation_id="add_tenant_member",
)
async def add_tenant_member(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    system: SystemSession,
    tenant_id: Slug,
    body: MembershipCreate,
) -> Membership:
    row = await add_member(
        session, system, tenant_id=tenant_id, project_id=None, body=body,
        created_by=auth.user_id,
    )  # fmt: skip
    await _audit_member(ctx, auth, session, AuditAction.MEMBER_ADD, row, ResourceKind.TENANT_MEMBER)
    return membership_dto(row)


@router.patch(
    "/v1/tenants/{tenant_id}/members/{principal_id}",
    response_model=Membership,
    operation_id="update_tenant_member",
)
async def update_tenant_member(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    tenant_id: Slug,
    principal_id: PrincipalId,
    body: MembershipUpdate,
) -> Membership:
    row = await change_role(
        session, tenant_id=tenant_id, project_id=None, principal_id=principal_id, role=body.role
    )
    await _audit_member(
        ctx, auth, session, AuditAction.MEMBER_UPDATE, row, ResourceKind.TENANT_MEMBER
    )
    return membership_dto(row)


@router.delete(
    "/v1/tenants/{tenant_id}/members/{principal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="remove_tenant_member",
)
async def remove_tenant_member(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: Slug, principal_id: PrincipalId
) -> Response:
    row = await remove_member(
        session, tenant_id=tenant_id, project_id=None, principal_id=principal_id
    )
    await _audit_member(
        ctx, auth, session, AuditAction.MEMBER_REMOVE, row, ResourceKind.TENANT_MEMBER
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/v1/tenants/{tenant_id}/projects/{project_id}/members",
    response_model=Page[Membership],
    operation_id="list_members",
)
async def list_members(
    session: Session,
    tenant_id: Slug,
    project_id: Slug,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[Membership]:
    """Project grants plus tenant-wide grants (which apply to every project)."""
    return await _members_page(
        session, tenant_id, project_id, include_tenant_wide=True, limit=limit, cursor=cursor
    )


@router.post(
    "/v1/tenants/{tenant_id}/projects/{project_id}/members",
    response_model=Membership,
    status_code=status.HTTP_201_CREATED,
    operation_id="add_member",
)
async def add_project_member(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    system: SystemSession,
    tenant_id: Slug,
    project_id: Slug,
    body: MembershipCreate,
) -> Membership:
    row = await add_member(
        session, system, tenant_id=tenant_id, project_id=project_id, body=body,
        created_by=auth.user_id,
    )  # fmt: skip
    await _audit_member(ctx, auth, session, AuditAction.MEMBER_ADD, row, ResourceKind.MEMBER)
    return membership_dto(row)


@router.patch(
    "/v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}",
    response_model=Membership,
    operation_id="update_member",
)
async def update_project_member(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    tenant_id: Slug,
    project_id: Slug,
    principal_id: PrincipalId,
    body: MembershipUpdate,
) -> Membership:
    row = await change_role(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id=principal_id,
        role=body.role,
    )
    await _audit_member(ctx, auth, session, AuditAction.MEMBER_UPDATE, row, ResourceKind.MEMBER)
    return membership_dto(row)


@router.delete(
    "/v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="remove_member",
)
async def remove_project_member(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    tenant_id: Slug,
    project_id: Slug,
    principal_id: PrincipalId,
) -> Response:
    row = await remove_member(
        session, tenant_id=tenant_id, project_id=project_id, principal_id=principal_id
    )
    await _audit_member(ctx, auth, session, AuditAction.MEMBER_REMOVE, row, ResourceKind.MEMBER)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _audit_member(
    ctx: Ctx,
    auth: RequestAuth,
    session: AsyncSession,
    action: AuditAction,
    row: m.Membership,
    kind: ResourceKind,
) -> None:
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                action,
                kind,
                resource_id=row.principal_id,
                project_id=row.project_id,
                details={"role": row.role, "project_id": row.project_id},
            )
        ],
        session=session,
    )


# ================================================================================== audit


def encode_audit_cursor(ev: m.AuditEvent) -> str:
    raw = f"{ev.ts.isoformat()}|{ev.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_audit_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, ident = raw.split("|", 1)
        return datetime.fromisoformat(ts), ident
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise MapiError(
            "the cursor is invalid", code=ErrorCode.SCHEMA_INVALID, pointer="/query/cursor"
        ) from None


async def audit_page(
    session: AsyncSession, q: AuditQuery, limit: int, cursor: str | None
) -> Page[AuditEvent]:
    rows = list(
        (
            await session.execute(
                audit_query(q, after=decode_audit_cursor(cursor)).limit(limit + 1)
            )
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[AuditEvent](
        items=[audit_dto(r) for r in rows],
        next_cursor=encode_audit_cursor(rows[-1]) if more and rows else None,
    )


@router.get(
    "/v1/tenants/{tenant_id}/audit",
    response_model=Page[AuditEvent],
    operation_id="list_tenant_audit",
)
async def list_tenant_audit(
    session: Session,
    tenant_id: Slug,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    project_id: Annotated[str | None, Query(max_length=64)] = None,
    actor_id: Annotated[str | None, Query(max_length=128)] = None,
    action: AuditAction | None = None,
    outcome: AuditOutcome | None = None,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
) -> Page[AuditEvent]:
    """The tenant's audit log, newest first (tenant admins)."""
    q = AuditQuery(
        tenant_id=tenant_id, project_id=project_id, actor_id=actor_id, action=action,
        outcome=outcome, since=since, until=until,
    )  # fmt: skip
    return await audit_page(session, q, limit, cursor)
