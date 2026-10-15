"""Wire formats: XrJSON, zarr zip, DataTree zip, stack/unstack and Arrow IPC."""

from __future__ import annotations

import io
import json
import zipfile

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mapi_sdk import wire
from mapi_sdk._util import normalise_dataset
from mapi_sdk.errors import PayloadLimitError, WireFormatError
from mapi_sdk.testing.strategies import datasets

PROPS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _panel() -> xr.Dataset:
    return xr.Dataset(
        {
            "y": (("geo", "time"), np.array([[1.0, np.nan, 3.0], [4.0, 5.0, 6.0]])),
            "label": (("geo",), np.array(["north", "süd"])),
            "flag": (("time",), np.array([True, False, True])),
            "n": ((), np.int64(3)),
        },
        coords={
            "geo": ["NL", "DE"],
            "time": pd.date_range("2024-01-07", periods=3, freq="W-SUN"),
        },
        attrs={"source": "unit", "nested": {"a": [1, 2]}},
    )


# ------------------------------------------------------------------------------------ XrJSON


def test_xrjson_roundtrip_and_shape() -> None:
    ds = _panel()
    obj = wire.to_xrjson(ds)
    assert set(obj) == {"dims", "attrs", "coords", "data_vars"}
    assert obj["data_vars"]["y"]["data"][0][1] is None
    assert obj["coords"]["time"]["dtype"] == "datetime64[ns]"
    back = wire.loads_xrjson(wire.dumps_xrjson(ds))
    xr.testing.assert_identical(normalise_dataset(ds), back)


def test_xrjson_accepts_to_dict_output_without_dtypes() -> None:
    ds = xr.Dataset(
        {"y": (("g",), [1.5, 2.5]), "k": (("g",), [1, 2]), "b": (("g",), [True, False])},
        coords={"g": ["a", "b"]},
    )
    back = wire.from_xrjson(ds.to_dict(data="list"))
    xr.testing.assert_identical(ds, back)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"bogus": 1}, "unknown XrJSON keys"),
        ({"data_vars": {"y": {"dims": ["a"], "data": [[1]]}}}, "nested deeper"),
        ({"data_vars": {"y": {"dims": ["a", "b"], "data": [[1, 2], [3]]}}}, "ragged"),
        ({"data_vars": {"y": {"dims": ["a"], "data": [1, "x"]}}}, "mixed"),
        ({"data_vars": {"y": {"dims": ["a"], "data": [1], "dtype": "complex128"}}}, "unsupported"),
        ({"data_vars": {"y": {"dims": ["a"], "data": [None], "dtype": "int64"}}}, "integers"),
        (
            {
                "data_vars": {
                    "y": {"dims": ["a"], "data": [1]},
                    "z": {"dims": ["a"], "data": [1, 2]},
                }
            },
            "inconsistent",
        ),
        ({"dims": {"a": 3}, "data_vars": {"y": {"dims": ["a"], "data": [1]}}}, "declared"),
        ({"data_vars": {"y": {"dims": ["a", "a"], "data": [[1]]}}}, "repeated"),
        ({"data_vars": {"y": {"dims": ["a"], "data": [1], "extra": 1}}}, "unknown keys"),
        ({"data_vars": {"y": {"dims": ["a"], "data": [1], "dtype": "datetime64[ns]"}}}, "ISO"),
        ({"data_vars": {"y": {"dims": ["a"]}}}, "missing data"),
        ({"data_vars": {"": {"dims": ["a"], "data": [1]}}}, "invalid variable name"),
    ],
)
def test_xrjson_rejects_malformed(payload: dict, match: str) -> None:
    with pytest.raises(WireFormatError, match=match):
        wire.from_xrjson(payload)


