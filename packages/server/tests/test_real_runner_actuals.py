"""Forecasts with actuals through the whole local stack: the real GCS shim, the
``LocalProcessExecutor`` and the real ``mapi-runner``, with a ``ts.sarimax`` endpoint fitted
on 144 monthly observations (1949-01 to 1960-12), as on a developer's stack.

* A plain forecast answers with the conditioning record inline (``conditioned: false``, the end
  of the fitted history).
* Dates to forecast with the target empty are forecast exactly (``horizon_source: rows``).
* Target values are actuals: ``ts.sarimax`` conditions on them with its fitted parameters,
  so the forecast starts after the last actual and moves with the actuals - never the old
  silent ignore (the same numbers for the supplied months, the horizon dropped). Models that
  cannot condition are refused before a run (``test_endpoints_actuals.py``).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest
from mapi_gcs_shim.server import ShimServer
from mapi_server.context import ServerContext
from mapi_server.executors import LocalProcessExecutor
from mapi_server.orchestrator import Orchestrator
from mapi_server.settings import ObjectStoreKind, Settings

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SPEC = """\
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: airline, project: default}
data:
  dataset: dataset://default/airline@latest
  split: {kind: time_holdout, dim: t, last: 12}
model:
  use: ts.sarimax@^1
  settings: {order: [1, 1, 0]}
  fit: {mode: evaluate_then_refit, seed: 1}
