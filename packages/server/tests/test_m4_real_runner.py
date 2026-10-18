"""M4 tree datasets end to end with the real runner: the server with the real GCS shim,
``LocalProcessExecutor`` and the real ``mapi-runner`` supervisor + worker.

Two NetCDF files (one store each) are concatenated along ``store_id`` into node ``/sales``
and a raw HDF5 file (no dim metadata) is mapped into node ``/promo``: one multi-file version
of layout ``tree``. The raw file is inspected first (as a user would before writing its
mapping). A spec then selects both nodes with ``data.nodes``, is validated statically and in
the sandbox, fitted with a small gradient-boosting model and scored on the tree version.
Finally an HDF5 file with an external link is refused by the runner's HDF5 guard.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import h5py
import httpx
import numpy as np
import pytest
import yaml
from mapi_conformance.fixtures.xgb_store_demand import data as xgb
from mapi_gcs_shim.server import ShimServer
from mapi_server.context import ServerContext
from mapi_server.executors import LocalProcessExecutor
from mapi_server.orchestrator import Orchestrator
from mapi_server.settings import ObjectStoreKind, Settings

pytestmark = [pytest.mark.integration, pytest.mark.slow]

DATASET = "store_tree"


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


def _files(tmp: Path) -> tuple[list[str], list[Path]]:
    """Two stores' NetCDF files and one raw HDF5 file with every store's promotions."""
    data, _ = xgb.generate(0, "tiny")
    data = data.isel(store_id=[0, 1])
    stores = [str(s) for s in data.store_id.values]
    paths: list[Path] = []
    for i, store in enumerate(stores):
        path = tmp / f"sales_{i}.nc"
        part = data[["units_sold", "price"]].sel(store_id=store, drop=True)
        part.to_netcdf(path, engine="h5netcdf")
        paths.append(path)
    raw = tmp / "promo.h5"
    with h5py.File(raw, "w") as f:
        f["promo/flag"] = data["promo_flag"].transpose("store_id", "sku", "week").values
        f["axes/store"] = np.array(stores, dtype=h5py.string_dtype())
        f["axes/sku"] = np.array([str(s) for s in data.sku.values], dtype=h5py.string_dtype())
        f["notes/unused"] = np.zeros(3)
    paths.append(raw)
    return stores, paths


def _first_week() -> str:
    data, _ = xgb.generate(0, "tiny")
    return str(data.week.values[0])[:10]


def _spec_yaml() -> str:
    doc: dict[str, Any] = yaml.safe_load((Path(xgb.__file__).parent / "spec.yaml").read_text())
    doc["metadata"] = {"name": "store-tree", "project": "default"}
    doc["data"]["dataset"] = f"dataset://default/{DATASET}@latest"
    doc["data"]["nodes"] = {"sales": {"path": "/sales"}, "promo": {"path": "/promo"}}
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


async def _catalog(client: httpx.AsyncClient, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if (await client.get("/v1/catalog")).status_code == 200:
            return
        await asyncio.sleep(0.5)
    raise AssertionError("the describe run did not produce a catalog in time")


async def _upload(client: httpx.AsyncClient, dataset_id: str, path: Path, ctype: str) -> str:
    content = path.read_bytes()
    r = await client.post(
        f"/v1/datasets/{dataset_id}/uploads",
        json={"filename": path.name, "content_type": ctype, "size_bytes": len(content)},
    )
    assert r.status_code == 201, r.text
    session = r.json()
    async with httpx.AsyncClient() as http:
        put = await http.put(
            session["upload"]["url"], content=content, headers=session["upload"]["headers"]
        )
        assert put.status_code == 200, put.text
    return str(session["id"])


async def test_a_multi_file_tree_version_is_fitted_with_the_real_runner(
    client: httpx.AsyncClient, ctx: ServerContext, tmp_path: Path
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
        await _catalog(client, 300)
        stores, (nc0, nc1, raw) = _files(tmp_path)
        ds = (await client.post("/v1/datasets", json={"name": DATASET})).json()
        base = f"/v1/datasets/{ds['id']}"
        u0 = await _upload(client, ds["id"], nc0, "application/x-netcdf")
        u1 = await _upload(client, ds["id"], nc1, "application/x-netcdf")
        u2 = await _upload(client, ds["id"], raw, "application/x-hdf5")

        # ------------------------------------------- inspect the raw HDF5 file (sandboxed)
        r = await client.get(f"{base}/uploads/{u2}/inspection")
        assert r.status_code == 404, r.text  # never requested
        r = await client.post(f"{base}/uploads/{u2}/inspect")
        assert r.status_code == 202, r.text
        assert r.json()["kind"] == "inspect"
        inspect_run = await _wait(client, r.json()["id"], 300)
        assert inspect_run["state"] == "succeeded", inspect_run
        r = await client.get(f"{base}/uploads/{u2}/inspection")
        assert r.status_code == 200, r.text
        inspection = r.json()
        assert inspection["format"] == "hdf5" and not inspection["truncated"]
        arrays = {a["path"]: a for a in inspection["arrays"]}
        assert arrays["/promo/flag"]["shape"] == [2, 3, 60]
        assert arrays["/promo/flag"]["dims_hint"] is None  # raw: no dim metadata
        assert {"/axes/store", "/axes/sku", "/notes/unused"} <= set(arrays)
        # a finished inspection is returned as is
        again = await client.post(f"{base}/uploads/{u2}/inspect")
        assert again.json()["id"] == inspect_run["id"]

        # ------------------------------------ one version from three files (assembly)
        nc_map = {
            "format": "netcdf",
            "vars": {
                "units_sold": {"column": "units_sold", "role": "target"},
                "price": {"column": "price", "missing": "allow"},
            },
        }
        h5_map = {
            "format": "hdf5",
            "hdf5": {
                "vars": [
                    {
                        "path": "/promo/flag",
                        "name": "promo_flag",
                        "dims": ["store_id", "sku", "week"],
                    }
                ],
                "coords": [
                    {"dim": "store_id", "path": "/axes/store"},
                    {"dim": "sku", "path": "/axes/sku"},
                    {"dim": "week", "start": _first_week(), "step": "W-SUN"},
                ],
            },
        }
        concat = {"node": "/sales", "combine": "concat", "concat_dim": "store_id"}
        assembly = {
            "sources": [
                {"upload_id": u0, "mapping": nc_map, **concat, "concat_label": stores[0]},
                {"upload_id": u1, "mapping": nc_map, **concat, "concat_label": stores[1]},
                {"upload_id": u2, "mapping": h5_map, "node": "/promo"},
            ]
        }
        dv = await client.post(f"{base}/versions", json={"assembly": assembly})
        assert dv.status_code == 202, dv.text
        ingest = await _wait(client, dv.json()["ingest_run_id"], 300)
        assert ingest["state"] == "succeeded", ingest
        version = (await client.get(f"{base}/versions/1")).json()
        assert version["status"] == "ready", version
        assert version["layout"] == "tree"
        assert version["sources"] == [u0, u1, u2]
        tree = version["tree_schema"]["nodes"]
        assert list(tree) == ["/", "/sales", "/promo"]
        assert set(tree["/sales"]["vars"]) == {"units_sold", "price"}
        assert set(tree["/promo"]["vars"]) == {"promo_flag"}
        assert tree["/sales"]["vars"]["units_sold"]["role"] == "target"
        assert tree["/sales"]["dims"]["week"] == tree["/promo"]["dims"]["week"]
        # the root's schema (empty here), for readers that predate trees
        assert not version["data_schema"]["vars"]
        # the tree's data object and schema download through signed GETs
        async with httpx.AsyncClient() as http:
            for name, magic in (("data.tree.zip", b"PK"), ("tree_schema.json", b"{")):
                r = await client.get(f"{base}/versions/1/artifacts/{name}")
                assert r.status_code == 200, r.text
                body = await http.get(r.json()["url"])
                assert body.status_code == 200 and body.content.startswith(magic), name
        # the inspection outlives the upload it described
        assert (await client.get(f"{base}/uploads/{u2}/inspection")).status_code == 200

        # ------------------------------------------ static and sandboxed validation
        spec_yaml = _spec_yaml()
        static = await client.post("/v1/specs:validate", json={"yaml": spec_yaml})
        assert static.status_code == 200, static.text
        assert static.json()["valid"], static.json()["issues"]
        no_nodes = yaml.safe_load(spec_yaml)
        del no_nodes["data"]["nodes"]
        r = await client.post("/v1/specs:validate", json={"document": no_nodes})
        assert r.status_code == 200 and not r.json()["valid"], r.text
        assert any(i["pointer"].startswith("/data") for i in r.json()["issues"])

        spec = await client.post("/v1/specs", json={"yaml": spec_yaml})
        assert spec.status_code == 201, spec.text
        assert spec.json()["latest"]["valid"], spec.json()["latest"]["issues"]
        spec_id = spec.json()["id"]
        r = await client.post(
            "/v1/runs", json={"kind": "validate", "spec_id": spec_id, "revision": 1}
        )
        assert r.status_code == 201, r.text
        validated = await _wait(client, r.json()["id"], 300)
        assert validated["state"] == "succeeded", validated

        # -------------------------------------------------------------------------- fit
        r = await client.post(
            "/v1/runs",
            json={"kind": "fit", "spec_id": spec_id, "revision": 1},
            headers={"Idempotency-Key": "m4-e2e-fit"},
        )
        assert r.status_code == 201, r.text
        fit = await _wait(client, r.json()["id"], 600)
        assert fit["state"] == "succeeded", fit
        model_id = (await client.get("/v1/models")).json()["items"][0]["id"]
        mv = (await client.get(f"/v1/models/{model_id}/versions/1")).json()
        assert mv["status"] == "ready", mv
        assert "rmse" in mv["metrics"]

        # ------------------------------------- score on the tree version (nodes of the fit)
        request = {
            "request": {
                "apiVersion": "mapi/v1",
                "kind": "ScoreRequest",
                "model": "model://default/store-tree@v1",
                "mode": "predict",
                "data": f"dataset://default/{DATASET}@latest",
            }
        }
        r = await client.post(
            f"/v1/models/{model_id}/versions/1:score",
            json=request,
            headers={"Prefer": "wait=60", "Idempotency-Key": "m4-e2e-score"},
        )
        assert r.status_code in (200, 202), r.text
        score = await _wait(client, r.json()["run"]["id"], 300)
        assert score["state"] == "succeeded", score

        # ------------------------------------- an unsafe HDF5 file is refused in the sandbox
        evil = tmp_path / "evil.nc"
        with h5py.File(evil, "w") as f:
            f["x"] = np.arange(3.0)
            f["passwd"] = h5py.ExternalLink("/etc/passwd", "/")
        u3 = await _upload(client, ds["id"], evil, "application/x-netcdf")
        dv = await client.post(
            f"{base}/versions", json={"upload_id": u3, "mapping": {"format": "netcdf"}}
        )
        assert dv.status_code == 202, dv.text
        refused = await _wait(client, dv.json()["ingest_run_id"], 300)
        assert refused["state"] == "failed", refused
        # the worker's code travels in the detail; its text only as untrusted text
        assert "INGEST_UNSAFE_FILE" in refused["problem"]["detail"], refused
        assert "external link" in refused["problem"]["untrusted_text"]
        version = (await client.get(f"{base}/versions/2")).json()
        assert version["status"] == "failed", version
    finally:
        orch.stop.set()
        await asyncio.wait_for(task, 60)
