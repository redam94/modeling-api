"""Self-service sign-up and e-mail verification (plan §9 "Compute abuse", §14 M3).

``POST /auth/signup`` (public):

1. refuses early what can never sign up (:func:`mapi_spec.trust.check_signup`: terms,
   malformed or disposable addresses) and failed bot checks;
2. applies the sign-up rate limits: per client address and hour, per e-mail domain and day,
   and platform-wide per hour (``RATE_LIMITED`` with ``Retry-After``);
3. stores the sign-up with the keyed digest of a fresh single-use token (24 h) and e-mails
   the link. An address that already has an account gets exactly the same answer, so the
   endpoint cannot be used to find out who has one.

``POST /auth/verify-email`` (public) consumes the token and creates, in one transaction, a
``public``-tier tenant, its ``default`` project, the user (an invitation claimed at the first
sign-in with that verified address, or the existing account) as tenant admin, and the
tenant's verification record. Then the **Sybil heuristics** run: several workspaces from the
same client address, browser (device cookie) or e-mail within the look-back window, or a
burst from one domain, flag the sign-up for operator review (``GET /v1/admin/signups``).
A flag never blocks the sign-up unless ``MAPI_SIGNUP_FLAGGED_SUSPEND`` is set.

Everything is audited. Client addresses and device cookies are stored only as keyed
digests; the token only as an HMAC under a key derived from the server secret.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Final

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction, AuditEvent, AuditOutcome, client_fingerprint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import ProjectRole, ResourceKind, TenantStatus, TrustTier
from mapi_spec.trust import (
    EMAIL_VERIFICATION_TTL,
    SYBIL_FLAGS,
    SignupRequest,
    SignupReview,
    SignupReviewDecision,
    SignupReviewState,
    SignupState,
    VerificationMethod,
    VerificationResult,
    check_signup,
    generate_verification_token,
)
from pydantic import JsonValue
from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.identity import invite_user
from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError, ProblemWithHeaders
from mapi_server.services.audit import write_audit, write_failure_audit
from mapi_server.services.mail import MailerError, MailMessage
from mapi_server.services.quotas import count_requests
from mapi_server.services.runs import now
from mapi_server.services.tenancy import grant
from mapi_server.services.trust import trust_services

__all__ = [
    "DEFAULT_PROJECT",
    "DEVICE_COOKIE",
    "REVIEW_SUSPEND_PREFIX",
    "list_signups",
    "request_signup",
    "review_signup",
    "signup_dto",
    "sweep_signups",
    "tenant_slug",
    "token_digest",
    "verify_email",
]

log = logging.getLogger(__name__)

DEFAULT_PROJECT: Final = "default"
DEVICE_COOKIE: Final = "mapi_device"
"""A random, HttpOnly per-browser id (1 year): the "same device" Sybil signal."""
REVIEW_SUSPEND_PREFIX: Final = "sign-up review: "
"""``status_reason`` prefix of tenants suspended by the sign-up review (an approval lifts
exactly these suspensions, never an operator's own)."""
_SLUG_MAX: Final = 40
_SLUG_RE: Final = re.compile(r"[^a-z0-9]+")


# ================================================================================= helpers


def _key(ctx: ServerContext, purpose: str) -> bytes:
    """A purpose-bound key derived from the server's token pepper."""
    return hmac.new(ctx.secrets.token_pepper, f"mapi.{purpose}.v1".encode(), "sha256").digest()


def token_digest(ctx: ServerContext, token: str) -> str:
    """The stored form of a verification token (HMAC-SHA256, hex)."""
    return hmac.new(_key(ctx, "email-verification"), token.encode(), hashlib.sha256).hexdigest()


def _device_hash(ctx: ServerContext, device_id: str | None) -> str | None:
    if not device_id or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", device_id):
        return None
    return client_fingerprint(device_id, _key(ctx, "device"))


def tenant_slug(label: str) -> str:
    """A tenant-id slug from an organisation name or an e-mail's local part."""
    slug = _SLUG_RE.sub("-", label.strip().lower()).strip("-")[:_SLUG_MAX].strip("-")
    return slug if len(slug) >= 3 else f"ws-{slug}".strip("-") if slug else "workspace"


async def _free_tenant_id(s: AsyncSession, label: str) -> str:
    base = tenant_slug(label)
    candidate = base
    for _ in range(20):
        if await s.get(m.Tenant, candidate) is None:
            return candidate
        candidate = f"{base[: _SLUG_MAX - 5]}-{secrets.token_hex(2)}"
    raise MapiError("could not allocate a workspace id; try again", code=ErrorCode.CONFLICT)


def _retry_after(oldest: datetime | None, window: timedelta, at: datetime) -> str:
    if oldest is None:
        return str(int(window.total_seconds()))
    return str(max(1, int((oldest + window - at).total_seconds()) + 1))


def signup_dto(row: m.Signup) -> SignupReview:
    flags = [f for f in (row.flags or []) if isinstance(f, str)]
    return SignupReview(
        id=row.id,
        email=row.email,
        email_domain=row.email_domain,
        name=row.name,
        organisation=row.organisation,
        state=SignupState(row.state),
        review=SignupReviewState(row.review),
        flags=flags,
        reasons_plain=[SYBIL_FLAGS.get(f, f) for f in flags],
        tenant_id=row.created_tenant_id,
        ip_hash=row.ip_hash,
        device_hash=row.device_hash,
        created_at=row.created_at,
        expires_at=row.expires_at,
        verified_at=row.verified_at,
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        review_reason=row.review_reason,
    )


# ================================================================================= sign-up


async def _rejected(
    ctx: ServerContext, auth: RequestAuth, exc: MapiError, *, domain: str | None
) -> None:
    """Audit a refused sign-up, at most a few times per client and minute."""
    subject = f"signupfail:{client_fingerprint(auth.client_ip, ctx.secrets.audit_key) or '-'}"
    details: dict[str, JsonValue] = {"reason": exc.code.value}
    if domain:
        details["email_domain"] = domain
    ev = auth.audit(
        ctx,
        AuditAction.AUTH_SIGNUP_REJECTED,
        ResourceKind.TENANT,
        AuditOutcome.DENIED,
        code=exc.code,
        details=details,
    )
    await write_failure_audit(ctx.db, [ev], subject[:96], now=now())


async def _check_rate(
    s: AsyncSession, ctx: ServerContext, *, ip_hash: str | None, domain: str, at: datetime
) -> None:
    st = ctx.settings
    checks: list[tuple[str, ColumnElement[bool] | None, timedelta, int]] = [
        ("on this platform", None, timedelta(hours=1), st.signup_global_per_hour),
        ("from this e-mail domain", m.Signup.email_domain == domain, timedelta(days=1),
         st.signup_per_domain_per_day),
    ]  # fmt: skip
    if ip_hash is not None:
        checks.append(
            ("from this network address", m.Signup.ip_hash == ip_hash, timedelta(hours=1),
             st.signup_per_ip_per_hour)
        )  # fmt: skip
    for what, cond, window, limit in checks:
        stmt = select(func.count(), func.min(m.Signup.created_at)).where(
            m.Signup.created_at > at - window
        )
        if cond is not None:
            stmt = stmt.where(cond)
        count, oldest = (await s.execute(stmt)).one()
        if int(count) >= limit:
            raise ProblemWithHeaders(
                f"too many sign-ups {what}; try again later",
                code=ErrorCode.RATE_LIMITED,
                headers={"Retry-After": _retry_after(oldest, window, at)},
            )


def _verification_mail(ctx: ServerContext, to: str, name: str, token: str) -> MailMessage:
    link = ctx.settings.verify_link(token)
    hours = int(EMAIL_VERIFICATION_TTL.total_seconds() // 3600)
    body = (
        f"Hello {name},\n\n"
        "Someone (hopefully you) asked to create a modeling-api workspace with this address.\n"
        f"Open this link within {hours} hours to confirm it and create the workspace:\n\n"
        f"{link}\n\n"
        "If you already have an account, the link adds a new workspace to it.\n"
        "If this was not you, ignore this e-mail: nothing is created without the link.\n"
    )
    return MailMessage(
        to=to,
        subject="Confirm your e-mail address for modeling-api",
        body=body,
        tags={"kind": "email_verification", "token": token, "link": link},
    )


async def request_signup(
    ctx: ServerContext, auth: RequestAuth, body: SignupRequest, *, device_id: str | None
) -> None:
    """Accept a sign-up and e-mail its verification link (see the module docstring)."""
    if not ctx.settings.signup_open:
        raise MapiError(
            "self-service sign-up is not available on this server", code=ErrorCode.FORBIDDEN
        )
    trust = trust_services(ctx)
    domain: str | None = None
    try:
        domain = check_signup(body, trust.email_policy)
        if not await trust.challenge.verify(body.challenge, client_ip=auth.client_ip):
            raise MapiError(
                "the bot check failed; reload the page and try again",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/challenge",
            )
    except MapiError as exc:
        await _rejected(ctx, auth, exc, domain=domain)
        raise
    at = now()
    email = body.email.strip().lower()
    ip_hash = client_fingerprint(auth.client_ip, ctx.secrets.audit_key)
    token = generate_verification_token()
    signup_id = new_id("sgn")
    try:
        async with ctx.db.session() as s:
            await _check_rate(s, ctx, ip_hash=ip_hash, domain=domain, at=at)
            # only the newest link of an address works
            await s.execute(
                update(m.Signup)
                .where(func.lower(m.Signup.email) == email, m.Signup.state == "pending")
                .values(state=SignupState.EXPIRED.value, token_hash=None)
            )
            s.add(
                m.Signup(
                    id=signup_id,
                    email=email,
                    email_domain=domain,
                    name=body.name.strip(),
                    organisation=body.organisation.strip() if body.organisation else None,
                    terms_version=body.terms_version,
                    token_hash=token_digest(ctx, token),
                    state=SignupState.PENDING.value,
                    review=SignupReviewState.NONE.value,
                    flags=[],
                    ip_hash=ip_hash,
                    device_hash=_device_hash(ctx, device_id),
                    created_at=at,
                    expires_at=at + EMAIL_VERIFICATION_TTL,
                )
            )
            await write_audit(
                ctx.db,
                [
                    auth.audit(
                        ctx,
                        AuditAction.AUTH_SIGNUP,
                        ResourceKind.TENANT,
                        resource_id=signup_id,
                        details={"email_domain": domain},
                    )
                ],
                session=s,
            )
    except ProblemWithHeaders as exc:
        await _rejected(ctx, auth, exc, domain=domain)
        raise
    try:
        await trust.mailer.send(_verification_mail(ctx, email, body.name.strip(), token))
    except MailerError:  # the answer stays the same: no oracle, and the sign-up can be redone
        log.exception("could not send the verification e-mail of sign-up %s", signup_id)


# ============================================================================ verification


def _link_expired() -> MapiError:
    return MapiError(
        "this link is invalid, expired or was already used; sign up again for a new one",
        code=ErrorCode.LINK_EXPIRED,
    )


async def _sybil_flags(
    s: AsyncSession, ctx: ServerContext, row: m.Signup, at: datetime
) -> list[str]:
    """Which heuristics this (being verified) sign-up trips; it is not counted itself."""
    st = ctx.settings
    since = at - timedelta(days=st.signup_sybil_window_days)
    verified = (
        m.Signup.created_tenant_id.is_not(None),
        m.Signup.verified_at > since,
        m.Signup.id != row.id,
    )
    flags: list[str] = []
    signals = (
        ("same_ip", m.Signup.ip_hash == row.ip_hash, row.ip_hash),
        ("same_device", m.Signup.device_hash == row.device_hash, row.device_hash),
        ("same_email", func.lower(m.Signup.email) == row.email.lower(), row.email),
    )
    for flag, cond, value in signals:
        if value is None:
            continue
        n = (await s.execute(select(func.count()).where(cond, *verified))).scalar_one()
        if int(n) >= st.signup_sybil_threshold:
            flags.append(flag)
    burst = (
        await s.execute(
            select(func.count()).where(
                m.Signup.email_domain == row.email_domain,
                m.Signup.created_tenant_id.is_not(None),
                m.Signup.verified_at > at - timedelta(hours=1),
                m.Signup.id != row.id,
            )
        )
    ).scalar_one()
    if int(burst) >= 2 * st.signup_sybil_threshold:
        flags.append("domain_burst")
    return flags


async def verify_email(ctx: ServerContext, auth: RequestAuth, token: str) -> VerificationResult:
    """Consume a verification token and create the sign-up's public tenant."""
    at = now()
    ip = client_fingerprint(auth.client_ip, ctx.secrets.audit_key)
    if ip is not None:
        async with ctx.db.session() as s:
            before = await count_requests(s, f"verify:{ip}"[:96], now=at)
        if before >= ctx.settings.verify_per_ip_per_minute:
            raise ProblemWithHeaders(
                "too many verification attempts; try again in a minute",
                code=ErrorCode.RATE_LIMITED,
                headers={"Retry-After": "60"},
            )
    result: VerificationResult | None = None
    async with ctx.db.session() as s:
        row = (
            await s.execute(
                select(m.Signup)
                .where(m.Signup.token_hash == token_digest(ctx, token))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.state != SignupState.PENDING.value:
            raise _link_expired()
        if row.expires_at <= at or row.review == SignupReviewState.REJECTED.value:
            row.state = SignupState.EXPIRED.value  # committed: the link is dead for good
            row.token_hash = None
        else:
            result = await _create_workspace(s, ctx, auth, row, at)
    if result is None:
        raise _link_expired()
    return result


async def _create_workspace(
    s: AsyncSession, ctx: ServerContext, auth: RequestAuth, row: m.Signup, at: datetime
) -> VerificationResult:
    label = row.organisation or row.email.split("@", 1)[0]
    tenant_id = await _free_tenant_id(s, label)
    flags = await _sybil_flags(s, ctx, row, at)
    suspend = bool(flags) and ctx.settings.signup_flagged_suspend
    s.add(
        m.Tenant(
            id=tenant_id,
            name=row.organisation or f"{row.name}'s workspace"[:200],
            trust_tier=TrustTier.PUBLIC.value,
            status=(TenantStatus.SUSPENDED if suspend else TenantStatus.ACTIVE).value,
            status_reason=(REVIEW_SUSPEND_PREFIX + "waiting for an operator") if suspend else None,
            data_residency=ctx.settings.effective_default_residency(),
            created_at=at,
            updated_at=at,
        )
    )
    await s.flush()
    s.add(
        m.Project(
            tenant_id=tenant_id,
            id=DEFAULT_PROJECT,
            project_id=DEFAULT_PROJECT,
            name="Default project",
            created_at=at,
        )
    )
    s.add(
        m.TenantVerification(
            tenant_id=tenant_id,
            email_verified=True,
            email_verified_at=at,
            identity_verified=False,
            method=VerificationMethod.EMAIL.value,
            verified_at=at,
            updated_at=at,
        )
    )
    await s.flush()
    user = await invite_user(s, row.email)
    if user.name is None:
        user.name = row.name
    await grant(s, tenant_id=tenant_id, project_id=None, user=user, role=ProjectRole.ADMIN)
    row.state = SignupState.VERIFIED.value
    row.token_hash = None
    row.verified_at = at
    row.user_id = user.id
    row.created_tenant_id = tenant_id
    events = [
        auth.audit(
            ctx, AuditAction.AUTH_EMAIL_VERIFIED, ResourceKind.TENANT, resource_id=row.id,
            tenant_id=tenant_id, project_id=DEFAULT_PROJECT,
            details={"email_domain": row.email_domain, "user_id": user.id},
        ),
        auth.audit(
            ctx, AuditAction.TENANT_CREATE, ResourceKind.TENANT, resource_id=tenant_id,
            tenant_id=tenant_id, project_id=DEFAULT_PROJECT,
            details={"trust_tier": TrustTier.PUBLIC.value, "source": "signup"},
        ),
    ]  # fmt: skip
    if flags:
        row.review = SignupReviewState.FLAGGED.value
        row.flags = flags
        events.append(
            auth.audit(
                ctx, AuditAction.AUTH_SIGNUP_FLAGGED, ResourceKind.TENANT,
                AuditOutcome.DENIED if suspend else AuditOutcome.SUCCESS, resource_id=row.id,
                tenant_id=tenant_id, details={"flags": list(flags), "suspended": suspend},
            )
        )  # fmt: skip
        log.warning("sign-up %s (tenant %s) flagged for review: %s", row.id, tenant_id, flags)
    await write_audit(ctx.db, events, session=s)
    return VerificationResult(
        email=row.email,
        tenant_id=tenant_id,
        project_id=DEFAULT_PROJECT,
        trust_tier=TrustTier.PUBLIC,
        login_url="/auth/login?return_to=%2F",
    )


# ================================================================================ review


async def list_signups(
    s: AsyncSession,
    *,
    review: SignupReviewState | None,
    tenant_id: str | None,
    limit: int,
    after: str | None,
) -> tuple[list[SignupReview], bool]:
    """A page of sign-ups, newest first (ids sort by creation time), and whether more exist."""
    stmt = select(m.Signup)
    if review is not None:
        stmt = stmt.where(m.Signup.review == review.value)
    if tenant_id is not None:
        stmt = stmt.where(m.Signup.created_tenant_id == tenant_id)
    if after is not None:
        stmt = stmt.where(m.Signup.id < after)
    rows = list((await s.execute(stmt.order_by(m.Signup.id.desc()).limit(limit + 1))).scalars())
    return [signup_dto(r) for r in rows[:limit]], len(rows) > limit


async def review_signup(
    s: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    signup_id: str,
    body: SignupReviewDecision,
) -> SignupReview:
    """An operator's decision; a rejection suspends the tenant, an approval lifts a
    suspension the review itself caused."""
    row = (
        await s.execute(select(m.Signup).where(m.Signup.id == signup_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("sign-up", signup_id)
    at = now()
    reject = body.decision == "reject"
    row.review = (SignupReviewState.REJECTED if reject else SignupReviewState.APPROVED).value
    row.reviewed_by = auth.principal_id
    row.reviewed_at = at
    row.review_reason = body.reason
    if row.state == SignupState.PENDING.value and reject:
        row.state = SignupState.EXPIRED.value
        row.token_hash = None
    events = [
        auth.audit(
            ctx, AuditAction.AUTH_SIGNUP_REVIEW, ResourceKind.TENANT, resource_id=row.id,
            tenant_id=row.created_tenant_id,
            details={"decision": body.decision, "reason": body.reason},
        )
    ]  # fmt: skip
    if row.created_tenant_id is not None:
        tenant = await s.get(m.Tenant, row.created_tenant_id, with_for_update=True)
        if tenant is not None:
            if tenant.status == TenantStatus.DELETING.value:
                raise Conflict(f"tenant {tenant.id} is being deleted")
            if reject and tenant.status == TenantStatus.ACTIVE.value:
                tenant.status = TenantStatus.SUSPENDED.value
                tenant.status_reason = (REVIEW_SUSPEND_PREFIX + body.reason)[:500]
                tenant.updated_at = at
                events.append(_tenant_event(ctx, auth, AuditAction.TENANT_SUSPEND, tenant.id,
                                            body.reason))  # fmt: skip
            elif (
                not reject
                and tenant.status == TenantStatus.SUSPENDED.value
                and (tenant.status_reason or "").startswith(REVIEW_SUSPEND_PREFIX)
            ):
                tenant.status = TenantStatus.ACTIVE.value
                tenant.status_reason = None
                tenant.updated_at = at
                events.append(_tenant_event(ctx, auth, AuditAction.TENANT_REACTIVATE, tenant.id,
                                            body.reason))  # fmt: skip
    await write_audit(ctx.db, events, session=s)
    return signup_dto(row)


def _tenant_event(
    ctx: ServerContext, auth: RequestAuth, action: AuditAction, tenant_id: str, reason: str
) -> AuditEvent:
    return auth.audit(
        ctx, action, ResourceKind.TENANT, resource_id=tenant_id, tenant_id=tenant_id,
        details={"reason": reason, "source": "signup_review"},
    )  # fmt: skip


async def sweep_signups(s: AsyncSession, ctx: ServerContext, *, at: datetime) -> int:
    """Expire stale links and delete unverified sign-ups past their retention (their e-mail
    is personal data with no purpose left). Verified ones stay for the Sybil look-back."""
    await s.execute(
        update(m.Signup)
        .where(m.Signup.state == SignupState.PENDING.value, m.Signup.expires_at <= at)
        .values(state=SignupState.EXPIRED.value, token_hash=None)
    )
    res = await s.execute(
        delete(m.Signup).where(
            m.Signup.state == SignupState.EXPIRED.value,
            m.Signup.created_at < at - timedelta(days=ctx.settings.signup_retention_days),
        )
    )
    return int(getattr(res, "rowcount", 0) or 0)
