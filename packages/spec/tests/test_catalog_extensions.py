"""v0.18 catalog extensions (diagnostics, provider, plot settings, plugin nodes and templates),
the merged node/template maps, and the v0.18 HTTP table and permissions."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest
from mapi_spec import api, library_workflows
from mapi_spec.about import About
from mapi_spec.api import CLIENT_OPERATIONS, ROUTE_ACCESS, V018_ENDPOINTS, RouteScope
from mapi_spec.catalog import (
    CATALOG_DIAGNOSTICS_MAX,
    Catalog,
    CatalogDiagnostic,
    PlotCatalogEntry,
    TemplateCatalogEntry,
)
from mapi_spec.errors import ERROR_INFO, ErrorCode
from mapi_spec.loader import canonical_json
from mapi_spec.node_authoring import about, node, num
from mapi_spec.plots import PlotProvenance
from mapi_spec.run import catalog_digest
from mapi_spec.sharing import embeddable
from mapi_spec.tenancy import PERMISSIONS, Action, Level, ProjectRole, ResourceKind, can
from mapi_spec.tokens import scope_allows
from mapi_spec.workflow import validate_workflow
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES, template_from_entry
from pydantic import BaseModel, ValidationError

# the hand-written test catalog's digest before v0.18 (computed from the v0.17 sources)
CONFTEST_CATALOG_DIGEST = "sha256:4b06816adf14ae322156d52f4fd3ec3c0183eeba094b108b6e3dc19e3067da7e"
_V018_KEYS = {
    "provider", "settings_schema", "tags", "pack_label", "workflow_nodes",
    "workflow_templates", "diagnostics",
}  # fmt: skip


def card(title: str = "Weekly zero check") -> About:
    return about(
        title,
        "Checks every week of the data for zero sales and reports the share of zero weeks.",
        use=["Before you fit a count model"],
        avoid=["The data has no zeros by design"],
        needs=["A dataset with a weekly target"],
        assumes=["Zeros are real values, not missing values"],
        outputs=["A report with the share of zero weeks"],
        difficulty="beginner",
        runtime="seconds",
        example="It finds that 12% of the weeks of store 4 have zero sales.",
    )


ZERO = node(
    "test.acme_zero_share",
    "test",
    card("Share of zeros"),
    plugin="acme",
    params={"threshold": num("The largest share that passes", 0.2, 0, 1)},
    outputs={"result": "test_result"},
    fields={"share": ("number", "The share of zeros")},
)
DOC: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "Workflow",
    "metadata": {"name": "acme-weekly-check", "project": "templates"},
    "inputs": {"data": {"type": "dataset"}},
    "nodes": [{"id": "zeros", "use": "test.acme_zero_share", "inputs": {"data": "inputs.data"}}],
}


def template(**kw: Any) -> TemplateCatalogEntry:
    base: dict[str, Any] = {"id": "acme_weekly_check", "about": card(), "document": DOC}
    return TemplateCatalogEntry(**{**base, **kw})


def _strip_v018(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_v018(v) for k, v in obj.items() if k not in _V018_KEYS}
    if isinstance(obj, list):
        return [_strip_v018(v) for v in obj]
    return obj


# ==================================================================================== digest


def test_catalog_digest_is_unchanged_with_empty_extensions(catalog: Catalog) -> None:
    cat = catalog
    assert catalog_digest(cat) == CONFTEST_CATALOG_DIGEST
    dumped = cat.model_dump(mode="json")
    assert set(dumped) == {"steps", "models", "plots"}
    for group in ("steps", "models", "plots"):
        for entry in dumped[group]:
            assert "provider" not in entry
    for entry in dumped["plots"]:
        assert not _V018_KEYS & set(entry), entry
    assert canonical_json(cat) == canonical_json(Catalog.model_validate(dumped))


def test_live_catalog_digest_ignores_the_empty_extensions() -> None:
    registry = pytest.importorskip("mapi_sdk.registry")
    cat = registry.default_registry().catalog()
    dumped = cat.model_dump(mode="json", exclude_defaults=True)
    # built-ins carry no provider, nodes, templates or diagnostics; whatever plot fields
    # are deliberately filled (settings_schema, tags, pack_label) change the digest only
    for key in ("provider", "workflow_nodes", "workflow_templates", "diagnostics"):
        assert f'"{key}"' not in json.dumps(dumped), key
    stripped = Catalog.model_validate(_strip_v018(dumped))
    assert canonical_json(stripped) == canonical_json(_strip_v018(dumped))


# ============================================================================ catalog models


def test_diagnostics_are_bounded_and_split_trusted_from_untrusted_text() -> None:
    d = CatalogDiagnostic(
        code="load_error",
        group="mapi.models",
        entry_point="acme",
        distribution="acme-models",
        message="The entry point could not be loaded.",
        untrusted_text="ImportError: <b>no module</b>",
    )
    assert d.untrusted_text == "ImportError: <b>no module</b>"  # kept verbatim, shown as text
    for bad in ({"code": "nope"}, {"message": "x" * 501}, {"untrusted_text": "x" * 501},
                {"group": "g" * 65}):  # fmt: skip
        with pytest.raises(ValidationError):
            CatalogDiagnostic(**{**d.model_dump(), **bad})
    with pytest.raises(ValidationError):
        Catalog(diagnostics=[d] * (CATALOG_DIAGNOSTICS_MAX + 1))
    assert len(Catalog(diagnostics=[d] * CATALOG_DIAGNOSTICS_MAX).diagnostics) == 500


def test_plot_entries_take_settings_tags_and_a_pack_label() -> None:
    e = PlotCatalogEntry(
        id="acme.weekly_total",
        title="Weekly total",
        provider="acme-plots",
        settings_schema={"type": "object", "properties": {"title": {"type": "string"}}},
        tags=["retail", "weekly"],
        pack_label="Acme charts",
    )
    assert e.model_dump(mode="json")["pack_label"] == "Acme charts"
    plain = PlotCatalogEntry(id="core.ppc", title="Predictive check")
    assert set(plain.model_dump(mode="json")) & _V018_KEYS == set()
    for bad in ({"tags": ["Not A Slug"]}, {"tags": [f"t{i}" for i in range(9)]},
                {"pack_label": "x" * 41}, {"settings_schema": {"type": 5}}):  # fmt: skip
        with pytest.raises(ValidationError):
            PlotCatalogEntry(id="acme.x", title="X", **bad)


def test_template_entries() -> None:
    t = template(tags=["retail"], provider="acme-lab")
    assert t.ref == "acme_weekly_check@1.0.0"
    for bad_id in ("weekly", "acme_", "lib_weekly", "t_acme_weekly", "Acme_x", "acme-weekly"):
        with pytest.raises(ValidationError):
            template(id=bad_id)
    with pytest.raises(ValidationError):
        template(version="1.0")


def test_catalog_ids_are_unique_per_kind() -> None:
    Catalog(workflow_nodes=[ZERO], workflow_templates=[template()])
    with pytest.raises(ValidationError, match="workflow node"):
        Catalog(workflow_nodes=[ZERO, ZERO])
    with pytest.raises(ValidationError, match="workflow template"):
        Catalog(workflow_templates=[template(), template()])


def test_a_catalog_with_extensions_round_trips() -> None:
    cat = Catalog(workflow_nodes=[ZERO], workflow_templates=[template()])
    again = Catalog.model_validate_json(cat.model_dump_json())
    assert again == cat
    assert again.workflow_nodes[0].plugin == "acme"


# ===================================================================== merged nodes/templates


def test_node_contracts_merge_plugins_and_builtins_win() -> None:
    assert dict(library_workflows.node_contracts(None)) == NODE_IMPLEMENTATIONS
    shadow = NODE_IMPLEMENTATIONS["test.adf"].model_copy(update={"version": "9.0.0"})
    orchestrated = ZERO.model_copy(update={"id": "test.acme_orch", "runs_in": "orchestrator"})
    unnamed = ZERO.model_copy(update={"id": "test.acme_anon", "plugin": None})
    cat = Catalog(workflow_nodes=[ZERO, shadow, orchestrated, unnamed])
    merged = library_workflows.node_contracts(cat)
    assert merged["test.acme_zero_share"] == ZERO
    assert merged["test.adf"].version == "1.0.0"
    assert "test.acme_orch" not in merged and "test.acme_anon" not in merged
    with pytest.raises(TypeError):
        merged["x"] = ZERO  # type: ignore[index]


def test_a_workflow_with_a_plugin_node_validates_only_against_the_merged_map() -> None:
    cat = Catalog(workflow_nodes=[ZERO])
    alone = validate_workflow(DOC)
    assert not alone.valid
    assert ErrorCode.NODE_UNKNOWN in {i.code for i in alone.issues}
    merged = validate_workflow(DOC, nodes=library_workflows.node_contracts(cat))
    assert merged.valid, merged.issues
    assert merged.resolved == {"zeros": "test.acme_zero_share@1.0.0"}


def test_all_templates_adds_valid_plugin_templates_only() -> None:
    assert library_workflows.all_templates(None) == WORKFLOW_TEMPLATES
    broken = copy.deepcopy(DOC)
    broken["nodes"][0]["use"] = "test.acme_missing"
    taken = template(id="ts_readiness_arimax")  # a built-in id: the built-in stays
    cat = Catalog(
        workflow_nodes=[ZERO],
        workflow_templates=[template(), template(id="acme_broken", document=broken), taken],
    )
    out = library_workflows.all_templates(cat)
    assert set(out) == set(WORKFLOW_TEMPLATES) | {"acme_weekly_check"}
    assert out["acme_weekly_check"].source == "plugin"
    assert out[taken.id].source == "builtin"
    # without the node the template is not offered either
    assert "acme_weekly_check" not in library_workflows.all_templates(
        Catalog(workflow_templates=[template()])
    )


def test_template_from_entry() -> None:
    t = template_from_entry(template(tags=["retail"]))
    assert (t.id, t.source, t.tags) == ("acme_weekly_check", "plugin", ["retail"])
    assert t.spec.metadata.name == "acme-weekly-check"
    assert "source" not in WORKFLOW_TEMPLATES["mmm_governed"].model_dump()
    assert t.model_dump()["source"] == "plugin"


# ================================================================================== sharing


def test_an_authored_chart_is_never_embeddable() -> None:
    prov = PlotProvenance(
        author="lib_01jaf3q2x8m9v7k5h4g3f2e1d0-r3", generated_at="2026-10-21T00:00:00Z"
    )
    assert prov.source == "author"
    for source_kind in ("model_plot", "spec"):
        for recipe in ("core.fit_vs_actual@1.0.0", None):
            assert not embeddable(source_kind, prov.source, recipe)
    assert embeddable("model_plot", "recipe", "core.fit_vs_actual@1.0.0")


# ======================================================================= v0.18 HTTP contract


def _resolve(name: str) -> type[BaseModel] | None:
    """A DTO by name across the v0.18 modules (``None`` while its module is not written)."""
    m = re.fullmatch(r"(?:Page|list)\[(\w+)\]", name)
    inner = m.group(1) if m else name
    import importlib

    for mod in ("mapi_spec.api", "mapi_spec.catalog", "mapi_spec.jobs", "mapi_spec.library",
                "mapi_spec.onboarding", "mapi_spec.samples"):  # fmt: skip
        try:
            module = importlib.import_module(mod)
        except ImportError:
            continue
        obj = getattr(module, inner, None)
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            return obj
    return None


@pytest.mark.parametrize("e", V018_ENDPOINTS, ids=lambda e: e.operation_id)
def test_v018_rows_follow_the_conventions(e: api.EndpointDecl) -> None:
    assert e.path.startswith("/v1/"), e.path
    assert e.success in (200, 201, 202), e
    for param in re.findall(r"\{([^}]+)\}", e.path):
        assert re.fullmatch(r"[a-z_]+", param), e.path
    if e.request is not None:
        assert e.method in ("POST", "PATCH"), e
    assert e.paginated == (e.response or "").startswith("Page["), e
    if e.paginated:
        assert "limit" in e.query, e
    for code in e.errors:
        assert code in ERROR_INFO
    access = ROUTE_ACCESS[e.operation_id]
    if access.scope is RouteScope.PROJECT:
        assert api.PROJECT_HEADER in e.headers, e.operation_id
        assert access.resource is not None
        assert PERMISSIONS[(access.resource, access.action)].level is Level.PROJECT  # type: ignore[index]
    if access.scope is RouteScope.TENANT:
        assert e.path.startswith("/v1/tenants/{tenant_id}/"), e.path
    assert e.operation_id not in CLIENT_OPERATIONS


def test_v018_table_is_last_unique_and_exported() -> None:
    v019 = len(api.V019_ENDPOINTS) + len(api.V020_ENDPOINTS)  # the v0.19 and v0.20 rows
    assert api.ENDPOINTS[-len(V018_ENDPOINTS) - v019 : -v019] == V018_ENDPOINTS
    assert len({e.operation_id for e in V018_ENDPOINTS}) == len(V018_ENDPOINTS) == 23
    keys = [(e.method, e.path) for e in api.ENDPOINTS]
    assert len(keys) == len(set(keys))
    assert set(ROUTE_ACCESS) == {e.operation_id for e in api.ENDPOINTS}
    assert "V018_ENDPOINTS" in api.__all__
    by_op = {e.operation_id: e for e in V018_ENDPOINTS}
    assert by_op["create_library_revision"].headers[-1] == "If-Match"
    assert by_op["update_onboarding"].request == "OnboardingPatch"
    assert ROUTE_ACCESS["admin_describe_catalog"].scope is RouteScope.PLATFORM
    # tokens cannot reach onboarding or samples: those are tenant routes, not token routes
    for op in ("get_onboarding", "update_onboarding", "reset_onboarding", "list_samples",
               "install_sample", "retry_sample", "retire_sample"):  # fmt: skip
        assert ROUTE_ACCESS[op].scope is RouteScope.TENANT
        assert op not in api.TOKEN_TENANT_ROUTES


def test_v018_dtos_resolve() -> None:
    names = {n for e in V018_ENDPOINTS for n in (e.request, e.response) if n}
    for name in names:
        dto = _resolve(name)
        assert dto is not None and issubclass(dto, BaseModel), name
        m = re.fullmatch(r"(?:Page|list)\[(\w+)\]", name)
        inner = m.group(1) if m else name
        # api re-exports every DTO (its JSON schemas and the endpoint checks read api), except
        # the recipe preview pair: mapi_spec.jobs imports api, so they live in jobs only
        if inner not in ("RecipePreviewRequest", "RecipePreviewResult"):
            assert getattr(api, inner) is dto and inner in api.__all__, inner


def test_v018_error_codes() -> None:
    want = {
        ErrorCode.LIBRARY_INVALID: 422, ErrorCode.LIBRARY_DATA_SHAPE: 422,
        ErrorCode.SCHEMA_UNSAFE: 422, ErrorCode.EXTENSION_UNAVAILABLE: 409,
        ErrorCode.SAMPLES_DISABLED: 404, ErrorCode.PREVIEW_DISABLED: 409,
    }  # fmt: skip
    for code, status in want.items():
        assert ERROR_INFO[code].status == status


# =================================================================== v0.18 permission rows


@pytest.mark.parametrize(
    ("role", "action", "resource", "allowed"),
    [
        (ProjectRole.CLIENT, Action.READ, ResourceKind.LIBRARY, False),
        (ProjectRole.VIEWER, Action.READ, ResourceKind.LIBRARY, True),
        (ProjectRole.VIEWER, Action.CREATE, ResourceKind.LIBRARY, False),
        (ProjectRole.EDITOR, Action.CREATE, ResourceKind.LIBRARY, True),
        (ProjectRole.EDITOR, Action.UPDATE, ResourceKind.LIBRARY, True),
        (ProjectRole.EDITOR, Action.DELETE, ResourceKind.LIBRARY, True),
        (ProjectRole.VIEWER, Action.READ, ResourceKind.TENANT_LIBRARY, True),
        (ProjectRole.EDITOR, Action.CREATE, ResourceKind.TENANT_LIBRARY, False),
        (ProjectRole.RUNNER, Action.UPDATE, ResourceKind.TENANT_LIBRARY, False),
        (ProjectRole.ADMIN, Action.CREATE, ResourceKind.TENANT_LIBRARY, True),
        (ProjectRole.ADMIN, Action.UPDATE, ResourceKind.CATALOG, False),  # operators only
    ],
)
def test_v018_permission_rows(
    role: ProjectRole, action: Action, resource: ResourceKind, allowed: bool
) -> None:
    assert can(role, action, resource) is allowed
    if resource is ResourceKind.TENANT_LIBRARY and action is not Action.READ:
        assert PERMISSIONS[(resource, action)].level is Level.TENANT


def test_tokens_read_the_library_but_write_only_with_admin() -> None:
    assert scope_allows(["models:read"], "read", "library")
    for action in ("create", "update", "delete"):
        for scope in ("models:read", "runs:create", "workflows:write", "datasets:write"):
            assert not scope_allows([scope], action, "library"), (scope, action)
        assert scope_allows(["admin"], action, "library")
