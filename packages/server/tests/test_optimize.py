"""Budget optimisation (M3.5): ``POST /v1/models/{id}/versions/{v}:optimize`` queues a
sandboxed ``optimize`` run against the verified bundle, ``Prefer: wait`` answers 202 while
it runs, the finalizer verifies ``optimize.json`` and ``GET /v1/optimizations/{run_id}``
serves the plan - plus capability refusal, idempotent replays, the webhook event and tenant
isolation."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.optimize import load_optimize_result
from mapi_server.services.runs import apply_transition, load_run
from mapi_server.services.webhooks.ssrf import StaticResolver
from mapi_server_testkit.builders import simulate_outputs
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.integrations import fitted_version
from mapi_spec.api import Optimization
from mapi_spec.executor import RunKind
from mapi_spec.optimize import OPTIMIZE_OUTPUT, OptimizeJob, OptimizeResult
from mapi_spec.run import Actor, RunState, RunTrigger
from sqlalchemy import select

pytestmark = pytest.mark.integration

BODY: dict[str, Any] = {
    "total_budget": 1_000_000,
    "channels": {"tv": {"min_multiplier": 0.5, "max_multiplier": 1.5}},
    "objective": "p10",
    "frontier": {"n_points": 5},
}


async def _mmm_version(ctx: ServerContext) -> m.ModelVersion:
    """A ready model version whose model declares the decision capability (as the
    mmm-framework MMMs do)."""
    mv = await fitted_version(ctx)
    async with ctx.db.session() as s:
        row = await s.get(m.ModelVersion, mv.id)
        assert row is not None
        caps = dict(row.capabilities or {})
        caps["decision"] = True
        row.capabilities = caps
    return mv


def _url(mv: m.ModelVersion) -> str:
    return f"/v1/models/{mv.model_id}/versions/{mv.version}:optimize"


def _result() -> OptimizeResult:
    return OptimizeResult.model_validate(OptimizeResult.model_json_schema()["examples"][0])


async def _finalizing(ctx: ServerContext, run_id: str) -> tuple[m.Run, m.RunAttempt]:
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, run.attempt))
        assert att is not None
        s.expunge_all()
    return run, att


async def test_an_optimization_is_queued_against_the_bundle(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    mv = await _mmm_version(ctx)
    r = await client.post(_url(mv), json=BODY)
    assert r.status_code == 202, r.text
    view = Optimization.model_validate(r.json())
    assert view.run.kind == RunKind.OPTIMIZE and not view.done and view.result is None
    assert view.request.total_budget == 1_000_000 and view.request.objective == "p10"
    async with ctx.db.session() as s:
        run = await load_run(s, None, view.run.id)
    assert run.state == RunState.QUEUED.value and run.model_version_id == mv.id
    job = OptimizeJob.model_validate(run.payload)
    assert job.model.endswith(f"@v{mv.version}") and job.model.startswith("model://")
    assert "bundle" in run.inputs and set(run.inputs) <= {"bundle", "bundle.manifest"}
    got = Optimization.model_validate((await client.get(f"/v1/optimizations/{run.id}")).json())
    assert got.run.id == run.id and not got.done
    # Prefer: wait answers 202 while the run is not done (the fake executor never finishes)
    waited = await client.post(_url(mv), json=BODY, headers={"Prefer": "wait=1"})
    assert waited.status_code == 202
    assert waited.headers.get("Preference-Applied") == "wait=1"
    # an Idempotency-Key replays the same run
    first = await client.post(_url(mv), json=BODY, headers={"Idempotency-Key": "plan-1"})
    again = await client.post(_url(mv), json=BODY, headers={"Idempotency-Key": "plan-1"})
    assert first.json()["run"]["id"] == again.json()["run"]["id"]


async def test_models_without_a_decision_capability_are_refused(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    mv = await fitted_version(ctx)  # the trend model cannot optimise budgets
    r = await client.post(_url(mv), json=BODY)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "CAPABILITY_UNSUPPORTED"
    async with ctx.db.session() as s:
        kinds = (await s.execute(select(m.Run.kind))).scalars().all()
    assert RunKind.OPTIMIZE.value not in kinds


@pytest.mark.parametrize(
    "body",
    [
        {"total_budget": 10, "budget_change_pct": 5},
        {"objective": "p99"},
        {"mode": "free", "total_budget": 10},
        {"channels": {"tv": {"min_multiplier": 2, "max_multiplier": 1}}},
        {"unknown": 1},
    ],
)
async def test_invalid_requests_are_422(
    ctx: ServerContext, client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    mv = await _mmm_version(ctx)
    r = await client.post(_url(mv), json=body)
    assert r.status_code == 422, r.text


async def test_unknown_versions_are_404(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    mv = await _mmm_version(ctx)
    r = await client.post(f"/v1/models/{mv.model_id}/versions/99:optimize", json=BODY)
    assert r.status_code == 404
    assert (await client.get("/v1/optimizations/run_nope")).status_code == 404


async def test_the_finalized_plan_is_served_and_bad_plans_are_refused(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    mv = await _mmm_version(ctx)
    run_id = (await client.post(_url(mv), json=BODY)).json()["run"]["id"]
    run, att = await _finalizing(ctx, run_id)
    plan = _result()
    await simulate_outputs(
        ctx, run, att, add={OPTIMIZE_OUTPUT: (plan.model_dump_json().encode(), "application/json")}
    )
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    view = Optimization.model_validate((await client.get(f"/v1/optimizations/{run_id}")).json())
    assert view.done and view.result == plan
    assert view.result.allocation[0].channel == "tv"
    # the run's own page lists the plan's output
    assert (await client.get(f"/v1/runs/{run_id}")).status_code == 200
    # a run whose optimize.json is not a plan fails as an integrity problem
    bad_id = (await client.post(_url(mv), json=BODY)).json()["run"]["id"]
    bad, bad_att = await _finalizing(ctx, bad_id)
    await simulate_outputs(
        ctx,
        bad,
        bad_att,
        add={OPTIMIZE_OUTPUT: (json.dumps({"x": 1}).encode(), "application/json")},
    )
    assert await Finalizer(ctx).finalize(bad.id, bad_att.attempt) is RunState.FAILED
    failed = Optimization.model_validate((await client.get(f"/v1/optimizations/{bad_id}")).json())
    assert failed.done and failed.result is None
    assert failed.run.problem is not None and failed.run.problem.code == "INTEGRITY_FAILURE"
    # a run that uploads no plan at all fails too
    none_id = (await client.post(_url(mv), json=BODY)).json()["run"]["id"]
    nrun, natt = await _finalizing(ctx, none_id)
    await simulate_outputs(ctx, nrun, natt)
    assert await Finalizer(ctx).finalize(nrun.id, natt.attempt) is RunState.FAILED


async def test_a_finished_plan_fires_optimize_completed(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ctx.webhook_resolver = StaticResolver({"hooks.example.com": ["93.184.216.34"]})
    created = await client.post(
        "/v1/webhooks",
        json={"url": "https://hooks.example.com/mapi", "events": ["optimize.completed"]},
    )
    assert created.status_code == 201, created.text
    mv = await _mmm_version(ctx)
    run_id = (await client.post(_url(mv), json=BODY)).json()["run"]["id"]
    run, att = await _finalizing(ctx, run_id)
    await simulate_outputs(
        ctx,
        run,
        att,
        add={OPTIMIZE_OUTPUT: (_result().model_dump_json().encode(), "application/json")},
    )
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    async with ctx.db.session() as s:
        rows = (await s.execute(select(m.WebhookEvent))).scalars().all()
    events = [r for r in rows if r.type == "optimize.completed"]
    assert len(events) == 1
    assert events[0].envelope["data"]["run_id"] == run_id
    assert events[0].envelope["data"]["optimization"] == f"/v1/optimizations/{run_id}"


async def test_other_runs_and_other_tenants_are_not_optimizations(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    mv = await _mmm_version(ctx)
    assert (await client.get(f"/v1/optimizations/{mv.run_id}")).status_code == 404  # a fit
    run_id = (await client.post(_url(mv), json=BODY)).json()["run"]["id"]
    # the same id from another tenant's session: row-level security hides it
    async with ctx.db.tenant_session("someone-else") as s:
        with pytest.raises(Exception):  # noqa: B017 - NotFoundError from load_run
            run = await load_run(s, None, run_id)
            await load_optimize_result(s, ctx, run)
