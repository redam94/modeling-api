"""Sweep documents are retired (v0.14): stored documents still parse; nothing expands them."""

from __future__ import annotations

from typing import Any

import mapi_spec
import pytest
from mapi_spec import Sweep
from mapi_spec.loader import parse_document
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _doc(grid: dict[str, list[Any]]) -> dict[str, Any]:
    return {
        "apiVersion": "mapi/v1",
        "kind": "Sweep",
        "metadata": {"name": "sens", "project": "growth"},
        "base": "spec://growth/eu-geo-mmm@r2",
        "grid": grid,
        "compare_by": "elpd_loo",
    }


def test_stored_sweep_documents_still_parse() -> None:
    sw = Sweep.model_validate(
        _doc({"/model/settings/pooling": ["partial", "full"], "/model/fit/draws": [500, 1000]})
    )
    assert len(sw.points()) == 4
    assert isinstance(parse_document(_doc({"/model/fit/draws": [500]})), Sweep)
    assert "Retired in v0.14" in (Sweep.__doc__ or "")


def test_sweep_grid_rules_unchanged() -> None:
    with pytest.raises(ValidationError, match="identity fields"):
        Sweep.model_validate(_doc({"/kind": ["ModelSpec"]}))
    with pytest.raises(ValidationError, match="at most 1000 points"):
        Sweep.model_validate(_doc({"/a": list(range(100)), "/b": list(range(11))}))


def test_expand_sweep_is_removed() -> None:
    assert not hasattr(mapi_spec, "expand_sweep")
    with pytest.raises(ImportError):
        __import__("mapi_spec.sweep")
