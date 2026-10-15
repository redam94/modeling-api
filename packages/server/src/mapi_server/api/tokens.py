"""API tokens (project-scoped): mint (the secret is shown once), list, get, revoke.

A token is minted *for the caller* (its owner; a token-authenticated caller mints for the
token's owner) in the selected project. It acts as its owner narrowed by its scopes and can
never do more than the owner's role at the time of use. Only ``HMAC(pepper, token)`` is
stored. Acting on other members' tokens (``all=true``, revoking theirs) needs ``manage`` on
``token`` (project admins).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status
from fastapi.responses import JSONResponse
from mapi_spec.api import ApiToken, Page, TokenCreate, TokenCreated, new_id
from mapi_spec.audit import AuditAction
from mapi_spec.tenancy import Action, PrincipalKind, ResourceKind
from mapi_spec.tokens import TokenStatus, generate_token, hash_token
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Limit,
    Session,
    decode_cursor,
    encode_cursor,
)
from mapi_server.auth.identity import token_record
from mapi_server.auth.request import RequestAuth
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.audit import write_audit
from mapi_server.services.runs import now

__all__ = ["router"]

router = APIRouter(prefix="/v1/tokens", tags=["tokens"])

TokenId = Annotated[str, Path(pattern=r"^tok_[0-9a-z]{26}$")]


def _public(row: m.ApiToken) -> ApiToken:
    return token_record(row).to_public(now())


def _owner(auth: RequestAuth) -> tuple[str, str]:
    """``(owner_id, owner_kind)`` of tokens this caller mints."""
    if auth.token is not None:
        p = auth.principal
        kind = getattr(p, "owner_kind", PrincipalKind.USER)
        return auth.token.owner_id, PrincipalKind(kind).value
    assert auth.user_id is not None
    return auth.user_id, PrincipalKind.USER.value


@router.get("", response_model=Page[ApiToken], operation_id="list_tokens")
async def list_tokens(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    all_: Annotated[bool, Query(alias="all")] = False,
    status_: Annotated[TokenStatus | None, Query(alias="status")] = None,
) -> Page[ApiToken]:
    """The caller's tokens in the project, newest first (``all=true``: everyone's)."""
    sc = ctx.scope
    stmt = select(m.ApiToken).where(
        m.ApiToken.tenant_id == sc.tenant_id, m.ApiToken.project_id == sc.project_id
    )
    if all_:
        auth.require(Action.MANAGE, ResourceKind.TOKEN)
    else:
        stmt = stmt.where(m.ApiToken.owner_id == _owner(auth)[0])
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.ApiToken.id < str(after))
    ts = now()
    if status_ is TokenStatus.REVOKED:
        stmt = stmt.where(m.ApiToken.revoked_at.is_not(None))
    elif status_ is TokenStatus.EXPIRED:
        stmt = stmt.where(m.ApiToken.revoked_at.is_(None), m.ApiToken.expires_at <= ts)
    elif status_ is TokenStatus.ACTIVE:
        stmt = stmt.where(m.ApiToken.revoked_at.is_(None), m.ApiToken.expires_at > ts)
    rows = list(
        (await session.execute(stmt.order_by(m.ApiToken.id.desc()).limit(limit + 1))).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[ApiToken](
        items=[_public(r) for r in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.post(
    "",
    response_model=TokenCreated,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_token",
)
async def create_token(ctx: Ctx, auth: Auth, session: Session, body: TokenCreate) -> JSONResponse:
    """Mint a token in the selected project; ``secret`` is returned now and never again."""
    sc = ctx.scope
    owner_id, owner_kind = _owner(auth)
    ts = now()
    for _ in range(3):  # a 40-bit prefix collision is unlikely; retry rather than fail
        parsed = generate_token()
        row = m.ApiToken(
            id=new_id("tok"),
            tenant_id=sc.tenant_id,
            project_id=sc.project_id,
            owner_id=owner_id,
            owner_kind=owner_kind,
            name=body.name,
            prefix=parsed.prefix,
            secret_hash=hash_token(parsed, ctx.secrets.token_pepper),
            scopes=list(body.scopes),
            rate_limit_per_minute=body.rate_limit_per_minute,
            vcpu_seconds_budget=body.vcpu_seconds_budget,
            budget_period=body.budget_period.value,
            created_at=ts,
            expires_at=ts + timedelta(days=body.expires_in_days),
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
                AuditAction.TOKEN_CREATE,
                ResourceKind.TOKEN,
                resource_id=row.id,
                details={
                    "scopes": list(body.scopes),
                    "expires_in_days": body.expires_in_days,
                    "prefix": row.prefix,
                    "owner_id": owner_id,
                },
            )
        ],
        session=session,
    )
    created = TokenCreated(token=_public(row), secret=str(parsed))
    return JSONResponse(
        created.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
        headers={"Cache-Control": "no-store"},
    )


async def _token(
    ctx: Ctx, auth: RequestAuth, session: AsyncSession, token_id: str, *, lock: bool = False
) -> m.ApiToken:
    sc = ctx.scope
    row = await session.get(m.ApiToken, token_id, with_for_update=lock)
    if row is None or (row.tenant_id, row.project_id) != (sc.tenant_id, sc.project_id):
        raise NotFoundError("token", token_id)
    if row.owner_id != _owner(auth)[0] and not auth.allows(Action.MANAGE, ResourceKind.TOKEN):
        raise NotFoundError("token", token_id)
    return row


@router.get("/{token_id}", response_model=ApiToken, operation_id="get_token")
async def get_token(ctx: Ctx, auth: Auth, session: Session, token_id: TokenId) -> ApiToken:
    return _public(await _token(ctx, auth, session, token_id))


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="revoke_token")
async def revoke_token(ctx: Ctx, auth: Auth, session: Session, token_id: TokenId) -> Response:
    """Revoke a token (idempotent). The next request with it is ``401``."""
    row = await _token(ctx, auth, session, token_id, lock=True)
    if row.revoked_at is None:
        row.revoked_at = now()
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    ctx,
                    AuditAction.TOKEN_REVOKE,
                    ResourceKind.TOKEN,
                    resource_id=row.id,
                    details={"prefix": row.prefix, "owner_id": row.owner_id},
                )
            ],
            session=session,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
