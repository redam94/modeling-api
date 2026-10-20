"""Experiments (v0.14) through workflows: sub-workflow runs and node runs take the experiment
of the run that started them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.settings import Settings
from mapi_server_testkit.workflows import catalog_file, drive, put_dataset
from mapi_spec.workflow import WorkflowRunStatus
from mapi_workflows.testing import weekly_series

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    """The suite's settings with the test registry's catalog (``test.adf`` and friends)."""
    return settings.model_copy(update={"catalog_path": catalog_file(tmp_path)})


async def test_workflow_children_join_the_workflow_runs_experiment(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """Sub-workflow runs and node runs take the experiment of the run that started them."""
    await put_dataset(ctx, "sales", weekly_series(100))

    def doc(nodes: list[dict[str, Any]], name: str, **extra: Any) -> dict[str, Any]:
        return {
            "apiVersion": "mapi/v1",
            "kind": "Workflow",
            "metadata": {"name": name, "project": "default"},
            "inputs": {"data": {"type": "dataset"}},
            "nodes": nodes,
            **extra,
        }

    inner = doc(
        [{"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}}],
        "inner",
        outputs={"adf": "adf.result"},
    )
    outer = doc(
        [
            {
                "id": "sub",
                "use": "subworkflow.run",
                "inputs": {"data": "inputs.data"},
                "with": {"workflow": "workflow://default/inner@latest"},
            }
        ],
        "outer",
    )
    for d in (inner, outer):
        r = await client.post("/v1/workflows", json={"document": d})
        assert r.status_code == 201, r.text
    wf_id = r.json()["id"]
    r = await client.post("/v1/experiments", json={"name": "workflows"})
    assert r.status_code == 201, r.text
    exp = r.json()
    body = {"inputs": {"data": "dataset://default/sales@latest"}}
    # an unknown or archived experiment refuses the start, as it refuses any new run
    r = await client.post(
        f"/v1/workflows/{wf_id}/runs", json={**body, "experiment_id": "exp_unknown"}
    )
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID", r.text
    other = (await client.post("/v1/experiments", json={"name": "closed"})).json()
    await client.patch(f"/v1/experiments/{other['id']}", json={"archived": True})
    r = await client.post(
        f"/v1/workflows/{wf_id}/runs", json={**body, "experiment_id": other["id"]}
    )
    assert r.status_code == 409, r.text
    # named at start (no PATCH racing the orchestrator): every node run joins it
    start = await client.post(
        f"/v1/workflows/{wf_id}/runs", json={**body, "experiment_id": exp["id"]}
    )
    assert start.status_code == 202, start.text
    top = start.json()["run_id"]
    assert (await client.get(f"/v1/runs/{top}")).json()["experiment_id"] == exp["id"]
    assert await drive(ctx, top) is WorkflowRunStatus.SUCCEEDED
    listed = (await client.get(f"/v1/runs?experiment_id={exp['id']}&limit=50")).json()["items"]
    kinds = sorted(x["kind"] for x in listed)
    assert kinds == ["workflow", "workflow", "workflow_node"], kinds
    assert (await client.get(f"/v1/experiments/{exp['id']}")).json()["run_count"] == 3
