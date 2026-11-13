"""Which catalog ids a document names, and the overlay pins they need (v0.19, WP7, D9).

Pure tests of :mod:`mapi_server.services.plugins.refs`: the ids of a model spec (steps,
model, plot recipes), of a workflow (nodes, uploaded ids in node parameters, the gallery
template it came from), the generic scan of stored payloads, and how pins from several
sources merge (one version per plugin, at most eight plugins).
"""

from __future__ import annotations

from typing import Any

import pytest
from mapi_server.services.plugins.refs import (
    TEMPLATE_LABEL,
    document_ids,
    merge_pins,
    model_version_pins,
    spec_catalog_ids,
    uploaded_ids,
    workflow_catalog_ids,
)
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.loader import parse_document
from mapi_spec.plugins import OVERLAYS_MAX, OverlayPin
from mapi_spec.spec import ModelSpec
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS

NS = "t_k3vq7m_acme"
NS2 = "t_k3vq7m_other"


def pin(ns: str = NS, n: int = 1) -> OverlayPin:
    return OverlayPin(
        plugin_version_id=f"plv_01{n:024d}", namespace=ns, sha256="sha256:" + f"{n:02x}" * 32
    )


SPEC = f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: trend, project: default}}
data:
  dataset: dataset://default/co2@latest
pipeline:
  - {{id: a, use: core.standardize@^1, with: {{vars: [y], over: [t]}}}}
  - {{id: b, use: {NS}.winsorize@^1}}
model:
  use: ts.hsgp_trend@^1
outputs:
  plots: [core.ppc_fan, {NS}.fan@1.0.0]
"""


def test_spec_ids_are_steps_model_and_recipes() -> None:
    spec = parse_document(_yaml(SPEC))
    assert isinstance(spec, ModelSpec)
    assert spec_catalog_ids(spec) == {
        "core.standardize",
        f"{NS}.winsorize",
        "ts.hsgp_trend",
        "core.ppc_fan",
        f"{NS}.fan",
    }
    assert uploaded_ids(spec_catalog_ids(spec)) == {f"{NS}.winsorize", f"{NS}.fan"}


def _yaml(text: str) -> dict[str, Any]:
    import yaml

    out: dict[str, Any] = yaml.safe_load(text)
    return out


def test_workflow_ids_cover_nodes_parameters_and_template() -> None:
    doc: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": "w", "project": "default", "labels": {TEMPLATE_LABEL: f"{NS}_flow"}},
        "inputs": {"data": {"type": "dataset"}},
        "nodes": [
            {"id": "adf", "use": "test.adf@1.0.0", "inputs": {"data": "inputs.data"}},
            {"id": "z", "use": f"test.{NS}_zeros", "inputs": {"data": "inputs.data"}},
            {
                "id": "fit",
                "use": "model.fit",
                "inputs": {"data": "inputs.data"},
                "with": {
                    "model": f"{NS2}.store_mean@^1",
                    "pipeline": [{"id": "w", "use": f"{NS}.winsorize"}],
                },
            },
            {"id": "p", "use": "plot.render", "with": {"recipe": f"{NS}.fan@1.0.0"}},
        ],
    }
    found = workflow_catalog_ids(doc, NODE_IMPLEMENTATIONS)
    assert {"test.adf", "model.fit", "plot.render"} <= found
    assert uploaded_ids(found) == {
        f"test.{NS}_zeros",
        f"{NS2}.store_mean",
        f"{NS}.winsorize",
        f"{NS}.fan",
        f"{NS}_flow",
    }


def test_document_scan_finds_every_uploaded_form() -> None:
    payload = {
        "request": {"model": "model://default/m@v1"},
        "plots": [f"{NS}.fan@^1", "core.fan"],
        "nested": {"keys": {f"step.{NS}_x": 1}, "t": f"{NS}_tmpl"},
        "noise": ["t_short", "t_k3vq7m_", "tenant.x", "t_k3vq7m_acme", 42, None],
    }
    assert document_ids(payload) == {f"{NS}.fan", f"step.{NS}_x", f"{NS}_tmpl"}


def test_merge_pins_dedupes_and_sorts() -> None:
    a, b = pin(NS, 1), pin(NS2, 2)
    assert merge_pins([b, a], [a]) == [a, b]
    assert merge_pins() == []


def test_two_versions_of_one_plugin_are_refused() -> None:
    with pytest.raises(MapiError) as err:
        merge_pins([pin(NS, 1)], [pin(NS, 2)])
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE
    assert NS in err.value.detail


def test_more_than_eight_plugins_are_refused() -> None:
    many = [pin(f"t_k3vq7m_p{i}x", i + 1) for i in range(OVERLAYS_MAX + 1)]
    with pytest.raises(MapiError) as err:
        merge_pins(many)
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE


def test_model_version_pins_come_from_the_lineage() -> None:
    p = pin()
    assert model_version_pins({"overlays": [p.model_dump(mode="json")]}) == [p]
    assert model_version_pins(None) == model_version_pins({}) == []
    # the legacy single hash names no version: such a version cannot run uploaded code again
    with pytest.raises(MapiError) as err:
        model_version_pins({"overlay_hash": "sha256:" + "ab" * 32})
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE
    with pytest.raises(MapiError) as err:
        model_version_pins({"overlays": [{"plugin_version_id": "nope"}]})
    assert err.value.code is ErrorCode.INTEGRITY_FAILURE
