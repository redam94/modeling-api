"""``inspect`` runs (M4): an upload's groups and arrays from metadata only, within every
:class:`~mapi_spec.ingest.UploadInspection` cap."""

from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_runner.worker.inspection import inspect_upload, run_inspect
from mapi_sdk.tree import write_tree_zip
from mapi_spec.ingest import (
    MAX_INSPECT_ATTR_CHARS,
    MAX_INSPECT_ATTRS,
    MAX_INSPECTION_BYTES,
    UploadInspection,
)
from mapi_spec.jobs import INSPECT_OUTPUT, InspectJob
from tree_fixtures import IngestCtx, tree_dict, write_netcdf_root, write_netcdf_tree, write_raw_hdf5

pytestmark = pytest.mark.unit


def test_raw_hdf5_lists_groups_and_arrays(tmp_path: Path) -> None:
    out = inspect_upload(write_raw_hdf5(tmp_path / "raw.h5"), InspectJob())
    assert out.format == "hdf5" and not out.truncated
    assert set(out.groups) == {"/", "/observations", "/psf", "/axes", "/spare"}
    image = next(a for a in out.arrays if a.path == "/observations/image")
    assert image.shape == [4, 5] and image.dtype == "float64" and image.dims_hint is None
    labels = next(a for a in out.arrays if a.path == "/axes/labels")
    assert labels.dtype == "str"


def test_netcdf_tree_gets_dim_hints_and_format(tmp_path: Path) -> None:
    out = inspect_upload(write_netcdf_tree(tmp_path / "t.nc"), InspectJob())
    assert out.format == "netcdf"
    assert out.groups == ["/", "/instrument", "/instrument/psf"]
    by_path = {a.path: a for a in out.arrays}
    assert by_path["/sales"].dims_hint == ["time", "geo"]
    assert by_path["/instrument/temp"].dims_hint == ["time"]
    assert by_path["/instrument/psf/kernel"].dims_hint == ["prow", "pcol"]
    # netCDF bookkeeping attributes are left out
    assert not any(
        k.startswith("_Netcdf4") or k in ("CLASS", "NAME") for a in out.arrays for k in a.attrs
    )


def test_netcdf3_and_zarr_and_parquet(tmp_path: Path) -> None:
    cdf = inspect_upload(write_netcdf_root(tmp_path / "c.nc", engine="scipy"), InspectJob())
    assert cdf.format == "netcdf" and {a.path for a in cdf.arrays} >= {"/sales", "/time"}
    assert next(a for a in cdf.arrays if a.path == "/sales").dims_hint == ["time", "geo"]
    zpath = tmp_path / "t.zarr.zip"
    zpath.write_bytes(write_tree_zip(xr.DataTree.from_dict(tree_dict())) or b"")
    z = inspect_upload(zpath, InspectJob())
    assert z.format == "zarr_zip" and "/instrument/psf" in z.groups
    kernel = next(a for a in z.arrays if a.path == "/instrument/psf/kernel")
    assert kernel.shape == [3, 3] and kernel.dims_hint == ["prow", "pcol"] and kernel.filters
    pq = tmp_path / "t.parquet"
    pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}).to_parquet(pq)
    p = inspect_upload(pq, InspectJob())
    assert p.format == "parquet" and [a.path for a in p.arrays] == ["/a", "/b"]
    assert p.arrays[0].shape == [3]
    csv = tmp_path / "t.csv"
    csv.write_text("a,b\n1,2\n")
    c = inspect_upload(csv, InspectJob())
    assert c.format == "csv" and not c.arrays and c.warnings


def test_arrays_past_the_cap_are_truncated(tmp_path: Path) -> None:
    path = tmp_path / "many.h5"
    with h5py.File(path, "w") as f:
        for i in range(30):
            f[f"x{i:02d}"] = np.arange(2.0)
    out = inspect_upload(path, InspectJob(max_arrays=10))
    assert len(out.arrays) == 10 and out.truncated
    assert any("stops after 10 arrays" in w for w in out.warnings)


def test_attributes_are_capped(tmp_path: Path) -> None:
    path = tmp_path / "attrs.h5"
    with h5py.File(path, "w") as f:
        f["x"] = np.arange(2.0)
        for i in range(MAX_INSPECT_ATTRS + 20):
            f["x"].attrs[f"a{i:03d}"] = i
        f["y"] = np.arange(2.0)
        f["y"].attrs["long"] = "z" * 5000
        f["y"].attrs["vector"] = np.arange(5)
        f["y"].attrs["nan"] = float("nan")
    out = inspect_upload(path, InspectJob())
    x = next(a for a in out.arrays if a.path == "/x")
    y = next(a for a in out.arrays if a.path == "/y")
    assert len(x.attrs) == MAX_INSPECT_ATTRS
    assert len(str(y.attrs["long"])) == MAX_INSPECT_ATTR_CHARS
    assert "vector" not in y.attrs and y.attrs["nan"] is None
    json.loads(out.model_dump_json())  # strict JSON (no NaN)


def test_run_inspect_writes_the_output(tmp_path: Path) -> None:
    ctx = IngestCtx(tmp_path / "out", {"upload": write_raw_hdf5(tmp_path / "raw.h5")})
    run_inspect(ctx, InspectJob())  # type: ignore[arg-type]
    doc = UploadInspection.model_validate_json(ctx.outputs[INSPECT_OUTPUT])
    assert doc.size_bytes == (tmp_path / "raw.h5").stat().st_size
    assert list(ctx.outputs) == [INSPECT_OUTPUT]


def test_the_whole_document_fits_the_byte_cap(tmp_path: Path) -> None:
    path = tmp_path / "fat.h5"
    text = "v" * (MAX_INSPECT_ATTR_CHARS - 10)
    with h5py.File(path, "w", libver="latest") as f:
        for i in range(140):
            ds = f.create_dataset(f"x{i:03d}", data=np.arange(2.0))
            for k in range(MAX_INSPECT_ATTRS):
                ds.attrs[f"a{k:02d}"] = text
    out = inspect_upload(path, InspectJob())
    assert len(out.model_dump_json().encode()) <= MAX_INSPECTION_BYTES
    assert len(out.arrays) == 140 and not any(a.attrs for a in out.arrays)
    assert any("attributes were left out" in w for w in out.warnings)
