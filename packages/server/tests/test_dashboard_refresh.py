"""Dashboard refreshes and refit requests (v0.16, plan D12 and §10.4).

Refreshes never refit: a manual refresh queues a score of the pinned version (debounced),
a linked workflow's success appends a readout from the run of its score node (no new run),
and an endpoint promotion either publishes a new revision (``follow_endpoint``) or flags the
dashboard for the analyst. A refit happens only when an editor with runner rights approves a
refit request made with an actuals upload: the numbers become a new dataset version and the
refit workflow starts as the approver once that version is ready.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Final

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import refits
from mapi_server.services.dashboards.inputs import clear_grid_cache
from mapi_server.services.dashboards.loop import DashboardLoop
from mapi_server.services.dashboards.readouts import on_workflow_succeeded
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller
from mapi_server_testkit.integrations import refit_version
from mapi_spec.api import new_id
from mapi_spec.dashboards import SHARE_SESSION_HEADER
from mapi_spec.xrjson import XrJsonDataset
from sqlalchemy import func, select, update
from test_dashboards_crud import GRID, create_published, dashboard_kit, dashboard_spec

pytestmark = pytest.mark.integration

WORKFLOW = {
    "apiVersion": "mapi/v1",
    "kind": "Workflow",
    "metadata": {"name": "refit", "project": "default"},
    "inputs": {"data": {"type": "dataset", "default": "dataset://default/co2@latest"}},
    "nodes": [{"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}}],
}
UPLOAD = {
    "inputs": {
        "coords": {"t": {"dims": ["t"], "data": ["2024-01-01", "2024-02-01"]}},
        "data_vars": {"y": {"dims": ["t"], "data": [410.0, 412.0]}},
    }
}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    clear_grid_cache()
    kit = await dashboard_kit(ctx, client)
    r = await kit["ana"].post("/v1/workflows", json={"document": WORKFLOW})
    assert r.status_code == 201, r.text
    kit["wf_id"] = r.json()["id"]
    return kit


async def _count(ctx: ServerContext, model: Any, *where: Any) -> int:
    async with ctx.db.session() as s:
        stmt = select(func.count()).select_from(model)
        for w in where:
            stmt = stmt.where(w)
        return int((await s.execute(stmt)).scalar_one())


def _refit_spec(kit: dict[str, Any]) -> dict[str, Any]:
    return {
        "actuals": {
            "enabled": True,
            "refit_workflow_id": kit["wf_id"],
            "allow_refit_requests": True,
        }
    }


# ==================================================================================== refresh


async def test_a_manual_refresh_is_queued_debounced_and_editor_only(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await create_published(kit)
    url = f"/v1/dashboards/{dash_id}:refresh"
    r = await kit["eve"].post(url)
    assert r.status_code == 202, r.text
    head = r.json()
    assert (head["kind"], head["trigger"], head["status"]) == ("refresh", "manual", "queued")
    again = await kit["eve"].post(url)
    assert again.json()["seq"] == head["seq"]  # within the debounce window
    assert (await kit["cleo"].post(url)).status_code == 404  # not a client operation
    assert (await kit["vic"].post(url)).status_code == 403
    await DashboardLoop(ctx).submit_queued()
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (dash_id, head["seq"]))
        assert row is not None and row.status == "running"
        run = await s.get(m.Run, row.run_ids[0])
        assert run is not None and run.created_by == kit["users"]["ana"].id
        assert run.labels["mapi.trigger"] == "manual"


async def test_a_linked_workflow_success_appends_a_readout_without_a_new_run(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await create_published(kit, refresh={"workflow_id": kit["wf_id"]})
    r = await kit["ana"].post(
        f"/v1/workflows/{kit['wf_id']}/runs",
        json={"inputs": {"data": "dataset://default/co2@latest"}},
    )
    assert r.status_code == 202, r.text
    wr_id = r.json()["run_id"]
    async with ctx.db.session() as s:
        wr = await s.get(m.WorkflowRunRow, wr_id)
        assert wr is not None
        score = m.Run(
            id=new_id("run"), tenant_id=wr.tenant_id, project_id=wr.project_id, kind="score",
            state="succeeded", attempt=1, resource_class="S", timeout_s=60,
            model_version_id=kit["model"]["mv_id"], labels={}, payload={}, inputs={},
            created_at=now(), updated_at=now(),
        )  # fmt: skip
        s.add(score)
        await s.flush()
        s.add(
            m.WorkflowNodeRunRow(
                run_id=wr_id, tenant_id=wr.tenant_id, project_id=wr.project_id, node_id="score",
                position=1, use="model.score@1", kind="model", status="succeeded",
                child_run_id=score.id,
            )
        )  # fmt: skip
        score_id = score.id
    runs = await _count(ctx, m.Run)
    async with ctx.db.session() as s:
        wr = await s.get(m.WorkflowRunRow, wr_id)
        assert wr is not None
        assert await on_workflow_succeeded(s, wr) == 1
        assert await on_workflow_succeeded(s, wr) == 0  # once per workflow run
    assert await _count(ctx, m.Run) == runs
    async with ctx.db.session() as s:
        row = (
            await s.execute(
                select(m.DashboardReadout).where(
                    m.DashboardReadout.dashboard_id == dash_id,
                    m.DashboardReadout.kind == "refresh",
                )
            )
        ).scalar_one()
        assert (row.trigger, row.status, row.visibility) == (
            "workflow",
            "materialising",
            "published",
        )
        assert row.run_ids == [score_id] and row.workflow_run_id == wr_id


async def _endpoint_dashboard(kit: dict[str, Any], follow: bool) -> str:
    ana: Caller = kit["ana"]
    r = await ana.put("/v1/endpoints/trend-prod", json={"model": "model://default/trend@v1"})
    assert r.status_code == 200, r.text
    return await create_published(
        kit,
        name=f"ep-{follow}",
        source={"kind": "endpoint", "alias": "trend-prod"},
        refresh={"follow_endpoint": follow},
    )


_GRID_OBJECT: Final = {
    "ui/grids/response_curves.json": (json.dumps(GRID).encode(), "application/json")
}


async def _promote(kit: dict[str, Any], version: int) -> None:
    r = await kit["ana"].put(
        "/v1/endpoints/trend-prod", json={"model": f"model://default/trend@v{version}"}
    )
    assert r.status_code == 200, r.text


async def _version(ctx: ServerContext, mv_id: str) -> m.ModelVersion:
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, mv_id)
        assert mv is not None
        return mv


@pytest.mark.parametrize("follow", [True, False])
async def test_an_endpoint_promotion_follows_or_flags(
    kit: dict[str, Any], ctx: ServerContext, follow: bool
) -> None:
    dash_id = await _endpoint_dashboard(kit, follow)
    v2 = await refit_version(ctx, await _version(ctx, kit["model"]["mv_id"]), add=_GRID_OBJECT)
    await _promote(kit, v2.version)
    body = (await kit["ana"].get(f"/v1/dashboards/{dash_id}")).json()
    revs = (await kit["ana"].get(f"/v1/dashboards/{dash_id}/revisions")).json()["items"]
    if follow:
        assert body["published_revision"]["model_version_id"] == v2.id
        assert body["published_revision"]["endpoint_revision"] == 2 and len(revs) == 2
        assert body["latest_readout"]["kind"] == "refresh"
        assert body["latest_readout"]["trigger"] == "endpoint"
        assert body["latest_readout"]["model_version_id"] == v2.id
        assert body["problem"] is None
    else:
        assert body["published_revision"]["model_version_id"] == kit["model"]["mv_id"]
        assert len(revs) == 1 and body["problem"] == "new_version_available"
        assert body["latest_readout"]["kind"] == "baseline"


async def _refreshes(ctx: ServerContext, dash_id: str) -> list[m.DashboardReadout]:
    async with ctx.db.session() as s:
        return list(
            (
                await s.execute(
                    select(m.DashboardReadout)
                    .where(
                        m.DashboardReadout.dashboard_id == dash_id,
                        m.DashboardReadout.kind == "refresh",
                    )
                    .order_by(m.DashboardReadout.seq)
                )
            ).scalars()
        )


async def test_a_version_the_spec_does_not_fit_is_not_followed(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    """Publishing checks the grid, the KPI and the input contract; following does too."""
    dash_id = await _endpoint_dashboard(kit, True)
    v2 = await refit_version(ctx, await _version(ctx, kit["model"]["mv_id"]))  # no grid
    await _promote(kit, v2.version)
    body = (await kit["ana"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["published_revision"]["model_version_id"] == kit["model"]["mv_id"]
    assert body["problem"] == "new_version_incompatible"
    assert await _refreshes(ctx, dash_id) == []


async def test_a_quick_rollback_retargets_the_queued_refresh(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await _endpoint_dashboard(kit, True)
    v1 = await _version(ctx, kit["model"]["mv_id"])
    v2 = await refit_version(ctx, v1, add=_GRID_OBJECT)
    await _promote(kit, v2.version)
    (queued,) = await _refreshes(ctx, dash_id)
    assert queued.model_version_id == v2.id and queued.status == "queued"
    await _promote(kit, v1.version)  # rolled back within the debounce window
    (row,) = await _refreshes(ctx, dash_id)  # coalesced: still one refresh
    body = (await kit["ana"].get(f"/v1/dashboards/{dash_id}")).json()
    assert row.seq == queued.seq and row.model_version_id == v1.id
    assert row.revision_id == body["published_revision"]["id"]
    # a refresh that already runs for v2 is not reused for the next promotion back to v2
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dash_id, m.DashboardReadout.seq == row.seq)
            .values(status="running")
        )
    await _promote(kit, v2.version)
    rows = await _refreshes(ctx, dash_id)
    assert [r.model_version_id for r in rows] == [v1.id, v2.id]
    # a failed refresh never suppresses the next one
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dash_id)
            .values(status="failed")
        )
    await _promote(kit, v1.version)
    assert [r.model_version_id for r in await _refreshes(ctx, dash_id)] == [v1.id, v2.id, v1.id]


async def _linked_run(kit: dict[str, Any], ctx: ServerContext, mv_id: str) -> str:
    """A run of the linked workflow whose score node scored ``mv_id``."""
    r = await kit["ana"].post(
        f"/v1/workflows/{kit['wf_id']}/runs",
        json={"inputs": {"data": "dataset://default/co2@latest"}},
    )
    assert r.status_code == 202, r.text
    wr_id = str(r.json()["run_id"])
    async with ctx.db.session() as s:
        score = m.Run(
            id=new_id("run"), tenant_id="default", project_id="default", kind="score",
            state="succeeded", attempt=1, resource_class="S", timeout_s=60,
            model_version_id=mv_id, labels={}, payload={}, inputs={},
            created_at=now(), updated_at=now(),
        )  # fmt: skip
        s.add(score)
        await s.flush()
        s.add(
            m.WorkflowNodeRunRow(
                run_id=wr_id, tenant_id="default", project_id="default", node_id="score",
                position=1, use="model.score@1", kind="model", status="succeeded",
                child_run_id=score.id,
            )
        )  # fmt: skip
    return wr_id


async def test_a_linked_workflow_never_publishes_a_version_that_ran_uploaded_code(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await create_published(kit, refresh={"workflow_id": kit["wf_id"]})
    v2 = await refit_version(ctx, await _version(ctx, kit["model"]["mv_id"]))
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, v2.id)
        assert mv is not None
        mv.lineage = {**(mv.lineage or {}), "overlay_hash": "sha256:" + "1" * 64}
    wr_id = await _linked_run(kit, ctx, v2.id)
    async with ctx.db.session() as s:
        wr = await s.get(m.WorkflowRunRow, wr_id)
        assert wr is not None
        assert await on_workflow_succeeded(s, wr) == 0
    assert await _refreshes(ctx, dash_id) == []
    body = (await kit["ana"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["problem"] == "workflow_version_refused"
    # another model's version is refused the same way
    other = await refit_version(ctx, await _version(ctx, kit["model"]["mv_id"]))
    async with ctx.db.session() as s:
        s.add(m.Model(id=new_id("mdl"), tenant_id="default", project_id="default",
                      name="other", latest_version=1, next_version=2))  # fmt: skip
        await s.flush()
        await s.execute(
            update(m.ModelVersion)
            .where(m.ModelVersion.id == other.id)
            .values(model_id=select(m.Model.id).where(m.Model.name == "other").scalar_subquery())
        )
    wr_id = await _linked_run(kit, ctx, other.id)
    async with ctx.db.session() as s:
        wr = await s.get(m.WorkflowRunRow, wr_id)
        assert wr is not None
        assert await on_workflow_succeeded(s, wr) == 0
    # a plain new version of the dashboard's model is published
    v3 = await refit_version(ctx, await _version(ctx, kit["model"]["mv_id"]))
    wr_id = await _linked_run(kit, ctx, v3.id)
    async with ctx.db.session() as s:
        wr = await s.get(m.WorkflowRunRow, wr_id)
        assert wr is not None
        assert await on_workflow_succeeded(s, wr) == 1


# ============================================================================ refit requests


async def test_a_refit_request_needs_the_dashboard_to_allow_it(kit: dict[str, Any]) -> None:
    plain = await create_published(kit, actuals={"enabled": True})
    r = await kit["cleo"].post(
        f"/v1/dashboards/{plain}/actuals", json={**UPLOAD, "request_refit": True}
    )
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_INPUT_DISABLED", r.text


async def _request(kit: dict[str, Any], dash_id: str, caller: str = "cleo", ys: Any = None) -> int:
    body = {**UPLOAD, "request_refit": True}
    if ys is not None:
        body = {"inputs": {**UPLOAD["inputs"], "data_vars": {"y": {"dims": ["t"], "data": ys}}},
                "request_refit": True}  # fmt: skip
    r = await kit[caller].post(f"/v1/dashboards/{dash_id}/actuals", json=body)
    assert r.status_code == 202, r.text
    assert r.json()["refit_status"] == "open"
    return int(r.json()["seq"])


async def test_a_newer_request_supersedes_the_open_one(kit: dict[str, Any]) -> None:
    dash_id = await create_published(kit, **_refit_spec(kit))
    first = await _request(kit, dash_id)
    second = await _request(kit, dash_id, ys=[400.0, 401.0])
    page = (await kit["eve"].get(f"/v1/dashboards/{dash_id}/refit-requests")).json()["items"]
    by_seq = {i["readout_seq"]: i for i in page}
    assert by_seq[first]["status"] == "superseded" and by_seq[second]["status"] == "open"
    assert by_seq[second]["requested_by_kind"] == "client"
    assert by_seq[second]["requested_by"] == kit["users"]["cleo"].id
    head = (await kit["cleo"].get(f"/v1/dashboards/{dash_id}/readouts/{first}")).json()
    assert head["refit_status"] == "superseded"
    open_ = (
        await kit["eve"].get(f"/v1/dashboards/{dash_id}/refit-requests", params={"status": "open"})
    ).json()["items"]
    assert [i["readout_seq"] for i in open_] == [second]


async def test_concurrent_requests_on_a_cached_upload_keep_one_open(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    """A double submit of the same numbers hits the uploader's own ready readout (no new
    readout, so no lock from it): the request itself locks the dashboard, so the partial
    unique index never fails the upload with a 500."""
    dash_id = await create_published(kit, **_refit_spec(kit))
    seq = await _request(kit, dash_id)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dash_id, m.DashboardReadout.seq == seq)
            .values(status="ready", ready_at=now(), snapshot={"lead": "Done."})
        )
    body = {**UPLOAD, "request_refit": True}
    url = f"/v1/dashboards/{dash_id}/actuals"
    answers = await asyncio.gather(*(kit["cleo"].post(url, json=body) for _ in range(4)))
    assert all(r.status_code < 300 for r in answers), [r.text for r in answers]
    assert {r.json()["seq"] for r in answers} == {seq}
    assert await _count(
        ctx, m.DashboardRefitRequest,
        m.DashboardRefitRequest.dashboard_id == dash_id, m.DashboardRefitRequest.status == "open",
    ) == 1  # fmt: skip


async def test_only_an_editor_with_runner_rights_decides(kit: dict[str, Any]) -> None:
    dash_id = await create_published(kit, **_refit_spec(kit))
    await _request(kit, dash_id)
    (req,) = (await kit["ana"].get(f"/v1/dashboards/{dash_id}/refit-requests")).json()["items"]
    url = f"/v1/dashboards/{dash_id}/refit-requests/{req['id']}"
    assert (await kit["cleo"].post(f"{url}:approve", json={})).status_code == 404
    assert (await kit["cleo"].get(f"/v1/dashboards/{dash_id}/refit-requests")).status_code == 404
    r = await kit["eve"].post(f"{url}:approve", json={})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"


async def test_a_rejected_request_starts_nothing(kit: dict[str, Any], ctx: ServerContext) -> None:
    dash_id = await create_published(kit, **_refit_spec(kit))
    await _request(kit, dash_id)
    (req,) = (await kit["eve"].get(f"/v1/dashboards/{dash_id}/refit-requests")).json()["items"]
    versions = await _count(ctx, m.DatasetVersion)
    runs = await _count(ctx, m.Run)
    r = await kit["eve"].post(
        f"/v1/dashboards/{dash_id}/refit-requests/{req['id']}:reject", json={"note": "Not now."}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected" and r.json()["decided_by"] == kit["users"]["eve"].id
    assert await _count(ctx, m.DatasetVersion) == versions and await _count(ctx, m.Run) == runs
    await DashboardLoop(ctx).once()
    assert await _count(ctx, m.WorkflowRunRow) == 0
    again = await kit["ana"].post(
        f"/v1/dashboards/{dash_id}/refit-requests/{req['id']}:approve", json={}
    )
    assert again.status_code == 409


async def test_approval_ingests_the_numbers_then_starts_the_workflow_as_the_approver(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await create_published(kit, **_refit_spec(kit))
    seq = await _request(kit, dash_id)
    (req,) = (await kit["ana"].get(f"/v1/dashboards/{dash_id}/refit-requests")).json()["items"]
    r = await kit["ana"].post(
        f"/v1/dashboards/{dash_id}/refit-requests/{req['id']}:approve",
        json={"note": "Refit with the January numbers."},
    )
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "approved" and done["decided_by"] == kit["users"]["ana"].id
    assert (
        done["dataset_version_id"] == "dataset://default/co2@v2" and done["workflow_run_id"] is None
    )
    # the readout entered the history
    head = await kit["vic"].get(f"/v1/dashboards/{dash_id}/readouts/{seq}")
    assert head.json()["visibility"] == "published"
    async with ctx.db.session() as s:
        ds = (await s.execute(select(m.Dataset).where(m.Dataset.name == "co2"))).scalar_one()
        dv = await s.get(m.DatasetVersion, (ds.id, 2))
        assert dv is not None and dv.status == "ingesting" and dv.ingest_run_id
        ingest = await s.get(m.Run, dv.ingest_run_id)
        assert ingest is not None and ingest.kind == "ingest"
        upload = await s.get(m.Upload, dv.upload_id)
        assert upload is not None
        csv = (await ctx.store.get(upload.bucket, upload.object_name)).decode()
        assert csv.splitlines() == ["t,y", "2024-01-01,410.0", "2024-02-01,412.0"]
        assert dv.mapping is not None and dv.mapping["vars"]["y"]["role"] == "target"
    # nothing starts before the version is ready
    await DashboardLoop(ctx).once()
    assert await _count(ctx, m.WorkflowRunRow) == 0
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DatasetVersion)
            .where(m.DatasetVersion.dataset_id == ds.id, m.DatasetVersion.version == 2)
            .values(status="ready", sha256="sha256:" + "2" * 64, ready_at=now())
        )
    assert await refits.advance(ctx) == 1
    async with ctx.db.session() as s:
        wr = (await s.execute(select(m.WorkflowRunRow))).scalar_one()
        assert wr.workflow_id == kit["wf_id"] and wr.created_by == kit["users"]["ana"].id
        assert wr.inputs == {"data": "dataset://default/co2@v2"}
        row = await s.get(m.DashboardRefitRequest, req["id"])
        assert row is not None and row.workflow_run_id == wr.run_id
        # the workflow's success refreshes the dashboard through the usual path
        assert await on_workflow_succeeded(s, wr) == 0  # (no score node in this workflow)


async def test_share_links_ask_for_a_refit_only_with_uploads(
    kit: dict[str, Any], client: httpx.AsyncClient
) -> None:
    dash_id = await create_published(kit, **_refit_spec(kit))
    for allowed, want in (([], 409), (["actuals"], 202)):
        r = await kit["ana"].post(
            f"/v1/dashboards/{dash_id}/shares", json={"allowed_inputs": allowed}
        )
        token = r.json()["token"]
        session = (await client.post("/v1/share-sessions", json={"token": token})).json()
        headers = {SHARE_SESSION_HEADER: session["session"]}
        assert session["dashboard"]["inputs"]["refit_requests"] is bool(allowed)
        r = await client.post(
            "/v1/shared/actuals", json={**UPLOAD, "request_refit": True}, headers=headers
        )
        assert r.status_code == want, r.text
    page = (await kit["eve"].get(f"/v1/dashboards/{dash_id}/refit-requests")).json()["items"]
    assert [i["requested_by_kind"] for i in page] == ["share"]


def test_an_upload_becomes_a_dense_csv() -> None:
    payload = XrJsonDataset.model_validate(
        {
            "coords": {"t": {"dims": ["t"], "data": ["2024-01-01", "2024-02-01"]},
                       "geo": {"dims": ["geo"], "data": ["de", "fr"]}},
            "data_vars": {"y": {"dims": ["t", "geo"], "data": [[1.0, 2.0], [3.0, None]]},
                          "price": {"dims": ["t"], "data": [9.5, 9.0]}},
        }
    )  # fmt: skip
    text = refits.upload_csv(payload, ["t", "geo"]).decode()
    assert text.splitlines() == [
        "t,geo,y,price",
        "2024-01-01,de,1.0,9.5",
        "2024-01-01,fr,2.0,9.5",
        "2024-02-01,de,3.0,9.0",
        "2024-02-01,fr,,9.0",
    ]
    assert dashboard_spec()["kpi"]["variable"] == "y"


async def _approved_and_started(kit: dict[str, Any], ctx: ServerContext) -> tuple[str, str]:
    dash_id = await create_published(kit, **_refit_spec(kit))
    await _request(kit, dash_id)
    (req,) = (await kit["ana"].get(f"/v1/dashboards/{dash_id}/refit-requests")).json()["items"]
    r = await kit["ana"].post(
        f"/v1/dashboards/{dash_id}/refit-requests/{req['id']}:approve", json={}
    )
    assert r.status_code == 200, r.text
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DatasetVersion)
            .where(m.DatasetVersion.status == "ingesting")
            .values(status="ready", sha256="sha256:" + "2" * 64, ready_at=now())
        )
    assert await refits.advance(ctx) == 1
    return dash_id, str(req["id"])


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_a_refit_whose_workflow_fails_is_marked_failed(
    kit: dict[str, Any], ctx: ServerContext, status: str
) -> None:
    dash_id, req_id = await _approved_and_started(kit, ctx)
    async with ctx.db.session() as s:
        req = await s.get(m.DashboardRefitRequest, req_id)
        assert req is not None and req.workflow_run_id is not None
        wr = await s.get(m.WorkflowRunRow, req.workflow_run_id)
        assert wr is not None
        wr.status = status
        wr.error = {"code": "USER_CODE_ERROR", "message": "the fit diverged"}
        assert await refits.on_workflow_ended(s, wr) == 1
    page = (await kit["eve"].get(f"/v1/dashboards/{dash_id}/refit-requests")).json()["items"]
    assert page[0]["status"] == "failed" and page[0]["problem"]["code"]
    seq = page[0]["readout_seq"]
    head = (await kit["cleo"].get(f"/v1/dashboards/{dash_id}/readouts/{seq}")).json()
    assert head["refit_status"] == "failed"


async def test_a_long_dataset_name_is_pinned_whole_and_a_bad_ref_never_aborts_the_pass(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await create_published(kit, **_refit_spec(kit))
    long = "co2-" + "x" * 120  # 124 characters: the ref is longer than 128
    async with ctx.db.session() as s:
        await s.execute(update(m.Dataset).where(m.Dataset.name == "co2").values(name=long))
    await _request(kit, dash_id)
    (req,) = (await kit["ana"].get(f"/v1/dashboards/{dash_id}/refit-requests")).json()["items"]
    r = await kit["ana"].post(
        f"/v1/dashboards/{dash_id}/refit-requests/{req['id']}:approve", json={}
    )
    assert r.status_code == 200, r.text
    assert r.json()["dataset_version_id"] == f"dataset://default/{long}@v2"
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardRefitRequest)
            .where(m.DashboardRefitRequest.id == req["id"])
            .values(dataset_version_id="dataset://default/" + "y" * 100 + "@")
        )
    assert await refits.advance(ctx) == 1  # failed, not raised
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardRefitRequest, req["id"])
        assert row is not None and row.status == "failed"


async def test_an_archived_dashboard_spends_nothing_more(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await create_published(kit, **_refit_spec(kit))
    await _request(kit, dash_id)
    (req,) = (await kit["ana"].get(f"/v1/dashboards/{dash_id}/refit-requests")).json()["items"]
    r = await kit["eve"].post(f"/v1/dashboards/{dash_id}:refresh")
    assert r.status_code == 202, r.text
    refresh_seq = r.json()["seq"]
    assert (await kit["ana"].post(f"/v1/dashboards/{dash_id}:archive")).status_code == 200
    runs = await _count(ctx, m.Run)
    await DashboardLoop(ctx).submit_queued()
    assert await _count(ctx, m.Run) == runs
    async with ctx.db.session() as s:
        queued = await s.get(m.DashboardReadout, (dash_id, refresh_seq))
        assert queued is not None and queued.status == "failed"
        decided = await s.get(m.DashboardRefitRequest, req["id"])
        assert decided is not None and decided.status == "superseded"
    r = await kit["ana"].post(
        f"/v1/dashboards/{dash_id}/refit-requests/{req['id']}:approve", json={}
    )
    assert r.status_code in (404, 409), r.text
    assert await _count(ctx, m.DatasetVersion) == 1