def test_xrjson_limits() -> None:
    tight = wire.WireLimits(max_elements=5, max_string_chars=3, max_json_bytes=200)
    with pytest.raises(PayloadLimitError):
        wire.from_xrjson(
            {"data_vars": {"y": {"dims": ["a"], "data": list(range(6))}}}, limits=tight
        )
    with pytest.raises(PayloadLimitError):
        wire.from_xrjson({"data_vars": {"y": {"dims": ["a"], "data": ["abcd"]}}}, limits=tight)
    with pytest.raises(PayloadLimitError):
        wire.loads_xrjson("[" * 300 + "]" * 300, limits=tight)
    with pytest.raises(WireFormatError):
        wire.loads_xrjson("[" * 5000 + "]" * 5000)
    deep = {"data_vars": {"y": {"dims": [f"d{i}" for i in range(40)], "data": []}}}
    with pytest.raises(PayloadLimitError, match="dims"):
        wire.from_xrjson(deep)
    with pytest.raises(WireFormatError, match="NaN"):
        wire.loads_xrjson('{"data_vars": {"y": {"dims": ["a"], "data": [NaN]}}}')
    with pytest.raises(WireFormatError, match="not valid JSON"):
        wire.loads_xrjson("{")
    big = xr.Dataset({"y": (("a",), np.zeros(10))})
    with pytest.raises(PayloadLimitError):
        wire.to_xrjson(big, limits=wire.WireLimits(max_elements=5))


def test_xrjson_rejects_inf_and_unserialisable_attrs() -> None:
    with pytest.raises(WireFormatError, match="infinite"):
        wire.to_xrjson(xr.Dataset({"y": (("a",), [np.inf])}))
    with pytest.raises(WireFormatError, match="attributes"):
        wire.to_xrjson(xr.Dataset(attrs={"bad": object()}))
    with pytest.raises(PayloadLimitError):
        wire.to_xrjson(
            xr.Dataset(attrs={"big": "x" * 100}), limits=wire.WireLimits(max_attrs_bytes=50)
        )


@PROPS
@given(datasets())
def test_property_xrjson_roundtrip(ds: xr.Dataset) -> None:
    back = wire.loads_xrjson(wire.dumps_xrjson(ds))
    xr.testing.assert_identical(normalise_dataset(ds), back)


# ----------------------------------------------------------------------------------- zarr zip


