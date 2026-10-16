"""M3.5 workflow condition language: grammar, strict evaluation, references for static
checks, and hostile inputs that must be refused with a typed error (never executed)."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.expr import (
    LIMITS,
    BoolOp,
    Compare,
    Ref,
    check_condition,
    evaluate,
    evaluate_condition,
    parse_expression,
)
from pydantic import BaseModel

pytestmark = pytest.mark.unit

ENV: dict[str, Any] = {
    "nodes": {
        "adf": {"p_value": 0.57, "reject": False, "decision": "unit_root", "statistic": -1.4,
                "critical_values": {"5%": -2.87}},
        "kpss": {"p_value": 0.01, "reject": True},
        "order": {"best_p": 2, "best_q": 1, "table": [[0, 0, 512.3], [2, 1, 498.1]]},
        "seas": {"seasonal": True, "strength": 0.81},
        "wanders": {"value": True, "arm": "then"},
    },
    "params": {"alpha": 0.05, "season": 52, "methods": ["naive", "seasonal_naive"]},
    "inputs": {"data": "dataset://growth/sales@v4"},
    "run": {"attempt": 1, "trigger": "schedule"},
}  # fmt: skip


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("nodes.adf.p_value < params.alpha", False),
        ("nodes.adf.p_value >= params.alpha or nodes.kpss.reject", True),
        ("not nodes.adf.reject and nodes.kpss.reject", True),
        ("not not true", True),
        ("(1 + 2) * 3 == 9", True),
        ("1 + 2 * 3 == 7", True),
        ("-nodes.adf.statistic > 1", True),
        ("10 % 3 == 1 and 7 / 2 == 3.5", True),
        ("nodes.adf.decision == 'unit_root'", True),
        ('nodes.adf.decision != "stationary"', True),
        ("'naive' in params.methods", True),
        ("'mean' not in params.methods", True),
        ("'root' in nodes.adf.decision", True),
        ("nodes.adf.critical_values['5%'] < -2", True),
        ("nodes.order.table[1][2] < nodes.order.table[0][2]", True),
        ("len(params.methods) == 2", True),
        ("abs(nodes.adf.statistic) > 1.0", True),
        ("min(1, 2, 3) == 1 and max([4, 9]) == 9", True),
        ("round(nodes.seas.strength, 1) == 0.8", True),
        ("any([false, nodes.seas.seasonal]) and not all([true, false])", True),
        ("exists(nodes.adf.p_value) and not exists(nodes.missing.p_value)", True),
        ("coalesce(nodes.missing.p_value, params.alpha) == 0.05", True),
        ("if(nodes.wanders.value, 1, 0) == 1", True),
        ("if(false, nodes.missing.x, 2) == 2", True),  # lazy: the missing ref never runs
        ("1 == 1.0 and true != 1", True),
        ("null == null and [1, 'a'] == [1, 'a']", True),
        ("'a' < 'b' and 2 <= 2", True),
        ("run.trigger == 'schedule' and inputs.data != ''", True),
        ("false and nodes.missing.x", False),  # short-circuit
        ("true or nodes.missing.x", True),
        ("1e-3 < 0.01 and 2.5E2 == 250", True),
    ],
)
def test_conditions_evaluate(text: str, expected: bool) -> None:
    assert evaluate_condition(text, ENV) is expected


def test_values_and_the_lazy_if() -> None:
    assert evaluate("[nodes.order.best_p, if(nodes.wanders.value, 1, 0), 1]", ENV) == [2, 1, 1]
    assert evaluate("'a' + 'b'", ENV) == "ab"
    assert evaluate("params.season * 2 - 4", ENV) == 100


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("nodes.adf.p_value < 'x'", "cannot compare"),
        ("nodes.kpss.reject and 1", "needs true or false"),
        ("nodes.adf.__class__ == 1", "not available"),
        ("not 1", "needs true or false"),
        ("nodes.missing.x > 1", "not available"),
        ("params.nope == 1", "not available"),
        ("nodes.adf.p_value / 0 > 1", "division by zero"),
        ("1 % 0 == 1", "division by zero"),
        ("'a' - 'b' == 1", "cannot use"),
        ("-'a' == 1", "cannot negate"),
        ("abs('x') == 1", "abs() needs a number"),
        ("len(3) == 1", "len() needs"),
        ("min([]) == 1", "needs at least one value"),
        ("min(1, 'a') == 1", "of one kind"),
        ("1 in 3", "'in' needs a list"),
        ("any([1])", "list of true/false"),
        ("round(1.5, 99) == 2", "between 0 and 15"),
        ("if(1, 2, 3) == 2", "if() needs true or false"),
        ("nodes.adf.p_value + 1", "not true or false"),
        ("99999999999999999 * 99999999999999999 > 1", "too large"),
        ("nodes.adf[0] == 1", "not available"),
    ],
)
def test_evaluation_errors_are_typed_and_plain(text: str, message: str) -> None:
    with pytest.raises(MapiError) as exc:
        evaluate_condition(text, ENV)
    assert exc.value.code is ErrorCode.EXPRESSION_ERROR
    assert message in exc.value.detail


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("a < 1", "unknown name 'a'"),
        ("nodes.adf.p_value <", "expected"),
        ("1 < 2 < 3", "cannot be chained"),
        ("(1 < 2", "expected ')'"),
        ("nodes.adf.p_value ** 2 > 1", "unexpected"),
        ("eval('1')", "unknown function 'eval'"),
        ("__import__('os')", "unknown function"),
        ("open('/etc/passwd')", "unknown function"),
        ("nodes['adf'].p_value > 1 or x", "unknown name"),
        ("lambda: 1", "unexpected character"),
        ("1 if true else 0", "unexpected"),
        ("nodes.adf.p_value; 1", "unexpected character"),
        ("'unterminated", "unterminated"),
        ("'bad \\q escape'", "unknown escape"),
        ("'two\nlines'", "cannot span"),
        ("1abc == 1", "cannot be followed"),
        ("nodes[1.5] == 1", "whole number or a string"),
        ("exists(1)", "takes a reference"),
        ("abs(1, 2) == 1", "abs() takes"),
        ("if(true, 1) == 1", "if() takes"),
        ("x" * 3000, "longer than"),
        ("1+" * 250 + "1 == 1", "tokens"),
        ("(" * 50 + "1" + ")" * 50 + " == 1", "nested deeper"),
        ("not " * 40 + "true", "nested deeper"),
        ("[" + ", ".join(["1"] * 150) + "] == 1", "items"),
        ("'" + "a" * 600 + "' == 'a'", "longer than"),
        ("1" * 40 + " == 1", "too long"),
        ("nodes" + ".a" * 20 + " == 1", "at most 16 parts"),
        ("1 == 1 @", "unexpected character"),
        ("and", "unexpected keyword"),
    ],
)
def test_parse_errors_are_typed(text: str, message: str) -> None:
    with pytest.raises(MapiError) as exc:
        parse_expression(text)
    assert exc.value.code is ErrorCode.EXPRESSION_INVALID
    assert message in exc.value.detail, exc.value.detail


def test_dunder_names_never_reach_python_objects() -> None:
    # attribute-looking paths only index dicts: a Python object in the environment is opaque
    class Secret:
        token = "s3cr3t"

    env = {"nodes": {"x": Secret(), "y": {"__class__": 1}}}
    with pytest.raises(MapiError):
        evaluate_condition("nodes.x.token == 's3cr3t'", env)
    with pytest.raises(MapiError):
        evaluate_condition("nodes.x == 1", env)
    assert evaluate_condition("nodes.y.__class__ == 1", env)  # just a dict key
    assert not evaluate_condition("exists(nodes.x.__dict__)", env)


def test_static_analysis_lists_references() -> None:
    ex = parse_expression("nodes.adf.p_value < params.alpha and coalesce(nodes.kpss.reject, false)")
    assert [str(r) for r in ex.references()] == [
        "nodes.adf.p_value", "params.alpha", "nodes.kpss.reject",
    ]  # fmt: skip
    assert ex.node_refs() == [("adf", ("p_value",)), ("kpss", ("reject",))]
    assert isinstance(ex.root, BoolOp) and isinstance(ex.root.operands[0], Compare)
    assert Ref("nodes", ("t", 0, "x")).__str__() == "nodes.t[0].x"


def test_condition_type_validates_by_parsing() -> None:
    from mapi_spec.expr import Condition

    class M(BaseModel):
        c: Condition

    assert M(c="nodes.adf.reject").c == "nodes.adf.reject"
    with pytest.raises(ValueError, match="unknown name"):
        M(c="os.system('x')")
    assert check_condition("true") == "true"


def test_limits_are_published() -> None:
    assert LIMITS.max_chars == 2000 and LIMITS.max_depth == 32 and LIMITS.max_tokens == 400


def test_evaluation_step_budget() -> None:
    from mapi_spec.expr import ExpressionLimits

    big = "[" + ", ".join(["1"] * 90) + "]"
    ex = parse_expression(f"len({big}) == 90", limits=ExpressionLimits(max_steps=50))
    with pytest.raises(MapiError) as exc:
        ex.evaluate_bool({})
    assert "too many steps" in exc.value.detail


_ALPHABET = st.sampled_from(
    [
        *"abcdefghijklmnopqrstuvwxyz_.,()[]'\"0123456789 <>=!+-*/%",
        "nodes",
        "params",
        "and",
        "or",
        "not",
        "in",
        "true",
        "false",
        "null",
        "if",
        "len",
        "__class__",
        "eval",
        "\\",
        "\n",
        "{",
        "}",
        "@",
        "$",
        "`",
    ]
)


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_ALPHABET, max_size=40).map("".join))
def test_fuzzed_text_only_ever_raises_typed_errors(text: str) -> None:
    """Whatever the text, parsing and evaluating either succeed or raise MapiError with an
    expression code: never another exception, never a hang."""
    try:
        ex = parse_expression(text)
    except MapiError as exc:
        assert exc.code is ErrorCode.EXPRESSION_INVALID
        return
    try:
        ex.evaluate(ENV)
    except MapiError as exc:
        assert exc.code is ErrorCode.EXPRESSION_ERROR


@settings(max_examples=200, deadline=None)
@given(
    st.recursive(
        st.one_of(
            st.integers(-1000, 1000),
            st.booleans(),
            st.sampled_from(["'a'", "params.alpha", "nodes.adf.p_value"]),
        ),
        lambda inner: st.one_of(
            st.tuples(inner, st.sampled_from(["+", "-", "*", "==", "<", "and", "or"]), inner).map(
                lambda t: f"({t[0]} {t[1]} {t[2]})"
            ),
            inner.map(lambda x: f"not ({x})"),
            st.lists(inner, max_size=3).map(lambda xs: "[" + ", ".join(map(str, xs)) + "]"),
        ),
        max_leaves=12,
    ).map(lambda x: str(x).replace("True", "true").replace("False", "false"))
)
def test_generated_expressions_are_well_behaved(text: str) -> None:
    try:
        parse_expression(text).evaluate(ENV)
    except MapiError as exc:
        assert exc.code in (ErrorCode.EXPRESSION_ERROR, ErrorCode.EXPRESSION_INVALID)
