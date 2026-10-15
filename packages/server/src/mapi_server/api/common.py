"""Shared API plumbing: dependencies, cursor pagination and ``Idempotency-Key`` handling."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any

from fastapi import Depends, Header, Query, Request
from mapi_spec.api import (
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_TTL_SECONDS,
    PAGE_LIMIT_DEFAULT,
    PAGE_LIMIT_MAX,
    check_idempotency_key,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.loader import canonical_json
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import RequestAuth, authenticate
from mapi_server.context import NoProjectScope, ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import Scope, now

__all__ = [
    "Auth",
    "Ctx",
    "Idempotency",
    "IdempotencyKeyHeader",
    "Limit",
    "Replay",
    "Session",
    "SystemSession",
    "decode_cursor",
    "encode_cursor",
    "get_ctx",
]


Auth = Annotated[RequestAuth, Depends(authenticate)]


def get_ctx(request: Request, auth: Auth) -> ServerContext:
    """The service container bound to this request's caller (``ctx.scope``, ``ctx.request``)."""
    base: ServerContext = request.app.state.ctx
    return base.for_request(auth)


Ctx = Annotated[ServerContext, Depends(get_ctx)]


async def _session(ctx: Ctx) -> AsyncIterator[AsyncSession]:
    """A tenant session (row-level security) pinned to the request's tenant."""
    tenant = ctx.request.tenant_id if ctx.request is not None else None
    if tenant is None:
        raise NoProjectScope("this route has no tenant; use SystemSession explicitly")
    async with ctx.db.tenant_session(tenant) as session:
        yield session


async def _system_session(ctx: Ctx) -> AsyncIterator[AsyncSession]:
    """A platform session for identity and operator routes, always with explicit filters.
    Inside a request it runs as the gateway role (:func:`~mapi_server.db.acting_as_gateway`):
    identity tables and a few metadata columns, never tenant content and never the
    row-level-security bypass."""
    async with ctx.db.session() as session:
        yield session


Session = Annotated[AsyncSession, Depends(_session)]
SystemSession = Annotated[AsyncSession, Depends(_system_session)]
Limit = Annotated[int, Query(ge=1, le=PAGE_LIMIT_MAX)]
IdempotencyKeyHeader = Annotated[str | None, Header(alias=IDEMPOTENCY_KEY_HEADER, max_length=255)]

DEFAULT_LIMIT = PAGE_LIMIT_DEFAULT


class BadCursor(MapiError):
    default_code = ErrorCode.SCHEMA_INVALID


def encode_cursor(key: Any) -> str:
    raw = json.dumps({"k": key}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> Any:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        return data["k"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise BadCursor("the cursor is invalid", pointer="/query/cursor") from None


# ---------------------------------------------------------------------- idempotency


@dataclass(frozen=True, slots=True)
class Replay:
    """A previous request with the same key and body: re-render this resource (200)."""

    resource_type: str
    resource_id: str


class Idempotency:
    """``Idempotency-Key`` records live in the same transaction as the resource they create.

    A concurrent duplicate blocks on the key's unique index until the first transaction
    commits, then replays; a rollback of the first request removes its key.
    """

    def __init__(self, session: AsyncSession, scope: Scope, key: str | None) -> None:
        self.session = session
        self.scope = scope
        self.key = key
        if key is not None:
            try:
                check_idempotency_key(key)
            except ValueError as exc:
                raise MapiError(
                    str(exc),
                    code=ErrorCode.SCHEMA_INVALID,
                    issues=[
                        Issue(
                            code=ErrorCode.SCHEMA_INVALID,
                            pointer="/header/Idempotency-Key",
                            message=str(exc),
                        )
                    ],
                ) from None

    @staticmethod
    def fingerprint(operation: str, body: Any) -> str:
        text = canonical_json({"operation": operation, "body": body})
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()

    async def begin(self, operation: str, body: Any) -> Replay | None:
        """Claim the key (``None`` → proceed) or return the replay target."""
        if self.key is None:
            return None
        s, sc = self.session, self.scope
        where = (
            m.IdempotencyKey.tenant_id == sc.tenant_id,
            m.IdempotencyKey.project_id == sc.project_id,
            m.IdempotencyKey.key == self.key,
        )
        await s.execute(delete(m.IdempotencyKey).where(*where, m.IdempotencyKey.expires_at < now()))
        digest = self.fingerprint(operation, body)
        claimed = (
            await s.execute(
                insert(m.IdempotencyKey)
                .values(
                    tenant_id=sc.tenant_id,
                    project_id=sc.project_id,
                    key=self.key,
                    operation=operation[:256],
                    request_hash=digest,
                    expires_at=now() + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS),
                )
                .on_conflict_do_nothing()
                .returning(m.IdempotencyKey.key)
            )
        ).scalar_one_or_none()
        if claimed is not None:
            return None
        row = (await s.execute(select(m.IdempotencyKey).where(*where))).scalar_one()
        if row.request_hash != digest:
            raise MapiError(
                "this Idempotency-Key was already used with a different request",
                code=ErrorCode.IDEMPOTENCY_CONFLICT,
            )
        if row.resource_type is None or row.resource_id is None:
            raise MapiError(
                "a request with this Idempotency-Key is still in progress; retry later",
                code=ErrorCode.CONFLICT,
            )
        return Replay(row.resource_type, row.resource_id)

    async def complete(self, resource_type: str, resource_id: str, status: int) -> None:
        if self.key is None:
            return
        row = await self.session.get(
            m.IdempotencyKey, (self.scope.tenant_id, self.scope.project_id, self.key)
        )
        if row is not None:
            row.resource_type = resource_type
            row.resource_id = resource_id
            row.response_status = status
