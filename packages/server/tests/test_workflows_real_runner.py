"""Workflows end to end on the real local stack: the server with the GCS shim,
``LocalProcessExecutor`` and the real ``mapi-runner`` supervisor + worker for every node.

The catalog comes from a sandboxed ``describe`` run of the worker's test registry (the
stand-in statsmodels models of :mod:`mapi_workflows.testing`); every data node runs as a
``workflow_node`` run that reads its generation-pinned inputs through signed URLs and
writes ``node_result.json`` and its derived dataset, which the finalizer verifies and the
orchestrator registers; ``model.fit`` is a real ``fit`` run whose bundle a later node
(residual checks) loads in its own sandbox.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
import pytest
from mapi_gcs_shim.server import ShimServer
from mapi_server.context import ServerContext
from mapi_server.executors import LocalProcessExecutor
from mapi_server.orchestrator import Orchestrator
from mapi_server.settings import ObjectStoreKind, Settings
from mapi_server_testkit.workflows import put_dataset
from mapi_workflows.testing import weekly_series

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REGISTRY = "mapi_workflows.testing:test_registry"


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
            "dispatch_poll_s": 0.2,
            "workflow_tick_s": 0.3,
        }
    )


async def _until(client: httpx.AsyncClient, url: str, done: Any, timeout: float) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    body: Any = None
    while loop.time() < deadline:
        r = await client.get(url)
        if r.status_code == 200:
            body = r.json()
            if done(body):
                return body
        await asyncio.sleep(0.5)
    raise AssertionError(f"{url} did not finish in {timeout}s: {body}")


DOC = {
    "apiVersion": "mapi/v1",
    "kind": "Workflow",
    "metadata": {"name": "readiness", "project": "default"},
    "inputs": {"data": {"type": "dataset"}},
    "nodes": [
        {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
        {"id": "wanders", "use": "branch.if", "with": {"condition": "not nodes.adf.reject"}},
        {
            "id": "diff",
            "use": "transform.difference",
            "inputs": {"data": "inputs.data"},
            "when": "wanders.then",
        },
        {"id": "acf", "use": "test.acf_pacf", "inputs": {"data": ["diff.data", "inputs.data"]}},
        {
            "id": "fit",
            "use": "model.fit",
            "inputs": {"data": "inputs.data"},
            "with": {
                "model": "ts.sarimax@^1",
                "settings": {
                    "order": {"$expr": "[nodes.acf.suggested_p, if(nodes.wanders.value, 1, 0), 0]"},
                    "exog": ["price"],
                },
            },
        },
        {"id": "resid", "use": "evaluate.residuals", "inputs": {"model": "fit.model"}},
        {
            "id": "report",
            "use": "report.summary",
            "inputs": {
                "tests": ["adf.result", "acf.result"],
                "items": ["resid.table"],
                "model": "fit.model",
            },
        },
    ],
    "outputs": {"model": "fit.model", "report": "report.report"},
}


async def test_a_workflow_on_the_real_runner_and_shim(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    executor = LocalProcessExecutor(
        command=[sys.executable, "-m", "mapi_runner", "--worker-registry", REGISTRY],
        bootstrap_url_template=ctx.settings.bootstrap_url_template(),
        workdir=ctx.settings.workdir,
        exec_token=ctx.settings.resolved_exec_token(),
    )
    orch = Orchestrator(ctx, executor, use_procrastinate=False)
    task = asyncio.create_task(orch.run())
    try:
        catalog = await _until(client, "/v1/catalog", lambda b: bool(b.get("models")), 300)
        assert any(e["manifest"]["id"] == "ts.sarimax" for e in catalog["models"])
        await put_dataset(ctx, "sales", weekly_series(120, random_walk=True, beta=3.0))
        created = await client.post("/v1/workflows", json={"document": DOC})
        assert created.status_code == 201, created.text
        rev = (await client.get(f"/v1/workflows/{created.json()['id']}/revisions/1")).json()
        assert rev["validation"]["valid"], rev["validation"]["issues"]
        started = await client.post(
            f"/v1/workflows/{created.json()['id']}/runs",
            json={"inputs": {"data": "dataset://default/sales@latest"}},
            headers={"Idempotency-Key": "wf-e2e-1"},
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        view = await _until(
            client,
            f"/v1/workflow-runs/{run_id}",
            lambda b: b["status"] in ("succeeded", "failed", "cancelled"),
            900,
        )
        nodes = {n["node_id"]: n for n in view["nodes"]}
        assert view["status"] == "succeeded", {
            k: (v["status"], v.get("error")) for k, v in nodes.items()
        }
        assert nodes["adf"]["result"]["decision"] == "unit_root"
        assert nodes["diff"]["outputs"]["data"]["ref"] == "dataset://default/wf-readiness-diff@v1"
        assert nodes["acf"]["result"]["n_obs"] == 119
        model = view["outputs"]["model"]["ref"]
        assert model == "model://default/wf-readiness-fit@v1"
        assert nodes["fit"]["params"]["settings"]["order"][1] == 1
        assert isinstance(nodes["resid"]["result"]["white_noise"], bool)
        report = view["outputs"]["report"]["inline"]
        assert report["model"] == model and len(report["sections"]) >= 3
        for nid in ("adf", "diff", "acf", "resid"):
            child = (await client.get(f"/v1/runs/{nodes[nid]['run_id']}")).json()
            assert child["kind"] == "workflow_node" and child["state"] == "succeeded", child
        fit = (await client.get(f"/v1/runs/{nodes['fit']['run_id']}")).json()
        assert fit["kind"] == "fit" and fit["state"] == "succeeded"
        replay = await client.post(
            f"/v1/workflows/{created.json()['id']}/runs",
            json={"inputs": {"data": "dataset://default/sales@latest"}},
            headers={"Idempotency-Key": "wf-e2e-1"},
        )
        assert replay.status_code == 200 and replay.json()["run_id"] == run_id
    finally:
        orch.stop.set()
        await asyncio.wait_for(task, 60)


def _panel() -> Any:
    """``y(geo, time)`` driven by a covariate ``x`` (the layout of ``toy.bayes_linear``)."""
    import numpy as np
    import pandas as pd
    import xarray as xr
    from mapi_spec.schema import DataSchema

    rng = np.random.default_rng(0)
    t = pd.date_range("2020-01-05", periods=40, freq="W-SUN")
    x = rng.normal(size=(2, 40))
    y = np.array([[10.0], [12.0]]) + 1.5 * x + rng.normal(0, 1.0, size=(2, 40))
    ds = xr.Dataset(
        {"y": (("geo", "time"), y), "x": (("geo", "time"), x)},
        coords={"geo": ["a", "b"], "time": t},
    )
    dims = ["geo", "time"]
    ds.attrs["mapi_schema"] = DataSchema.model_validate(
        {
            "dims": {
                "geo": {"kind": "categorical"},
                "time": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
            },
            "vars": {
                "y": {"dims": dims, "dtype": "float64", "role": "target"},
                "x": {"dims": dims, "dtype": "float64", "role": "covariate"},
            },
        }
    ).model_dump_json(exclude_none=True)
    return ds


def _bayes_fit(nid: str, features: list[str]) -> dict[str, Any]:
    return {
        "id": nid,
        "use": "model.fit",
        "inputs": {"data": "inputs.data"},
        "with": {
            "model": "toy.bayes_linear@^1",
            "settings": {"features": features},
            "fit": {"draws": 200, "chains": 2, "seed": 1},
            "evaluate": {"metrics": ["elpd_loo"]},
        },
    }


LADDER = {
    "apiVersion": "mapi/v1",
    "kind": "Workflow",
    "metadata": {"name": "ladder", "project": "default"},
    "inputs": {"data": {"type": "dataset"}},
    "nodes": [
        _bayes_fit("plain", []),
        _bayes_fit("driver", ["x"]),
        {
            "id": "weights",
            "use": "compare.stacking",
            "inputs": {"candidates": ["plain.metrics", "driver.metrics"]},
        },
        {"id": "s", "use": "model.summary", "inputs": {"model": "driver.model"}},
        {"id": "ppc", "use": "evaluate.ppc", "inputs": {"model": "driver.model"}},
        {
            "id": "report",
            "use": "report.summary",
            "inputs": {"items": ["weights.table", "s.table"], "metrics": ["ppc.metrics"]},
        },
    ],
    "outputs": {"report": "report.report"},
}


async def test_stacking_summary_and_ppc_on_the_real_runner(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """M5: the fit runs' pointwise elpd (``metrics.json``) reaches ``compare.stacking``."""
    executor = LocalProcessExecutor(
        command=[sys.executable, "-m", "mapi_runner", "--worker-registry", REGISTRY],
        bootstrap_url_template=ctx.settings.bootstrap_url_template(),
        workdir=ctx.settings.workdir,
        exec_token=ctx.settings.resolved_exec_token(),
    )
    orch = Orchestrator(ctx, executor, use_procrastinate=False)
    task = asyncio.create_task(orch.run())
    try:
        catalog = await _until(client, "/v1/catalog", lambda b: bool(b.get("models")), 300)
        assert any(e["manifest"]["id"] == "toy.bayes_linear" for e in catalog["models"])
        await put_dataset(ctx, "panel", _panel())
        created = await client.post("/v1/workflows", json={"document": LADDER})
        assert created.status_code == 201, created.text
        started = await client.post(
            f"/v1/workflows/{created.json()['id']}/runs",
            json={"inputs": {"data": "dataset://default/panel@latest"}},
        )
        assert started.status_code == 202, started.text
        view = await _until(
            client,
            f"/v1/workflow-runs/{started.json()['run_id']}",
            lambda b: b["status"] in ("succeeded", "failed", "cancelled"),
            900,
        )
        nodes = {n["node_id"]: n for n in view["nodes"]}
        assert view["status"] == "succeeded", {
            k: (v["status"], v.get("error")) for k, v in nodes.items()
        }
        w = nodes["weights"]["result"]
        assert w["method"] == "stacking", w
        assert w["best"] == "driver" and w["weights"]["driver"] > 0.9
        s = nodes["s"]["result"]
        assert s["params"]["beta[0]"]["hdi_low"] > 0 and s["max_rhat"] < 1.05
        assert isinstance(nodes["ppc"]["result"]["ok"], bool)
    finally:
        orch.stop.set()
        await asyncio.wait_for(task, 60)
