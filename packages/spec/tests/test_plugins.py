"""Uploaded plugins (v0.19, WP1): namespaces, ids, states, describe re-checks, pins, digests."""

from __future__ import annotations

import copy
import itertools
import json
import re
from typing import Any

import pytest
from mapi_spec import Catalog
from mapi_spec.about import About
from mapi_spec.api import Lineage
from mapi_spec.catalog import PlotCatalogEntry, StepCatalogEntry, TemplateCatalogEntry
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.governance import check_approver
from mapi_spec.namespaces import (
    UPLOAD_NAMESPACE_RE,
    check_namespace,
    namespace_of,
    reserved_namespace,
)
from mapi_spec.plugins import (
    DESCRIBE_MAX_BYTES,
    MOUNTABLE_STATES,
    PLUGIN_ENTRY_POINT_GROUPS,
    PLUGIN_TRANSITIONS,
    PLUGIN_VERSION_STATES,
    TENANT_CODE_RE,
    KitCheck,
    OverlayPin,
    PluginClaim,
    PluginDecision,
    PluginDescribe,
    PluginExports,
    PluginInspection,
    PluginKitReport,
    check_describe,
    check_transition,
    describe_digest,
    is_uploaded_id,
    is_uploaded_namespace,
    lineage_has_uploaded_code,
    lineage_pins,
    new_tenant_code,
    parse_upload_namespace,
    upload_dist_name,
    upload_namespace,
    uploaded_namespace_of,
)
from mapi_spec.run import BundleManifest, OutputManifest, RunSpec
from mapi_spec.sharing import uses_uploaded_ids
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NodeImplementation
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from pydantic import ValidationError

pytestmark = pytest.mark.unit

NS = "t_k3vq7m_acme"
OTHER_NS = "t_b2c3d4_acme"
SHA = "sha256:" + "ab" * 32
PLV = "plv_01jbz3q2x8m9v7k5h4g3f2e1d1"
ABOUT: dict[str, Any] = About.model_json_schema()["examples"][0]


def _uploaded_nodes_supported() -> bool:
    """NodeImplementation accepts an uploaded ``plugin`` namespace (the v0.19 change to
    ``workflow_nodes.py``, owned outside WP1)."""
    try:
        NodeImplementation.model_validate(_node())
    except ValidationError:
        return False
    return True


needs_uploaded_nodes = pytest.mark.skipif(
    "not _uploaded_nodes_supported()",
    reason="needs NodeImplementation to accept t_<code>_<short> plugins (workflow_nodes.py diff)",
)

# ================================================================================ namespaces


def test_namespace_round_trip() -> None:
    ns = upload_namespace("k3vq7m", "acme")
    assert ns == NS
    assert parse_upload_namespace(ns) == ("k3vq7m", "acme")
    assert upload_dist_name(ns) == "t-k3vq7m-acme"
    assert is_uploaded_namespace(ns)
    assert re.fullmatch(UPLOAD_NAMESPACE_RE, ns)
    for _ in range(200):
        code = new_tenant_code()
        assert re.fullmatch(TENANT_CODE_RE, code), code
        assert parse_upload_namespace(upload_namespace(code, "ab")) == (code, "ab")


@pytest.mark.parametrize(
    ("ns", "parsed"),
    [
        ("t_abcdef_ab", ("abcdef", "ab")),
        ("t_abcdef_a1b2c3", ("abcdef", "a1b2c3")),
        ("t_222222_zz", ("222222", "zz")),
        ("t_abcde_fab", None),  # a 5-char code: never re-split into another code
        ("t_abcdefg_ab", None),  # 7-char code
        ("t_abcde1_ab", None),  # '1' is not base32
        ("t_abcdef_a", None),  # short name too short
        ("t_abcdef_" + "a" * 17, None),  # short name too long
        ("t_abcdef_a_b", None),  # no '_' in the short name
        ("t_abcdef_1ab", None),  # short name starts with a letter
        ("t_ABCDEF_ab", None),
        ("tenant_abcdef_ab", None),
        ("acme", None),
        ("", None),
    ],
)
def test_namespace_parse_is_unambiguous(ns: str, parsed: tuple[str, str] | None) -> None:
    assert parse_upload_namespace(ns) == parsed
    assert is_uploaded_namespace(ns) is (parsed is not None)


