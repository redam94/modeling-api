"""The requests the M3.5 web screens send, against the real server: a workflow created by the
template wizard (renamed, with parameter and input defaults), the condition issue the branch
editor pins to its field, and the run the run dialog starts (`apps/web/src/features/workflows`)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.settings import Settings
from mapi_server_testkit.workflows import catalog_file, put_dataset
from mapi_spec.workflow_templates import TS_READINESS_ARIMAX
from mapi_workflows.testing import weekly_series

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"catalog_path": catalog_file(tmp_path)})


def _wizard_document() -> dict[str, Any]:
    """What `instantiate()` in `features/workflows/templates.tsx` sends."""
    doc: dict[str, Any] = copy.deepcopy(dict(TS_READINESS_ARIMAX.document))
    doc["metadata"] = {
        **doc["metadata"],
        "name": "sales-readiness",
        "project": "default",
        "labels": {"workflow-template": TS_READINESS_ARIMAX.id},
    }
    doc["params"]["max_p"]["default"] = 2
    doc["inputs"]["data"]["default"] = "dataset://default/sales@latest"
    return doc


async def test_the_wizard_document_is_a_runnable_workflow(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/workflows",
        json={
            "document": _wizard_document(),
            "message": "From the gallery template ts_readiness_arimax",
        },
    )
    assert r.status_code == 201, r.text
    wf = r.json()
    rev = (await client.get(f"/v1/workflows/{wf['id']}/revisions/1")).json()
    assert rev["validation"]["valid"] is True, rev["validation"]["issues"]
    assert rev["document"]["metadata"]["labels"]["workflow-template"] == "ts_readiness_arimax"


async def test_a_bad_condition_is_reported_at_its_field(client: httpx.AsyncClient) -> None:
    doc = _wizard_document()
    branch = next(i for i, n in enumerate(doc["nodes"]) if n["id"] == "wanders")
    doc["nodes"][branch]["with"] = {"condition": "nodes.adf.p_valu < 0.05"}
    v = (await client.post("/v1/workflows:validate", json={"document": doc})).json()
    assert v["valid"] is False
    issue = next(i for i in v["issues"] if i["pointer"] == f"/nodes/{branch}/with/condition")
    assert issue["code"] == "EXPRESSION_INVALID"
    assert "p_valu" in issue["message"]


async def test_the_run_dialog_body_is_accepted(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(80))
    wf = (await client.post("/v1/workflows", json={"document": _wizard_document()})).json()
    # The dialog sends the revision, the run's parameters, the bound inputs (none here: the
    # input's default reference applies) and `use_cache`, with an Idempotency-Key.
    body = {"revision": 1, "params": {"n_folds": 4}, "inputs": {}, "use_cache": True}
    key = {"Idempotency-Key": "9d3c1c56-5c1f-4c0b-9f31-0b2c9b8a0e11"}
    r = await client.post(f"/v1/workflows/{wf['id']}/runs", json=body, headers=key)
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["params"]["n_folds"] == 4 and run["params"]["max_p"] == 2
    assert run["inputs"]["data"].startswith("dataset://default/sales@v")
    again = await client.post(f"/v1/workflows/{wf['id']}/runs", json=body, headers=key)
    assert again.status_code == 200 and again.json()["run_id"] == run["run_id"]
