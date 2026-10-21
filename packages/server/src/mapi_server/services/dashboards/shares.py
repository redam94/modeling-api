"""Dashboard share links and share sessions (v0.16, plan D13).

A share link is an ``mpd_<40 base32>`` token (200 random bits) shown once; only
``HMAC-SHA256(pepper, "mapi/dashboard-share/v1:" + token)`` is stored, as for embeds. The
page carries it in the URL fragment (``/s#mpd_…``), strips it and exchanges it **once**, in
a POST body, for a short-lived session (:func:`open_session`):
``mps_<b64url(json{sid, exp, v})>.<b64url(HMAC(pepper, "mapi/share-session/v1:" + body))>``.
Every later request sends the session in ``X-Mapi-Share`` (:func:`resolve_session`); no
cookie is ever set or read.

Both run the same steps, in this order (as :func:`mapi_server.services.sharing.embed_view`):
the per-address limit **before** the format is looked at, the format, the lookup by hash (or
session id) as the gateway role (``404`` unknown; ``410 LINK_EXPIRED`` revoked, expired or
tenant not active), the per-link limit, then (sessions: the session version; exchanges: the
lock and the passcode) and a re-check in a session pinned to the link's tenant. Revoking a
link bumps its ``session_version``, so its sessions fail on their next request.

Passcodes (optional, 8-128 characters) are stored as
``scrypt$14$8$1$<salt>$<hash>`` of ``HMAC(pepper, passcode)`` and compared in constant time.
After ``share_passcode_max_failures`` wrong ones the link is locked for ``share_lockout_s``
(``429 SHARE_LOCKED`` with ``Retry-After``); the failures are audited, bounded per link.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.dashboards import (
    DASHBOARD_SHARE_TOKEN_RE,
    SHARE_SESSION_RE,
    ClientDashboard,
    DashboardShareCreate,
    ShareSession,
    ShareSessionCreate,
    generate_share_token,
    share_display,
)
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import Action, ResourceKind, TenantStatus
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, ProblemWithHeaders, unauthenticated
from mapi_server.services.audit import write_audit, write_failure_audit
from mapi_server.services.dashboards.access import (
    DashboardContext,
    actuals_mode,
    any_revision_spec,
    client_dashboard,
    effective_inputs,
    load_dashboard,
    load_revision,
)
from mapi_server.services.quotas import count_requests
from mapi_server.services.runs import now

__all__ = [
    "PASSCODE_SCHEME",
    "DashboardShareRevoker",
    "SharedView",
    "create_share",
    "hash_passcode",
    "mint_session",
    "open_session",
    "refresh_session",
    "resolve_session",
    "resolve_shared",
    "revoke_share",
    "share_token_hash",
    "shared_dashboard",
    "verify_passcode",
]

_TOKEN_PURPOSE: Final = b"mapi/dashboard-share/v1:"
_SESSION_PURPOSE: Final = b"mapi/share-session/v1:"
_PASSCODE_PURPOSE: Final = b"mapi/share-passcode/v1:"
PASSCODE_SCHEME: Final = "scrypt"
_SCRYPT_LOG_N: Final = 14
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_LEN: Final = 32
_SALT_LEN: Final = 16


# ================================================================================== hashing


def share_token_hash(pepper: bytes, token: str) -> str:
    """The stored form of a share token (keyed, so a database dump allows no guessing)."""
    digest = hmac.new(pepper, _TOKEN_PURPOSE + token.encode(), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(pepper: bytes, passcode: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
    keyed = hmac.new(pepper, _PASSCODE_PURPOSE + passcode.encode(), hashlib.sha256).digest()
    return hashlib.scrypt(
        keyed, salt=salt, n=2**log_n, r=r, p=p, dklen=_SCRYPT_LEN, maxmem=64 * 1024 * 1024
    )


def hash_passcode(pepper: bytes, passcode: str) -> str:
    """``scrypt$14$8$1$<salt>$<hash>`` of the peppered passcode (16-byte random salt)."""
    salt = os.urandom(_SALT_LEN)
    digest = _scrypt(pepper, passcode, salt, _SCRYPT_LOG_N, _SCRYPT_R, _SCRYPT_P)
    return "$".join(
        (PASSCODE_SCHEME, str(_SCRYPT_LOG_N), str(_SCRYPT_R), str(_SCRYPT_P), _b64(salt),
         _b64(digest))
    )  # fmt: skip


def verify_passcode(pepper: bytes, passcode: str, stored: str) -> bool:
    """Does ``passcode`` match ``stored`` (constant-time; a malformed record never does)?"""
    try:
        scheme, log_n, r, p, salt, digest = stored.split("$")
        if scheme != PASSCODE_SCHEME or not (10 <= int(log_n) <= 20):
            return False
        got = _scrypt(pepper, passcode, _unb64(salt), int(log_n), int(r), int(p))
        return hmac.compare_digest(got, _unb64(digest))
    except (ValueError, binascii.Error):
        return False


# =============================================================================== management


def _may_run(ctx: ServerContext) -> bool:
    auth = ctx.request
    return auth is not None and (
        auth.allows(Action.CREATE, ResourceKind.RUN) and auth.allows(Action.RUN, ResourceKind.MODEL)
    )


async def create_share(
    session: AsyncSession, ctx: ServerContext, dashboard_id: str, body: DashboardShareCreate
) -> tuple[m.DashboardShare, str]:
    """Mint a share link of a published dashboard (returns the row and the token, shown
    once). A link with inputs spends the publisher's compute, so it needs the runner role."""
    row = await load_dashboard(session, ctx, dashboard_id)
    if row.status != "published" or row.published_revision_id is None:
        raise MapiError(
            "publish the dashboard before you share it", code=ErrorCode.DASHBOARD_NOT_PUBLISHED
        )
    if body.allowed_inputs and not _may_run(ctx):
        raise MapiError(
            "a share link with what-ifs or uploads needs the runner role: its results run as "
            "the publisher and use the project's compute",
            code=ErrorCode.FORBIDDEN,
        )
    pepper = ctx.secrets.token_pepper
    token = generate_share_token()
    ts = now()
    auth = ctx.request
    user = (auth.user_id or auth.principal_id or "") if auth is not None else "system"
    passcode = body.passcode.get_secret_value() if body.passcode is not None else None
    share = m.DashboardShare(
        id=new_id("dss"),
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        dashboard_id=row.id,
        token_hash=share_token_hash(pepper, token),
        label=body.label,
        display=share_display(token),
        passcode_hash=hash_passcode(pepper, passcode) if passcode is not None else None,
        session_version=1,
        allowed_inputs=sorted(body.allowed_inputs),
        views=0,
        failed_attempts=0,
        created_by=user,
        created_at=ts,
        expires_at=ts + timedelta(days=body.expires_in_days),
    )
    session.add(share)
    await session.flush()
    if auth is not None:
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    ctx,
                    AuditAction.DASHBOARD_SHARE_CREATE,
                    ResourceKind.DASHBOARD_SHARE,
                    resource_id=share.id,
                    details={
                        "dashboard_id": row.id,
                        "expires_at": share.expires_at.isoformat(),
                        "allowed_inputs": list(share.allowed_inputs),
                        "has_passcode": passcode is not None,
                    },
                )
            ],
            session=session,
        )
    return share, token


