"""Server-sent events: snapshot, replay with Last-Event-ID resume, live frames, heartbeats,
the LISTEN/NOTIFY hub, and ``Prefer: wait`` scoring."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from mapi_server.api.runs import sse_stream
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.events import EventIngestor
from mapi_server.services.notify import RunNotice, notify_run
from mapi_server.services.runs import append_event, apply_transition, load_run, now
from mapi_server_testkit.builders import queued_fit, signed_event
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.api import SSE_HEARTBEAT
from mapi_spec.executor import RunEventType
from mapi_spec.run import (
    Actor,
    LogPayload,
    MetricPayload,
    ProgressPayload,
    RunnerPhase,
    RunState,
    RunTrigger,
    StatePayload,
)
from sqlalchemy import select


def parse_frames(text: str) -> list[dict[str, Any]]:
    frames = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, Any] = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                frame["comment"] = line[1:].strip()
            else:
                key, _, value = line.partition(": ")
                frame[key] = value
        if "data" in frame:
            frame["json"] = json.loads(frame["data"])
        frames.append(frame)
    return frames


async def _provisioned(ctx: ServerContext) -> m.RunAttempt:
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        return att


async def _terminal_with_events(ctx: ServerContext) -> tuple[str, int]:
    att = await _provisioned(ctx)
    ing = EventIngestor(ctx)
    await ing.ingest(signed_event(att, 0, StatePayload(phase=RunnerPhase.BOOTSTRAP)))
    await ing.ingest(signed_event(att, 1, MetricPayload(name="rmse", value=1.0)))
    await ing.ingest(signed_event(att, 2, MetricPayload(name="mape", value=0.1)))
    async with ctx.db.session() as s:
        run = await load_run(s, None, att.run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.FAIL_USER, Actor.RECONCILER)
        return run.id, run.last_event_id


async def test_stream_replays_then_ends(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    run_id, last = await _terminal_with_events(ctx)
    assert last == 3
    r = await client.get(f"/v1/runs/{run_id}/events", headers={"Accept": "text/event-stream"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    frames = parse_frames(r.text)
    assert frames[0]["event"] == "snapshot" and frames[0]["retry"] == "3000"
    assert "id" not in frames[0] and frames[0]["json"]["id"] == run_id
    assert [(f["event"], f.get("id")) for f in frames[1:4]] == [
        ("state", "1"),
        ("metric", "2"),
        ("metric", "3"),
    ]
    assert frames[1]["json"]["type"] == "state" and frames[1]["json"]["seq"] == 0
    assert frames[-1]["event"] == "end" and frames[-1]["json"]["state"] == "failed"


async def test_resume_after_disconnect_with_last_event_id(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    run_id, _ = await _terminal_with_events(ctx)
    r = await client.get(
        f"/v1/runs/{run_id}/events",
        headers={"Accept": "text/event-stream", "Last-Event-ID": "2"},
    )
    frames = parse_frames(r.text)
    assert [f["event"] for f in frames] == ["snapshot", "metric", "end"]
    assert frames[1]["id"] == "3"
    # garbage ids mean "from the start"
    r = await client.get(
        f"/v1/runs/{run_id}/events",
        headers={"Accept": "text/event-stream", "Last-Event-ID": "abc"},
    )
    assert [f.get("id") for f in parse_frames(r.text)][1:4] == ["1", "2", "3"]
    # JSON representation with ?after=
    page = (await client.get(f"/v1/runs/{run_id}/events?after=1&limit=1")).json()
    assert [e["id"] for e in page["items"]] == [2] and page["next_cursor"] == "2"


async def test_live_stream_with_progress_logs_heartbeat_and_end(ctx: ServerContext) -> None:
    att = await _provisioned(ctx)
    ing = EventIngestor(ctx)
    await ctx.hub.start()
    frames: list[str] = []

    async def consume() -> None:
        async for frame in sse_stream(ctx, None, att.run_id, 0):
            frames.append(frame)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.4)  # long enough for at least one heartbeat (0.3 s in tests)
    await ing.ingest(signed_event(att, 0, StatePayload(phase=RunnerPhase.FIT)))
    await ing.ingest(signed_event(att, 1, ProgressPayload(phase=RunnerPhase.SAMPLE, fraction=0.5)))
    await ing.ingest(signed_event(att, 2, LogPayload(stream="stderr", line="chain 0 done")))
    await asyncio.sleep(0.3)
    async with ctx.db.session() as s:
        run = await load_run(s, None, att.run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        await apply_transition(s, run, RunTrigger.VERIFIED, Actor.FINALIZER)
    await asyncio.wait_for(task, 5)
    text = "".join(frames)
    assert SSE_HEARTBEAT in frames
    parsed = [f for f in parse_frames(text) if "event" in f]
    events = [f["event"] for f in parsed]
    assert events[0] == "snapshot"
    assert "state" in events and "progress" in events and "log" in events
    assert events[-1] == "end" and parsed[-1]["json"]["state"] == "succeeded"
    snapshots = [f["json"]["state"] for f in parsed if f["event"] == "snapshot"]
    assert snapshots[0] == "provisioning" and "running" in snapshots
    progress = next(f for f in parsed if f["event"] == "progress")
    assert "id" not in progress and progress["json"]["payload"]["fraction"] == 0.5


async def test_streams_close_when_the_credential_is_revoked(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """A stream re-checks its caller's credential every ``sse_reauth_s``: a token revoked
    while a long run streams stops receiving its progress and logs, and reconnecting is
    refused."""
    from mapi_server.auth.identity import LOCAL_USER_ID
    from mapi_server_testkit.identity import make_token, token_caller

    ctx.settings = ctx.settings.model_copy(update={"sse_reauth_s": 0.2})
    att = await _provisioned(ctx)
    await ctx.hub.start()
    async with ctx.db.session() as s:
        owner = await s.get(m.User, LOCAL_USER_ID)
        assert owner is not None
    tok = token_caller(client, await make_token(ctx, owner, "default", "default", ["models:read"]))
    url = f"/v1/runs/{att.run_id}/events"

    async def revoke_soon() -> None:
        await asyncio.sleep(0.5)
        async with ctx.db.session() as s:
            for row in (await s.execute(select(m.ApiToken))).scalars():
                row.revoked_at = now()

    revoker = asyncio.create_task(revoke_soon())
    r = await asyncio.wait_for(tok.get(url, headers={"Accept": "text/event-stream"}), 10)
    await revoker
    assert r.status_code == 200
    events = [f["event"] for f in parse_frames(r.text) if "event" in f]
    assert events[0] == "snapshot" and "end" not in events  # closed, the run goes on
    again = await tok.get(url, headers={"Accept": "text/event-stream"})
    assert again.status_code == 401


async def test_end_is_never_sent_before_the_final_event(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit that appends the last event *and* makes the run terminal can land between
    the stream's reads; the stream must still send that event before ``end``."""
    import mapi_server.api.runs as runs_api

    att = await _provisioned(ctx)
    real = runs_api.load_run
    calls = 0

    async def racing_load_run(*args: Any, **kwargs: Any) -> m.Run:
        nonlocal calls
        calls += 1
        if calls == 2:  # first read inside the loop: the finalizer-style commit lands now
            async with ctx.db.session() as s:
                run = await real(s, None, att.run_id, for_update=True)
                await append_event(
                    s,
                    run,
                    type=RunEventType.ERROR,
                    payload={"failure_class": "integrity", "message": "boom"},
                )
                await apply_transition(s, run, RunTrigger.FAIL_USER, Actor.RECONCILER)
        return await real(*args, **kwargs)

    monkeypatch.setattr(runs_api, "load_run", racing_load_run)
    frames = [f async for f in sse_stream(ctx, None, att.run_id, 0)]
    events = [f["event"] for f in parse_frames("".join(frames)) if "event" in f]
    assert events[-1] == "end"
    assert "error" in events, events


