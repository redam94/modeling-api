"""``GET /v1/run-keys`` (v0.14): metric series, param keys and tag keys of the project's (or an
experiment's) runs with run counts, most common first, ``prefix`` and the caps."""

from __future__ import annotations

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.services import run_search
from mapi_server.services.runs import Scope, create_run
from mapi_spec.executor import RunKind
from mapi_spec.resources import ResourceClass

DEFAULT = Scope("default", "default")


async def _run(ctx: ServerContext, scope: Scope = DEFAULT) -> str:
    async with ctx.db.session() as s:
        run = await create_run(
            s, scope, kind=RunKind.FIT, resource_class=ResourceClass.S, timeout_s=600, payload={}
        )
        return run.id


async def _log(
    client: httpx.AsyncClient,
    run_id: str,
    metrics: dict[str, dict[str, str]],
    params: dict[str, object],
    tags: dict[str, str],
) -> None:
    if metrics:
        body = {"metrics": [{"key": k, "value": 1.0, "labels": v} for k, v in metrics.items()]}
        assert (await client.post(f"/v1/runs/{run_id}/metrics", json=body)).status_code == 200
    if params:
        r = await client.post(f"/v1/runs/{run_id}/params", json={"params": params})
        assert r.status_code == 200
    if tags:
        assert (await client.patch(f"/v1/runs/{run_id}", json={"tags": tags})).status_code == 200


async def test_keys_are_counted_per_run_most_common_first(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    r1, r2, r3 = [await _run(ctx) for _ in range(3)]
    await _log(
        client, r1, {"rmse": {}, "loss": {"split": "val"}}, {"lr": 0.1, "a.b": 1}, {"t": "x"}
    )
    await _log(client, r2, {"rmse": {}}, {"lr": 0.2}, {"t": "y", "team": "growth"})
    await _log(client, r3, {"rmse": {}, "loss": {"split": "train"}}, {}, {})
    keys = (await client.get("/v1/run-keys")).json()
    assert [(k["key"], k["labels"], k["runs"]) for k in keys["metrics"]] == [
        ("rmse", {}, 3),
        ("loss", {"split": "train"}, 1),
        ("loss", {"split": "val"}, 1),
    ]
    assert [(k["key"], k["runs"]) for k in keys["params"]] == [("lr", 2), ("a.b", 1)]
    assert [(k["key"], k["runs"]) for k in keys["tags"]] == [("t", 2), ("team", 1)]
    assert keys["truncated"] is False
    only = (await client.get("/v1/run-keys?prefix=lo")).json()
    assert [k["key"] for k in only["metrics"]] == ["loss", "loss"]
    assert only["params"] == [] and only["tags"] == []

    exp = (await client.post("/v1/experiments", json={"name": "e"})).json()
    await client.post(f"/v1/experiments/{exp['id']}/runs", json={"add": [r2]})
    scoped = (await client.get(f"/v1/run-keys?experiment_id={exp['id']}")).json()
    assert [(k["key"], k["runs"]) for k in scoped["metrics"]] == [("rmse", 1)]
    assert [k["key"] for k in scoped["params"]] == ["lr"]
    assert [k["key"] for k in scoped["tags"]] == ["t", "team"]


async def test_lists_are_capped(
    client: httpx.AsyncClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_search, "MAX_KEY_PARAMS", 1)
    run_id = await _run(ctx)
    await _log(client, run_id, {}, {"a": 1, "b": 2}, {})
    keys = (await client.get("/v1/run-keys")).json()
    assert [k["key"] for k in keys["params"]] == ["a"] and keys["truncated"] is True
