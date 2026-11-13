"""Plugin workflow nodes and templates in the control plane (v0.18).

A plugin node arrives as a JSON contract in the catalog (the runner exports it; the control
plane never imports plugin code). It is listed with the built-in nodes, workflow documents
using it validate, a run creates its node row and the orchestrator starts it as a
``workflow_node`` child run that carries the plugin's namespace (the dispatcher routes it to
the image that ships the plugin). Contracts that break the plugin rules and templates that do
not validate are hidden and listed as diagnostics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.catalog import harden_catalog
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.workflows import WorkflowOrchestrator
from mapi_server.services.workflows.node_catalog import (
    NODE_PLUGIN_PARAM,
    contracts_for,
    templates_for,
)
from mapi_server.settings import Settings
from mapi_server_testkit.builders import CATALOG
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.workflows import put_dataset
from mapi_spec.catalog import Catalog
from mapi_spec.executor import RunKind
from mapi_spec.node_authoring import about, num, schema
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from mapi_workflows.testing import weekly_series
from sqlalchemy import select

DATA = "dataset://default/sales@latest"
NODE = "test.acme_zero_share"
TEMPLATE = "acme_weekly_check"

ACME_ABOUT = about(
    "Share of zeros",
    "Counts how many values of a column are zero and says if that is too many.",
    ["You want to know if a series has too many zero weeks"],
    ["The series has no zeros by design"],
    ["A dataset with one numeric column"],
    ["Zero means no activity, not a missing value"],
    ["The share of zeros and a verdict"],
    "beginner",
    "seconds",
    "12 of 52 weeks are zero: the share is 0.23.",
)


def _contract(node_id: str = NODE, *, plugin: str = "acme", **change: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": node_id,
        "version": "1.0.0",
        "kind": "test",
        "inputs": {"data": {"type": "dataset", "description": "The data to check"}},
        "outputs": {"result": "test_result"},
        "params_schema": schema({"max_share": num("The largest share of zeros", 0.2, lo=0, hi=1)}),
        "result_fields": {"share": {"type": "number", "description": "The share of zeros"}},
        "about": ACME_ABOUT.model_dump(mode="json"),
        "plugin": plugin,
    }
    out.update(change)
    return out


def _doc(node_use: str = NODE, *, name: str = "zeros") -> dict[str, Any]:
    return {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": name, "project": "default"},
        "inputs": {"data": {"type": "dataset"}},
        "nodes": [
            {
                "id": "zeros",
                "use": node_use,
                "inputs": {"data": "inputs.data"},
                "with": {"max_share": 0.3},
            }
        ],
    }


def _template(template_id: str = TEMPLATE, **change: Any) -> dict[str, Any]:
    doc = _doc(name="weekly-check")
    out: dict[str, Any] = {
        "id": template_id,
        "version": "1.0.0",
        "about": ACME_ABOUT.model_dump(mode="json"),
        "document": doc,
        "tags": ["checks"],
        "provider": "acme-lab",
    }
    out.update(change)
    return out


def plugin_catalog() -> dict[str, Any]:
    """The fixture catalog plus one good plugin node and template, and bad ones."""
    data: dict[str, Any] = json.loads(CATALOG.read_text())
    data["workflow_nodes"] = [
        _contract(),
        # runs in the orchestrator: plugin nodes are sandbox-only
        _contract("test.acme_inline", runs_in="orchestrator"),
        # an unsafe parameter schema
        _contract(
            "test.acme_regex",
            params_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "pattern": "^(a|aa)*c$"}},
            },
        ),
    ]
    data["workflow_templates"] = [
        _template(),
        # uses a node nobody installed
        _template("acme_broken", document=_doc("test.acme_missing", name="broken")),
        # collides with a built-in template id
        _template(next(iter(WORKFLOW_TEMPLATES))),
    ]
    return data


# ============================================================================ pure


def test_the_catalog_keeps_valid_plugin_nodes_and_templates_only() -> None:
    catalog = harden_catalog(Catalog.model_validate(plugin_catalog()))
    assert [c.id for c in catalog.workflow_nodes] == [NODE]
    assert [t.id for t in catalog.workflow_templates] == [TEMPLATE]
    by_id = {d.id: d for d in catalog.diagnostics}
    assert by_id["test.acme_inline"].code == "node_contract_invalid"
    assert by_id["test.acme_regex"].code == "schema_unsafe"
    assert by_id["acme_broken"].code == "template_invalid"
    assert (
        by_id["acme_broken"].untrusted_text
        and "acme_missing" in by_id["acme_broken"].untrusted_text
    )
    assert by_id[next(iter(WORKFLOW_TEMPLATES))].code == "id_collision"
    assert all(d.group.startswith("mapi.workflow_") for d in catalog.diagnostics)


def test_reserved_namespaces_are_refused() -> None:
    data: dict[str, Any] = json.loads(CATALOG.read_text())
    data["workflow_nodes"] = [
        # a built-in plugin's namespace (the platform's own namespaces and the upload
        # prefixes are refused by the contract model itself, at the runner)
        _contract("test.mmm_zero_share", plugin="mmm"),
    ]
    data["workflow_templates"] = [_template("core_weekly_check")]
    catalog = harden_catalog(Catalog.model_validate(data))
    assert catalog.workflow_nodes == [] and catalog.workflow_templates == []
    assert {d.code for d in catalog.diagnostics} == {"namespace_reserved"}


def test_the_merged_maps_put_built_ins_first() -> None:
    assert contracts_for(None) == NODE_IMPLEMENTATIONS
    assert templates_for(None) == WORKFLOW_TEMPLATES


# ===================================================================== integration

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(plugin_catalog()))
    return settings.model_copy(update={"catalog_path": path})


async def _all(client: httpx.AsyncClient, url: str, **params: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        q = {**params, "limit": 200, **({"cursor": cursor} if cursor else {})}
        r = await client.get(url, params=q)
        assert r.status_code == 200, r.text
        body = r.json()
        out += body["items"]
        cursor = body.get("next_cursor")
        if not cursor:
            return out


@pytest.mark.integration
async def test_plugin_nodes_and_templates_are_listed(client: httpx.AsyncClient) -> None:
    nodes = {n["id"]: n for n in await _all(client, "/v1/catalog/workflow-nodes")}
    assert set(NODE_IMPLEMENTATIONS) < set(nodes)
    assert nodes[NODE]["plugin"] == "acme"
    assert "test.acme_inline" not in nodes and "test.acme_regex" not in nodes
    tests = await _all(client, "/v1/catalog/workflow-nodes", kind="test")
    assert NODE in {n["id"] for n in tests}
    r = await client.get(f"/v1/catalog/workflow-nodes/{NODE}")
    assert r.status_code == 200 and r.json()["about"]["title"] == "Share of zeros"
    assert (await client.get("/v1/catalog/workflow-nodes/test.acme_inline")).status_code == 404

    gallery = {t["id"]: t for t in await _all(client, "/v1/workflow-templates")}
    assert set(WORKFLOW_TEMPLATES) < set(gallery)
    assert gallery[TEMPLATE]["source"] == "plugin"
    assert all("source" not in gallery[i] for i in WORKFLOW_TEMPLATES)  # default left out
    assert "acme_broken" not in gallery
    r = await client.get(f"/v1/workflow-templates/{TEMPLATE}")
    assert r.status_code == 200 and r.json()["source"] == "plugin"
    assert (await client.get("/v1/workflow-templates/acme_broken")).status_code == 404


@pytest.mark.integration
async def test_documents_using_a_plugin_node_validate(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/workflows:validate", json={"document": _doc()})
    assert r.status_code == 200 and r.json()["valid"], r.text
    r = await client.post("/v1/workflows:validate", json={"document": _doc("test.acme_inline")})
    assert r.status_code == 200 and not r.json()["valid"]
    bad = _doc()
    bad["nodes"][0]["with"] = {"max_share": 2}  # out of the contract's range
    r = await client.post("/v1/workflows:validate", json={"document": bad})
    assert not r.json()["valid"]
    assert r.json()["issues"][0]["pointer"].startswith("/nodes/0/with")


@pytest.mark.integration
async def test_a_workflow_from_a_plugin_template_is_created(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/workflows", json={"template": TEMPLATE})
    assert r.status_code == 201, r.text
    wf = r.json()
    assert wf["template"] == TEMPLATE
    rev = (await client.get(f"/v1/workflows/{wf['id']}/revisions/1")).json()
    assert rev["validation"]["valid"]
    assert rev["document"]["metadata"]["labels"]["workflow-template"] == TEMPLATE
    r = await client.post("/v1/workflows", json={"template": "acme_broken"})
    assert r.status_code == 404


async def _started_run(client: httpx.AsyncClient, ctx: ServerContext) -> str:
    await put_dataset(ctx, "sales", weekly_series(60))
    r = await client.post("/v1/workflows", json={"document": _doc()})
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]
    r = await client.post(f"/v1/workflows/{wf_id}/runs", json={"inputs": {"data": DATA}})
    assert r.status_code == 202, r.text
    return str(r.json()["run_id"])


async def _node_child(ctx: ServerContext, run_id: str) -> m.Run:
    await WorkflowOrchestrator(ctx).tick_all()
    async with ctx.db.session() as s:
        row = await s.get(m.WorkflowNodeRunRow, (run_id, "zeros"))
        assert row is not None and row.child_run_id is not None, row and row.status
        child = await s.get(m.Run, row.child_run_id)
        assert child is not None
        s.expunge(child)
    return child


@pytest.mark.integration
async def test_a_plugin_node_runs_as_a_sandboxed_child_with_its_namespace(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    run_id = await _started_run(client, ctx)
    async with ctx.db.session() as s:
        row = await s.get(m.WorkflowNodeRunRow, (run_id, "zeros"))
        assert row is not None
        assert (row.use, row.kind) == (f"{NODE}@1.0.0", "test")
    child = await _node_child(ctx, run_id)
    assert child.kind == RunKind.WORKFLOW_NODE.value
    params = child.payload["params"]
    assert child.payload["use"] == f"{NODE}@1.0.0"
    assert params[NODE_PLUGIN_PARAM] == "acme"
    assert params["max_share"] == 0.3


@pytest.mark.integration
async def test_the_dispatcher_routes_a_plugin_node_by_its_namespace(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    child = await _node_child(ctx, await _started_run(client, ctx))
    async with ctx.db.session() as s:
        run = await s.get(m.Run, child.id)
        assert run is not None
        assert await Dispatcher(ctx, FakeExecutor())._plugin(run) == "acme"


@pytest.mark.integration
async def test_a_run_needs_the_plugin_to_still_be_installed(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await put_dataset(ctx, "sales", weekly_series(60))
    r = await client.post("/v1/workflows", json={"document": _doc()})
    wf_id = r.json()["id"]
    # the image is rebuilt without the plugin: same digest key, catalog without the node
    async with ctx.db.session() as s:
        row = await s.get(m.Catalog, ctx.catalogs.image_digest)
        assert row is not None
        cat = dict(row.catalog)
        cat.pop("workflow_nodes", None)
        cat.pop("workflow_templates", None)
        row.catalog = cat
        row.catalog_digest = "sha256:" + "cd" * 32
    ctx.catalogs.invalidate()
    r = await client.post(f"/v1/workflows/{wf_id}/runs", json={"inputs": {"data": DATA}})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "EXTENSION_UNAVAILABLE"
    async with ctx.db.session() as s:
        assert (await s.execute(select(m.WorkflowRunRow))).first() is None
