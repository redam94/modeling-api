"""Saved run views (v0.14): a name and a ``list_runs`` query, shared in a project.

The query (:class:`~mapi_spec.api.RunViewQuery`) is parsed again with the run-search parsers
when it is saved (every filter, the order, every column and tag; an error is 422
``SCHEMA_INVALID`` with a pointer under ``/query``), so a stored view always runs. A view may
belong to an experiment (deleted with it). Names are unique per (project, experiment)
case-insensitively (409); a project keeps at most :data:`~mapi_spec.tracking.MAX_RUN_VIEWS`
views (``QUOTA_EXCEEDED``).
"""

from __future__ import annotations

from mapi_spec.api import RunView, RunViewCreate, RunViewPatch, RunViewQuery, new_id
from mapi_spec.errors import ErrorCode, MapiError, QuotaExceeded
from mapi_spec.tracking import (
    MAX_RUN_VIEWS,
    check_tag_key,
    parse_run_filter,
    parse_run_order,
    parse_run_ref,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.experiments import load_experiment, name_key
from mapi_server.services.runs import Scope, now

__all__ = [
    "check_view_query",
    "create_view",
    "load_view",
    "update_view",
    "view_dto",
]


def _invalid(detail: str, pointer: str) -> MapiError:
    return MapiError(detail, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


def check_view_query(query: RunViewQuery) -> None:
    """Parse every part of a view's query with the run-search grammar (the request model
    did too; this keeps stored rows valid whatever built them)."""
    for i, item in enumerate(query.filters):
        try:
            parse_run_filter(item)
        except ValueError as exc:
            raise _invalid(str(exc), f"/query/filters/{i}") from None
    if query.order_by is not None:
        try:
            parse_run_order(query.order_by)
        except ValueError as exc:
            raise _invalid(str(exc), "/query/order_by") from None
    for i, item in enumerate(query.columns):
        try:
            parse_run_ref(item)
        except ValueError as exc:
            raise _invalid(str(exc), f"/query/columns/{i}") from None
    for i, item in enumerate(query.tags):
        try:
            check_tag_key(item.partition(":")[0], reserved=True)
        except ValueError as exc:
            raise _invalid(str(exc), f"/query/tags/{i}") from None


def view_dto(row: m.RunView) -> RunView:
    return RunView(
        id=row.id,
        name=row.name,
        experiment_id=row.experiment_id,
        query=RunViewQuery.model_validate(row.query),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def load_view(
    session: AsyncSession, scope: Scope, view_id: str, *, for_update: bool = False
) -> m.RunView:
    stmt = select(m.RunView).where(
        m.RunView.id == view_id,
        m.RunView.tenant_id == scope.tenant_id,
        m.RunView.project_id == scope.project_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("run view", view_id)
    return row


async def _name_free(
    session: AsyncSession,
    scope: Scope,
    experiment_id: str | None,
    key: str,
    *,
    other_than: str | None = None,
) -> None:
    v = m.RunView
    stmt = select(v.name).where(
        v.tenant_id == scope.tenant_id,
        v.project_id == scope.project_id,
        v.name_key == key,
        v.experiment_id.is_(None) if experiment_id is None else v.experiment_id == experiment_id,
    )
    if other_than is not None:
        stmt = stmt.where(v.id != other_than)
    taken = (await session.execute(stmt)).scalar_one_or_none()
    if taken is not None:
        where = "in this experiment" if experiment_id else "in this project"
        raise Conflict(f"a saved view named {taken!r} already exists {where}")


async def _flush(session: AsyncSession, name: str) -> None:
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:  # a concurrent save took the name
        raise Conflict(f"a saved view named {name!r} already exists") from None


async def create_view(
    session: AsyncSession, scope: Scope, body: RunViewCreate, *, created_by: str | None
) -> m.RunView:
    check_view_query(body.query)
    if body.experiment_id is not None:
        try:
            await load_experiment(session, scope, body.experiment_id)
        except NotFoundError:
            raise _invalid(
                f"experiment {body.experiment_id} does not exist in this project",
                "/experiment_id",
            ) from None
    have = (
        await session.execute(
            select(func.count())
            .select_from(m.RunView)
            .where(m.RunView.tenant_id == scope.tenant_id, m.RunView.project_id == scope.project_id)
        )
    ).scalar_one()
    if int(have) >= MAX_RUN_VIEWS:
        raise QuotaExceeded(
            f"the project already has {int(have)} saved views (at most {MAX_RUN_VIEWS}); "
            "delete one first"
        )
    key = name_key(body.name)
    await _name_free(session, scope, body.experiment_id, key)
    ts = now()
    row = m.RunView(
        id=new_id("rv"),
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        experiment_id=body.experiment_id,
        name=body.name,
        name_key=key,
        query=body.query.model_dump(mode="json", exclude_defaults=True),
        created_by=created_by[:64] if created_by else None,
        created_at=ts,
        updated_at=ts,
    )
    session.add(row)
    await _flush(session, body.name)
    return row


async def update_view(
    session: AsyncSession, scope: Scope, row: m.RunView, body: RunViewPatch
) -> list[str]:
    """Rename and/or replace the query; returns the fields that changed."""
    changed: list[str] = []
    if body.name is not None and body.name != row.name:
        key = name_key(body.name)
        await _name_free(session, scope, row.experiment_id, key, other_than=row.id)
        row.name, row.name_key = body.name, key
        changed.append("name")
    if body.query is not None:
        check_view_query(body.query)
        query = body.query.model_dump(mode="json", exclude_defaults=True)
        if query != row.query:
            row.query = query
            changed.append("query")
    if changed:
        row.updated_at = now()
        await _flush(session, row.name)
    return changed
