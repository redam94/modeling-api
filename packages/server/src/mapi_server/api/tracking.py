"""Run tracking routes (v0.11): rename/annotate/tag a run, its metric series and history,
its params, and metric/param writes from API and notebook users after the fact.

Writes lock the run row (the ingestor does the same), are allowed in any run state (post-hoc
KPIs on finished runs are the point), go in with ``source="api"`` and are audited
(``run.update`` / ``run.log``). Budgets are the :class:`~mapi_spec.tracking.TrackingLimits`
defaults; a write over them is refused whole (``QUOTA_EXCEEDED``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query
from mapi_spec.api import (
    MetricHistory,
    MetricSeries,
    MetricsWrite,
    MetricsWriteResult,
    Page,
    ParamsWrite,
    ParamsWriteResult,
    Run,
    RunParam,
    RunPatch,
)
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import ResourceKind
from mapi_spec.tracking import (
    METRIC_KEY_RE,
    TrackingLimits,
    check_labels,
    labels_key,
    parse_labels_key,
)
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
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
from mapi_server.api.runs import RunId, run_view
from mapi_server.db import models as m
from mapi_server.errors import Conflict
from mapi_server.services.audit import write_audit
from mapi_server.services.experiments import check_run_experiment
from mapi_server.services.notify import notify_run
from mapi_server.services.runs import load_run, now
from mapi_server.services.tracking import (
    metric_history,
    series_dto,
    write_metric_points,
    write_params,
    write_tags,
)

__all__ = ["router"]

router = APIRouter(prefix="/v1/runs", tags=["runs"])

MetricKeyPath = Annotated[str, Path(pattern=METRIC_KEY_RE)]
_MAX_CONFLICT_KEYS = 10


def _bad_query(detail: str, pointer: str) -> MapiError:
    return MapiError(detail, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


@router.patch("/{run_id}", response_model=Run, operation_id="update_run")
async def update_run(ctx: Ctx, auth: Auth, session: Session, run_id: RunId, body: RunPatch) -> Run:
    """Merge patch: a field left out is unchanged, ``null`` clears the name or the note, and
    a tag set to ``null`` is deleted. ``experiment_id`` (v0.14) moves the run to another
    experiment (not an archived one; unknown: 422) and ``null`` takes it out of its
    experiment. The note text is never written to the audit log."""
    run = await load_run(session, ctx.scope, run_id, for_update=True)
    fields = sorted(body.model_fields_set)
    if "experiment_id" in body.model_fields_set and body.experiment_id != run.experiment_id:
        if body.experiment_id is not None:
            await check_run_experiment(session, ctx.scope, body.experiment_id)
        run.experiment_id = body.experiment_id
    if "name" in body.model_fields_set:
        run.name = _clean_text(body.name)
    if "note" in body.model_fields_set:
        run.note = body.note if body.note and body.note.strip() else None
    tags_set = tags_deleted = 0
    if body.tags:
        tags_set, tags_deleted = await write_tags(
            session, run, body.tags, actor=auth.principal_id or "api"
        )
    run.updated_at = now()
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.RUN_UPDATE,
                ResourceKind.RUN,
                resource_id=run.id,
                details={
                    "fields": list(fields),
                    "tags_set": tags_set,
                    "tags_deleted": tags_deleted,
                },
            )
        ],
        session=session,
    )
    await notify_run(session, run.id, "run")
    if tags_set or tags_deleted:
        await notify_run(session, run.id, "tracking")
    return await run_view(session, run)


@router.get("/{run_id}/metrics", response_model=Page[MetricSeries], operation_id="list_run_metrics")
async def list_run_metrics(
    ctx: Ctx,
    session: Session,
    run_id: RunId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    key: Annotated[str | None, Query(pattern=METRIC_KEY_RE)] = None,
    attempt: Annotated[int | None, Query(ge=1)] = None,
) -> Page[MetricSeries]:
    """The run's metric series, ordered by key and labels. Without ``attempt`` each series is
    summarised for the newest attempt that wrote it; with it, for that attempt only."""
    run = await load_run(session, ctx.scope, run_id)
    after = decode_cursor(cursor)
    if after is not None and not (
        isinstance(after, list) and len(after) == 2 and all(isinstance(a, str) for a in after)
    ):
        raise _bad_query("the cursor is invalid", "/query/cursor")
    if attempt is None:
        s = m.RunMetricSummary
        stmt = select(s).where(s.run_id == run.id)
        if key is not None:
            stmt = stmt.where(s.key == key)
        if after is not None:
            stmt = stmt.where(
                or_(s.key > after[0], and_(s.key == after[0], s.labels_key > after[1]))
            )
        rows = list(
            (await session.execute(stmt.order_by(s.key, s.labels_key).limit(limit + 1))).scalars()
        )
        items = [series_dto(r) for r in rows]
    else:
        items = await _attempt_series(session, run, attempt, key, after, limit + 1)
    more = len(items) > limit
    items = items[:limit]
    last = items[-1] if items else None
    return Page[MetricSeries](
        items=items,
        next_cursor=encode_cursor([last.key, labels_key(last.labels)]) if more and last else None,
    )


async def _attempt_series(
    session: AsyncSession, run: m.Run, attempt: int, key: str | None, after: Any, limit: int
) -> list[MetricSeries]:
    """Summaries of one attempt, aggregated from its points (the stored summaries hold the
    newest attempt only)."""
    p = m.RunMetric
    order = (p.ts.desc(), p.step.desc())
    stmt = select(
        p.key,
        p.labels_key,
        func.count().label("n"),
        func.min(p.ts).label("first_ts"),
        func.max(p.ts).label("last_ts"),
        func.min(p.value).label("min"),
        func.max(p.value).label("max"),
        func.array_agg(aggregate_order_by(p.value, *order))[1].label("last"),
        func.array_agg(aggregate_order_by(p.step, *order))[1].label("last_step"),
    ).where(p.run_id == run.id, p.attempt == attempt)
    if key is not None:
        stmt = stmt.where(p.key == key)
    if after is not None:
        stmt = stmt.where(or_(p.key > after[0], and_(p.key == after[0], p.labels_key > after[1])))
    rows = (
        await session.execute(
            stmt.group_by(p.key, p.labels_key).order_by(p.key, p.labels_key).limit(limit)
        )
    ).all()
    return [
        MetricSeries(
            key=r.key,
            labels=parse_labels_key(r.labels_key),
            attempt=attempt,
            count=int(r.n),
            last=float(r.last),
            last_step=None if r.last_step < 0 else int(r.last_step),
            min=float(r.min),
            max=float(r.max),
            first_ts=r.first_ts,
            last_ts=r.last_ts,
        )
        for r in rows
    ]


@router.get(
    "/{run_id}/metrics/{key}", response_model=MetricHistory, operation_id="get_metric_history"
)
async def get_metric_history(
    ctx: Ctx,
    session: Session,
    run_id: RunId,
    key: MetricKeyPath,
    max_points: Annotated[int, Query(ge=10, le=10_000)] = 1000,
    attempt: Annotated[int | None, Query(ge=1)] = None,
    label: Annotated[list[str] | None, Query(max_length=8)] = None,
) -> MetricHistory:
    """Every series of ``key`` (at most 64, by labels), downsampled to ``max_points`` per
    series (LTTB over step and value; the first and the last point are always kept).
    ``label=k:v`` (repeatable) keeps the series with that label value."""
    run = await load_run(session, ctx.scope, run_id)
    wanted: dict[str, str] = {}
    for item in label or ():
        k, sep, v = item.partition(":")
        if not sep:
            raise _bad_query(f"label {item!r} must be 'key:value'", "/query/label")
        wanted[k] = v
    try:
        wanted = check_labels(wanted)
    except ValueError as exc:
        raise _bad_query(str(exc), "/query/label") from None
    return await metric_history(
        session, run, key, max_points=max_points, attempt=attempt, labels=wanted
    )


@router.post("/{run_id}/metrics", response_model=MetricsWriteResult, operation_id="log_run_metrics")
async def log_run_metrics(
    ctx: Ctx, auth: Auth, session: Session, run_id: RunId, body: MetricsWrite
) -> MetricsWriteResult:
    """Log metric points (for example business KPIs measured after the fit) into the run's
    current attempt. A stepped point already stored is kept (``duplicates``); a scalar point
    replaces an older one."""
    run = await load_run(session, ctx.scope, run_id, for_update=True)
    ts = now()
    accepted, duplicates = await write_metric_points(
        session,
        run,
        run.attempt,
        [(p.key, check_labels(p.labels), p.step, p.value, p.ts or ts) for p in body.metrics],
        source="api",
        limits=TrackingLimits(),
    )
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.RUN_LOG,
                ResourceKind.RUN,
                resource_id=run.id,
                details={"metrics": len(body.metrics)},
            )
        ],
        session=session,
    )
    if accepted:
        await notify_run(session, run.id, "tracking")
    return MetricsWriteResult(accepted=accepted, duplicates=duplicates)


@router.get("/{run_id}/params", response_model=Page[RunParam], operation_id="list_run_params")
async def list_run_params(
    ctx: Ctx,
    session: Session,
    run_id: RunId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    prefix: Annotated[str | None, Query(max_length=250)] = None,
) -> Page[RunParam]:
    """The run's params, ordered by key. ``value`` is untrusted text: render it as text."""
    run = await load_run(session, ctx.scope, run_id)
    p = m.RunParam
    stmt = select(p).where(p.run_id == run.id)
    if prefix:
        stmt = stmt.where(p.key.startswith(prefix, autoescape=True))
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(p.key > str(after))
    rows = list((await session.execute(stmt.order_by(p.key).limit(limit + 1))).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[RunParam](
        items=[
            RunParam(
                key=r.key,
                value=r.value_text,
                value_num=r.value_num,
                source=r.source,
                logged_at=r.logged_at,
            )
            for r in rows
        ],
        next_cursor=encode_cursor(rows[-1].key) if more and rows else None,
    )


@router.post("/{run_id}/params", response_model=ParamsWriteResult, operation_id="log_run_params")
async def log_run_params(
    ctx: Ctx, auth: Auth, session: Session, run_id: RunId, body: ParamsWrite
) -> ParamsWriteResult:
    """Log params. A param is write-once: logging the stored value again is ``unchanged``;
    a different value refuses the whole write (409 ``CONFLICT``, nothing is written)."""
    run = await load_run(session, ctx.scope, run_id, for_update=True)
    accepted, unchanged, conflicts = await write_params(
        session, run, body.params, source="api", limits=TrackingLimits()
    )
    if conflicts:
        shown = ", ".join(repr(k) for k in conflicts[:_MAX_CONFLICT_KEYS])
        more = len(conflicts) - _MAX_CONFLICT_KEYS
        raise Conflict(
            f"params are write-once and these keys already have another value: {shown}"
            + (f" (and {more} more)" if more > 0 else "")
        )
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.RUN_LOG,
                ResourceKind.RUN,
                resource_id=run.id,
                details={"params": len(body.params)},
            )
        ],
        session=session,
    )
    if accepted:
        await notify_run(session, run.id, "tracking")
    return ParamsWriteResult(accepted=accepted, unchanged=unchanged)
