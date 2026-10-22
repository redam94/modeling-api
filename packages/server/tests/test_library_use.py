"""``POST /v1/library/{item_id}:use``: what a library item compiles to (v0.18 D3, D4, D8, D9).

A declarative model recipe compiles to a spec fragment that validates against the checkout
catalog (``validate_spec`` with the dataset's schema); presets fill exposed settings; prep
presets fill slots and prefix step ids; workflow templates come back as documents for the
caller's project. Lineage is in labels; nothing is saved."""

from __future__ import annotations

import copy
import functools
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import grant_role, make_tenant, make_user, session_caller
from mapi_spec.catalog import Catalog
from mapi_spec.errors import has_errors
from mapi_spec.library import LibraryItem, LibraryUseResult
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ModelSpec
from mapi_spec.validate import validate_spec
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

URL = "/v1/library"
DATASET = "dataset://growth/stores@latest"
SCHEMA: dict[str, Any] = {
    "layout": "long",
    "dims": {"row": {"kind": "integer", "policy": "extend"}},
    "vars": {
        "sales": {"dims": ["row"], "dtype": "float64", "role": "target"},
        "price": {"dims": ["row"], "dtype": "float64"},
        "promo": {"dims": ["row"], "dtype": "float64"},
        "tv": {"dims": ["row"], "dtype": "float64", "role": "media"},
        "search": {"dims": ["row"], "dtype": "float64", "role": "media"},
        "store": {"dims": ["row"], "dtype": "str"},
    },
}
RECIPE: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ModelRecipe",
    "metadata": {"name": "store-sales", "title": "Store sales with promo lift"},
    "declarative": {
        "outcome": {"var": "sales", "family": "negative_binomial"},
        "terms": [
            {
                "var": "price",
                "transform": [{"use": "log1p"}, {"use": "standardize", "with": {"over": ["obs"]}}],
                "prior": {"sigma": 0.5},
                "sign": "negative",
            },
            {"var": "promo", "prior": {"sigma": 1}, "sign": "positive", "varies_by": "store"},
        ],
        "groups": [{"dim": "store"}],
    },
    "plots": ["core.ppc"],
}
PRESET: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "PrepPreset",
    "metadata": {"name": "scale-media", "title": "Scale the media columns"},
    "slots": {"media": {"kind": "var_list", "label": "Media columns", "role": "media"}},
    "steps": [
        {
            "id": "scale",
            "use": "core.max_abs_scale@^1",
            "with": {"vars": ["$slot:media"], "over": ["row"]},
        }
    ],
}
SARIMAX: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ModelRecipe",
    "metadata": {"name": "weekly-sarimax", "title": "Weekly SARIMAX"},
    "base": "ts.sarimax@^1",
    "settings": {"order": [1, 0, 0], "trend": "c"},
    "exposed": ["order"],
}
TEMPLATE: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "WorkflowTemplate",
    "metadata": {"name": "weekly-check", "title": "Weekly readiness check"},
    "document": WORKFLOW_TEMPLATES["ts_readiness_arimax"].document,
}


@functools.cache
def _checkout_catalog() -> str:
    registry = pytest.importorskip("mapi_sdk.registry")
    text: str = registry.default_registry().catalog().model_dump_json()
    return text


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    path = tmp_path / "checkout_catalog.json"
    path.write_text(_checkout_catalog())
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT, "catalog_path": path})


