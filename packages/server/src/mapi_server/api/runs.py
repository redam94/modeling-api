"""Runs: submit (idempotent), list/get, events (SSE with ``Last-Event-ID`` resume), logs,
cancel, resume and retry."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from mapi_plots.sanitize import SanitizeError, sanitize_plot
from mapi_spec.api import (
    LAST_EVENT_ID_HEADER,
    SSE_HEARTBEAT,
    SSE_RETRY_MS,
    LogLine,
    Page,
    PlotSpec,
    Run,
    RunCreate,
    RunEventView,
    RunRetry,
    SseEventName,
    parse_last_event_id,
)
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunEventType, RunKind
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.tenancy import Action, ResourceKind
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Idempotency,
    IdempotencyKeyHeader,
    Limit,
    Session,
    decode_cursor,
    encode_cursor,
)
from mapi_server.auth.request import still_authorized
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict
from mapi_server.jsonlimits import loads_limited
from mapi_server.services.audit import write_audit
from mapi_server.services.cost import run_costs
from mapi_server.services.runs import apply_transition, load_run, run_dto
from mapi_server.services.submit import admit, submit_retry, submit_run

__all__ = ["RUN_PLOTS_OUTPUT", "render_run", "router", "run_view", "sse_stream"]

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/runs", tags=["runs"])

RunId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
_PAGE = 500
RUN_PLOTS_OUTPUT = "plots.json"
"""Output a run renders its plots into (a JSON array of PlotSpec documents)."""
_MAX_RUN_PLOTS = 50


async def run_view(session: AsyncSession, run: m.Run) -> Run:
    progress = await session.get(m.RunProgress, run.id)
    costs = await run_costs(session, [run])
    return run_dto(run, progress, cost=costs.get(run.id))


async def render_run(session: AsyncSession, run_id: str, status_code: int) -> JSONResponse:
    run = await load_run(session, None, run_id)
    view = await run_view(session, run)
    return JSONResponse(view.model_dump(mode="json"), status_code=status_code)


@router.post(
    "",
    response_model=Run,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_run",
    responses={200: {"model": Run, "description": "Idempotent replay"}},
)
async def create_run(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    request: Request,
    body: RunCreate,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    if body.kind not in (RunKind.FIT, RunKind.VALIDATE):  # runs against a model version
        auth.require(Action.RUN, ResourceKind.MODEL)
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin("POST /v1/runs", await request.json())
    if replay is not None:
        return await render_run(session, replay.resource_id, 200)
    run = await submit_run(session, ctx, body)
    await idem.complete("run", run.id, 201)
    return await render_run(session, run.id, 201)


@router.get("", response_model=Page[Run], operation_id="list_runs")
async def list_runs(
    ctx: Ctx,
    session: Session,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    kind: RunKind | None = None,
    state: RunState | None = None,
    spec_id: str | None = None,
) -> Page[Run]:
    stmt = select(m.Run).where(
        m.Run.tenant_id == ctx.scope.tenant_id, m.Run.project_id == ctx.scope.project_id
    )
    if kind is not None:
        stmt = stmt.where(m.Run.kind == kind.value)
    if state is not None:
        stmt = stmt.where(m.Run.state == state.value)
    if spec_id is not None:
        stmt = stmt.where(m.Run.spec_id == spec_id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.Run.id < str(after))
    rows = list((await session.execute(stmt.order_by(m.Run.id.desc()).limit(limit + 1))).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    progress = {
        p.run_id: p
        for p in (
            await session.execute(
                select(m.RunProgress).where(m.RunProgress.run_id.in_([r.id for r in rows]))
            )
        ).scalars()
    }
    costs = await run_costs(session, rows)
    return Page[Run](
        items=[run_dto(r, progress.get(r.id), cost=costs.get(r.id)) for r in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.get("/{run_id}", response_model=Run, operation_id="get_run")
async def get_run(ctx: Ctx, session: Session, run_id: RunId) -> Run:
    return await run_view(session, await load_run(session, ctx.scope, run_id))


def event_view(e: m.RunEvent) -> RunEventView:
    return RunEventView(
        id=e.id,
        run_id=e.run_id,
        attempt=e.attempt,
        seq=e.seq,
        ts=e.ts,
        type=RunEventType(e.type),
        payload=e.payload,
    )


@router.get(
    "/{run_id}/events",
    response_model=Page[RunEventView],
    operation_id="run_events",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def run_events(
    ctx: Ctx,
    request: Request,
    run_id: RunId,
    after: Annotated[int | None, Query(ge=0)] = None,
    limit: Limit = DEFAULT_LIMIT,
    last_event_id: Annotated[str | None, Header(alias=LAST_EVENT_ID_HEADER)] = None,
) -> Response:
    """``Accept: text/event-stream`` → live SSE; anything else → a JSON page of events."""
    async with ctx.data_session() as session:
        await load_run(session, ctx.scope, run_id)
        if "text/event-stream" not in request.headers.get("accept", ""):
            stmt = select(m.RunEvent).where(m.RunEvent.run_id == run_id)
            if after is not None:
                stmt = stmt.where(m.RunEvent.id > after)
            rows = list(
                (await session.execute(stmt.order_by(m.RunEvent.id).limit(limit + 1))).scalars()
            )
            more = len(rows) > limit
            rows = rows[:limit]
            page = Page[RunEventView](
                items=[event_view(e) for e in rows],
                next_cursor=str(rows[-1].id) if more and rows else None,
            )
            return JSONResponse(page.model_dump(mode="json"))
    resume = parse_last_event_id(last_event_id)
    if resume is None and after is not None:
        resume = after
    return StreamingResponse(
        sse_stream(ctx, request, run_id, resume or 0),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _frame(
    event: SseEventName | str, data: str, *, id: int | None = None, retry: int | None = None
) -> str:
    lines = []
    if id is not None:
        lines.append(f"id: {id}")
    if retry is not None:
        lines.append(f"retry: {retry}")
    lines.append(f"event: {SseEventName(event).value}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


async def sse_stream(
    ctx: ServerContext, request: Request | None, run_id: str, last_id: int
) -> AsyncIterator[str]:
    """``snapshot`` → replay after ``last_id`` → live frames → ``end`` (plan §8.7).

    Notifications only wake the loop; the database is always the source of what is sent,
    so a lost notification delays a frame but never drops it. Progress and log frames are
    not persisted events and carry no ``id:``; a reconnecting client gets the latest
    progress from the ``snapshot`` frame.

    Every ``sse_reauth_s`` the caller's credential is checked again
    (:func:`~mapi_server.auth.request.still_authorized`): once it no longer grants access
    the stream closes without ``end``, and the client's reconnect gets the 401/403.
    """
    settings = ctx.settings
    cursor = last_id
    auth = ctx.request
    checked_at = time.monotonic()
    async with ctx.hub.subscribe(run_id) as sub:
        async with ctx.data_session() as session:
            run = await load_run(session, None, run_id)
            first_progress = await session.get(m.RunProgress, run_id)
            view = run_dto(run, first_progress)
            last_log = (
                await session.execute(
                    select(m.RunLog.attempt, m.RunLog.seq)
                    .where(m.RunLog.run_id == run_id)
                    .order_by(m.RunLog.attempt.desc(), m.RunLog.seq.desc())
                    .limit(1)
                )
            ).first()
        log_cursor: tuple[int, int] = (last_log[0], last_log[1]) if last_log else (0, -1)
        # keyed by the progress row's own attempt: after an infra retry the row can still
        # hold the previous attempt's (higher) seq, which must not hold back the new one
        progress_key = (
            (first_progress.attempt, first_progress.seq) if first_progress is not None else (0, -1)
        )
        sent_state = view.state
        yield _frame(SseEventName.SNAPSHOT, view.model_dump_json(), retry=SSE_RETRY_MS)
        last_write = time.monotonic()
        while True:
            frames: list[str] = []
            async with ctx.data_session() as session:
                # The state is read *before* the events (READ COMMITTED, separate statements):
                # a commit that appends the final event and makes the run terminal is then
                # either missed by both reads (seen next pass) or its event is read too, so
                # ``end`` never goes out without the run's last event.
                run = await load_run(session, None, run_id)
                rows = list(
                    (
                        await session.execute(
                            select(m.RunEvent)
                            .where(m.RunEvent.run_id == run_id, m.RunEvent.id > cursor)
                            .order_by(m.RunEvent.id)
                            .limit(_PAGE)
                        )
                    ).scalars()
                )
                for e in rows:
                    frames.append(_frame(e.type, event_view(e).model_dump_json(), id=e.id))
                    cursor = e.id
                progress = await session.get(m.RunProgress, run_id)
                if progress is not None and (progress.attempt, progress.seq) > progress_key:
                    progress_key = (progress.attempt, progress.seq)
                    frames.append(
                        _frame(
                            SseEventName.PROGRESS,
                            _live_json(
                                run_id,
                                progress.attempt,
                                progress.seq,
                                progress.updated_at,
                                "progress",
                                {
                                    "phase": progress.phase,
                                    "fraction": progress.fraction,
                                    "message": progress.message,
                                    "eta_seconds": progress.eta_seconds,
                                    "chains": progress.chains,
                                },
                            ),
                        )
                    )
                logs = list(
                    (
                        await session.execute(
                            select(m.RunLog)
                            .where(
                                m.RunLog.run_id == run_id,
                                or_(
                                    m.RunLog.attempt > log_cursor[0],
                                    and_(
                                        m.RunLog.attempt == log_cursor[0],
                                        m.RunLog.seq > log_cursor[1],
                                    ),
                                ),
                            )
                            .order_by(m.RunLog.attempt, m.RunLog.seq)
                            .limit(_PAGE)
                        )
                    ).scalars()
                )
                for line in logs:
                    log_cursor = (line.attempt, line.seq)
                    frames.append(
                        _frame(
                            SseEventName.LOG,
                            _live_json(
                                run_id,
                                line.attempt,
                                line.seq,
                                line.ts,
                                "log",
                                {
                                    "stream": line.stream,
                                    "line": line.line,
                                    "truncated": line.truncated,
                                },
                            ),
                        )
                    )
                state = RunState(run.state)
                if state is not sent_state or (state.terminal and len(rows) < _PAGE):
                    view = await run_view(session, run)
            if state is not sent_state:
                frames.append(_frame(SseEventName.SNAPSHOT, view.model_dump_json()))
                sent_state = state
            for f in frames:
                yield f
            if frames:
                last_write = time.monotonic()
            if len(rows) == _PAGE or len(logs) == _PAGE:
                continue  # more backlog: no waiting
            if state.terminal:
                yield _frame(SseEventName.END, view.model_dump_json())
                return
            if request is not None and await request.is_disconnected():
                return
            if (
                request is not None
                and auth is not None
                and time.monotonic() - checked_at >= settings.sse_reauth_s
            ):
                if not await still_authorized(request, auth):
                    log.info("closing the event stream of run %s: access was revoked", run_id)
                    return
                checked_at = time.monotonic()
            wait = min(
                settings.sse_poll_s,
                max(0.0, settings.sse_heartbeat_s - (time.monotonic() - last_write)),
            )
            notice = await sub.wait(wait) if wait > 0 else None
            if notice is None and time.monotonic() - last_write >= settings.sse_heartbeat_s:
                yield SSE_HEARTBEAT
                last_write = time.monotonic()
            sub.drain()


def _live_json(
    run_id: str, attempt: int, seq: int, ts: object, type_: str, payload: dict[str, object]
) -> str:
    from datetime import datetime

    stamp = ts.isoformat().replace("+00:00", "Z") if isinstance(ts, datetime) else str(ts)
    return json.dumps(
        {
            "run_id": run_id,
            "attempt": attempt,
            "seq": seq,
            "ts": stamp,
            "type": type_,
            "payload": payload,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


@router.get("/{run_id}/plots", response_model=Page[PlotSpec], operation_id="list_run_plots")
async def list_run_plots(
    ctx: Ctx,
    session: Session,
    run_id: RunId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[PlotSpec]:
    """Plots a run rendered (``plots.json``) in its newest attempt that has them: succeeded
    and partial runs, and failed or cancelled runs whose earlier partial attempt wrote plots.
    Every read passes the plot sanitizer; plots it rejects are left out. Empty while no
    attempt has plots. The control plane decodes nothing but this size-capped JSON, read at
    the generation the finalizer verified.
    """
    run = await load_run(session, ctx.scope, run_id)
    # only the finalizer writes ``outputs`` (verified objects), so a running attempt has none
    attempts = await session.scalars(
        select(m.RunAttempt)
        .where(m.RunAttempt.run_id == run.id, m.RunAttempt.attempt <= run.attempt)
        .order_by(m.RunAttempt.attempt.desc())
    )
    meta: dict[str, Any] | None = None
    for att in attempts:
        got = (att.outputs or {}).get(RUN_PLOTS_OUTPUT)
        if isinstance(got, dict):
            meta = got
            break
    if meta is None:
        return Page[PlotSpec](items=[], next_cursor=None)
    cap = ctx.settings.max_sidecar_bytes
    if int(meta.get("size", 0)) > cap:
        raise MapiError(
            f"{RUN_PLOTS_OUTPUT} exceeds the {cap}-byte sidecar limit",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    raw = await ctx.store.get(
        str(meta["bucket"]), str(meta["name"]), generation=int(meta["generation"]), max_bytes=cap
    )
    try:
        docs = loads_limited(raw)
    except ValueError:
        docs = None
    if not isinstance(docs, list):
        raise MapiError(
            f"{RUN_PLOTS_OUTPUT} is not a JSON array of plot specs",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    plots: list[PlotSpec] = []
    for doc in docs[:_MAX_RUN_PLOTS]:
        try:
            plots.append(sanitize_plot(doc).plot)
        except SanitizeError:
            continue  # the finalizer's rule for fit plots: unsafe plots are dropped
    start = int(decode_cursor(cursor) or 0)
    page = plots[start : start + limit]
    more = start + limit < len(plots)
    return Page[PlotSpec](items=page, next_cursor=encode_cursor(start + limit) if more else None)


@router.get("/{run_id}/logs", response_model=Page[LogLine], operation_id="run_logs")
async def run_logs(
    ctx: Ctx,
    session: Session,
    run_id: RunId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    attempt: Annotated[int | None, Query(ge=1)] = None,
    stream: Annotated[str | None, Query(pattern=r"^(stdout|stderr|system)$")] = None,
) -> Page[LogLine]:
    """Captured lines, oldest first. ``line`` is untrusted text: render it as text only."""
    await load_run(session, ctx.scope, run_id)
    stmt = select(m.RunLog).where(m.RunLog.run_id == run_id)
    if attempt is not None:
        stmt = stmt.where(m.RunLog.attempt == attempt)
    if stream is not None:
        stmt = stmt.where(m.RunLog.stream == stream)
    after = decode_cursor(cursor)
    if after is not None:
        a, s = int(after[0]), int(after[1])
        stmt = stmt.where(or_(m.RunLog.attempt > a, and_(m.RunLog.attempt == a, m.RunLog.seq > s)))
    rows = list(
        (
            await session.execute(stmt.order_by(m.RunLog.attempt, m.RunLog.seq).limit(limit + 1))
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[LogLine](
        items=[
            LogLine(
                run_id=r.run_id,
                attempt=r.attempt,
                seq=r.seq,
                ts=r.ts,
                stream=r.stream,
                line=r.line,
                truncated=r.truncated,
            )
            for r in rows
        ],
        next_cursor=encode_cursor([rows[-1].attempt, rows[-1].seq]) if more and rows else None,
    )


@router.post(
    "/{run_id}:cancel",
    response_model=Run,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="cancel_run",
)
async def cancel_run(ctx: Ctx, auth: Auth, session: Session, run_id: RunId) -> Run:
    """Idempotent: ``cancelling``, or the terminal state if the run already finished.

    A ``finalizing`` run is left alone (the response shows it still finalizing): its
    execution is over, and cancelling would only throw away a complete result after the
    finalizer reserved a version number and copied write-once bundle objects.
    """
    run = await load_run(session, ctx.scope, run_id, for_update=True)
    state = RunState(run.state)
    if not state.terminal and state is not RunState.FINALIZING:
        await apply_transition(session, run, RunTrigger.CANCEL, Actor.API)
        await write_audit(
            ctx.db,
            [auth.audit(ctx, AuditAction.RUN_CANCEL, ResourceKind.RUN, resource_id=run.id)],
            session=session,
        )
    return await run_view(session, run)


@router.post(
    "/{run_id}:resume",
    response_model=Run,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="resume_run",
)
async def resume_run(ctx: Ctx, session: Session, run_id: RunId) -> Run:
    """Re-queue a ``partial`` run from its checkpoints (new attempt)."""
    run = await load_run(session, ctx.scope, run_id, for_update=True)
    if run.state != RunState.PARTIAL.value:
        raise Conflict(f"only partial runs can be resumed (run is {run.state})")
    run.finalize = {}
    run.finalize_attempts = 0
    # resuming spends compute again: the same kill switches and quotas as a new run
    await admit(session, ctx, run, untrusted=run.untrusted_code, audit_action=None)
    await apply_transition(session, run, RunTrigger.RESUME, Actor.API)
    return await run_view(session, run)


@router.post(
    "/{run_id}:retry",
    response_model=Run,
    status_code=status.HTTP_201_CREATED,
    operation_id="retry_run",
    responses={200: {"model": Run, "description": "Idempotent replay"}},
)
async def retry_run(
    ctx: Ctx,
    session: Session,
    request: Request,
    run_id: RunId,
    body: RunRetry,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """A *new* run from a failed one (optionally one resource class up or a new seed)."""
    old = await load_run(session, ctx.scope, run_id)
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin(f"POST {request.url.path}", await request.json())
    if replay is not None:
        return await render_run(session, replay.resource_id, 200)
    run = await submit_retry(session, ctx, old, body)
    await idem.complete("run", run.id, 201)
    return await render_run(session, run.id, 201)


async def wait_terminal(
    ctx: ServerContext,
    run_id: str,
    timeout: float,
    *,
    disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> m.Run:
    """Wait (via notifications, with polling) until the run is terminal or ``timeout``.

    ``disconnected`` (``Request.is_disconnected``) is checked on every wake-up, at least
    every ``sse_poll_s``: a client that gave up does not keep a waiter and a notification
    subscription alive for the rest of its ``Prefer: wait``.
    """
    deadline = time.monotonic() + timeout
    async with ctx.hub.subscribe(run_id) as sub:
        while True:
            async with ctx.data_session() as session:
                run = await load_run(session, None, run_id)
            remaining = deadline - time.monotonic()
            if RunState(run.state).terminal or remaining <= 0:
                return run
            if disconnected is not None and await disconnected():
                return run
            await sub.wait(min(remaining, ctx.settings.sse_poll_s))
            await asyncio.sleep(0)
