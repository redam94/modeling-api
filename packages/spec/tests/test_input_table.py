"""An endpoint's inline input as a long table (``EndpointSchema.input_table``): its columns
and example rows, a table's rows as XrJSON (``xrjson_from_rows``) and the forecast call over
a table with actuals and future rows (``table_forecast_call``)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from mapi_spec.endpoints import (
    EndpointSchemaSnapshot,
    EndpointScoreCall,
    InputTable,
    capability_breaks,
    example_actuals_call,
    input_table,
    table_forecast_call,
)
from mapi_spec.xrjson import table_cell, xrjson_from_rows

pytestmark = pytest.mark.unit


def snapshot(**over: Any) -> EndpointSchemaSnapshot:
    """The airline endpoint (monthly ``y``), with a ``price`` driver unless told otherwise."""
    doc: dict[str, Any] = {
        "input_schema": {
            "dims": {"t": {"kind": "time", "freq": "MS", "policy": "extend"}},
            "vars": {
                "y": {"dims": ["t"], "dtype": "int64", "role": "target"},
                "price": {"dims": ["t"], "dtype": "float64", "role": "covariate",
                          "future_required": True},
            },
        },
        "schema_hash": "sha256:" + "a" * 64,
        "score_modes": ["evaluate", "forecast", "predict"],
        "representation": "quantiles",
        "horizon_dim": "t",
        "future_required": ["price"],
        "fit_coords": {"t": {"kind": "time", "size": 144, "start": "1949-01-01",
                             "end": "1960-12-01", "freq": "MS"}},
        "conditions_on_actuals": True,
    }  # fmt: skip
    doc.update(over)
    return EndpointSchemaSnapshot.model_validate(doc)


def panel() -> EndpointSchemaSnapshot:
    return snapshot(
        input_schema={
            "dims": {
                "geo": {"kind": "categorical"},
                "t": {"kind": "time", "freq": "W-SUN", "policy": "extend"},
            },
            "vars": {
                "sales": {"dims": ["geo", "t"], "dtype": "float64", "role": "target"},
                "spend": {"dims": ["geo", "t"], "dtype": "float64", "role": "spend",
                          "future_required": True},
                "region": {"dims": ["geo"], "dtype": "str", "role": "covariate"},
                "row": {"dims": ["t"], "dtype": "int64", "role": "index"},
            },
        },
        fit_coords={
            "geo": {"kind": "categorical", "size": 3, "values": ["de", "fr", "it"]},
            "t": {"kind": "time", "size": 104, "start": "2024-01-07", "end": "2025-12-28",
                  "freq": "W-SUN"},
        },
        future_required=["spend"],
    )  # fmt: skip


# ------------------------------------------------------------------------------ input_table


def test_the_table_of_a_monthly_forecast() -> None:
    table = input_table(snapshot())
    assert table is not None
    assert table.header == ["t", "y", "price"]
    assert [c.role for c in table.columns] == ["coord", "target", "driver"]
    assert table.columns[0].freq == "MS" and table.horizon_dim == "t"
    assert table.conditions_on_actuals
    assert "actual" in table.columns[1].description
    assert table.variables == {"y": ["t"], "price": ["t"]}
    assert table.rows[0] == ["1961-01-01", None, None]
    assert [r[0] for r in table.rows] == [f"1961-{m:02d}-01" for m in range(1, 9)]
    assert len(input_table(snapshot(), periods=3).rows) == 3  # type: ignore[union-attr]


def test_a_model_that_cannot_condition_says_so_in_the_target_column() -> None:
    table = input_table(snapshot(conditions_on_actuals=False))
    assert table is not None and not table.conditions_on_actuals
    assert "fitted history" in table.columns[1].description
    assert table.rows  # the dates to forecast are still listed


def test_a_panel_lists_the_periods_for_the_first_levels() -> None:
    table = input_table(panel())
    assert table is not None
    # the forecast dim first, then the other dims; index variables are no column
    assert table.header == ["t", "geo", "sales", "spend", "region"]
    assert [c.role for c in table.columns] == ["coord", "coord", "target", "driver", "input"]
    assert table.rows[:3] == [
        ["2026-01-04", "de", None, None, None],
        ["2026-01-04", "fr", None, None, None],
        ["2026-01-11", "de", None, None, None],
    ]
    assert len(table.rows) == 16
    assert table.variables["region"] == ["geo"]


def test_tables_without_example_rows() -> None:
    predict_only = snapshot(score_modes=["predict"])
    table = input_table(predict_only)
    assert table is not None and table.rows == [] and table.header == ["t", "y", "price"]
    unknown_levels = panel().model_copy(update={"fit_coords": {"t": panel().fit_coords["t"]}})
    assert input_table(unknown_levels).rows == []  # type: ignore[union-attr]
    only_index = snapshot(
        input_schema={"dims": {"t": {"kind": "time", "freq": "MS"}},
                      "vars": {"row": {"dims": ["t"], "dtype": "int64", "role": "index"}}},
    )  # fmt: skip
    assert input_table(only_index) is None


def test_the_table_matches_the_actuals_example() -> None:
    snap = snapshot()
    table = input_table(snap)
    example = example_actuals_call(snap)
    assert table is not None and example is not None
    call = table_forecast_call(
        table, [dict(zip(table.header, r, strict=True)) for r in table.rows],
        output=example.output,
    )  # fmt: skip
    assert call.model_dump(mode="json") == example.model_dump(mode="json")


# ------------------------------------------------------------------------- xrjson_from_rows


def test_rows_become_an_inline_dataset() -> None:
    rows = [
        {"t": dt.datetime(1961, 1, 1), "geo": "de", "sales": 1.5, "region": "north"},
        {"t": dt.date(1961, 1, 1), "geo": "fr", "sales": 2, "region": "south"},
        {"t": "1961-02-01", "geo": "de", "sales": "", "region": "north"},
        {"t": "1961-02-01", "geo": "fr", "sales": float("nan"), "ignored": "x"},
    ]
    ds = xrjson_from_rows(rows, {"sales": ["t", "geo"], "region": ["geo"], "spend": ["t", "geo"]})
    dumped = ds.model_dump(mode="json", exclude_defaults=True)
    assert dumped == {
        "coords": {"t": {"dims": ["t"], "data": ["1961-01-01", "1961-02-01"]},
                   "geo": {"dims": ["geo"], "data": ["de", "fr"]}},
        "data_vars": {"sales": {"dims": ["t", "geo"], "data": [[1.5, 2], [None, None]]},
                      "region": {"dims": ["geo"], "data": ["north", "south"]}},
    }  # fmt: skip


def test_bad_tables_are_refused() -> None:
    with pytest.raises(ValueError, match="at least one of"):
        xrjson_from_rows([{"t": "1961-01-01"}], {"y": ["t"]})
    with pytest.raises(ValueError, match="column named 't'"):
        xrjson_from_rows([{"y": 1}], {"y": ["t"]})
    with pytest.raises(ValueError, match="row 2 has no value"):
        xrjson_from_rows([{"t": "1961-01-01", "y": 1}, {"t": "", "y": 2}], {"y": ["t"]})
    with pytest.raises(ValueError, match="two different values"):
        xrjson_from_rows([{"t": "1961-01-01", "y": 1}, {"t": "1961-01-01", "y": 2}], {"y": ["t"]})
    # the same value twice is fine (a variable over fewer dims repeats on every row)
    same = xrjson_from_rows(
        [{"t": "a", "y": 1}, {"t": "a", "y": 1}, {"t": "b", "y": None}], {"y": ["t"]}
    )
    assert same.data_vars["y"].data == [1, None]
    with pytest.raises(TypeError):
        table_cell(object())


def test_cells_from_pandas_and_numpy() -> None:
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    frame = pd.DataFrame(
        {"t": pd.date_range("1961-01-01", periods=3, freq="MS"), "y": [417, np.nan, 3]}
    )
    ds = xrjson_from_rows(frame.to_dict("records"), {"y": ["t"]})
    assert ds.coords["t"].data == ["1961-01-01", "1961-02-01", "1961-03-01"]
    assert ds.data_vars["y"].data == [417.0, None, 3.0]
    assert table_cell(np.datetime64("1961-01-01")) == "1961-01-01"
    assert table_cell(np.int64(4)) == 4 and table_cell(pd.NaT) is None
    assert table_cell(pd.Timestamp("1961-01-01 06:30")) == "1961-01-01T06:30:00"


# ---------------------------------------------------------------------- table_forecast_call


def _table() -> InputTable:
    table = input_table(snapshot())
    assert table is not None
    return table


@pytest.mark.parametrize(
    ("ys", "periods", "expected"),
    [
        ([417, 391, 419, None, None], None, 2),  # actuals then two dates: exactly those
        ([417, 391, 419], None, 8),  # ends with an actual: 8 appended
        ([417, 391, 419], 5, 5),
        ([None, None, None, None], None, 4),  # no actual: every listed date
    ],
)
def test_the_horizon_of_a_table_call(ys: list[Any], periods: int | None, expected: int) -> None:
    rows = [{"t": f"1961-{m + 1:02d}-01", "y": y} for m, y in enumerate(ys)]
    call = table_forecast_call(_table(), rows, periods=periods)
    assert call.mode == "forecast" and call.horizon is not None
    assert (call.horizon.dim, call.horizon.periods) == ("t", expected)
    assert call.future_covariates.value == "carry_last"
    assert "price" not in call.inline_inputs()["inline.data"].data_vars  # no column, no var
    EndpointScoreCall.model_validate(call.model_dump(mode="json"))


def test_losing_actuals_is_a_capability_break() -> None:
    capable, plain = snapshot(), snapshot(conditions_on_actuals=False)
    [why] = capability_breaks(capable, plain)
    assert "ACTUALS_NOT_SUPPORTED" in why
    assert capability_breaks(plain, capable) == []  # gaining it breaks nobody
    assert capability_breaks(capable, capable) == []
    no_forecast = plain.model_copy(update={"score_modes": ["predict"]})
    assert capability_breaks(capable, no_forecast) == []  # a lost mode is not this check's


def test_a_table_call_needs_a_forecasting_endpoint() -> None:
    no_horizon = _table().model_copy(update={"horizon_dim": None})
    with pytest.raises(ValueError, match="no dim a forecast extends"):
        table_forecast_call(no_horizon, [{"t": "1961-01-01", "y": 1}])