async def _people(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    await make_tenant(ctx, "acme", projects=("growth", "retail"))
    out: dict[str, Any] = {}
    for role, project in (("viewer", "growth"), ("editor", "growth"), ("retail_editor", "retail")):
        u = await make_user(ctx, f"{role}@acme.test")
        await grant_role(ctx, u, "acme", project, role.removeprefix("retail_"))
        out[role] = await session_caller(ctx, client, u, tenant_id="acme", project_id=project)
    admin = await make_user(ctx, "admin@acme.test")
    await grant_role(ctx, admin, "acme", None, "admin")
    out["admin"] = await session_caller(ctx, client, admin, tenant_id="acme", project_id="growth")
    async with ctx.db.tenant_session("acme") as s:
        ds = m.Dataset(id="ds_stores", tenant_id="acme", project_id="growth", name="stores",
                       labels={}, latest_version=1, next_version=2)  # fmt: skip
        s.add(ds)
        await s.flush()
        s.add(
            m.DatasetVersion(
                dataset_id=ds.id, version=1, tenant_id="acme", project_id="growth",
                status="ready", sha256="sha256:" + "0" * 64, size_bytes=1,
                data_schema=SCHEMA, objects={}, issues=[],
            )
        )  # fmt: skip
    return out


async def _create(caller: Any, doc: dict[str, Any], scope: str = "project") -> LibraryItem:
    r = await caller.post(URL, json={"document": doc, "scope": scope})
    assert r.status_code == 201, r.text
    return LibraryItem.model_validate(r.json())


async def _use(caller: Any, item: LibraryItem, **body: Any) -> LibraryUseResult:
    r = await caller.post(f"{URL}/{item.id}:use", json=body)
    assert r.status_code == 200, r.text
    return LibraryUseResult.model_validate(r.json())


def _spec(fragment: dict[str, Any]) -> ModelSpec:
    return ModelSpec.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ModelSpec",
            "metadata": {"name": "from-library", "project": "growth", **fragment["metadata"]},
            "data": {"dataset": DATASET, **fragment.get("data", {})},
            "pipeline": fragment["pipeline"],
            "model": fragment["model"],
            "outputs": fragment.get("outputs", {}),
        }
    )


