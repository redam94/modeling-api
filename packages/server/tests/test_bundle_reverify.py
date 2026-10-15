"""Bundle inputs are re-verified against the signed manifest whenever URLs are minted for a run
(plan §8.6: dispatch and retry, not only submission)."""

from __future__ import annotations

import httpx
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run
from mapi_server_testkit.builders import queued_fit, simulate_outputs
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.run import Actor, RunState, RunTrigger


async def _model_version(ctx: ServerContext) -> m.ModelVersion:
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        s.expunge_all()
    await simulate_outputs(ctx, run, att)
    assert await Finalizer(ctx).finalize(run_id, 1) is RunState.SUCCEEDED
    async with ctx.db.session() as s:
        done = await load_run(s, None, run_id)
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
        assert mv is not None
        s.expunge_all()
    return mv


async def _evaluate(client: httpx.AsyncClient, mv: m.ModelVersion) -> str:
    r = await client.post(f"/v1/models/{mv.model_id}/versions/{mv.version}:evaluate", json={})
    assert r.status_code == 202, r.text
    return str(r.json()["id"])


async def _forge_manifest(ctx: ServerContext, mv: m.ModelVersion) -> None:
    async with ctx.db.session() as s:
        row = await s.get(m.ModelVersion, mv.id)
        assert row is not None
        row.manifest = {**row.manifest, "tenant_id": "evil"}


async def test_dispatch_refuses_a_bundle_whose_manifest_no_longer_verifies(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    mv = await _model_version(ctx)
    run_id = await _evaluate(client, mv)
    await _forge_manifest(ctx, mv)
    executor = FakeExecutor()
    await Dispatcher(ctx, executor).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
    assert executor.submit_calls == 0, "no URLs may be minted for an unverified bundle"
    assert run.state == RunState.FAILED.value and run.infra_retries == 0
    assert run.problem is not None and run.problem["code"] == "INTEGRITY_FAILURE"


async def test_dispatch_refuses_a_bundle_input_that_differs_from_the_manifest(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    mv = await _model_version(ctx)
    run_id = await _evaluate(client, mv)
    async with ctx.db.session() as s:  # a tampered run row points at another generation
        row = await load_run(s, None, run_id, for_update=True)
        bundle = dict(row.inputs["bundle"])
        bundle["generation"] = int(bundle["generation"]) + 1
        row.inputs = {**row.inputs, "bundle": bundle}
    executor = FakeExecutor()
    await Dispatcher(ctx, executor).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
    assert executor.submit_calls == 0
    assert run.state == RunState.FAILED.value
    assert run.problem is not None and run.problem["code"] == "INTEGRITY_FAILURE"


async def test_retry_re_verifies_the_bundle(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    mv = await _model_version(ctx)
    run_id = await _evaluate(client, mv)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.LEASE, Actor.DISPATCHER)
        await apply_transition(s, run, RunTrigger.SUBMITTED, Actor.DISPATCHER)
        await apply_transition(s, run, RunTrigger.FAIL_USER, Actor.RECONCILER)
    await _forge_manifest(ctx, mv)
    r = await client.post(f"/v1/runs/{run_id}:retry", json={})
    assert r.status_code == 500 and r.json()["code"] == "INTEGRITY_FAILURE", r.text
    listing = (await client.get("/v1/runs?kind=evaluate")).json()["items"]
    assert [x["id"] for x in listing] == [run_id]  # no new run was created
