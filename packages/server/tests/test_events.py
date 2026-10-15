"""Event ingestion: HMAC, dedupe, coalesced progress, logs, transitions, the HTTP path."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.events import EventIngestor, IngestResult, events_from_envelope
from mapi_server.services.runs import load_run
from mapi_server_testkit.builders import queued_fit, signed_event
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.executor import RunEvent, RunEventType
from mapi_spec.run import (
    InvalidEvent,
    LogPayload,
    MetricPayload,
    ProgressPayload,
    RunnerPhase,
    RunState,
    StatePayload,
    encode_event_line,
    new_hmac_key,
    sign_event,
)
from sqlalchemy import func, select


@pytest.fixture
async def provisioned(ctx: ServerContext) -> m.RunAttempt:
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        return att


async def _run(ctx: ServerContext, run_id: str) -> m.Run:
    async with ctx.db.session() as s:
        return await load_run(s, None, run_id)


async def test_first_verified_event_moves_to_running(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ing = EventIngestor(ctx)
    ev = signed_event(provisioned, 0, StatePayload(phase=RunnerPhase.BOOTSTRAP))
    assert await ing.ingest(ev) is IngestResult.ACCEPTED
    run = await _run(ctx, provisioned.run_id)
    assert run.state == RunState.RUNNING.value and run.started_at is not None
    assert run.last_event_at is not None and run.last_event_id == 1
    assert await ing.ingest(ev) is IngestResult.DUPLICATE
    assert (await _run(ctx, provisioned.run_id)).last_event_id == 1


async def test_bad_hmac_is_dropped_and_counted(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ing = EventIngestor(ctx)
    forged = signed_event(provisioned, 0, StatePayload(phase=RunnerPhase.FIT), key=new_hmac_key())
    assert await ing.ingest(forged) is IngestResult.BAD_HMAC
    unsigned = forged.model_copy(update={"hmac": None})
    assert await ing.ingest(unsigned) is IngestResult.BAD_HMAC
    run = await _run(ctx, provisioned.run_id)
    assert run.state == RunState.PROVISIONING.value  # forged events never change a run
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (provisioned.run_id, 1))
        assert att is not None and att.violations == 2


async def test_wrong_payload_shape_is_a_violation(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ev = sign_event(
        RunEvent(
            run_id=provisioned.run_id,
            attempt=1,
            seq=0,
            type=RunEventType.PROGRESS,
            ts=datetime.now(UTC),
            payload={"state": "pending"},
        ),
        provisioned.hmac_key,
    )
    assert await EventIngestor(ctx).ingest(ev) is IngestResult.INVALID


async def test_progress_is_coalesced_into_one_row(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ing = EventIngestor(ctx)
    for seq, frac in ((1, 0.2), (3, 0.6), (2, 0.4)):  # seq 2 arrives late
        await ing.ingest(
            signed_event(provisioned, seq, ProgressPayload(phase=RunnerPhase.SAMPLE, fraction=frac))
        )
    async with ctx.db.session() as s:
        rows = list((await s.execute(select(m.RunProgress))).scalars())
        n_events = (await s.execute(select(func.count()).select_from(m.RunEvent))).scalar_one()
    assert len(rows) == 1 and rows[0].seq == 3 and rows[0].fraction == 0.6
    assert n_events == 0  # progress is never appended to run_events


async def test_logs_and_persisted_types(ctx: ServerContext, provisioned: m.RunAttempt) -> None:
    ing = EventIngestor(ctx)
    await ing.ingest(signed_event(provisioned, 0, LogPayload(stream="stdout", line="hello")))
    await ing.ingest(signed_event(provisioned, 0, LogPayload(stream="stdout", line="hello")))
    await ing.ingest(signed_event(provisioned, 1, MetricPayload(name="rmse", value=0.5)))
    async with ctx.db.session() as s:
        logs = list((await s.execute(select(m.RunLog))).scalars())
        events = list((await s.execute(select(m.RunEvent))).scalars())
        att = await s.get(m.RunAttempt, (provisioned.run_id, 1))
    assert [x.line for x in logs] == ["hello"]
    assert [(e.id, e.type, e.seq, e.source) for e in events] == [(1, "metric", 1, "runner")]
    assert att is not None and att.log_bytes == 5 and att.last_seq == 1


async def test_stale_and_late_events_are_dropped(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ing = EventIngestor(ctx)
    ev = signed_event(provisioned, 0, StatePayload(phase=RunnerPhase.FIT))
    unknown = ev.model_copy(update={"attempt": 7})
    assert await ing.ingest(unknown) is IngestResult.UNKNOWN_ATTEMPT
    async with ctx.db.session() as s:
        run = await load_run(s, None, provisioned.run_id, for_update=True)
        run.state = RunState.FAILED.value
    assert await ing.ingest(ev) is IngestResult.LATE


async def test_internal_events_endpoint_accepts_pubsub_pushes(
    client: httpx.AsyncClient, ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ev = signed_event(provisioned, 0, StatePayload(phase=RunnerPhase.BOOTSTRAP))
    entry = {
        "jsonPayload": json.loads(encode_event_line(ev)),
        "labels": {"run.googleapis.com/execution_name": provisioned.external_id},
    }
    push = {
        "message": {
            "data": base64.b64encode(json.dumps(entry).encode()).decode(),
            "messageId": "1",
        },
        "subscription": "projects/p/subscriptions/mapi-events",
    }
    r = await client.post("/internal/events", json=push)
    assert r.status_code == 200 and r.json() == {"result": "accepted"}
    r = await client.post("/internal/events", json=push)  # Pub/Sub redelivery
    assert r.json() == {"result": "duplicate"}
    # identity cross-check against the execution label
    ev2 = signed_event(provisioned, 1, StatePayload(phase=RunnerPhase.FIT))
    entry2 = {
        "jsonPayload": json.loads(encode_event_line(ev2)),
        "labels": {"run.googleapis.com/execution_name": "someone-else"},
    }
    r = await client.post("/internal/events", json=entry2)
    assert r.json() == {"result": "identity_mismatch"}
    # a bare line object works too; malformed bodies are 422
    r = await client.post("/internal/events", json=json.loads(encode_event_line(ev2)))
    assert r.json() == {"result": "accepted"}
    r = await client.post("/internal/events", json={"message": {"data": "%%%"}})
    assert r.status_code == 422
    r = await client.post("/internal/events", json={"mapi_event": {"seq": "x"}})
    assert r.status_code == 422


async def test_internal_events_token(
    client: httpx.AsyncClient, ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ctx.settings.internal_token = "s3cret"
    ev = signed_event(provisioned, 0, StatePayload(phase=RunnerPhase.BOOTSTRAP))
    body = json.loads(encode_event_line(ev))
    r = await client.post("/internal/events", json=body)
    assert r.status_code == 401
    r = await client.post("/internal/events", json=body, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_envelope_parsing_rejects_garbage() -> None:
    with pytest.raises(InvalidEvent):
        events_from_envelope({"message": {"data": base64.b64encode(b"[1]").decode()}})
    with pytest.raises(InvalidEvent):
        events_from_envelope({"jsonPayload": "nope"})
    with pytest.raises(InvalidEvent):
        events_from_envelope({"something": "else"})
