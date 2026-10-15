"""Regression tests for the M0 review findings on the wire readers (hostile payloads)."""

from __future__ import annotations

import pytest
from mapi_sdk import wire
from mapi_sdk.errors import PayloadLimitError, WireFormatError

# ------------------------------------------------- quality-security-3 (string amplification)


def test_xrjson_strings_are_budgeted_by_decoded_bytes() -> None:
    """One long string among many short ones would decode to a huge fixed-width array."""
    leaves = ["a"] * 20_000 + ["b" * 4096]
    payload = {"dims": {"i": len(leaves)}, "data_vars": {"s": {"dims": ["i"], "data": leaves}}}
    with pytest.raises(PayloadLimitError, match="decode"):
        wire.from_xrjson(payload)
    # ordinary label payloads are untouched
    ok = {"data_vars": {"s": {"dims": ["i"], "data": ["north", "south"] * 1000}}}
    assert wire.from_xrjson(ok)["s"].dtype.kind == "U"


# --------------------------------------- quality-security-12 (declared dims, special dates)


def test_xrjson_caps_declared_dim_sizes() -> None:
    payload = {"dims": {"a": 0, "b": 10**15}, "data_vars": {"v": {"dims": ["a", "b"], "data": []}}}
    with pytest.raises(PayloadLimitError, match="'b'"):
        wire.from_xrjson(payload)


@pytest.mark.parametrize("value", ["now", "today", "2024-01-07 junk", "1e9"])
def test_xrjson_datetimes_must_be_iso_8601(value: str) -> None:
    payload = {"coords": {"t": {"dims": ["t"], "data": [value], "dtype": "datetime64[ns]"}}}
    with pytest.raises(WireFormatError, match="ISO-8601"):
        wire.from_xrjson(payload)


def _bomb_zip(arrays: int, length: int) -> bytes:
    """A tiny zip whose metadata declares ``arrays`` float64 arrays of ``length`` with no
    chunks (zarr fills missing chunks, so no data is needed to allocate them)."""
    import io
    import json
    import zipfile

    import numpy as np
    import xarray as xr

    good = wire.write_zarr_zip(xr.Dataset({"y": (("a",), np.array([1.0, 2.0]))}))
    assert good is not None
    entries = wire.zip_entries(good)
    meta = json.loads(entries["y/zarr.json"])
    meta.update(shape=[length], dimension_names=[f"d{0}"])
    meta["chunk_grid"]["configuration"]["chunk_shape"] = [1 << 20]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zarr.json", entries["zarr.json"])
        for k in range(arrays):
            m = {**meta, "dimension_names": [f"d{k}"]}
            zf.writestr(f"v{k}/zarr.json", json.dumps(m))
    return buf.getvalue()


def test_zarr_zip_rejects_declared_data_far_beyond_the_archive() -> None:
    bomb = _bomb_zip(8, 1 << 28)  # 2**31 float64 elements (16 GiB) from a few KB
    assert len(bomb) < 20_000
    with pytest.raises(PayloadLimitError):
        wire.read_zarr_zip(bomb)
    one = _bomb_zip(1, 1 << 26)  # "only" 512 MiB, still ~100000x the archive
    with pytest.raises(PayloadLimitError, match="archive"):
        wire.read_zarr_zip(one)


def test_zarr_zip_enforces_max_string_chars() -> None:
    import numpy as np
    import xarray as xr

    data = wire.write_zarr_zip(xr.Dataset({"s": (("a",), np.array(["x" * 100_000]))}))
    assert data is not None and len(data) < 5_000  # compresses extremely well
    with pytest.raises(PayloadLimitError, match="characters"):
        wire.read_zarr_zip(data)
    tree = wire.write_datatree_zip(
        xr.DataTree.from_dict({"/g": xr.Dataset({"s": ("a", ["x" * 5000])})})
    )
    assert tree is not None
    with pytest.raises(PayloadLimitError, match="characters"):
        wire.read_datatree_zip(tree)


def test_xrjson_iso_datetimes_still_decode() -> None:
    values = ["2024-01-07", "2024-01-14T00:00:00", "2024-01-21T00:00:00.000000000", None]
    payload = {"coords": {"t": {"dims": ["t"], "data": values, "dtype": "datetime64[ns]"}}}
    t = wire.from_xrjson(payload)["t"].values
    assert str(t[2])[:10] == "2024-01-21" and str(t[3]) == "NaT"
