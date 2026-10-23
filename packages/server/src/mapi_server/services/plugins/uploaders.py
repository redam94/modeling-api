"""Who may upload code (v0.19 D1, D2) and the tenant's uploaded-code settings.

Three gates decide (D1): the tenant passes the code-upload rule
(:func:`~mapi_server.services.verification.code_upload_rule`: internal and self-hosted
tenants yes, verified tenants only with identity verification), the tenant is **not** on the
``public`` tier (refused on every plugin route in v0.19, whatever
``MAPI_PUBLIC_CODE_UPLOAD_ENABLED`` says), and the person is a tenant admin or an editor of
the project whom a tenant admin attested (D2, ``user_verifications``). The platform kill
switch is not a gate here: uploads are stored while it is off, and no pipeline run is placed.

A tenant admin attests an editor ("I know who this person is") and can revoke it; nobody
attests themselves (a database check too). The tenant's settings row holds its tenant code
(assigned once, on the first claim or settings change, never reused) and the private-use
switch.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC

from mapi_spec.audit import AuditAction
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.plugins import PluginSettings, VerifiedUploader, new_tenant_code
from mapi_spec.tenancy import ProjectRole, ResourceKind, TrustTier, effective_role
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.audit import add_audit
from mapi_server.services.runs import now
from mapi_server.services.verification import code_upload_rule

__all__ = [
    "attest_uploader",
    "caller_id",
    "ensure_settings",
    "is_tenant_admin",
    "list_uploaders",
    "plugin_settings",
    "refuse_tokens",
    "require_can_upload",
    "require_plugins_available",
    "revoke_uploader",
    "update_settings",
]

TENANT_CODE_ATTEMPTS = 8
"""Fresh tenant codes tried before giving up (a collision is one in about 10**9)."""


def caller_id(auth: RequestAuth) -> str:
    """The acting person (a token's owner); every plugin route has one."""
    user = auth.user_id
    if not user:  # pragma: no cover - the routes are never anonymous
        raise MapiError("this action needs a signed-in person", code=ErrorCode.FORBIDDEN)
    return user


def is_tenant_admin(auth: RequestAuth) -> bool:
    """Is the caller (a token: its owner) an admin of the request's tenant?"""
    return bool(auth.context and auth.context.tenant_role is ProjectRole.ADMIN)


def refuse_tokens(auth: RequestAuth, what: str) -> None:
    """``FORBIDDEN`` for an API token: ``what`` needs a person signed in to the web app (or
    the single-tenant local administrator)."""
    if auth.token is not None:
        raise MapiError(
            f"API tokens cannot {what}. Sign in to the web app.", code=ErrorCode.FORBIDDEN
        )


def require_plugins_available(ctx: ServerContext, auth: RequestAuth) -> None:
    """The checks of every plugin route: uploads are turned on in this deployment, and the
    tenant is not on the public tier (refused in v0.19 whatever
    ``MAPI_PUBLIC_CODE_UPLOAD_ENABLED`` says)."""
    if not ctx.settings.plugins_enabled:
        raise MapiError("Uploaded code is turned off in this deployment.", code=ErrorCode.FORBIDDEN)
    tier = auth.context.trust_tier if auth.context is not None else None
    if tier is TrustTier.PUBLIC:
        raise MapiError(
            "Public workspaces cannot upload code. Built-in models and declarative specs are "
            "always available.",
            code=ErrorCode.FORBIDDEN,
        )


async def _attested(session: AsyncSession, tenant_id: str, user_id: str) -> bool:
    row = await session.get(m.UserVerification, (tenant_id, user_id))
    return row is not None and row.revoked_at is None


async def require_can_upload(
    session: AsyncSession, ctx: ServerContext, auth: RequestAuth, *, project_id: str
) -> None:
    """D1 + D2 for claims, tickets and ``:complete``: the deployment and the tier allow
    uploads, the tenant passes the code-upload rule (``IDENTITY_VERIFICATION_REQUIRED``), and
    the caller is a tenant admin, or an editor of ``project_id`` with an active attestation
    (``FORBIDDEN``). A token acts only in its own project."""
    require_plugins_available(ctx, auth)
    tenant_id = auth.tenant_id
    if tenant_id is None:  # pragma: no cover - plugin routes are project routes
        raise MapiError("uploads need a workspace", code=ErrorCode.FORBIDDEN)
    tenant = await session.get(m.Tenant, tenant_id)
    if tenant is None:  # pragma: no cover - authentication loaded it
        raise NotFoundError("tenant", tenant_id)
    allowed, reason = code_upload_rule(
        ctx, tenant, await session.get(m.TenantVerification, tenant_id)
    )
    if not allowed:
        raise MapiError(
            f"{reason} Built-in models and declarative specs are always available.",
            code=ErrorCode.IDENTITY_VERIFICATION_REQUIRED,
        )
    if auth.token is not None and auth.token.project_id != project_id:
        raise MapiError(
            f"This API token belongs to the project {auth.token.project_id!r}; it cannot "
            f"upload for the project {project_id!r}.",
            code=ErrorCode.FORBIDDEN,
        )
    if await session.get(m.Project, (tenant_id, project_id)) is None:
        raise NotFoundError("project", f"{tenant_id}/{project_id}")
    if is_tenant_admin(auth):
        return
    user = caller_id(auth)
    role = effective_role(auth.memberships, user, tenant_id, project_id)
    if role is None or role < ProjectRole.EDITOR:
        raise MapiError(
            f"Uploading code needs the editor role in the project {project_id!r}.",
            code=ErrorCode.FORBIDDEN,
        )
    if not await _attested(session, tenant_id, user):
        raise MapiError(
            "A tenant admin must confirm who you are before you can upload code. Ask a "
            "tenant admin to add you to the verified uploaders in the workspace settings.",
            code=ErrorCode.FORBIDDEN,
        )


# ============================================================================ tenant settings


async def ensure_settings(
    session: AsyncSession, tenant_id: str, *, by: str | None
) -> m.TenantPluginSettings:
    """The tenant's settings row, created with a fresh tenant code on first use (retried on
    a code collision with another tenant; concurrent first uses of one tenant share one row).
    """
    row = await session.get(m.TenantPluginSettings, tenant_id)
    if row is not None:
        return row
    for _ in range(TENANT_CODE_ATTEMPTS):
        candidate = m.TenantPluginSettings(
            tenant_id=tenant_id,
            tenant_code=new_tenant_code(),
            private_use=True,
            updated_by=by,
            updated_at=now(),
        )
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
        except IntegrityError:
            # another request of this tenant created the row first (use it), or the code
            # is another tenant's (try a new one); the savepoint expunged the candidate
            existing = await session.get(m.TenantPluginSettings, tenant_id, populate_existing=True)
            if existing is not None:
                return existing
            continue
        return candidate
    raise MapiError(  # pragma: no cover - 8 collisions in a 30-bit space
        "Could not assign a code to this workspace. Try again.", code=ErrorCode.CONFLICT
    )


def _settings_dto(
    ctx: ServerContext,
    tenant: m.Tenant,
    verification: m.TenantVerification | None,
    row: m.TenantPluginSettings | None,
) -> PluginSettings:
    if not ctx.settings.plugins_enabled:
        allowed, reason = False, "Uploaded code is turned off in this deployment."
    elif TrustTier(tenant.trust_tier) is TrustTier.PUBLIC:
        allowed, reason = False, "Public workspaces cannot upload code."
    else:
        allowed, reason = code_upload_rule(ctx, tenant, verification)
    return PluginSettings(
        tenant_code=row.tenant_code if row is not None else None,
        private_use=row.private_use if row is not None else True,
        uploads_allowed=allowed,
        reason=reason[:500],
    )


async def plugin_settings(
    session: AsyncSession, ctx: ServerContext, tenant_id: str
) -> PluginSettings:
    tenant = await session.get(m.Tenant, tenant_id)
    if tenant is None:  # pragma: no cover - authentication loaded it
        raise NotFoundError("tenant", tenant_id)
    return _settings_dto(
        ctx,
        tenant,
        await session.get(m.TenantVerification, tenant_id),
        await session.get(m.TenantPluginSettings, tenant_id),
    )


async def update_settings(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    tenant_id: str,
    *,
    private_use: bool,
) -> PluginSettings:
    """Turn private use on or off (audited). Turning it off stops new admissions and
    dispatches of the uploaders' own unapproved versions at once (the gate reads it)."""
    by = caller_id(auth)
    row = await ensure_settings(session, tenant_id, by=by)
    before = row.private_use
    row.private_use = private_use
    row.updated_by = by
    row.updated_at = now()
    await session.flush()
    await add_audit(
        session,
        [
            auth.audit(
                ctx,
                AuditAction.PLUGIN_SETTINGS_UPDATE,
                ResourceKind.PLUGIN_SETTINGS,
                resource_id=tenant_id,
                details={"private_use": private_use, "was": before},
                tenant_id=tenant_id,
            )
        ],
    )
    return await plugin_settings(session, ctx, tenant_id)


# ========================================================================= attested uploaders


async def _members(session: AsyncSession, tenant_id: str, user_id: str) -> Sequence[m.Membership]:
    return list(
        (
            await session.execute(
                select(m.Membership).where(
                    m.Membership.tenant_id == tenant_id, m.Membership.principal_id == user_id
                )
            )
        ).scalars()
    )


def _uploader_dto(row: m.UserVerification, member: m.Membership | None) -> VerifiedUploader:
    return VerifiedUploader(
        user_id=row.user_id,
        display_name=member.display_name if member is not None else None,
        email=member.email if member is not None else None,
        method="provider" if row.method == "provider" else "admin_attested",
        verified_by=row.verified_by,
        verified_at=row.verified_at.astimezone(UTC),
    )


async def list_uploaders(session: AsyncSession, tenant_id: str) -> list[VerifiedUploader]:
    """Active attestations of the tenant, newest first."""
    rows = list(
        (
            await session.execute(
                select(m.UserVerification)
                .where(
                    m.UserVerification.tenant_id == tenant_id,
                    m.UserVerification.revoked_at.is_(None),
                )
                .order_by(m.UserVerification.verified_at.desc(), m.UserVerification.user_id)
            )
        ).scalars()
    )
    out = []
    for row in rows:
        members = await _members(session, tenant_id, row.user_id)
        out.append(_uploader_dto(row, members[0] if members else None))
    return out


async def attest_uploader(
    session: AsyncSession, ctx: ServerContext, auth: RequestAuth, tenant_id: str, user_id: str
) -> VerifiedUploader:
    """A tenant admin attests a member of the tenant (idempotent; audited). Not yourself
    (``FORBIDDEN``), not a person outside the tenant (``NOT_FOUND``), not a client-only
    member (``CONFLICT``)."""
    refuse_tokens(auth, "confirm who may upload code")
    by = caller_id(auth)
    if user_id == by:
        raise MapiError(
            "You cannot confirm your own identity. Another tenant admin must do it (tenant "
            "admins can upload without it).",
            code=ErrorCode.FORBIDDEN,
        )
    members = await _members(session, tenant_id, user_id)
    if not members:
        raise NotFoundError("member", user_id)
    if all(ProjectRole(mm.role) is ProjectRole.CLIENT for mm in members):
        raise Conflict("A client of a dashboard cannot upload code. Give the person a role first.")
    row = await session.get(m.UserVerification, (tenant_id, user_id), with_for_update=True)
    if row is not None and row.revoked_at is None:
        return _uploader_dto(row, members[0])
    ts = now()
    if row is None:
        row = m.UserVerification(
            tenant_id=tenant_id,
            user_id=user_id,
            method="admin_attested",
            verified_by=by,
            verified_at=ts,
        )
        session.add(row)
    else:
        row.method = "admin_attested"
        row.verified_by = by
        row.verified_at = ts
        row.revoked_at = None
        row.revoked_by = None
    await session.flush()
    await add_audit(
        session,
        [
            auth.audit(
                ctx,
                AuditAction.PLUGIN_UPLOADER_ATTEST,
                ResourceKind.PLUGIN_SETTINGS,
                resource_id=user_id,
                details={"user_id": user_id, "verified_by": by},
                tenant_id=tenant_id,
            )
        ],
    )
    return _uploader_dto(row, members[0])


async def revoke_uploader(
    session: AsyncSession, ctx: ServerContext, auth: RequestAuth, tenant_id: str, user_id: str
) -> None:
    """Revoke an active attestation (audited; ``NOT_FOUND`` when there is none). Versions
    the person already uploaded keep their state: yank them separately when needed."""
    refuse_tokens(auth, "change who may upload code")
    row = await session.get(m.UserVerification, (tenant_id, user_id), with_for_update=True)
    if row is None or row.revoked_at is not None:
        raise NotFoundError("verified uploader", user_id)
    row.revoked_at = now()
    row.revoked_by = caller_id(auth)
    await session.flush()
    await add_audit(
        session,
        [
            auth.audit(
                ctx,
                AuditAction.PLUGIN_UPLOADER_REVOKE,
                ResourceKind.PLUGIN_SETTINGS,
                resource_id=user_id,
                details={"user_id": user_id, "revoked_by": row.revoked_by},
                tenant_id=tenant_id,
            )
        ],
    )
