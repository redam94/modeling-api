"""Experiments (v0.14): CRUD, unique names, archive rules, bulk run moves, delete, the
``list_runs`` filter, run creation and PATCH moves, inheritance through retries and
workflows (``test_experiments_workflows.py``), permissions and audit rows."""

from __future__ import annotations

from typing import Any

import httpx
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import Scope, create_run
from mapi_server_testkit.builders import trend_spec
from mapi_server_testkit.identity import grant_role, make_user, session_caller
from mapi_spec.executor import RunKind
from mapi_spec.resources import ResourceClass
from sqlalchemy import select


async def _run(ctx: ServerContext, **fields: Any) -> str:
    async with ctx.db.session() as s:
        run = await create_run(
            s,
            Scope("default", "default"),
            kind=RunKind.FIT,
            resource_class=ResourceClass.S,
            timeout_s=600,
            payload={},
        )
        for k, v in fields.items():
            setattr(run, k, v)
        return run.id


async def _experiment(client: httpx.AsyncClient, name: str, **body: Any) -> dict[str, Any]:
    r = await client.post("/v1/experiments", json={"name": name, **body})
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


async def _audit(ctx: ServerContext, action: str) -> list[m.AuditEvent]:
    async with ctx.db.session() as s:
        rows = await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == action))
        return list(rows.scalars())


