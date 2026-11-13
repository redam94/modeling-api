"""The workflow node contracts and templates in effect: built-ins merged with a catalog's
plugin contributions (v0.18).

The control plane validates workflows against :func:`node_contracts` and lists the gallery
from :func:`all_templates`; both take the cached catalog of the current image digest (``None``
before the first ``describe``: built-ins only). Built-ins always win a collision, and a plugin
node is only merged when it is one the sandbox may run (a :data:`PLUGIN_NODE_KINDS` kind,
``runs_in="sandbox"``, ``plugin`` set). The full contract checks, with diagnostics, run when
the server parses the catalog (``plugin_contract_issues``). Pure and JSON-only.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from mapi_spec.catalog import Catalog
from mapi_spec.workflow import validate_workflow
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, PLUGIN_NODE_KINDS, NodeImplementation
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES, WorkflowTemplate, template_from_entry

__all__ = ["all_templates", "node_contracts"]


def _runnable_plugin_node(c: NodeImplementation) -> bool:
    return (
        c.plugin is not None
        and c.kind in PLUGIN_NODE_KINDS
        and c.runs_in == "sandbox"
        and c.id not in NODE_IMPLEMENTATIONS
    )


def node_contracts(catalog: Catalog | None) -> Mapping[str, NodeImplementation]:
    """Every node contract by id: the built-in nodes plus the catalog's plugin nodes (a
    read-only mapping; the built-in contract wins an id collision)."""
    if catalog is None or not catalog.workflow_nodes:
        return MappingProxyType(NODE_IMPLEMENTATIONS)
    merged = dict(NODE_IMPLEMENTATIONS)
    for c in catalog.workflow_nodes:
        if _runnable_plugin_node(c) and c.id not in merged:
            merged[c.id] = c
    return MappingProxyType(merged)


def all_templates(catalog: Catalog | None) -> dict[str, WorkflowTemplate]:
    """Every gallery template by id: the built-in templates, then the catalog's plugin
    templates whose document validates against :func:`node_contracts` (an invalid one, or one
    whose id is taken, is left out; the server lists it as a ``template_invalid``
    diagnostic)."""
    out = dict(WORKFLOW_TEMPLATES)
    if catalog is None or not catalog.workflow_templates:
        return out
    nodes = node_contracts(catalog)
    for entry in catalog.workflow_templates:
        if entry.id in out:
            continue
        result = validate_workflow(entry.document, catalog=catalog, nodes=nodes)
        if not result.valid:
            continue
        out[entry.id] = template_from_entry(entry)
    return out