async def revoke_share(
    session: AsyncSession, ctx: ServerContext, dashboard_id: str, share_id: str
) -> m.DashboardShare:
    """Revoke (idempotent: a second revoke changes nothing and writes no audit row). The
    link's sessions end with it (``session_version`` moves on)."""
    row = await load_dashboard(session, ctx, dashboard_id)
    share = (
        await session.execute(
            select(m.DashboardShare)
            .where(m.DashboardShare.id == share_id, m.DashboardShare.dashboard_id == row.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if share is None:
        raise NotFoundError("share link", share_id)
    if share.revoked_at is not None:
        return share
    share.revoked_at = now()
    share.session_version += 1
    await session.flush()
    if ctx.request is not None:
        await write_audit(
            ctx.db,
            [
                ctx.request.audit(
                    ctx,
                    AuditAction.DASHBOARD_SHARE_REVOKE,
                    ResourceKind.DASHBOARD_SHARE,
                    resource_id=share.id,
                    details={"dashboard_id": row.id},
                )
            ],
            session=session,
        )
    return share


class DashboardShareRevoker:
    """Revokes the tenant's live share links when its deletion starts (tenant hooks)."""

    name = "dashboard_shares"

    async def revoke(self, session: AsyncSession, tenant_id: str, *, now: datetime) -> int:
        res = await session.execute(
            update(m.DashboardShare)
            .where(m.DashboardShare.tenant_id == tenant_id, m.DashboardShare.revoked_at.is_(None))
            .values(revoked_at=now, session_version=m.DashboardShare.session_version + 1)
        )
        return int(getattr(res, "rowcount", 0) or 0)


# ================================================================================= sessions


@dataclass(frozen=True, slots=True)
class _Found:
    """The gateway's view of a share link (only the lookup columns, migration 0018)."""

    id: str
    tenant_id: str
    project_id: str
    dashboard_id: str
    expires_at: datetime
    session_version: int


@dataclass(frozen=True, slots=True)
class SharedView:
    """A resolved share session: the dashboard context and the link's expiry."""

    context: DashboardContext
    ends_at: datetime


def _retry_after(seconds: float) -> dict[str, str]:
    return {"Retry-After": str(max(1, int(seconds + 0.999)))}


async def _limit(s: AsyncSession, subject: str, limit: int, at: datetime) -> None:
    before = await count_requests(s, subject, now=at)
    if before >= limit:
        raise ProblemWithHeaders(
            "too many requests for this link; try again shortly",
            code=ErrorCode.RATE_LIMITED,
            headers=_retry_after(60 - at.second),
        )


def _expired() -> MapiError:
    return MapiError(
        "this link has ended or was removed; ask the sender for a new link",
        code=ErrorCode.LINK_EXPIRED,
    )


async def _ip_limit(ctx: ServerContext, client_ip: str | None, at: datetime) -> None:
    """The per-address limit: counted for every request, valid or not, before anything of
    the request is looked at (no free oracle for guessed tokens)."""
    settings = ctx.settings
    if not settings.rate_limits_enabled:
        return
    who = hmac.new(ctx.secrets.audit_key, (client_ip or "-").encode(), hashlib.sha256).hexdigest()
    async with ctx.db.session() as s:  # the gateway role inside a request
        await _limit(s, f"shareip:{who[:32]}", settings.share_ip_rate_per_minute, at)


async def _lookup(ctx: ServerContext, at: datetime, **where: str) -> _Found:
    """Find a live link as the gateway role (``404`` unknown, ``410`` ended), then count it
    against the per-link limit."""
    cols = (
        m.DashboardShare.id,
        m.DashboardShare.tenant_id,
        m.DashboardShare.project_id,
        m.DashboardShare.dashboard_id,
        m.DashboardShare.expires_at,
        m.DashboardShare.revoked_at,
        m.DashboardShare.session_version,
    )
    stmt = select(*cols)
    if "token_hash" in where:
        stmt = stmt.where(m.DashboardShare.token_hash == where["token_hash"])
    else:
        stmt = stmt.where(m.DashboardShare.id == where["share_id"])
    async with ctx.db.session() as s:
        row = (await s.execute(stmt)).one_or_none()
        if row is None:
            raise NotFoundError("share link", "link")
        share_id, tenant_id, project_id, dashboard_id, expires_at, revoked_at, version = row
        if revoked_at is not None or expires_at <= at:
            raise _expired()
        tenant = await s.get(m.Tenant, tenant_id)
        if tenant is None or tenant.status != TenantStatus.ACTIVE.value:
            raise _expired()
        if ctx.settings.rate_limits_enabled:
            await _limit(s, f"share:{share_id}", ctx.settings.share_rate_per_minute, at)
    return _Found(share_id, tenant_id, project_id, dashboard_id, expires_at, int(version))


def mint_session(ctx: ServerContext, share_id: str, version: int, expires_at: datetime) -> str:
    """A signed share session for ``share_id`` at ``version`` until ``expires_at``."""
    body = json.dumps(
        {"sid": share_id, "exp": int(expires_at.timestamp()), "v": version},
        separators=(",", ":"),
    )
    encoded = _b64(body.encode())
    sig = hmac.new(
        ctx.secrets.token_pepper, _SESSION_PURPOSE + encoded.encode(), hashlib.sha256
    ).digest()
    return f"mps_{encoded}.{_b64(sig)}"


def _session_claims(ctx: ServerContext, header: str | None, at: datetime) -> tuple[str, int]:
    """``(share_id, version)`` of a well-formed, correctly signed, unexpired session."""
    if not header or not SHARE_SESSION_RE.match(header):
        raise unauthenticated("a share session is required (X-Mapi-Share)")
    encoded, sig = header[4:].split(".", 1)
    want = hmac.new(
        ctx.secrets.token_pepper, _SESSION_PURPOSE + encoded.encode(), hashlib.sha256
    ).digest()
    try:
        if not hmac.compare_digest(want, _unb64(sig)):
            raise unauthenticated("the share session is not valid; open the link again")
        claims: Any = json.loads(_unb64(encoded))
        share_id, exp, version = str(claims["sid"]), int(claims["exp"]), int(claims["v"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise unauthenticated("the share session is not valid; open the link again") from None
    if exp <= int(at.timestamp()):
        raise unauthenticated("the share session has ended; open the link again")
    return share_id, version


async def _published(
    session: AsyncSession, dashboard_id: str
) -> tuple[m.Dashboard, m.DashboardRevision]:
    row = await session.get(m.Dashboard, dashboard_id)
    if row is None or row.status != "published":
        raise _expired()
    rev = await load_revision(session, row)
    if rev is None:
        raise _expired()
    return row, rev


async def _context(
    session: AsyncSession,
    ctx: ServerContext,
    share: m.DashboardShare,
    row: m.Dashboard,
    rev: m.DashboardRevision,
) -> DashboardContext:
    spec = any_revision_spec(rev)
    mode = await actuals_mode(session, ctx, rev.model_version_id) if spec.actuals.enabled else None
    return DashboardContext(
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        dashboard_id=row.id,
        revision_id=rev.id,
        principal_kind="share",
        principal_id=share.id,
        inputs=effective_inputs(spec, mode, share.allowed_inputs or []),
        is_analyst=False,
    )


def _token_of(body: ShareSessionCreate | Mapping[str, Any]) -> tuple[str, str | None]:
    """The token and passcode of an exchange (a raw body is validated only now, after the
    per-address limit counted it; a malformed token answers as an unknown one)."""
    if isinstance(body, ShareSessionCreate):
        parsed = body
    else:
        try:
            parsed = ShareSessionCreate.model_validate(body)
        except ValidationError:
            raise NotFoundError("share link", "link") from None
    if not DASHBOARD_SHARE_TOKEN_RE.match(parsed.token):
        raise NotFoundError("share link", "link")
    passcode = parsed.passcode.get_secret_value() if parsed.passcode is not None else None
    return parsed.token, passcode


async def _unlock_failed_audit(
    ctx: ServerContext, share: m.DashboardShare, client_ip: str | None, locked: bool, at: datetime
) -> None:
    ev = make_audit_event(
        action=AuditAction.DASHBOARD_SHARE_UNLOCK_FAILED,
        resource_kind=ResourceKind.DASHBOARD_SHARE,
        outcome=AuditOutcome.DENIED,
        ts=at,
        fingerprint_key=ctx.secrets.audit_key,
        tenant_id=share.tenant_id,
        project_id=share.project_id,
        resource_id=share.id,
        code=ErrorCode.SHARE_LOCKED if locked else ErrorCode.SHARE_PASSCODE_INVALID,
        client_ip=client_ip,
        details={"dashboard_id": share.dashboard_id, "locked": locked},
    )
    # anyone with the link can cause these: at most one row per link and minute. The
    # transition to locked has a budget of its own, so a burst of wrong passcodes that
    # spent the minute's row still leaves the lockout in the audit log
    subject = f"sharelock:{share.id}" if locked else f"sharefail:{share.id}"
    await write_failure_audit(ctx.db, [ev], subject, now=at, limit=1)


async def open_session(
    ctx: ServerContext,
    body: ShareSessionCreate | Mapping[str, Any],
    client_ip: str | None,
) -> ShareSession:
    """``POST /v1/share-sessions``: exchange a token (and its passcode) for a session.

    ``SHARE_PASSCODE_REQUIRED`` / ``SHARE_PASSCODE_INVALID`` (401) for a missing or wrong
    passcode, ``SHARE_LOCKED`` (429, ``Retry-After``) while the link is locked."""
    at = now()
    await _ip_limit(ctx, client_ip, at)
    token, passcode = _token_of(body)
    found = await _lookup(ctx, at, token_hash=share_token_hash(ctx.secrets.token_pepper, token))
    settings = ctx.settings
    failure: MapiError | None = None
    audit_locked: bool | None = None
    result: ShareSession | None = None
    async with ctx.db.tenant_session(found.tenant_id) as session:
        share = await session.get(m.DashboardShare, found.id, with_for_update=True)
        if share is None or share.revoked_at is not None or share.expires_at <= at:
            raise _expired()
        if share.locked_until is not None and share.locked_until > at:
            failure = ProblemWithHeaders(
                "the link is locked after too many wrong passcodes; try again later",
                code=ErrorCode.SHARE_LOCKED,
                headers=_retry_after((share.locked_until - at).total_seconds()),
            )
        elif share.passcode_hash is not None and passcode is None:
            failure = MapiError(
                "this link needs a passcode", code=ErrorCode.SHARE_PASSCODE_REQUIRED
            )
        elif share.passcode_hash is not None and not verify_passcode(
            ctx.secrets.token_pepper, passcode or "", share.passcode_hash
        ):
            share.failed_attempts += 1
            audit_locked = share.failed_attempts >= settings.share_passcode_max_failures
            if audit_locked:
                share.locked_until = at + timedelta(seconds=settings.share_lockout_s)
                share.failed_attempts = 0
                failure = ProblemWithHeaders(
                    "the link is locked after too many wrong passcodes; try again later",
                    code=ErrorCode.SHARE_LOCKED,
                    headers=_retry_after(settings.share_lockout_s),
                )
            else:
                failure = MapiError(
                    "the passcode is not correct", code=ErrorCode.SHARE_PASSCODE_INVALID
                )
        else:
            row, rev = await _published(session, share.dashboard_id)
            dctx = await _context(session, ctx, share, row, rev)
            share.failed_attempts = 0
            share.locked_until = None
            share.views += 1
            share.last_viewed_at = at
            ends = min(at + timedelta(seconds=settings.share_session_ttl_s), share.expires_at)
            dashboard = await client_dashboard(
                session, row, rev, dctx.inputs, ends_at=share.expires_at
            )
            result = ShareSession(
                session=mint_session(ctx, share.id, share.session_version, ends),
                expires_at=ends,
                dashboard=dashboard,
            )
        audited = share
    # the failure counter and the lock are committed before the refusal is answered
    if audit_locked is not None:
        await _unlock_failed_audit(ctx, audited, client_ip, audit_locked, at)
    if failure is not None:
        raise failure
    assert result is not None
    return result


async def resolve_session(
    ctx: ServerContext, header: str | None, client_ip: str | None
) -> DashboardContext:
    """The dashboard context of a share session (``X-Mapi-Share``), checked again on every
    request: ``401`` for a missing, forged, expired or superseded session, ``410`` once the
    link is revoked or expired, its tenant is not active or the dashboard is unpublished."""
    return (await resolve_shared(ctx, header, client_ip)).context


async def resolve_shared(
    ctx: ServerContext, header: str | None, client_ip: str | None
) -> SharedView:
    """:func:`resolve_session` with the link's expiry (``ClientDashboard.ends_at``)."""
    at = now()
    await _ip_limit(ctx, client_ip, at)
    share_id, version = _session_claims(ctx, header, at)
    found = await _lookup(ctx, at, share_id=share_id)
    if found.session_version != version:
        raise unauthenticated("the share session has ended; open the link again")
    async with ctx.db.tenant_session(found.tenant_id) as session:
        share = await session.get(m.DashboardShare, found.id)
        if share is None or share.revoked_at is not None or share.expires_at <= at:
            raise _expired()
        if share.session_version != version:
            raise unauthenticated("the share session has ended; open the link again")
        row, rev = await _published(session, share.dashboard_id)
        dctx = await _context(session, ctx, share, row, rev)
        return SharedView(context=dctx, ends_at=share.expires_at)


async def shared_dashboard(ctx: ServerContext, view: SharedView) -> ClientDashboard:
    """``GET /v1/shared/dashboard``: what the link shows (the published revision)."""
    dctx = view.context
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        row, rev = await _published(session, dctx.dashboard_id)
        return await client_dashboard(session, row, rev, dctx.inputs, ends_at=view.ends_at)


async def refresh_session(
    ctx: ServerContext, header: str | None, client_ip: str | None
) -> ShareSession:
    """``POST /v1/share-sessions:refresh``: a fresh session while the link is valid."""
    view = await resolve_shared(ctx, header, client_ip)
    at = now()
    dctx = view.context
    ends = min(at + timedelta(seconds=ctx.settings.share_session_ttl_s), view.ends_at)
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        share = await session.get(m.DashboardShare, dctx.principal_id)
        if share is None:  # pragma: no cover - resolved just now
            raise _expired()
        row, rev = await _published(session, dctx.dashboard_id)
        dashboard = await client_dashboard(session, row, rev, dctx.inputs, ends_at=view.ends_at)
        return ShareSession(
            session=mint_session(ctx, share.id, share.session_version, ends),
            expires_at=ends,
            dashboard=dashboard,
        )
