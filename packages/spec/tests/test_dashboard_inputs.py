"""v0.17 ``mapi_spec.dashboard_inputs``: every refusal of ``check_rows`` (with its pointer),
the resolved values, the row labels per dim policy and the inline payload."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_spec.dashboard_inputs import (
    HORIZON_KEY,
    RowContext,
    check_rows,
    check_value,
    resolve_row,
    row_coordinates,
    row_policy_issues,
    rows_payload,
)
from mapi_spec.dashboard_v2 import ScenarioInputDef, ScenarioRowsDef
from mapi_spec.errors import ErrorCode
from mapi_spec.schema import DimPolicy


def _defs(**kw: Any) -> ScenarioRowsDef:
    base: dict[str, Any] = {
        "enabled": True,
        "max_rows": 3,
        "outputs": ["y_hat"],
        "inputs": [
            {"name": "price", "label": "Price", "bind": {"var": "price"}, "type": "number",
             "min": 10, "max": 30, "step": 0.5, "default": 19.5},
            {"name": "units", "label": "Units", "bind": {"var": "units"}, "type": "integer",
             "min": 0, "max": 10, "default": 2},
            {"name": "plan", "label": "Plan", "bind": {"var": "plan"}, "type": "choice",
             "choices": ["Basic", "Plus Max"], "default": "Basic"},
            {"name": "start", "label": "Start", "bind": {"var": "start"}, "type": "date",
             "date_min": "2026-01-01", "date_max": "2026-12-31", "default": "2026-06-01"},
            {"name": "promo", "label": "Promotion", "bind": {"var": "promo"}, "type": "toggle",
             "default": False},
        ],
        "fixed": {"region": "north"},
    }  # fmt: skip
    base.update(kw)
    return ScenarioRowsDef.model_validate(base)


def _ptrs(rows: list[dict[str, Any]], defs: ScenarioRowsDef | None = None) -> list[str]:
    issues = check_rows(rows, defs or _defs())
    assert all(i.code is ErrorCode.DASHBOARD_INPUT_INVALID for i in issues)
    return [i.pointer for i in issues]


def test_valid_rows_pass() -> None:
    rows: list[dict[str, Any]] = [
        {"price": 10, "units": 10, "plan": "plus max", "start": "2026-01-01", "promo": True},
        {"price": 29.5},
        {"plan": None},
    ]
    assert check_rows(rows, _defs()) == []


@pytest.mark.parametrize(
    ("row", "pointer"),
    [
        ({"price": 9.5}, "/rows/0/price"),  # below min
        ({"price": 30.5}, "/rows/0/price"),  # above max
        ({"price": 19.25}, "/rows/0/price"),  # off the step grid
        ({"price": float("nan")}, "/rows/0/price"),
        ({"price": float("inf")}, "/rows/0/price"),
        ({"price": "19.5"}, "/rows/0/price"),  # wrong type
        ({"price": True}, "/rows/0/price"),  # a bool is not a number
        ({"units": 2.5}, "/rows/0/units"),  # an integer with a fraction
        ({"plan": "gold"}, "/rows/0/plan"),  # not a choice
        ({"plan": "<b>Basic</b>"}, "/rows/0/plan"),  # not plain text
        ({"plan": "b" * 129}, "/rows/0/plan"),  # too long
        ({"plan": False}, "/rows/0/plan"),
        ({"start": "2025-12-31"}, "/rows/0/start"),  # before date_min
        ({"start": "2026-13-01"}, "/rows/0/start"),  # not a date
        ({"start": "20260601"}, "/rows/0/start"),  # not YYYY-MM-DD
        ({"promo": 1}, "/rows/0/promo"),  # a toggle takes true or false
        ({"discount": 1}, "/rows/0/discount"),  # unknown key
        ({"region": "south"}, "/rows/0/region"),  # fixed values are not inputs
    ],
)
def test_each_refusal_has_a_pointer(row: dict[str, Any], pointer: str) -> None:
    assert _ptrs([row]) == [pointer]


def test_row_count_and_state_refusals() -> None:
    assert _ptrs([{}] * 4) == ["/rows"]
    assert _ptrs([]) == ["/rows"]
    assert _ptrs([{}], _defs(enabled=False)) == ["/rows"]
    many = [{"price": 1, "units": -1}] * 3
    assert _ptrs(many) == [f"/rows/{i}/{n}" for i in range(3) for n in ("price", "units")]


def test_step_tolerance_and_default_step_of_integers() -> None:
    price = _defs().inputs[0]
    assert check_value(10 + 0.5 * 7 + 1e-12, price) is None
    assert check_value(10.3, price) is not None
    units = _defs().inputs[1]
    assert check_value(3, units) is None and check_value(3.0, units) is None


def test_horizon_key() -> None:
    defs = _defs(horizon={"dim": "date", "min": 1, "max": 12, "default": 6})
    assert check_rows([{HORIZON_KEY: 12}], defs) == []
    assert [i.pointer for i in check_rows([{HORIZON_KEY: 13}], defs)] == ["/rows/0/horizon"]
    assert [i.pointer for i in check_rows([{HORIZON_KEY: 2.5}], defs)] == ["/rows/0/horizon"]
    assert resolve_row({}, defs)[HORIZON_KEY] == 6
    assert resolve_row({HORIZON_KEY: 3}, defs)[HORIZON_KEY] == 3
    assert [i.pointer for i in check_rows([{HORIZON_KEY: 3}], _defs())] == ["/rows/0/horizon"]


def test_resolve_row_merges_defaults_and_fixed() -> None:
    values = resolve_row({"price": 12, "plan": " plus  max "}, _defs())
    assert values == {
        "price": 12.0, "units": 2, "plan": "Plus Max", "start": "2026-06-01", "promo": False,
        "region": "north",
    }  # fmt: skip
    assert isinstance(values["units"], int) and isinstance(values["price"], float)
    with pytest.raises(ValueError, match="between"):
        resolve_row({"price": 100}, _defs())


# ================================================================================ row labels

CTX_NEW = RowContext(
    dims=["store", "sku", "week"],
    row_dim="store",
    policies={"store": DimPolicy.NEW_LEVELS, "sku": DimPolicy.FIXED, "week": DimPolicy.EXTEND},
    levels={"store": ["s1", "s2"], "sku": ["k1"], "week": ["2026-10-04", "2026-10-11"]},
)


def test_new_levels_get_case_labels() -> None:
    defs = _defs(fixed={"week": "2026-10-11"})
    coords = row_coordinates([{}, {"price": 11}], defs, CTX_NEW)
    assert coords == [
        {"store": "case1", "sku": "k1", "week": "2026-10-11"},
        {"store": "case2", "sku": "k1", "week": "2026-10-11"},
    ]


def test_extend_takes_the_next_labels_or_integers() -> None:
    ctx = RowContext(dims=["t"], row_dim="t", policies={"t": DimPolicy.EXTEND},
                     levels={"t": [0, 1, 2]})  # fmt: skip
    assert [c["t"] for c in row_coordinates([{}, {}, {}], _defs(), ctx)] == [3, 4, 5]
    dated = ctx.model_copy(update={"next_labels": ["2026-11-01", "2026-12-01"], "levels": {}})
    assert [c["t"] for c in row_coordinates([{}, {}], _defs(), dated)] == [
        "2026-11-01", "2026-12-01",
    ]  # fmt: skip
    with pytest.raises(ValueError, match="at most 2"):
        row_coordinates([{}, {}, {}], _defs(), dated)


def test_fixed_dims_take_existing_levels_only() -> None:
    sku = {"name": "sku", "label": "Product", "bind": {"dim": "sku"}, "type": "choice",
           "choices": ["k1", "k2"], "default": "k1"}  # fmt: skip
    defs = _defs(inputs=[*_defs().inputs, ScenarioInputDef.model_validate(sku)],
                 fixed={"week": "2026-10-11"})  # fmt: skip
    ctx = CTX_NEW.model_copy(update={"levels": {**CTX_NEW.levels, "sku": ["k1", "K2"]}})
    coords = row_coordinates([{"sku": "k2"}], defs, ctx)
    assert coords[0]["sku"] == "K2"  # the fitted spelling
    small = CTX_NEW.model_copy(update={"levels": {**CTX_NEW.levels, "sku": ["k1", "k3"]}})
    with pytest.raises(ValueError, match="fixed"):
        row_coordinates([{"sku": "k2"}], defs, small)
    issues = row_policy_issues(defs, small)
    assert [i.pointer for i in issues] == ["/scenario_rows/inputs/5/choices"]


def test_dims_without_a_value_are_refused() -> None:
    ctx = RowContext(dims=["store", "sku"], row_dim="store",
                     policies={"store": DimPolicy.FIXED, "sku": DimPolicy.FIXED},
                     levels={"store": ["s1", "s2"], "sku": ["k1", "k2"]})  # fmt: skip
    with pytest.raises(ValueError, match="nothing gives"):
        row_coordinates([{}], _defs(), ctx)
    msgs = [i.message for i in row_policy_issues(_defs(), ctx)]
    assert len(msgs) == 2 and "fixed" in msgs[0]
    assert row_policy_issues(_defs(enabled=False), ctx) == []
    assert row_policy_issues(_defs(fixed={"week": "2026-10-11"}), CTX_NEW) == []


def test_duplicate_cells_are_refused() -> None:
    ctx = RowContext(dims=["store"], row_dim="store", policies={"store": DimPolicy.FIXED},
                     levels={"store": ["s1"]})  # fmt: skip
    with pytest.raises(ValueError, match="same case"):
        row_coordinates([{}, {}], _defs(), ctx)


def test_rows_payload() -> None:
    defs = _defs(fixed={"week": "2026-10-11", "region": "north"})
    ctx = CTX_NEW.model_copy(update={"var_dims": {"region": ["store"]}})
    ds = rows_payload([{"price": 11, "promo": True}, {}], defs, ctx)
    assert ds.coords["store"].data == ["case1", "case2"]
    assert ds.coords["week"].data == ["2026-10-11"]
    assert ds.data_vars["price"].dims == ["store", "sku", "week"]
    assert ds.data_vars["price"].data == [[[11.0]], [[19.5]]]
    assert ds.data_vars["promo"].data == [[[True]], [[False]]]
    assert ds.data_vars["plan"].data == [[["Basic"]], [["Basic"]]]
    assert ds.data_vars["region"].dims == ["store"]
    assert ds.data_vars["region"].data == ["north", "north"]
    assert "week" not in ds.data_vars  # a fixed dim value is a coordinate


def test_rows_payload_puts_null_in_cells_no_row_gives() -> None:
    ctx = RowContext(dims=["store", "week"], row_dim="week",
                     policies={"store": DimPolicy.FIXED, "week": DimPolicy.EXTEND},
                     levels={"store": ["s1", "s2"], "week": [1, 2]})  # fmt: skip
    store = ScenarioInputDef.model_validate(
        {"name": "store", "label": "Store", "bind": {"dim": "store"}, "type": "choice",
         "choices": ["s1", "s2"], "default": "s1"}
    )  # fmt: skip
    defs = ScenarioRowsDef(enabled=True, outputs=["y_hat"], inputs=[store, _defs().inputs[0]])
    ds = rows_payload([{"store": "s1"}, {"store": "s2", "price": 20}], defs, ctx)
    assert ds.coords["store"].data == ["s1", "s2"] and ds.coords["week"].data == [3, 4]
    assert ds.data_vars["price"].data == [[19.5, None], [None, 20.0]]


def test_row_context_validation() -> None:
    with pytest.raises(ValueError, match="row_dim"):
        RowContext(dims=["a"], row_dim="b")
    with pytest.raises(ValueError, match="unique"):
        RowContext(dims=["a", "a"])
    with pytest.raises(ValueError, match="among"):
        RowContext(dims=["a"], var_dims={"x": ["b"]})
