"""Runs: submission (Idempotency-Key), listing, cancel/resume/retry, logs and events pages."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import apply_transition, load_run
from mapi_server_testkit.builders import LEAKY_SPEC, TREND_SPEC, ready_dataset
from mapi_spec.api import Run
from mapi_spec.run import Actor, RunState, RunTrigger


async def _spec(client: httpx.AsyncClient, ctx: ServerContext, yaml: str = TREND_SPEC) -> str:
    await ready_dataset(ctx)
    r = await client.post("/v1/specs", json={"yaml": yaml})
    assert r.status_code == 201, r.text
    spec_id: str = r.json()["id"]
    return spec_id


async def _force(ctx: ServerContext, run_id: str, *steps: tuple[RunTrigger, Actor]) -> None:
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        for trig, actor in steps:
            await apply_transition(s, run, trig, actor)


async def test_create_fit_run_pins_inputs_and_queues(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    spec_id = await _spec(client, ctx)
    r = await client.post(
        "/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1, "labels": {"a": "b"}}
    )
    assert r.status_code == 201, r.text
    run = Run.model_validate(r.json())
    assert run.state is RunState.QUEUED and run.attempt == 1 and run.kind == "fit"
    assert run.resource_class == "S" and run.timeout.seconds == 1800
    assert run.spec_hash and run.dataset_id and run.labels == {"a": "b"}
    async with ctx.db.session() as s:
        row = await s.get(m.Run, run.id)
        assert row is not None
        assert row.inputs["dataset"]["bucket"] == ctx.buckets.data
        assert row.payload["model"]["use"] == "ts.hsgp_trend@1.0.0"
        assert row.payload["pipeline"][0]["use"] == "core.standardize@1.0.0"
        assert row.payload["data"]["dataset"].startswith("dataset://default/co2@sha256:")
        assert row.checkpointable is False


async def test_idempotency_key_replays_or_conflicts(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    spec_id = await _spec(client, ctx)
    body: dict[str, Any] = {"kind": "fit", "spec_id": spec_id, "revision": 1}
    h = {"Idempotency-Key": "fit-click-1"}
    first = await client.post("/v1/runs", json=body, headers=h)
    assert first.status_code == 201
    again = await client.post("/v1/runs", json=body, headers=h)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    changed = await client.post("/v1/runs", json={**body, "resource_class": "M"}, headers=h)
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    bad = await client.post("/v1/runs", json=body, headers={"Idempotency-Key": "with space"})
    assert bad.status_code == 422
    listing = (await client.get("/v1/runs")).json()
    assert len(listing["items"]) == 1


async def test_invalid_revision_is_refused_with_its_first_error_code(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    spec_id = await _spec(client, ctx, LEAKY_SPEC)
    r = await client.post("/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1})
    assert r.status_code == 422
    assert r.json()["code"] == "LEAKAGE_RISK"
    assert (await client.get("/v1/runs")).json()["items"] == []
    r = await client.post("/v1/runs", json={"kind": "fit", "spec_id": "spc_nope", "revision": 1})
    assert r.status_code == 404


async def test_run_create_shape_rules(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/runs", json={"kind": "ingest"})
    assert r.status_code == 422
    r = await client.post("/v1/runs", json={"kind": "score", "model_version_id": "mv_x"})
    assert r.status_code == 422
    r = await client.post("/v1/runs", json={"kind": "evaluate", "model_version_id": "mv_missing"})
    assert r.status_code == 404


async def test_list_filters_and_pagination(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    spec_id = await _spec(client, ctx)
    ids = []
    for _ in range(3):
        r = await client.post("/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1})
        ids.append(r.json()["id"])
    v = await client.post("/v1/runs", json={"kind": "validate", "spec_id": spec_id, "revision": 1})
    page = (await client.get("/v1/runs?kind=fit&limit=2")).json()
    assert [x["id"] for x in page["items"]] == ids[::-1][:2]
    rest = (await client.get(f"/v1/runs?kind=fit&limit=2&cursor={page['next_cursor']}")).json()
    assert [x["id"] for x in rest["items"]] == [ids[0]] and rest["next_cursor"] is None
    only_validate = (await client.get("/v1/runs?kind=validate")).json()["items"]
    assert [x["id"] for x in only_validate] == [v.json()["id"]]
    assert len((await client.get(f"/v1/runs?spec_id={spec_id}&state=queued")).json()["items"]) == 4
    assert (await client.get("/v1/runs?state=running")).json()["items"] == []
    assert (await client.get("/v1/runs?state=bogus")).status_code == 422


async def test_cancel_is_idempotent(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    spec_id = await _spec(client, ctx)
    run = (
        await client.post("/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1})
    ).json()
    r = await client.post(f"/v1/runs/{run['id']}:cancel")
    assert r.status_code == 202 and r.json()["state"] == "cancelling"
    r = await client.post(f"/v1/runs/{run['id']}:cancel")
    assert r.status_code == 202 and r.json()["state"] == "cancelling"
    await _force(ctx, run["id"], (RunTrigger.CANCEL_CONFIRMED, Actor.RECONCILER))
    r = await client.post(f"/v1/runs/{run['id']}:cancel")
    assert r.status_code == 202 and r.json()["state"] == "cancelled"
    assert r.json()["problem"]["code"] == "CANCELLED"
    assert (await client.post("/v1/runs/run_missing:cancel")).status_code == 404


async def test_cancel_of_a_finalizing_run_is_a_no_op(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """The execution already finished: cancelling now would discard a complete result after
    the finalizer reserved a version number and copied write-once bundle objects."""
    spec_id = await _spec(client, ctx)
    run = (
        await client.post("/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1})
    ).json()
    await _force(
        ctx,
        run["id"],
        (RunTrigger.LEASE, Actor.DISPATCHER),
        (RunTrigger.SUBMITTED, Actor.DISPATCHER),
        (RunTrigger.COMPLETE, Actor.RECONCILER),
    )
    r = await client.post(f"/v1/runs/{run['id']}:cancel")
    assert r.status_code == 202 and r.json()["state"] == "finalizing"
    async with ctx.db.session() as s:
        row = await load_run(s, None, run["id"])
        assert row.state == RunState.FINALIZING.value


async def test_resume_and_retry_rules(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    spec_id = await _spec(client, ctx)
    run = (
        await client.post("/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1})
    ).json()
    rid = run["id"]
    assert (await client.post(f"/v1/runs/{rid}:resume")).status_code == 409
    assert (await client.post(f"/v1/runs/{rid}:retry", json={})).status_code == 409
    await _force(
        ctx,
        rid,
        (RunTrigger.LEASE, Actor.DISPATCHER),
        (RunTrigger.SUBMITTED, Actor.DISPATCHER),
        (RunTrigger.FAIL_RESOURCE_EXHAUSTED, Actor.RECONCILER),
    )
    r = await client.post(
        f"/v1/runs/{rid}:retry",
        json={"resource_class": "M", "seed": 11},
        headers={"Idempotency-Key": "retry-1"},
    )
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["retry_of"] == rid and new["resource_class"] == "M" and new["state"] == "queued"
    again = await client.post(
        f"/v1/runs/{rid}:retry",
        json={"resource_class": "M", "seed": 11},
        headers={"Idempotency-Key": "retry-1"},
    )
    assert again.status_code == 200 and again.json()["id"] == new["id"]
    async with ctx.db.session() as s:
        row = await s.get(m.Run, new["id"])
        assert row is not None and row.payload["model"]["fit"]["seed"] == 11
    # resume a PARTIAL run → new attempt, queued
    run2 = (
        await client.post("/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1})
    ).json()
    await _force(
        ctx,
        run2["id"],
        (RunTrigger.LEASE, Actor.DISPATCHER),
        (RunTrigger.SUBMITTED, Actor.DISPATCHER),
        (RunTrigger.COMPLETE, Actor.RECONCILER),
        (RunTrigger.VERIFIED_PARTIAL, Actor.FINALIZER),
    )
    r = await client.post(f"/v1/runs/{run2['id']}:resume")
    assert r.status_code == 202
    assert r.json()["state"] == "queued" and r.json()["attempt"] == 2


async def test_logs_and_events_pages(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    spec_id = await _spec(client, ctx)
    run = (
        await client.post("/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1})
    ).json()
    async with ctx.db.session() as s:
        for seq in range(5):
            s.add(
                m.RunLog(
                    run_id=run["id"],
                    tenant_id="default",
                    project_id="default",
                    attempt=1,
                    seq=seq,
                    ts=datetime.now(UTC),
                    stream="stderr" if seq % 2 else "stdout",
                    line=f"line {seq}",
                    truncated=False,
                )
            )
    page = (await client.get(f"/v1/runs/{run['id']}/logs?limit=3")).json()
    assert [x["line"] for x in page["items"]] == ["line 0", "line 1", "line 2"]
    rest = (await client.get(f"/v1/runs/{run['id']}/logs?cursor={page['next_cursor']}")).json()
    assert [x["seq"] for x in rest["items"]] == [3, 4]
    errs = (await client.get(f"/v1/runs/{run['id']}/logs?stream=stderr")).json()
    assert [x["seq"] for x in errs["items"]] == [1, 3]
    events = (await client.get(f"/v1/runs/{run['id']}/events")).json()
    assert events == {"items": [], "next_cursor": None}
    assert (await client.get("/v1/runs/run_missing/events")).status_code == 404
    assert (await client.get("/v1/runs/run_missing/logs")).status_code == 404


async def test_run_endpoints_are_scoped_to_the_project(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """A run of another project is invisible by id on every run endpoint (plots included)."""
    spec_id = await _spec(client, ctx)
    run = (
        await client.post("/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1})
    ).json()
    async with ctx.db.session() as s:
        row = await s.get(m.Run, run["id"])
        assert row is not None
        row.project_id = "someone-else"
    for path in ("", "/plots", "/logs", "/events"):
        r = await client.get(f"/v1/runs/{run['id']}{path}")
        assert r.status_code == 404, (path, r.status_code, r.text)
