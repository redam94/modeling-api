"""Scoring a model version with typed-in values: ``GET …/inputs`` describes what a score call
can send (the input contract an endpoint would freeze, and the input table), and ``…:score``
takes ``inline`` XrJSON data, checked against that contract and stored as a run input exactly
as an endpoint's inline payloads are.

The fitted ``trend`` model (``ts.hsgp_trend``) has a monthly ``t`` and a target ``y`` fitted
through 2023-12.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server_testkit.integrations import fitted_version
from mapi_spec.endpoints import ModelInputSchema
from sqlalchemy import func, select

pytestmark = pytest.mark.integration


async def _version(ctx: ServerContext) -> str:
    await fitted_version(ctx)
    async with ctx.db.session() as s:
        mv = (await s.execute(select(m.ModelVersion))).scalars().one()
        return f"/v1/models/{mv.model_id}/versions/{mv.version}"


def _request(**extra: Any) -> dict[str, Any]:
    return {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "model://default/trend@v1",
        **extra,
    }


def _months(ys: list[float | None]) -> dict[str, Any]:
    return {
        "coords": {"t": {"dims": ["t"], "data": [f"2024-{k + 1:02d}-01" for k in range(len(ys))]}},
        "data_vars": {"y": {"dims": ["t"], "data": ys}},
    }


async def _runs(ctx: ServerContext) -> int:
    async with ctx.db.session() as s:
        return int((await s.execute(select(func.count()).select_from(m.Run))).scalar_one())


async def test_the_inputs_of_a_version(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    base = await _version(ctx)
    r = await client.get(f"{base}/inputs", params={"periods": 3})
    assert r.status_code == 200, r.text
    inputs = ModelInputSchema.model_validate(r.json())
    assert inputs.model_ref == "model://default/trend@v1"
    snap = inputs.snapshot
    assert snap.horizon_dim == "t" and "forecast" in snap.score_modes
    assert set(snap.input_schema.dims) == {"t"} and "y" in snap.input_schema.vars
    assert snap.fit_coords["t"].end is not None
    table = inputs.input_table
    assert table is not None and table.horizon_dim == "t"
    assert [c.name for c in table.columns] == ["t", "y"]
    assert [row[0] for row in table.rows] == ["2024-01-01", "2024-02-01", "2024-03-01"]
    # the same contract an endpoint serving the version freezes
    await client.put("/v1/endpoints/trend-prod", json={"model": "model://default/trend@v1"})
    schema = (await client.get("/v1/endpoints/trend-prod/schema")).json()
    assert schema["snapshot"]["schema_hash"] == snap.schema_hash
    # periods are clamped to 1..200
    rows = (await client.get(f"{base}/inputs", params={"periods": 999})).json()
    assert len(rows["input_table"]["rows"]) == 200


async def test_scoring_typed_in_values(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    base = await _version(ctx)
    body = {
        "request": _request(mode="forecast", horizon={"dim": "t", "periods": 2}),
        "inline": {"data": _months([None, None])},
    }
    r = await client.post(f"{base}:score", json=body)
    assert r.status_code == 202, r.text
    async with ctx.db.session() as s:
        run = await s.get(m.Run, r.json()["run"]["id"])
        assert run is not None
        ref = run.inputs["inline.data"]
        assert ref["bucket"] == ctx.buckets.quarantine and "/inline/" in ref["name"]
        assert "data" not in run.payload["request"]
    stored = json.loads(await ctx.store.get(ref["bucket"], ref["name"]))
    assert stored["coords"]["t"]["data"] == ["2024-01-01", "2024-02-01"]


async def test_typed_in_values_are_checked_before_a_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    base = await _version(ctx)
    before = await _runs(ctx)
    bad = {
        "coords": {"t": {"dims": ["t"], "data": ["2024-01-01"]}},
        "data_vars": {"spend": {"dims": ["t"], "data": [1.0]}},
    }
    r = await client.post(
        f"{base}:score", json={"request": _request(mode="predict"), "inline": {"data": bad}}
    )
    assert r.status_code == 422, r.text
    pointers = {(e["code"], e["pointer"]) for e in r.json()["errors"]}
    assert ("SCHEMA_INVALID", "/inline/data/data_vars/spend") in pointers
    # inline and a dataset reference for the same input
    both = {
        "request": _request(mode="predict", data="dataset://default/trend@v1"),
        "inline": {"data": _months([1.0])},
    }
    r = await client.post(f"{base}:score", json=both)
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID"
    assert await _runs(ctx) == before
