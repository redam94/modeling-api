"""Tabular ingest: column mappings, the MFF preset, profiles and previews (in process)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

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
        (lambda df: df.assign(Date=["not a date"] * len(df)), "as a time dim"),
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


def _mff_rows(spec: list[tuple[str, str, str, str, float]]) -> pd.DataFrame:
    """Rows ``(geo, product, campaign, variable, value)`` repeated over three weeks."""
    rows = []
    for period in pd.date_range("2026-01-04", periods=3, freq="W-SUN"):
        for geo, product, campaign, var, value in spec:
            rows.append({"Period": period.strftime("%Y-%m-%d"), "Geography": geo,
                         "Product": product, "Campaign": campaign, "Outlet": "", "Creative": "",
                         "VariableName": var, "VariableValue": value})  # fmt: skip
    return pd.DataFrame(rows)


def test_mff_national_rows_are_spread_and_breakdowns_summed() -> None:
    """mmm-framework's MFF semantics: a variable given only in total (blank or 'National'
    Geography) is spread across the geos; Campaign rows are summed unless split."""
    df = _mff_rows(
        [
            ("north", "", "", "sales", 30.0),
            ("south", "", "", "sales", 10.0),
            ("National", "", "", "tv", 40.0),
            ("north", "", "brand", "search", 2.0),
            ("north", "", "promo", "search", 3.0),
            ("south", "", "brand", "search", 1.0),
        ]
    )
    ds, schema = dataset_from_mff(df, MffOptions(roles={"sales": "target"}))
    assert dict(ds.sizes) == {"date": 3, "geo": 2}
    assert ds["tv"].sel(geo="north").values.tolist() == [20.0] * 3  # equal split
    assert ds["search"].sel(geo="north").values.tolist() == [5.0] * 3  # campaigns summed
    assert ds["search"].sel(geo="south").values.tolist() == [1.0] * 3
    assert not [i for i in validate_dataset(schema, ds) if i.severity.value == "error"]
    # by the KPI's share of each geo, as mmm-framework's 'sales' allocation
    kpi, _ = dataset_from_mff(
        df, MffOptions(roles={"sales": "target"}, allocation={"Geography": "kpi"})
    )
    assert kpi["tv"].sel(geo="north").values.tolist() == [30.0] * 3
    assert kpi["tv"].sel(geo="south").values.tolist() == [10.0] * 3
    # a breakdown kept as a dim on request
    split, _ = dataset_from_mff(df, MffOptions(split=["Campaign"]))
    assert "campaign" in split.dims and "brand" in [str(c) for c in split["campaign"].values]
    # a variable may not mix per-geo rows and national rows
    mixed = pd.concat([df, _mff_rows([("", "", "", "sales", 40.0)])], ignore_index=True)
    with pytest.raises(MapiError, match="rows for single Geography labels"):
        dataset_from_mff(mixed, MffOptions())


def test_mff_preset_errors() -> None:
    with pytest.raises(MapiError, match="no 'VariableValue'"):
        dataset_from_mff(_mff().drop(columns="VariableValue"), MffOptions())
    bad = _mff().astype({"VariableValue": object})
    bad.loc[0, "VariableValue"] = "lots"
    with pytest.raises(MapiError, match="not numbers"):
        dataset_from_mff(bad, MffOptions())
    with pytest.raises(MapiError, match="no variable 'nope'"):
        dataset_from_mff(_mff(), MffOptions(roles={"nope": "target"}))


@pytest.mark.parametrize(
    "dates",
    [
        # one offset, a 'Z' suffix, and offsets that change with daylight saving time
        [f"2024-01-{d:02d}T00:00:00+01:00" for d in (7, 14, 21)],
        [f"2024-01-{d:02d}T00:00:00Z" for d in (7, 14, 21)],
        ["2024-03-24T00:00:00+01:00", "2024-03-31T00:00:00+02:00", "2024-04-07T00:00:00+02:00"],
    ],
)
def test_dates_with_utc_offsets_are_read_as_local_dates(dates: list[str]) -> None:
    """BI exports often write offsets: they used to become tz-aware labels the dataset
    then refused ('labels are not dates'), or fail outright when DST changed the offset."""
    from mapi_runner.worker.ingest import INGEST_NOTES_ATTR
    from mapi_sdk.wire import read_zarr_zip, write_zarr_zip

    df = pd.DataFrame({"Date": dates, "Market": "de", "Rev": [1.0, 2.0, 3.0], "Tv": 1.0})
    ds, schema = dataset_from_table(df, _mapping())
    assert ds["date"].dtype.kind == "M"
    assert [str(d)[:10] for d in ds["date"].values] == [d[:10] for d in dates]
    assert schema.dims["date"].freq == "W-SUN"
    assert not [i for i in validate_dataset(schema, ds) if i.severity.value == "error"]
    [note] = ds.attrs.pop(INGEST_NOTES_ATTR)
    assert "UTC offsets" in note["message"] and note["pointer"] == "/mapping/dims/date"
    raw = write_zarr_zip(ds)
    assert raw is not None and read_zarr_zip(raw)["date"].dtype.kind == "M"


def test_a_tz_aware_parquet_timestamp_is_read_as_a_local_date() -> None:
    from mapi_runner.worker.ingest import INGEST_NOTES_ATTR

    stamps = pd.date_range("2024-01-07", periods=3, freq="W-SUN", tz="Europe/Berlin")
    df = pd.DataFrame({"Date": stamps, "Market": "de", "Rev": [1.0, 2.0, 3.0], "Tv": 1.0})
    ds, _ = dataset_from_table(df, _mapping())
    assert str(ds["date"].values[0])[:19] == "2024-01-07T00:00:00"
    assert INGEST_NOTES_ATTR in ds.attrs
    mff = _mff()
    mff["Period"] = mff["Period"] + "T00:00:00+01:00"
    ds, schema = dataset_from_mff(mff, MffOptions())
    assert ds["date"].dtype.kind == "M" and schema.dims["date"].freq == "W-SUN"
    assert ds.attrs[INGEST_NOTES_ATTR][0]["pointer"] == "/mapping/mff/Period"


def _diagonal(n: int) -> pd.DataFrame:
    """``n`` rows whose two keys never repeat: a dense cube of ``n x n`` cells."""
    return pd.DataFrame({"a": [f"a{i}" for i in range(n)], "b": [f"b{i}" for i in range(n)],
                         "v": 1.0})  # fmt: skip


def _diagonal_mapping() -> IngestMapping:
    return IngestMapping.model_validate(
        {
            "format": "csv",
            "dims": {"a": {"column": "a", "spec": {"kind": "categorical"}},
                     "b": {"column": "b", "spec": {"kind": "categorical"}}},
            "vars": {"v": {"column": "v", "role": "target"}},
        }
    )  # fmt: skip


def test_a_dense_cube_larger_than_the_limits_is_refused_before_it_is_built() -> None:
    """A small CSV of 'diagonal' rows mapped to two dims asks for n^2 cells (100k rows:
    1e10 float64, ~80 GB); it fails as PAYLOAD_TOO_LARGE instead of exhausting memory."""
    from mapi_runner.worker.tabular import IngestLimits
    from mapi_spec.errors import ErrorCode

    small = IngestLimits(max_cells=10_000)
    ds, _ = dataset_from_table(_diagonal(100), _diagonal_mapping(), limits=small)
    assert dict(ds.sizes) == {"a": 100, "b": 100}
    with pytest.raises(MapiError) as exc:
        dataset_from_table(_diagonal(101), _diagonal_mapping(), limits=small)
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    assert exc.value.pointer == "/mapping/dims"
    assert "10,201" in exc.value.detail
    # the default limits refuse the memory bomb outright (and quickly)
    with pytest.raises(MapiError) as exc:
        dataset_from_table(_diagonal(100_000), _diagonal_mapping())
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    # the byte budget (the class's scratch) applies too
    tight = IngestLimits(max_uncompressed_bytes=8 * 50 * 50)
    with pytest.raises(MapiError) as exc:
        dataset_from_table(_diagonal(51), _diagonal_mapping(), limits=tight)
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_the_mff_pivot_is_capped_like_a_dense_table() -> None:
    from mapi_runner.worker.tabular import IngestLimits
    from mapi_spec.errors import ErrorCode

    with pytest.raises(MapiError) as exc:
        dataset_from_mff(_mff(), MffOptions(), limits=IngestLimits(max_cells=19))
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    ds, _ = dataset_from_mff(_mff(), MffOptions(), limits=IngestLimits(max_cells=20))
    assert dict(ds.sizes) == {"date": 5, "geo": 2}


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


def test_parquet_and_netcdf_uploads(tmp_path: Path) -> None:
    from mapi_runner.worker.ingest import _read_table, _self_describing, _sniff

    pq = tmp_path / "u.parquet"
    _table().to_parquet(pq)
    assert _sniff(pq) == "parquet"
    ds, _schema = dataset_from_table(_read_table(pq, "parquet", max_rows=100), _mapping())
    assert dict(ds.sizes) == {"date": 6, "market": 2}

    nc = tmp_path / "u.nc"
    ds.to_netcdf(nc, engine="scipy")  # netCDF-3
    assert _sniff(nc) == "netcdf"
    back, inferred = _self_describing(nc, "netcdf", None)
    assert set(back.data_vars) == {"revenue", "tv"}
    assert inferred.dims["date"].kind == "time"
    mapped, mschema = _self_describing(
        nc,
        "netcdf",
        IngestMapping(
            format="netcdf",
            dims={"week": DimMapping(column="date", spec=DimSpec(kind="time", policy="extend"))},
            vars={"sales": VarMapping(column="revenue", role=Role.TARGET)},
        ),
    )
    assert set(mapped.data_vars) == {"sales"} and "week" in mapped.dims
    assert mschema.vars["sales"].role is Role.TARGET
    with pytest.raises(MapiError, match="rows"):
        _read_table(pq, "parquet", max_rows=5)


def test_the_profile_of_8_bit_pixels_counts_levels_and_the_preview_has_no_pixels() -> None:
    """v0.14: an image stack (pixels with ``mapi_image``) keeps profile and preview small."""
    import xarray as xr
    from mapi_sdk.schema import infer_schema

    rng = np.random.default_rng(2)
    px = rng.integers(0, 256, (30, 5, 4, 3)).astype(np.uint8)
    ds = xr.Dataset(
        {"pixels": (("image", "y", "x", "channel"), px, {"mapi_image": "1"})},
        coords={
            "image": [f"{i:02d}" for i in range(30)],
            "y": np.arange(5),
            "x": np.arange(4),
            "channel": ["r", "g", "b"],
            "path": ("image", [f"{i}.png" for i in range(30)]),
        },
    )
    prof = profile(ds, infer_schema(ds), [], b"")["vars"]["pixels"]
    flat = px.astype(float).ravel()
    assert prof["count"] == flat.size and prof["missing"] == 0
    assert prof["mean"] == pytest.approx(flat.mean()) and prof["std"] == pytest.approx(flat.std())
    assert (prof["min"], prof["max"]) == (flat.min(), flat.max())
    assert prof["p95"] == pytest.approx(np.quantile(flat, 0.95), abs=2)
    pv = preview(ds, 10)
    assert pv["total_rows"] == 30 and [c["name"] for c in pv["columns"]] == ["image", "path"]
    # without the attribute a 4-D variable previews one row per cell, as before
    plain = ds.copy(deep=True)
    plain["pixels"].attrs = {}
    assert preview(plain, 10)["total_rows"] == px.size
