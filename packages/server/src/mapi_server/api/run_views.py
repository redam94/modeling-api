"""Saved run views and run keys (v0.14): the runs table's saved queries, and the tracked
keys its column picker offers.

Views use the run permissions (reads ``run:read``, writes ``run:update``); saving and
deleting are audited (``run_view.save`` / ``run_view.delete``, resource kind ``run``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status
from mapi_spec.api import Page, RunKeys, RunView, RunViewCreate, RunViewPatch
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import ResourceKind
from sqlalchemy import and_, delete, or_, select

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Limit,
    Session,
    decode_cursor,
    encode_cursor,
)
from mapi_server.db import models as m
from mapi_server.services.audit import write_audit
from mapi_server.services.run_search import run_keys
from mapi_server.services.run_views import create_view, load_view, update_view, view_dto

__all__ = ["keys_router", "router"]

router = APIRouter(prefix="/v1/run-views", tags=["runs"])
keys_router = APIRouter(tags=["runs"])

ViewId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]


@router.get("", response_model=Page[RunView], operation_id="list_run_views")
async def list_run_views(
    ctx: Ctx,
    session: Session,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    experiment_id: str | None = None,
) -> Page[RunView]:
    """Saved views of the project ordered by name; ``experiment_id`` keeps the views of
    that experiment."""
    v = m.RunView
    stmt = select(v).where(v.tenant_id == ctx.scope.tenant_id, v.project_id == ctx.scope.project_id)
    if experiment_id is not None:
        stmt = stmt.where(v.experiment_id == experiment_id)
    after = decode_cursor(cursor)
    if after is not None:
        if not (
            isinstance(after, list) and len(after) == 2 and all(isinstance(a, str) for a in after)
        ):
            raise MapiError(
                "the cursor is invalid", code=ErrorCode.SCHEMA_INVALID, pointer="/query/cursor"
            )
        stmt = stmt.where(or_(v.name_key > after[0], and_(v.name_key == after[0], v.id > after[1])))
    rows = list((await session.execute(stmt.order_by(v.name_key, v.id).limit(limit + 1))).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[RunView](
        items=[view_dto(r) for r in rows],
        next_cursor=encode_cursor([rows[-1].name_key, rows[-1].id]) if more and rows else None,
    )


@router.post(
    "", response_model=RunView, status_code=status.HTTP_201_CREATED, operation_id="create_run_view"
)
async def create_run_view(ctx: Ctx, auth: Auth, session: Session, body: RunViewCreate) -> RunView:
    row = await create_view(session, ctx.scope, body, created_by=auth.principal_id)
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.RUN_VIEW_SAVE,
                ResourceKind.RUN,
                resource_id=row.id,
                details={"created": True},
            )
        ],
        session=session,
    )
    return view_dto(row)


@router.patch("/{view_id}", response_model=RunView, operation_id="update_run_view")
async def update_run_view(
    ctx: Ctx, auth: Auth, session: Session, view_id: ViewId, body: RunViewPatch
) -> RunView:
    row = await load_view(session, ctx.scope, view_id, for_update=True)
    changed = await update_view(session, ctx.scope, row, body)
    if changed:
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    ctx,
                    AuditAction.RUN_VIEW_SAVE,
                    ResourceKind.RUN,
                    resource_id=row.id,
                    details={"fields": list(changed)},
                )
            ],
            session=session,
        )
    return view_dto(row)


@router.delete(
    "/{view_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_run_view",
)
async def delete_run_view(ctx: Ctx, auth: Auth, session: Session, view_id: ViewId) -> Response:
    row = await load_view(session, ctx.scope, view_id, for_update=True)
    await session.execute(delete(m.RunView).where(m.RunView.id == row.id))
    await write_audit(
        ctx.db,
        [auth.audit(ctx, AuditAction.RUN_VIEW_DELETE, ResourceKind.RUN, resource_id=row.id)],
        session=session,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@keys_router.get("/v1/run-keys", response_model=RunKeys, operation_id="list_run_keys")
async def list_run_keys(
    ctx: Ctx,
    session: Session,
    experiment_id: str | None = None,
    prefix: Annotated[str | None, Query(max_length=250)] = None,
) -> RunKeys:
    """Metric series, param keys and tag keys of the project's runs (or one experiment's),
    with how many runs have each, most common first. ``prefix`` keeps keys that start with
    it. Lists are capped (metrics and params 500, tags 200; ``truncated``)."""
    return await run_keys(session, ctx.scope, experiment_id, prefix)
