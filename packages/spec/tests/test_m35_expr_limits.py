"""The condition language never raises anything but ``MapiError`` (M3.5 review), and run
parameters cannot carry numbers too large for it."""

from __future__ import annotations

import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.expr import parse_expression
from mapi_spec.workflow import WorkflowSpec, check_run_params

HUGE = int("9" * 400)


@pytest.mark.parametrize(
    "text",
    [
        "params.x * 2.0",
        "params.x + 1.5",
        "params.x - 0.5",
        "params.x / 3.0",
        "params.x * 0.0 + 0.05",
        "params.x % 7.5",
        "params.x + params.x + 0.5 > 1.0",
        "round(params.x * 1.0, 2)",
    ],
)
def test_huge_numbers_give_an_expression_error(text: str) -> None:
    with pytest.raises(MapiError) as exc:
        parse_expression(text).evaluate({"params": {"x": HUGE}})
    assert exc.value.code is ErrorCode.EXPRESSION_ERROR


def test_huge_numbers_still_compare_exactly() -> None:
    env = {"params": {"x": HUGE, "y": 1.5}}
    assert parse_expression("params.x == params.x").evaluate(env) is True
    assert parse_expression("params.x > params.y").evaluate(env) is True
    assert parse_expression("params.x == 1.0").evaluate(env) is False


def _nested(depth: int) -> object:
    v: object = 1
    for _ in range(depth):
        v = [v]
    return v


@pytest.mark.parametrize("op", ["==", "!=", "in"])
def test_deeply_nested_data_gives_an_expression_error(op: str) -> None:
    deep = _nested(5000)
    env = {"nodes": {"a": {"v": deep, "w": [deep]}}}
    right = "nodes.a.w" if op == "in" else "nodes.a.v"
    with pytest.raises(MapiError) as exc:
        parse_expression(f"nodes.a.v {op} {right}").evaluate(env)
    assert exc.value.code is ErrorCode.EXPRESSION_ERROR


def test_moderately_nested_equality_still_works() -> None:
    env = {"nodes": {"a": {"v": _nested(20), "w": _nested(20)}}}
    assert parse_expression("nodes.a.v == nodes.a.w").evaluate(env) is True
    env["nodes"]["a"]["w"] = _nested(21)  # type: ignore[index]
    assert parse_expression("nodes.a.v == nodes.a.w").evaluate(env) is False


def _spec(params: dict[str, object]) -> WorkflowSpec:
    return WorkflowSpec.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "Workflow",
            "metadata": {"name": "p", "project": "growth"},
            "params": params,
            "inputs": {"data": {"type": "dataset"}},
            "nodes": [{"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}}],
        }
    )


@pytest.mark.parametrize("value", [HUGE, -HUGE, 1e300, 10**16])
def test_run_parameters_refuse_numbers_too_large(value: float) -> None:
    spec = _spec({"x": {"type": "number", "default": 1}, "n": {"type": "integer", "default": 1}})
    for name in ("x", "n"):
        if name == "n" and not isinstance(value, int):
            continue
        with pytest.raises(MapiError) as exc:
            check_run_params(spec, {name: value})
        assert exc.value.code is ErrorCode.WORKFLOW_INVALID
        assert "too large" in exc.value.issues[0].message


def test_run_parameters_accept_ordinary_numbers() -> None:
    spec = _spec({"x": {"type": "number", "default": 1}, "seed": {"type": "integer"}})
    assert check_run_params(spec, {"x": 0.05, "seed": 2**32}) == {"x": 0.05, "seed": 2**32}


def test_a_default_too_large_is_refused() -> None:
    with pytest.raises(ValueError, match="too large"):
        _spec({"x": {"type": "number", "default": HUGE}})
