"""Workflows through the control plane (M3.5): documents and revisions, runs driven by the
orchestrator with children executed for real in-process, caching, cancellation, retries,
sub-workflows, governance gating, schedules, dataset triggers, SSE, webhooks, row-level
security and the route table."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.runs import apply_transition, load_run, now
from mapi_server.services.webhooks.crypto import SecretBox
from mapi_server.services.webhooks.manage import new_webhook
from mapi_server.services.workflows import (
    WorkflowOrchestrator,
    dataset_version_ready,
    fire_due_schedules,
    fire_due_triggers,
    set_policy_resolver,
)
from mapi_server.settings import Settings
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.workflows import catalog_file, drive, put_dataset
from mapi_spec.api import M35_ENDPOINTS
from mapi_spec.governance import GovernancePolicy, PolicyCheck
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.webhooks import WebhookEventType
from mapi_spec.workflow import WorkflowRunStatus
from mapi_spec.workflow_templates import TS_READINESS_ARIMAX
from mapi_workflows.testing import weekly_series
from sqlalchemy import func, select

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

DATA = "dataset://default/sales@latest"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    """The suite's settings with the test registry's catalog (``ts.sarimax`` and friends)."""
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


SMALL = [
    {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
    {"id": "wanders", "use": "branch.if", "with": {"condition": "not nodes.adf.reject"}},
    {
        "id": "diff",
        "use": "transform.difference",
        "inputs": {"data": "inputs.data"},
        "when": "wanders.then",
    },
    {"id": "kpss", "use": "test.kpss", "inputs": {"data": ["diff.data", "inputs.data"]}},
    {"id": "report", "use": "report.summary", "inputs": {"tests": ["adf.result", "kpss.result"]}},
]


async def _create(client: httpx.AsyncClient, doc: dict[str, Any]) -> dict[str, Any]:
    r = await client.post("/v1/workflows", json={"document": doc})
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def _start(client: httpx.AsyncClient, wf_id: str, **body: Any) -> dict[str, Any]:
    r = await client.post(f"/v1/workflows/{wf_id}/runs", json={"inputs": {"data": DATA}, **body})
    assert r.status_code == 202, r.text
    out: dict[str, Any] = r.json()
    return out


# =================================================================== catalog and gallery


async def test_node_catalog_and_template_gallery(client: httpx.AsyncClient) -> None:
    page = (await client.get("/v1/catalog/workflow-nodes", params={"limit": 200})).json()
    ids = [n["id"] for n in page["items"]]
    assert "test.adf" in ids and "model.fit" in ids and len(ids) == 51
    tests = (await client.get("/v1/catalog/workflow-nodes", params={"kind": "test"})).json()
    assert all(n["kind"] == "test" for n in tests["items"])
    adf = (await client.get("/v1/catalog/workflow-nodes/test.adf")).json()
    assert adf["about"]["title"].startswith("Does the series wander")
    assert (await client.get("/v1/catalog/workflow-nodes/test.nope")).status_code == 404
    first = (await client.get("/v1/catalog/workflow-nodes", params={"limit": 10})).json()
    second = (
        await client.get(
            "/v1/catalog/workflow-nodes", params={"limit": 10, "cursor": first["next_cursor"]}
        )
    ).json()
    assert not {n["id"] for n in first["items"]} & {n["id"] for n in second["items"]}
    gallery = (await client.get("/v1/workflow-templates")).json()
    assert {t["id"] for t in gallery["items"]} == {
        "ts_readiness_arimax",
        "var_vecm_cointegration",
        "mmm_governed",
        "forecast_bakeoff",
    }
    one = (await client.get("/v1/workflow-templates/ts_readiness_arimax")).json()
    assert one["about"]["difficulty"] == "beginner"
    assert (await client.get("/v1/workflow-templates/nope")).status_code == 404


# ============================================================== documents and revisions


async def test_validate_create_revise_archive_delete(client: httpx.AsyncClient) -> None:
    bad = _doc([{"id": "adf", "use": "test.adf", "inputs": {"data": "nowhere.data"}}])
    v = (await client.post("/v1/workflows:validate", json={"document": bad})).json()
    assert v["valid"] is False
    assert any(i["pointer"] == "/nodes/0/inputs/data" for i in v["issues"])
    ok = (await client.post("/v1/workflows:validate", json={"document": _doc(SMALL)})).json()
    assert ok["valid"] and ok["order"][0] == "adf"

    wf = await _create(client, _doc(SMALL))
    assert wf["ref"] == "workflow://default/checks" and wf["latest_revision"] == 1
    dup = await client.post("/v1/workflows", json={"document": _doc(SMALL)})
    assert dup.status_code == 409
    revs = (await client.get(f"/v1/workflows/{wf['id']}/revisions")).json()["items"]
    assert revs[0]["validation"]["valid"] and revs[0]["spec_hash"].startswith("sha256:")

    stale = await client.post(
        f"/v1/workflows/{wf['id']}/revisions",
        json={"document": _doc(SMALL), "expected_revision": 7},
    )
    assert stale.status_code == 409
    text = (
        "apiVersion: mapi/v1\nkind: Workflow\nmetadata: {name: checks, project: default}\n"
        "inputs: {data: {type: dataset}}\n"
        "nodes:\n  - {id: adf, use: test.adf, inputs: {data: inputs.data}}\n"
    )
    r2 = await client.post(
        f"/v1/workflows/{wf['id']}/revisions",
        json={"text": text, "expected_revision": 1, "message": "simpler"},
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["revision"] == 2 and r2.json()["ref"].endswith("@r2")
    renamed = copy.deepcopy(_doc(SMALL))
    renamed["metadata"]["name"] = "other"
    assert (
        await client.post(f"/v1/workflows/{wf['id']}/revisions", json={"document": renamed})
    ).status_code == 422
    got = (await client.get(f"/v1/workflows/{wf['id']}/revisions/1")).json()
    assert got["document"]["nodes"][0]["id"] == "adf"
    assert (await client.get(f"/v1/workflows/{wf['id']}/revisions/9")).status_code == 404

    patched = await client.patch(f"/v1/workflows/{wf['id']}", json={"archived": True})
    assert patched.json()["archived"] is True
    listed = (await client.get("/v1/workflows", params={"archived": "true"})).json()["items"]
    assert [w["id"] for w in listed] == [wf["id"]]
    refused = await client.post(f"/v1/workflows/{wf['id']}/runs", json={"inputs": {}})
    assert refused.status_code == 409
    assert (await client.delete(f"/v1/workflows/{wf['id']}")).status_code == 204
    assert (await client.get(f"/v1/workflows/{wf['id']}")).status_code == 404


async def test_templates_and_unrunnable_revisions(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/workflows", json={"template": "ts_readiness_arimax"})
    assert r.status_code == 201, r.text
    rev = (await client.get(f"/v1/workflows/{r.json()['id']}/revisions/1")).json()
    assert rev["validation"]["valid"], rev["validation"]["issues"]
    assert rev["document"]["metadata"]["labels"]["workflow-template"] == "ts_readiness_arimax"
    doc = _doc(
        [
            {
                "id": "fit",
                "use": "model.fit",
                "inputs": {"data": "inputs.data"},
                "with": {"model": "mmm.unknown@^1"},
            }
        ],
        name="broken",
    )
    wf = await _create(client, doc)
    rev = (await client.get(f"/v1/workflows/{wf['id']}/revisions/1")).json()
    assert not rev["validation"]["valid"]
    assert rev["validation"]["issues"][0]["code"] == "REF_UNRESOLVED"
    refused = await client.post(f"/v1/workflows/{wf['id']}/runs", json={"inputs": {"data": DATA}})
    assert refused.status_code == 409
    assert refused.json()["code"] == "WORKFLOW_NOT_RUNNABLE"


async def test_run_inputs_and_params_are_checked(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    wf = await _create(client, _doc(SMALL, params={"alpha": {"type": "number", "default": 0.05}}))
    missing = await client.post(f"/v1/workflows/{wf['id']}/runs", json={"inputs": {}})
    assert missing.status_code == 422 and missing.json()["code"] == "WORKFLOW_INVALID"
    unknown = await client.post(
        f"/v1/workflows/{wf['id']}/runs", json={"inputs": {"data": "dataset://default/nope@latest"}}
    )
    assert unknown.json()["code"] == "REF_UNRESOLVED"
    bad = await client.post(
        f"/v1/workflows/{wf['id']}/runs", json={"inputs": {"data": DATA}, "params": {"alpha": "x"}}
    )
    assert bad.status_code == 422
    assert bad.json()["errors"][0]["pointer"] == "/params/alpha"
    run = await _start(client, wf["id"], params={"alpha": 0.1})
    assert run["status"] == "queued" and run["params"] == {"alpha": 0.1}
    assert run["inputs"]["data"] == "dataset://default/sales@v1"
    assert [n["status"] for n in run["nodes"]] == ["pending"] * 5
    parent = (await client.get(f"/v1/runs/{run['run_id']}")).json()
    assert parent["kind"] == "workflow" and parent["state"] == "queued"


# ======================================================================== orchestration


async def test_small_workflow_runs_with_branches_children_and_events(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(120, random_walk=True))
    wf = await _create(client, _doc(SMALL))
    run = await _start(client, wf["id"])
    status = await drive(ctx, run["run_id"])
    assert status is WorkflowRunStatus.SUCCEEDED
    view = (await client.get(f"/v1/workflow-runs/{run['run_id']}")).json()
    nodes = {n["node_id"]: n for n in view["nodes"]}
    assert nodes["adf"]["result"]["decision"] == "unit_root"
    assert view["decisions"][0]["arm"] == "then"
    assert view["decisions"][0]["inputs"] == {"nodes.adf.reject": False}
    # the differenced series is a registered dataset version, and KPSS read it
    diff_ref = nodes["diff"]["outputs"]["data"]["ref"]
    assert diff_ref == "dataset://default/wf-checks-diff@v1"
    assert nodes["kpss"]["result"]["n_obs"] == 119
    # report is computed in the control plane: no child run
    assert nodes["report"]["run_id"] is None and nodes["report"]["status"] == "succeeded"
    assert nodes["adf"]["run_id"] and nodes["adf"]["child_runs"] == [nodes["adf"]["run_id"]]
    child = (await client.get(f"/v1/runs/{nodes['adf']['run_id']}")).json()
    assert child["kind"] == "workflow_node" and child["parent_run_id"] == run["run_id"]
    assert child["labels"]["workflow-node"] == "adf"
    parent = (await client.get(f"/v1/runs/{run['run_id']}")).json()
    assert parent["state"] == "succeeded" and parent["finished_at"]
    assert view["cost"] is not None and view["cost"]["vcpu_seconds"] > 0  # the children's roll-up
    events = (await client.get(f"/v1/workflow-runs/{run['run_id']}/events")).json()["items"]
    types = [e["type"] for e in events]
    assert types[0] == "workflow.run.started" and types[-1] == "workflow.run.succeeded"
    assert "workflow.branch.decided" in types and "workflow.node.started" in types
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    # node views and output links
    one = (await client.get(f"/v1/workflow-runs/{run['run_id']}/nodes/diff")).json()
    assert one["status"] == "succeeded"
    page = (
        await client.get(f"/v1/workflow-runs/{run['run_id']}/nodes", params={"status": "succeeded"})
    ).json()
    assert len(page["items"]) == 5
    link = await client.get(f"/v1/workflow-runs/{run['run_id']}/nodes/diff/outputs/data")
    assert link.status_code == 200 and link.json()["name"] == "data.zarr.zip"
    assert (
        await client.get(f"/v1/workflow-runs/{run['run_id']}/nodes/adf/outputs/result")
    ).status_code == 404
    runs = (await client.get(f"/v1/workflows/{wf['id']}/runs")).json()["items"]
    assert [r["run_id"] for r in runs] == [run["run_id"]]


async def test_unchanged_nodes_are_reused_from_the_cache(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(120, random_walk=True))
    wf = await _create(client, _doc(SMALL))
    first = await _start(client, wf["id"])
    assert await drive(ctx, first["run_id"]) is WorkflowRunStatus.SUCCEEDED
    async with ctx.db.session() as s:
        before = (await s.execute(select(func.count()).select_from(m.Run))).scalar_one()
    second = await _start(client, wf["id"])
    assert await drive(ctx, second["run_id"]) is WorkflowRunStatus.SUCCEEDED
    view = (await client.get(f"/v1/workflow-runs/{second['run_id']}")).json()
    nodes = {n["node_id"]: n for n in view["nodes"]}
    for nid in ("adf", "diff", "kpss"):
        assert nodes[nid]["status"] == "cached", nid
        assert nodes[nid]["cached_from"] == first["run_id"]
    async with ctx.db.session() as s:
        after = (await s.execute(select(func.count()).select_from(m.Run))).scalar_one()
    assert after == before + 1  # only the parent run: no child ran again
    third = await _start(client, wf["id"], use_cache=False)
    assert await drive(ctx, third["run_id"]) is WorkflowRunStatus.SUCCEEDED
    view = (await client.get(f"/v1/workflow-runs/{third['run_id']}")).json()
    assert {n["status"] for n in view["nodes"]} == {"succeeded"}


async def test_cancelling_a_run_cancels_its_children(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    wf = await _create(client, _doc(SMALL))
    run = await _start(client, wf["id"])
    orch = WorkflowOrchestrator(ctx)
    await orch.tick(run["run_id"])
    view = (await client.get(f"/v1/workflow-runs/{run['run_id']}")).json()
    child_id = view["nodes"][0]["run_id"]
    assert view["status"] == "running" and child_id
    r = await client.post(f"/v1/workflow-runs/{run['run_id']}:cancel")
    assert r.status_code == 202 and r.json()["status"] == "cancelling"
    await orch.tick(run["run_id"])
    child = (await client.get(f"/v1/runs/{child_id}")).json()
    assert child["state"] == "cancelling"
    async with ctx.db.session() as s:  # the reconciler confirms the cancellation
        row = await load_run(s, None, child_id, for_update=True)
        await apply_transition(s, row, RunTrigger.CANCEL_CONFIRMED, Actor.RECONCILER)
    await orch.tick(run["run_id"])
    view = (await client.get(f"/v1/workflow-runs/{run['run_id']}")).json()
    assert view["status"] == "cancelled"
    assert {n["status"] for n in view["nodes"]} == {"cancelled"}
    parent = (await client.get(f"/v1/runs/{run['run_id']}")).json()
    assert parent["state"] == "cancelled"
    again = await client.post(f"/v1/workflow-runs/{run['run_id']}:cancel")
    assert again.json()["status"] == "cancelled"


async def test_failed_children_are_retried_then_fail_the_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    nodes = copy.deepcopy(SMALL)
    nodes[0]["retries"] = 1
    wf = await _create(client, _doc(nodes))
    run = await _start(client, wf["id"])
    calls: list[str] = []

    def fail_first(r: m.Run) -> bool:
        calls.append(r.id)
        return len(calls) == 1

    assert await drive(ctx, run["run_id"], fail=fail_first) is WorkflowRunStatus.SUCCEEDED
    adf = (await client.get(f"/v1/workflow-runs/{run['run_id']}/nodes/adf")).json()
    assert adf["attempt"] == 2 and len(adf["child_runs"]) == 2
    wf2 = await _create(client, _doc(SMALL, name="fragile"))
    run2 = await _start(client, wf2["id"], use_cache=False)
    assert await drive(ctx, run2["run_id"], fail=lambda r: True) is WorkflowRunStatus.FAILED
    view = (await client.get(f"/v1/workflow-runs/{run2['run_id']}")).json()
    assert view["error"]["code"] == "USER_CODE_ERROR" and "adf" in view["error"]["message"]
    statuses = {n["node_id"]: n["status"] for n in view["nodes"]}
    assert statuses["adf"] == "failed" and statuses["kpss"] == "cancelled"
    parent = (await client.get(f"/v1/runs/{run2['run_id']}")).json()
    assert parent["state"] == "failed" and parent["failure_class"] == "user"


async def test_subworkflows_run_as_child_workflow_runs(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(100))
    inner = _doc(
        [{"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}}],
        name="inner",
        outputs={"adf": "adf.result"},
    )
    await _create(client, inner)
    outer = _doc(
        [
            {
                "id": "sub",
                "use": "subworkflow.run",
                "inputs": {"data": "inputs.data"},
                "with": {"workflow": "workflow://default/inner@latest"},
            },
            {"id": "report", "use": "report.summary", "inputs": {"tests": ["sub.adf"]}},
        ],
        name="outer",
    )
    wf = await _create(client, outer)
    run = await _start(client, wf["id"])
    assert await drive(ctx, run["run_id"]) is WorkflowRunStatus.SUCCEEDED
    view = (await client.get(f"/v1/workflow-runs/{run['run_id']}")).json()
    sub = next(n for n in view["nodes"] if n["node_id"] == "sub")
    assert sub["outputs"]["adf"]["inline"]["test"] == "test.adf"
    child = (await client.get(f"/v1/workflow-runs/{sub['run_id']}")).json()
    assert child["parent_run_id"] == run["run_id"] and child["trigger"] == "parent"
    assert child["status"] == "succeeded"


async def test_governance_policies_gate_the_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """A resolved policy whose blocking check cannot run (no check runner is installed)
    fails the governance node, and the run with it."""
    await put_dataset(ctx, "sales", weekly_series(100))
    doc = _doc(
        [
            {
                "id": "fit",
                "use": "model.fit",
                "inputs": {"data": "inputs.data"},
                "with": {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0]}},
            },
            {"id": "gov", "use": "governance.policy", "inputs": {"model": "fit.model"}},
        ],
        name="governed",
    )
    wf = await _create(client, doc)
    policy = GovernancePolicy(
        id="pol_1",
        tenant_id="default",
        project_id=None,
        scope="tenant",
        version=1,
        name="all",
        checks=[PolicyCheck(check="convergence")],
        created_at=datetime.now(UTC),
    )
    seen: list[str] = []

    async def resolver(session: Any, ctx_: Any, mv: m.ModelVersion, pid: str | None) -> list[Any]:
        seen.append(mv.id)
        return [policy]

    set_policy_resolver(resolver)
    try:
        run = await _start(client, wf["id"])
        assert await drive(ctx, run["run_id"]) is WorkflowRunStatus.SUCCEEDED
    finally:
        set_policy_resolver(None)
    view = (await client.get(f"/v1/workflow-runs/{run['run_id']}")).json()
    nodes = {n["node_id"]: n for n in view["nodes"]}
    assert seen
    gov = nodes["gov"]
    assert gov["result"]["passed"] is False and gov["result"]["failed_checks"] == ["convergence"]
    ev = gov["outputs"]["evidence"]["inline"]
    assert ev["policies"] == ["pol_1@1"] and ev["results"][0]["status"] == "error"
    # the fit is an ordinary model version of the spec wf-<workflow>-<node>
    fit = nodes["fit"]
    assert fit["result"]["model_version"] == "model://default/wf-governed-fit@v1"
    spec = (await client.get("/v1/specs")).json()["items"]
    assert [s_["name"] for s_ in spec] == ["wf-governed-fit"]
    child = (await client.get(f"/v1/runs/{fit['run_id']}")).json()
    assert child["kind"] == "fit" and child["parent_run_id"] == run["run_id"]


async def test_the_canonical_readiness_to_arimax_template_through_the_server(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(156, random_walk=True, beta=3.0))
    r = await client.post("/v1/workflows", json={"template": TS_READINESS_ARIMAX.id})
    assert r.status_code == 201, r.text
    run = await _start(
        client,
        r.json()["id"],
        params={
            "drivers": ["price"],
            "n_folds": 4,
            "gap": 1,
            "horizons": [1, 4, 8],
            "max_p": 2,
            "max_q": 2,
        },
    )
    assert await drive(ctx, run["run_id"], max_rounds=80) is WorkflowRunStatus.SUCCEEDED
    view = (await client.get(f"/v1/workflow-runs/{run['run_id']}")).json()
    nodes = {n["node_id"]: n for n in view["nodes"]}
    assert nodes["wanders"]["result"]["arm"] == "then"
    assert nodes["sdiff"]["status"] == "skipped"
    assert nodes["backtest"]["result"]["n_folds"] == 4
    assert nodes["compare"]["result"]["best"] == "backtest"
    assert nodes["select"]["result"]["beats_baselines"] is True
    model_ref = view["outputs"]["model"]["ref"]
    assert model_ref.startswith("model://default/wf-ts-readiness-arimax-fit@v")
    assert nodes["governance"]["result"]["passed"] is True  # no policy store: none applies
    report = view["outputs"]["report"]["inline"]
    assert report["format"] == "mapi.report/v1" and report["model"] == model_ref
    mv = (await client.get("/v1/models")).json()["items"]
    assert any(i["name"] == "wf-ts-readiness-arimax-fit" for i in mv)


# ===================================================================== parents & routes


async def test_workflow_parent_runs_are_never_dispatched(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    wf = await _create(client, _doc(SMALL))
    run = await _start(client, wf["id"])
    executor = FakeExecutor()
    await Dispatcher(ctx, executor).dispatch_once()
    assert executor.submit_calls == 0
    parent = (await client.get(f"/v1/runs/{run['run_id']}")).json()
    assert parent["state"] == "queued"


def test_every_workflow_route_is_served() -> None:
    from mapi_server.app import openapi_document

    doc = openapi_document()
    served = {
        (method.upper(), path, op.get("operationId"))
        for path, item in doc["paths"].items()
        for method, op in item.items()
        if isinstance(op, dict)
    }
    mine = [e for e in M35_ENDPOINTS if "workflow" in e.path or "workflow" in e.operation_id]
    missing = [e.operation_id for e in mine if (e.method, e.path, e.operation_id) not in served]
    assert len(mine) == 28 and not missing, missing
    start = doc["paths"]["/v1/workflows/{workflow_id}/runs"]["post"]
    assert "409" in start["responses"] and "429" in start["responses"]


# ===================================================================== automation


async def test_schedules_fire_once_per_due_time(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    wf = await _create(client, _doc(SMALL))
    url = f"/v1/workflows/{wf['id']}/schedules"
    too_often = await client.post(url, json={"cron": "* * * * *", "inputs": {"data": DATA}})
    assert too_often.status_code == 422
    bad_tz = await client.post(url, json={"cron": "0 6 * * 1", "timezone": "Mars/Olympus"})
    assert bad_tz.status_code == 422
    r = await client.post(
        url,
        json={
            "cron": "0 6 * * 1",
            "timezone": "Europe/Berlin",
            "inputs": {"data": DATA},
            "description": "Mondays",
        },
    )
    assert r.status_code == 201, r.text
    sch = r.json()
    due = datetime.fromisoformat(sch["next_run_at"])
    assert due.astimezone(UTC).hour in (4, 5)  # 06:00 Berlin
    assert await fire_due_schedules(ctx, at=due - timedelta(minutes=1)) == 0
    assert await fire_due_schedules(ctx, at=due + timedelta(minutes=1)) == 1
    assert await fire_due_schedules(ctx, at=due + timedelta(minutes=2)) == 0
    runs = (await client.get(f"/v1/workflows/{wf['id']}/runs")).json()["items"]
    assert len(runs) == 1 and runs[0]["trigger"] == "schedule"
    assert runs[0]["trigger_id"] == sch["id"]
    listed = (await client.get(url)).json()["items"][0]
    assert listed["last_run_id"] == runs[0]["run_id"]
    assert datetime.fromisoformat(listed["next_run_at"]) > due
    paused = await client.patch(f"{url}/{sch['id']}", json={"active": False})
    assert paused.json()["active"] is False
    assert (await client.delete(f"{url}/{sch['id']}")).status_code == 204
    assert (await client.get(url)).json()["items"] == []


async def test_dataset_triggers_debounce_new_versions(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    wf = await _create(client, _doc(SMALL))
    url = f"/v1/workflows/{wf['id']}/triggers"
    wrong = await client.post(url, json={"dataset": "dataset://default/sales", "input": "nope"})
    assert wrong.status_code == 422
    r = await client.post(
        url, json={"dataset": "dataset://default/sales", "input": "data", "debounce_seconds": 60}
    )
    assert r.status_code == 201, r.text
    for n in (90, 100):  # two versions arrive within the window
        ds, dv = await put_dataset(ctx, "sales", weekly_series(n))
        async with ctx.db.session() as s:
            row = await s.get(m.Dataset, ds.id)
            ver = await s.get(m.DatasetVersion, (dv.dataset_id, dv.version))
            assert row is not None and ver is not None
            assert await dataset_version_ready(s, row, ver) == 1
    assert await fire_due_triggers(ctx) == 0  # still inside the debounce window
    assert await fire_due_triggers(ctx, at=now() + timedelta(seconds=61)) == 1
    runs = (await client.get(f"/v1/workflows/{wf['id']}/runs")).json()["items"]
    assert len(runs) == 1 and runs[0]["trigger"] == "dataset.version.created"
    assert runs[0]["inputs"]["data"] == "dataset://default/sales@v3"
    assert (await client.delete(f"{url}/{r.json()['id']}")).status_code == 204


async def test_finished_runs_emit_webhooks(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    async with ctx.db.session() as s:
        hook, _ = new_webhook(
            SecretBox(ctx.secrets.webhook_key),
            tenant_id="default",
            project_id="default",
            url="https://hooks.example.com/x",
            events=[WebhookEventType.WORKFLOW_RUN_SUCCEEDED, WebhookEventType.WORKFLOW_RUN_FAILED],
            description=None,
            active=True,
            created_by=None,
        )
        s.add(hook)
    wf = await _create(client, _doc(SMALL))
    run = await _start(client, wf["id"])
    assert await drive(ctx, run["run_id"]) is WorkflowRunStatus.SUCCEEDED
    async with ctx.db.session() as s:
        events = list((await s.execute(select(m.WebhookEvent))).scalars())
    wf_events = [e for e in events if e.type == "workflow.run.succeeded"]
    assert len(wf_events) == 1
    data = wf_events[0].envelope["data"]
    assert data["run_id"] == run["run_id"] and data["workflow"] == "workflow://default/checks@r1"


async def test_sse_streams_the_events_then_ends(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    wf = await _create(client, _doc(SMALL))
    run = await _start(client, wf["id"])
    assert await drive(ctx, run["run_id"]) is WorkflowRunStatus.SUCCEEDED
    url = f"/v1/workflow-runs/{run['run_id']}/events"
    r = await client.get(url, headers={"Accept": "text/event-stream"})
    text = r.text
    assert text.startswith("retry: ") and "event: snapshot" in text
    assert "event: workflow.run.started" in text and text.rstrip().endswith("}")
    assert "event: end" in text
    resumed = await client.get(url, headers={"Accept": "text/event-stream", "Last-Event-ID": "3"})
    assert "id: 3\n" not in resumed.text and "id: 4\n" in resumed.text


# ======================================================================== isolation


async def test_workflows_of_another_tenant_are_invisible(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from mapi_server_testkit.identity import grant_role, make_tenant, make_user, session_caller

    for tenant in ("alpha", "beta"):
        await make_tenant(ctx, tenant, projects=("main",))
        await put_dataset(ctx, "sales", weekly_series(60), tenant=tenant, project="main")
    callers = {}
    for tenant in ("alpha", "beta"):
        user = await make_user(ctx, f"ana@{tenant}.test")
        await grant_role(ctx, user, tenant, "main", "admin")
        callers[tenant] = await session_caller(
            ctx, client, user, tenant_id=tenant, project_id="main"
        )
    ids = {}
    for tenant, caller in callers.items():
        doc = _doc(SMALL)
        doc["metadata"]["project"] = "main"
        r = await caller.post("/v1/workflows", json={"document": doc})
        assert r.status_code == 201, r.text
        ids[tenant] = r.json()["id"]
        started = await caller.post(
            f"/v1/workflows/{ids[tenant]}/runs",
            json={"inputs": {"data": "dataset://main/sales@latest"}},
        )
        assert started.status_code == 202, started.text
        ids[tenant + "_run"] = started.json()["run_id"]
    alpha = callers["alpha"]
    assert (await alpha.get(f"/v1/workflows/{ids['beta']}")).status_code == 404
    assert (await alpha.get(f"/v1/workflow-runs/{ids['beta_run']}")).status_code == 404
    assert (await alpha.post(f"/v1/workflow-runs/{ids['beta_run']}:cancel")).status_code == 404
    listed = (await alpha.get("/v1/workflows")).json()["items"]
    assert [w["id"] for w in listed] == [ids["alpha"]]
    # row-level security: a session pinned to alpha sees none of beta's rows, in any table
    async with ctx.db.tenant_session("alpha") as s:
        for model in (m.Workflow, m.WorkflowRevisionRow, m.WorkflowRunRow, m.WorkflowNodeRunRow):
            tenants = set((await s.execute(select(model.tenant_id))).scalars())
            assert tenants == {"alpha"}, model.__tablename__
        rows = (
            await s.execute(
                select(func.count())
                .select_from(m.WorkflowRunRow)
                .where(m.WorkflowRunRow.run_id == ids["beta_run"])
            )
        ).scalar_one()
        assert rows == 0
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    with pytest.raises((DBAPIError, ProgrammingError)):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(
                m.Workflow(
                    id="wf_forged",
                    tenant_id="beta",
                    project_id="main",
                    name="x",
                    latest_revision=1,
                    archived=False,
                )
            )
            await s.flush()
    assert RunState