@pytest.mark.parametrize(
    ("code", "short"),
    [("abcde", "ab"), ("abcdef", "a_b"), ("ABCDEF", "ab"), ("abcdef", "x"), ("abcde1", "ab")],
)
def test_upload_namespace_refuses_bad_parts(code: str, short: str) -> None:
    with pytest.raises(ValueError):
        upload_namespace(code, short)
    with pytest.raises(ValueError):
        upload_dist_name(f"t_{code}_{short}" if "_" not in short else "acme")


@pytest.mark.parametrize(
    ("id_", "ns"),
    [
        (f"{NS}.store_mean", NS),  # model
        (f"{NS}.winsorize", NS),  # step
        (f"{NS}.weekly_total", NS),  # plot
        (f"test.{NS}_zero_share", NS),  # workflow node
        (f"plot.{NS}_x", NS),
        (f"{NS}_weekly_check", NS),  # workflow template
        (f"{NS}.store_mean@^1.0", NS),  # a ref with a range
        (f"{OTHER_NS}.x", OTHER_NS),
    ],
)
def test_uploaded_ids_of_all_five_kinds(id_: str, ns: str) -> None:
    assert uploaded_namespace_of(id_) == ns
    assert is_uploaded_id(id_)
    assert uses_uploaded_ids([id_])


@pytest.mark.parametrize(
    "id_",
    [
        "core.log1p",
        "mmm.geo_mmm",
        "acme.store_mean",
        "test.acme_zero_share",
        "acme_weekly_check",
        NS,  # a namespace alone is not an id
        f"{NS}.",
        f"{NS}_",
        f"test.{NS}",
        "t_abcde_fab.x",
        "test.t_abcde_fab_x",
        f"te_st.{NS}_x",
        f"Test.{NS}_x",
    ],
)
def test_not_uploaded_ids(id_: str) -> None:
    assert not is_uploaded_id(id_)


def test_builtin_ids_are_never_uploaded(catalog: Catalog) -> None:
    ids = [e.id for e in catalog.models + catalog.steps + catalog.plots]
    ids += list(NODE_IMPLEMENTATIONS)
    ids += list(WORKFLOW_TEMPLATES)
    assert len(ids) > 50
    assert not [i for i in ids if is_uploaded_id(i)]
    assert not uses_uploaded_ids(ids)


def test_uses_uploaded_ids_fails_closed_on_malformed_upload_ids() -> None:
    assert uses_uploaded_ids(["core.log1p", "t_bad.x"])
    assert uses_uploaded_ids(["t_bad_template"])
    assert uses_uploaded_ids(["tenant_x.y"])
    assert not uses_uploaded_ids(["core.log1p", "", "test.adf"])


def test_check_namespace_claimed_accepts_only_that_namespace() -> None:
    dist = "t-k3vq7m-acme"
    assert check_namespace(f"{NS}.x", dist, claimed=NS) is None
    assert check_namespace(f"test.{NS}_x", dist, plugin=NS, claimed=NS) is None
    for id_, plugin in [
        (f"{OTHER_NS}.x", None),  # another upload's namespace
        ("core.x", None),  # a built-in namespace
        ("acme.x", None),  # a third-party namespace
        ("x", None),  # no namespace
        (f"test.{OTHER_NS}_x", OTHER_NS),
    ]:
        reason = check_namespace(id_, dist, plugin=plugin, claimed=NS)
        assert reason is not None and NS in reason, id_
    # a claim that is not an uploaded namespace never opens a door
    assert check_namespace("acme.x", "acme", claimed="acme") is not None
    # without a claim, uploaded namespaces stay reserved for everyone (built-ins included)
    assert check_namespace(f"{NS}.x", dist) is not None
    assert check_namespace(f"{NS}.x", "mapi-plugin-mmm") is not None
    assert reserved_namespace(NS) is not None
    assert namespace_of(f"{NS}.x") == NS


# ==================================================================================== states


