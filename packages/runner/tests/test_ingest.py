"""Tabular ingest: column mappings, the MFF preset, profiles and previews (in process)."""

from __future__ import annotations

import io
import json
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
from mapi_runner.jobs import IngestJob, MffOptions
from mapi_runner.worker.ingest import dataset_from_mff, dataset_from_table, preview, profile
from mapi_sdk.schema import validate_dataset
from mapi_spec.api import DimMapping, IngestMapping, VarMapping
from mapi_spec.errors import MapiError
from mapi_spec.schema import DimSpec, Role

pytestmark = pytest.mark.unit


def _table() -> pd.DataFrame:
    dates = pd.date_range("2026-01-04", periods=6, freq="W-SUN")
    rows = []
    for market in ("de", "fr"):
        for i, d in enumerate(dates):
            rows.append(
                {"Date": d.strftime("%Y-%m-%d"), "Market": market, "Rev": 100.0 + i, "Tv": i * 2}
            )
    return pd.DataFrame(rows)


def _mapping(**over: object) -> IngestMapping:
    base: dict[str, object] = {
        "format": "csv",
        "dims": {
            "date": DimMapping(column="Date", spec=DimSpec(kind="time", policy="extend")),
            "market": DimMapping(column="Market", spec=DimSpec(kind="categorical")),
        },
        "vars": {
            "revenue": VarMapping(column="Rev", role=Role.TARGET, units="EUR"),
            "tv": VarMapping(column="Tv", role=Role.SPEND),
        },
    }
    base.update(over)
    return IngestMapping.model_validate(base)


def test_table_to_dense_dataset_with_inferred_frequency() -> None:
    ds, schema = dataset_from_table(_table(), _mapping())
    assert dict(ds.sizes) == {"date": 6, "market": 2}
    assert schema.dims["date"].freq == "W-SUN"
    assert schema.vars["revenue"].role is Role.TARGET and schema.vars["revenue"].units == "EUR"
    assert schema.vars["tv"].dims == ("date", "market")
    assert ds["market"].dtype.kind == "U"
    assert not [i for i in validate_dataset(schema, ds) if i.severity.value == "error"]
    p = profile(ds, schema, [], b"raw")
    assert p["vars"]["revenue"]["min"] == 100.0 and p["vars"]["tv"]["missing"] == 0
    pv = preview(ds, 3)
    assert pv["total_rows"] == 12 and len(pv["rows"]) == 3
    json.dumps(pv)  # JSON-safe (timestamps as ISO strings)


@pytest.mark.parametrize(
    ("df_change", "message"),
    [
        (lambda df: df.drop(columns="Rev"), "not in the upload"),
        (lambda df: pd.concat([df, df.iloc[:1]]), "repeat a"),
        (lambda df: df.assign(Date=["not a date"] * len(df)), "cannot be read as a time"),
        (lambda df: df.assign(Market=[None] * len(df)), "empty labels"),
    ],
)
def test_table_errors_are_schema_invalid(df_change: object, message: str) -> None:
    df = df_change(_table())  # type: ignore[operator]
    with pytest.raises(MapiError) as exc:
        dataset_from_table(df, _mapping())
    assert exc.value.code.value == "SCHEMA_INVALID" and message in exc.value.detail


def test_dense_gaps_need_missing_allowed() -> None:
    df = _table().iloc[1:]  # de's first week is missing
    ds, schema = dataset_from_table(df, _mapping())
    errors = [i for i in validate_dataset(schema, ds) if i.severity.value == "error"]
    assert errors, "a gap in a dense cube is an error when missing is forbid"
    allow = _mapping(
        vars={
            "revenue": VarMapping(column="Rev", role=Role.TARGET, missing="allow"),
            "tv": VarMapping(column="Tv", missing="allow"),
        }
    )
    ds, schema = dataset_from_table(df, allow)
    assert not [i for i in validate_dataset(schema, ds) if i.severity.value == "error"]


def test_long_layout_needs_one_dim() -> None:
    with pytest.raises(MapiError, match="exactly one"):
        dataset_from_table(_table(), _mapping(layout="long"))


def _mff() -> pd.DataFrame:
    rows = []
    for period in pd.date_range("2026-01-04", periods=5, freq="W-SUN"):
        for geo in ("north", "south"):
            for var, value in (("sales", 10.0), ("tv_spend", 3.0)):
                rows.append(
                    {
                        "Period": period.strftime("%Y-%m-%d"),
                        "Geography": geo,
                        "Product": "all",
                        "Campaign": "",
                        "Outlet": "",
                        "Creative": "",
                        "VariableName": var,
                        "VariableValue": value,
                    }
                )
    return pd.DataFrame(rows)


def test_mff_preset_pivots_variables_and_keeps_varying_keys() -> None:
    opts = MffOptions(roles={"sales": "target", "tv_spend": "spend"}, units={"sales": "EUR"})
    ds, schema = dataset_from_mff(_mff(), opts)
    assert set(ds.data_vars) == {"sales", "tv_spend"}
    assert dict(ds.sizes) == {"date": 5, "geo": 2}  # Product etc. are constant: not dims
    assert schema.dims["date"].freq == "W-SUN" and schema.dims["date"].policy.value == "extend"
    assert schema.vars["sales"].role is Role.TARGET and schema.vars["sales"].units == "EUR"
    assert not [i for i in validate_dataset(schema, ds) if i.severity.value == "error"]


def test_mff_preset_errors() -> None:
    with pytest.raises(MapiError, match="no 'VariableValue'"):
        dataset_from_mff(_mff().drop(columns="VariableValue"), MffOptions())
    bad = _mff().astype({"VariableValue": object})
    bad.loc[0, "VariableValue"] = "lots"
    with pytest.raises(MapiError, match="not numbers"):
        dataset_from_mff(bad, MffOptions())
    with pytest.raises(MapiError, match="no variable 'nope'"):
        dataset_from_mff(_mff(), MffOptions(roles={"nope": "target"}))


def test_ingest_job_needs_exactly_one_of_mapping_or_preset() -> None:
    with pytest.raises(ValueError):
        IngestJob()
    with pytest.raises(ValueError):
        IngestJob(mapping=_mapping(), preset="mff")
    assert IngestJob(preset="mff").mff.value_column == "VariableValue"


def test_csv_parsing_handles_numeric_strings() -> None:
    csv = "Date,Market,Rev,Tv\n2026-01-04,de,1.5,2\n2026-01-04,fr,x,3\n"
    df = pd.read_csv(io.StringIO(csv))
    ds, schema = dataset_from_table(df, _mapping())
    assert schema.vars["revenue"].dtype == "str"  # a non-numeric cell keeps the column text
    assert np.issubdtype(ds["tv"].dtype, np.number)


def test_supervisor_import_never_loads_plugin_or_array_code() -> None:
    code = (
        "import sys, mapi_runner.supervisor, mapi_runner.__main__, mapi_runner.channel;"
        "bad = [m for m in ('mapi_sdk', 'mapi_pymc', 'mapi_sklearn', 'mapi_steps', "
        "'mapi_plots', 'numpy', 'xarray', 'pandas', 'pymc') if m in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""
