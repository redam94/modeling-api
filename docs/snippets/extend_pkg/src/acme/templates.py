"""acme_weekly_check: a workflow template scaffolded by ``mapi plugin init --kind template``.

A plugin template is plain JSON: an About card for the gallery plus a workflow document
that people start from. Its id is ``<namespace>_<name>``. The platform validates the
document against the node contracts it knows (built-ins and installed plugin nodes) and
hides a template that does not validate. Replace the document with yours, update the About
card and keep ``mapi plugin test`` (the contract kit) green.
"""

from __future__ import annotations

from typing import Any

from mapi_spec.about import About
from mapi_spec.catalog import TemplateCatalogEntry

__all__ = ["TEMPLATES", "WEEKLY_CHECK"]

DOCUMENT: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "Workflow",
    "metadata": {"name": "weekly-check", "project": "templates"},
    "inputs": {"data": {"type": "dataset"}},
    "nodes": [
        {
            "id": "zeros",
            "use": "test.acme_zero_share",
            "with": {"var": "sales", "threshold": 0.1},
            "inputs": {"data": "inputs.data"},
        },
        {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
    ],
    "outputs": {"zeros": "zeros.result", "adf": "adf.result"},
}
"""The workflow people start from (``POST /v1/workflows {template}`` copies it)."""

WEEKLY_CHECK = TemplateCatalogEntry(
    id="acme_weekly_check",
    version="0.1.0",
    about=About(
        title="Weekly data check",
        summary_plain="Checks weekly sales for too many zeros and for a level that wanders. "
        "Run it before you fit a model.",
        use_when=["You get new weekly data and want to check it before you fit a model"],
        avoid_when=["Your data is not weekly"],
        data_needs=["A weekly series with a numeric target"],
        assumptions=["The weeks are complete and in order"],
        outputs=["A verdict per check, ready for a report"],
        difficulty="beginner",
        typical_runtime="seconds",
        example="A store chain checks its weekly sales every Monday before it fits a model.",
    ),
    document=DOCUMENT,
    tags=["weekly", "checks"],
)

TEMPLATES = [WEEKLY_CHECK]
"""The ``mapi.workflow_templates`` entry point: every template listed here is offered."""
