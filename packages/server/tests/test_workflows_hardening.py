"""Workflow orchestration hardening (M3.5 review): fail-fast, cancellation, searches,
poisoned runs, fan-out limits, automation, cost roll-ups and route races."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.services.runs import apply_transition, load_run
from mapi_server.services.workflows import WorkflowOrchestrator
from mapi_server.settings import Settings
from mapi_server_testkit.workflows import catalog_file, put_dataset
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_workflows.testing import weekly_series

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
