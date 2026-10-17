"""A workflow's ``governance.policy`` node with ``submit: true`` (M3.5 review).

The validation it records is held to every policy in force (a node naming a lenient policy
does not drop the tenant floor), it is submitted by the person who started the workflow
(so that person cannot approve it), and a validation nobody submitted cannot be approved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.settings import Settings
from mapi_server_testkit.identity import grant_role, make_user, session_caller
from mapi_server_testkit.workflows import catalog_file, drive, put_dataset
from mapi_spec.governance import SYSTEM_ACTOR
from mapi_spec.workflow import WorkflowRunStatus
from mapi_workflows.testing import weekly_series
from sqlalchemy import update

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

DATA = "dataset://default/sales@latest"
TENANT_POL = "/v1/tenants/default/governance/policies"
POL = "/v1/governance/policies"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"catalog_path": catalog_file(tmp_path)})


def _governed(policy: str | None = None) -> dict[str, Any]:
    gov: dict[str, Any] = {"submit": True}
    if policy is not None:
        gov["policy"] = policy
    return {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": "governed", "project": "default"},
        "inputs": {"data": {"type": "dataset"}},
        "nodes": [
            {
                "id": "fit",
                "use": "model.fit",
                "inputs": {"data": "inputs.data"},
                "with": {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0]}},
            },
            {
                "id": "gov",
                "use": "governance.policy",
                "inputs": {"model": "fit.model", "data": "inputs.data"},
                "with": gov,
            },
        ],
    }


async def _run_governed(
    ctx: ServerContext, caller: httpx.AsyncClient, policy: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    wf = await caller.post("/v1/workflows", json={"document": _governed(policy)})
    assert wf.status_code == 201, wf.text
    run = await caller.post(
        f"/v1/workflows/{wf.json()['id']}/runs", json={"inputs": {"data": DATA}}
    )
    assert run.status_code == 202, run.text
    assert await drive(ctx, run.json()["run_id"]) is WorkflowRunStatus.SUCCEEDED
    view = (await caller.get(f"/v1/workflow-runs/{run.json()['run_id']}")).json()
    gov = {n["node_id"]: n for n in view["nodes"]}["gov"]
    vid = gov["result"].get("validation_id")
    assert vid, gov
    return view, (await caller.get(f"/v1/validations/{vid}")).json()


async def test_a_node_naming_a_lenient_policy_keeps_the_tenant_floor(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(100))
    floor = await client.post(
        TENANT_POL,
        json={
            "name": "floor",
            "applies_to": {"families": ["forecast"]},
            "checks": [{"check": "convergence"}],
            "require_approval": True,
            "approvals_required": 2,
            "require_validated_for_promotion": True,
        },
    )
    assert floor.status_code == 201, floor.text
    lenient = await client.post(POL, json={"name": "lenient", "checks": [{"check": "convergence"}]})
    assert lenient.status_code == 201, lenient.text
    view, val = await _run_governed(ctx, client, lenient.json()["id"])
    assert f"{floor.json()['id']}@1" in val["policies"]
    assert val["require_approval"] is True and val["approvals_required"] == 2
    assert val["status"] == "awaiting_approval"
    assert val["submitted_by"] == view["created_by"] and val["submitted_by"] != SYSTEM_ACTOR
    r = await client.put(
        "/v1/endpoints/fc-prod", json={"model": "model://default/wf-governed-fit@v1"}
    )
    assert r.status_code == 409 and r.json()["code"] == "NOT_VALIDATED", r.text


async def test_the_person_who_started_the_workflow_cannot_approve(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from mapi_server.db import models as m

    await put_dataset(ctx, "sales", weekly_series(100))
    rita = await make_user(ctx, "rita@default.test")
    await grant_role(ctx, rita, "default", "default", "runner")
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == rita.id)
            .values(capabilities=["reviewer"])
        )
    me = await session_caller(ctx, client, rita, tenant_id="default", project_id="default")
    r = await client.post(
        POL,
        json={
            "name": "Forecasts converge, reviewed",
            "applies_to": {"families": ["forecast"]},
            "checks": [{"check": "convergence"}],
            "require_approval": True,
            "require_validated_for_promotion": True,
        },
    )
    assert r.status_code == 201, r.text
    view, val = await _run_governed(ctx, me)
    assert val["status"] == "awaiting_approval"
    assert val["submitted_by"] == rita.id == view["created_by"]
    own = await me.post(
        f"/v1/validations/{val['id']}/approvals", json={"decision": "approve", "comment": "mine"}
    )
    assert own.status_code == 403 and own.json()["code"] == "SELF_APPROVAL_FORBIDDEN", own.text
