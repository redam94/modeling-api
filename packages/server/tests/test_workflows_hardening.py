"""Workflow orchestration hardening (M3.5 review): fail-fast, cancellation, searches,
poisoned runs, fan-out limits, automation, cost roll-ups and route races."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import apply_transition, load_run
from mapi_server.services.workflows import WorkflowOrchestrator
from mapi_server.settings import Settings
from mapi_server_testkit.workflows import catalog_file, drive, execute_children, put_dataset
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.workflow import WorkflowRunStatus
from mapi_workflows.testing import weekly_series
from sqlalchemy import func, select

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

DATA = "dataset://default/sales@latest"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"catalog_path": catalog_file(tmp_path)})


def _doc(nodes: list[dict[str, Any]], name: str = "checks", **extra: Any) -> dict[str, Any]:
    return {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": name, "project": "default"},
        "inputs": {"data": {"type": "dataset"}},
        "nodes": nodes,
        **extra,
    }


async def _create(client: httpx.AsyncClient, doc: dict[str, Any]) -> str:
    r = await client.post("/v1/workflows", json={"document": doc})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _start(client: httpx.AsyncClient, wf_id: str, **body: Any) -> str:
    r = await client.post(f"/v1/workflows/{wf_id}/runs", json={"inputs": {"data": DATA}, **body})
    assert r.status_code == 202, r.text
    return str(r.json()["run_id"])


ADF = {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}}


async def _confirm_cancel(ctx: ServerContext, run_id: str) -> None:
    """The reconciler confirming that a cancelled child's execution is gone."""
    async with ctx.db.session() as s:
        row = await load_run(s, None, run_id, for_update=True)
        if row.state == RunState.CANCELLING.value:
            await apply_transition(s, row, RunTrigger.CANCEL_CONFIRMED, Actor.RECONCILER)


# ============================================================================ fail-fast


