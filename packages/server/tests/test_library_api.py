"""``/v1/library``: items, immutable revisions, ETags, archive, caps and ``:validate`` (v0.18
D1, D2). Documents compile against the checkout catalog (``tabular.declarative_glm`` and every
built-in step, plot and workflow node)."""

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
from mapi_spec.library import LibraryItem, LibraryRevision, LibraryValidation
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

URL = "/v1/library"

RECIPE: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ModelRecipe",
    "metadata": {"name": "store-sales", "title": "Store sales with promo lift", "tags": ["retail"]},
    "base": "declarative",
    "declarative": {
        "outcome": {"var": "sales", "family": "negative_binomial"},
        "terms": [
            {
                "var": "price",
                "transform": [{"use": "log1p"}, {"use": "standardize", "with": {"over": ["obs"]}}],
                "prior": {"dist": "normal", "sigma": 0.5},
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
            "with": {"vars": ["$slot:media"], "over": ["obs"]},
        }
    ],
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
    await make_tenant(ctx, "acme", projects=("growth",))
    out: dict[str, Any] = {}
    for role in ("viewer", "editor"):
        u = await make_user(ctx, f"{role}@acme.test")
        await grant_role(ctx, u, "acme", "growth", role)
        out[role] = await session_caller(ctx, client, u, tenant_id="acme", project_id="growth")
        out[f"{role}_user"] = u
    return out


def _doc(base: dict[str, Any], **metadata: Any) -> dict[str, Any]:
    doc = copy.deepcopy(base)
    doc["metadata"].update(metadata)
    return doc


async def _create(caller: Any, doc: dict[str, Any], **body: Any) -> LibraryItem:
    r = await caller.post(URL, json={"document": doc, **body})
    assert r.status_code == 201, r.text
    item = LibraryItem.model_validate(r.json())
    assert r.headers["ETag"] == item.etag == f'"r{item.latest_revision}"'
    return item


async def _revision(caller: Any, item_id: str, rev: int) -> LibraryRevision:
    r = await caller.get(f"{URL}/{item_id}/revisions/{rev}")
    assert r.status_code == 200, r.text
    return LibraryRevision.model_validate(r.json())


async def test_create_read_and_audit(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    who = await _people(ctx, client)
    item = await _create(who["editor"], RECIPE, message="first version")
    assert (item.kind, item.scope, item.project_id) == ("model_recipe", "project", "growth")
    assert (item.name, item.title, item.tags) == (
        "store-sales",
        RECIPE["metadata"]["title"],
        ["retail"],
    )
    assert item.created_by == who["editor_user"].id

    r = await who["viewer"].get(f"{URL}/{item.id}")
    assert r.status_code == 200 and r.headers["ETag"] == '"r1"'
    rev = await _revision(who["viewer"], item.id, 1)
    assert rev.valid and rev.message == "first version"
    assert rev.document == RECIPE
    assert rev.catalog_digest is not None and rev.doc_hash.startswith("sha256:")
    # compiled next to the source: the declarative model on the trusted built-in
    assert rev.compiled is not None
    assert rev.compiled["model"]["use"] == "tabular.declarative_glm@^1"  # type: ignore[index,call-overload]

    page = (await who["viewer"].get(f"{URL}/{item.id}/revisions")).json()
    assert [r["revision"] for r in page["items"]] == [1]

    async with ctx.db.session() as s:
        audit = (
            await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == "library.create"))
        ).scalar_one()
    assert (audit.resource_kind, audit.resource_id, audit.tenant_id) == ("library", item.id, "acme")
    assert audit.details["valid"] is True


