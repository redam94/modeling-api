"""Tenant verification and the code-upload rule (plan §9 "Compute abuse", cut lines A/B).

* E-mail verification is recorded when a sign-up is verified
  (:mod:`mapi_server.services.signup`).
* Identity verification is recorded by an operator (``PUT /v1/admin/tenants/{id}/
  verification``, audited) or from an :class:`~mapi_server.services.trust.IdentityVerifier`
  provider's answer (:func:`apply_identity_check`).
* :func:`require_uploaded_code_allowed` enforces the rule at run admission (and must be
  called by the plugin upload of M4): uploaded code needs an ``internal`` or
  ``self-hosted-trusted`` tenant, or identity verification; public tenants never run it
  before cut line B (``MAPI_PUBLIC_CODE_UPLOAD_ENABLED``). Built-in plugins and declarative
  specs are always allowed: untrusted data, trusted code.

The request-path role may only *read* ``tenant_verifications`` (migration
``0010_trust_lifecycle``), so a tenant session can never mark its own tenant verified.
"""

from __future__ import annotations

from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import ResourceKind, TrustTier
from mapi_spec.trust import (
    VerificationMethod,
    VerificationStatus,
    VerificationUpdate,
    uploaded_code_allowed,
)
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.audit import write_audit
from mapi_server.services.runs import now
from mapi_server.services.trust import IdentityCheck, trust_services

__all__ = [
    "apply_identity_check",
    "code_upload_rule",
    "put_verification",
    "require_uploaded_code_allowed",
    "verification_status",
]


def code_upload_rule(
    ctx: ServerContext, tenant: m.Tenant, row: m.TenantVerification | None
) -> tuple[bool, str]:
    """May ``tenant`` run uploaded code, and why (plain language)."""
    return uploaded_code_allowed(
        TrustTier(tenant.trust_tier),
        identity_verified=bool(row and row.identity_verified),
        public_code_enabled=ctx.settings.public_code_upload_enabled,
    )


def _status(
    ctx: ServerContext, tenant: m.Tenant, row: m.TenantVerification | None
) -> VerificationStatus:
    allowed, reason = code_upload_rule(ctx, tenant, row)
    return VerificationStatus(
        tenant_id=tenant.id,
        trust_tier=TrustTier(tenant.trust_tier),
        email_verified=bool(row and row.email_verified),
        identity_verified=bool(row and row.identity_verified),
        method=VerificationMethod(row.method) if row is not None else VerificationMethod.NONE,
        verified_at=row.verified_at if row is not None else None,
        uploaded_code_allowed=allowed,
        reason_plain=reason,
    )


async def verification_status(
    session: AsyncSession, ctx: ServerContext, tenant_id: str
) -> VerificationStatus:
    """``GET /v1/tenants/{tenant_id}/verification`` (tenant session or platform session)."""
    tenant = await session.get(m.Tenant, tenant_id)
    if tenant is None:
        raise NotFoundError("tenant", tenant_id)
    row = await session.get(m.TenantVerification, tenant_id)
    return _status(ctx, tenant, row)


async def _upsert(session: AsyncSession, tenant_id: str) -> tuple[m.Tenant, m.TenantVerification]:
    tenant = await session.get(m.Tenant, tenant_id)
    if tenant is None:
        raise NotFoundError("tenant", tenant_id)
    row = await session.get(m.TenantVerification, tenant_id, with_for_update=True)
    if row is None:
        row = m.TenantVerification(
            tenant_id=tenant_id,
            email_verified=False,
            identity_verified=False,
            method=VerificationMethod.NONE.value,
            updated_at=now(),
        )
        session.add(row)
    return tenant, row


async def put_verification(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    tenant_id: str,
    body: VerificationUpdate,
) -> VerificationStatus:
    """An operator records (or revokes) identity verification; audited with the reason."""
    tenant, row = await _upsert(session, tenant_id)
    at = now()
    before = row.identity_verified
    row.identity_verified = body.identity_verified
    row.method = body.method.value
    row.verified_at = at if body.identity_verified else None
    row.verified_by = auth.principal_id
    row.reason = body.reason
    row.updated_at = at
    await session.flush()
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.TENANT_VERIFICATION_UPDATE,
                ResourceKind.TENANT,
                resource_id=tenant_id,
                tenant_id=tenant_id,
                details={
                    "identity_verified": body.identity_verified,
                    "was": before,
                    "method": body.method.value,
                    "reason": body.reason,
                },
            )
        ],
        session=session,
    )
    return _status(ctx, tenant, row)


async def apply_identity_check(
    session: AsyncSession, ctx: ServerContext, tenant_id: str, check: IdentityCheck
) -> VerificationStatus:
    """Record a provider's answer (platform role; audited as the platform)."""
    tenant, row = await _upsert(session, tenant_id)
    at = now()
    row.identity_verified = check.verified
    row.method = VerificationMethod(check.method).value
    row.verified_at = at if check.verified else None
    row.verified_by = f"provider:{trust_provider_name(ctx)}"
    row.provider_reference = check.reference
    row.reason = check.reason[:500] or None
    row.updated_at = at
    await session.flush()
    await write_audit(
        ctx.db,
        [
            make_audit_event(
                action=AuditAction.TENANT_VERIFICATION_UPDATE,
                resource_kind=ResourceKind.TENANT,
                outcome=AuditOutcome.SUCCESS if check.verified else AuditOutcome.DENIED,
                ts=at,
                fingerprint_key=ctx.secrets.audit_key,
                tenant_id=tenant_id,
                resource_id=tenant_id,
                details={
                    "identity_verified": check.verified,
                    "method": row.method,
                    "provider": trust_provider_name(ctx),
                    "reference": check.reference,
                },
            )
        ],
        session=session,
    )
    return _status(ctx, tenant, row)


def trust_provider_name(ctx: ServerContext) -> str:
    return trust_services(ctx).identity.name


async def require_uploaded_code_allowed(
    session: AsyncSession, ctx: ServerContext, tenant_id: str
) -> None:
    """``IDENTITY_VERIFICATION_REQUIRED`` (403) unless the tenant may run uploaded code."""
    tenant = await session.get(m.Tenant, tenant_id)
    if tenant is None:
        raise NotFoundError("tenant", tenant_id)
    row = await session.get(m.TenantVerification, tenant_id)
    allowed, reason = code_upload_rule(ctx, tenant, row)
    if not allowed:
        raise MapiError(
            f"{reason} Built-in models and declarative specs are always available.",
            code=ErrorCode.IDENTITY_VERIFICATION_REQUIRED,
        )