async def test_create_list_get_rename_and_unique_names(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    a = await _experiment(client, "  Adstock study ", description="lengths 2, 4, 8")
    assert a["name"] == "Adstock study" and a["id"].startswith("exp_")
    assert (a["run_count"], a["last_run_at"], a["archived"]) == (0, None, False)
    dup = await client.post("/v1/experiments", json={"name": "ADSTOCK STUDY"})
    assert dup.status_code == 409 and dup.json()["code"] == "CONFLICT"
    bad = await client.post("/v1/experiments", json={"name": "a\x00b"})
    assert bad.status_code == 422
    b = await _experiment(client, "benchmarks")
    page = (await client.get("/v1/experiments?limit=1")).json()
    assert [e["id"] for e in page["items"]] == [b["id"]]  # newest first
    rest = (await client.get(f"/v1/experiments?cursor={page['next_cursor']}")).json()
    assert [e["id"] for e in rest["items"]] == [a["id"]] and rest["next_cursor"] is None
    found = (await client.get("/v1/experiments?name=adstock")).json()["items"]
    assert [e["id"] for e in found] == [a["id"]]
    assert (await client.get(f"/v1/experiments/{a['id']}")).json()["description"] == (
        "lengths 2, 4, 8"
    )
    assert (await client.get("/v1/experiments/exp_nope")).status_code == 404
    taken = await client.patch(f"/v1/experiments/{b['id']}", json={"name": "adstock study"})
    assert taken.status_code == 409
    r = await client.patch(
        f"/v1/experiments/{b['id']}", json={"name": "Benchmarks", "description": None}
    )
    assert r.status_code == 200 and r.json()["name"] == "Benchmarks"
    # the same name in other case is a rename of itself, not a conflict
    assert (await client.patch(f"/v1/experiments/{b['id']}", json={"name": "benchmarks"})).json()[
        "name"
    ] == "benchmarks"
    assert (await client.patch(f"/v1/experiments/{b['id']}", json={})).status_code == 422
    same = await client.patch(f"/v1/experiments/{b['id']}", json={"name": None})
    assert same.status_code == 200 and same.json()["name"] == "benchmarks"  # null: unchanged
    assert len(await _audit(ctx, "experiment.create")) == 2
    updates = await _audit(ctx, "experiment.update")
    assert [u.details["fields"] for u in updates] == [["name"], ["name"]]
    assert all(u.resource_kind == "run" for u in updates)


async def test_bulk_moves_counts_and_the_runs_filter(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    a = await _experiment(client, "a")
    b = await _experiment(client, "b")
    r1, r2, r3 = [await _run(ctx) for _ in range(3)]
    r = await client.post(f"/v1/experiments/{a['id']}/runs", json={"add": [r1, r2, "run_missing"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"added": 2, "removed": 0, "missing": ["run_missing"]}
    again = await client.post(f"/v1/experiments/{a['id']}/runs", json={"add": [r1]})
    assert again.json()["added"] == 0  # already there
    # moving a run to another experiment takes it out of the first
    r = await client.post(f"/v1/experiments/{b['id']}/runs", json={"add": [r2, r3]})
    assert r.json()["added"] == 2
    one = (await client.get(f"/v1/experiments/{a['id']}")).json()
    assert one["run_count"] == 1 and one["last_run_at"] is not None
    listed = (await client.get(f"/v1/runs?experiment_id={b['id']}")).json()["items"]
    assert {x["id"] for x in listed} == {r2, r3}
    assert all(x["experiment_id"] == b["id"] for x in listed)
    r = await client.post(f"/v1/experiments/{b['id']}/runs", json={"remove": [r2, r1]})
    assert r.json() == {"added": 0, "removed": 1, "missing": []}  # r1 is in a, left alone
    assert (await client.get(f"/v1/runs/{r2}")).json()["experiment_id"] is None
    bad = await client.post(f"/v1/experiments/{a['id']}/runs", json={"add": [r1], "remove": [r1]})
    assert bad.status_code == 422
    assert (await client.post(f"/v1/experiments/{a['id']}/runs", json={})).status_code == 422
    moves = [u.details for u in await _audit(ctx, "experiment.update")]
    assert {"added": 2, "removed": 0} in moves and {"added": 0, "removed": 1} in moves


async def test_archived_experiments_take_no_new_runs(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    spec_id = await trend_spec(ctx)
    exp = await _experiment(client, "archived one")
    run_in = await _run(ctx, experiment_id=exp["id"])
    other = await _run(ctx)
    r = await client.patch(f"/v1/experiments/{exp['id']}", json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] is True
    body = {"kind": "fit", "spec_id": spec_id, "revision": 1, "experiment_id": exp["id"]}
    r = await client.post("/v1/runs", json=body)
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    r = await client.patch(f"/v1/runs/{other}", json={"experiment_id": exp["id"]})
    assert r.status_code == 409
    r = await client.post(f"/v1/experiments/{exp['id']}/runs", json={"add": [other]})
    assert r.status_code == 409
    # it still lists and filters, and its runs can leave it
    listed = (await client.get(f"/v1/runs?experiment_id={exp['id']}")).json()["items"]
    assert [x["id"] for x in listed] == [run_in]
    assert (await client.get("/v1/experiments?archived=true")).json()["items"][0]["id"] == exp["id"]
    assert (await client.get("/v1/experiments?archived=false")).json()["items"] == []
    r = await client.post(f"/v1/experiments/{exp['id']}/runs", json={"remove": [run_in]})
    assert r.json()["removed"] == 1
    r = await client.patch(f"/v1/experiments/{exp['id']}", json={"archived": False})
    assert r.json()["archived"] is False
    r = await client.post("/v1/runs", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["experiment_id"] == exp["id"]
    unknown = await client.post("/v1/runs", json={**body, "experiment_id": "exp_nope"})
    assert unknown.status_code == 422
    assert unknown.json()["errors"][0]["pointer"] == "/experiment_id"


async def test_patch_moves_a_run_and_delete_keeps_runs_and_drops_views(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    exp = await _experiment(client, "to delete")
    run_id = await _run(ctx)
    r = await client.patch(f"/v1/runs/{run_id}", json={"experiment_id": exp["id"]})
    assert r.status_code == 200 and r.json()["experiment_id"] == exp["id"]
    (audit,) = await _audit(ctx, "run.update")
    assert audit.details["fields"] == ["experiment_id"]
    unknown = await client.patch(f"/v1/runs/{run_id}", json={"experiment_id": "exp_nope"})
    assert unknown.status_code == 422
    view = await client.post(
        "/v1/run-views", json={"name": "v", "experiment_id": exp["id"], "query": {}}
    )
    assert view.status_code == 201, view.text
    assert (await client.delete(f"/v1/experiments/{exp['id']}")).status_code == 204
    assert (await client.get(f"/v1/experiments/{exp['id']}")).status_code == 404
    assert (await client.delete(f"/v1/experiments/{exp['id']}")).status_code == 404
    assert (await client.get(f"/v1/runs/{run_id}")).json()["experiment_id"] is None
    assert (await client.get("/v1/run-views")).json()["items"] == []
    assert len(await _audit(ctx, "experiment.delete")) == 1
    r = await client.patch(f"/v1/runs/{run_id}", json={"experiment_id": None})
    assert r.status_code == 200 and r.json()["experiment_id"] is None


async def test_a_retry_stays_in_the_experiment(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    spec_id = await trend_spec(ctx)
    exp = await _experiment(client, "retries")
    body = {"kind": "fit", "spec_id": spec_id, "revision": 1, "experiment_id": exp["id"]}
    run = (await client.post("/v1/runs", json=body)).json()
    async with ctx.db.session() as s:
        row = await s.get(m.Run, run["id"])
        assert row is not None
        row.state = "failed"
    # an archived experiment takes no retry either (as it takes no create, move or bulk add)
    r = await client.patch(f"/v1/experiments/{exp['id']}", json={"archived": True})
    assert r.status_code == 200
    r = await client.post(f"/v1/runs/{run['id']}:retry", json={})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT", r.text
    assert (await client.get(f"/v1/experiments/{exp['id']}")).json()["run_count"] == 1
    await client.patch(f"/v1/experiments/{exp['id']}", json={"archived": False})
    r = await client.post(f"/v1/runs/{run['id']}:retry", json={})
    assert r.status_code == 201, r.text
    assert r.json()["retry_of"] == run["id"] and r.json()["experiment_id"] == exp["id"]


async def test_viewers_read_and_cannot_write(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    exp = await _experiment(client, "visible")
    run_id = await _run(ctx)
    viewer = await make_user(ctx, "viewer@default.test")
    await grant_role(ctx, viewer, "default", "default", "viewer")
    vc = await session_caller(ctx, client, viewer)
    assert (await vc.get("/v1/experiments")).status_code == 200
    assert (await vc.get(f"/v1/experiments/{exp['id']}")).status_code == 200
    assert (await vc.post("/v1/experiments", json={"name": "x"})).status_code == 403
    assert (await vc.patch(f"/v1/experiments/{exp['id']}", json={"name": "y"})).status_code == 403
    assert (await vc.delete(f"/v1/experiments/{exp['id']}")).status_code == 403
    r = await vc.post(f"/v1/experiments/{exp['id']}/runs", json={"add": [run_id]})
    assert r.status_code == 403
