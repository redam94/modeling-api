from __future__ import annotations

from typing import Any

import pytest
from mapi_spec import ModelSpec, Sweep, expand_sweep
from mapi_spec.errors import ValidationFailed
from mapi_spec.examples import E33_YAML
from mapi_spec.loader import load_model_spec
from mapi_spec.sweep import set_pointer

pytestmark = pytest.mark.unit


def _sweep(grid: dict[str, list[Any]]) -> Sweep:
    return Sweep.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "Sweep",
            "metadata": {"name": "sens", "project": "growth"},
            "base": "spec://growth/eu-geo-mmm@r2",
            "grid": grid,
            "compare_by": "elpd_loo",
        }
    )


@pytest.fixture
def base() -> ModelSpec:
    return load_model_spec(E33_YAML)


def test_expand_sweep(base: ModelSpec) -> None:
    sw = _sweep({"/model/settings/pooling": ["partial", "full"], "/model/fit/draws": [500, 1000]})
    children = expand_sweep(sw, base)
    assert [c.metadata.name for c in children] == [f"eu-geo-mmm-{i}" for i in range(4)]
    assert {(c.model.settings["pooling"], c.model.fit.draws) for c in children} == {
        ("partial", 500),
        ("partial", 1000),
        ("full", 500),
        ("full", 1000),
    }
    # everything else is inherited from the base revision
    assert all(c.pipeline == base.pipeline for c in children)


def test_expand_sweep_errors_point_into_grid(base: ModelSpec) -> None:
    with pytest.raises(ValidationFailed) as exc:
        expand_sweep(_sweep({"/model/fit/draws": [0, 10]}), base)
    assert exc.value.issues[0].pointer == "/grid/~1model~1fit~1draws"
    assert exc.value.issues[0].context == {"point": 0}
    with pytest.raises(ValidationFailed, match="cannot apply"):
        expand_sweep(_sweep({"/pipeline/99/with/l_max": [1]}), base)


def test_set_pointer() -> None:
    assert set_pointer({"a": [1, 2]}, "/a/-", 3) == {"a": [1, 2, 3]}
    assert set_pointer({"a": [1, 2]}, "/a/0", 9) == {"a": [9, 2]}
    assert set_pointer({"a": [{"b": 1}]}, "/a/0/b", 2) == {"a": [{"b": 2}]}
    assert set_pointer({}, "/x/y", 1) == {"x": {"y": 1}}
    assert set_pointer({"a": 1}, "", 5) == 5
    original = {"a": {"b": 1}}
    set_pointer(original, "/a/b", 2)
    assert original == {"a": {"b": 1}}  # never mutates the input
    with pytest.raises(TypeError):
        set_pointer({"a": 1}, "/a/b", 2)
    with pytest.raises(IndexError):
        set_pointer({"a": []}, "/a/3", 2)