async def test_text_bodies_parse_with_the_budgeted_loader(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    text = (
        "apiVersion: mapi/v1\nkind: PrepPreset\n"
        "metadata: {name: log-sales, title: Log sales}\n"
        "steps:\n  - {id: log, use: core.log1p@^1, with: {vars: [sales]}}\n"
    )
    r = await who["editor"].post(URL, json={"text": text})
    assert r.status_code == 201, r.text
    rev = await _revision(who["editor"], r.json()["id"], 1)
    assert rev.valid, rev.issues
    assert rev.compiled == {
        "pipeline": [{"id": "p_log", "use": "core.log1p@^1", "with": {"vars": ["sales"]}}]
    }
    bad = await who["editor"].post(URL, json={"text": "kind: [unclosed"})
    assert bad.status_code == 400 and bad.json()["code"] == "SPEC_PARSE_ERROR"
    neither = await who["editor"].post(URL, json={"document": RECIPE, "text": text})
    assert neither.status_code == 422


async def test_invalid_documents_are_stored_but_templates_are_refused(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    # does not parse: nothing to store
    r = await who["editor"].post(URL, json={"document": {**RECIPE, "kind": "Nope"}})
    assert r.status_code == 422 and r.json()["code"] == "LIBRARY_INVALID"
    assert r.json()["errors"][0]["pointer"] == "/kind"
    # parses but does not compile: stored, valid=false, issues point into the document
    preset = _doc(RECIPE, name="sarimax-preset")
    preset.pop("declarative")
    preset.update(base="acme.missing@^1", settings={"order": [1, 0, 0]})
    item = await _create(who["editor"], preset)
    rev = await _revision(who["editor"], item.id, 1)
    assert not rev.valid
    assert [(i.code, i.pointer) for i in rev.issues] == [("EXTENSION_UNAVAILABLE", "/base")]
    # a team workflow template must be valid when saved (D8)
    broken = _doc(TEMPLATE, name="broken")
    broken["document"] = {**broken["document"], "nodes": [{"id": "x", "use": "test.nope"}]}
    r = await who["editor"].post(URL, json={"document": broken})
    assert r.status_code == 422 and r.json()["code"] == "LIBRARY_INVALID"
    assert all(i["pointer"].startswith("/document") for i in r.json()["errors"])
    assert (await who["editor"].post(URL, json={"document": TEMPLATE})).status_code == 201


async def test_user_text_is_stored_verbatim(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    who = await _people(ctx, client)
    title = '<img src=x onerror="alert(1)"> & <b>bold</b>'
    item = await _create(who["editor"], _doc(PRESET, title=title, summary="<script>x</script>"))
    assert item.title == title and item.summary == "<script>x</script>"
    rev = await _revision(who["editor"], item.id, 1)
    assert rev.document["metadata"]["title"] == title  # type: ignore[index,call-overload]


async def test_revisions_need_the_current_etag(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    item = await _create(who["editor"], RECIPE)
    url = f"{URL}/{item.id}/revisions"
    changed = _doc(RECIPE, name="store-sales-v2", title="Tighter price prior")
    changed["declarative"]["terms"][0]["prior"]["sigma"] = 0.25

    r = await who["editor"].post(url, json={"document": changed}, headers={"If-Match": '"r9"'})
    assert r.status_code == 412 and r.json()["code"] == "PRECONDITION_FAILED"
    r = await who["editor"].post(
        url, json={"document": changed, "message": "tighter"}, headers={"If-Match": '"r1"'}
    )
    assert r.status_code == 201, r.text
    assert r.headers["ETag"] == '"r2"'
    assert LibraryRevision.model_validate(r.json()).revision == 2
    # the metadata renames the item; the old revision is unchanged
    now = LibraryItem.model_validate((await who["viewer"].get(f"{URL}/{item.id}")).json())
    assert (now.latest_revision, now.name, now.title) == (
        2,
        "store-sales-v2",
        "Tighter price prior",
    )
    assert (await _revision(who["viewer"], item.id, 1)).document == RECIPE
    page = (await who["viewer"].get(url)).json()
    assert [x["revision"] for x in page["items"]] == [2, 1]
    # the same stale ETag again: refused
    r = await who["editor"].post(url, json={"document": RECIPE}, headers={"If-Match": '"r1"'})
    assert r.status_code == 412
    # the kind cannot change
    r = await who["editor"].post(url, json={"document": PRESET})
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/kind"
    # a viewer cannot revise; an unknown revision is 404
    assert (await who["viewer"].post(url, json={"document": RECIPE})).status_code == 403
    assert (await who["viewer"].get(f"{url}/7")).status_code == 404


async def test_names_are_unique_per_kind_and_scope(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    await _create(who["editor"], RECIPE)
    r = await who["editor"].post(URL, json={"document": RECIPE})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    # another kind may reuse the name
    await _create(who["editor"], _doc(PRESET, name="store-sales"))
    # a revision cannot take a name that is in use
    other = await _create(who["editor"], _doc(RECIPE, name="other"))
    r = await who["editor"].post(f"{URL}/{other.id}/revisions", json={"document": RECIPE})
    assert r.status_code == 409


async def test_archive_hides_and_unarchive_shows(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    keep = await _create(who["editor"], PRESET)
    gone = await _create(who["editor"], RECIPE)
    r = await who["viewer"].post(f"{URL}/{gone.id}:archive")
    assert r.status_code == 403
    r = await who["editor"].post(f"{URL}/{gone.id}:archive")
    assert r.status_code == 200 and r.json()["archived"] is True

    ids = [i["id"] for i in (await who["viewer"].get(URL)).json()["items"]]
    assert ids == [keep.id]
    ids = [
        i["id"]
        for i in (await who["viewer"].get(URL, params={"include_archived": "true"})).json()["items"]
    ]
    assert set(ids) == {keep.id, gone.id}
    # still readable by id; no revision while archived
    assert (await who["viewer"].get(f"{URL}/{gone.id}")).status_code == 200
    r = await who["editor"].post(f"{URL}/{gone.id}/revisions", json={"document": RECIPE})
    assert r.status_code == 409
    assert (await who["editor"].post(f"{URL}/{gone.id}:unarchive")).json()["archived"] is False
    async with ctx.db.session() as s:
        actions = (
            await s.execute(
                select(m.AuditEvent.details).where(m.AuditEvent.action == "library.archive")
            )
        ).scalars()
        assert [d["archived"] for d in actions] == [True, False]


async def test_list_filters_and_pages(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    who = await _people(ctx, client)
    made = [await _create(who["editor"], _doc(PRESET, name=f"preset-{i}")) for i in range(3)]
    await _create(who["editor"], _doc(RECIPE, title="Store 100% model"))
    ed = who["editor"]
    kinds = (await ed.get(URL, params={"kind": "prep_preset"})).json()["items"]
    assert {i["id"] for i in kinds} == {x.id for x in made}
    found = (await ed.get(URL, params={"q": "100%"})).json()["items"]
    assert [i["name"] for i in found] == ["store-sales"]
    assert (await ed.get(URL, params={"q": "PRESET-1"})).json()["items"][0]["name"] == "preset-1"
    assert (await ed.get(URL, params={"scope": "tenant"})).json()["items"] == []

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = (await ed.get(URL, params=params)).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 4
    assert (await ed.get(URL, params={"cursor": "bogus"})).status_code == 422
    assert (await ed.get(URL, params={"kind": "plugin"})).status_code == 422


async def test_caps_answer_409(
    ctx: ServerContext, client: httpx.AsyncClient, settings: Settings
) -> None:
    who = await _people(ctx, client)
    ctx.settings.library_items_per_tenant = 2
    ctx.settings.library_revisions_per_item = 2
    first = await _create(who["editor"], PRESET)
    second = await _create(who["editor"], RECIPE)
    await who["editor"].post(f"{URL}/{second.id}:archive")  # archived items count
    r = await who["editor"].post(URL, json={"document": _doc(PRESET, name="third")})
    assert r.status_code == 409 and "most allowed" in r.json()["detail"]

    url = f"{URL}/{first.id}/revisions"
    assert (await who["editor"].post(url, json={"document": PRESET})).status_code == 201
    r = await who["editor"].post(url, json={"document": PRESET})
    assert r.status_code == 409 and "revisions" in r.json()["detail"]


async def test_validate_points_into_the_document(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    ok = await who["viewer"].post(f"{URL}:validate", json={"document": RECIPE})
    assert ok.status_code == 200, ok.text
    v = LibraryValidation.model_validate(ok.json())
    assert v.valid and v.compiled is not None
    # the data-shape check waits for a dataset (an info issue)
    assert [(i.code, i.severity) for i in v.issues] == [("LIBRARY_DATA_SHAPE", "info")]

    bad = copy.deepcopy(RECIPE)
    bad["declarative"]["terms"][1]["prior"]["sigma"] = -1
    v = LibraryValidation.model_validate(
        (await who["viewer"].post(f"{URL}:validate", json={"document": bad})).json()
    )
    assert not v.valid
    assert [i.pointer for i in v.issues] == ["/declarative/terms/1/prior/sigma"]

    bad = copy.deepcopy(RECIPE)
    bad["declarative"]["terms"][1]["varies_by"] = "region"
    v = LibraryValidation.model_validate(
        (await who["viewer"].post(f"{URL}:validate", json={"document": bad})).json()
    )
    assert not v.valid and v.issues[0].pointer == "/declarative/terms/1/varies_by"

    # a preset step setting that the step's schema refuses
    bad = copy.deepcopy(PRESET)
    bad["steps"][0]["with"]["nope"] = 1
    v = LibraryValidation.model_validate(
        (await who["viewer"].post(f"{URL}:validate", json={"document": bad})).json()
    )
    assert not v.valid and v.issues[0].pointer.startswith("/steps/0/with")

    # text that does not parse: answered as issues, not an error
    v = LibraryValidation.model_validate(
        (await who["viewer"].post(f"{URL}:validate", json={"text": "kind: ["})).json()
    )
    assert not v.valid and v.issues


async def test_validate_with_a_dataset(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    who = await _people(ctx, client)
    schema = {
        "layout": "long",
        "dims": {"row": {"kind": "integer", "policy": "extend"}},
        "vars": {
            "sales": {"dims": ["row"], "dtype": "float64", "role": "target"},
            "price": {"dims": ["row"], "dtype": "float64"},
            "store": {"dims": ["row"], "dtype": "str"},
        },
    }
    async with ctx.db.tenant_session("acme") as s:
        ds = m.Dataset(id="ds_stores", tenant_id="acme", project_id="growth", name="stores",
                       labels={}, latest_version=1, next_version=2)  # fmt: skip
        s.add(ds)
        await s.flush()
        s.add(
            m.DatasetVersion(
                dataset_id=ds.id, version=1, tenant_id="acme", project_id="growth",
                status="ready", sha256="sha256:" + "0" * 64, size_bytes=1,
                data_schema=schema, objects={}, issues=[],
            )
        )  # fmt: skip
    body = {"document": RECIPE, "dataset": "dataset://growth/stores@latest"}
    v = LibraryValidation.model_validate(
        (await who["viewer"].post(f"{URL}:validate", json=body)).json()
    )
    assert not v.valid
    assert [(i.pointer, i.message) for i in v.issues] == [
        ("/declarative/terms/1/var", "The dataset has no variable 'promo'.")
    ]
    missing = {**body, "dataset": "dataset://growth/nothing@latest"}
    v = LibraryValidation.model_validate(
        (await who["viewer"].post(f"{URL}:validate", json=missing)).json()
    )
    assert v.issues[0].pointer == "/dataset" and v.issues[0].code == "REF_UNRESOLVED"


def _pin_the_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limiter counts in fixed one-minute windows: keep every request of the test in
    one window, so a test that crosses a minute boundary does not flake."""
    from mapi_server.services.library import compile as library_compile

    at = library_compile.now()
    monkeypatch.setattr(library_compile, "now", lambda: at)


async def test_validate_is_rate_limited(
    ctx: ServerContext, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_the_minute(monkeypatch)
    who = await _people(ctx, client)
    ctx.settings.library_validate_per_minute = 2
    for _ in range(2):
        r = await who["viewer"].post(f"{URL}:validate", json={"document": PRESET})
        assert r.status_code == 200
    r = await who["viewer"].post(f"{URL}:validate", json={"document": PRESET})
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"
    assert int(r.headers["Retry-After"]) >= 1
    # per user: someone else still gets an answer
    assert (
        await who["editor"].post(f"{URL}:validate", json={"document": PRESET})
    ).status_code == 200


async def test_the_platform_seeds_items_idempotently(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """``create_item_system`` (the sample project's library, D20): ``created_by="system"``,
    checked against the catalog when one is given, the same item on a second call."""
    from mapi_server.services.library import create_item_system

    who = await _people(ctx, client)
    async with ctx.db.session() as s:
        snap = await ctx.catalogs.require(s)
    async with ctx.db.tenant_session("acme") as s:
        first = await create_item_system(
            s, "acme", "growth", RECIPE, catalog=snap.catalog, catalog_digest=snap.digest
        )
        unchecked = await create_item_system(s, "acme", "growth", PRESET)
        assert first is not None and unchecked is not None
        first_id, unchecked_id = first.id, unchecked.id
    async with ctx.db.tenant_session("acme") as s:
        again = await create_item_system(s, "acme", "growth", RECIPE, catalog=snap.catalog)
        assert again is not None and again.id == first_id
        assert await create_item_system(s, "acme", "growth", TEMPLATE, max_items=2) is None

    items = (await who["viewer"].get(URL)).json()["items"]
    assert {i["id"] for i in items} == {first_id, unchecked_id}
    assert {i["created_by"] for i in items} == {"system"}
    rev = await _revision(who["viewer"], first_id, 1)
    assert rev.valid and rev.catalog_digest == snap.digest
    rev = await _revision(who["viewer"], unchecked_id, 1)
    assert rev.catalog_digest is None and [i.severity for i in rev.issues] == ["info"]