def test_zarr_zip_roundtrip_and_encoding_rules(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ds = _panel()
    path = tmp_path / "d.zarr.zip"
    assert wire.write_zarr_zip(ds, path) is None
    entries = wire.zip_entries(path)
    time_meta = json.loads(entries["time/zarr.json"])
    assert time_meta["data_type"] == "int64"
    assert time_meta["attributes"] == {
        "units": "nanoseconds since 1970-01-01",
        "calendar": "proleptic_gregorian",
    }
    geo_meta = json.loads(entries["geo/zarr.json"])
    assert geo_meta["data_type"] == "string"
    assert [c["name"] for c in geo_meta["codecs"]] == ["vlen-utf8", "zstd"]
    with zipfile.ZipFile(path) as zf:
        assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
        names = [i.filename for i in zf.infolist()]
        assert len(names) == len(set(names))
    back = wire.read_zarr_zip(path)
    xr.testing.assert_identical(normalise_dataset(ds), back)


def test_zarr_zip_is_deterministic_and_blosc() -> None:
    ds = _panel()
    assert wire.write_zarr_zip(ds) == wire.write_zarr_zip(ds)
    data = wire.write_zarr_zip(ds, compressor="blosc")
    assert data is not None
    meta = json.loads(wire.zip_entries(data)["y/zarr.json"])
    assert meta["codecs"][-1]["name"] == "blosc"
    xr.testing.assert_identical(normalise_dataset(ds), wire.read_zarr_zip(data))
    buf = io.BytesIO()
    wire.write_zarr_zip(ds, buf)
    assert buf.getvalue() == data or len(buf.getvalue()) > 0
    with pytest.raises(WireFormatError, match="not allowed"):
        wire.write_zarr_zip(ds, compressor="gzip")  # type: ignore[arg-type]


def test_zarr_zip_multiindex_reset_and_restored() -> None:
    stacked = _panel()[["y"]].stack(z=["geo", "time"])
    data = wire.write_zarr_zip(stacked)
    assert data is not None
    assert "z/zarr.json" not in wire.zip_entries(data)
    back = wire.read_zarr_zip(data)
    assert isinstance(back.indexes["z"], pd.MultiIndex)
    xr.testing.assert_identical(normalise_dataset(stacked), back)


def test_zarr_zip_rejects_unpersistable_dtypes() -> None:
    with pytest.raises(WireFormatError, match="cannot be persisted"):
        wire.write_zarr_zip(xr.Dataset({"c": (("a",), np.array([1j]))}))
    with pytest.raises(WireFormatError, match="cannot be persisted"):
        wire.write_zarr_zip(xr.Dataset({"o": (("a",), np.array([{"x": 1}], dtype=object))}))


def _zip(entries: dict[str, bytes], **info_kw: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            for k, v in info_kw.items():
                setattr(info, k, v)
            zf.writestr(info, data)
    return buf.getvalue()


def _with_meta(meta_patch: dict) -> bytes:
    data = wire.write_zarr_zip(xr.Dataset({"y": (("a",), [1.0, 2.0])}))
    assert data is not None
    entries = wire.zip_entries(data)
    meta = json.loads(entries["y/zarr.json"])
    meta.update(meta_patch)
    entries["y/zarr.json"] = json.dumps(meta).encode()
    return _zip(entries)


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"codecs": [{"name": "bytes"}, {"name": "gzip"}]}, "zstd/blosc"),
        ({"codecs": [{"name": "sharding_indexed"}]}, "serializer"),
        ({"data_type": "complex128"}, "not allowed"),
        ({"storage_transformers": [{"name": "x"}]}, "storage transformers"),
        ({"chunk_grid": {"name": "rectilinear"}}, "regular"),
        ({"shape": [-1]}, "invalid shape"),
        ({"attributes": {"units": "days since 2000-01-01", "calendar": "360_day"}}, "calendar"),
        ({"node_type": "weird"}, "unknown node"),
        ({"zarr_format": 2}, "not zarr v3"),
        ({"codecs": []}, "missing codecs"),
    ],
)
def test_zarr_zip_rejects_hostile_metadata(patch: dict, match: str) -> None:
    with pytest.raises(WireFormatError, match=match):
        wire.read_zarr_zip(_with_meta(patch))


def test_zarr_zip_rejects_huge_declared_arrays() -> None:
    with pytest.raises(PayloadLimitError):
        wire.read_zarr_zip(_with_meta({"shape": [10**12]}))


@pytest.mark.parametrize("name", ["../evil", "/abs/zarr.json", "a\\b", "c:/x", "a/./b"])
def test_zip_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(WireFormatError, match="unsafe name"):
        wire.zip_entries(_zip({name: b"x"}))


def test_zip_rejects_links_v2_and_garbage() -> None:
    link = _zip({"zarr.json": b"{}"}, external_attr=(0o120777 << 16))
    with pytest.raises(WireFormatError, match="regular file"):
        wire.zip_entries(link)
    with pytest.raises(WireFormatError, match="v3"):
        wire.read_zarr_zip(_zip({".zgroup": b"{}"}))
    with pytest.raises(WireFormatError, match="not a readable zip"):
        wire.read_zarr_zip(b"not a zip")
    with pytest.raises(WireFormatError, match="invalid zarr metadata"):
        wire.read_zarr_zip(_zip({"zarr.json": b"{nope"}))
    with pytest.raises(PayloadLimitError):
        wire.zip_entries(_zip({"a": b"1", "b": b"2"}), limits=wire.WireLimits(max_zip_entries=1))
    with pytest.raises(PayloadLimitError):
        wire.zip_entries(_zip({"a": b"12345"}), limits=wire.WireLimits(max_uncompressed_bytes=3))