async def test_progress_of_a_previous_attempt_is_not_shown_or_blocking(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    att1 = await _provisioned(ctx)
    ing = EventIngestor(ctx)
    await ing.ingest(signed_event(att1, 0, StatePayload(phase=RunnerPhase.FIT)))
    await ing.ingest(signed_event(att1, 9, ProgressPayload(phase=RunnerPhase.SAMPLE, fraction=0.9)))
    async with ctx.db.session() as s:
        run = await load_run(s, None, att1.run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.INFRA_RETRY, Actor.RECONCILER)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        att2 = await s.get(m.RunAttempt, (att1.run_id, 2))
        assert att2 is not None
    view = (await client.get(f"/v1/runs/{att1.run_id}")).json()
    assert view["attempt"] == 2 and view["progress"] is None  # attempt 1's 90 % is stale
    await ctx.hub.start()
    frames: list[str] = []

    async def consume() -> None:
        async for frame in sse_stream(ctx, None, att1.run_id, 0):
            frames.append(frame)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    await ing.ingest(signed_event(att2, 1, ProgressPayload(phase=RunnerPhase.SAMPLE, fraction=0.1)))
    await asyncio.sleep(0.3)
    async with ctx.db.session() as s:
        run = await load_run(s, None, att1.run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.FAIL_USER, Actor.RECONCILER)
    await asyncio.wait_for(task, 5)
    parsed = [f for f in parse_frames("".join(frames)) if "event" in f]
    assert parsed[0]["json"]["progress"] is None
    progress = [f["json"] for f in parsed if f["event"] == "progress"]
    assert [(p["attempt"], p["payload"]["fraction"]) for p in progress] == [(2, 0.1)]


async def test_notification_hub_fans_out_on_commit(ctx: ServerContext) -> None:
    await ctx.hub.start()
    assert ctx.hub.connected
    att = await _provisioned(ctx)
    async with ctx.hub.subscribe(att.run_id) as sub:
        assert ctx.hub.subscriber_count(att.run_id) == 1
        async with ctx.db.session() as s:
            run = await load_run(s, None, att.run_id, for_update=True)
            await append_event(
                s, run, type=RunEventType.WARNING, payload={"code": "X1", "message": "m"}
            )
        notice = await sub.wait(5)
        assert notice is not None and notice.run_id == att.run_id
        seen = [notice, *sub.drain()]
        assert any(n.kind == "event" and n.event_id == 1 for n in seen)
        ctx.hub.publish(RunNotice(att.run_id, "run"))
        assert (await sub.wait(1)) == RunNotice(att.run_id, "run")
    assert ctx.hub.subscriber_count() == 0
    async with ctx.db.session() as s:
        await notify_run(s, "other-run", "run")
    ctx.hub._dispatch("not json")  # malformed payloads are ignored


async def test_prefer_wait_stops_when_the_client_disconnects(ctx: ServerContext) -> None:
    """An abandoned long-poll (client timed out and retried) must not keep its waiter and
    notification subscription alive for the whole Prefer: wait interval."""
    from mapi_server.api.runs import wait_terminal

    run_id = await queued_fit(ctx)
    checks = 0

    async def disconnected() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    started = asyncio.get_running_loop().time()
    run = await asyncio.wait_for(
        wait_terminal(ctx, run_id, 30.0, disconnected=disconnected), timeout=5
    )
    assert asyncio.get_running_loop().time() - started < 5
    assert run.id == run_id and not RunState(run.state).terminal
    assert ctx.hub.subscriber_count(run_id) == 0


async def test_score_with_prefer_wait_returns_202_while_running(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from mapi_server.services.finalizer import Finalizer
    from mapi_server_testkit.builders import simulate_outputs

    att = await _provisioned(ctx)
    async with ctx.db.session() as s:
        run = await load_run(s, None, att.run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
    await simulate_outputs(ctx, run, att)
    await Finalizer(ctx).finalize(run.id, 1)
    async with ctx.db.session() as s:
        fitted = await load_run(s, None, run.id)
        mv = await s.get(m.ModelVersion, fitted.output_model_version_id)
        assert mv is not None
    body = {
        "request": {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": "model://default/trend@v1",
            "mode": "forecast",
            "horizon": {"dim": "t", "periods": 6},
        }
    }
    url = f"/v1/models/{mv.model_id}/versions/1:score"
    r = await client.post(url, json=body, headers={"Prefer": "wait=1", "Idempotency-Key": "s1"})
    assert r.status_code == 202, r.text
    assert r.headers["Preference-Applied"] == "wait=1"
    result = r.json()
    assert result["done"] is False and result["run"]["kind"] == "score"
    assert result["run"]["state"] == "queued" and result["outputs"] == []
    again = await client.post(url, json=body, headers={"Idempotency-Key": "s1"})
    assert again.json()["run"]["id"] == result["run"]["id"]
    async with ctx.db.session() as s:
        score_run = await s.get(m.Run, result["run"]["id"])
        assert score_run is not None
        assert score_run.inputs["bundle"]["bucket"] == ctx.buckets.bundles
        assert score_run.payload["model"] == "model://default/trend@v1"
    # the URL's version must match the request's model ref
    wrong = {**body, "request": {**body["request"], "model": "model://default/trend@v9"}}
    assert (await client.post(url, json=wrong)).status_code == 422
    # unsupported mode → MODE_NOT_SUPPORTED from the static CoordPlan checks
    bad = {
        **body,
        "request": {
            **body["request"],
            "mode": "counterfactual",
            "interventions": [{"var": "y", "multiply": 2}],
        },
    }
    r = await client.post(url, json=bad)
    assert r.status_code == 422 and r.json()["code"] == "MODE_NOT_SUPPORTED"
    ev = await client.post(f"/v1/models/{mv.model_id}/versions/1:evaluate", json={})
    assert ev.status_code == 202 and ev.json()["kind"] == "evaluate"


async def test_score_scoped_tokens_can_score_their_model(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """``score:<model id>`` and ``score:*`` tokens are the least-privilege scoring credential:
    they can call ``:score`` on their model, and nothing broader."""
    from mapi_server.auth.identity import LOCAL_USER_ID
    from mapi_server.services.finalizer import Finalizer
    from mapi_server_testkit.builders import simulate_outputs
    from mapi_server_testkit.identity import make_token, token_caller

    att = await _provisioned(ctx)
    async with ctx.db.session() as s:
        run = await load_run(s, None, att.run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
    await simulate_outputs(ctx, run, att)
    await Finalizer(ctx).finalize(run.id, 1)
    async with ctx.db.session() as s:
        fitted = await load_run(s, None, run.id)
        mv = await s.get(m.ModelVersion, fitted.output_model_version_id)
        owner = await s.get(m.User, LOCAL_USER_ID)
        assert mv is not None and owner is not None
    body = {
        "request": {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": "model://default/trend@v1",
            "mode": "forecast",
            "horizon": {"dim": "t", "periods": 6},
        }
    }
    url = f"/v1/models/{mv.model_id}/versions/1:score"

    async def caller(scope: str) -> Any:
        secret = await make_token(ctx, owner, "default", "default", [scope])
        return token_caller(client, secret)

    for scope in ("score:*", f"score:{mv.model_id}"):
        r = await (await caller(scope)).post(url, json=body)
        assert r.status_code == 202, (scope, r.text)
        assert r.json()["run"]["kind"] == "score"
    for scope in ("score:another-model", "models:read", "datasets:write"):
        r = await (await caller(scope)).post(url, json=body)
        assert r.status_code == 403, (scope, r.text)
    # a scoring token cannot fit, evaluate or read other things
    tok = await caller(f"score:{mv.model_id}")
    ev = await tok.post(f"/v1/models/{mv.model_id}/versions/1:evaluate", json={})
    assert ev.status_code == 403
    assert (await tok.get("/v1/datasets")).status_code == 403


async def test_wait_returns_200_with_outputs_when_the_run_finishes(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from mapi_server.api.models import _score_result

    att = await _provisioned(ctx)
    run_id = att.run_id
    async with ctx.db.session() as s:
        row = await s.get(m.RunAttempt, (run_id, 1))
        assert row is not None
        row.outputs = {
            "result.json": {
                "bucket": ctx.buckets.runs,
                "name": f"t/default/runs/{run_id}/a1/result.json",
                "generation": (
                    await ctx.store.put(
                        ctx.buckets.runs, f"t/default/runs/{run_id}/a1/result.json", b"{}"
                    )
                ).generation,
            }
        }
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        await apply_transition(s, run, RunTrigger.VERIFIED, Actor.FINALIZER)
    result = await _score_result(ctx, run_id)
    assert result.done and result.run.state is RunState.SUCCEEDED
    assert [o.name for o in result.outputs] == ["result.json"]
