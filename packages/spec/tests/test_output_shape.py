"""v0.17 ``mapi_spec.output_shape``: parity of ``classify_table`` with ``shape.ts`` ``classify``
(golden cases computed by running the TypeScript), the ``meaning`` addition and the column
helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.api import ScoreSummaryTable
from mapi_spec.capabilities import OutputDecl
from mapi_spec.output_shape import (
    band_of,
    centre_of,
    classify_table,
    is_date_dim,
    quantile_of,
    series_dim,
    table_dims,
    table_stats,
    time_dim_of,
)

GOLDEN = Path(__file__).parent / "golden" / "output_shape.json"
CASES: list[dict[str, Any]] = json.loads(GOLDEN.read_text())["cases"]


def _table(**kw: Any) -> ScoreSummaryTable:
    return ScoreSummaryTable.model_validate(kw)


def test_golden_has_at_least_thirty_tables() -> None:
    assert len(CASES) >= 30
    assert {c["expected"]["shape"] for c in CASES} == {
        "grid", "areal", "classification", "time", "categorical", "scalar",
    }  # fmt: skip


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_parity_with_shape_ts(case: dict[str, Any]) -> None:
    t = ScoreSummaryTable.model_validate(case["table"])
    want = case["expected"]
    shape = classify_table(t, None, has_field=case["has_field"])
    assert shape == want["shape"]
    assert table_dims(t) == want["dims"]
    if shape == "time":
        assert time_dim_of(t) == want["time"]
        assert series_dim(t) == want["series"]


def test_a_declared_class_probability_is_a_classification() -> None:
    t = _table(variable="y_hat", columns=["store", "class", "mean"], rows=[["s", "a", 0.2]])
    assert classify_table(t, None, has_field=False) == "categorical"
    decl = OutputDecl(dims=["class"], meaning="class_probability", classes_dim="class")
    assert classify_table(t, decl, has_field=False) == "classification"
    # the grid and the area plots still win; other meanings change nothing
    assert classify_table(t, decl, has_field=True) == "grid"
    areal = _table(variable="y", columns=["a", "mean"], rows=[["x", 1]], plots=["sci.area_map"])
    assert classify_table(areal, decl, has_field=False) == "areal"
    state = OutputDecl(dims=["state"], meaning="state_probability", classes_dim="state")
    assert classify_table(t, state, has_field=False) == "categorical"
    assert classify_table(t, OutputDecl(dims=["class"]), has_field=False) == "categorical"


def test_column_helpers() -> None:
    t = _table(
        variable="y",
        columns=["geo", "week", "mean", "hdi_50_lower", "hdi_50_upper", "hdi_90_lower",
                 "hdi_90_upper"],
        rows=[["de", "2026-01-05", 1, 0.5, 1.5, 0, 2]],
    )  # fmt: skip
    assert table_dims(t) == ["geo", "week"]
    assert table_stats(t)[0] == "mean"
    assert centre_of(table_stats(t)) == "mean"
    assert band_of(table_stats(t)) == ("hdi_90_lower", "hdi_90_upper")
    assert band_of(["median", "q5", "q25", "q75", "q95"]) == ("q5", "q95")
    assert band_of(["median", "q50"]) is None
    assert centre_of(["point", "interval_lower", "interval_upper"]) == "point"
    assert quantile_of("q97_5") == 97.5 and quantile_of("q0") == 0.0 and quantile_of("mean") is None
    assert is_date_dim(t, "week") and not is_date_dim(t, "geo")


def test_circular_tables_have_no_time_dim() -> None:
    t = _table(variable="y", columns=["hour", "mean"], rows=[[1, 2]], period=24,
               dims=["hour"], time_dim="hour")  # fmt: skip
    assert time_dim_of(t) is None and series_dim(t) is None