def test_zip_rejects_duplicate_entries() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("zarr.json", b"{}")
        with pytest.warns(UserWarning):
            zf.writestr("zarr.json", b"{}")
    with pytest.raises(WireFormatError, match="twice"):
        wire.zip_entries(buf.getvalue())


@PROPS
@given(datasets())
def test_property_zarr_zip_roundtrip(ds: xr.Dataset) -> None:
    data = wire.write_zarr_zip(ds)
    assert data is not None
    xr.testing.assert_identical(normalise_dataset(ds), wire.read_zarr_zip(data))


# ------------------------------------------------------------------------------------ DataTree


def test_datatree_roundtrip() -> None:
    ds = _panel()
    tree = xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"format": "bundle/v1"}),
            "/model/posterior": ds,
            "/preprocess/scale/arrays": xr.Dataset({"s": (("geo",), [1.0, 2.0])}),
            "/empty": xr.Dataset(),
        }
    )
    data = wire.write_datatree_zip(tree)
    assert data is not None
    back = wire.read_datatree_zip(data)
    assert set(back.groups) == set(tree.groups)
    xr.testing.assert_identical(back["model/posterior"].to_dataset(), normalise_dataset(ds))
    assert back.attrs == {"format": "bundle/v1"}


# ------------------------------------------------------------------------------- stack/unstack


def test_stack_drops_empty_rows_and_unstack_restores() -> None:
    ds = xr.Dataset(
        {
            "y": (("geo", "time"), np.array([[1.0, np.nan], [np.nan, np.nan]])),
            "x": (("geo", "time"), np.array([[1.0, np.nan], [2.0, np.nan]])),
            "w": (("geo",), np.array([10, 20])),
        },
        coords={"geo": ["a", "b"], "time": [1, 2]},
    )
    st_all = wire.stack_rows(ds, ["geo", "time"], drop="all")
    assert st_all.sizes["row"] == 2
    assert list(st_all["geo"].values) == ["a", "b"]
    st_any = wire.stack_rows(ds, ["geo", "time"], drop="any")
    assert st_any.sizes["row"] == 1  # only full-rank vars (y, x) decide emptiness
    back = wire.unstack_rows(st_all)
    xr.testing.assert_identical(back, ds)


def test_stack_with_mask_var_and_errors() -> None:
    ds = xr.Dataset(
        {
            "y": (("g", "t"), np.arange(4.0).reshape(2, 2)),
            "m": (("g", "t"), np.array([[True, False], [True, True]])),
        },
        coords={"g": ["a", "b"], "t": [0, 1]},
    )
    st_m = wire.stack_rows(ds, ["g", "t"], mask_var="m")
    assert st_m.sizes["row"] == 3
    assert "m" not in st_m
    back = wire.unstack_rows(st_m)
    assert np.isnan(back["y"].sel(g="a", t=1))
    with pytest.raises(ValueError, match="unknown dims"):
        wire.stack_rows(ds, ["nope"])
    with pytest.raises(ValueError, match="already exists"):
        wire.stack_rows(ds, ["g"], new_dim="t")
    with pytest.raises(ValueError, match="at least one"):
        wire.stack_rows(ds, [])
    with pytest.raises(ValueError, match="not found"):
        wire.unstack_rows(ds)


