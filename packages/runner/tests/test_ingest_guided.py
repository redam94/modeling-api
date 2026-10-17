"""M3 sandboxed ingest: wide variables, the Meridian and MFF-with-mapping presets, column
profiles (``columns.json``) and the caps on rows, columns, cells and decompression - each
refusal a plain message with a suggested action."""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_runner.jobs import IngestJob
from mapi_runner.worker.ingest import (
    _self_describing,
    build_dataset,
    dataset_from_table,
    mff_table,
)
from mapi_runner.worker.tabular import IngestLimits, column_profiles, read_table, write_profiles
from mapi_sdk.schema import validate_dataset
from mapi_sdk.wire import write_zarr_zip
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.guided import (
    GUIDED_TEMPLATES,
    ColumnProfile,
    GuidedAnswers,
    build_draft,
    draft_schema,
    suggest_roles,
)
from mapi_spec.ingest import (
    IngestMapping,
    MffOptions,
)
from mapi_spec.schema import Role

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
BI_MFF = FIXTURES / "mff_long.csv"


def _errors(ds: xr.Dataset, schema: Any) -> list[str]:
    return [i.message for i in validate_dataset(schema, ds) if i.severity.value == "error"]


def _write(tmp_path: Path, name: str, data: bytes | str) -> Path:
    path = tmp_path / name
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def _refused(exc: pytest.ExceptionInfo[MapiError], code: ErrorCode) -> str:
    err = exc.value
    assert err.code is code, (err.code, err.detail)
    [issue] = err.issues
    assert issue.suggested_action, "every refusal says what to do"
    return err.detail


# ------------------------------------------------------------------------- a geo MMM table


def _mmm_frame() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    weeks = pd.date_range("2025-01-05", periods=30, freq="W-SUN")
    rows = []
    for region in ("north", "south", "west"):
        for w in weeks:
            tv, search = rng.uniform(0, 100), rng.uniform(0, 50)
            rows.append(
                {
                    "Week": w.strftime("%Y-%m-%d"),
                    "Region": region,
                    "Revenue (EUR)": 1000 + 2 * tv + 3 * search + rng.normal(0, 5),
                    "TV_Spend": round(tv, 2),
                    "SearchSpend": round(search, 2),
                    "Avg Price": round(rng.uniform(2, 3), 2),
                }
            )
    return pd.DataFrame(rows)


def test_a_guided_draft_ingests_into_the_schema_it_promised(tmp_path: Path) -> None:
    """Upload → profiles (sandbox) → detection → draft → ingest with the draft mapping."""
    path = _write(tmp_path, "upload", _mmm_frame().to_csv(index=False))
    df = read_table(path, "csv", IngestLimits())
    profiles = column_profiles(df)
    by = {p.name: p for p in profiles}
    assert by["Week"].dtype == "date" and by["Week"].detected_freq == "W-SUN"
    assert by["Region"].dtype == "string" and by["Region"].n_distinct == 3
    assert by["TV_Spend"].dtype == "number" and by["TV_Spend"].min is not None
    sug = suggest_roles(GUIDED_TEMPLATES["geo_mmm"], profiles)
    answers = {q.question_id: q.answer for q in sug.questions}
    req = GuidedAnswers(
        template_id="geo_mmm", name="mmm", dataset="media", answers=answers, columns=profiles
    )
    draft = build_draft(GUIDED_TEMPLATES["geo_mmm"], req, project="growth")
    ds, schema, cols = build_dataset(path, IngestJob(mapping=draft.mapping))
    assert dict(ds.sizes) == {"week": 30, "region": 3, "channel": 2}
    assert list(ds["channel"].values) == list(draft.mapping.wide["spend"].columns)  # kept order
    assert schema.vars["spend"].role is Role.SPEND
    assert schema.vars["spend"].dims == ("week", "region", "channel")
    assert schema.dims["week"].freq == "W-SUN"
    assert not _errors(ds, schema)
    promised = draft_schema(draft.mapping, profiles)
    assert set(promised.vars) == set(schema.vars) and set(promised.dims) == set(schema.dims)
    for name, var in promised.vars.items():
        assert var.dims == schema.vars[name].dims and var.role is schema.vars[name].role
    assert cols is not None and {c.name for c in cols} == set(df.columns)
    back = [ColumnProfile.model_validate(c) for c in json.loads(write_profiles(cols))]
    assert back == cols


