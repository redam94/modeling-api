"""Explorers and dataset About through the API (M3.5): explore runs for dataset and model
versions (pinned inputs, cached answers, idempotent replays), the exploration view with
its index and signed view links, comparisons, ingest-time views via the artifact route,
the reviewer capability on memberships, and the M3.5 routes this component serves."""

from __future__ import annotations

import json

import httpx
import pytest
from mapi_server.app import openapi_document
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.finalizer import Finalizer
from mapi_server_testkit.builders import ready_dataset, simulate_outputs
from mapi_server_testkit.identity import grant_role, make_user
from mapi_server_testkit.integrations import fitted_version, refit_version
from mapi_server_testkit.plots import _to_finalizing
from mapi_spec.api import M35_ENDPOINTS
from mapi_spec.run import RunState
from mapi_spec.views import EXPLORE_INDEX, ExplorerIndex
from sqlalchemy import select

pytestmark = pytest.mark.integration

SERVED_HERE = {
    "list_governance_checks",
    "get_dataset_about",
    "update_dataset_about",
    "explore_dataset_version",
    "get_exploration",
    "explore_model_version",
    "compare_model_versions",
    "list_policies",
    "create_policy",
    "get_policy",
    "update_policy",
    "delete_policy",
    "list_policy_versions",
    "list_tenant_policies",
    "create_tenant_policy",
    "get_tenant_policy",
    "update_tenant_policy",
    "delete_tenant_policy",
    "request_model_validation",
    "list_model_validations",
    "list_validations",
    "get_validation",
    "list_validation_evidence",
    "cancel_validation",
    "resubmit_validation",
    "decide_validation",
    "deprecate_validation",
    "export_evidence_pack",
}


def test_this_component_serves_its_m35_rows() -> None:
    doc = openapi_document()
    served = {
        (method.upper(), path, op.get("operationId"))
        for path, item in doc["paths"].items()
        for method, op in item.items()
        if isinstance(op, dict)
    }
    rows = [e for e in M35_ENDPOINTS if e.operation_id in SERVED_HERE]
    assert len(rows) == len(SERVED_HERE)
    missing = [e.operation_id for e in rows if (e.method, e.path, e.operation_id) not in served]
    assert not missing, missing


def _index(target: str, ref: str) -> bytes:
    return (
        ExplorerIndex.model_validate(
            {
                "target": target,
                "ref": ref,
                "sections": [
                    {"section": "stl", "view": "ui/explore/stl.json"},
                    {
                        "section": "vif",
                        "view": "ui/explore/vif.json",
                        "status": "skipped",
                        "message_plain": "Not available: one driver.",
                    },
                ],
            }
        )
        .model_dump_json()
        .encode()
    )


async def _finish_explore(ctx: ServerContext, run_id: str, target: str, ref: str) -> None:
    run, att = await _to_finalizing(ctx, run_id)
    add = {
        EXPLORE_INDEX: (_index(target, ref), "application/json"),
        "ui/explore/stl.json": (
            json.dumps({"format": "mapi.data-explorer.stl/v1"}).encode(),
            "application/json",
        ),
    }
    await simulate_outputs(ctx, run, att, add=add)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED


