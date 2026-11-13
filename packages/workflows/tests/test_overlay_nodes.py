"""Workflow nodes, templates and governance checks from uploaded code (v0.19, D17, G3).

The loaders follow the SDK registry's path rule: an entry point whose distribution lives
under a mounted overlay directory is an upload whatever its name. Its nodes and templates
must use the overlay's namespace (and carry ``upload:<pvid>``); a legacy mapping export and
governance check runners from an upload are refused with a diagnostic.

The overlays are written by the test and loaded only in a fresh ``python -I`` child.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

NS = "t_k3vq7m_acme"
PVID = "plv_01jbz3q2x8m9v7k5h4g3f2e1d1"

_MODULE = textwrap.dedent(
    """
    from mapi_spec.about import About
    from mapi_spec.catalog import TemplateCatalogEntry

    NS = {ns!r}
    TEMPLATE_ID = {template_id!r}
    ABOUT = About(
        title="Weekly level check",
        summary_plain="Checks whether the weekly level of a series wanders over time.",
        use_when=["You want to know if a series has a stable level"],
        avoid_when=["You have fewer than twenty weeks of data"],
        data_needs=["One numeric series over time"],
        assumptions=["The series is measured at a regular interval"],
        outputs=["A yes/no verdict with its evidence"],
        difficulty="beginner",
        typical_runtime="seconds",
        example="A store checks its weekly sales before it fits a model.",
    )
    DOCUMENT = {{
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {{"name": "weekly-level-check", "project": "templates"}},
        "inputs": {{"data": {{"type": "dataset"}}}},
        "nodes": [{{"id": "adf", "use": "test.adf", "inputs": {{"data": "inputs.data"}}}}],
        "outputs": {{"adf": "adf.result"}},
    }}
    TEMPLATES = [
        TemplateCatalogEntry(id=TEMPLATE_ID, version="1.0.0", about=ABOUT, document=DOCUMENT)
    ]


    def NODES():
        from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NodeImplementation
        from mapi_workflows.runtime.nodes import NodeExport, builtin_nodes

        base = NODE_IMPLEMENTATIONS["test.adf"].model_dump(mode="json")
        contract = NodeImplementation.model_validate(
            {{**base, "id": {node_id!r}, "plugin": {node_plugin!r}}}
        )
        return [NodeExport(contract, builtin_nodes()["test.adf"])]


    def LEGACY():
        from mapi_workflows.runtime.nodes import builtin_nodes

        return {{"test.adf": builtin_nodes()["test.adf"]}}


    CHECKS = {{"null_shuffle": lambda ctx: None}}
    """
)


def make_overlay(
    root: Path,
    *,
    dist: str = "t-k3vq7m-acme",
    template_id: str = f"{NS}_weekly",
    node_id: str = f"test.{NS}_adf",
    node_plugin: str = NS,
    groups: Mapping[str, Mapping[str, str]] | None = None,
) -> Path:
    root.mkdir(parents=True)
    (root / NS).mkdir()
    (root / NS / "__init__.py").write_text(
        _MODULE.format(ns=NS, template_id=template_id, node_id=node_id, node_plugin=node_plugin)
    )
    info = root / f"{NS}-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1.0\n")
    eps = (
        groups
        if groups is not None
        else {
            "mapi.workflow_templates": {"acme": f"{NS}:TEMPLATES"},
            "mapi.governance_checks": {"acme": f"{NS}:CHECKS"},
        }
    )
    (info / "entry_points.txt").write_text(
        "".join(
            f"[{g}]\n" + "".join(f"{k} = {v}\n" for k, v in t.items()) + "\n"
            for g, t in eps.items()
        )
    )
    return root


_CHILD = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    d, ns, pvid = json.loads(sys.argv[1])
    sys.path.append(d)
    from mapi_sdk.registry import set_process_overlays
    set_process_overlays([(Path(d), ns, pvid)])
    from mapi_workflows.runtime.governance import load_check_runners
    from mapi_workflows.runtime.nodes import (
        all_node_contracts, load_node_exports, load_plugin_templates, node_registry,
    )
    exports, node_diags = load_node_exports()
    templates, template_diags = load_plugin_templates(all_node_contracts())
    check_diags = []
    runners = load_check_runners(diagnostics=check_diags)
    only, _ = load_node_exports(only="upload:" + pvid)
    print("RESULT " + json.dumps({
        "nodes": sorted(exports),
        "only_nodes": sorted(only),
        "node_fns": sorted(k for k in node_registry() if ".t_" in k),
        "templates": {t.id: t.provider for t in templates},
        "runners": sorted(runners),
        "diagnostics": [
            x.model_dump(mode="json") for x in node_diags + template_diags + check_diags
        ],
    }))
    """
)