def test_wide_variables_must_agree_on_labels() -> None:
    df = _mmm_frame()
    base = {
        "format": "csv",
        "dims": {"week": {"column": "Week", "spec": {"kind": "time"}},
                 "region": {"column": "Region", "spec": {"kind": "categorical"}}},
    }  # fmt: skip
    mapping = IngestMapping.model_validate(
        {**base, "wide": {"spend": {"dim": "channel", "columns": {"tv": "TV_Spend"}},
                          "price": {"dim": "channel", "columns": {"search": "Avg Price"}}}}
    )  # fmt: skip
    with pytest.raises(MapiError) as exc:
        dataset_from_table(df, mapping)
    assert "label 'channel' differently" in _refused(exc, ErrorCode.SCHEMA_INVALID)
    clash = IngestMapping.model_validate(
        {**base, "wide": {"spend": {"dim": "region", "columns": {"tv": "TV_Spend"}}}}
    )
    with pytest.raises(MapiError, match="already a dim"):
        dataset_from_table(df, clash)
    text = df.assign(TV_Spend=["€1,000"] * len(df))
    ok = IngestMapping.model_validate(
        {**base, "wide": {"spend": {"dim": "channel", "columns": {"tv": "TV_Spend"}}}}
    )
    with pytest.raises(MapiError) as exc:
        dataset_from_table(text, ok)
    assert "not numbers" in _refused(exc, ErrorCode.SCHEMA_INVALID)
    assert exc.value.untrusted_text and "€" in exc.value.untrusted_text


def test_missing_columns_suggest_close_names() -> None:
    mapping = IngestMapping.model_validate(
        {"format": "csv", "dims": {"week": {"column": "week", "spec": {"kind": "time"}}},
         "vars": {"y": {"column": "Revenue", "role": "target"}}}
    )  # fmt: skip
    with pytest.raises(MapiError) as exc:
        dataset_from_table(_mmm_frame(), mapping)
    assert exc.value.issues[0].suggested_action == "Did you mean 'Week'?"


def test_repeated_rows_name_the_column_that_varies() -> None:
    df = _mmm_frame()
    mapping = IngestMapping.model_validate(
        {"format": "csv", "dims": {"week": {"column": "Week", "spec": {"kind": "time"}}},
         "vars": {"y": {"column": "Revenue (EUR)"}}}
    )  # fmt: skip
    with pytest.raises(MapiError) as exc:
        dataset_from_table(df, mapping)
    assert "repeat a (week) combination" in exc.value.detail
    assert "'Region'" in (exc.value.issues[0].suggested_action or "")


# ================================================================================= Meridian


def test_meridian_preset(tmp_path: Path) -> None:
    rows = []
    for geo in ("g1", "g2"):
        for i, t in enumerate(pd.date_range("2025-01-06", periods=8, freq="W-MON")):
            rows.append({"time": t.date().isoformat(), "geo": geo, "conversions": 50 + i,
                         "revenue_per_conversion": 2.5, "population": 1e5,
                         "competitor_sales": 10.0 + i, "tv_impressions": 1000.0 * i,
                         "tv_spend": 100.0 * i, "search_impressions": 300.0,
                         "search_spend": 30.0})  # fmt: skip
    path = _write(tmp_path, "upload", pd.DataFrame(rows).to_csv(index=False))
    mapping = IngestMapping.model_validate(
        {"format": "csv", "preset": "meridian",
         "meridian": {"kpi": "conversions", "kpi_type": "non_revenue",
                      "revenue_per_kpi": "revenue_per_conversion", "population": "population",
                      "controls": ["competitor_sales"], "media_channels": ["tv", "search"],
                      "media": ["tv_impressions", "search_impressions"],
                      "media_spend": ["tv_spend", "search_spend"], "freq": "W-MON"}}
    )  # fmt: skip
    ds, schema, cols = build_dataset(path, IngestJob(mapping=mapping))
    assert dict(ds.sizes) == {"time": 8, "geo": 2, "channel": 2}
    assert schema.vars["kpi"].role is Role.TARGET
    assert schema.vars["media"].role is Role.MEDIA and schema.vars["spend"].role is Role.SPEND
    assert schema.vars["competitor_sales"].role is Role.CONTROL
    assert schema.dims["time"].freq == "W-MON" and schema.dims["geo"].policy.value == "new_levels"
    assert float(ds["spend"].sel(channel="tv", geo="g1").max()) == 700.0
    assert not _errors(ds, schema) and cols is not None


# ===================================================================================== MFF