evaluate: {metrics: [rmse]}
"""


@pytest.fixture
def settings(settings: Settings, gcs_shim: ShimServer) -> Settings:
    return settings.model_copy(
        update={
            "object_store": ObjectStoreKind.SHIM,
            "gcs_endpoint": gcs_shim.endpoint,
            "gcs_root": gcs_shim.root,
            "exec_token": None,
            "catalog_path": None,
            "complete_event_grace_s": 30.0,
            "lost_execution_grace_s": 30.0,
            "reconcile_interval_s": 0.2,
        }
    )


async def _wait(client: httpx.AsyncClient, run_id: str, timeout: float) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        run: dict[str, Any] = (await client.get(f"/v1/runs/{run_id}")).json()
        if run["state"] in ("succeeded", "failed", "partial", "cancelled", "rejected"):
            return run
        await asyncio.sleep(0.5)
    raise AssertionError(f"run {run_id} did not finish in {timeout}s")


async def _catalog(client: httpx.AsyncClient, timeout: float) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        r = await client.get("/v1/catalog")
        if r.status_code == 200:
            body: dict[str, Any] = r.json()
            return body
        await asyncio.sleep(0.5)
    raise AssertionError("the describe run did not produce a catalog in time")


async def _score(client: httpx.AsyncClient, body: dict[str, Any], key: str) -> httpx.Response:
    r = await client.post(
        "/v1/endpoints/ts-forecast/score",
        json=body,
        headers={"Prefer": "wait=60", "Idempotency-Key": key},
    )
    for _ in range(20):  # collect a slow run with the same key
        if r.status_code != 202:
            break
        r = await client.post(
            "/v1/endpoints/ts-forecast/score",
            json=body,
            headers={"Prefer": "wait=30", "Idempotency-Key": key},
        )
    return r


def _months(first: str, n: int) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(first, periods=n, freq="MS")]


def _inline(ys: list[Any]) -> dict[str, Any]:
    """Inline data from 1961-01 on: ``y`` per month (``None``: a month to forecast)."""
    return {
        "coords": {"t": {"dims": ["t"], "data": _months("1961-01-01", len(ys))}},
        "data_vars": {"y": {"dims": ["t"], "data": ys}},
    }


async def test_a_sarimax_endpoint_forecasts_with_dates_and_answers_for_actuals(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    executor = LocalProcessExecutor(
        command=[sys.executable, "-m", "mapi_runner"],
        bootstrap_url_template=ctx.settings.bootstrap_url_template(),
        workdir=ctx.settings.workdir,
        exec_token=ctx.settings.resolved_exec_token(),
    )
    orch = Orchestrator(ctx, executor, use_procrastinate=False)
    task = asyncio.create_task(orch.run())
    try:
        catalog = await _catalog(client, 300)
        entry = next(e for e in catalog["models"] if e["manifest"]["id"] == "ts.sarimax")
        assert entry["manifest"]["capabilities"].get("conditions_on_actuals") is True

        # ---------------------------------------------- 144 monthly observations, ingested
        rng = np.random.default_rng(3)
        t = np.arange(144)
        y = np.round(120 + 2.2 * t + 25 * np.sin(2 * np.pi * t / 12) + rng.normal(0, 6, 144))
        frame = pd.DataFrame({"t": _months("1949-01-01", 144), "y": y})
        csv = frame.to_csv(index=False).encode()
        ds = (await client.post("/v1/datasets", json={"name": "airline"})).json()
        up = await client.post(
            f"/v1/datasets/{ds['id']}/uploads",
            json={"filename": "airline.csv", "content_type": "text/csv", "size_bytes": len(csv)},
        )
        assert up.status_code == 201, up.text
        async with httpx.AsyncClient() as http:
            put = await http.put(
                up.json()["upload"]["url"], content=csv, headers=up.json()["upload"]["headers"]
            )
            assert put.status_code == 200, put.text
        mapping = {
            "format": "csv",
            "dims": {"t": {"column": "t",
                           "spec": {"kind": "time", "policy": "extend", "freq": "MS"}}},
            "vars": {"y": {"column": "y", "role": "target", "missing": "forbid"}},
        }  # fmt: skip
        dv = await client.post(
            f"/v1/datasets/{ds['id']}/versions",
            json={"upload_id": up.json()["id"], "mapping": mapping},
        )
        assert dv.status_code == 202, dv.text
        ingest = await _wait(client, dv.json()["ingest_run_id"], 300)
        assert ingest["state"] == "succeeded", ingest

        # ------------------------------------------------------------ fit and promote
        spec = await client.post("/v1/specs", json={"yaml": SPEC})
        assert spec.status_code == 201 and spec.json()["latest"]["valid"], spec.text
        run = await client.post(
            "/v1/runs", json={"kind": "fit", "spec_id": spec.json()["id"], "revision": 1}
        )
        assert run.status_code == 201, run.text
        fit = await _wait(client, run.json()["id"], 600)
        assert fit["state"] == "succeeded", fit
        r = await client.put(
            "/v1/endpoints/ts-forecast",
            json={"model": "model://default/airline@v1", "override": True,
                  "reason": "an end-to-end test of forecasts with actuals"},
        )  # fmt: skip
        assert r.status_code == 200, r.text
        schema = (await client.get("/v1/endpoints/ts-forecast/schema")).json()
        assert schema["snapshot"]["conditions_on_actuals"] is True
        assert schema["actuals_example"]["data"]["coords"]["t"]["data"][0] == "1961-01-01"
        assert schema["input_table"]["rows"][0][0] == "1961-01-01"

        # ------------------------------------- a plain forecast says what it started from
        plain = {"mode": "forecast", "horizon": {"dim": "t", "periods": 8},
                 "future_covariates": "carry_last",
                 "output": {"summary": ["median", "q5", "q95"]}}  # fmt: skip
        r = await _score(client, plain, "plain")
        assert r.status_code == 200, r.text
        answer = r.json()
        assert answer["run"]["state"] == "succeeded", answer["run"]
        cond = answer["summary"]["conditioning"]
        assert cond["conditioned"] is False and cond["fit_end"] == "1960-12-01"
        assert cond["periods"] == 8 and cond["horizon_source"] == "horizon"
        assert "no actuals were supplied" in cond["message"]
        [table] = answer["summary"]["tables"]
        assert [row[0] for row in table["rows"]][:1] == ["1961-01-01"]
        assert len(table["rows"]) == 8

        # ------------------------------ dates to forecast (target empty): exactly those
        dates = {**plain, "data": _inline([None] * 5)}
        r = await _score(client, dates, "dates")
        assert r.status_code == 200, r.text
        cond = r.json()["summary"]["conditioning"]
        assert cond["conditioned"] is False and cond["horizon_source"] == "rows"
        assert cond["periods"] == 5
        assert len(r.json()["summary"]["tables"][0]["rows"]) == 5

        # --------------------------------------------- actuals: used, never ignored
        def summary(resp: httpx.Response) -> tuple[dict[str, Any], list[list[Any]]]:
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["run"]["state"] == "succeeded", body["run"]
            return body["summary"]["conditioning"], body["summary"]["tables"][0]["rows"]

        cond, rows = summary(await _score(client, {**plain, "data": _inline([417, 391, 419])}, "a"))
        assert cond["conditioned"] and cond["n_actuals"] == 3 and cond["revised"] == 0
        assert cond["actuals_through"] == "1961-03-01" and cond["horizon_source"] == "horizon"
        assert cond["series"][0]["forecast_from"] == "1961-04-01" and cond["periods"] == 8
        assert [row[0] for row in rows] == _months("1961-01-01", 3 + 8)
        wild, wild_rows = summary(
            await _score(client, {**plain, "data": _inline([9999, 1, 50000])}, "b")
        )
        assert wild["actuals_through"] == "1961-03-01"
        assert rows[3:] != wild_rows[3:]  # different actuals, a different forecast
        # actuals plus listed dates: exactly those dates, the unused periods flagged
        listed, listed_rows = summary(
            await _score(client, {**plain, "data": _inline([417, 391, 419, None, None])}, "c")
        )
        assert listed["horizon_source"] == "rows" and listed["periods"] == 2
        assert [row[0] for row in listed_rows] == _months("1961-01-01", 5)
        np.testing.assert_allclose(
            [float(v) for v in listed_rows[3][1:]], [float(v) for v in rows[3][1:]], rtol=1e-9
        )
    finally:
        orch.stop.set()
        await asyncio.wait_for(task, 60)
