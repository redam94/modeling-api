"""Forecasts with actuals through an endpoint (``mapi_spec.actuals``): the API null-checks an
inline payload's target and refuses actuals a model cannot use before a run starts, checks
them for a model that can (the capability of the plugin build that scores, from the catalog),
and serves the schema's actuals example, input table and snippets for such endpoints.

The fitted ``trend`` model (``ts.hsgp_trend``, monthly ``y`` fitted through 2023-12) cannot
condition on actuals; a catalog whose entry for that exact plugin version declares
``conditions_on_actuals`` stands in for a capable plugin build.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server_testkit.integrations import fitted_version, refit_version
from mapi_spec.endpoints import EndpointSchema, EndpointScoreCall
from sqlalchemy import func, select

pytestmark = pytest.mark.integration

ALIAS = "/v1/endpoints/trend-prod"


async def _endpoint(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    await fitted_version(ctx)
    r = await client.put(ALIAS, json={"model": "model://default/trend@v1"})
    assert r.status_code == 200, r.text


async def _capable(ctx: ServerContext, flag: bool = True) -> None:
    """The cached catalog's ts.hsgp_trend@1.0.0 declares (or not) conditions_on_actuals."""
    async with ctx.db.session() as s:
        row = await s.get(m.Catalog, ctx.catalogs.image_digest)
        assert row is not None
        catalog = json.loads(json.dumps(row.catalog))
        for entry in catalog["models"]:
            if entry["manifest"]["id"] == "ts.hsgp_trend":
                entry["manifest"]["capabilities"]["conditions_on_actuals"] = flag
        row.catalog = catalog
    ctx.catalogs.invalidate()


def _call(ys: list[Any], *, start: int = 1, periods: int = 8, **extra: Any) -> dict[str, Any]:
    months = [f"2024-{start + k:02d}-01" for k in range(len(ys))]
    return {
        "mode": "forecast",
        "horizon": {"dim": "t", "periods": periods},
        "data": {
            "coords": {"t": {"dims": ["t"], "data": months}},
            "data_vars": {"y": {"dims": ["t"], "data": ys}},
        },
        **extra,
    }


async def _runs(ctx: ServerContext) -> int:
    async with ctx.db.session() as s:
        return int((await s.execute(select(func.count()).select_from(m.Run))).scalar_one())


async def _codes(client: httpx.AsyncClient, body: dict[str, Any]) -> set[tuple[str, str]]:
    v = (await client.post(f"{ALIAS}/validate", json=body)).json()
    return {(i["code"], i["severity"]) for i in v["issues"]}


async def test_actuals_a_model_cannot_use_are_refused_before_a_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await _endpoint(client, ctx)
    body = _call([417.0, 391.0, 419.0])
    v = (await client.post(f"{ALIAS}/validate", json=body)).json()
    assert not v["valid"]
    [issue] = [i for i in v["issues"] if i["code"] == "ACTUALS_NOT_SUPPORTED"]
    assert issue["pointer"] == "/data"
    assert "3 periods (2024-01-01 to 2024-03-01)" in issue["message"]
    assert "2023-12-01" in issue["message"] and "Refit" in issue["suggested_action"]
    before = await _runs(ctx)
    r = await client.post(f"{ALIAS}/score", json=body, headers={"Prefer": "wait=1"})
    assert r.status_code == 422 and r.json()["code"] == "ACTUALS_NOT_SUPPORTED", r.text
    assert r.json()["type"].endswith("ACTUALS_NOT_SUPPORTED")
    assert await _runs(ctx) == before  # nothing was started (and nothing ignored)
    # predict and evaluate payloads may carry targets: they are not actuals
    predict = {**body, "mode": "predict"}
    predict.pop("horizon")
    assert ("ACTUALS_NOT_SUPPORTED", "error") not in await _codes(client, predict)