def load_in_child(d: Path) -> dict[str, Any]:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _CHILD, json.dumps([str(d), NS, PVID])],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, proc.stderr[-4000:]
    result: dict[str, Any] = json.loads(lines[-1].removeprefix("RESULT "))
    return result


def nodes_accept_uploads() -> bool:
    from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NodeImplementation

    base = NODE_IMPLEMENTATIONS["test.adf"].model_dump(mode="json")
    try:
        NodeImplementation.model_validate({**base, "id": f"test.{NS}_x", "plugin": NS})
    except ValueError:
        return False
    return True


needs_upload_nodes = pytest.mark.skipif(
    not nodes_accept_uploads(),
    reason="needs NodeImplementation to accept t_<code>_<short> plugins (workflow_nodes.py diff)",
)


def _codes(r: Mapping[str, Any], group: str) -> list[str]:
    return [x["code"] for x in r["diagnostics"] if x["group"] == group]


def test_an_uploaded_template_gets_the_upload_provider(tmp_path: Path) -> None:
    r = load_in_child(make_overlay(tmp_path / "ov"))
    assert r["templates"][f"{NS}_weekly"] == f"upload:{PVID}"


def test_a_renamed_distribution_still_gives_an_uploaded_template(tmp_path: Path) -> None:
    r = load_in_child(make_overlay(tmp_path / "ov", dist="mapi-plugin-ts"))
    assert r["templates"][f"{NS}_weekly"] == f"upload:{PVID}"


@pytest.mark.parametrize("template_id", ["acme_weekly", "t_k3vq7m_beta_weekly"])
def test_a_template_outside_the_claimed_namespace_is_refused(
    tmp_path: Path, template_id: str
) -> None:
    r = load_in_child(make_overlay(tmp_path / "ov", template_id=template_id))
    assert template_id not in r["templates"]
    assert "namespace_reserved" in _codes(r, "mapi.workflow_templates")


def test_uploaded_governance_checks_are_ignored_with_a_diagnostic(tmp_path: Path) -> None:
    r = load_in_child(make_overlay(tmp_path / "ov"))
    assert "null_shuffle" not in r["runners"]
    gov = [x for x in r["diagnostics"] if x["group"] == "mapi.governance_checks"]
    assert [x["code"] for x in gov] == ["load_error"]
    assert "not supported" in gov[0]["message"]


def test_an_uploaded_legacy_mapping_never_fills_a_builtin_node(tmp_path: Path) -> None:
    d = make_overlay(tmp_path / "ov", groups={"mapi.workflow_nodes": {"acme": f"{NS}:LEGACY"}})
    r = load_in_child(d)
    assert r["node_fns"] == []
    rows = [x for x in r["diagnostics"] if x["group"] == "mapi.workflow_nodes"]
    assert [x["code"] for x in rows] == ["node_contract_invalid"]
    assert "mapping" in rows[0]["message"]


@needs_upload_nodes
def test_an_uploaded_node_is_loaded(tmp_path: Path) -> None:
    d = make_overlay(tmp_path / "ov", groups={"mapi.workflow_nodes": {"acme": f"{NS}:NODES"}})
    r = load_in_child(d)
    assert r["nodes"] == [f"test.{NS}_adf"]
    assert r["only_nodes"] == [f"test.{NS}_adf"]
    assert r["node_fns"] == [f"test.{NS}_adf"]


@needs_upload_nodes
@pytest.mark.parametrize(
    ("node_id", "plugin"),
    [("test.t_k3vq7m_beta_adf", "t_k3vq7m_beta"), ("test.acme_adf", "acme")],
)
def test_an_uploaded_node_outside_the_claimed_namespace_is_refused(
    tmp_path: Path, node_id: str, plugin: str
) -> None:
    d = make_overlay(
        tmp_path / "ov",
        node_id=node_id,
        node_plugin=plugin,
        groups={"mapi.workflow_nodes": {"acme": f"{NS}:NODES"}},
    )
    r = load_in_child(d)
    assert r["nodes"] == [] and r["node_fns"] == []
    assert _codes(r, "mapi.workflow_nodes") == ["namespace_reserved"]
