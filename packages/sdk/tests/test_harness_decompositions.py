"""Which outputs the harness treats as additive decompositions (contributions)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from mapi_sdk.harness import _decompositions
from mapi_spec.capabilities import OutputDecl


def _fitted(outputs: dict[str, dict[str, Any]]) -> Any:
    decls = {k: OutputDecl.model_validate(v) for k, v in outputs.items()}
    return SimpleNamespace(manifest=SimpleNamespace(outputs=decls))


def test_contributions_are_decompositions_but_vector_fields_are_not() -> None:
    fitted = _fitted(
        {
            "y_hat": {"dims": ["t"]},
            "contributions": {"dims": ["component", "t"]},
            "traction": {
                "dims": ["component", "row", "col"],
                "grid": {"y": "row", "x": "col", "component": "component"},
            },
        }
    )
    assert _decompositions(fitted) == ["contributions"]