async def test_dates_to_forecast_with_an_empty_target_are_fine(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await _endpoint(client, ctx)
    v = (await client.post(f"{ALIAS}/validate", json=_call([None] * 8))).json()
    assert v["valid"], v["issues"]
    # the payload lists 3 periods: horizon.periods (8) is unused, and the answer says so
    v = (await client.post(f"{ALIAS}/validate", json=_call([None] * 3))).json()
    assert v["valid"]
    [warn] = [i for i in v["issues"] if i["pointer"] == "/horizon/periods"]
    assert warn["severity"] == "warning" and "not used" in warn["message"]


async def test_a_capable_plugin_build_accepts_and_checks_actuals(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await _endpoint(client, ctx)
    await _capable(ctx)
    # actuals then the dates to forecast
    v = await client.post(f"{ALIAS}/validate", json=_call([417.0, 391.0, 419.0, None, None]))
    assert v.json()["valid"], v.json()
    # this model accepts a missing target: a gap is filtered as a missing observation
    gap, late = _call([417.0, None, 419.0]), _call([None, None, 417.0])
    assert (await client.post(f"{ALIAS}/validate", json=gap)).json()["valid"]
    assert (await client.post(f"{ALIAS}/validate", json=late)).json()["valid"]
    # coordinates that skip periods are no gap in the actuals but a broken extension
    skipped = await _codes(client, _call([417.0], start=3))
    assert ("NON_CONTIGUOUS_EXTENSION", "error") in skipped
    # a model that needs a complete history refuses both
    async with ctx.db.session() as s:
        mv = (await s.execute(select(m.ModelVersion))).scalars().one()
        meta = json.loads(json.dumps(mv.score_meta))
        meta["resolved"]["model"]["input_schema"]["vars"]["y"]["missing"] = "forbid"
        mv.score_meta = meta
    assert ("ACTUALS_INVALID", "error") in await _codes(client, gap)
    assert ("ACTUALS_INVALID", "error") in await _codes(client, late)
    # revisions inside the fitted range are fine
    revised = _call([401.0, 417.0], periods=4)
    revised["data"]["coords"]["t"]["data"] = ["2023-12-01", "2024-01-01"]
    assert (await client.post(f"{ALIAS}/validate", json=revised)).json()["valid"]
    # the score call is accepted (it runs in the sandbox, which checks every series again)
    r = await client.post(f"{ALIAS}/score", json=_call([417.0, 391.0, 419.0]))
    assert r.status_code == 202, r.text
    async with ctx.db.session() as s:
        run = await s.get(m.Run, r.json()["run"]["id"])
        assert run is not None and "inline.data" in run.inputs
        assert run.payload["request"]["mode"] == "forecast"


async def test_the_schema_and_snippets_of_a_capable_endpoint(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await _endpoint(client, ctx)
    plain = EndpointSchema.model_validate((await client.get(f"{ALIAS}/schema")).json())
    assert not plain.snapshot.conditions_on_actuals and plain.actuals_example is None
    assert plain.input_table is not None and not plain.input_table.conditions_on_actuals
    assert plain.input_table.header == ["t", "y"]
    await _capable(ctx)
    schema = EndpointSchema.model_validate((await client.get(f"{ALIAS}/schema")).json())
    assert schema.snapshot.conditions_on_actuals  # live: the stored snapshot predates it
    assert schema.example == plain.example
    example = schema.actuals_example
    assert example is not None
    call = EndpointScoreCall.model_validate(example)
    assert call.mode == "forecast" and call.future_covariates.value == "carry_last"
    assert example["data"]["coords"]["t"]["data"][0] == "2024-01-01"  # type: ignore[index,call-overload]
    assert (await client.post(f"{ALIAS}/validate", json=example)).json()["valid"]
    table = schema.input_table
    assert table is not None and table.conditions_on_actuals
    assert table.rows[0] == ["2024-01-01", None] and len(table.rows) == 8
    snippets = (await client.get(f"{ALIAS}/snippets")).json()["items"]
    by = {s["language"]: s["code"] for s in snippets}
    assert "api.endpoints.forecast('trend-prod', rows" in by["python"]
    assert "function scoreTrendProdInputSheet()" in by["apps_script"]
    assert '"2024-01-01"' in by["curl"] and "conditioning" in by["curl"]
    # the plugin build that scores lost the capability again: the API says so at once
    await _capable(ctx, False)
    again = EndpointSchema.model_validate((await client.get(f"{ALIAS}/schema")).json())
    assert not again.snapshot.conditions_on_actuals and again.actuals_example is None
    codes = await _codes(client, _call([417.0]))
    assert ("ACTUALS_NOT_SUPPORTED", "error") in codes


async def test_a_dataset_payload_with_target_values_is_checked_too(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """A dataset reference's stored summary says where its targets hold values (datasets
    ingested since the summariser records it): the same static refusal applies."""
    mv = await fitted_version(ctx)
    async with ctx.db.session() as s:
        dv = (await s.execute(select(m.DatasetVersion))).scalars().first()
        assert dv is not None
        summary = json.loads(json.dumps(dv.summary))
        summary["coords"]["t"].update(start="2024-01-01", end="2024-03-01", size=3)
        summary["observed"] = {"y": {"t": dict(summary["coords"]["t"])}}
        dv.summary = summary
    request = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "model://default/trend@v1",
        "mode": "forecast",
        "horizon": {"dim": "t", "periods": 4},
        "data": "dataset://default/co2@latest",
    }
    r = await client.post(f"/v1/models/{mv.model_id}/versions/1:score", json={"request": request})
    assert r.status_code == 422 and r.json()["code"] == "ACTUALS_NOT_SUPPORTED", r.text


async def test_promoting_a_version_that_cannot_use_actuals_is_a_breaking_change(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """Callers of an endpoint that conditions on actuals send them; a version whose plugin
    build refuses them breaks those calls, so the endpoint's schema_change policy decides."""
    v1 = await fitted_version(ctx)
    v2 = await refit_version(ctx, v1)
    await _capable(ctx)
    r = await client.put(ALIAS, json={"model_version_id": v1.id})
    assert r.status_code == 200 and r.json()["schema_snapshot"]["conditions_on_actuals"]
    # v2 was fitted with a plugin version the catalog does not list: its fitted capabilities
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, v2.id)
        assert mv is not None
        meta = json.loads(json.dumps(mv.score_meta))
        meta["resolved"]["model"]["resolved"] = "ts.hsgp_trend@1.0.1"
        mv.score_meta = meta
    r = await client.put(ALIAS, json={"model_version_id": v2.id})
    assert r.status_code == 409 and r.json()["code"] == "PROMOTION_BLOCKED", r.text
    assert "ACTUALS_NOT_SUPPORTED" in r.json()["errors"][0]["message"]
    r = await client.put(
        ALIAS, json={"model_version_id": v2.id, "evaluation_policy": {"schema_change": "allow"}}
    )
    assert r.status_code == 200 and not r.json()["schema_snapshot"]["conditions_on_actuals"]


async def test_text_in_a_numeric_target_is_refused_before_a_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await _endpoint(client, ctx)
    await _capable(ctx)
    before = await _runs(ctx)
    r = await client.post(f"{ALIAS}/score", json=_call(["417", None]))
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID", r.text
    assert "'y' holds text" in r.json()["errors"][0]["message"]
    assert await _runs(ctx) == before


async def test_a_model_fitted_in_a_transformed_space_refuses_actuals(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """A spec labelled as fitted on non-replayable transform output (a workflow's
    transform.log): the version's score metadata says so, the endpoint does not offer actuals
    even when its plugin build could condition on them, and they are refused before a run."""
    from mapi_server_testkit.builders import TREND_SPEC
    from mapi_spec.actuals import DATA_SPACE_LABEL, TRANSFORMED_SPACE

    labelled = TREND_SPEC.replace(
        "metadata: {name: trend, project: default}",
        f"metadata: {{name: trend, project: default, labels: {{{DATA_SPACE_LABEL}: "
        f"{TRANSFORMED_SPACE}}}}}",
    )
    mv = await fitted_version(ctx, yaml=labelled)
    assert mv.score_meta is not None and mv.score_meta["data_space"] == "transformed"
    await _capable(ctx)
    r = await client.put(ALIAS, json={"model": "model://default/trend@v1"})
    assert r.status_code == 200, r.text
    schema = EndpointSchema.model_validate((await client.get(f"{ALIAS}/schema")).json())
    assert not schema.snapshot.conditions_on_actuals and schema.actuals_example is None
    v = (await client.post(f"{ALIAS}/validate", json=_call([417.0]))).json()
    [err] = [i for i in v["issues"] if i["code"] == "ACTUALS_NOT_SUPPORTED"]
    assert "transform" in err["message"]
    plain = (await client.post(f"{ALIAS}/validate", json=_call([None] * 8))).json()
    assert plain["valid"]
    assert any("transformed space" in i["message"] for i in plain["issues"])
