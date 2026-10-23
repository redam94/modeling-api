"""The tenant catalog view (v0.19, WP7, plan D11).

``view = base catalog ∪ visible overlay entries``: approved uploads for the whole tenant,
the caller's own kit-passed uploads while the tenant allows private use. These tests seed
plugin rows directly (the pipeline that produces them is WP6's) and check what each caller
sees: two tenants see disjoint uploads, a private upload is the uploader's alone, the ETag
differs per tenant and user, built-ins win an id collision, entries outside their namespace
are dropped, and the views never reach the ``catalogs`` table.

The helpers at the top (``seed_version`` and friends) are shared by the other WP7 tests.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards.runas import owner_auth
from mapi_server.services.plugins.gate import VisibleVersion
from mapi_server.services.plugins.view import (
    CatalogView,
    ViewCatalogs,
    catalog_view,
    clear_view_cache,
    upload_provider,
    view_catalogs,
)
from mapi_server.services.workflows.node_catalog import templates_for
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.builders import CATALOG
from mapi_server_testkit.identity import grant_role, make_tenant, make_user, session_caller
from mapi_spec.api import new_id
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.node_authoring import about
from mapi_spec.plugins import PluginExports, describe_digest
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from sqlalchemy import func, select

CODE_A = "k3vq7m"
NS_A = f"t_{CODE_A}_acme"
CODE_B = "p7x2ab"
NS_B = f"t_{CODE_B}_beta"

ABOUT = about(
    "Store mean",
    "Predicts each store's sales from its own average and a smooth trend.",
    ["You want a quick baseline per store"],
    ["You need channel effects"],
    ["Weekly sales per store"],
    ["Stores behave alike over time"],
    ["A forecast per store"],
    "beginner",
    "seconds",
    "Store 12 sells about 1,200 units a week.",
)


def sha(n: int) -> str:
    return "sha256:" + f"{n % 256:02x}" * 32


def exports(namespace: str, *, recipe_schema: bool = True) -> dict[str, Any]:
    """Overlay exports in ``namespace``: a step (``winsorize``, the fixture's standardize
    contract), a model (``store_mean``, the fixture model's manifest with an About), a plot
    recipe (``fan``) and a workflow template (``<ns>_flow``, a built-in document)."""
    base = json.loads(CATALOG.read_text())
    step = dict(next(s for s in base["steps"] if s["id"] == "core.standardize"))
    step["id"] = f"{namespace}.winsorize"
    model = json.loads(json.dumps(base["models"][0]))
    model["manifest"]["id"] = f"{namespace}.store_mean"
    model["manifest"]["about"] = ABOUT.model_dump(mode="json")
    plot = dict(next(p for p in base["plots"] if p["id"] == "core.ppc_fan"))
    plot["id"] = f"{namespace}.fan"
    plot["title"] = "Store fan"
    if recipe_schema:
        plot["settings_schema"] = {
            "type": "object",
            "properties": {"width": {"type": "integer", "minimum": 100, "maximum": 2000}},
            "additionalProperties": False,
        }
    template = next(iter(WORKFLOW_TEMPLATES.values()))
    tmpl = {
        "id": f"{namespace}_flow",
        "version": "1.0.0",
        "about": template.about.model_dump(mode="json"),
        "document": {
            "apiVersion": "mapi/v1",
            "kind": "Workflow",
            "metadata": {"name": "flow", "project": "default"},
            "inputs": {"data": {"type": "dataset"}},
            "nodes": [{"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}}],
        },
    }
    return {"steps": [step], "models": [model], "plots": [plot], "workflow_templates": [tmpl]}


EXAMPLE: dict[str, Any] = {
    "id": "example",
    "kind": "vega-lite",
    "schema_version": "v5",
    "title": "Example",
    "persona": "both",
    "datasets": {"fan": {"kind": "data_ref", "path": "ui/plotdata/fan.arrow"}},
    "spec": {"data": {"name": "fan"}, "mark": "area"},
    "provenance": {"generated_at": "2026-10-15T09:40:00Z", "recipe": "example@1.0.0"},
}


async def seed_version(
    ctx: ServerContext,
    *,
    tenant_id: str = "default",
    namespace: str = NS_A,
    uploaded_by: str,
    state: str = "approved",
    version: str = "1.0.0",
    version_id: str | None = None,
    sha256: str | None = None,
    payload: dict[str, Any] | None = None,
    examples: dict[str, Any] | None = None,
    project_id: str = "default",
) -> str:
    """One plugin version with a passing describe for the current image digest; returns
    its id. Creates the tenant's plugin settings and the plugin on first use."""
    code = namespace.split("_")[1]
    short = namespace.split("_")[2]
    vid = version_id or new_id("plv")
    body = payload if payload is not None else exports(namespace)
    ex = PluginExports.model_validate(body)
    ts = datetime.now(UTC)
    async with ctx.db.session() as s:
        if await s.get(m.TenantPluginSettings, tenant_id) is None:
            s.add(m.TenantPluginSettings(tenant_id=tenant_id, tenant_code=code, private_use=True))
            await s.flush()
        plugin = (
            await s.execute(select(m.Plugin).where(m.Plugin.namespace == namespace))
        ).scalar_one_or_none()
        if plugin is None:
            plugin = m.Plugin(
                id=new_id("plg"),
                tenant_id=tenant_id,
                namespace=namespace,
                short_name=short,
                title=short.title(),
                created_by=uploaded_by,
            )
            s.add(plugin)
            await s.flush()
        scope = {"approved": "tenant", "kit_passed": "private", "awaiting_approval": "private"}
        row = m.PluginVersion(
            id=vid,
            tenant_id=tenant_id,
            plugin_id=plugin.id,
            version=version,
            state=state,
            scope=scope.get(state, "none"),
            project_id=project_id,
            uploaded_by=uploaded_by,
            upload_sha256=sha256 or sha(int(hashlib.sha256(vid.encode()).hexdigest()[:2], 16)),
            size_bytes=4096,
            quarantine_key=f"t/{tenant_id}/plugins/{vid}.whl",
            quarantine_generation=1,
            overlay_key=f"t/{tenant_id}/plugins/{plugin.id}/{vid}.whl",
            overlay_generation=1,
        )
        if state == "yanked":
            row.yanked_at, row.yank_reason = ts, "withdrawn"
        if state == "purged":
            row.purged_at = ts
        s.add(row)
        await s.flush()
        s.add(
            m.PluginDescribe(
                version_id=vid,
                base_image_digest=ctx.catalogs.image_digest,
                tenant_id=tenant_id,
                run_id=new_id("run"),
                describe_digest=describe_digest(ex),
                exports=ex.model_dump(mode="json", exclude_defaults=True),
                plot_examples=examples if examples is not None else {f"{namespace}.fan": EXAMPLE},
                kit={"ok": True, "checks": []},
                issues=[],
                ok=True,
            )
        )
    clear_view_cache()
    return vid


async def set_private_use(ctx: ServerContext, tenant_id: str, on: bool) -> None:
    async with ctx.db.session() as s:
        row = await s.get(m.TenantPluginSettings, tenant_id)
        assert row is not None
        row.private_use = on


async def set_state(ctx: ServerContext, version_id: str, state: str) -> None:
    """Move a seeded version (bypassing the state machine: tests only)."""
    async with ctx.db.session() as s:
        row = await s.get(m.PluginVersion, version_id)
        assert row is not None
        row.state = state
        row.scope = {"approved": "tenant", "kit_passed": "private"}.get(state, "none")
        if state == "yanked":
            row.yanked_at, row.yank_reason = datetime.now(UTC), "security"
    clear_view_cache()


async def acting(
    ctx: ServerContext, user_id: str, *, tenant_id: str = "default", project_id: str = "default"
) -> ServerContext:
    """``ctx`` as a request of ``user_id`` in the project (their grants now)."""
    auth: RequestAuth | None = await owner_auth(ctx, tenant_id, project_id, user_id)
    assert auth is not None
    return ctx.for_request(auth)


async def view_of(ctx: ServerContext, tenant_id: str, user_id: str | None) -> CatalogView:
    async with ctx.db.session() as s:
        return await catalog_view(ctx, s, tenant_id=tenant_id, user_id=user_id)


def ids(view: CatalogView) -> set[str]:
    c = view.catalog
    return (
        {e.id for e in c.steps}
        | {e.id for e in c.models}
        | {e.id for e in c.plots}
        | {e.id for e in c.workflow_templates}
        | {e.id for e in c.workflow_nodes}
    )


def uploaded(view: CatalogView) -> set[str]:
    return {i for i in ids(view) if i.startswith("t_")}


# ============================================================================== fixtures


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def people(ctx: ServerContext) -> dict[str, m.User]:
    """``ana`` (admin) and ``ed`` (editor) of tenant ``default``; ``bob`` admin of ``beta``."""
    await make_tenant(ctx, "beta", tier="internal")
    out: dict[str, m.User] = {}
    for name, tenant, role in (("ana", "default", "admin"), ("ed", "default", "editor"),
                               ("bob", "beta", "admin")):  # fmt: skip
        user = await make_user(ctx, f"{name}@{tenant}.test")
        await grant_role(ctx, user, tenant, None, role)
        out[name] = user
    return out


pytestmark = pytest.mark.integration


# ============================================================================ visibility


async def test_two_tenants_see_disjoint_uploads(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    await seed_version(ctx, uploaded_by=people["ana"].id)
    await seed_version(ctx, tenant_id="beta", namespace=NS_B, uploaded_by=people["bob"].id,
                       project_id="main")  # fmt: skip
    a = await view_of(ctx, "default", people["ed"].id)
    b = await view_of(ctx, "beta", people["bob"].id)
    assert uploaded(a) == {f"{NS_A}.winsorize", f"{NS_A}.store_mean", f"{NS_A}.fan",
                           f"{NS_A}_flow"}  # fmt: skip
    assert uploaded(b) == {f"{NS_B}.winsorize", f"{NS_B}.store_mean", f"{NS_B}.fan",
                           f"{NS_B}_flow"}  # fmt: skip
    assert not uploaded(a) & uploaded(b)
    # the base catalog is everyone's, and its digest is what specs pin
    assert a.snapshot.digest == b.snapshot.digest == a.base.digest
    assert ids(a) - uploaded(a) == ids(b) - uploaded(b)
    with pytest.raises(MapiError) as err:
        a.pins_for([f"{NS_B}.winsorize"])
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE


async def test_overlay_entries_carry_their_provider_and_hash(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    vid = await seed_version(ctx, uploaded_by=people["ana"].id, sha256=sha(7))
    view = await view_of(ctx, "default", None)
    step = next(e for e in view.catalog.steps if e.id == f"{NS_A}.winsorize")
    model = next(e for e in view.catalog.models if e.id == f"{NS_A}.store_mean")
    plot = next(e for e in view.catalog.plots if e.id == f"{NS_A}.fan")
    tmpl = next(e for e in view.catalog.workflow_templates if e.id == f"{NS_A}_flow")
    for entry in (step, model, plot, tmpl):
        assert entry.provider == upload_provider(vid)
        assert entry.overlay_hash == sha(7)
    # built-in entries are untouched
    assert all(e.provider is None for e in view.catalog.steps if e.id.startswith("core."))
    assert view.pins_for([f"{NS_A}.fan@^1", "core.standardize"])[0].model_dump(mode="json") == {
        "plugin_version_id": vid,
        "namespace": NS_A,
        "sha256": sha(7),
    }


async def test_a_private_upload_is_the_uploaders_alone(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    await seed_version(ctx, uploaded_by=people["ed"].id, state="kit_passed")
    mine = await view_of(ctx, "default", people["ed"].id)
    theirs = await view_of(ctx, "default", people["ana"].id)
    nobody = await view_of(ctx, "default", None)
    assert f"{NS_A}.winsorize" in uploaded(mine)
    assert uploaded(theirs) == set() and uploaded(nobody) == set()
    assert mine.overlays[0].scope == "private"
    # a tenant admin turns private use off: the uploader no longer sees it either
    await set_private_use(ctx, "default", False)
    clear_view_cache()
    assert uploaded(await view_of(ctx, "default", people["ed"].id)) == set()


@pytest.mark.parametrize("state", ["yanked", "purged", "rejected", "kit_failed", "describing"])
async def test_versions_that_may_not_run_are_not_shown(
    ctx: ServerContext, people: dict[str, m.User], state: str
) -> None:
    await seed_version(ctx, uploaded_by=people["ed"].id, state=state)
    assert uploaded(await view_of(ctx, "default", people["ed"].id)) == set()


async def test_the_newest_visible_version_of_a_plugin_wins(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    old = "plv_01aaaaaaaaaaaaaaaaaaaaaaaa"
    new = "plv_01zzzzzzzzzzzzzzzzzzzzzzzz"
    await seed_version(ctx, uploaded_by=people["ed"].id, version_id=old, sha256=sha(1))
    await seed_version(ctx, uploaded_by=people["ed"].id, version_id=new, version="1.1.0",
                       state="kit_passed", sha256=sha(2))  # fmt: skip
    # the uploader tries the newer private version; teammates keep the approved one
    assert [v.version_id for v in (await view_of(ctx, "default", people["ed"].id)).overlays] == [
        new
    ]
    assert [v.version_id for v in (await view_of(ctx, "default", people["ana"].id)).overlays] == [
        old
    ]


async def test_built_ins_win_and_foreign_ids_are_dropped(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    body = exports(NS_A)
    # a forged describe: a built-in id, another tenant's namespace and an unsafe schema
    builtin = dict(body["steps"][0], id="core.standardize")
    foreign = dict(body["steps"][0], id=f"{NS_B}.sneaky")
    unsafe = dict(
        body["steps"][0],
        id=f"{NS_A}.regex",
        settings_schema={"type": "object",
                         "properties": {"x": {"type": "string", "pattern": "^(a|aa)*c$"}}},
    )  # fmt: skip
    body["steps"] = [*body["steps"], builtin, foreign, unsafe]
    body["workflow_templates"].append(
        dict(body["workflow_templates"][0], id=next(iter(WORKFLOW_TEMPLATES)))
    )
    await seed_version(ctx, uploaded_by=people["ana"].id, payload=body)
    view = await view_of(ctx, "default", None)
    steps = [e for e in view.catalog.steps if e.id == "core.standardize"]
    assert len(steps) == 1 and steps[0].provider is None  # the built-in, unchanged
    assert f"{NS_B}.sneaky" not in ids(view) and f"{NS_A}.regex" not in ids(view)
    assert f"{NS_A}.winsorize" in ids(view)
    builtin_tmpl = next(iter(WORKFLOW_TEMPLATES))
    assert all(t.id != builtin_tmpl for t in view.catalog.workflow_templates)


async def test_views_are_cached_per_key_and_never_stored(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    await seed_version(ctx, uploaded_by=people["ana"].id)
    one = await view_of(ctx, "default", people["ana"].id)
    assert await view_of(ctx, "default", people["ana"].id) is one
    assert (await view_of(ctx, "default", people["ed"].id)).etag != one.etag
    async with ctx.db.session() as s:
        assert (await s.execute(select(func.count()).select_from(m.Catalog))).scalar_one() == 1
        row = await s.get(m.Catalog, ctx.catalogs.image_digest)
        assert row is not None and NS_A not in json.dumps(row.catalog)
    async with ctx.db.session() as s:
        base = await ctx.catalogs.require(s)
    assert NS_A not in base.catalog.model_dump_json()


async def test_the_service_adapter_answers_with_the_view(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    await seed_version(ctx, uploaded_by=people["ana"].id)
    async with ctx.db.session() as s:
        catalogs = await view_catalogs(ctx, s, tenant_id="default", user_id=None)
        assert isinstance(catalogs, ViewCatalogs)
        snap = await catalogs.require(s)
        assert snap.overlay_key and snap.digest == (await ctx.catalogs.require(s)).digest
        assert snap.catalog.resolve_step(f"{NS_A}.winsorize@^1") is not None
        example = await catalogs.example(s, f"{NS_A}.fan@1.0.0")
        assert example is not None and example.title == "Example"
        # the base catalog of the image is not changed by the view
        assert (await ctx.catalogs.require(s)).catalog.resolve_step(f"{NS_A}.winsorize") is None
        # the node and template maps are cached per view, not per base digest
        assert f"{NS_A}_flow" in templates_for(snap)
        assert f"{NS_A}_flow" not in templates_for(await ctx.catalogs.require(s))


def test_a_view_needs_no_database_to_answer_pins() -> None:
    """``pins_for`` is pure over the visible versions (one per namespace)."""
    from mapi_server.services.catalog import CatalogSnapshot
    from mapi_server.services.plugins.view import _build
    from mapi_spec.catalog import Catalog

    digest = "sha256:" + "11" * 32
    base = CatalogSnapshot(digest, digest, Catalog(), None, "file")
    vv = VisibleVersion(
        version_id="plv_01aaaaaaaaaaaaaaaaaaaaaaaa",
        plugin_id="plg_01aaaaaaaaaaaaaaaaaaaaaaaa",
        namespace=NS_A,
        sha256=sha(3),
        describe_digest=sha(4),
        scope="tenant",
        uploaded_by="usr_x",
        exports=PluginExports.model_validate({"steps": exports(NS_A)["steps"]}),
        plot_examples={},
    )
    view = _build(base, (vv,), '"e"')
    assert [p.namespace for p in view.pins_for([f"{NS_A}.winsorize", "core.x"])] == [NS_A]
    assert view.pins_for(["core.standardize"]) == []
    with pytest.raises(MapiError) as err:
        view.pins_for([f"{NS_B}.winsorize"])
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE
    # live L-F5: the refusal names the workspace setting that can also hide an upload
    assert "Settings › Workspace › Uploaded code" in err.value.detail


# ========================================================================== HTTP routes


async def test_catalog_routes_answer_with_the_callers_view(
    ctx: ServerContext, client: httpx.AsyncClient, people: dict[str, m.User]
) -> None:
    await seed_version(ctx, uploaded_by=people["ed"].id, state="kit_passed")
    ed = await session_caller(ctx, client, people["ed"], tenant_id="default",
                              project_id="default")  # fmt: skip
    ana = await session_caller(ctx, client, people["ana"], tenant_id="default",
                               project_id="default")  # fmt: skip
    bob = await session_caller(ctx, client, people["bob"], tenant_id="beta", project_id="main")
    r_ed = await ed.get("/v1/catalog/steps", params={"limit": 200})
    r_ana = await ana.get("/v1/catalog/steps", params={"limit": 200})
    r_bob = await bob.get("/v1/catalog/steps", params={"limit": 200})
    for r in (r_ed, r_ana, r_bob):
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "private"
    assert f"{NS_A}.winsorize" in {e["id"] for e in r_ed.json()["items"]}
    assert f"{NS_A}.winsorize" not in {e["id"] for e in r_ana.json()["items"]}
    assert len({r_ed.headers["etag"], r_ana.headers["etag"], r_bob.headers["etag"]}) == 3
    full = (await ed.get("/v1/catalog")).json()
    assert f"{NS_A}.store_mean" in {e["manifest"]["id"] for e in full["models"]}
    base_digest = (await ana.get("/v1/catalog")).json()["digest"]
    assert full["digest"] == base_digest  # the view keeps the base digest
    r = await ed.get(f"/v1/catalog/plots/{NS_A}.fan/1.0.0/example")
    assert r.status_code == 200 and r.json()["title"] == "Example"
    assert (await ana.get(f"/v1/catalog/plots/{NS_A}.fan/1.0.0/example")).status_code == 404
    gallery = await ed.get("/v1/workflow-templates", params={"limit": 200})
    assert f"{NS_A}_flow" in {t["id"] for t in gallery.json()["items"]}
    gallery = await ana.get("/v1/workflow-templates", params={"limit": 200})
    assert f"{NS_A}_flow" not in {t["id"] for t in gallery.json()["items"]}


async def test_diagnostics_never_show_uploads(
    ctx: ServerContext, client: httpx.AsyncClient, people: dict[str, m.User]
) -> None:
    body = exports(NS_A)
    body["steps"].append(
        dict(body["steps"][0], id=f"{NS_A}.regex",
             settings_schema={"type": "object",
                              "properties": {"x": {"type": "string", "pattern": "^(a|aa)*c$"}}})
    )  # fmt: skip
    await seed_version(ctx, uploaded_by=people["ed"].id, payload=body)
    ana = await session_caller(ctx, client, people["ana"], tenant_id="default",
                               project_id="default")  # fmt: skip
    r = await ana.get("/v1/catalog/diagnostics")
    assert r.status_code == 200, r.text
    assert NS_A not in r.text