def test_mff_with_a_mapping_gathers_variables_into_a_wide_spend(tmp_path: Path) -> None:
    cols = [
        ColumnProfile(name="Period", dtype="date", n_rows=10, detected_freq="W-MON"),
        ColumnProfile(name="Geography", dtype="string", n_rows=10, n_distinct=4),
    ]
    sug_cols = cols + [
        ColumnProfile(name=v, dtype="number", n_rows=10, min=0.0, origin="mff_variable")
        for v in ("Sales", "media_var_0", "media_var_1")
    ]
    req = GuidedAnswers(
        template_id="geo_mmm", name="mff", dataset="mff", format="mff",
        answers={"kpi": "Sales", "date": "Period", "geo": "Geography",
                 "spend": ["media_var_0", "media_var_1"]},
        columns=sug_cols,
    )  # fmt: skip
    draft = build_draft(GUIDED_TEMPLATES["geo_mmm"], req, project="p")
    # the fixture also varies by Product: refused until it is a dim (or combined)
    with pytest.raises(MapiError) as exc:
        build_dataset(BI_MFF, IngestJob(mapping=draft.mapping))
    assert "repeat a" in exc.value.detail
    combined = draft.mapping.model_copy(
        update={"mff": MffOptions(combine={"Geography": ["Geography", "Product"]})}
    )
    ds, schema, profiles = build_dataset(BI_MFF, IngestJob(mapping=combined))
    assert dict(ds.sizes) == {"period": 40, "geography": 4, "channel": 2}
    assert sorted(ds["geography"].values) == ["DE|Phones", "DE|Watches", "US|Phones", "US|Watches"]
    assert schema.dims["period"].freq == "W-MON"
    assert schema.vars["sales"].role is Role.TARGET
    assert profiles is not None
    origins = {p.name: p.origin for p in profiles}
    assert origins["Sales"] == "mff_variable" and origins["Period"] == "column"


def test_mff_refusals_are_plain() -> None:
    df = pd.read_csv(BI_MFF)
    with pytest.raises(MapiError) as exc:
        mff_table(df.drop(columns="VariableName"), MffOptions())
    assert "MFF upload has no 'VariableName'" in _refused(exc, ErrorCode.SCHEMA_INVALID)
    with pytest.raises(MapiError) as exc:
        mff_table(df, MffOptions(), IngestLimits(max_columns=3))
    assert "6 variables" in _refused(exc, ErrorCode.PAYLOAD_TOO_LARGE)
    dup = pd.concat([df, df.head(1)])
    with pytest.raises(MapiError, match="repeat a key"):
        mff_table(dup, MffOptions())
    bad = df.assign(VariableName=df["VariableName"].replace({"Price": "Geography"}))
    with pytest.raises(MapiError, match="name of an MFF key"):
        mff_table(bad, MffOptions())


# ======================================================================================= caps


def test_row_column_and_cell_caps(tmp_path: Path) -> None:
    csv = "a,b\n" + "\n".join(f"{i},{i}" for i in range(20))
    path = _write(tmp_path, "upload", csv)
    with pytest.raises(MapiError) as exc:
        read_table(path, "csv", IngestLimits(max_rows=10))
    assert "more than 10 rows" in _refused(exc, ErrorCode.PAYLOAD_TOO_LARGE)
    wide = _write(tmp_path, "wide", ",".join(f"c{i}" for i in range(30)) + "\n" + "1," * 29 + "1")
    with pytest.raises(MapiError) as exc:
        read_table(wide, "csv", IngestLimits(max_columns=20))
    assert "30 columns" in _refused(exc, ErrorCode.PAYLOAD_TOO_LARGE)
    long = _write(tmp_path, "long", "a,b\n1," + "x" * 500 + "\n")
    with pytest.raises(MapiError) as exc:
        read_table(long, "csv", IngestLimits(max_cell_chars=100))
    assert "500 characters (row 2)" in _refused(exc, ErrorCode.SCHEMA_INVALID)
    header = _write(tmp_path, "header", "a," + "h" * 300 + "\n1,2\n")
    with pytest.raises(MapiError, match="column name is 300"):
        read_table(header, "csv", IngestLimits())


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "the upload is empty"),
        (b"a,a\n1,2\n", "appears twice"),
        ("a,b\n1,\xe9\n".encode("latin-1"), "not UTF-8"),
        (b'a,b\n1,"open\n', "not a readable CSV"),
        (b"BZh91AY&SY", "bzip2 compressed uploads are not supported"),
        (b"PK\x03\x04rest", "is a zip archive, not a csv table"),
    ],
)
def test_unreadable_uploads_are_explained(tmp_path: Path, content: bytes, message: str) -> None:
    path = _write(tmp_path, "upload", content)
    with pytest.raises(MapiError) as exc:
        read_table(path, "csv", IngestLimits())
    assert message in exc.value.detail
    assert exc.value.issues[0].suggested_action


