"""Tree versions in ingest (M4): NetCDF groups become trees (root-only files stay plain
datasets), zarr trees, raw HDF5 mappings with every coord variant, multi-file assemblies
(node mount, concat, merge) and their typed errors, and the outputs of a tree version."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_runner.jobs import IngestJob
from mapi_runner.worker.ingest import build_dataset, build_upload, run_ingest
from mapi_runner.worker.tabular import IngestLimits
from mapi_runner.worker.tree_ingest import TREE_SCHEMA_ATTR, explore_slug
from mapi_sdk.tree import read_tree_zip, write_tree_zip
from mapi_sdk.wire import read_zarr_zip, write_zarr_zip
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import IngestAssembly, IngestMapping
from mapi_spec.jobs import DATASET_OUTPUTS, TREE_DATASET_OUTPUTS, upload_input
from mapi_spec.schema import DataSchema
from mapi_spec.tree import TreeSchema
from mapi_spec.views import ExplorerIndex
from tree_fixtures import (
    WEEKS,
    IngestCtx,
    root_dataset,
    tree_dict,
    write_netcdf_root,
    write_netcdf_tree,
    write_raw_hdf5,
)

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore::FutureWarning")]

NETCDF = IngestMapping(format="netcdf")
LIMITS = IngestLimits()


def _ingest(tmp: Path, inputs: dict[str, Path], job: IngestJob) -> IngestCtx:
    ctx = IngestCtx(tmp / "out", inputs)
    run_ingest(ctx, job)  # type: ignore[arg-type]
    return ctx


def _hdf5(vars_: list[dict[str, Any]], coords: list[dict[str, Any]] | None = None) -> IngestMapping:
    return IngestMapping.model_validate(
        {"format": "hdf5", "hdf5": {"vars": vars_, "coords": coords or []}}
    )


# ============================================================================ NetCDF


def test_netcdf_with_groups_becomes_a_tree_version(tmp_path: Path) -> None:
    ctx = _ingest(
        tmp_path, {"upload": write_netcdf_tree(tmp_path / "t.nc")}, IngestJob(mapping=NETCDF)
    )
    plain = {o for o in ctx.outputs if not o.startswith("ui/")}
    assert plain == set(TREE_DATASET_OUTPUTS) - {"columns.json"}
    schema = TreeSchema.model_validate_json(ctx.outputs["tree_schema.json"])
    assert list(schema.nodes) == ["/", "/instrument", "/instrument/psf"]
    assert set(schema.nodes["/instrument"].vars) == {"temp"}
    assert schema.nodes["/instrument"].dims["time"] == schema.nodes["/"].dims["time"]
    root = DataSchema.model_validate_json(ctx.outputs["schema.json"])
    assert root == schema.root and set(root.vars) == {"sales", "tv"}
    tree = read_tree_zip(ctx.outputs["data.tree.zip"])
    assert TreeSchema.model_validate_json(tree.attrs[TREE_SCHEMA_ATTR]) == schema
    np.testing.assert_allclose(
        tree["instrument/psf"]["kernel"].values, tree_dict()["/instrument/psf"]["kernel"].values
    )
    summary = ctx.json("summary.json")
    assert set(summary["nodes"]) == set(schema.nodes)
    assert summary["vars"] == ["sales", "tv"]  # root fields for readers before M4
    assert ctx.json("profile.json")["node"] == "/" and ctx.json("preview.json")["node"] == "/"
    nodes = ctx.json("ui/explore/nodes/index.json")
    assert nodes["default"] == "/" and set(nodes["nodes"]) == set(schema.nodes)
    for path, entry in nodes["nodes"].items():
        index = ExplorerIndex.model_validate(ctx.json(entry["index"]))
        assert index.node == path and entry["slug"] == explore_slug(path)
        assert all(s.view.startswith(f"ui/explore/nodes/{entry['slug']}/") for s in index.sections)
    assert ExplorerIndex.model_validate(ctx.json("ui/explore/index.json")).node == "/"


def test_root_only_netcdf_stays_a_dataset_version(tmp_path: Path) -> None:
    for engine in ("h5netcdf", "scipy"):
        path = write_netcdf_root(tmp_path / f"{engine}.nc", engine=engine)
        ctx = _ingest(tmp_path / engine, {"upload": path}, IngestJob(mapping=NETCDF))
        assert {o for o in ctx.outputs if not o.startswith("ui/")} == set(DATASET_OUTPUTS) - {
            "columns.json"
        }
        ds = read_zarr_zip(ctx.outputs["data.zarr.zip"])
        assert dict(ds.sizes) == {"time": 8, "geo": 2}
        assert "node" not in ctx.json("profile.json")


def test_mapping_renames_and_selects_per_node(tmp_path: Path) -> None:
    mapping = IngestMapping.model_validate(
        {
            "format": "netcdf",
            "dims": {"week": {"column": "time", "spec": {"kind": "time", "policy": "extend"}}},
            "vars": {
                "revenue": {"column": "sales", "role": "target", "units": "EUR"},
                "temp": {"column": "temp"},
                "kernel": {"column": "kernel", "missing": "allow"},
            },
        }
    )
    up = build_upload(write_netcdf_tree(tmp_path / "t.nc"), mapping, LIMITS)
    assert isinstance(up.schema, TreeSchema)
    root = up.schema.nodes["/"]
    assert set(root.vars) == {"revenue"} and root.vars["revenue"].role.value == "target"
    assert root.vars["revenue"].units == "EUR" and "week" in root.dims
    assert set(up.schema.nodes["/instrument"].vars) == {"temp"}
    assert up.schema.nodes["/instrument/psf"].vars["kernel"].missing == "allow"
    with pytest.raises(MapiError, match="no group of the upload has the variable"):
        build_upload(
            write_netcdf_tree(tmp_path / "u.nc"),
            IngestMapping.model_validate(
                {"format": "netcdf", "vars": {"nope": {"column": "sales"}, "x": {"column": "x"}}}
            ),
            LIMITS,
        )


def test_groups_over_the_node_limit_and_bad_names_are_refused(tmp_path: Path) -> None:
    path = write_netcdf_tree(tmp_path / "t.nc")
    with pytest.raises(MapiError) as exc:
        build_upload(path, NETCDF, IngestLimits(max_nodes=2))
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    bad = tmp_path / "bad.nc"
    xr.DataTree.from_dict(
        {"/": root_dataset(), "/has space": xr.Dataset({"a": ("i", [1.0])})}
    ).to_netcdf(bad, engine="h5netcdf")
    with pytest.raises(MapiError) as exc:
        build_upload(bad, NETCDF, LIMITS)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID and "group name" in exc.value.detail


def test_empty_groups_are_kept_not_dropped(tmp_path: Path) -> None:
    path = tmp_path / "e.nc"
    xr.DataTree.from_dict({"/": root_dataset(), "/empty": xr.Dataset()}).to_netcdf(
        path, engine="h5netcdf"
    )
    up = build_upload(path, NETCDF, LIMITS)
    assert isinstance(up.schema, TreeSchema) and "/empty" in up.schema.nodes


def test_build_dataset_refuses_trees_but_keeps_plain_uploads(tmp_path: Path) -> None:
    with pytest.raises(MapiError, match="tree dataset"):
        build_dataset(write_netcdf_tree(tmp_path / "t.nc"), IngestJob(mapping=NETCDF))
    ds, _schema, cols = build_dataset(
        write_netcdf_root(tmp_path / "r.nc"), IngestJob(mapping=NETCDF)
    )
    assert "sales" in ds and cols is None


# ============================================================================== zarr


def test_zarr_tree_upload_keeps_its_stored_schema(tmp_path: Path) -> None:
    tree = xr.DataTree.from_dict(tree_dict())
    path = tmp_path / "t.zarr.zip"
    path.write_bytes(write_tree_zip(tree) or b"")
    up = build_upload(path, IngestMapping(format="zarr_zip"), LIMITS)
    assert isinstance(up.data, xr.DataTree) and isinstance(up.schema, TreeSchema)
    assert set(up.schema.nodes) == {"/", "/instrument", "/instrument/psf"}
    plain = tmp_path / "p.zarr.zip"
    plain.write_bytes(write_zarr_zip(root_dataset()) or b"")
    assert isinstance(
        build_upload(plain, IngestMapping(format="zarr_zip"), LIMITS).data, xr.Dataset
    )


# ========================================================================== raw HDF5


def test_raw_hdf5_mapping_with_every_coord_variant(tmp_path: Path) -> None:
    mapping = _hdf5(
        [
            {"path": "/observations/image", "name": "image", "dims": ["row", "col"],
             "units": "counts"},
            {"path": "/psf/kernel", "node": "/psf", "name": "kernel", "dims": ["prow", "pcol"]},
            {"path": "/observations/flux", "node": "/series", "name": "flux", "dims": ["date"],
             "role": "target"},
        ],
        [
            {"dim": "row", "path": "/axes/rows"},
            {"dim": "col", "start": 0.0, "step": 0.5},
            {"dim": "date", "node": "/series", "start": "2024-01-01", "step": "W-MON",
             "spec": {"kind": "time", "policy": "extend", "freq": "W-MON"}},
            {"dim": "prow", "node": "/psf"},
            {"dim": "pcol", "node": "/psf", "start": 10, "step": 2},
        ],
    )  # fmt: skip
    ctx = _ingest(
        tmp_path, {"upload": write_raw_hdf5(tmp_path / "raw.h5")}, IngestJob(mapping=mapping)
    )
    tree = read_tree_zip(ctx.outputs["data.tree.zip"])
    root = tree.to_dataset()
    np.testing.assert_array_equal(root["row"].values, [10.0, 20.0, 30.0, 40.0])
    np.testing.assert_array_equal(root["col"].values, [0.0, 0.5, 1.0, 1.5, 2.0])
    psf = tree["psf"].to_dataset()
    assert psf["prow"].values.tolist() == [0, 1, 2] and psf["pcol"].values.tolist() == [10, 12, 14]
    series = tree["series"].to_dataset()
    assert pd.DatetimeIndex(series["date"].values).equals(
        pd.date_range("2024-01-01", periods=6, freq="W-MON")
    )
    schema = TreeSchema.model_validate_json(ctx.outputs["tree_schema.json"])
    assert schema.nodes["/psf"].dims["prow"].kind == "index"
    assert schema.nodes["/series"].dims["date"].freq == "W-MON"
    assert schema.nodes["/series"].vars["flux"].role.value == "target"
    assert schema.nodes["/"].vars["image"].units == "counts"
    warned = [w for w in ctx.warnings if "not mapped" in w[1]]
    assert len(warned) == 1 and warned[0][2] is not None
    assert "/spare/unused" in warned[0][2] and "/axes/rows" not in warned[0][2]


def test_raw_hdf5_all_in_the_root_is_a_plain_dataset(tmp_path: Path) -> None:
    mapping = _hdf5([{"path": "/observations/flux", "name": "flux", "dims": ["i"]}])
    up = build_upload(write_raw_hdf5(tmp_path / "raw.h5"), mapping, LIMITS)
    assert isinstance(up.data, xr.Dataset) and isinstance(up.schema, DataSchema)
    assert up.schema.dims["i"].kind == "index"


def test_raw_hdf5_text_labels_and_dates_from_a_path(tmp_path: Path) -> None:
    path = tmp_path / "labels.h5"
    with h5py.File(path, "w") as f:
        f["v"] = np.arange(3.0)
        f["names"] = np.array(["a", "b", "c"], dtype=h5py.string_dtype())
        f["days"] = np.array(["2024-01-01", "2024-01-02", "2024-01-03"], dtype=h5py.string_dtype())
    up = build_upload(path, _hdf5([{"path": "/v", "name": "v", "dims": ["k"]}],
                                  [{"dim": "k", "path": "/names"}]), LIMITS)  # fmt: skip
    assert isinstance(up.data, xr.Dataset) and up.data["k"].values.tolist() == ["a", "b", "c"]
    up = build_upload(
        path,
        _hdf5(
            [{"path": "/v", "name": "v", "dims": ["day"]}],
            [{"dim": "day", "path": "/days", "spec": {"kind": "time", "policy": "extend"}}],
        ),
        LIMITS,
    )
    assert isinstance(up.data, xr.Dataset) and up.data["day"].dtype.kind == "M"


@pytest.mark.parametrize(
    ("vars_", "coords", "pointer", "message"),
    [
        ([{"path": "/observations/image", "name": "image", "dims": ["row"]}], [],
         "/mapping/hdf5/vars/0/dims", "names 1 dims"),
        ([{"path": "/nowhere", "name": "x", "dims": ["i"]}], [],
         "/mapping/hdf5/vars/0/path", "not in the file"),
        ([{"path": "/observations", "name": "x", "dims": ["i"]}], [],
         "/mapping/hdf5/vars/0/path", "group, not an array"),
        ([{"path": "/observations/image", "name": "image", "dims": ["row", "col"]},
          {"path": "/observations/flux", "name": "flux", "dims": ["row"]}], [],
         "/mapping/hdf5/vars/1/dims", "has 4 labels"),
        ([{"path": "/observations/flux", "name": "flux", "dims": ["row"]}],
         [{"dim": "row", "path": "/axes/rows"}], "/mapping/hdf5/coords/0/path", "has shape"),
        ([{"path": "/observations/flux", "name": "flux", "dims": ["d"]}],
         [{"dim": "d", "start": "2024-01-01", "step": "not-a-freq"}],
         "/mapping/hdf5/coords/0", "date range"),
    ],
)  # fmt: skip
def test_raw_hdf5_mapping_errors_are_typed(
    tmp_path: Path,
    vars_: list[dict[str, Any]],
    coords: list[dict[str, Any]],
    pointer: str,
    message: str,
) -> None:
    with pytest.raises(MapiError) as exc:
        build_upload(write_raw_hdf5(tmp_path / "raw.h5"), _hdf5(vars_, coords), LIMITS)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    assert exc.value.pointer == pointer and message in exc.value.detail


def test_raw_hdf5_child_dim_must_match_its_ancestor(tmp_path: Path) -> None:
    mapping = _hdf5(
        [
            {"path": "/observations/image", "name": "image", "dims": ["row", "col"]},
            {"path": "/observations/flux", "node": "/child", "name": "flux", "dims": ["row"]},
        ]
    )
    with pytest.raises(MapiError, match="its ancestor"):
        build_upload(write_raw_hdf5(tmp_path / "raw.h5"), mapping, LIMITS)


def test_format_must_match_the_bytes(tmp_path: Path) -> None:
    csv = tmp_path / "x.csv"
    csv.write_text("a,b\n1,2\n")
    with pytest.raises(MapiError, match="not a hdf5 file"):
        build_upload(csv, _hdf5([{"path": "/x", "name": "x", "dims": ["i"]}]), LIMITS)


# =========================================================================== assembly


def _year(tmp: Path, year: int, *, extra: bool = False, shift: float = 0.0) -> Path:
    ds = root_dataset() + shift
    if extra:
        ds["promo"] = ds["tv"] * 0
    path = tmp / f"y{year}.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    return path


def _assembly(sources: list[dict[str, Any]]) -> IngestJob:
    return IngestJob(assembly=IngestAssembly.model_validate({"sources": sources}))


def _src(i: int, **kw: Any) -> dict[str, Any]:
    return {"upload_id": f"upl_{i:026d}", "mapping": {"format": "netcdf"}, **kw}


def test_concat_along_a_new_dim_stays_a_plain_dataset(tmp_path: Path) -> None:
    job = _assembly(
        [
            _src(0, combine="concat", concat_dim="year", concat_label=2024),
            _src(1, combine="concat", concat_dim="year", concat_label=2025),
        ]
    )
    inputs = {
        upload_input(0): _year(tmp_path, 2024),
        upload_input(1): _year(tmp_path, 2025, shift=1),
    }
    ctx = _ingest(tmp_path, inputs, job)
    ds = read_zarr_zip(ctx.outputs["data.zarr.zip"])
    assert ds["year"].values.tolist() == [2024, 2025] and ds["sales"].dims[0] == "year"
    np.testing.assert_allclose(ds["sales"].sel(year=2025) - ds["sales"].sel(year=2024), 1.0)
    schema = DataSchema.model_validate_json(ctx.outputs["schema.json"])
    assert schema.dims["year"].kind == "integer" and schema.dims["time"].freq == "W-MON"


def test_node_mount_including_a_tree_file_under_a_node(tmp_path: Path) -> None:
    job = _assembly([_src(0), _src(1, node="/archive")])
    inputs = {
        upload_input(0): write_netcdf_root(tmp_path / "root.nc"),
        upload_input(1): write_netcdf_tree(tmp_path / "tree.nc"),
    }
    ctx = _ingest(tmp_path, inputs, job)
    schema = TreeSchema.model_validate_json(ctx.outputs["tree_schema.json"])
    assert list(schema.nodes) == ["/", "/archive", "/archive/instrument", "/archive/instrument/psf"]
    tree = read_tree_zip(ctx.outputs["data.tree.zip"])
    assert "kernel" in tree["archive/instrument/psf"].to_dataset()


def test_merge_with_joins(tmp_path: Path) -> None:
    a = root_dataset()[["sales"]]
    b = root_dataset()[["tv"]].isel(time=slice(2, None))
    pa, pb = tmp_path / "a.nc", tmp_path / "b.nc"
    a.to_netcdf(pa, engine="h5netcdf")
    b.to_netcdf(pb, engine="h5netcdf")
    inputs = {upload_input(0): pa, upload_input(1): pb}

    def merged(join: str) -> Any:
        sources = [
            _src(0, node="/m", combine="merge", join=join),
            _src(1, node="/m", combine="merge", join=join),
        ]
        return build_assembly(tmp_path, inputs, sources)

    with pytest.raises(MapiError) as exc:
        merged("exact")
    assert exc.value.code is ErrorCode.SCHEMA_INVALID and exc.value.pointer.startswith("/assembly")
    inner = merged("inner")
    assert inner.sizes["time"] == 6
    outer = merged("outer")
    assert outer.sizes["time"] == 8 and bool(outer["tv"].isnull().any())


def build_assembly(tmp: Path, inputs: dict[str, Path], sources: list[dict[str, Any]]) -> xr.Dataset:
    ctx = _ingest(tmp / f"run{len(list(tmp.iterdir()))}", inputs, _assembly(sources))
    tree = read_tree_zip(ctx.outputs["data.tree.zip"])
    schema = TreeSchema.model_validate_json(ctx.outputs["tree_schema.json"])
    node = tree["m"].to_dataset()
    if bool(node["tv"].isnull().any()):
        assert schema.nodes["/m"].vars["tv"].missing == "allow"
    return node


def test_merge_conflicting_values_is_an_error(tmp_path: Path) -> None:
    a = root_dataset()
    b = root_dataset() + 1  # the same variables with other values
    pa, pb = tmp_path / "a.nc", tmp_path / "b.nc"
    a.to_netcdf(pa, engine="h5netcdf")
    b.to_netcdf(pb, engine="h5netcdf")
    with pytest.raises(MapiError, match="conflict"):
        _ingest(
            tmp_path,
            {upload_input(0): pa, upload_input(1): pb},
            _assembly([_src(0, combine="merge"), _src(1, combine="merge")]),
        )


def test_concat_of_files_with_different_variables_is_an_error(tmp_path: Path) -> None:
    job = _assembly(
        [
            _src(0, combine="concat", concat_dim="year", concat_label="a"),
            _src(1, combine="concat", concat_dim="year", concat_label="b"),
        ]
    )
    inputs = {upload_input(0): _year(tmp_path, 1), upload_input(1): _year(tmp_path, 2, extra=True)}
    with pytest.raises(MapiError) as exc:
        _ingest(tmp_path, inputs, job)
    assert "same variables" in exc.value.detail and exc.value.pointer == "/assembly/sources/1"


def test_concat_dim_already_in_the_file_is_an_error(tmp_path: Path) -> None:
    job = _assembly(
        [
            _src(0, combine="concat", concat_dim="geo", concat_label=1),
            _src(1, combine="concat", concat_dim="geo", concat_label=2),
        ]
    )
    inputs = {upload_input(0): _year(tmp_path, 1), upload_input(1): _year(tmp_path, 2)}
    with pytest.raises(MapiError) as exc:
        _ingest(tmp_path, inputs, job)
    assert exc.value.pointer == "/assembly/sources/0/concat_dim"


def test_two_sources_writing_one_node_is_an_error(tmp_path: Path) -> None:
    job = _assembly([_src(0), _src(1, node="/instrument")])
    inputs = {
        upload_input(0): write_netcdf_tree(tmp_path / "tree.nc"),
        upload_input(1): write_netcdf_root(tmp_path / "root.nc"),
    }
    with pytest.raises(MapiError, match="would get data from sources"):
        _ingest(tmp_path, inputs, job)


def test_source_errors_point_into_the_assembly(tmp_path: Path) -> None:
    bad = {"upload_id": "upl_" + "1" * 26, "mapping": {"format": "zarr_zip"}}
    job = _assembly([_src(0, node="/a"), {**bad, "node": "/b"}])
    inputs = {
        upload_input(0): write_netcdf_root(tmp_path / "root.nc"),
        upload_input(1): write_netcdf_root(tmp_path / "other.nc"),
    }
    with pytest.raises(MapiError) as exc:
        _ingest(tmp_path, inputs, job)
    assert exc.value.pointer == "/assembly/sources/1/mapping/format"


def test_assembly_limits_apply_across_sources(tmp_path: Path) -> None:
    sources = [_src(0, node="/a"), _src(1, node="/b")]
    inputs = {
        upload_input(0): write_netcdf_root(tmp_path / "a.nc"),
        upload_input(1): write_netcdf_root(tmp_path / "b.nc"),
    }
    one = int(root_dataset().nbytes)
    job = _assembly(sources).model_copy(update={"max_uncompressed_bytes": one + one // 2})
    with pytest.raises(MapiError) as exc:
        _ingest(tmp_path, inputs, job)
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    with pytest.raises(MapiError) as exc:
        _ingest(tmp_path / "n", inputs, _assembly(sources).model_copy(update={"max_nodes": 2}))
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_csv_sources_combine_and_keep_their_column_profiles(tmp_path: Path) -> None:
    paths = {}
    for i, region in enumerate(("north", "south")):
        p = tmp_path / f"{region}.csv"
        pd.DataFrame({"week": WEEKS.strftime("%Y-%m-%d"), "sales": np.arange(8.0) + i}).to_csv(
            p, index=False
        )
        paths[upload_input(i)] = p
    mapping = {
        "format": "csv",
        "dims": {"week": {"column": "week", "spec": {"kind": "time", "policy": "extend"}}},
        "vars": {"sales": {"column": "sales", "role": "target"}},
    }
    job = _assembly(
        [
            {"upload_id": f"upl_{i:026d}", "mapping": mapping, "combine": "concat",
             "concat_dim": "region", "concat_label": region}
            for i, region in enumerate(("north", "south"))
        ]
    )  # fmt: skip
    ctx = _ingest(tmp_path, paths, job)
    ds = read_zarr_zip(ctx.outputs["data.zarr.zip"])
    assert ds["region"].values.tolist() == ["north", "south"]
    schema = DataSchema.model_validate_json(ctx.outputs["schema.json"])
    assert (
        schema.vars["sales"].role.value == "target" and schema.dims["region"].kind == "categorical"
    )
    assert "columns.json" in ctx.outputs


def test_ingest_limits_read_the_m4_fields() -> None:
    job = IngestJob(
        mapping=NETCDF,
        max_nodes=10,
        max_sources=3,
        max_hdf5_objects=99,
        max_ndim=4,
        max_attr_bytes=128,
    )
    lim = IngestLimits.from_job(job)
    got = (lim.max_nodes, lim.max_sources, lim.max_hdf5_objects, lim.max_ndim, lim.max_attr_bytes)
    assert got == (10, 3, 99, 4, 128)