async def test_dataset_about_and_exploration(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    ds, _dv = await ready_dataset(ctx)
    about = (await client.get(f"/v1/datasets/{ds.id}/about")).json()
    assert about["description_user"] is None and "48" in about["summary_generated"]
    r = await client.patch(
        f"/v1/datasets/{ds.id}/about", json={"description_user": "Monthly CO2 from Mauna Loa"}
    )
    assert r.status_code == 200 and r.json()["description_user"] == "Monthly CO2 from Mauna Loa"
    body = {"sections": ["stl", "vif", "stationarity"], "max_lag": 12}
    r = await client.post(
        f"/v1/datasets/{ds.id}/versions/1/explore", json=body, headers={"Idempotency-Key": "k-1"}
    )
    assert r.status_code == 202, r.text
    ex = r.json()
    assert ex["run"]["kind"] == "explore" and ex["target"] == "dataset" and not ex["done"]
    assert ex["ref"] == "dataset://default/co2@v1"
    replay = await client.post(
        f"/v1/datasets/{ds.id}/versions/1/explore", json=body, headers={"Idempotency-Key": "k-1"}
    )
    assert replay.status_code == 200 and replay.json()["run"]["id"] == ex["run"]["id"]
    cached = await client.post(f"/v1/datasets/{ds.id}/versions/1/explore", json=body)
    assert cached.status_code == 200 and cached.json()["run"]["id"] == ex["run"]["id"]
    other = await client.post(
        f"/v1/datasets/{ds.id}/versions/1/explore", json={"sections": ["stl"]}
    )
    assert other.status_code == 202 and other.json()["run"]["id"] != ex["run"]["id"]
    async with ctx.db.session() as s:
        run = await s.get(m.Run, ex["run"]["id"])
        assert run is not None and set(run.inputs) == {"dataset"}
        assert run.payload["data"]["sections"] == ["stl", "vif", "stationarity"]
    await _finish_explore(ctx, ex["run"]["id"], "dataset", ex["ref"])
    done = (await client.get(f"/v1/explorations/{ex['run']['id']}")).json()
    assert done["done"] and done["index"]["sections"][1]["status"] == "skipped"
    assert {v["name"] for v in done["views"]} == {EXPLORE_INDEX, "ui/explore/stl.json"}
    assert (await client.get("/v1/explorations/run_nope")).status_code == 404
    missing = await client.post(f"/v1/datasets/{ds.id}/versions/9/explore", json={})
    assert missing.status_code == 404
    assert (
        await client.post(
            f"/v1/datasets/{ds.id}/versions/1/explore", json={"sections": ["telepathy"]}
        )
    ).status_code == 422
    async with ctx.db.session() as s:
        actions = [a.action for a in (await s.execute(select(m.AuditEvent))).scalars()]
    assert "dataset.about_update" in actions


async def test_ingest_views_are_served_by_the_artifact_route(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds, _dv = await ready_dataset(ctx)
    meta = await ctx.store.put(ctx.buckets.data, "t/default/x/ui/explore/index.json", b"{}")
    async with ctx.db.session() as s:
        row = await s.get(m.DatasetVersion, (ds.id, 1))
        assert row is not None
        row.objects = {
            **row.objects,
            EXPLORE_INDEX: {
                "bucket": meta.bucket,
                "name": meta.name,
                "generation": meta.generation,
            },
        }
    r = await client.get(f"/v1/datasets/{ds.id}/versions/1/artifacts/ui/explore/index.json")
    assert r.status_code == 200 and r.json()["name"] == EXPLORE_INDEX
    bad = await client.get(f"/v1/datasets/{ds.id}/versions/1/artifacts/ui/explore/../data.zarr.zip")
    assert bad.status_code == 404
    assert (
        await client.get(f"/v1/datasets/{ds.id}/versions/1/artifacts/ui/other.json")
    ).status_code == 404


async def test_model_exploration_and_comparison(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    mv2 = await refit_version(ctx, mv)
    r = await client.post(
        f"/v1/models/{mv.model_id}/versions/1/explore",
        json={"sections": ["params", "forecast"], "horizon": 6},
    )
    assert r.status_code == 202, r.text
    ex = r.json()
    assert ex["target"] == "model" and ex["ref"] == "model://default/trend@v1"
    async with ctx.db.session() as s:
        run = await s.get(m.Run, ex["run"]["id"])
        assert run is not None and {"bundle", "dataset"} <= set(run.inputs)
        assert run.model_version_id == mv.id
    cmp = await client.post(
        "/v1/models:compare",
        json={"models": ["model://default/trend@v1", "model://default/trend@v2"]},
    )
    assert cmp.status_code == 202, cmp.text
    async with ctx.db.session() as s:
        crun = await s.get(m.Run, cmp.json()["run"]["id"])
        assert crun is not None and "compare.0" in crun.inputs
        assert crun.payload["model"]["compare_with"] == ["model://default/trend@v2"]
    unknown = await client.post(
        "/v1/models:compare",
        json={"models": ["model://default/trend@v1", "model://default/nope@v1"]},
    )
    assert unknown.status_code == 404
    await _finish_explore(ctx, ex["run"]["id"], "model", ex["ref"])
    done = (await client.get(f"/v1/explorations/{ex['run']['id']}")).json()
    assert done["done"] and done["index"]["target"] == "model"
    assert mv2.version == 2


async def test_the_reviewer_capability_is_granted_on_memberships(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    rita = await make_user(ctx, "rita@default.test")
    await grant_role(ctx, rita, "default", "default", "editor")
    r = await client.patch(
        f"/v1/tenants/default/projects/default/members/{rita.id}",
        json={"capabilities": ["reviewer"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["capabilities"] == ["reviewer"] and r.json()["role"] == "editor"
    r = await client.patch(
        f"/v1/tenants/default/projects/default/members/{rita.id}", json={"role": "runner"}
    )
    assert r.json()["capabilities"] == ["reviewer"] and r.json()["role"] == "runner"
    from mapi_server.auth.identity import memberships_of
    from mapi_server.auth.request import held_capabilities

    async with ctx.db.session() as s:
        ms = await memberships_of(s, rita.id)
    assert {c.value for c in held_capabilities(ms, rita.id, "default", "default")} == {"reviewer"}
    assert not held_capabilities(ms, rita.id, "default", "other")