def test_semicolon_csv_and_gzip(tmp_path: Path) -> None:
    text = "Week;Sales\n2026-01-04;1,5\n2026-01-11;2\n"
    df = read_table(_write(tmp_path, "semi", text), "csv", IngestLimits())
    assert list(df.columns) == ["Week", "Sales"]
    gz = _write(tmp_path, "gz", gzip.compress(_mmm_frame().to_csv(index=False).encode()))
    assert len(read_table(gz, "csv", IngestLimits())) == 90
    assert not list(tmp_path.glob("gz.csv"))  # the decompressed copy is removed


def test_decompression_bombs_are_refused(tmp_path: Path) -> None:
    bomb = gzip.compress(b"a,b\n" + b"0,0\n" * 500_000)
    path = _write(tmp_path, "bomb", bomb)
    with pytest.raises(MapiError) as exc:
        read_table(path, "csv", IngestLimits(max_decompression_ratio=20, ratio_floor_bytes=0))
    assert "expands to more than" in _refused(exc, ErrorCode.PAYLOAD_TOO_LARGE)
    assert not list(tmp_path.glob("bomb.csv"))

    pq = tmp_path / "zeros.parquet"
    pd.DataFrame({"a": np.zeros(200_000)}).to_parquet(pq)
    with pytest.raises(MapiError) as exc:
        read_table(pq, "parquet", IngestLimits(max_decompression_ratio=2, ratio_floor_bytes=0))
    assert "Parquet file expands" in _refused(exc, ErrorCode.PAYLOAD_TOO_LARGE)
    with pytest.raises(MapiError, match="200,000 rows"):
        read_table(pq, "parquet", IngestLimits(max_rows=1000))
    assert len(read_table(pq, "parquet", IngestLimits())) == 200_000

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zarr.json", b"\0" * 5_000_000)
    zpath = _write(tmp_path, "z.zarr.zip", buf.getvalue())
    with pytest.raises(MapiError) as exc:
        _self_describing(
            zpath, "zarr_zip", None, IngestLimits(max_decompression_ratio=10, ratio_floor_bytes=0)
        )
    assert "archive expands" in _refused(exc, ErrorCode.PAYLOAD_TOO_LARGE)

    nc = tmp_path / "x.nc"
    xr.Dataset({"v": (("t",), np.zeros(10_000))}).to_netcdf(nc, engine="scipy")
    with pytest.raises(MapiError, match="decodes to"):
        _self_describing(nc, "netcdf", None, IngestLimits(max_uncompressed_bytes=1000))


def test_self_describing_uploads_keep_working(tmp_path: Path) -> None:
    ds = xr.Dataset(
        {"y": (("t",), np.arange(5.0))}, coords={"t": pd.date_range("2026-01-04", periods=5)}
    )
    path = _write(tmp_path, "upload", write_zarr_zip(ds) or b"")
    mapping = IngestMapping(format="zarr_zip")
    back, _, cols = build_dataset(path, IngestJob(mapping=mapping))
    assert cols is None and float(back["y"].sum()) == 10.0
    with pytest.raises(MapiError, match="not a netcdf file"):
        build_dataset(path, IngestJob(mapping=IngestMapping(format="netcdf")))


def test_column_profiles_detect_types_and_order() -> None:
    df = pd.DataFrame(
        {
            "when": ["2026-01-04", "2026-01-11", "2026-01-18", "2026-01-25"],
            "n": [1, 2, 3, 5],
            "x": [0.5, None, 2.5, -1.0],
            "flag": ["true", "false", "TRUE", "false"],
            "label": ["a", "b", "a", None],
            "code": ["1", "2", "2", "3"],
        }
    )
    by = {p.name: p for p in column_profiles(df)}
    assert by["when"].dtype == "date" and by["when"].detected_freq == "W-SUN"
    assert by["when"].monotonic is True and by["when"].min_date == "2026-01-04T00:00:00"
    assert by["n"].dtype == "integer" and by["n"].monotonic is True and by["n"].max == 5.0
    assert by["x"].dtype == "number" and by["x"].n_missing == 1 and by["x"].min == -1.0
    assert by["flag"].dtype == "boolean"
    assert by["label"].dtype == "string" and by["label"].n_distinct == 2
    assert by["label"].sample == ["a", "b"]
    assert by["code"].dtype == "integer"  # numbers written as text
    assert all(p.origin == "column" for p in by.values())