def test_transition_table_is_exactly_d8() -> None:
    edges = {(a, b) for a, targets in PLUGIN_TRANSITIONS.items() for b in targets}
    assert edges == {
        ("uploaded", "inspecting"),
        ("inspecting", "inspect_failed"),
        ("inspecting", "describing"),
        ("describing", "kit_failed"),
        ("describing", "kit_passed"),
        ("kit_passed", "awaiting_approval"),
        ("awaiting_approval", "approved"),
        ("awaiting_approval", "rejected"),
        ("kit_passed", "yanked"),
        ("awaiting_approval", "yanked"),
        ("approved", "yanked"),
        ("approved", "awaiting_approval"),  # D12 rekit with changed exports
        ("inspect_failed", "purged"),
        ("kit_failed", "purged"),
        ("rejected", "purged"),
        ("yanked", "purged"),
    }
    assert set(PLUGIN_TRANSITIONS) == set(PLUGIN_VERSION_STATES)
    assert {"kit_passed", "awaiting_approval", "approved"} == MOUNTABLE_STATES
    assert len(PLUGIN_ENTRY_POINT_GROUPS) == 5
    assert "mapi.governance_checks" not in PLUGIN_ENTRY_POINT_GROUPS


@pytest.mark.parametrize(("a", "b"), list(itertools.product(PLUGIN_VERSION_STATES, repeat=2)))
def test_every_illegal_edge_is_refused(a: Any, b: Any) -> None:
    if b in PLUGIN_TRANSITIONS[a]:
        check_transition(a, b)
        return
    with pytest.raises(MapiError) as exc:
        check_transition(a, b)
    assert exc.value.code is ErrorCode.PLUGIN_STATE_CONFLICT


def test_purged_is_terminal_and_mountable_states_can_be_yanked() -> None:
    assert PLUGIN_TRANSITIONS["purged"] == frozenset()
    for state in MOUNTABLE_STATES:
        assert "yanked" in PLUGIN_TRANSITIONS[state]


# ========================================================================= describe fixtures


def _node(**over: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": f"test.{NS}_zero_share",
        "kind": "test",
        "inputs": {"data": {"type": "dataset", "description": "The data"}},
        "outputs": {"result": "test_result"},
        "params_schema": {"type": "object", "properties": {"alpha": {"type": "number"}}},
        "about": {**ABOUT, "title": "Zero share test"},
        "plugin": NS,
    }
    node.update(over)
    return node


def _exports(catalog: Catalog, *, nodes: bool = True) -> dict[str, Any]:
    model = next(m for m in catalog.models if m.id == "ts.hsgp").model_dump(mode="json")
    model["manifest"]["id"] = f"{NS}.store_mean"
    model["manifest"]["about"] = {**ABOUT, "title": "Store mean model"}
    step = next(s for s in catalog.steps if s.id == "core.mean_scale").model_dump(mode="json")
    step["id"] = f"{NS}.winsorize"
    step["about"] = {**ABOUT, "title": "Winsorize"}
    plot = {
        "id": f"{NS}.weekly_total",
        "title": "Weekly total",
        "about": {**ABOUT, "title": "Weekly total"},
        "settings_schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
    }
    template = {
        "id": f"{NS}_weekly_check",
        "about": {**ABOUT, "title": "Weekly check"},
        "document": {"nodes": [{"id": "a", "use": f"test.{NS}_zero_share"}]},
    }
    out: dict[str, Any] = {
        "models": [model],
        "steps": [step],
        "plots": [plot],
        "workflow_templates": [template],
    }
    if nodes:
        out["workflow_nodes"] = [_node()]
    return out


def _describe(catalog: Catalog, *, nodes: bool = True, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "plugin_version_id": PLV,
        "overlay_sha256": SHA,
        "base_image_digest": "sha256:" + "cd" * 32,
        "exports": _exports(catalog, nodes=nodes),
        "plot_examples": {f"{NS}.weekly_total": {"kind": "vega-lite"}},
        "kit": {
            "ok": True,
            "checks": [
                {"entry_id": f"{NS}.winsorize", "kind": "step", "check": "check_step",
                 "status": "pass"},
                {"entry_id": f"{NS}.store_mean", "kind": "model", "check": "check_model",
                 "status": "skipped", "message": "No toy fixture fits this model."},
            ],
        },
    }  # fmt: skip
    d.update(over)
    return d