async def test_a_declarative_recipe_compiles_to_a_valid_spec(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    item = await _create(who["editor"], RECIPE)
    used = await _use(who["editor"], item, dataset=DATASET)
    label = f"{item.id}-r1"
    assert used.kind == "model_recipe"
    assert used.labels == {"mapi.library/model": label}
    frag = used.spec_fragment
    assert frag is not None
    assert frag["model"]["use"] == "tabular.declarative_glm@^1"  # type: ignore[index,call-overload]
    assert frag["data"] == {"bind": {"dims": {"obs": "row"}, "vars": {"y": "sales"}}}
    assert [s["id"] for s in frag["pipeline"]] == ["t0_log1p", "t1_standardize"]  # type: ignore[union-attr,index,call-overload]
    assert frag["metadata"] == {"labels": {"mapi.library/model": label}}
    # the fragment is an ordinary spec: it validates against the checkout catalog and data
    catalog = Catalog.model_validate_json(_checkout_catalog())
    v = validate_spec(_spec(frag), catalog, DataSchema.model_validate(SCHEMA))  # type: ignore[arg-type]
    assert not has_errors(v.issues), [(i.pointer, i.message) for i in v.issues]
    # without a dataset the shape is left for later (an info note, no binding of the rows)
    bare = await _use(who["editor"], item)
    assert [i.code for i in bare.issues] == ["LIBRARY_DATA_SHAPE"]


async def test_model_recipes_need_spec_create(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    recipe = await _create(who["editor"], RECIPE)
    preset = await _create(who["editor"], PRESET)
    r = await who["viewer"].post(f"{URL}/{recipe.id}:use", json={"dataset": DATASET})
    assert r.status_code == 403
    # a viewer may use a prep preset (the result is only steps)
    used = await _use(who["viewer"], preset, slots={"media": ["tv", "search"]}, dataset=DATASET)
    assert used.pipeline is not None


async def test_prep_presets_fill_slots_and_prefix_ids(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    item = await _create(who["editor"], PRESET)
    used = await _use(who["editor"], item, slots={"media": ["tv", "search"]}, dataset=DATASET)
    assert used.pipeline == [
        {
            "id": "p_scale",
            "use": "core.max_abs_scale@^1",
            "with": {"vars": ["tv", "search"], "over": ["row"]},
        }
    ]
    assert used.labels == {"mapi.library/prep": f"{item.id}-r1"}

    r = await who["editor"].post(f"{URL}/{item.id}:use", json={"dataset": DATASET})
    assert r.status_code == 422 and r.json()["code"] == "LIBRARY_INVALID"
    assert r.json()["errors"][0]["pointer"] == "/slots/media"
    r = await who["editor"].post(
        f"{URL}/{item.id}:use", json={"slots": {"media": ["radio"]}, "dataset": DATASET}
    )
    assert r.status_code == 422
    assert "radio" in r.json()["errors"][0]["message"]
    r = await who["editor"].post(
        f"{URL}/{item.id}:use", json={"slots": {"media": ["tv"]}, "dataset": "dataset://growth/x"}
    )
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/dataset"


async def test_a_recipe_brings_its_prep_preset(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    prep = await _create(who["editor"], PRESET)
    doc = copy.deepcopy(RECIPE)
    doc["metadata"]["name"] = "with-prep"
    doc["prep"] = {"item": prep.id, "revision": 1}
    item = await _create(who["editor"], doc)
    rev = (await who["editor"].get(f"{URL}/{item.id}/revisions/1")).json()
    assert rev["valid"] is True, rev["issues"]
    used = await _use(who["editor"], item, dataset=DATASET, slots={"media": ["tv", "search"]})
    assert used.labels == {
        "mapi.library/model": f"{item.id}-r1",
        "mapi.library/prep": f"{prep.id}-r1",
    }
    ids = [s["id"] for s in used.spec_fragment["pipeline"]]  # type: ignore[index,union-attr,call-overload]
    assert ids == ["p_scale", "t0_log1p", "t1_standardize"]

    # a reference to an item the project cannot read is stored as invalid
    other = await _create(who["retail_editor"], PRESET)
    doc["metadata"]["name"] = "with-foreign-prep"
    doc["prep"] = {"item": other.id}
    foreign = await _create(who["editor"], doc)
    rev = (await who["editor"].get(f"{URL}/{foreign.id}/revisions/1")).json()
    assert rev["valid"] is False
    assert [i["pointer"] for i in rev["issues"] if i["severity"] == "error"] == ["/prep/item"]
    r = await who["editor"].post(f"{URL}/{foreign.id}:use", json={"dataset": DATASET})
    assert r.status_code == 422


async def test_presets_fill_exposed_settings(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    who = await _people(ctx, client)
    item = await _create(who["editor"], SARIMAX)
    used = await _use(who["editor"], item, exposed={"order": [2, 1, 0]})
    model = used.spec_fragment["model"]  # type: ignore[index]
    assert model == {"use": "ts.sarimax@^1", "settings": {"order": [2, 1, 0], "trend": "c"}}
    r = await who["editor"].post(f"{URL}/{item.id}:use", json={"exposed": {"trend": "n"}})
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/exposed/trend"


async def test_a_missing_base_model_is_409(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    who = await _people(ctx, client)
    doc = {**copy.deepcopy(SARIMAX), "base": "acme.gone@^1"}
    item = await _create(who["editor"], doc)
    r = await who["editor"].post(f"{URL}/{item.id}:use", json={})
    assert r.status_code == 409 and r.json()["code"] == "EXTENSION_UNAVAILABLE"
    assert r.json()["errors"][0]["pointer"] == "/base"


async def test_team_workflow_templates_start_in_the_callers_project(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    item = await _create(who["admin"], TEMPLATE, scope="tenant")
    used = await _use(who["retail_editor"], item)
    label = f"{item.id}-r1"
    assert used.labels == {"mapi.library/template": label}
    doc = used.workflow_document
    assert doc is not None
    assert doc["metadata"]["project"] == "retail"  # type: ignore[index,call-overload]
    assert doc["metadata"]["labels"]["mapi.library/template"] == label  # type: ignore[index,call-overload]
    # the web posts it as an ordinary workflow
    r = await who["retail_editor"].post("/v1/workflows", json={"document": doc})
    assert r.status_code == 201, r.text


async def test_a_chart_template_needs_a_model_version(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    chart = {
        "apiVersion": "mapi/v1",
        "kind": "ChartTemplate",
        "metadata": {"name": "fit-dots", "title": "Fit as dots"},
        "source_recipe": "core.fit_vs_actual@^1",
        "plot": {
            "data": {"name": "points"},
            "mark": "point",
            "encoding": {
                "x": {"field": "observed", "type": "quantitative"},
                "y": {"field": "point", "type": "quantitative"},
            },
        },
    }
    item = await _create(who["editor"], chart)
    r = await who["editor"].post(f"{URL}/{item.id}:use", json={})
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/model_id"
    r = await who["editor"].post(
        f"{URL}/{item.id}:use", json={"model_id": "mdl_nothing", "model_version": 1}
    )
    assert r.status_code == 404
