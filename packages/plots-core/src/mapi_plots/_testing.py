"""Small JSON-only helpers shared by the test suites (and handy in notebooks)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

__all__ = ["make_plot_doc"]


def make_plot_doc(kind: str, spec: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A minimal PlotSpec document around ``spec`` that declares one dataset named ``d``."""
    doc: dict[str, Any] = {
        "id": "t.plot",
        "title": "test",
        "kind": kind,
        "spec": spec,
        "datasets": {"d": {"kind": "inline_arrow", "data": ""}},
        "schema_version": "v6" if kind == "vega-lite" else "4.1.1",
        "provenance": {
            "author": "tests",
            "generated_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        },
    }
    doc.update(extra)
    return doc
