"""Experiments (v0.14): named groups of runs in a project.

A run is in at most one experiment (``runs.experiment_id``). It joins one when it is created
(``RunCreate.experiment_id``), when it is moved (``PATCH /v1/runs/{id}``, bulk
``POST /v1/experiments/{id}/runs``), or by inheritance: a workflow's node and sub-workflow
runs take the workflow run's experiment, and a retry takes the experiment of the run it
retries. Names are unique per project case-insensitively (``name_key``). An archived
experiment still lists, filters and compares, but accepts no new runs (409 ``CONFLICT``).
Deleting an experiment keeps its runs (the foreign key sets ``experiment_id`` to null) and
deletes its saved views (cascade).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from mapi_spec.api import Experiment, ExperimentCreate, ExperimentPatch, new_id
from mapi_spec.errors import ErrorCode, MapiError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.notify import notify_run
from mapi_server.services.runs import Scope, now

__all__ = [
    "check_run_experiment",
    "create_experiment",
    "experiment_dtos",
    "load_experiment",
    "move_runs",
    "name_key",
    "update_experiment",
]


def name_key(name: str) -> str:
    """What names are compared on: ``lower(btrim(name))``."""
    return name.strip().lower()


async def load_experiment(
    session: AsyncSession, scope: Scope, experiment_id: str, *, for_update: bool = False
) -> m.Experiment:
    stmt = select(m.Experiment).where(
        m.Experiment.id == experiment_id,
        m.Experiment.tenant_id == scope.tenant_id,
        m.Experiment.project_id == scope.project_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("experiment", experiment_id)
    return row


def _archived(row: m.Experiment) -> Conflict:
    return Conflict(f"experiment {row.name!r} is archived and accepts no new runs; unarchive it")


async def check_run_experiment(
    session: AsyncSession, scope: Scope, experiment_id: str, *, pointer: str = "/experiment_id"
) -> m.Experiment:
    """The experiment a run is created in or moved to: unknown → 422 ``SCHEMA_INVALID`` at
    ``pointer``, archived → 409 ``CONFLICT``."""
    try:
        row = await load_experiment(session, scope, experiment_id)
    except NotFoundError:
        raise MapiError(
            f"experiment {experiment_id} does not exist in this project",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=pointer,
        ) from None
    if row.archived_at is not None:
        raise _archived(row)
    return row


async def experiment_dtos(session: AsyncSession, rows: Sequence[m.Experiment]) -> list[Experiment]:
    """API views of a page of experiments with their run counts (one query)."""
    ids = [r.id for r in rows]
    counts: dict[str, tuple[int, datetime | None]] = {}
    if ids:
        agg = await session.execute(
            select(m.Run.experiment_id, func.count(), func.max(m.Run.created_at))
            .where(m.Run.experiment_id.in_(ids))
            .group_by(m.Run.experiment_id)
        )
        counts = {str(e): (int(n), last) for e, n, last in agg.all()}
    return [
        Experiment(
            id=r.id,
            name=r.name,
            description=r.description,
            archived=r.archived_at is not None,
            created_by=r.created_by,
            created_at=r.created_at,
            updated_at=r.updated_at,
            run_count=counts.get(r.id, (0, None))[0],
            last_run_at=counts.get(r.id, (0, None))[1],
        )
        for r in rows
    ]


async def _name_free(
    session: AsyncSession, scope: Scope, key: str, *, other_than: str | None = None
) -> None:
    stmt = select(m.Experiment.name).where(
        m.Experiment.tenant_id == scope.tenant_id,
        m.Experiment.project_id == scope.project_id,
        m.Experiment.name_key == key,
    )
    if other_than is not None:
        stmt = stmt.where(m.Experiment.id != other_than)
    taken = (await session.execute(stmt)).scalar_one_or_none()
    if taken is not None:
        raise Conflict(f"an experiment named {taken!r} already exists in this project")


async def create_experiment(
    session: AsyncSession, scope: Scope, body: ExperimentCreate, *, created_by: str | None
) -> m.Experiment:
    key = name_key(body.name)
    await _name_free(session, scope, key)
    ts = now()
    row = m.Experiment(
        id=new_id("exp"),
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        name=body.name,
        name_key=key,
        description=body.description,
        archived_at=None,
        created_by=created_by[:64] if created_by else None,
        created_at=ts,
        updated_at=ts,
    )
    session.add(row)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:  # a concurrent create took the name
        raise Conflict(
            f"an experiment named {body.name!r} already exists in this project"
        ) from None
    return row


async def update_experiment(
    session: AsyncSession, scope: Scope, row: m.Experiment, body: ExperimentPatch
) -> list[str]:
    """Apply a merge patch; returns the fields that changed."""
    changed: list[str] = []
    fields = body.model_fields_set
    if "name" in fields and body.name is not None and body.name != row.name:
        key = name_key(body.name)
        await _name_free(session, scope, key, other_than=row.id)
        row.name, row.name_key = body.name, key
        changed.append("name")
    if "description" in fields and body.description != row.description:
        row.description = body.description
        changed.append("description")
    if "archived" in fields and body.archived is not None:
        if body.archived and row.archived_at is None:
            row.archived_at = now()
            changed.append("archived")
        elif not body.archived and row.archived_at is not None:
            row.archived_at = None
            changed.append("archived")
    if changed:
        row.updated_at = now()
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            raise Conflict(
                f"an experiment named {row.name!r} already exists in this project"
            ) from None
    return changed


async def move_runs(
    session: AsyncSession,
    scope: Scope,
    row: m.Experiment,
    add: Sequence[str],
    remove: Sequence[str],
) -> tuple[int, int, list[str]]:
    """Move runs of the project into ``row`` (``add``: a run leaves its previous experiment)
    or out of it (``remove``). Returns ``(added, removed, missing ids)``; an id that is not a
    run of the project is missing, a removed run that is not in ``row`` is left alone.
    Adding to an archived experiment is refused (409)."""
    if add and row.archived_at is not None:
        raise _archived(row)
    wanted = list(dict.fromkeys([*add, *remove]))
    found = set(
        (
            await session.execute(
                select(m.Run.id).where(
                    m.Run.tenant_id == scope.tenant_id,
                    m.Run.project_id == scope.project_id,
                    m.Run.id.in_(wanted),
                )
            )
        ).scalars()
    )
    missing = [i for i in wanted if i not in found]
    changed: list[str] = []
    added = removed = 0
    if add:
        ids = [i for i in add if i in found]
        moved = (
            await session.execute(
                update(m.Run)
                .where(m.Run.id.in_(ids), m.Run.experiment_id.is_distinct_from(row.id))
                .values(experiment_id=row.id, updated_at=now())
                .returning(m.Run.id)
            )
        ).scalars()
        out = list(moved)
        added = len(out)
        changed.extend(out)
    if remove:
        ids = [i for i in remove if i in found]
        moved = (
            await session.execute(
                update(m.Run)
                .where(m.Run.id.in_(ids), m.Run.experiment_id == row.id)
                .values(experiment_id=None, updated_at=now())
                .returning(m.Run.id)
            )
        ).scalars()
        out = list(moved)
        removed = len(out)
        changed.extend(out)
    for run_id in changed:
        await notify_run(session, run_id, "run")  # open streams send a new snapshot
    return added, removed, missing