def _issues(d: dict[str, Any], *, titles: tuple[str, ...] = ("Bayesian MMM",)) -> list[Any]:
    return check_describe(PluginDescribe.model_validate(d), namespace=NS, builtin_titles=titles)


def _checks(d: dict[str, Any], **kw: Any) -> set[str]:
    return {str(i.context.get("check")) for i in _issues(d, **kw)}


# ============================================================================ check_describe


def test_a_clean_describe_has_no_issues(catalog: Catalog) -> None:
    assert _issues(_describe(catalog, nodes=False)) == []


@needs_uploaded_nodes
def test_a_clean_describe_with_a_node_has_no_issues(catalog: Catalog) -> None:
    assert _issues(_describe(catalog)) == []


def test_issues_are_errors_with_a_check_name_and_no_uploaded_text_in_message(
    catalog: Catalog,
) -> None:
    d = _describe(catalog, nodes=False)
    d["exports"]["steps"][0]["id"] = "core.evil"  # a built-in namespace
    d["exports"]["plots"][0]["title"] = "Weekly‮ total"
    issues = _issues(d)
    assert issues
    for issue in issues:
        assert issue.severity is Severity.ERROR
        assert issue.context.get("check")
        assert "‮" not in issue.message


def test_refuses_an_id_outside_the_namespace(catalog: Catalog) -> None:
    d = _describe(catalog, nodes=False)
    d["exports"]["steps"][0]["id"] = "acme.winsorize"
    issues = [i for i in _issues(d) if i.context["check"] == "namespace"]
    assert [i.pointer for i in issues] == ["/exports/steps/0/id"]
    assert issues[0].untrusted_text == "acme.winsorize"
    assert issues[0].code is ErrorCode.PLUGIN_REFUSED


