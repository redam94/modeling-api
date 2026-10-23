"""Dashboard read tokens: mint, list and revoke (v0.20, plan D12-D13).

A read token lets a chat tool (an MCP client) read **one** published dashboard as its
owner. It is an ordinary ``api_tokens`` row with exactly one scope,
``dashboard:<dashboard_id>`` (:func:`mapi_spec.tokens.dashboard_scope`), and the
``dashboard_id`` column set (it counts the active tokens and goes with its dashboard).

* Only a signed-in person mints one, for themself, on a dashboard they can read now
  (clients included, for the dashboards granted to them). The server sets the scope, the
  owner and the lifetime (at most ``dashboard_read_token_max_days``); the request names only
  a label and a number of days. At most ``dashboard_read_tokens_per_user`` tokens of one
  owner are active on one dashboard (``READ_TOKEN_LIMIT``).
* The secret is returned once; only ``HMAC(pepper, token)`` is stored.
* Owners list and revoke their own tokens; editors list (``all=true``) and revoke every
  token of the dashboard. Another owner's token is ``404`` to everyone else.
* The server accepts these tokens on the MCP endpoint only
  (:data:`mapi_spec.api.DASHBOARD_TOKEN_ROUTES`), and re-checks the owner's access on
  every use: removing a grant or a membership stops the token without a revoke.

Minting and revoking are audited (never with the secret).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Final

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction
from mapi_spec.chat import ReadToken, ReadTokenCreate, ReadTokenCreated
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import Action, PrincipalKind, ResourceKind
from mapi_spec.tokens import dashboard_scope, generate_token, hash_token
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, unauthenticated
from mapi_server.services.audit import write_audit
from mapi_server.services.dashboards.access import (
    load_dashboard,
    load_revision,
    member_context,
    require_read_token_target,
)
from mapi_server.services.runs import now

__all__ = ["LIST_LIMIT", "list_tokens", "mint", "read_token_dto", "revoke"]

LIST_LIMIT: Final = 200
"""The newest read tokens one listing returns (owners hold at most a few active ones)."""


# ================================================================================== helpers


def _session_user(auth: RequestAuth) -> str:
    """The signed-in person acting. Read tokens are managed from a web session only (the
    route is in :data:`mapi_spec.api.SESSION_ONLY_OPERATIONS`; checked again here)."""
    if auth.token is not None:
        raise MapiError(
            "read tokens are managed from a signed-in session, not with an API token",
            code=ErrorCode.FORBIDDEN,
        )
    if auth.user_id is None or auth.tenant_id is None:
        raise unauthenticated()
    return auth.user_id


def _bound(ctx: ServerContext, auth: RequestAuth) -> ServerContext:
    return ctx if ctx.request is auth else ctx.for_request(auth)


async def _labels(
    session: AsyncSession, tenant_id: str, project_id: str, owner_ids: Iterable[str]
) -> dict[str, str]:
    """``owner_id`` → the name people know them by (display name, else e-mail) from their
    membership in the project (or the tenant)."""
    ids = sorted(set(owner_ids))
    if not ids:
        return {}
    labels: dict[str, str] = {}
    rows = await session.execute(
        select(m.Membership).where(
            m.Membership.tenant_id == tenant_id, m.Membership.principal_id.in_(ids)
        )
    )
    for mm in rows.scalars():
        if mm.project_id not in (None, project_id):
            continue
        label = (mm.display_name or mm.email or "").strip()
        if label and (mm.principal_id not in labels or mm.project_id == project_id):
            labels[mm.principal_id] = label
    return labels


def read_token_dto(row: m.ApiToken, owner_label: str | None = None) -> ReadToken:
    """A read token as listed (never its hash; the secret exists only at creation)."""
    assert row.dashboard_id is not None
    return ReadToken(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        dashboard_id=row.dashboard_id,
        owner_id=row.owner_id,
        owner_label=(owner_label or row.owner_id)[:200],
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


async def _active_count(
    session: AsyncSession, tenant_id: str, dashboard_id: str, owner_id: str, at: datetime
) -> int:
    stmt = select(func.count()).where(
        m.ApiToken.tenant_id == tenant_id,
        m.ApiToken.dashboard_id == dashboard_id,
        m.ApiToken.owner_id == owner_id,
        m.ApiToken.revoked_at.is_(None),
        m.ApiToken.expires_at > at,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _serialize_mints(session: AsyncSession, dashboard_id: str, owner_id: str) -> None:
    """One mint at a time per (dashboard, owner) until the transaction ends, so two
    concurrent requests cannot both pass the active-token limit."""
    key = f"mapi.read_token:{dashboard_id}:{owner_id}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


# ================================================================================== actions


async def mint(
    ctx: ServerContext, auth: RequestAuth, dashboard_id: str, body: ReadTokenCreate
) -> ReadTokenCreated:
    """Mint a read token for the caller on a published dashboard they can read now.

    ``404`` for a dashboard the caller cannot see, ``DASHBOARD_NOT_PUBLISHED`` (409) before
    the first publish or while it is not published (archived), ``403 FORBIDDEN`` for a layout
    1 dashboard, ``403 UPLOADED_CODE_NOT_SHAREABLE`` for one made with uploaded code (the
    checks :func:`~.access.token_context` makes on every use, so no token is made that can
    never work), ``READ_TOKEN_LIMIT`` (409) when the caller already holds the maximum of
    active tokens on it. The secret is in the result and nowhere else."""
    owner_id = _session_user(auth)
    rctx = _bound(ctx, auth)
    settings = ctx.settings
    if auth.context is not None and not auth.context.tenant_status.allows_writes:
        raise MapiError(
            "the organisation is not active: you cannot make read tokens now",
            code=ErrorCode.FORBIDDEN,
        )
    max_days = settings.dashboard_read_token_max_days
    if body.expires_in_days > max_days:
        raise MapiError(
            f"a read token can last at most {max_days} days",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/expires_in_days",
        )
    assert auth.tenant_id is not None
    async with ctx.db.tenant_session(auth.tenant_id) as session:
        dctx = await member_context(rctx, auth, dashboard_id, session=session)
        dash = await session.get(m.Dashboard, dctx.dashboard_id)
        rev = await load_revision(session, dash) if dash is not None else None
        if dash is None or rev is None or dash.status != "published":
            raise MapiError(
                "the dashboard is not published: a chat tool can read published dashboards only",
                code=ErrorCode.DASHBOARD_NOT_PUBLISHED,
            )
        await require_read_token_target(session, dash, rev)
        await _serialize_mints(session, dctx.dashboard_id, owner_id)
        ts = now()
        limit = settings.dashboard_read_tokens_per_user
        if await _active_count(session, dctx.tenant_id, dctx.dashboard_id, owner_id, ts) >= limit:
            raise MapiError(
                f"you already have {limit} active read tokens for this dashboard: revoke one first",
                code=ErrorCode.READ_TOKEN_LIMIT,
            )
        scope = dashboard_scope(dctx.dashboard_id)
        for _ in range(3):  # a 40-bit prefix collision is unlikely; retry rather than fail
            parsed = generate_token()
            row = m.ApiToken(
                id=new_id("tok"),
                tenant_id=dctx.tenant_id,
                project_id=dctx.project_id,
                owner_id=owner_id,
                owner_kind=PrincipalKind.USER.value,
                name=body.name,
                prefix=parsed.prefix,
                secret_hash=hash_token(parsed, ctx.secrets.token_pepper),
                scopes=[scope],
                rate_limit_per_minute=None,
                vcpu_seconds_budget=None,
                budget_period="month",
                created_at=ts,
                expires_at=ts + timedelta(days=body.expires_in_days),
                dashboard_id=dctx.dashboard_id,
            )
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
                continue
            break
        else:  # pragma: no cover - three prefix collisions in a row
            raise RuntimeError("could not allocate a token prefix")
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    ctx,
                    AuditAction.DASHBOARD_READ_TOKEN_CREATE,
                    ResourceKind.TOKEN,
                    resource_id=row.id,
                    tenant_id=dctx.tenant_id,
                    project_id=dctx.project_id,
                    details={
                        "dashboard_id": dctx.dashboard_id,
                        "prefix": row.prefix,
                        "owner_id": owner_id,
                        "expires_in_days": body.expires_in_days,
                    },
                )
            ],
            session=session,
        )
        labels = await _labels(session, dctx.tenant_id, dctx.project_id, [owner_id])
        listed = read_token_dto(row, labels.get(owner_id))
    return ReadTokenCreated(**listed.model_dump(), secret=str(parsed))


async def list_tokens(
    ctx: ServerContext, auth: RequestAuth, dashboard_id: str, *, all_: bool
) -> list[ReadToken]:
    """The caller's read tokens on the dashboard, newest first (revoked and expired ones
    included, so people see what stopped working). ``all_``: every owner's tokens, for
    editors (``(dashboard, update)``)."""
    owner_id = _session_user(auth)
    if all_:
        auth.require(Action.UPDATE, ResourceKind.DASHBOARD)
    rctx = _bound(ctx, auth)
    sc = rctx.scope
    assert auth.tenant_id is not None
    async with ctx.db.tenant_session(auth.tenant_id) as session:
        if all_:
            await load_dashboard(session, rctx, dashboard_id)
        # the caller's own tokens need no sight of the dashboard: an owner who lost access
        # (a removed grant, an archived dashboard) still sees and revokes their credentials
        stmt = select(m.ApiToken).where(
            m.ApiToken.tenant_id == sc.tenant_id,
            m.ApiToken.project_id == sc.project_id,
            m.ApiToken.dashboard_id == dashboard_id,
        )
        if not all_:
            stmt = stmt.where(m.ApiToken.owner_id == owner_id)
        rows = list(
            (await session.execute(stmt.order_by(m.ApiToken.id.desc()).limit(LIST_LIMIT)))
            .scalars()
            .all()
        )
        labels = await _labels(session, sc.tenant_id, sc.project_id, (r.owner_id for r in rows))
    return [read_token_dto(r, labels.get(r.owner_id)) for r in rows]


async def revoke(ctx: ServerContext, auth: RequestAuth, dashboard_id: str, token_id: str) -> None:
    """Revoke a read token of the dashboard (idempotent): the caller's own, or anyone's for
    an editor. Any other token answers ``404``. The next MCP request with it is ``401``.

    The caller's own token needs no sight of the dashboard: an owner whose grant was removed
    (or whose dashboard was archived) can still revoke it, so it never works again when the
    access comes back."""
    owner_id = _session_user(auth)
    rctx = _bound(ctx, auth)
    sc = rctx.scope
    assert auth.tenant_id is not None
    async with ctx.db.tenant_session(auth.tenant_id) as session:
        row = await session.get(m.ApiToken, token_id, with_for_update=True)
        if (
            row is None
            or (row.tenant_id, row.project_id) != (sc.tenant_id, sc.project_id)
            or row.dashboard_id != dashboard_id
        ):
            raise NotFoundError("read token", token_id)
        if row.owner_id != owner_id:
            if not auth.allows(Action.UPDATE, ResourceKind.DASHBOARD):
                raise NotFoundError("read token", token_id)
            await load_dashboard(session, rctx, dashboard_id)  # an editor acts on the dashboard
        if row.revoked_at is not None:
            return
        row.revoked_at = now()
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    ctx,
                    AuditAction.DASHBOARD_READ_TOKEN_REVOKE,
                    ResourceKind.TOKEN,
                    resource_id=row.id,
                    tenant_id=row.tenant_id,
                    project_id=row.project_id,
                    details={
                        "dashboard_id": dashboard_id,
                        "prefix": row.prefix,
                        "owner_id": row.owner_id,
                    },
                )
            ],
            session=session,
        )
