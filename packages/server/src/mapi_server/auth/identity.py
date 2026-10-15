"""Users, browser sessions, login transactions, API tokens and memberships.

These lookups happen *before* the tenant of a request is known (which token is this, which
tenants does this user belong to), so they run on a platform session: the gateway role in
the API (identity tables only, no row-level-security bypass). Every function here filters
explicitly by the identifier it was given; none returns another principal's rows.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from mapi_spec.api import new_id
from mapi_spec.auth import SESSION_ABSOLUTE_TTL, SESSION_IDLE_TTL, LoginTransaction
from mapi_spec.tenancy import Membership, PrincipalKind, ProjectRole, UserPrincipal
from mapi_spec.tokens import BudgetPeriod, TokenRecord
from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.services.runs import now

__all__ = [
    "LOCAL_ISSUER",
    "LOCAL_USER_ID",
    "SESSION_TOUCH_INTERVAL",
    "AccountDisabled",
    "VerifiedIdentity",
    "claim_or_create_user",
    "create_session",
    "delete_expired_session",
    "delete_session",
    "invite_user",
    "is_operator",
    "load_session",
    "local_principal",
    "membership_dto",
    "memberships_of",
    "pop_login",
    "save_login",
    "session_key",
    "sweep_expired_logins",
    "token_record",
    "user_principal",
]

LOCAL_USER_ID: Final = "usr_" + "0" * 26
"""The principal of credential-less requests in the single-tenant profile."""
LOCAL_ISSUER: Final = "https://localhost/mapi-local"
SESSION_TOUCH_INTERVAL: Final = timedelta(minutes=1)
_LOGIN_SWEEP_AGE: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """Claims of a verified ID token (issuer, audience, nonce and expiry already checked)."""

    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    name: str | None


def session_key(cookie_value: str) -> str:
    """Primary key of a session or login row: the SHA-256 of the cookie value."""
    return "sha256:" + hashlib.sha256(cookie_value.encode()).hexdigest()


def user_principal(user: m.User) -> UserPrincipal:
    if user.issuer is None or user.subject is None:
        raise ValueError("an invited user has not signed in yet")
    return UserPrincipal(
        id=user.id,
        issuer=user.issuer,
        subject=user.subject,
        email=user.email,
        email_verified=user.email_verified,
        name=user.name,
    )


def local_principal() -> UserPrincipal:
    return UserPrincipal(
        id=LOCAL_USER_ID,
        issuer=LOCAL_ISSUER,
        subject="local",
        email=None,
        email_verified=False,
        name="Local administrator",
    )


def _norm_email(email: str | None) -> str | None:
    return email.strip().lower() if email else None


class AccountDisabled(Exception):
    """A verified sign-in for an account an operator disabled (it stays disabled)."""


def is_operator(user: m.User, operator_emails: Iterable[str]) -> bool:
    """Is this user a platform operator under the *current* configuration?

    Decided on every request from the verified e-mail, so removing an address from the
    operator list takes effect at once for sessions and CLI ID tokens alike (the stored
    ``users.operator`` flag only records the status at the last sign-in).
    """
    email = _norm_email(user.email)
    if not email or not user.email_verified or user.issuer is None:
        return False
    return email in {e.strip().lower() for e in operator_emails if e.strip()}


# ======================================================================================= users


async def claim_or_create_user(
    s: AsyncSession, ident: VerifiedIdentity, *, operator_emails: Iterable[str] = ()
) -> m.User:
    """The user for a verified sign-in: the known ``(issuer, subject)``, else the pending
    invitation for the *verified* e-mail (claimed now), else a new user.

    Raises :class:`AccountDisabled` for a disabled account: signing in never re-activates
    it (only an invitation becomes active at its first sign-in).
    """
    ts = now()
    email = _norm_email(ident.email)
    operators = {e.strip().lower() for e in operator_emails if e.strip()}
    user = (
        await s.execute(
            select(m.User)
            .where(m.User.issuer == ident.issuer, m.User.subject == ident.subject)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if user is None and email and ident.email_verified:
        user = (
            await s.execute(
                select(m.User)
                .where(m.User.issuer.is_(None), func.lower(m.User.email) == email)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if user is not None:
            user.issuer, user.subject = ident.issuer, ident.subject
    if user is not None and user.status == "disabled":
        raise AccountDisabled(user.id)
    if user is None:
        user = m.User(
            id=new_id("usr"),
            issuer=ident.issuer,
            subject=ident.subject,
            created_at=ts,
            operator=False,
        )
        s.add(user)
    user.email = email
    user.email_verified = ident.email_verified
    user.name = ident.name
    user.status = "active"
    user.last_login_at = ts
    # operator status follows the configuration at every sign-in (verified e-mail only)
    user.operator = bool(email and ident.email_verified and email in operators)
    await s.flush()
    await s.execute(
        update(m.Membership)
        .where(m.Membership.principal_id == user.id)
        .values(email=user.email, display_name=user.name)
    )
    return user


async def invite_user(s: AsyncSession, email: str) -> m.User:
    """The user with this e-mail: a signed-in user whose e-mail is verified, an existing
    invitation, or a new invitation (claimed at the first sign-in with that verified
    e-mail)."""
    norm = _norm_email(email)
    assert norm is not None
    found = (
        await s.execute(
            select(m.User)
            .where(
                func.lower(m.User.email) == norm,
                (m.User.issuer.is_(None)) | (m.User.email_verified.is_(True)),
            )
            .order_by(m.User.issuer.is_(None), m.User.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if found is not None:
        return found
    user = m.User(
        id=new_id("usr"),
        email=norm,
        email_verified=False,
        status="invited",
        operator=False,
        created_at=now(),
    )
    s.add(user)
    await s.flush()
    return user


# ==================================================================================== sessions


async def create_session(
    s: AsyncSession,
    user: m.User,
    *,
    ip_hash: str | None = None,
    user_agent_hash: str | None = None,
) -> tuple[str, m.WebSession]:
    """A new session: returns the cookie value (shown once) and the row (keyed by its hash)."""
    value = secrets.token_urlsafe(32)
    ts = now()
    row = m.WebSession(
        id=session_key(value),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        created_at=ts,
        last_seen_at=ts,
        expires_at=ts + SESSION_ABSOLUTE_TTL,
        ip_hash=ip_hash,
        user_agent_hash=user_agent_hash,
    )
    s.add(row)
    await s.flush()
    return value, row


async def load_session(
    s: AsyncSession, cookie_value: str, *, at: datetime | None = None
) -> tuple[m.WebSession, m.User] | None:
    """The live session for a cookie (absolute and idle expiry enforced), touched at most
    once per :data:`SESSION_TOUCH_INTERVAL`. An expired session is deleted in ``s`` (callers
    that then fail the request use :func:`delete_expired_session` in a new transaction, and
    the housekeeper sweeps the rest)."""
    if not cookie_value or len(cookie_value) > 256:
        return None
    ts = at or now()
    row = await s.get(m.WebSession, session_key(cookie_value))
    if row is None:
        return None
    if ts >= row.expires_at or ts - row.last_seen_at > SESSION_IDLE_TTL:
        await s.delete(row)
        return None
    user = await s.get(m.User, row.user_id)
    if user is None or user.status != "active" or user.issuer is None:
        return None
    if ts - row.last_seen_at >= SESSION_TOUCH_INTERVAL:
        row.last_seen_at = ts
    return row, user


def _expired_session(at: datetime) -> ColumnElement[bool]:
    return (m.WebSession.expires_at <= at) | (m.WebSession.last_seen_at < at - SESSION_IDLE_TTL)


async def delete_expired_session(
    s: AsyncSession, cookie_value: str, *, at: datetime | None = None
) -> bool:
    """Delete the session of this cookie if (and only if) it has expired."""
    if not cookie_value or len(cookie_value) > 256:
        return False
    result = await s.execute(
        delete(m.WebSession).where(
            m.WebSession.id == session_key(cookie_value), _expired_session(at or now())
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def sweep_expired_logins(s: AsyncSession, *, at: datetime | None = None) -> int:
    """Delete every expired session and every sign-in abandoned for longer than an hour
    (a pending login is valid for :data:`~mapi_spec.auth.LOGIN_TTL`)."""
    ts = at or now()
    sessions = await s.execute(delete(m.WebSession).where(_expired_session(ts)))
    logins = await s.execute(
        delete(m.LoginTransactionRow).where(
            m.LoginTransactionRow.created_at < ts - _LOGIN_SWEEP_AGE
        )
    )
    return int(getattr(sessions, "rowcount", 0) or 0) + int(getattr(logins, "rowcount", 0) or 0)


async def delete_session(s: AsyncSession, cookie_value: str) -> bool:
    result = await s.execute(
        delete(m.WebSession).where(m.WebSession.id == session_key(cookie_value))
    )
    return bool(getattr(result, "rowcount", 0))


async def save_login(s: AsyncSession, tx: LoginTransaction) -> str:
    """Store a pending login; returns the login cookie value."""
    value = secrets.token_urlsafe(32)
    s.add(
        m.LoginTransactionRow(
            id=session_key(value),
            state=tx.state,
            nonce=tx.nonce,
            code_verifier=tx.code_verifier,
            return_to=tx.return_to,
            created_at=tx.created_at,
        )
    )
    # opportunistic clean-up of abandoned logins
    await s.execute(
        delete(m.LoginTransactionRow).where(
            m.LoginTransactionRow.created_at < now() - _LOGIN_SWEEP_AGE
        )
    )
    await s.flush()
    return value


async def pop_login(s: AsyncSession, cookie_value: str) -> LoginTransaction | None:
    """Take a pending login (single use: the row is deleted whatever happens next)."""
    if not cookie_value or len(cookie_value) > 256:
        return None
    row = await s.get(m.LoginTransactionRow, session_key(cookie_value))
    if row is None:
        return None
    await s.delete(row)
    await s.flush()
    return LoginTransaction(
        state=row.state,
        nonce=row.nonce,
        code_verifier=row.code_verifier,
        return_to=row.return_to,
        created_at=row.created_at,
    )


# ====================================================================================== tokens


def token_record(row: m.ApiToken) -> TokenRecord:
    return TokenRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        owner_id=row.owner_id,
        name=row.name,
        prefix=row.prefix,
        secret_hash=row.secret_hash,
        scopes=list(row.scopes),
        rate_limit_per_minute=row.rate_limit_per_minute,
        vcpu_seconds_budget=row.vcpu_seconds_budget,
        budget_period=BudgetPeriod(row.budget_period),
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


# ================================================================================= memberships


def membership_dto(row: m.Membership) -> Membership:
    return Membership(
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        principal_id=row.principal_id,
        principal_kind=PrincipalKind(row.principal_kind),
        role=ProjectRole(row.role),
        email=row.email,
        display_name=row.display_name,
        created_at=row.created_at,
        created_by=row.created_by,
    )


async def memberships_of(
    s: AsyncSession, principal_id: str, *, tenant_ids: Sequence[str] | None = None
) -> list[Membership]:
    """Every grant of one principal (optionally restricted to some tenants)."""
    stmt = select(m.Membership).where(m.Membership.principal_id == principal_id)
    if tenant_ids is not None:
        stmt = stmt.where(m.Membership.tenant_id.in_(list(tenant_ids)))
    rows = (await s.execute(stmt.order_by(m.Membership.tenant_id))).scalars()
    return [membership_dto(r) for r in rows]
