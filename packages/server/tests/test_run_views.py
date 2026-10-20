"""Saved run views (v0.14): CRUD, query validation with pointers, unique names per project and
experiment, the per-project cap, permissions and audit rows."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services import run_views as views_mod
from mapi_server.services.run_views import check_view_query
from mapi_server_testkit.identity import grant_role, make_user, session_caller
from mapi_spec.api import RunViewQuery
from mapi_spec.errors import MapiError
from sqlalchemy import select

QUERY: dict[str, Any] = {
    "kind": "fit",
    "filters": ["metric.rmse < 15", "param.lr = 0.01"],
    "order_by": "-metric.rmse",
    "columns": ["metric.rmse", "metric.loss{split=val}.min", "tag.team"],
    "tags": ["team:growth"],
}


async def _view(client: httpx.AsyncClient, name: str, **body: Any) -> dict[str, Any]:
    r = await client.post("/v1/run-views", json={"name": name, "query": QUERY, **body})
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


async def test_save_list_rename_and_delete_views(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    b = await _view(client, "best fits")
    assert b["id"].startswith("rv_") and b["experiment_id"] is None
    assert b["query"]["filters"] == QUERY["filters"] and b["query"]["order_by"] == "-metric.rmse"
    a = await _view(client, "Alpha")
    page = (await client.get("/v1/run-views?limit=1")).json()
    assert [v["name"] for v in page["items"]] == ["Alpha"]  # by name, case ignored
    rest = (await client.get(f"/v1/run-views?cursor={page['next_cursor']}")).json()
    assert [v["name"] for v in rest["items"]] == ["best fits"] and rest["next_cursor"] is None
    r = await client.patch(f"/v1/run-views/{a['id']}", json={"query": {"state": "succeeded"}})
    assert r.status_code == 200 and r.json()["query"] == {
        "kind": None,
        "state": "succeeded",
        "name": None,
        "spec_id": None,
        "experiment_id": None,
        "parent_run_id": None,
        "tags": [],
        "filters": [],
        "order_by": None,
        "columns": [],
    }
    r = await client.patch(f"/v1/run-views/{a['id']}", json={"name": "alpha 2"})
    assert r.status_code == 200 and r.json()["name"] == "alpha 2"
    assert (await client.patch(f"/v1/run-views/{a['id']}", json={})).status_code == 422
    assert (await client.patch("/v1/run-views/rv_nope", json={"name": "x"})).status_code == 404
    assert (await client.delete(f"/v1/run-views/{a['id']}")).status_code == 204
    assert (await client.delete(f"/v1/run-views/{a['id']}")).status_code == 404
    names = [v["name"] for v in (await client.get("/v1/run-views")).json()["items"]]
    assert names == ["best fits"]
    async with ctx.db.session() as s:
        actions = [
            r.action
            for r in (
                await s.execute(
                    select(m.AuditEvent)
                    .where(m.AuditEvent.action.like("run_view.%"))
                    .order_by(m.AuditEvent.id)
                )
            ).scalars()
        ]
    assert actions == ["run_view.save"] * 4 + ["run_view.delete"]


async def test_names_are_unique_per_project_and_experiment(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    exp = (await client.post("/v1/experiments", json={"name": "e"})).json()
    await _view(client, "Shared")
    dup = await client.post("/v1/run-views", json={"name": "shared", "query": {}})
    assert dup.status_code == 409 and dup.json()["code"] == "CONFLICT"
    # the same name in an experiment is another view
    scoped = await _view(client, "shared", experiment_id=exp["id"])
    dup = await client.post(
        "/v1/run-views", json={"name": "SHARED", "experiment_id": exp["id"], "query": {}}
    )
    assert dup.status_code == 409
    listed = (await client.get(f"/v1/run-views?experiment_id={exp['id']}")).json()["items"]
    assert [v["id"] for v in listed] == [scoped["id"]]
    other = await _view(client, "other", experiment_id=exp["id"])
    r = await client.patch(f"/v1/run-views/{other['id']}", json={"name": "Shared"})
    assert r.status_code == 409
    unknown = await client.post(
        "/v1/run-views", json={"name": "x", "experiment_id": "exp_nope", "query": {}}
    )
    assert unknown.status_code == 422 and unknown.json()["errors"][0]["pointer"] == (
        "/experiment_id"
    )


@pytest.mark.parametrize(
    ("query", "where"),
    [
        ({"filters": ["metric.rmse <<< 3"]}, "filters"),
        ({"order_by": "-nope.x"}, "order_by"),
        ({"columns": ["metric.rmse", "bogus"]}, "columns"),
        ({"tags": ["bad key!"]}, "tags"),
        ({"filters": ["metric.rmse < 1"] * 9}, "filters"),
        ({"unknown": 1}, "unknown"),
    ],
)
async def test_queries_the_run_search_refuses_are_refused(
    client: httpx.AsyncClient, query: dict[str, Any], where: str
) -> None:
    r = await client.post("/v1/run-views", json={"name": "bad", "query": query})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "SCHEMA_INVALID"
    assert any(f"/query/{where}" in e["pointer"] for e in r.json()["errors"]), r.json()


def test_the_server_check_points_at_the_bad_item() -> None:
    query = RunViewQuery.model_construct(
        filters=["metric.rmse < 1", "metric.rmse ?? 2"],
        columns=[],
        tags=[],
        order_by=None,
    )
    with pytest.raises(MapiError) as info:
        check_view_query(query)
    assert info.value.pointer == "/query/filters/1"


async def test_a_project_keeps_at_most_max_views(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views_mod, "MAX_RUN_VIEWS", 2)
    await _view(client, "one")
    await _view(client, "two")
    r = await client.post("/v1/run-views", json={"name": "three", "query": {}})
    assert r.json()["code"] == "QUOTA_EXCEEDED"


async def test_viewers_read_and_cannot_write(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    view = await _view(client, "visible")
    viewer = await make_user(ctx, "viewer@default.test")
    await grant_role(ctx, viewer, "default", "default", "viewer")
    vc = await session_caller(ctx, client, viewer)
    assert (await vc.get("/v1/run-views")).status_code == 200
    assert (await vc.get("/v1/run-keys")).status_code == 200
    assert (await vc.post("/v1/run-views", json={"name": "x", "query": {}})).status_code == 403
    assert (await vc.patch(f"/v1/run-views/{view['id']}", json={"name": "y"})).status_code == 403
    assert (await vc.delete(f"/v1/run-views/{view['id']}")).status_code == 403
