"""The workflows guide's cookbook: every YAML fragment is a valid workflow (statically, against
the node catalog and the test registry's model catalog)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from mapi_spec.workflow import validate_workflow
from mapi_workflows.testing import test_registry

GUIDE = Path(__file__).resolve().parents[3] / "docs" / "guides" / "workflows.md"

# the nodes a fragment may refer to without defining them (added unless the fragment has them)
CONTEXT: list[dict[str, Any]] = [
    {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
    {"id": "wanders", "use": "branch.if", "with": {"condition": "not nodes.adf.reject"}},
    {
        "id": "diff",
        "use": "transform.difference",
        "inputs": {"data": "inputs.data"},
        "when": "wanders.then",
    },
    {
        "id": "folds",
        "use": "split.expanding_window",
        "inputs": {"data": "inputs.data"},
        "with": {"n_folds": 4, "horizons": [1]},
    },
    {
        "id": "fit0",
        "use": "model.fit",
        "inputs": {"data": "inputs.data"},
        "with": {"model": "ts.sarimax@^1"},
    },
    {
        "id": "naive0",
        "use": "evaluate.baseline",
        "inputs": {"data": "inputs.data", "split": "folds.plan"},
    },
    {"id": "cmp0", "use": "compare.models", "inputs": {"candidates": ["naive0.metrics"]}},
    {
        "id": "select",
        "use": "select.best",
        "inputs": {"table": "cmp0.table", "models": ["fit0.model"]},
    },
    {"id": "report", "use": "report.summary", "inputs": {"tests": ["adf.result"]}},
]


def _fragments() -> list[str]:
    text = GUIDE.read_text()
    part = text.split("## Cookbook", 1)[1].split("## Where nodes run", 1)[0]
    return re.findall(r"```yaml\n(.*?)```", part, re.S)


def _document(fragment: str) -> dict[str, Any]:
    head, _, tail = fragment.partition("\nexecution:")
    nodes: list[dict[str, Any]] = yaml.safe_load(head)
    ids = {n["id"] for n in nodes}
    doc: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": "cookbook", "project": "docs"},
        "inputs": {"data": {"type": "dataset"}},
        "nodes": [n for n in CONTEXT if n["id"] not in ids] + nodes,
    }
    if tail:
        doc["execution"] = yaml.safe_load("execution:" + tail)["execution"]
    return doc


def test_every_cookbook_fragment_is_a_valid_workflow() -> None:
    catalog = test_registry().catalog()
    fragments = _fragments()
    assert len(fragments) >= 8
    for fragment in fragments:
        if "subworkflow.run" in fragment:
            continue  # needs a stored child revision: see test_templates_local
        res = validate_workflow(_document(fragment), catalog=catalog)
        errors = [(i.pointer, i.message) for i in res.issues if i.severity.value == "error"]
        assert not errors, (fragment, errors)
