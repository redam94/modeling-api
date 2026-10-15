"""``core.formula``: grammar, allow-list, evaluation and the step's schema behaviour."""

from __future__ import annotations

from typing import Any

import mapi_steps as ms
import numpy as np
import pytest
import xarray as xr
from mapi_sdk.schema import infer_schema
from mapi_sdk.steps import StepContext
from mapi_spec.errors import ErrorCode, MapiError
from mapi_steps.formula import FormulaError, compile_formula, evaluate

pytestmark = pytest.mark.unit


def ev(expr: str, ds: xr.Dataset) -> Any:
    return evaluate(compile_formula(expr), ds)


def test_arithmetic_precedence_and_functions(weekly: xr.Dataset) -> None:
    y, p = weekly["y"], weekly["price"]
    xr.testing.assert_allclose(ev("1 + 2 * y ** 2 / 4 - -y", weekly), 1 + 2 * y**2 / 4 + y)
    xr.testing.assert_allclose(ev("log1p(y) + exp(0) * abs(-y)", weekly), np.log1p(y) + abs(y))
    xr.testing.assert_allclose(ev("fillna(price, 0)", weekly), p.fillna(0))
    xr.testing.assert_allclose(ev("clip(y, 300, 400)", weekly), y.clip(300, 400))
    xr.testing.assert_allclose(ev("minimum(y, 350)", weekly), np.minimum(y, 350))
    xr.testing.assert_allclose(
        ev("where(y > 350 & ~isnull(price), 1, 0)", weekly), xr.where((y > 350) & p.notnull(), 1, 0)
    )
    xr.testing.assert_allclose(
        ev("mean(spend, 'channel', 'geo')", weekly), weekly["spend"].mean(["channel", "geo"])
    )
    xr.testing.assert_allclose(ev("expit(logit(share))", weekly), weekly["share"])
    assert ev("2 ** 3 + 1", weekly) == 9.0
    assert bool((ev("(y >= 0) | (y != y)", weekly)).all())


def test_var_function_reaches_odd_names(weekly: xr.Dataset) -> None:
    ds = weekly.rename({"price": "unit-price"})
    xr.testing.assert_allclose(ev("var('unit-price') * 2", ds), ds["unit-price"] * 2)


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("__import__('os')", "unknown function"),
        ("y.__class__", "does not parse"),
        ("y[0]", "does not parse"),
        ("lambda: 1", "does not parse"),
        ("open('x')", "unknown function"),
        ("log(y, 2)", "takes 1"),
        ("where(y, 1)", "takes 3"),
        ("sum(y, channel)", "strings"),
        ("log('y')", "string"),
        ("'just text'", "bare string"),
        ("var(y)", "string"),
        ("1 +", "does not parse"),
        ("y" + " + y" * 600, "too large"),
        ("x" * 5000, "longer than"),
    ],
)
def test_rejections(expr: str, message: str) -> None:
    with pytest.raises(FormulaError, match=message):
        compile_formula(expr)


def test_settings_validation_rejects_bad_formulas() -> None:
    with pytest.raises(MapiError) as err:
        ms.FormulaStep().parse_settings({"out": "z", "expr": "eval('1')"})
    assert err.value.code is ErrorCode.SETTINGS_INVALID


def test_step_dims_broadcast_and_errors(weekly: xr.Dataset) -> None:
    inst = ms.FormulaStep()
    ctx = StepContext(step_id="t", schema=infer_schema(weekly, targets=["y"]))

    def go(settings: dict[str, Any], ds: xr.Dataset = weekly) -> xr.Dataset:
        s = inst.parse_settings(settings)
        return inst.transform(ds, s, inst.fit(ds, s, ctx), ctx)

    out = go(
        {"out": "ratio", "expr": "y / sum(spend, 'channel')", "dims": ["time", "geo"], "units": "1"}
    )
    assert out["ratio"].dims == ("time", "geo") and out["ratio"].attrs == {"units": "1"}
    out = go({"out": "k", "expr": "2", "dims": ["geo"]})
    assert out["k"].dims == ("geo",) and float(out["k"].sum()) == 6.0
    with pytest.raises(MapiError, match="declared"):
        go({"out": "bad", "expr": "spend", "dims": ["geo", "time"]})
    with pytest.raises(MapiError, match="unknown variables"):
        go({"out": "bad", "expr": "ghost + 1", "dims": []})
    with pytest.raises(MapiError, match="already exists"):
        go({"out": "y", "expr": "y + 1", "dims": ["geo", "time"]})
    with pytest.raises(MapiError, match="operand"):
        go({"out": "bad", "expr": "sum(y, 'channel')", "dims": []})
    # derived from the target: skipped when the target is absent at score time
    no_target = weekly.drop_vars("y")
    out = go({"out": "ly", "expr": "log(y)", "dims": ["geo", "time"]}, no_target)
    assert "ly" not in out
