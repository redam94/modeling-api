"""Health, identity, projects, catalog, schemas, errors and the route table."""

from __future__ import annotations

import json
import subprocess
import sys

import httpx
from mapi_server.app import openapi_document
from mapi_server.context import ServerContext
from mapi_spec.api import M1_ENDPOINTS, Health, Page, Project
from mapi_spec.errors import PROBLEM_CONTENT_TYPE, Problem


async def test_plot_validate_runs_the_sanitizer(client: httpx.AsyncClient) -> None:
    from mapi_server_testkit.fake_runner import SAFE_PLOT, UNSAFE_PLOT

    r = await client.post("/v1/plots:validate", json=SAFE_PLOT)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True and body["sanitized"]["id"] == "core.ppc_fan"
    r = await client.post("/v1/plots:validate", json=UNSAFE_PLOT)
    body = r.json()
    assert r.status_code == 200 and body["valid"] is False and body["sanitized"] is None
    assert {i["pointer"] for i in body["issues"]} >= {"/spec/mark", "/spec/encoding/url"}
    assert all(i["code"] == "PLOT_UNSAFE" for i in body["issues"] if i["severity"] == "error")
    r = await client.post("/v1/plots:validate", json={"id": "x"})
    assert r.json()["valid"] is False
    r = await client.post("/v1/plots:validate", json=[1, 2])
    assert r.status_code == 422


async def test_healthz_and_readyz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert Health.model_validate(r.json()).status == "ok"
    r = await client.get("/readyz")
    assert r.status_code == 200
    body = Health.model_validate(r.json())
    assert body.checks["postgres"].status == "ok"
    assert body.checks["object_store"].status == "ok"
    assert body.checks["catalog"].status == "ok"


async def test_readyz_fails_when_storage_is_down(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ctx.store._buckets.clear()  # type: ignore[attr-defined]
    r = await client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["object_store"]["status"] == "fail"


async def test_me_and_projects(client: httpx.AsyncClient) -> None:
    me = (await client.get("/v1/me")).json()
    assert me == {
        "subject": "local",
        "tenant_id": "default",
        "project_ids": ["default"],
        "auth": "none",
        "scopes": [],
    }
    page = Page[Project].model_validate((await client.get("/v1/projects")).json())
    assert [p.id for p in page.items] == ["default"]
    assert page.next_cursor is None
    assert (await client.get("/v1/projects/default")).json()["tenant_id"] == "default"


async def test_not_found_is_a_problem_document(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/projects/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    problem = Problem.model_validate(r.json())
    assert problem.code == "NOT_FOUND"
    assert problem.type == "urn:mapi:error:NOT_FOUND"
    r = await client.get("/v1/definitely-not-a-route")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"


async def test_request_validation_maps_to_schema_invalid_with_pointers(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post("/v1/runs", json={"kind": "fit", "spec_id": "x", "bogus": 1})
    assert r.status_code == 422
    problem = Problem.model_validate(r.json())
    assert problem.code == "SCHEMA_INVALID"
    pointers = {i.pointer for i in problem.errors}
    assert "/body/bogus" in pointers
    r = await client.get("/v1/runs?limit=0")
    assert r.status_code == 422
    assert r.json()["errors"][0]["pointer"] == "/query/limit"


async def test_bad_cursor_is_schema_invalid(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/runs?cursor=***")
    assert r.status_code == 422
    assert r.json()["code"] == "SCHEMA_INVALID"


async def test_catalog_endpoints(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    cat = (await client.get("/v1/catalog")).json()
    assert cat["digest"].startswith("sha256:")
    assert {m["manifest"]["id"] for m in cat["models"]} == {"ts.hsgp_trend"}
    steps = (await client.get("/v1/catalog/steps?limit=2")).json()
    assert len(steps["items"]) == 2 and steps["next_cursor"]
    rest = (await client.get(f"/v1/catalog/steps?limit=50&cursor={steps['next_cursor']}")).json()
    ids = [s["id"] for s in steps["items"] + rest["items"]]
    assert ids == sorted(ids) and len(ids) == 4 and rest["next_cursor"] is None
    plots = (await client.get("/v1/catalog/plots")).json()
    assert {p["id"] for p in plots["items"]} == {"core.ppc_fan", "core.convergence"}
    model = (await client.get("/v1/catalog/models/ts.hsgp_trend/1.0.0")).json()
    assert model["manifest"]["capabilities"]["representation"] == "draws"
    assert (await client.get("/v1/catalog/models/ts.hsgp_trend/9.9.9")).status_code == 404


async def test_catalog_unavailable_is_503(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    ctx.catalogs.image_digest = "sha256:" + "ff" * 32
    ctx.catalogs.invalidate()
    r = await client.get("/v1/catalog")
    assert r.status_code == 503
    assert r.json()["code"] == "INFRA_ERROR"


async def test_schemas(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/schemas/modelspec.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/schema+json")
    assert r.json()["title"]
    assert (await client.get("/v1/schemas/nope.json")).status_code == 404


def test_route_table_covers_every_m1_endpoint() -> None:
    """Every row of ``mapi_spec.api.M1_ENDPOINTS`` is served with its operation id."""
    doc = openapi_document()
    served = {
        (method.upper(), path, op.get("operationId"))
        for path, item in doc["paths"].items()
        for method, op in item.items()
        if isinstance(op, dict)
    }
    missing = [
        f"{e.method} {e.path} ({e.operation_id})"
        for e in M1_ENDPOINTS
        if (e.method, e.path, e.operation_id) not in served
    ]
    assert not missing, missing


def test_openapi_document_is_31_and_names_operations() -> None:
    doc = openapi_document()
    assert doc["openapi"].startswith("3.1")
    ops = {
        op["operationId"]
        for path in doc["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    }
    assert {e.operation_id for e in M1_ENDPOINTS} <= ops
    assert "PlotSpec" in doc["components"]["schemas"]


def test_openapi_module_prints_json() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "mapi_server.openapi"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "", "MAPI_DATABASE_URL": "postgresql://unused/unused"},
    )
    doc = json.loads(out.stdout)
    assert doc["info"]["title"] == "modeling-api"
