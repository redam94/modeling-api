"""Experiment routes (v0.14): named groups of runs, their run counts, bulk run moves.

Experiments use the run permissions (reads ``run:read``, writes ``run:update``) and every
change is audited (``experiment.create`` / ``experiment.update`` / ``experiment.delete``,
resource kind ``run``, resource id the experiment's).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status
from mapi_spec.api import (
    Experiment,
    ExperimentCreate,
    ExperimentPatch,
    ExperimentRunsResult,
    ExperimentRunsWrite,
    Page,
)
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import ResourceKind
from sqlalchemy import delete, select

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
from mapi_server.services.experiments import (
    create_experiment as create_row,
)
from mapi_server.services.experiments import (
    experiment_dtos,
    load_experiment,
    move_runs,
)
from mapi_server.services.experiments import (
    update_experiment as update_row,
)

__all__ = ["ExperimentId", "router"]

router = APIRouter(prefix="/v1/experiments", tags=["experiments"])

ExperimentId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]


async def _one(session: Session, row: m.Experiment) -> Experiment:
    return (await experiment_dtos(session, [row]))[0]


@router.get("", response_model=Page[Experiment], operation_id="list_experiments")
async def list_experiments(
    ctx: Ctx,
    session: Session,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    name: Annotated[str | None, Query(max_length=200)] = None,
    archived: bool | None = None,
) -> Page[Experiment]:
    """Experiments of the project, newest first. ``name`` is a case-insensitive substring;
    ``archived`` keeps the archived (true) or the active (false) ones."""
    e = m.Experiment
    stmt = select(e).where(e.tenant_id == ctx.scope.tenant_id, e.project_id == ctx.scope.project_id)
    if name:
        stmt = stmt.where(e.name.icontains(name.strip(), autoescape=True))
    if archived is not None:
        stmt = stmt.where(e.archived_at.is_not(None) if archived else e.archived_at.is_(None))
    after = decode_cursor(cursor)
    if after is not None:
        if not isinstance(after, str):
            raise MapiError(
                "the cursor is invalid", code=ErrorCode.SCHEMA_INVALID, pointer="/query/cursor"
            )
        stmt = stmt.where(e.id < after)
    rows = list((await session.execute(stmt.order_by(e.id.desc()).limit(limit + 1))).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[Experiment](
        items=await experiment_dtos(session, rows),
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.post(
    "",
    response_model=Experiment,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_experiment",
)
async def create_experiment(
    ctx: Ctx, auth: Auth, session: Session, body: ExperimentCreate
) -> Experiment:
    row = await create_row(session, ctx.scope, body, created_by=auth.principal_id)
    await write_audit(
        ctx.db,
        [auth.audit(ctx, AuditAction.EXPERIMENT_CREATE, ResourceKind.RUN, resource_id=row.id)],
        session=session,
    )
    return await _one(session, row)


@router.get("/{experiment_id}", response_model=Experiment, operation_id="get_experiment")
async def get_experiment(ctx: Ctx, session: Session, experiment_id: ExperimentId) -> Experiment:
    return await _one(session, await load_experiment(session, ctx.scope, experiment_id))


@router.patch("/{experiment_id}", response_model=Experiment, operation_id="update_experiment")
async def update_experiment(
    ctx: Ctx, auth: Auth, session: Session, experiment_id: ExperimentId, body: ExperimentPatch
) -> Experiment:
    """Merge patch: rename (names stay unique per project), describe, archive or unarchive.
    The description text is never written to the audit log."""
    row = await load_experiment(session, ctx.scope, experiment_id, for_update=True)
    changed = await update_row(session, ctx.scope, row, body)
    if changed:
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    ctx,
                    AuditAction.EXPERIMENT_UPDATE,
                    ResourceKind.RUN,
                    resource_id=row.id,
                    details={"fields": list(changed)},
                )
            ],
            session=session,
        )
    return await _one(session, row)


@router.delete(
    "/{experiment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_experiment",
)
async def delete_experiment(
    ctx: Ctx, auth: Auth, session: Session, experiment_id: ExperimentId
) -> Response:
    """Delete an experiment: its runs stay (they leave the experiment) and its saved views
    are deleted."""
    row = await load_experiment(session, ctx.scope, experiment_id, for_update=True)
    await session.execute(delete(m.Experiment).where(m.Experiment.id == row.id))
    await write_audit(
        ctx.db,
        [auth.audit(ctx, AuditAction.EXPERIMENT_DELETE, ResourceKind.RUN, resource_id=row.id)],
        session=session,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{experiment_id}/runs",
    response_model=ExperimentRunsResult,
    operation_id="update_experiment_runs",
)
async def update_experiment_runs(
    ctx: Ctx, auth: Auth, session: Session, experiment_id: ExperimentId, body: ExperimentRunsWrite
) -> ExperimentRunsResult:
    """Move runs into the experiment (``add``; a run leaves its previous experiment) or out
    of it (``remove``). Ids that are not runs of the project come back in ``missing``. An
    archived experiment takes no new runs (409)."""
    row = await load_experiment(session, ctx.scope, experiment_id, for_update=True)
    added, removed, missing = await move_runs(session, ctx.scope, row, body.add, body.remove)
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.EXPERIMENT_UPDATE,
                ResourceKind.RUN,
                resource_id=row.id,
                details={"added": added, "removed": removed},
            )
        ],
        session=session,
    )
    return ExperimentRunsResult(added=added, removed=removed, missing=missing)
