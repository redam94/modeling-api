"""The registry of node implementations that work on data (the sandbox side).

:func:`builtin_nodes` maps every contract node id whose ``runs_in`` is ``sandbox`` or
``child_run`` (except ``subworkflow.run``, which drivers handle) to its implementation;
orchestrator nodes (branches, comparisons, selections, reports) are computed by the engine.
:func:`node_registry` adds the implementations other distributions export under the
``mapi.workflow_nodes`` entry point group (only for ids the contract catalog knows: a node
must have its ports, parameters and About card in :mod:`mapi_spec.workflow_nodes`).
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from typing import Final

from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, SUBWORKFLOW_NODE
from mapi_workflows.runtime.evaluate import EVALUATE_NODES
from mapi_workflows.runtime.governance import GOVERNANCE_NODES
from mapi_workflows.runtime.models import MODEL_NODES
from mapi_workflows.runtime.plots import PLOT_NODES
from mapi_workflows.runtime.scoring import SCORE_NODES
from mapi_workflows.runtime.search import SEARCH_NODES
from mapi_workflows.runtime.splits import SPLIT_NODES
from mapi_workflows.runtime.stattests import TEST_NODES
from mapi_workflows.runtime.transforms import TRANSFORM_NODES
from mapi_workflows.runtime.values import NodeFn

__all__ = ["ENTRY_POINT_GROUP", "builtin_nodes", "node_registry", "sandbox_node_ids"]

ENTRY_POINT_GROUP: Final = "mapi.workflow_nodes"


def builtin_nodes() -> dict[str, NodeFn]:
    """Every built-in data node implementation by node id."""
    out: dict[str, NodeFn] = {}
    for table in (
        TEST_NODES,
        TRANSFORM_NODES,
        SPLIT_NODES,
        SEARCH_NODES,
        MODEL_NODES,
        EVALUATE_NODES,
        GOVERNANCE_NODES,
        SCORE_NODES,
        PLOT_NODES,
    ):
        out.update(table)
    return out


def sandbox_node_ids() -> list[str]:
    """Contract node ids that need data (everything but orchestrator nodes and
    sub-workflows)."""
    return sorted(
        i
        for i, impl in NODE_IMPLEMENTATIONS.items()
        if impl.runs_in != "orchestrator" and i != SUBWORKFLOW_NODE
    )


def node_registry(extra: Mapping[str, NodeFn] | None = None) -> dict[str, NodeFn]:
    """Built-ins, entry-point exports (contract ids only, built-ins win) and ``extra``
    (which wins: tests and drivers can override)."""
    out = builtin_nodes()
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        if ep.value.startswith("mapi_workflows."):
            continue
        try:
            obj = ep.load()
            table = obj() if callable(obj) else obj
        except Exception:
            continue
        if isinstance(table, Mapping):
            for k, v in table.items():
                if (
                    isinstance(k, str)
                    and k in NODE_IMPLEMENTATIONS
                    and k not in out
                    and callable(v)
                ):
                    out[k] = v
    out.update(extra or {})
    return out
