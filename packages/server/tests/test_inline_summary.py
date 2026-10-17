"""The control plane's summary of an inline payload (``inline_summary``): coordinates, where
the targets and the drivers a forecast needs hold values (a JSON null check), and text sent
where the schema wants numbers - all before a run starts."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_server.services.endpoints import inline_summary
from mapi_spec.errors import ErrorCode, Issue, Severity
from mapi_spec.schema import DataSchema
from mapi_spec.xrjson import XrJsonDataset

pytestmark = pytest.mark.unit

SCHEMA = DataSchema.model_validate(
    {
        "dims": {"t": {"kind": "time", "policy": "extend", "freq": "MS"}},
        "vars": {
            "y": {"dims": ["t"], "dtype": "int64", "role": "target"},
            "price": {"dims": ["t"], "dtype": "float64", "future_required": True},
            "region": {"dims": ["t"], "dtype": "str"},
        },
    }
)


def _payload(t: list[Any], **data_vars: list[Any]) -> XrJsonDataset:
    return XrJsonDataset.model_validate(
        {
            "coords": {"t": {"dims": ["t"], "data": t}},
            "data_vars": {k: {"dims": ["t"], "data": v} for k, v in data_vars.items()},
        }
    )


def _summarise(payload: XrJsonDataset) -> tuple[Any, list[Issue]]:
    issues: list[Issue] = []
    return inline_summary(payload, SCHEMA, "/data", issues), issues


def test_drivers_a_forecast_needs_are_summarised_where_they_hold_values() -> None:
    summary, issues = _summarise(
        _payload(["1961-01-01", "1961-02-01"], y=[450, None], price=[None, 2.5])
    )
    assert issues == []
    assert summary.observed["y"]["t"].end == "1961-01-01T00:00:00"
    assert summary.observed["price"]["t"].start == "1961-02-01T00:00:00"
    assert "region" not in summary.observed  # not needed on future periods


@pytest.mark.parametrize("cell", ["450", " ", "#N/A"])
def test_text_where_the_schema_wants_numbers_is_refused(cell: str) -> None:
    _, issues = _summarise(_payload(["1961-01-01", "1961-02-01"], y=[cell, None], price=[1, 1]))
    [err] = [i for i in issues if i.severity is Severity.ERROR]
    assert err.code is ErrorCode.SCHEMA_INVALID and err.pointer == "/data/data_vars/y"
    assert "'y'" in err.message and "1961-01-01" in err.message
    # a text variable may hold text
    _, ok = _summarise(_payload(["1961-01-01"], y=[1], price=[1.0], region=["north"]))
    assert ok == []


def test_dates_with_a_utc_offset_are_off_the_grid_and_nothing_else() -> None:
    summary, issues = _summarise(
        _payload(["1961-01-01T00:00:00-05:00", "1961-02-01T00:00:00-05:00"], y=[None, None])
    )
    assert [i.code for i in issues] == [ErrorCode.NON_CONTIGUOUS_EXTENSION]
    # no stand-in "index" summary: it would read as periods 0..1, before the fitted range
    assert "t" not in summary.coords