@pytest.mark.parametrize(
    ("group", "path", "value"),
    [
        ("steps", ("id",), f"{OTHER_NS}.winsorize"),  # another tenant's namespace
        ("steps", ("id",), "core.winsorize"),  # a built-in id
        ("plots", ("id",), f"{OTHER_NS}.weekly_total"),
        ("models", ("manifest", "id"), "mmm.store_mean"),
        ("workflow_templates", ("id",), f"{OTHER_NS}_weekly_check"),
        ("workflow_templates", ("id",), "acme_weekly_check"),
    ],
)
def test_refuses_foreign_namespaces(
    catalog: Catalog, group: str, path: tuple[str, ...], value: str
) -> None:
    d = _describe(catalog, nodes=False)
    target = d["exports"][group][0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    if group == "plots":
        d["plot_examples"] = {}
    assert "namespace" in _checks(d)


@needs_uploaded_nodes
def test_refuses_a_node_whose_plugin_is_not_the_namespace(catalog: Catalog) -> None:
    d = _describe(catalog)
    d["exports"]["workflow_nodes"] = [_node(id=f"test.{OTHER_NS}_zero_share", plugin=OTHER_NS)]
    issues = [i for i in _issues(d) if i.context["check"] == "namespace"]
    assert {i.pointer for i in issues} == {
        "/exports/workflow_nodes/0/id",
        "/exports/workflow_nodes/0/plugin",
    }


@needs_uploaded_nodes
def test_node_contract_rules_still_apply(catalog: Catalog) -> None:
    d = _describe(catalog)
    d["exports"]["workflow_nodes"] = [_node(outputs={}, runs_in="orchestrator")]
    pointers = {i.pointer for i in _issues(d) if i.context["check"] == "node_contract"}
    assert "/exports/workflow_nodes/0/outputs" in pointers
    assert "/exports/workflow_nodes/0/runs_in" in pointers


@pytest.mark.parametrize("pattern", [r"(a+)+$", r"(a|aa)+$", r"((a+)+)+", r"(\w+\s?)+$"])
def test_refuses_unsafe_schemas(catalog: Catalog, pattern: str) -> None:
    d = _describe(catalog, nodes=False)
    d["exports"]["plots"][0]["settings_schema"] = {
        "type": "object",
        "properties": {"name": {"type": "string", "pattern": pattern}},
    }
    issues = [i for i in _issues(d) if i.context["check"] == "schema_unsafe"]
    assert issues and all(i.code is ErrorCode.SCHEMA_UNSAFE for i in issues)
    assert issues[0].pointer.startswith("/exports/plots/0/settings_schema")


def test_refuses_unsafe_schemas_in_models_and_templates(catalog: Catalog) -> None:
    d = _describe(catalog, nodes=False)
    d["exports"]["models"][0]["manifest"]["fit_schema"] = {
        "type": "object", "properties": {"x": {"type": "string", "pattern": "(a+)+$"}},
    }  # fmt: skip
    d["exports"]["workflow_templates"][0]["document"]["params_schema"] = {
        "type": "object", "properties": {"y": {"$ref": "https://evil.example/schema"}},
    }  # fmt: skip
    pointers = {i.pointer for i in _issues(d) if i.context["check"] == "schema_unsafe"}
    assert any(p.startswith("/exports/models/0/manifest/fit_schema") for p in pointers)
    assert any(
        p.startswith("/exports/workflow_templates/0/document/params_schema") for p in pointers
    )


@pytest.mark.parametrize(
    "url",
    ["https://a@b.example/x", "https://bücher.example/", "https://10.0.0.1/", "http://x.org"],
)
def test_refuses_about_reference_urls(catalog: Catalog, url: str) -> None:
    d = _describe(catalog, nodes=False)
    if url.startswith("http://"):
        # the About model itself refuses non-https links before check_describe runs
        d["exports"]["steps"][0]["about"]["references"] = [{"title": "Paper", "url": url}]
        with pytest.raises(ValidationError):
            PluginDescribe.model_validate(d)
        return
    d["exports"]["steps"][0]["about"]["references"] = [{"title": "Paper", "url": url}]
    issues = [i for i in _issues(d) if i.context["check"] == "reference_url"]
    assert [i.pointer for i in issues] == ["/exports/steps/0/about"]
    assert issues[0].code is ErrorCode.ABOUT_INCOMPLETE


def test_refuses_a_missing_about(catalog: Catalog) -> None:
    d = _describe(catalog, nodes=False)
    del d["exports"]["plots"][0]["about"]
    del d["exports"]["steps"][0]["about"]
    pointers = {i.pointer for i in _issues(d) if i.context["check"] == "about_incomplete"}
    assert pointers == {"/exports/plots/0/about", "/exports/steps/0/about"}


@pytest.mark.parametrize("ch", ["‮", "⁦", "​", "﻿", "\x07"])
def test_refuses_hidden_characters_in_titles(catalog: Catalog, ch: str) -> None:
    d = _describe(catalog, nodes=False)
    d["exports"]["steps"][0]["about"]["title"] = f"Win{ch}sorize"
    issues = [i for i in _issues(d) if i.context["check"] == "text_unsafe"]
    assert [i.pointer for i in issues] == ["/exports/steps/0/about/title"]
    assert ch not in (issues[0].untrusted_text or "")


def test_refuses_hidden_characters_in_diagnostics(catalog: Catalog) -> None:
    d = _describe(catalog, nodes=False)
    d["diagnostics"] = [
        {"code": "load_error", "group": "mapi.steps", "message": "x",
         "untrusted_text": "boom ‮"},
    ]  # fmt: skip
    assert "text_unsafe" in _checks(d)


@pytest.mark.parametrize(
    "title", ["Bayesian MMM", "Bayes1an MMM", "Bаyesian MMM", "bayesian  m.m.m"]
)
def test_refuses_a_title_confusable_with_a_builtin(catalog: Catalog, title: str) -> None:
    d = _describe(catalog, nodes=False)
    d["exports"]["models"][0]["manifest"]["about"]["title"] = title
    issues = [i for i in _issues(d) if i.context["check"] == "title_confusable"]
    assert [i.pointer for i in issues] == ["/exports/models/0/manifest/about/title"]
    assert _checks(d, titles=("Weekly totals",)) - {"text_unsafe"} == set()


def test_refuses_no_exports_and_examples_for_unknown_plots(catalog: Catalog) -> None:
    d = _describe(catalog, nodes=False, exports={}, plot_examples={f"{NS}.nope": {}})
    assert _checks(d) == {"no_exports", "plot_example"}


def test_refuses_duplicates(catalog: Catalog) -> None:
    d = _describe(catalog, nodes=False)
    d["exports"]["steps"].append(copy.deepcopy(d["exports"]["steps"][0]))
    assert "duplicate" in _checks(d)


def test_refuses_more_than_two_mib(catalog: Catalog) -> None:
    d = _describe(catalog, nodes=False)
    big = {"data": {"values": [{"x": "y" * 1000} for _ in range(2200)]}}
    d["plot_examples"] = {f"{NS}.weekly_total": big}
    assert len(json.dumps(d)) > DESCRIBE_MAX_BYTES
    assert "size" in _checks(d)


def test_check_describe_needs_an_uploaded_namespace(catalog: Catalog) -> None:
    with pytest.raises(ValueError):
        check_describe(
            PluginDescribe.model_validate(_describe(catalog, nodes=False)),
            namespace="acme",
            builtin_titles=(),
        )


def test_uploaded_template_ids_validate_and_digests_ignore_new_empty_fields() -> None:
    t = TemplateCatalogEntry.model_validate(
        {"id": f"{NS}_weekly", "about": ABOUT, "document": {"nodes": []}}
    )
    assert "overlay_hash" not in t.model_dump(mode="json")
    with pytest.raises(ValidationError):
        TemplateCatalogEntry.model_validate({"id": "t_weekly", "about": ABOUT, "document": {}})


# ==================================================================================== digests


def test_describe_digest_is_stable_under_key_order(catalog: Catalog) -> None:
    exports = _exports(catalog, nodes=False)
    a = PluginExports.model_validate(exports)
    shuffled = json.loads(json.dumps(exports, sort_keys=True))
    shuffled = {k: shuffled[k] for k in reversed(list(shuffled))}
    b = PluginExports.model_validate(shuffled)
    assert describe_digest(a) == describe_digest(b)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", describe_digest(a))
    exports["steps"][0]["about"]["title"] = "Winsorize (changed)"
    assert describe_digest(PluginExports.model_validate(exports)) != describe_digest(a)
    assert describe_digest(PluginExports()) == describe_digest(PluginExports.model_validate({}))


# ======================================================================================= pins


def test_lineage_pins() -> None:
    pin = {"plugin_version_id": PLV, "namespace": NS, "sha256": SHA}
    assert lineage_pins({"overlays": [pin]}) == [OverlayPin.model_validate(pin)]
    assert lineage_pins(None) == [] and lineage_pins({}) == []
    assert lineage_pins({"overlay_hash": SHA}) == []  # legacy: no version to pin
    assert lineage_has_uploaded_code({"overlay_hash": SHA})
    assert lineage_has_uploaded_code({"overlays": [pin]})
    assert not lineage_has_uploaded_code({"overlays": [], "overlay_hash": None})
    assert not lineage_has_uploaded_code(None)
    for bad in ({"overlays": "x"}, {"overlays": [{"namespace": "acme"}]}):
        with pytest.raises(MapiError) as exc:
            lineage_pins(bad)
        assert exc.value.code is ErrorCode.INTEGRITY_FAILURE
        assert lineage_has_uploaded_code(bad)


def test_overlay_pin_is_strict() -> None:
    with pytest.raises(ValidationError):
        OverlayPin.model_validate({"plugin_version_id": PLV, "namespace": "acme", "sha256": SHA})
    with pytest.raises(ValidationError):
        OverlayPin.model_validate({"plugin_version_id": "run_x", "namespace": NS, "sha256": SHA})


def test_new_fields_are_left_out_when_empty() -> None:
    lineage = Lineage.model_validate(Lineage.model_json_schema()["examples"][0])
    assert "overlays" not in lineage.model_dump(mode="json")
    pinned = Lineage.model_validate(
        {**lineage.model_dump(mode="json"),
         "overlays": [{"plugin_version_id": PLV, "namespace": NS, "sha256": SHA}]}
    )  # fmt: skip
    assert pinned.model_dump(mode="json")["overlays"][0]["namespace"] == NS
    for model, field in (
        (RunSpec, "overlays"),
        (OutputManifest, "overlay_hashes"),
        (BundleManifest, "overlay_hashes"),
    ):
        example = model.model_json_schema()["examples"][0]
        assert field not in model.model_validate(example).model_dump(mode="json"), model
    step = StepCatalogEntry.model_validate({"id": "core.x", "version": "1.0.0", "kind": "value",
                                            "stateful": False})  # fmt: skip
    plot = PlotCatalogEntry.model_validate({"id": "core.p", "title": "P"})
    assert "overlay_hash" not in step.model_dump(mode="json")
    assert "overlay_hash" not in plot.model_dump(mode="json")


def test_runspec_overlays_are_one_per_namespace() -> None:
    example = RunSpec.model_json_schema()["examples"][0]
    get = next(iter(example["inputs"].values()))
    mount = {"pin": {"plugin_version_id": PLV, "namespace": NS, "sha256": SHA}, "get": get,
             "size": 12960}  # fmt: skip
    rs = RunSpec.model_validate({**example, "overlays": [mount]})
    assert rs.overlays[0].pin.namespace == NS
    with pytest.raises(ValidationError):
        RunSpec.model_validate({**example, "overlays": [mount, mount]})


# =============================================================================== DTO guards


def test_request_dtos_normalize_untrusted_text() -> None:
    claim = PluginClaim.model_validate({"short_name": "acme", "title": " Acme‮ tools "})
    assert claim.title == "Acme tools"
    with pytest.raises(ValidationError):
        PluginClaim.model_validate({"short_name": "acme", "title": "​"})
    with pytest.raises(ValidationError):
        PluginClaim.model_validate({"short_name": "a_b", "title": "x"})
    d = PluginDecision.model_validate(
        {"overlay_sha256": SHA, "describe_digest": SHA, "base_image_digest": SHA,
         "note": "Looks⁦ fine"}
    )  # fmt: skip
    assert d.note == "Looks fine"


def test_sandbox_results_are_normalized_and_consistent() -> None:
    check = KitCheck.model_validate(
        {"kind": "step", "check": "check_step", "status": "fail", "message": "bad‮\x00"}
    )
    assert check.message == "bad"
    with pytest.raises(ValidationError):
        PluginKitReport.model_validate({"ok": True, "checks": [check.model_dump()]})
    with pytest.raises(ValidationError):
        PluginInspection.model_validate(
            {"ok": True, "archive_sha256": SHA,
             "problems": [{"check": "pth_file", "message": "A .pth file."}]}
        )  # fmt: skip


# ============================================================================== governance


def test_check_approver_default_message_is_unchanged() -> None:
    with pytest.raises(MapiError) as exc:
        check_approver(submitter_id="u1", approver_id="u1", is_reviewer=True)
    assert (
        str(exc.value.detail) == "the person who submitted a model for validation cannot approve it"
    )
    assert exc.value.code is ErrorCode.SELF_APPROVAL_FORBIDDEN
    with pytest.raises(MapiError) as exc:
        check_approver(
            submitter_id="u1", approver_id="u1", is_reviewer=True, subject="plugin version"
        )
    assert exc.value.detail == "the person who submitted plugin version cannot approve it"
    check_approver(submitter_id="u1", approver_id="u2", is_reviewer=True, subject="plugin version")


def test_refuses_entries_that_claim_another_provenance(catalog: Catalog) -> None:
    d = _describe(catalog, nodes=False)
    d["exports"]["steps"][0]["provider"] = f"upload:{PLV}"  # what the runner sets: fine
    d["exports"]["plots"][0]["overlay_hash"] = SHA
    assert _issues(d) == []
    d["exports"]["models"][0]["provider"] = "mapi-plugin-mmm"
    d["exports"]["workflow_templates"][0]["overlay_hash"] = "sha256:" + "00" * 32
    pointers = {i.pointer for i in _issues(d) if i.context["check"] == "provenance"}
    assert pointers == {"/exports/models/0", "/exports/workflow_templates/0"}