async def test_a_node_failing_at_start_stops_its_running_siblings(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    bad_fit = {
        "id": "fit",
        "use": "model.fit",
        "inputs": {"data": "inputs.data"},
        "with": {"model": "ts.sarimax@^1", "settings": {"order": "not-an-order", "bogus": 1}},
    }
    rid = await _start(client, await _create(client, _doc([ADF, bad_fit], name="ff")))
    orch = WorkflowOrchestrator(ctx)
    for _ in range(3):
        await orch.tick(rid)
    view = (await client.get(f"/v1/workflow-runs/{rid}")).json()
    adf = next(n for n in view["nodes"] if n["node_id"] == "adf")
    child = (await client.get(f"/v1/runs/{adf['run_id']}")).json()
    assert child["state"] in ("cancelling", "cancelled"), child["state"]
    await _confirm_cancel(ctx, adf["run_id"])
    await orch.tick(rid)
    view = (await client.get(f"/v1/workflow-runs/{rid}")).json()
    assert view["status"] == "failed", (view["status"], view["nodes"])
    assert view["error"]["code"] == "SETTINGS_INVALID"
    assert {n["node_id"]: n["status"] for n in view["nodes"]} == {
        "adf": "cancelled",
        "fit": "failed",
    }


# ============================================================================== searches

GRID = {
    "id": "grid",
    "use": "search.grid",
    "inputs": {"data": "inputs.data", "split": "hold.plan"},
    "with": {
        "model": "ts.sarimax@^1",
        "settings": {"order": [1, 0, 0]},
        "grid": {"/order": [[1, 0, 0], [2, 0, 0]]},
    },
}
HOLD = {
    "id": "hold",
    "use": "split.holdout",
    "inputs": {"data": "inputs.data"},
    "with": {"split": {"kind": "time_holdout", "dim": "time", "last": 8}},
}


async def test_cancelling_between_a_search_and_its_refit_starts_no_fit(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(120))
    rid = await _start(client, await _create(client, _doc([HOLD, GRID], name="gridwf")))
    orch = WorkflowOrchestrator(ctx)
    for _ in range(2):
        await orch.tick(rid)
        await execute_children(ctx)
    async with ctx.db.session() as s:
        row = await s.get(m.WorkflowNodeRunRow, (rid, "grid"))
        assert row is not None and row.child_run_id is not None
        child = await s.get(m.Run, row.child_run_id)
        assert child is not None and child.state == "succeeded" and row.phase == "node"
    # the backtests finished; the user cancels before the orchestrator collects them
    c = await client.post(f"/v1/workflow-runs/{rid}:cancel")
    assert c.status_code == 202
    for _ in range(3):
        await orch.tick(rid)
    view = (await client.get(f"/v1/workflow-runs/{rid}")).json()
    async with ctx.db.session() as s:
        fits = list((await s.execute(select(m.Run).where(m.Run.kind == "fit"))).scalars())
    assert [(f.id, f.state) for f in fits] == []
    assert view["status"] == "cancelled", view["status"]
    assert {n["node_id"]: n["status"] for n in view["nodes"]}["grid"] == "cancelled"


async def test_a_grid_search_over_a_backtest_plan_refits_the_winner(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(120))
    folds = {
        "id": "folds",
        "use": "split.expanding_window",
        "inputs": {"data": "inputs.data"},
        "with": {"horizons": [4], "n_folds": 2, "step": 4},
    }
    grid = {**GRID, "inputs": {"data": "inputs.data", "split": "folds.plan"}}
    rid = await _start(client, await _create(client, _doc([folds, grid], name="gridbt")))
    status = await drive(ctx, rid)
    view = (await client.get(f"/v1/workflow-runs/{rid}")).json()
    g = next(n for n in view["nodes"] if n["node_id"] == "grid")
    assert status is WorkflowRunStatus.SUCCEEDED, g.get("error")
    assert g["result"]["model_version"].startswith("model://default/wf-gridbt-grid@v")
    assert g["outputs"]["best"]["ref"] == g["result"]["model_version"]
    async with ctx.db.session() as s:
        fit = (await s.execute(select(m.Run).where(m.Run.kind == "fit"))).scalar_one()
    # the winner is fitted on all the data (a backtest plan is not a holdout)
    assert "split" not in fit.payload["data"]
    assert fit.payload["model"]["settings"]["order"] in ([1, 0, 0], [2, 0, 0])


# ========================================================================= poisoned runs

HUGE = int("9" * 400)


async def test_a_number_parameter_too_large_is_refused_at_start(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    adf = {**ADF, "with": {"alpha": {"$expr": "params.x * 0.0 + 0.05"}}}
    doc = _doc([adf], name="poison", params={"x": {"type": "number", "default": 1}})
    wf = await _create(client, doc)
    body = json.dumps({"inputs": {"data": DATA}, "params": {"x": HUGE}})
    r = await client.post(
        f"/v1/workflows/{wf}/runs",
        content=body.encode(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422 and r.json()["code"] == "WORKFLOW_INVALID", r.text


async def test_an_expression_overflowing_on_nested_data_fails_the_node(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    adf = {**ADF, "with": {"alpha": {"$expr": "params.cfg.x * 0.0 + 0.05"}}}
    doc = _doc([adf], name="poison2", params={"cfg": {"type": "object", "default": {}}})
    wf = await _create(client, doc)
    body = json.dumps({"inputs": {"data": DATA}, "params": {"cfg": {"x": HUGE}}})
    r = await client.post(
        f"/v1/workflows/{wf}/runs",
        content=body.encode(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 202, r.text
    rid = r.json()["run_id"]
    assert await drive(ctx, rid, max_rounds=5) is WorkflowRunStatus.FAILED
    view = (await client.get(f"/v1/workflow-runs/{rid}")).json()
    assert view["nodes"][0]["error"]["code"] == "EXPRESSION_ERROR"


async def test_a_run_that_cannot_be_advanced_is_failed_and_others_go_on(
    client: httpx.AsyncClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_workflows.engine import WorkflowEngine

    await put_dataset(ctx, "sales", weekly_series(80))
    wf = await _create(client, _doc([ADF], name="two"))
    bad, good = await _start(client, wf), await _start(client, wf)
    real_step = WorkflowEngine.step

    def step(self: WorkflowEngine) -> Any:
        if self.run_meta.get("id") == bad:
            raise OverflowError("int too large to convert to float")
        return real_step(self)

    monkeypatch.setattr(WorkflowEngine, "step", step)
    orch = WorkflowOrchestrator(ctx)
    assert await orch.tick(bad) is WorkflowRunStatus.QUEUED  # recorded, not raised
    # the loop goes on with the other run; the next pass over the bad one fails it
    assert await drive(ctx, good) is WorkflowRunStatus.SUCCEEDED
    view = (await client.get(f"/v1/workflow-runs/{bad}")).json()
    assert view["error"]["code"] == "INTERNAL_ERROR" and "OverflowError" in view["error"]["message"]
    assert (await client.get(f"/v1/runs/{bad}")).json()["state"] == "failed"


async def test_a_run_that_keeps_raising_is_stopped_with_its_children(
    client: httpx.AsyncClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    rid = await _start(client, await _create(client, _doc([ADF], name="stuck")))
    orch = WorkflowOrchestrator(ctx)
    await orch.tick(rid)  # the adf child is queued

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(WorkflowOrchestrator, "_sub_ports", boom)
    await orch.tick(rid)
    assert await orch.tick(rid) is WorkflowRunStatus.FAILED
    view = (await client.get(f"/v1/workflow-runs/{rid}")).json()
    assert view["status"] == "failed" and view["nodes"][0]["status"] == "cancelled"
    child = (await client.get(f"/v1/runs/{view['nodes'][0]['run_id']}")).json()
    assert child["state"] in ("cancelling", "cancelled")
    events = (await client.get(f"/v1/workflow-runs/{rid}/events")).json()["items"]
    assert events[-1]["type"] == "workflow.run.failed"


# ================================================================================ fan-out


async def test_sub_workflows_count_against_the_active_run_limit(
    client: httpx.AsyncClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ctx.settings, "workflow_max_active_runs", 5)
    await put_dataset(ctx, "sales", weekly_series(100))
    await _create(client, _doc([ADF], name="inner", outputs={"adf": "adf.result"}))
    nodes = [
        {
            "id": f"s{i}",
            "use": "subworkflow.run",
            "inputs": {"data": "inputs.data"},
            "with": {"workflow": "workflow://default/inner@latest"},
        }
        for i in range(8)
    ]
    outer = await _create(client, _doc(nodes, name="outer", execution={"max_parallel": 8}))
    rid = await _start(client, outer)
    await WorkflowOrchestrator(ctx).tick(rid)
    async with ctx.db.session() as s:
        active = (
            await s.execute(
                select(func.count())
                .select_from(m.WorkflowRunRow)
                .where(m.WorkflowRunRow.status.in_(["queued", "running", "cancelling"]))
            )
        ).scalar_one()
    assert active <= 5
    view = (await client.get(f"/v1/workflow-runs/{rid}")).json()
    failed = [n for n in view["nodes"] if n["status"] == "failed"]
    assert failed and failed[0]["error"]["code"] == "QUOTA_EXCEEDED"
    assert view["error"]["code"] == "QUOTA_EXCEEDED"


# ============================================================================= automation


async def _member(ctx: ServerContext, client: httpx.AsyncClient, role: str) -> tuple[Any, Any]:
    from mapi_server_testkit.identity import grant_role, make_user, session_caller

    bob = await make_user(ctx, "bob@default.test")
    await grant_role(ctx, bob, "default", "default", role)
    return bob, await session_caller(ctx, client, bob, tenant_id="default", project_id="default")


async def _remove(ctx: ServerContext, user_id: str) -> None:
    from sqlalchemy import delete

    async with ctx.db.session() as s:
        await s.execute(delete(m.Membership).where(m.Membership.principal_id == user_id))


async def _schedule(ctx: ServerContext) -> m.WorkflowScheduleRow:
    async with ctx.db.session() as s:
        return (await s.execute(select(m.WorkflowScheduleRow))).scalar_one()


async def test_a_removed_members_schedule_stops_firing(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from datetime import datetime, timedelta

    from mapi_server.services.workflows import fire_due_schedules

    await put_dataset(ctx, "sales", weekly_series(80))
    bob, me = await _member(ctx, client, "runner")
    wf = await _create(me, _doc([ADF], name="nightly"))
    url = f"/v1/workflows/{wf}/schedules"
    r = await me.post(url, json={"cron": "0 6 * * *", "inputs": {"data": DATA}})
    assert r.status_code == 201, r.text
    due = datetime.fromisoformat(r.json()["next_run_at"])
    assert await fire_due_schedules(ctx, at=due + timedelta(minutes=1)) == 1
    runs = (await client.get(f"/v1/workflows/{wf}/runs")).json()["items"]
    assert runs[0]["created_by"] == bob.id
    await _remove(ctx, bob.id)
    assert await fire_due_schedules(ctx, at=due + timedelta(days=1, minutes=1)) == 0
    sch = await _schedule(ctx)
    assert sch.active is False and "no longer" in (sch.last_error or "")
    assert len((await client.get(f"/v1/workflows/{wf}/runs")).json()["items"]) == 1


async def test_a_schedule_made_with_a_revoked_token_stops_firing(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from datetime import datetime, timedelta

    from mapi_server.services.workflows import fire_due_schedules
    from mapi_server_testkit.identity import make_token, token_caller

    await put_dataset(ctx, "sales", weekly_series(80))
    bob, _me = await _member(ctx, client, "runner")
    secret = await make_token(
        ctx, bob, "default", "default", ["workflows:write", "workflows:run", "models:read"]
    )
    tok = token_caller(client, secret)
    wf = await _create(client, _doc([ADF], name="tokened"))
    url = f"/v1/workflows/{wf}/schedules"
    r = await tok.post(url, json={"cron": "0 6 * * *", "inputs": {"data": DATA}})
    assert r.status_code == 201, r.text
    due = datetime.fromisoformat(r.json()["next_run_at"])
    assert await fire_due_schedules(ctx, at=due + timedelta(minutes=1)) == 1
    run = (await client.get(f"/v1/workflows/{wf}/runs")).json()["items"][0]
    assert run["created_by"] == bob.id  # the token's owner, not the token
    async with ctx.db.session() as s:
        row = (await s.execute(select(m.ApiToken))).scalars().first()
        assert row is not None
        row.revoked_at = datetime.now(tz=row.expires_at.tzinfo)
    assert await fire_due_schedules(ctx, at=due + timedelta(days=1, minutes=1)) == 0
    sch = await _schedule(ctx)
    assert sch.active is False and "token" in (sch.last_error or "")


async def test_a_removed_members_trigger_stops_firing(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from datetime import timedelta

    from mapi_server.services.runs import now
    from mapi_server.services.workflows import dataset_version_ready, fire_due_triggers

    await put_dataset(ctx, "sales", weekly_series(80))
    bob, me = await _member(ctx, client, "runner")
    wf = await _create(me, _doc([ADF], name="ontrigger"))
    r = await me.post(
        f"/v1/workflows/{wf}/triggers",
        json={"dataset": "dataset://default/sales", "input": "data", "debounce_seconds": 60},
    )
    assert r.status_code == 201, r.text
    await _remove(ctx, bob.id)
    ds, dv = await put_dataset(ctx, "sales", weekly_series(90))
    async with ctx.db.session() as s:
        d, v = await s.get(m.Dataset, ds.id), await s.get(m.DatasetVersion, (ds.id, dv.version))
        assert d is not None and v is not None
        await dataset_version_ready(s, d, v)
    assert await fire_due_triggers(ctx, at=now() + timedelta(seconds=61)) == 0
    async with ctx.db.session() as s:
        trg = (await s.execute(select(m.WorkflowTriggerRow))).scalar_one()
    assert trg.active is False and "no longer" in (trg.last_error or "")
    assert (await client.get(f"/v1/workflows/{wf}/triggers")).json()["items"][0]["active"] is False
    assert (await client.get(f"/v1/workflows/{wf}/runs")).json()["items"] == []


async def test_a_version_ready_while_its_trigger_fires_is_not_lost(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from datetime import timedelta

    from mapi_server.services.runs import now
    from mapi_server.services.workflows import dataset_version_ready, fire_due_triggers

    await put_dataset(ctx, "sales", weekly_series(80))
    wf = await _create(client, _doc([ADF], name="ontrigger"))
    r = await client.post(
        f"/v1/workflows/{wf}/triggers",
        json={"dataset": "dataset://default/sales", "input": "data", "debounce_seconds": 60},
    )
    assert r.status_code == 201, r.text

    async def ready(session: Any, version: tuple[m.Dataset, m.DatasetVersion]) -> None:
        ds, dv = version
        d, v = (
            await session.get(m.Dataset, ds.id),
            await session.get(m.DatasetVersion, (ds.id, dv.version)),
        )
        await dataset_version_ready(session, d, v)

    v2 = await put_dataset(ctx, "sales", weekly_series(90))
    async with ctx.db.session() as s:
        await ready(s, v2)
    v3 = await put_dataset(ctx, "sales", weekly_series(100))
    async with ctx.db.session() as fin:
        await ready(fin, v3)  # the finalizer of v3, its transaction still open ...
        during = await fire_due_triggers(ctx, at=now() + timedelta(seconds=61))  # ... the loop
    after = await fire_due_triggers(ctx, at=now() + timedelta(days=1))
    runs = (await client.get(f"/v1/workflows/{wf}/runs")).json()["items"]
    assert any(x["inputs"]["data"].endswith("@v3") for x in runs), (during, after, runs)


# ================================================================================== views


async def test_the_cost_roll_up_includes_sub_workflow_children(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(100))
    await _create(client, _doc([ADF], name="inner", outputs={"adf": "adf.result"}))
    sub = {
        "id": "sub",
        "use": "subworkflow.run",
        "inputs": {"data": "inputs.data"},
        "with": {"workflow": "workflow://default/inner@latest"},
    }
    rid = await _start(client, await _create(client, _doc([sub, {**ADF, "id": "own"}])))
    assert await drive(ctx, rid) is WorkflowRunStatus.SUCCEEDED
    view = (await client.get(f"/v1/workflow-runs/{rid}")).json()
    inner_id = next(n for n in view["nodes"] if n["node_id"] == "sub")["run_id"]
    inner = (await client.get(f"/v1/workflow-runs/{inner_id}")).json()
    assert inner["cost"] and inner["cost"]["vcpu_seconds"] > 0
    assert view["cost"]["vcpu_seconds"] == pytest.approx(2 * inner["cost"]["vcpu_seconds"])


# ================================================================================= races


async def test_a_cancel_racing_completion_does_not_reopen_the_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from mapi_server.services.workflows.runs import request_cancel

    await put_dataset(ctx, "sales", weekly_series(80))
    rid = await _start(client, await _create(client, _doc([ADF])))
    await WorkflowOrchestrator(ctx).tick(rid)
    async with ctx.db.session() as api:
        wr = await api.get(m.WorkflowRunRow, rid)  # the cancel route read it (running) ...
        assert wr is not None and wr.status == "running"
        assert await drive(ctx, rid) is WorkflowRunStatus.SUCCEEDED  # ... the run finished ...
        await request_cancel(api, wr)  # ... and the route's cancel lands afterwards
    await WorkflowOrchestrator(ctx).tick(rid)
    view = (await client.get(f"/v1/workflow-runs/{rid}")).json()
    assert view["status"] == "succeeded"
    assert (await client.get(f"/v1/runs/{rid}")).json()["state"] == "succeeded"
    events = (await client.get(f"/v1/workflow-runs/{rid}/events")).json()["items"]
    assert [e["type"] for e in events].count("workflow.run.succeeded") == 1
    async with ctx.db.session() as s:
        row = await s.get(m.WorkflowRunRow, rid)
        assert row is not None and row.cancel_requested is False
    again = await client.post(f"/v1/workflow-runs/{rid}:cancel")
    assert again.status_code == 202 and again.json()["status"] == "succeeded"


async def test_workflow_runs_and_their_node_runs_are_not_retried_one_by_one(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    wf = await _create(client, _doc([ADF], name="fr"))
    rid = await _start(client, wf)
    assert await drive(ctx, rid, fail=lambda _r: True) is WorkflowRunStatus.FAILED
    node_run = (await client.get(f"/v1/workflow-runs/{rid}")).json()["nodes"][0]["run_id"]
    assert (await client.get(f"/v1/runs/{node_run}")).json()["state"] == "failed"
    for run_id in (rid, node_run):
        r = await client.post(f"/v1/runs/{run_id}:retry", json={})
        assert r.status_code == 409 and r.json()["code"] == "CONFLICT", r.text
        assert f"/v1/workflows/{wf}/runs" in r.json()["detail"]
    async with ctx.db.session() as s:
        retried = (
            await s.execute(select(func.count()).where(m.Run.retry_of.is_not(None)))
        ).scalar_one()
    assert retried == 0
