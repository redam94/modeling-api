"""Workflow run events: persisted rows (SSE ids) and the live event stream.

The orchestrator appends the engine's events (``workflow.node.started``,
``workflow.branch.decided``, ...) in the transaction that changes the run and queues a
``NOTIFY`` on the run's channel; ``GET /v1/workflow-runs/{run_id}/events`` replays them
after ``Last-Event-ID`` and streams new ones (the database is the source of what is sent: a
lost notification delays a frame but never drops it).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Final

from fastapi import Request
from mapi_spec.api import SSE_HEARTBEAT, SSE_RETRY_MS
from mapi_spec.workflow import WorkflowEvent, WorkflowEventType, WorkflowRunStatus
from mapi_workflows.engine import EngineEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.notify import notify_run

__all__ = ["append_events", "event_dto", "workflow_event_stream"]

_PAGE: Final = 500


def event_dto(row: m.WorkflowEventRow) -> WorkflowEvent:
    return WorkflowEvent(
        seq=row.id,
        type=WorkflowEventType(row.type),
        run_id=row.run_id,
        node_id=row.node_id,
        ts=row.ts,
        data=row.data,
    )


async def append_events(
    session: AsyncSession, wr: m.WorkflowRunRow, events: Sequence[EngineEvent]
) -> None:
    """Persist ``events`` for run ``wr`` (the caller holds the run row lock)."""
    if not events:
        return
    for ev in events:
        wr.last_event_id += 1
        session.add(
            m.WorkflowEventRow(
                run_id=wr.run_id,
                id=wr.last_event_id,
                tenant_id=wr.tenant_id,
                project_id=wr.project_id,
                type=ev.type.value,
                node_id=ev.node_id,
                ts=ev.ts,
                data=json.loads(json.dumps(ev.data, default=str)),
            )
        )
    await session.flush()
    await notify_run(session, wr.run_id, "event", wr.last_event_id)


def _frame(event: str, data: str, *, id: int | None = None, retry: int | None = None) -> str:
    lines = []
    if id is not None:
        lines.append(f"id: {id}")
    if retry is not None:
        lines.append(f"retry: {retry}")
    lines.append(f"event: {event}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


async def workflow_event_stream(
    ctx: ServerContext,
    request: Request | None,
    run_id: str,
    last_id: int,
    snapshot: str,
) -> AsyncIterator[str]:
    """``snapshot`` (the run) -> events after ``last_id`` -> live events -> ``end``."""
    settings = ctx.settings
    cursor = last_id
    async with ctx.hub.subscribe(run_id) as sub:
        yield _frame("snapshot", snapshot, retry=SSE_RETRY_MS)
        last_write = time.monotonic()
        while True:
            frames: list[str] = []
            async with ctx.data_session() as session:
                wr = await session.get(m.WorkflowRunRow, run_id)
                status = WorkflowRunStatus(wr.status) if wr is not None else None
                rows = list(
                    (
                        await session.execute(
                            select(m.WorkflowEventRow)
                            .where(
                                m.WorkflowEventRow.run_id == run_id,
                                m.WorkflowEventRow.id > cursor,
                            )
                            .order_by(m.WorkflowEventRow.id)
                            .limit(_PAGE)
                        )
                    ).scalars()
                )
            for e in rows:
                frames.append(_frame(e.type, event_dto(e).model_dump_json(), id=e.id))
                cursor = e.id
            for f in frames:
                yield f
            if frames:
                last_write = time.monotonic()
            if len(rows) == _PAGE:
                continue
            if status is None or status.terminal:
                yield _frame(
                    "end",
                    json.dumps({"run_id": run_id, "status": status.value if status else None}),
                )
                return
            if request is not None and await request.is_disconnected():
                return
            wait = min(
                settings.sse_poll_s,
                max(0.0, settings.sse_heartbeat_s - (time.monotonic() - last_write)),
            )
            notice = await sub.wait(wait) if wait > 0 else None
            if notice is None and time.monotonic() - last_write >= settings.sse_heartbeat_s:
                yield SSE_HEARTBEAT
                last_write = time.monotonic()
            sub.drain()
