"""The generated node reference page, the registry and the import boundaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS
from mapi_workflows.orchestrator import ORCHESTRATOR_NODES
from mapi_workflows.reference import node_reference
from mapi_workflows.runtime.nodes import builtin_nodes, sandbox_node_ids

PAGE = Path(__file__).resolve().parents[3] / "docs" / "reference" / "workflow-nodes.md"


def test_the_committed_reference_page_matches_the_catalog() -> None:
    assert PAGE.read_text() == node_reference(), (
        "docs/reference/workflow-nodes.md is stale: run "
        "`uv run python -m mapi_workflows.reference > docs/reference/workflow-nodes.md`"
    )


def test_every_node_of_the_catalog_has_an_implementation() -> None:
    impls = builtin_nodes()
    for node_id in sandbox_node_ids():
        assert node_id in impls, node_id
    orchestrated = {i for i, n in NODE_IMPLEMENTATIONS.items() if n.runs_in == "orchestrator"}
    assert orchestrated - {"branch.if"} == set(ORCHESTRATOR_NODES)
    assert set(impls) | set(ORCHESTRATOR_NODES) | {"branch.if", "subworkflow.run"} == set(
        NODE_IMPLEMENTATIONS
    )


def test_the_engine_core_imports_no_array_library() -> None:
    code = (
        "import sys, mapi_workflows, mapi_workflows.engine, mapi_workflows.orchestrator, "
        "mapi_workflows.binding, mapi_workflows.reference\n"
        "bad = [m for m in ('numpy', 'xarray', 'pandas', 'scipy', 'statsmodels', 'mapi_sdk') "
        "if m in sys.modules]\n"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
