"""The v0.5 Python client against the real server (embedded Postgres, the real routes and
services, child runs executed in-process): About cards and the chooser, and a workflow from
document to finished run with its event stream, nodes, outputs, schedules and triggers.
(Governance, validations, explorers and optimisation: ``test_client_m35_governance.py``.)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mapi_client import AsyncClient, WorkflowEnd, WorkflowEventFrame, WorkflowSnapshot
from mapi_server.context import ServerContext
from mapi_server.settings import Settings
from mapi_server_testkit.client import asgi_client
from mapi_server_testkit.workflows import catalog_file, drive, put_dataset
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.governance import GOVERNANCE_CHECKS
from mapi_spec.workflow import WorkflowRunStatus
from mapi_workflows.testing import weekly_series

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

DATA = "dataset://default/sales@latest"
SMALL: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "Workflow",
    "metadata": {"name": "readiness", "project": "default"},
    "params": {"alpha": {"type": "number", "default": 0.05}},
    "inputs": {"data": {"type": "dataset"}},
    "nodes": [
        {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
        {"id": "wanders", "use": "branch.if", "with": {"condition": "not nodes.adf.reject"}},
        {
            "id": "diff",
            "use": "transform.difference",
            "inputs": {"data": "inputs.data"},
            "when": "wanders.then",
        },
        {"id": "kpss", "use": "test.kpss", "inputs": {"data": ["diff.data", "inputs.data"]}},
        {
            "id": "report",
            "use": "report.summary",
            "inputs": {"tests": ["adf.result", "kpss.result"]},
        },
    ],
}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    """The suite's settings with the test registry's catalog (``ts.sarimax`` and friends)."""
    return settings.model_copy(update={"catalog_path": catalog_file(tmp_path)})


@pytest.fixture
async def api(ctx: ServerContext) -> AsyncIterator[AsyncClient]:
    async with asgi_client(ctx) as c:
        yield c


# ============================================================ About cards and the chooser


async def test_about_cards_and_help_me_choose(api: AsyncClient) -> None:
    models = [c async for c in api.catalog.abouts(kind="model")]
    assert "ts.sarimax" in {c.id for c in models}
    assert all(c.about.summary_plain and c.about.use_when for c in models)
    sarimax = await api.catalog.about("ts.sarimax", kind="model")
    assert sarimax.difficulty.value in ("beginner", "intermediate", "expert")
    questions = await api.catalog.chooser()
    assert [q.id for q in questions.questions][:2] == ["goal", "data_shape"]
    result = await api.catalog.recommend(
        goal="forecast", data_shape="one_series", experience="beginner", limit=5
    )
    assert result.items and "goal" in result.answered
    assert all(0 <= r.score <= 1 and r.about is not None for r in result.items)
    checks = await api.catalog.recommend(goal="validate_model", kind="check")
    assert {r.kind.value for r in checks.items} == {"check"}
    with pytest.raises(ValueError):
        await api.catalog.recommend(goal="teleport")
    nodes = [n async for n in api.catalog.workflow_nodes(kind="test")]
    assert "test.adf" in {n.id for n in nodes} and all(n.kind.value == "test" for n in nodes)
    assert (await api.catalog.workflow_node("search.arma_order")).about.title
    assert len([c async for c in api.governance.checks()]) == len(GOVERNANCE_CHECKS)


# ============================================================================= workflows


async def test_a_workflow_from_document_to_finished_run(
    api: AsyncClient, ctx: ServerContext, tmp_path: Path
) -> None:
    await put_dataset(ctx, "sales", weekly_series(120, random_walk=True))
    bad = {**SMALL, "nodes": [{"id": "adf", "use": "test.adf", "inputs": {"data": "x.data"}}]}
    check = await api.workflows.validate(bad)
    assert not check.valid and check.issues[0].pointer == "/nodes/0/inputs/data"
    path = tmp_path / "readiness.yaml"
    path.write_text(json.dumps(SMALL), "utf-8")  # JSON is YAML too
    assert (await api.workflows.validate(path)).order[0] == "adf"

    wf, rev = await api.workflows.upsert(path, message="first")
    assert wf.ref == "workflow://default/readiness" and rev.revision == 1
    again, same = await api.workflows.upsert(SMALL)
    assert again.id == wf.id and same.revision == 1  # unchanged: no new revision
    with pytest.raises(MapiError) as info:
        await api.workflows.revise(wf.id, SMALL, expected_revision=7)
    assert info.value.code is ErrorCode.CONFLICT

    run = await api.workflows.run(wf.id, inputs={"data": DATA}, params={"alpha": 0.1})
    assert run.status is WorkflowRunStatus.QUEUED
    assert run.inputs["data"] == "dataset://default/sales@v1"  # pinned at start
    assert await drive(ctx, run.run_id) is WorkflowRunStatus.SUCCEEDED
    done = await api.workflows.wait(run.run_id)
    nodes = {n.node_id: n for n in done.nodes}
    assert nodes["adf"].result is not None and done.decisions[0].arm == "then"

    items = [i async for i in api.workflows.stream_events(run.run_id)]
    assert isinstance(items[0], WorkflowSnapshot) and isinstance(items[-1], WorkflowEnd)
    assert items[-1].status is WorkflowRunStatus.SUCCEEDED
    frames = [i for i in items if isinstance(i, WorkflowEventFrame)]
    assert frames[0].type == "workflow.run.started"
    assert "workflow.branch.decided" in {f.type for f in frames}
    resumed = [i async for i in api.workflows.stream_events(run.run_id, last_event_id=3)]
    assert next(f.id for f in resumed if isinstance(f, WorkflowEventFrame)) == 4
    paged = [e async for e in api.workflows.events(run.run_id, limit=2)]
    assert [e.seq for e in paged] == [f.id for f in frames]

    succeeded = [n async for n in api.workflows.nodes(run.run_id, status="succeeded")]
    assert {n.node_id for n in succeeded} == set(nodes)
    blob = await api.workflows.download_output(run.run_id, "diff", "data")
    assert blob[:2] == b"PK"  # the differenced series: a real, verified zarr zip
    with pytest.raises(MapiError) as info:
        await api.workflows.output_link(run.run_id, "adf", "result")  # inline JSON, no file
    assert info.value.code is ErrorCode.NOT_FOUND
    assert [r.run_id async for r in api.workflows.runs(wf.id)] == [run.run_id]


async def test_templates_schedules_triggers_and_cleanup(api: AsyncClient) -> None:
    gallery = [t async for t in api.workflows.templates()]
    assert {t.id for t in gallery} >= {"ts_readiness_arimax", "forecast_bakeoff"}
    wf = await api.workflows.create(template="ts_readiness_arimax")
    rev = await api.workflows.revision(wf.id, 1)
    assert rev.validation.valid, rev.validation.issues
    sched = await api.workflows.schedule(
        wf.id, "0 6 * * 1", timezone="Europe/Berlin", inputs={"data": DATA}
    )
    assert sched.next_run_at is not None and sched.active
    paused = await api.workflows.update_schedule(wf.id, sched.id, active=False)
    assert not paused.active
    with pytest.raises(ValueError, match="15 minutes"):  # refused before it is sent
        await api.workflows.schedule(wf.id, "* * * * *")
    with pytest.raises(ValueError, match="unknown time zone"):
        await api.workflows.schedule(wf.id, "0 6 * * 1", timezone="Mars/Olympus_Mons")
    trig = await api.workflows.trigger(
        wf.id, "dataset://default/sales", "data", debounce_seconds=600
    )
    assert [t.id async for t in api.workflows.triggers(wf.id)] == [trig.id]
    await api.workflows.delete_trigger(wf.id, trig.id)
    await api.workflows.delete_schedule(wf.id, sched.id)
    assert [s async for s in api.workflows.schedules(wf.id)] == []
    archived = await api.workflows.update(wf.id, archived=True, description="parked")
    assert archived.archived and archived.description == "parked"
    assert (await api.workflows.find(wf.name)) is not None
    await api.workflows.delete(wf.id)
    assert (await api.workflows.find(wf.name)) is None