@PROPS
@given(datasets(dtypes=("float64",), min_size=1), st.sampled_from(["all", "any", "none"]))
def test_property_stack_unstack_roundtrip(ds: xr.Dataset, drop: str) -> None:
    """Kept values survive exactly; dropped rows come back as NaN; with ``drop="all"``
    a full-rank variable is never missing where it had a value."""
    ds = normalise_dataset(ds)
    dims = [str(d) for d in ds.dims]
    users = [v for v in ds.data_vars if set(ds[v].dims) & set(dims)]
    st_ = wire.stack_rows(ds, dims, drop=drop)  # type: ignore[arg-type]
    back = wire.unstack_rows(st_)
    for v in users:
        orig = ds[v]
        got = back[v].transpose(*orig.dims)
        if drop == "none":
            xr.testing.assert_identical(got, orig)
            continue
        assert bool(((got == orig) | got.isnull()).all())
        if drop == "all" and set(orig.dims) == set(dims):
            xr.testing.assert_identical(got, orig)


# ----------------------------------------------------------------------------------- Arrow IPC


def test_arrow_view_roundtrip_and_limits() -> None:
    ds = _panel()
    ds["y"].attrs["units"] = "EUR"
    table = wire.to_arrow_table(ds)
    assert table.num_rows == 6
    assert set(table.column_names) >= {"geo", "time", "y", "label"}
    meta = json.loads(table.schema.metadata[b"mapi.attrs"])
    assert meta["vars"]["y"] == {"units": "EUR"}
    data = wire.write_arrow_ipc(table)
    assert wire.read_arrow_ipc(data).equals(table)
    with pytest.raises(PayloadLimitError):
        wire.to_arrow_table(ds, max_rows=2)
    with pytest.raises(PayloadLimitError):
        wire.write_arrow_ipc(table, max_bytes=10)
    with pytest.raises(PayloadLimitError):
        wire.read_arrow_ipc(data, max_bytes=10)
    with pytest.raises(WireFormatError):
        wire.read_arrow_ipc(b"garbage")
    da = ds["y"]
    assert wire.to_arrow_table(da).num_rows == 6
    scalar = wire.to_arrow_table(xr.Dataset({"s": ((), 1.5)}))
    assert scalar.num_rows == 1


def test_restore_multiindex_rejects_bad_attr() -> None:
    with pytest.raises(WireFormatError):
        wire.restore_multiindex(xr.Dataset(attrs={wire.MULTIINDEX_ATTR: {"z": "bad"}}))


def test_zarr_zip_opens_with_zarr_zipstore(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Our deterministic zips are plain zarr v3 ZipStores (range-readable by the runner)."""
    from zarr.storage import ZipStore

    ds = _panel()
    path = tmp_path / "d.zarr.zip"
    wire.write_zarr_zip(ds, path)
    store = ZipStore(path, mode="r")
    try:
        back = xr.open_zarr(store, zarr_format=3, consolidated=False, chunks=None).load()
    finally:
        store.close()
    # plain zarr readers see the reserved variable-order attribute; ours remove it
    assert back.attrs.pop(wire.VAR_ORDER_ATTR) == [str(v) for v in ds.data_vars]
    xr.testing.assert_identical(normalise_dataset(back.drop_encoding()), normalise_dataset(ds))


def test_variable_order_survives_the_round_trip() -> None:
    """zarr lists members in no fixed order; "first variable" defaults depend on it."""
    names = ["contribution", "spend", "target_scale", "a_last", "current_spend"]
    ds = xr.Dataset({n: ("x", np.arange(3.0)) for n in names}, attrs={"k": 1})
    assert list(wire.read_zarr_zip(wire.write_zarr_zip(ds)).data_vars) == names
    tree = xr.DataTree.from_dict({"/": xr.Dataset(), "/derived/curves": ds})
    back = wire.read_datatree_zip(wire.write_datatree_zip(tree))
    node = back["derived/curves"]
    assert isinstance(node, xr.DataTree)
    assert list(node.to_dataset().data_vars) == names and node.attrs == {"k": 1}
    assert wire.VAR_ORDER_ATTR not in back.attrs
    with pytest.raises(wire.WireFormatError, match="reserved"):
        wire.write_zarr_zip(ds.assign_attrs({wire.VAR_ORDER_ATTR: ["x"]}))
