"""Plugin workflow templates (v0.18): the ``mapi.workflow_templates`` entry-point group,
validation against the merged node contracts and the diagnostics of refused templates."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec.catalog import Catalog, TemplateCatalogEntry
from mapi_spec.library_workflows import all_templates, node_contracts
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from mapi_workflows.runtime.nodes import (
    ENTRY_POINT_GROUP,
    TEMPLATE_ENTRY_POINT_GROUP,
    all_node_contracts,
    load_plugin_templates,
    plugin_node_contracts,
)
from test_plugin_nodes import ACME_ABOUT, ACME_DIST, ZERO_SHARE, FakeEP, install

_DOCUMENT: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "Workflow",
    "metadata": {"name": "acme-weekly-check", "project": "templates"},
    "inputs": {"data": {"type": "dataset"}},
    "nodes": [
        {
            "id": "zeros",
            "use": "test.acme_zero_share",
            "with": {"threshold": 0.2},
            "inputs": {"data": "inputs.data"},
        },
        {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
    ],
    "outputs": {"zeros": "zeros.result", "adf": "adf.result"},
}

WEEKLY_CHECK = TemplateCatalogEntry(
    id="acme_weekly_check",
    version="1.0.0",
    about=ACME_ABOUT.model_copy(
        update={
            "title": "Weekly data check",
            "summary_plain": "Checks weekly sales for zeros and for a wandering level.",
            "example": "A store chain checks its weekly sales before it fits a model.",
        }
    ),
    document=_DOCUMENT,
    tags=["retail"],
)


def _entry(**update: Any) -> TemplateCatalogEntry:
    return WEEKLY_CHECK.model_copy(update=update)


def _with_node(monkeypatch: pytest.MonkeyPatch, templates: list[FakeEP]) -> None:
    install(
        monkeypatch,
        {
            ENTRY_POINT_GROUP: [FakeEP("acme", [ZERO_SHARE])],
            TEMPLATE_ENTRY_POINT_GROUP: templates,
        },
    )


def test_a_valid_template_is_loaded_with_its_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_node(monkeypatch, [FakeEP("acme", WEEKLY_CHECK, value="acme:TEMPLATE")])
    entries, diags = load_plugin_templates(all_node_contracts())
    assert diags == []
    assert [e.id for e in entries] == ["acme_weekly_check"]
    assert entries[0].provider == ACME_DIST
    assert entries[0].document == _DOCUMENT


def test_a_template_needs_the_plugin_node_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validated against the built-ins only, the acme node is unknown: the template is
    refused (``template_invalid``) and the reasons are plugin-side text."""
    _with_node(monkeypatch, [FakeEP("acme", [WEEKLY_CHECK], value="acme:TEMPLATES")])
    entries, diags = load_plugin_templates(all_node_contracts(plugins=[]))
    assert entries == []
    assert [d.code for d in diags] == ["template_invalid"]
    d = diags[0]
    assert d.group == TEMPLATE_ENTRY_POINT_GROUP and d.id == "acme_weekly_check"
    assert d.untrusted_text is not None and "/nodes/0/use" in d.untrusted_text


def test_an_invalid_document_is_dropped_and_the_others_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = copy.deepcopy(_DOCUMENT)
    broken["nodes"][1]["inputs"] = {"data": "nowhere.data"}
    _with_node(
        monkeypatch,
        [
            FakeEP(
                "acme",
                lambda: [_entry(id="acme_broken", document=broken), WEEKLY_CHECK],
                value="acme:templates",
            )
        ],
    )
    entries, diags = load_plugin_templates(all_node_contracts())
    assert [e.id for e in entries] == ["acme_weekly_check"]
    assert [(d.code, d.id) for d in diags] == [("template_invalid", "acme_broken")]


@pytest.mark.parametrize(
    ("export", "dist", "code"),
    [
        pytest.param(_entry(id="ts_acme_check"), ACME_DIST, "namespace_reserved", id="builtin-ns"),
        pytest.param(
            _entry(id="tenant_acme_check"), ACME_DIST, "namespace_reserved", id="upload-ns"
        ),
        pytest.param(
            {**WEEKLY_CHECK.model_dump(mode="json"), "id": "t_acme_check"},
            ACME_DIST,
            "template_invalid",
            id="platform-ns",
        ),
        pytest.param(
            _entry(about=ACME_ABOUT.model_copy(update={"summary_plain": "checks"})),
            ACME_DIST,
            "about_incomplete",
            id="no-about",
        ),
        pytest.param({"id": "acme_x"}, ACME_DIST, "template_invalid", id="bad-entry"),
        pytest.param("acme_weekly_check", ACME_DIST, "template_invalid", id="not-an-entry"),
    ],
)
def test_a_refused_template_is_a_diagnostic(
    monkeypatch: pytest.MonkeyPatch, export: Any, dist: str, code: str
) -> None:
    _with_node(monkeypatch, [FakeEP("acme", [export], dist=dist, value="acme:T")])
    entries, diags = load_plugin_templates(all_node_contracts())
    assert entries == []
    assert [d.code for d in diags] == [code]
    assert diags[0].distribution == dist


def test_a_builtin_distribution_may_use_a_builtin_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin = _entry(id="ts_acme_check")
    _with_node(monkeypatch, [FakeEP("ts", [builtin], dist="mapi-plugin-ts", value="x:T")])
    entries, diags = load_plugin_templates(all_node_contracts())
    assert diags == []
    assert entries == [builtin]  # no provider for a built-in distribution


def test_ids_collide_with_builtins_and_earlier_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    taken = next(i for i in WORKFLOW_TEMPLATES if "_" in i)
    _with_node(
        monkeypatch,
        [
            FakeEP("a", [WEEKLY_CHECK, _entry(id=taken)], dist="mapi-workflows", value="x:A"),
            FakeEP("b", [WEEKLY_CHECK], dist="acme-copy", value="x:B"),
        ],
    )
    entries, diags = load_plugin_templates(all_node_contracts())
    assert [e.id for e in entries] == ["acme_weekly_check"]
    assert [(d.code, d.id) for d in diags] == [
        ("id_collision", taken),
        ("id_collision", "acme_weekly_check"),
    ]


def test_a_broken_entry_point_is_a_load_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_node(
        monkeypatch,
        [
            FakeEP("broken", ImportError("no module named acme_lab"), value="acme_lab:T"),
            FakeEP("acme", WEEKLY_CHECK, value="acme:T"),
        ],
    )
    entries, diags = load_plugin_templates(all_node_contracts())
    assert [e.id for e in entries] == ["acme_weekly_check"]
    assert [d.code for d in diags] == ["load_error"]


def test_the_exports_round_trip_through_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the runner exports (plugin contracts and templates) is what the control plane
    merges back with :mod:`mapi_spec.library_workflows`."""
    _with_node(monkeypatch, [FakeEP("acme", WEEKLY_CHECK, value="acme:T")])
    contracts = plugin_node_contracts()
    entries, _ = load_plugin_templates(all_node_contracts(contracts))
    catalog = Catalog.model_validate(
        Catalog(workflow_nodes=contracts, workflow_templates=entries).model_dump(mode="json")
    )
    assert "test.acme_zero_share" in node_contracts(catalog)
    merged = all_templates(catalog)
    assert merged["acme_weekly_check"].source == "plugin"
    assert set(WORKFLOW_TEMPLATES) < set(merged)
