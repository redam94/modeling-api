"""``GET /v1/run-compare`` (v0.14): runs in the given order, summaries, default and chosen
metric keys with label series, params that differ, media thinning with links, and the
refusals (1 or 11 runs, duplicates, a run of another project)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.services import compare as compare_mod
from mapi_server.services.compare import thin
from mapi_server.services.runs import Scope, create_run
from mapi_server_testkit.identity import make_tenant
from mapi_server_testkit.media import b64, png_bytes
from mapi_spec.executor import RunKind
from mapi_spec.resources import ResourceClass

DEFAULT = Scope("default", "default")


async def _run(ctx: ServerContext, scope: Scope = DEFAULT) -> str:
    async with ctx.db.session() as s:
        run = await create_run(
            s, scope, kind=RunKind.FIT, resource_class=ResourceClass.S, timeout_s=600, payload={}
        )
        return run.id


async def _metrics(client: httpx.AsyncClient, run_id: str, points: list[dict[str, Any]]) -> None:
    r = await client.post(f"/v1/runs/{run_id}/metrics", json={"metrics": points})
    assert r.status_code == 200, r.text


def _curve(key: str, n: int, *, scale: float, labels: dict[str, str]) -> list[dict[str, Any]]:
    return [{"key": key, "value": scale / (i + 1), "step": i, "labels": labels} for i in range(n)]


def test_thin_keeps_the_first_and_the_last() -> None:
    assert thin(3, 50) == [0, 1, 2]
    picked = thin(120, 50)
    assert len(picked) == 50 and picked[0] == 0 and picked[-1] == 119
    assert picked == sorted(set(picked))
    assert thin(10, 1) == [9] and thin(0, 5) == []


async def test_compare_two_runs(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    a, b = await _run(ctx), await _run(ctx)
    for run_id, scale in ((a, 1.0), (b, 2.0)):
        await _metrics(
            client,
            run_id,
            [
                *_curve("loss", 40, scale=scale, labels={"split": "train"}),
                *_curve("loss", 40, scale=scale, labels={"split": "val"}),
                {"key": "rmse", "value": scale},
            ],
        )
    await _metrics(client, a, [{"key": "only_a", "value": 3.0}])
    await client.post(f"/v1/runs/{a}/params", json={"params": {"lr": 0.1, "seed": 1}})
    await client.post(f"/v1/runs/{b}/params", json={"params": {"lr": 0.2, "seed": 1, "extra": "x"}})

    r = await client.get("/v1/run-compare", params={"run": [b, a], "max_points": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["id"] for x in body["runs"]] == [b, a]
    assert {(s["run_id"], s["key"]) for s in body["summaries"]} >= {(a, "only_a"), (b, "rmse")}
    # default keys: the ones most runs have first, then by key
    assert [mt["key"] for mt in body["metrics"]] == ["loss", "rmse", "only_a"]
    loss = body["metrics"][0]["series"]
    assert [(s["run_id"], s["labels"]["split"]) for s in loss] == [
        (b, "train"),
        (b, "val"),
        (a, "train"),
        (a, "val"),
    ]
    assert all(len(s["steps"]) == 10 and s["downsampled"] and s["count"] == 40 for s in loss)
    params = {p["key"]: p for p in body["params"]}
    assert list(params) == ["extra", "lr", "seed"]
    assert params["lr"]["values"] == {a: "0.1", b: "0.2"} and params["lr"]["differs"]
    assert params["extra"]["values"] == {a: None, b: "x"} and params["extra"]["differs"]
    assert not params["seed"]["differs"]
    assert body["media"] == [] and body["truncated"] == {}

    chosen = await client.get(
        "/v1/run-compare", params={"run": [a, b], "key": ["rmse"], "params": "false"}
    )
    out = chosen.json()
    assert [mt["key"] for mt in out["metrics"]] == ["rmse"] and out["params"] == []
    rmse = out["metrics"][0]["series"]
    assert [(s["run_id"], s["steps"], s["values"]) for s in rmse] == [
        (a, [None], [1.0]),
        (b, [None], [2.0]),
    ]


async def test_media_series_are_thinned_with_links(
    client: httpx.AsyncClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compare_mod, "MAX_COMPARE_MEDIA_ITEMS", 3)
    a, b = await _run(ctx), await _run(ctx)
    for step in range(5):
        r = await client.post(
            f"/v1/runs/{a}/media",
            json={
                "key": "samples",
                "kind": "image",
                "content_type": "image/png",
                "data": b64(png_bytes(4, 3, shade=step)),
                "step": step,
            },
        )
        assert r.status_code == 201, r.text
    r = await client.get("/v1/run-compare", params={"run": [a, b], "media_key": "samples"})
    body = r.json()
    (series,) = body["media"]
    assert series["run_id"] == a and series["key"] == "samples" and series["kind"] == "image"
    assert [i["step"] for i in series["items"]] == [0, 2, 4]
    assert all(i["url"] for i in series["items"])
    assert body["truncated"] == {"media": True}


async def test_refusals(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    a, b = await _run(ctx), await _run(ctx)
    one = await client.get("/v1/run-compare", params={"run": [a]})
    assert one.status_code == 422 and one.json()["code"] == "SCHEMA_INVALID"
    many = [await _run(ctx) for _ in range(11)]
    assert (await client.get("/v1/run-compare", params={"run": many})).status_code == 422
    dup = await client.get("/v1/run-compare", params={"run": [a, a]})
    assert dup.status_code == 422
    bad_key = await client.get("/v1/run-compare", params={"run": [a, b], "key": "no spaces"})
    assert bad_key.status_code == 422
    too_many_keys = {"run": [a, b], "key": [f"k{i}" for i in range(13)]}
    assert (await client.get("/v1/run-compare", params=too_many_keys)).status_code == 422
    missing = await client.get("/v1/run-compare", params={"run": [a, "run_nope"]})
    assert missing.status_code == 404
    await make_tenant(ctx, "other", projects=("main",))
    foreign = await _run(ctx, Scope("other", "main"))
    r = await client.get("/v1/run-compare", params={"run": [a, foreign]})
    assert r.status_code == 404
    pts = await client.get("/v1/run-compare", params={"run": [a, b], "max_points": 5})
    assert pts.status_code == 422
