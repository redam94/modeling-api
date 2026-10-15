"""The whole local stack end to end: the server with the real GCS shim (``ShimObjectStore``),
``LocalProcessExecutor`` and the real ``mapi-runner`` supervisor + worker.

Nothing is faked: the catalog comes from a sandboxed ``describe`` run that the orchestrator
queues on start, the upload goes through a signed PUT to the shim, ingest parses the CSV in
the runner, the fit and score runs read generation-pinned signed GETs and write through the
POST policy, and the finalizer verifies every manifest against the shim's server-computed
CRC32C/MD5 before registering the model version.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from mapi_conformance.fixtures.xgb_store_demand import data as xgb
from mapi_gcs_shim.server import ShimServer
from mapi_server.context import ServerContext
from mapi_server.executors import LocalProcessExecutor
from mapi_server.orchestrator import Orchestrator
from mapi_server.settings import ObjectStoreKind, Settings
from mapi_spec.storage import crc32c, crc32c_to_base64

pytestmark = [pytest.mark.integration, pytest.mark.slow]

DATASET = "store_sku_weekly"


@pytest.fixture
def settings(settings: Settings, gcs_shim: ShimServer) -> Settings:
    """The suite's settings, pointed at a real shim and without a pre-exported catalog."""
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


def _spec_yaml() -> str:
    doc: dict[str, Any] = yaml.safe_load((Path(xgb.__file__).parent / "spec.yaml").read_text())
    doc["metadata"] = {"name": "store-demand", "project": "default"}
    doc["data"]["dataset"] = f"dataset://default/{DATASET}@latest"
    doc["model"]["settings"]["estimator"]["params"] = {"n_estimators": 20, "max_depth": 3}
    return yaml.safe_dump(doc, sort_keys=False)


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


async def test_ingest_fit_score_with_the_real_runner_and_shim(
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
        # ------------------------------------------------------------ catalog via describe
        catalog = await _catalog(client, 300)
        assert any(e["manifest"]["id"] == "tabular.regressor" for e in catalog["models"])

        # ------------------------------------------------- upload (signed PUT) and ingest
        data, _ = xgb.generate(0, "tiny")
        schema = xgb.schema()
        csv = data.to_dataframe().reset_index().to_csv(index=False).encode()
        ds = (await client.post("/v1/datasets", json={"name": DATASET})).json()
        up = await client.post(
            f"/v1/datasets/{ds['id']}/uploads",
            json={"filename": "weekly.csv", "content_type": "text/csv", "size_bytes": len(csv)},
        )
        assert up.status_code == 201, up.text
        session = up.json()
        async with httpx.AsyncClient() as http:
            put = await http.put(
                session["upload"]["url"], content=csv, headers=session["upload"]["headers"]
            )
            assert put.status_code == 200, put.text
        mapping = {
            "format": "csv",
            "dims": {
                d: {"column": d, "spec": s.model_dump(mode="json", exclude_none=True)}
                for d, s in schema.dims.items()
            },
            "vars": {
                v: {"column": v, "role": s.role.value, "missing": s.missing}
                for v, s in schema.vars.items()
            },
        }
        dv = await client.post(
            f"/v1/datasets/{ds['id']}/versions",
            json={"upload_id": session["id"], "mapping": mapping},
        )
        assert dv.status_code == 202, dv.text
        ingest = await _wait(client, dv.json()["ingest_run_id"], 300)
        assert ingest["state"] == "succeeded", ingest
        version = (await client.get(f"/v1/datasets/{ds['id']}/versions/1")).json()
        assert version["status"] == "ready" and version["sha256"].startswith("sha256:")

        # ---------------------------------------------------------------------------- fit
        spec = await client.post("/v1/specs", json={"yaml": _spec_yaml()})
        assert spec.status_code == 201, spec.text
        assert spec.json()["latest"]["valid"], spec.json()["latest"]["issues"]
        run = await client.post(
            "/v1/runs",
            json={"kind": "fit", "spec_id": spec.json()["id"], "revision": 1},
            headers={"Idempotency-Key": "e2e-fit-1"},
        )
        assert run.status_code == 201, run.text
        fit = await _wait(client, run.json()["id"], 600)
        assert fit["state"] == "succeeded", fit
        model_id = (await client.get("/v1/models")).json()["items"][0]["id"]
        mv = (await client.get(f"/v1/models/{model_id}/versions/1")).json()
        assert mv["status"] == "ready"
        names = {a["name"] for a in mv["artifacts"]}
        assert {"bundle.zarr.zip", "coords.json", "metrics.json", "plots.json"} <= names
        assert "rmse" in mv["metrics"]
        plots = (await client.get(f"/v1/models/{model_id}/versions/1/plots")).json()
        assert plots["items"], "the fit registered no plots"

        # --------------------------------- artifact download through a signed GET (shim)
        link = (await client.get(f"/v1/models/{model_id}/versions/1/artifacts/metrics.json")).json()
        async with httpx.AsyncClient() as http:
            body = await http.get(link["url"])
        assert body.status_code == 200
        assert crc32c_to_base64(crc32c(body.content)) == link["crc32c"]

        # -------------------------------------------------------------------------- score
        request = {
            "request": {
                "apiVersion": "mapi/v1",
                "kind": "ScoreRequest",
                "model": "model://default/store-demand@v1",
                "mode": "predict",
                "data": f"dataset://default/{DATASET}@latest",
            }
        }
        r = await client.post(
            f"/v1/models/{model_id}/versions/1:score",
            json=request,
            headers={"Prefer": "wait=60", "Idempotency-Key": "e2e-score-1"},
        )
        assert r.status_code in (200, 202), r.text
        score = await _wait(client, r.json()["run"]["id"], 300)
        assert score["state"] == "succeeded", score
        replay = await client.post(
            f"/v1/models/{model_id}/versions/1:score",
            json=request,
            headers={"Prefer": "wait=0", "Idempotency-Key": "e2e-score-1"},
        )
        assert replay.status_code == 200 and replay.json()["done"], replay.text
        outputs = {o["name"] for o in replay.json()["outputs"]}
        assert "score.zarr.zip" in outputs

        # ----------------------------------------- evaluate (defaults to the fit dataset)
        ev = await client.post(f"/v1/models/{model_id}/versions/1:evaluate", json={"params": {}})
        assert ev.status_code == 202, ev.text
        evaluated = await _wait(client, ev.json()["id"], 300)
        assert evaluated["state"] == "succeeded", evaluated
    finally:
        orch.stop.set()
        await asyncio.wait_for(task, 60)
