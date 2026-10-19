"""The six M5 gallery templates on the server path.

The conformance tests run the templates with ``LocalRunner``. These tests run them through
the real ``WorkflowOrchestrator``: ``model.fit`` as ``fit`` child runs (elpd for stacking),
data nodes as ``workflow_node`` runs, and ``compare``, ``select``, ``branch`` and ``report``
in the control plane. The testkit ``drive`` helper executes the child runs in process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import mapi_server_testkit.workflows as tk
import pytest
import yaml
from mapi_server.context import ServerContext
from mapi_server.settings import Settings
from mapi_spec.workflow import WorkflowRunStatus

pytest.importorskip("mapi_plugin_science")

pytestmark = [pytest.mark.slow, pytest.mark.filterwarnings("ignore")]

_FIT = {"draws": 100, "tune": 100, "chains": 2, "seed": 3}


@pytest.fixture(autouse=True)
def _science_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from mapi_plugin_tabular import PLUGINS as TABULAR
    from mapi_workflows.testing import science_registry

    reg = science_registry()
    reg.register(list(TABULAR))
    monkeypatch.setattr(tk, "_REGISTRY", reg)


@pytest.fixture
def settings(settings: Settings, tmp_path: Path, _science_registry: None) -> Settings:
    return settings.model_copy(update={"catalog_path": tk.catalog_file(tmp_path)})


async def _run(
    client: httpx.AsyncClient,
    ctx: ServerContext,
    template: str,
    datasets: dict[str, Any],
    params: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    for name, ds in datasets.items():
        await tk.put_dataset(ctx, name, ds)
    r = await client.post("/v1/workflows", json={"template": template})
    assert r.status_code == 201, r.text
    inputs = {port: f"dataset://default/{port}@latest" for port in datasets}
    r = await client.post(
        f"/v1/workflows/{r.json()['id']}/runs", json={"inputs": inputs, "params": params}
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    status = await tk.drive(ctx, run_id, max_rounds=80)
    view = (await client.get(f"/v1/workflow-runs/{run_id}")).json()
    assert status is WorkflowRunStatus.SUCCEEDED, view.get("error")
    nodes = {n["node_id"]: n for n in view["nodes"]}
    plots = _report_plots(nodes)
    for nid, n in nodes.items():
        if n["status"] == "succeeded" and "recipe" in (n.get("result") or {}):
            assert nid in plots, nid  # every plot that ran is embedded in the report
    return nodes, view


def _report_plots(nodes: dict[str, Any]) -> set[str]:
    report = nodes["report"]["outputs"]["report"]["inline"]
    assert report["format"] == "mapi.report/v1"
    return {s["node_id"] for s in report["sections"] if s["kind"] == "plot" and s["plot"]}


async def test_spatial_ladder(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    from mapi_plugin_science.areal_data import areal_dataset

    data = areal_dataset(n_row=4, n_col=5, truth={"b1": 1.0}, seed=1)
    nodes, _ = await _run(
        client, ctx, "sci_spatial_ladder", {"data": data}, {**_FIT, "hdi_prob": 0.9}
    )
    assert nodes["best"]["result"]["selected"] in {"iid", "icar", "bym2"}
    agree = nodes["converged"]["result"]["status"] not in {"fail", "error"}
    assert nodes["clear"]["result"]["arm"] == ("then" if agree else "else")


async def test_network_ladder(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    from mapi_plugin_science.network_data import network_dataset

    data = network_dataset(n_nodes=16, seed=1)
    nodes, _ = await _run(client, ctx, "sci_network_ladder", {"data": data}, {**_FIT, "n_sims": 20})
    assert nodes["ppc"]["status"] == "succeeded"


async def test_phylo_signal(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    from mapi_plugin_science.phylo_data import PHYLO_SCHEMA, phylo_dataset

    data = phylo_dataset(n_species=30, truth={"slope": {"body": 0.7, "group_size": 0.6}}, seed=4)
    data.attrs["mapi_schema"] = PHYLO_SCHEMA.model_dump_json(exclude_none=True)
    nodes, _ = await _run(client, ctx, "sci_phylo_signal", {"data": data}, dict(_FIT))
    assert nodes["clear"]["result"]["arm"] in {"then", "else"}


@pytest.mark.parametrize("with_layout", [True, False], ids=["layout", "no-layout"])
async def test_calibrate_then_design(
    client: httpx.AsyncClient, ctx: ServerContext, with_layout: bool
) -> None:
    import xarray as xr
    from mapi_plugin_science.heat_plate_data import HEAT_SCHEMA, heat_plate_dataset

    data = heat_plate_dataset()
    data.attrs["mapi_schema"] = HEAT_SCHEMA.model_dump_json(exclude_none=True)
    datasets: dict[str, Any] = {"data": data}
    if with_layout:
        layout = xr.zeros_like(data["heater_w"].isel(run=[0])).assign_coords(run=["design"])
        layout[0, 1, 2] = layout[0, 4, 9] = 5.0
        datasets["layout"] = xr.Dataset({"heater_w": layout})
    bind = {
        "dims": {"y": "y_cm", "x": "x_cm"},
        "vars": {"heater_power": "heater_w", "temperature": "reading_c"},
    }
    nodes, _ = await _run(
        client, ctx, "sci_calibrate_then_design", datasets,
        {**_FIT, "bind": bind, "max_rhat": 1.5},
    )  # fmt: skip
    assert nodes["good"]["result"]["arm"] == "then"
    assert nodes["field"]["status"] == "succeeded"


def _case(data: Any, schema: Any, spec_yaml: str) -> tuple[Any, dict[str, Any]]:
    data.attrs["mapi_schema"] = schema.model_dump_json(exclude_none=True)
    spec = yaml.safe_load(spec_yaml)
    return data, {
        "model": spec["model"]["use"],
        "settings": spec["model"].get("settings", {}),
        "bind": spec["data"].get("bind", {}),
        **_FIT,
    }


async def test_bayes_workflow_audit(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    from mapi_plugin_tabular.datasets import RADON_SCHEMA, RADON_SPEC_YAML, radon_dataset

    data, params = _case(radon_dataset(counties=8, homes=64), RADON_SCHEMA, RADON_SPEC_YAML)
    params |= {
        "prior_predictive": {"draws": 50},
        "rhat_max": 1.1,
        "min_ess": 50,
        "n_sims": 5,
        "vars": ["intercept", "beta", "sigma"],
    }
    nodes, _ = await _run(client, ctx, "bayes_workflow_audit", {"data": data}, params)
    assert nodes["converged"]["result"]["arm"] in {"then", "else"}


async def test_image_model_check(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    from mapi_plugin_science.lens_data import LENS_SCHEMA, LENS_SPEC_YAML, lens_dataset

    data, params = _case(lens_dataset(), LENS_SCHEMA, LENS_SPEC_YAML)
    nodes, _ = await _run(
        client, ctx, "sci_image_model_check", {"data": data}, params | {"shuffle": True}
    )
    assert nodes["report"]["status"] == "succeeded"
