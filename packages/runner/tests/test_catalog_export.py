"""The image catalog export (v0.18): ``full_catalog`` for ``describe`` and ``warm.py``.

A fake distribution (``acme-lab``) is installed next to the real entry points by wrapping
:func:`importlib.metadata.entry_points`: one plot recipe, one workflow node, one workflow
template, plus exports the loaders must refuse. An image of built-ins only must export the
exact catalog ``Registry.catalog()`` gives (same JSON, same digest).
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
from pathlib import Path
from typing import Any

import pytest
import xarray as xr
from mapi_plots.recipes import get_recipe
from mapi_runner.catalog_export import cap_diagnostics, extension_versions, full_catalog
from mapi_runner.worker.validate import run_describe
from mapi_sdk.registry import Registry
from mapi_spec.catalog import (
    CATALOG_DIAGNOSTICS_MAX,
    DIAGNOSTICS_PER_DISTRIBUTION_MAX,
    Catalog,
    CatalogDiagnostic,
    TemplateCatalogEntry,
)
from mapi_spec.jobs import PLOT_EXAMPLES_OUTPUT
from mapi_spec.node_authoring import about, num, schema
from mapi_spec.run import catalog_digest
from mapi_spec.workflow_nodes import NodeImplementation, NodeKind
from mapi_workflows.runtime.nodes import NodeExport
from mapi_workflows.runtime.values import NodeContext, NodeOutcome

pytestmark = pytest.mark.filterwarnings("ignore")

ACME = "acme-lab"

ACME_ABOUT = about(
    "Share of zeros",
    "Counts how many values of the target are zero.",
    use=["Before a count model"],
    avoid=["The data has no zeros by design"],
    needs=["A numeric target"],
    assumes=["Zeros are real values, not missing values"],
    outputs=["The share of zeros and a verdict"],
    difficulty="beginner",
    runtime="instant",
    example="In weekly store sales, 12% of the weeks have zero sales.",
)


def _zero_share(ctx: NodeContext) -> NodeOutcome:
    dv = ctx.dataset("data")
    assert dv is not None
    data: xr.Dataset = dv.data
    share = float((data["sales"].values == 0).mean())
    return NodeOutcome(result={"share": share}, outputs={"result": {"statistic": share}})


ZERO_SHARE = NodeExport(
    contract=NodeImplementation(
        id="test.acme_zero_share",
        version="1.0.0",
        kind=NodeKind.TEST,
        plugin="acme",
        inputs={"data": {"type": "dataset", "description": "The data to test"}},
        outputs={"result": "test_result"},
        params_schema=schema(
            {"threshold": num("The largest share of zeros that passes", 0.2, 0, 1)}
        ),
        result_fields={"share": {"type": "number", "description": "The share of zeros"}},
        about=ACME_ABOUT,
    ),
    fn=_zero_share,
)
RESERVED_NODE = NodeExport(
    contract=ZERO_SHARE.contract.model_copy(update={"id": "test.core_zero", "plugin": "core"}),
    fn=_zero_share,
)
TEMPLATE = TemplateCatalogEntry(
    id="acme_weekly_check",
    version="1.0.0",
    about=ACME_ABOUT.model_copy(update={"title": "Weekly data check"}),
    document={
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
            }
        ],
        "outputs": {"zeros": "zeros.result"},
    },
)
FAN = get_recipe("core.ppc_fan")
ACME_FAN = dataclasses.replace(FAN, id="acme.fan", example=None)
SQUATTER = dataclasses.replace(FAN, id="core.acme_fan", example=None)


class _Dist:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeEP:
    """An entry point of the fake distribution (``obj`` an exception: loading raises it)."""

    def __init__(self, name: str, obj: Any, value: str = "acme_lab:EXPORTS") -> None:
        self.name, self._obj, self.value, self.dist = name, obj, value, _Dist(ACME)

    def load(self) -> Any:
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


_REAL_ENTRY_POINTS = importlib.metadata.entry_points


def install(monkeypatch: pytest.MonkeyPatch, extra: dict[str, list[FakeEP]]) -> None:
    """The real entry points of every group plus ``extra``."""

    def entry_points(*, group: str) -> list[Any]:
        return [*_REAL_ENTRY_POINTS(group=group), *extra.get(group, [])]

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)


@pytest.fixture
def acme(monkeypatch: pytest.MonkeyPatch) -> Registry:
    install(
        monkeypatch,
        {
            "mapi.plots": [FakeEP("acme", [ACME_FAN, SQUATTER])],
            "mapi.workflow_nodes": [FakeEP("acme", [ZERO_SHARE, RESERVED_NODE])],
            "mapi.workflow_templates": [FakeEP("acme", TEMPLATE, value="acme_lab:TEMPLATE")],
            "mapi.governance_checks": [FakeEP("acme", ImportError("no module named acme_gov"))],
        },
    )
    registry = Registry()
    registry.load_entry_points()
    return registry


def _builtin_registry() -> Registry:
    registry = Registry()
    registry.load_entry_points()
    return registry


# ============================================================================ built-ins


def test_a_builtin_image_exports_exactly_the_registry_catalog() -> None:
    registry = _builtin_registry()
    plain = registry.catalog()
    full = full_catalog(registry)
    assert full.model_dump_json(indent=2) == plain.model_dump_json(indent=2)
    assert catalog_digest(full) == catalog_digest(plain)
    raw = json.loads(full.model_dump_json())
    assert not {"workflow_nodes", "workflow_templates", "diagnostics"} & set(raw)
    assert registry.diagnostics == [] and extension_versions(full) == {}


# ========================================================================= a fake dist


def test_plugin_nodes_templates_providers_and_diagnostics_are_exported(
    monkeypatch: pytest.MonkeyPatch, acme: Registry
) -> None:
    catalog = full_catalog(acme)
    monkeypatch.undo()
    builtin = _builtin_registry().catalog()
    # built-in entries are untouched; the acme recipe is added with its provider
    assert [p for p in catalog.plots if p.id != "acme.fan"] == builtin.plots
    assert catalog.steps == builtin.steps and catalog.models == builtin.models
    (fan,) = [p for p in catalog.plots if p.id == "acme.fan"]
    assert fan.provider == ACME
    assert [n.id for n in catalog.workflow_nodes] == ["test.acme_zero_share"]
    assert catalog.workflow_nodes[0] == ZERO_SHARE.contract
    assert [(t.id, t.provider) for t in catalog.workflow_templates] == [("acme_weekly_check", ACME)]
    rows = {(d.group, d.code, d.id) for d in catalog.diagnostics}
    assert ("mapi.plots", "namespace_reserved", "core.acme_fan") in rows
    assert ("mapi.workflow_nodes", "namespace_reserved", "test.core_zero") in rows
    assert ("mapi.governance_checks", "load_error", None) in rows
    assert all(d.distribution == ACME for d in catalog.diagnostics)
    # plugin text stays in untrusted_text; the message is platform-authored
    gov = next(d for d in catalog.diagnostics if d.group == "mapi.governance_checks")
    assert "acme_gov" in (gov.untrusted_text or "") and "acme_gov" not in gov.message
    # what the control plane parses: the JSON round-trips and has its own digest
    again = Catalog.model_validate_json(catalog.model_dump_json(indent=2))
    assert again == catalog
    assert catalog_digest(again) != catalog_digest(builtin)
    assert extension_versions(catalog) == {ACME: "unknown"}  # no such installed dist


def test_a_template_of_an_unknown_node_is_a_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        {"mapi.workflow_templates": [FakeEP("acme", [TEMPLATE], value="acme_lab:TEMPLATES")]},
    )
    catalog = full_catalog(_builtin_registry())
    assert catalog.workflow_templates == [] and catalog.workflow_nodes == []
    assert [(d.code, d.id) for d in catalog.diagnostics] == [
        ("template_invalid", "acme_weekly_check")
    ]


class _Ctx:
    """The parts of ``WorkerContext`` that ``run_describe`` uses."""

    def __init__(self, registry: Registry, out: Path) -> None:
        self.registry, self.out = registry, out
        self.warnings: list[tuple[str, str, str | None]] = []

    def state(self, *_: Any) -> None:
        pass

    def progress(self, *_: Any, **__: Any) -> None:
        pass

    def warning(self, code: str, message: str, untrusted_text: str | None = None) -> None:
        self.warnings.append((code, message, untrusted_text))

    def write_output(self, name: str, data: bytes) -> Path:
        path = self.out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_json(self, name: str, obj: Any) -> Path:
        return self.write_output(name, json.dumps(obj, sort_keys=True).encode())


def test_describe_writes_the_full_catalog_and_counts(acme: Registry, tmp_path: Path) -> None:
    ctx = _Ctx(acme, tmp_path)
    info = run_describe(ctx)  # type: ignore[arg-type]
    catalog = Catalog.model_validate_json((tmp_path / "catalog.json").read_bytes())
    assert catalog == full_catalog(acme)
    assert info["catalog_digest"] == catalog_digest(catalog)
    assert info["workflow_nodes"] == 1 and info["workflow_templates"] == 1
    assert info["diagnostics"] == len(catalog.diagnostics) > 0
    assert info["extensions"] == {ACME: "unknown"}
    assert {code for code, _, _ in ctx.warnings} == {"CATALOG_DIAGNOSTIC"}
    assert len(ctx.warnings) == len(catalog.diagnostics)
    assert (tmp_path / PLOT_EXAMPLES_OUTPUT).exists()


def test_describe_of_builtins_has_no_extension_keys(tmp_path: Path) -> None:
    ctx = _Ctx(_builtin_registry(), tmp_path)
    info = run_describe(ctx)  # type: ignore[arg-type]
    assert not {"workflow_nodes", "workflow_templates", "diagnostics", "extensions"} & set(info)
    assert ctx.warnings == []


# ================================================================================= caps


def _diag(dist: str | None, i: int) -> CatalogDiagnostic:
    return CatalogDiagnostic(
        code="load_error",
        group="mapi.models",
        entry_point=f"ep{i}",
        distribution=dist,
        message="The entry point could not be loaded.",
    )


def test_diagnostics_are_capped_per_distribution_with_a_count_row() -> None:
    rows = [_diag("noisy", i) for i in range(DIAGNOSTICS_PER_DISTRIBUTION_MAX + 7)]
    rows += [_diag("quiet", 0)]
    out = cap_diagnostics(rows)
    noisy = [d for d in out if d.distribution == "noisy"]
    assert len(noisy) == DIAGNOSTICS_PER_DISTRIBUTION_MAX + 1
    assert noisy[-1].entry_point is None and "7 more diagnostics" in noisy[-1].message
    assert [d.entry_point for d in out if d.distribution == "quiet"] == ["ep0"]
    assert cap_diagnostics(rows[:3]) == rows[:3]


def test_diagnostics_are_capped_in_total() -> None:
    rows = [_diag(f"dist{i}", i) for i in range(CATALOG_DIAGNOSTICS_MAX + 30)]
    out = cap_diagnostics(rows)
    assert len(out) == CATALOG_DIAGNOSTICS_MAX
    assert out[:-1] == rows[: CATALOG_DIAGNOSTICS_MAX - 1]
    assert "31 more diagnostics are not listed" in out[-1].message
    Catalog(diagnostics=out)  # within the catalog's own cap
