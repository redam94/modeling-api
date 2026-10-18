"""Review fixes of the M4 runner (tree datasets, HDF5): an HDF5 superblock behind a user
block is always guarded, root-only NetCDF keeps ingesting as before M4, raw HDF5 text and
date ranges are checked before they mislead or exhaust memory, assemblies align child
nodes by label and keep per-node layouts and non-index coords, long node paths get short
explorer slugs, inspect skips malformed zarr metadata, and tree-input models get the tree
in the explorer, governance and workflow scoring paths."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_runner.jobs import IngestJob
from mapi_runner.worker import tree_ingest
from mapi_runner.worker.data import load_data_and_tree
from mapi_runner.worker.explore_model import ModelContext
from mapi_runner.worker.governance import GovernanceEnv
from mapi_runner.worker.ingest import build_dataset, build_upload, run_ingest
from mapi_runner.worker.inspection import inspect_upload
from mapi_runner.worker.refit import Refitter
from mapi_runner.worker.tabular import IngestLimits, hdf5_superblock_offset
from mapi_runner.worker.tree_ingest import MAX_EXPLORE_SLUG, explore_slug, tree_from_nodes
from mapi_sdk.harness import fit_spec
from mapi_sdk.models import FitContext, FitOutput, Fitted, ScoreContext
from mapi_sdk.testing import toy
from mapi_sdk.tree import node_dataset, read_tree_zip, select_nodes, write_tree_zip
from mapi_spec.errors import ErrorCode, MapiError, UnsafeUpload
from mapi_spec.ingest import IngestAssembly, IngestMapping
from mapi_spec.jobs import InspectJob, upload_input
from mapi_spec.loader import load_model_spec
from mapi_spec.spec import FitConfig, ScoreRequest
from mapi_spec.tree import NodeSelect, TreeSchema
from tree_fixtures import IngestCtx, root_dataset

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore::FutureWarning")]

NETCDF = IngestMapping(format="netcdf")
CDF_PREFIX = b"CDF\x09" + b"\xff" * 12


def _userblock_file(path: Path, fill: Any) -> Path:
    """An HDF5 file behind a 512-byte user block whose first bytes look like NetCDF-3."""
    with h5py.File(path, "w", userblock_size=512) as f:
        f["x"] = np.arange(5.0)
        fill(f)
    with open(path, "r+b") as fh:
        fh.write(CDF_PREFIX)
    return path


# ============================================================== user-block HDF5 bypass


def test_superblock_offset_is_found_behind_a_user_block(tmp_path: Path) -> None:
    plain = tmp_path / "plain.h5"
    with h5py.File(plain, "w") as f:
        f["x"] = np.arange(3.0)
    assert hdf5_superblock_offset(plain) == 0
    hidden = _userblock_file(tmp_path / "ub.nc", lambda f: None)
    assert hdf5_superblock_offset(hidden) == 512
    text = tmp_path / "t.csv"
    text.write_text("a,b\n1,2\n" * 200)
    assert hdf5_superblock_offset(text) is None


def test_external_link_behind_a_user_block_is_refused(tmp_path: Path) -> None:
    secret = tmp_path / "secret.h5"
    with h5py.File(secret, "w") as f:
        f["x"] = np.arange(5.0) * 1000 + 7

    def link(f: h5py.File) -> None:
        f["leak"] = h5py.ExternalLink(str(secret), "/x")

    path = _userblock_file(tmp_path / "ub_ext.nc", link)
    with pytest.raises(UnsafeUpload) as exc:
        build_upload(path, NETCDF, IngestLimits())
    assert exc.value.code is ErrorCode.INGEST_UNSAFE_FILE
    with pytest.raises(UnsafeUpload):
        inspect_upload(path, InspectJob())


def test_external_raw_storage_behind_a_user_block_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"top secret bytes" * 30)

    def external(f: h5py.File) -> None:
        f.create_dataset("leak", shape=(400,), dtype="u1", external=[(str(outside), 0, 400)])

    path = _userblock_file(tmp_path / "ub_raw.nc", external)
    with pytest.raises(UnsafeUpload, match="stored in another file"):
        build_upload(path, NETCDF, IngestLimits())


def test_a_harmless_user_block_file_is_read_after_the_guard(tmp_path: Path) -> None:
    path = _userblock_file(tmp_path / "ub_ok.nc", lambda f: None)
    up = build_upload(path, NETCDF, IngestLimits())
    assert isinstance(up.data, xr.Dataset) and "x" in up.data


def test_a_non_hdf5_netcdf_is_never_opened_by_an_hdf5_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bad.nc"
    path.write_bytes(CDF_PREFIX + b"\x00" * 2048)  # NetCDF-3 magic, unreadable
    engines: list[str] = []
    real = xr.open_dataset

    def spy(*args: Any, **kw: Any) -> Any:
        engines.append(kw.get("engine", ""))
        return real(*args, **kw)

    monkeypatch.setattr(xr, "open_dataset", spy)
    with pytest.raises(MapiError) as exc:
        build_upload(path, NETCDF, IngestLimits())
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    assert engines == ["scipy"]


# ===================================================== root-only NetCDF back-compat


def test_root_only_netcdf_with_a_long_history_attribute_ingests(tmp_path: Path) -> None:
    ds = root_dataset()
    ds.attrs["history"] = "x" * 100_000
    path = tmp_path / "bigattr.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    out, schema, _ = build_dataset(path, IngestJob(mapping=NETCDF))
    assert set(out.data_vars) == {"sales", "tv"} and schema.vars
    # raw HDF5 (format hdf5) keeps the plan's strict attribute cap
    raw = tmp_path / "raw.h5"
    with h5py.File(raw, "w") as f:
        f["x"] = np.arange(3.0)
        f.attrs["history"] = "x" * 100_000
    mapping = IngestMapping.model_validate(
        {"format": "hdf5", "hdf5": {"vars": [{"path": "/x", "name": "x", "dims": ["n"]}]}}
    )
    with pytest.raises(UnsafeUpload):
        build_upload(raw, mapping, IngestLimits())


def test_root_only_netcdf_with_many_cf_attributes_ingests(tmp_path: Path) -> None:
    n = 1050  # x 10 attributes: more than max_hdf5_objects (10,000) attributes in all
    ds = xr.Dataset(
        {
            f"v{i:04d}": (
                ("time",),
                np.arange(4.0),
                {f"a{k}": f"value {k}" for k in range(10)},
            )
            for i in range(n)
        },
        coords={"time": pd.date_range("2024-01-07", periods=4, freq="W-SUN")},
    )
    path = tmp_path / "cf.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    out, _, _ = build_dataset(path, IngestJob(mapping=NETCDF))
    assert len(out.data_vars) == n


# ============================================================== raw HDF5 text and dates


def test_variable_length_text_is_budgeted_before_decoding(tmp_path: Path) -> None:
    path = tmp_path / "text.h5"
    values = np.array([""] * 2000, dtype=object)
    values[0] = "y" * 4096
    with h5py.File(path, "w") as f:
        f.create_dataset("labels", data=values, dtype=h5py.string_dtype(), compression="gzip")
    mapping = IngestMapping.model_validate(
        {"format": "hdf5", "hdf5": {"vars": [{"path": "/labels", "name": "label", "dims": ["n"]}]}}
    )
    # 2,000 x 4,096 x 4 bytes = 32 MB as fixed-width text: over a 10 MB budget
    limits = IngestLimits(max_uncompressed_bytes=10_000_000, ratio_floor_bytes=1)
    with pytest.raises(MapiError) as exc:
        build_upload(path, mapping, limits)
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    assert exc.value.pointer == "/mapping/hdf5/vars/0/path"
    ok = build_upload(path, mapping, IngestLimits())
    assert isinstance(ok.data, xr.Dataset) and ok.data["label"].values[0] == "y" * 4096


@pytest.mark.parametrize(
    ("start", "freq", "first"),
    [("2024-01-01", "W-SUN", None), ("2024-01-15", "MS", None), ("2024-01-07", "W-SUN", 1)],
)
def test_date_ranges_start_at_their_start(
    tmp_path: Path, start: str, freq: str, first: int | None
) -> None:
    path = tmp_path / "r.h5"
    with h5py.File(path, "w") as f:
        f["x"] = np.arange(3.0)
    mapping = IngestMapping.model_validate(
        {
            "format": "hdf5",
            "hdf5": {
                "vars": [{"path": "/x", "name": "x", "dims": ["t"]}],
                "coords": [{"dim": "t", "start": start, "step": freq}],
            },
        }
    )
    if first is None:  # off the frequency's anchor: refused, never shifted silently
        with pytest.raises(MapiError) as exc:
            build_upload(path, mapping, IngestLimits())
        assert exc.value.code is ErrorCode.SCHEMA_INVALID
        assert exc.value.pointer == "/mapping/hdf5/coords/0/start"
        return
    up = build_upload(path, mapping, IngestLimits())
    assert isinstance(up.data, xr.Dataset)
    assert str(up.data["t"].values[0])[:10] == start


# ===================================================================== assemblies


def _src(i: int, **kw: Any) -> dict[str, Any]:
    return {"upload_id": f"upl_{i:026d}", "mapping": {"format": "netcdf"}, **kw}


def _ingest(tmp: Path, inputs: dict[str, Path], sources: list[dict[str, Any]]) -> IngestCtx:
    ctx = IngestCtx(tmp, inputs)
    job = IngestJob(assembly=IngestAssembly.model_validate({"sources": sources}))
    run_ingest(ctx, job)  # type: ignore[arg-type]
    return ctx


@pytest.mark.parametrize("join", ["inner", "outer"])
def test_concat_aligns_child_nodes_by_label(tmp_path: Path, join: str) -> None:
    weeks = pd.date_range("2024-01-07", periods=4, freq="W-SUN")

    def write(path: Path, order: list[int], sales: list[float]) -> Path:
        vals = np.asarray(sales)
        xr.DataTree.from_dict(
            {
                "/": xr.Dataset({"sales": ("week", vals)}, coords={"week": weeks[order]}),
                "/promo": xr.Dataset({"tv": ("week", vals * 10)}),
            }
        ).to_netcdf(path, engine="h5netcdf")
        return path

    inputs = {
        upload_input(0): write(tmp_path / "a.nc", [0, 1, 2, 3], [1.0, 2.0, 3.0, 4.0]),
        upload_input(1): write(tmp_path / "b.nc", [1, 0, 2, 3], [20.0, 10.0, 30.0, 40.0]),
    }
    sources = [
        _src(i, combine="concat", concat_dim="store", concat_label=lab, join=join)
        for i, lab in enumerate("ab")
    ]
    ctx = _ingest(tmp_path / "out", inputs, sources)
    tree = read_tree_zip(ctx.outputs["data.tree.zip"])
    sales = node_dataset(tree, "/")["sales"].sel(store="b").values
    tv = node_dataset(tree, "/promo")["tv"].sel(store="b").values
    np.testing.assert_allclose(sales, [10.0, 20.0, 30.0, 40.0])
    np.testing.assert_allclose(tv / 10, sales)  # each week keeps its own value


def test_assembly_keeps_each_nodes_layout(tmp_path: Path) -> None:
    pd.DataFrame({"obs": [1, 2, 3, 4], "price": [1.0, 2, 3, 4]}).to_csv(
        tmp_path / "long.csv", index=False
    )
    pd.DataFrame(
        {
            "week": pd.date_range("2024-01-07", periods=4, freq="W-SUN").strftime("%Y-%m-%d"),
            "sales": [1.0, 2, 3, 4],
        }
    ).to_csv(tmp_path / "dense.csv", index=False)
    long_map = {
        "format": "csv",
        "layout": "long",
        "dims": {"obs": {"column": "obs", "spec": {"kind": "index"}}},
        "vars": {"price": {"column": "price"}},
    }
    dense_map = {
        "format": "csv",
        "dims": {"week": {"column": "week", "spec": {"kind": "time", "policy": "extend"}}},
        "vars": {"sales": {"column": "sales", "role": "target"}},
    }
    sources = [
        {"upload_id": f"upl_{0:026d}", "mapping": long_map, "node": "/a"},
        {"upload_id": f"upl_{1:026d}", "mapping": dense_map, "node": "/b"},
    ]
    inputs = {upload_input(0): tmp_path / "long.csv", upload_input(1): tmp_path / "dense.csv"}
    ctx = _ingest(tmp_path / "out", inputs, sources)
    schema = TreeSchema.model_validate_json(ctx.outputs["tree_schema.json"])
    assert schema.nodes["/a"].layout == "long" and schema.nodes["/b"].layout == "dense"
    with pytest.raises(ValueError, match="mix dense and long"):
        schema.flat({"a": NodeSelect(path="/a"), "b": NodeSelect(path="/b")})


def test_tree_from_nodes_keeps_non_index_coords() -> None:
    week = [1, 2, 3]
    fiscal = ("week", ["x", "y", "z"])
    tree = tree_from_nodes(
        {
            "/": xr.Dataset({"a": ("week", [1.0, 2, 3])}, coords={"week": week, "fiscal": fiscal}),
            "/c": xr.Dataset({"b": ("week", [0.0, 2, 4])}, coords={"week": week, "fiscal": fiscal}),
        }
    )
    child = node_dataset(tree, "/c")
    assert set(child.coords) == {"week", "fiscal"}
    assert "week" not in tree["c"].to_dataset(inherit=False).coords  # left to inheritance


def test_outer_join_size_is_checked_before_combining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def write(path: Path, offset: int) -> Path:
        xr.Dataset(
            {f"v{offset}": (("a", "b"), np.ones((60, 60)))},
            coords={"a": np.arange(60) + offset * 100, "b": np.arange(60) + offset * 100},
        ).to_netcdf(path, engine="h5netcdf")
        return path

    k = 4
    inputs = {upload_input(i): write(tmp_path / f"s{i}.nc", i) for i in range(k)}
    sources = [_src(i, node="/m", combine="merge", join="outer") for i in range(k)]

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the sources were combined before the size check")

    monkeypatch.setattr(tree_ingest, "_merge", boom)
    job = IngestJob(
        assembly=IngestAssembly.model_validate({"sources": sources}),
        max_uncompressed_bytes=400_000,  # the sources decode to 4 x 28.8 kB; the union to 4.6 MB
    )
    with pytest.raises(MapiError) as exc:
        run_ingest(IngestCtx(tmp_path / "out", inputs), job)  # type: ignore[arg-type]
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    assert "would hold" in str(exc.value)


def test_long_node_paths_get_short_explorer_slugs() -> None:
    path = "/" + "/".join(["a" * 64] * 4)
    slug = explore_slug(path)
    assert slug.startswith("~h") and len(slug) <= MAX_EXPLORE_SLUG
    assert len(f"ui/explore/nodes/{slug}/plots/a_long_section_name.json") <= 256
    assert explore_slug(path) == slug and explore_slug(path + "b") != slug
    assert explore_slug("/sales/north") == "sales~north"


# ========================================================================= inspect


def test_inspect_skips_a_zarr_array_with_a_negative_shape(tmp_path: Path) -> None:
    good = tmp_path / "good.zarr.zip"
    write_tree_zip(
        xr.DataTree.from_dict({"/": xr.Dataset(), "/g": xr.Dataset({"y": ("n", [1.0, 2, 3])})}),
        good,
    )
    bad = tmp_path / "neg.zarr.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(bad, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info))
        dst.writestr("x/zarr.json", json.dumps({"node_type": "array", "shape": [-5]}))
    out = inspect_upload(bad, InspectJob())
    assert all(min(a.shape, default=0) >= 0 for a in out.arrays)
    assert "/x" not in {a.path for a in out.arrays}
    assert any("shape" in w for w in out.warnings)


# =================================================== tree-input models off the score path


class _Seen:
    def __init__(self) -> None:
        self.fit: list[xr.DataTree | None] = []
        self.score: list[xr.DataTree | None] = []


def _spy_registry(seen: _Seen) -> Any:
    class Spy(toy.TreeLinearModel):
        def fit(self, ds: xr.Dataset, s: Any, cfg: FitConfig, ctx: FitContext) -> FitOutput:
            seen.fit.append(ctx.tree)
            out: FitOutput = super().fit(ds, s, cfg, ctx)
            return out

        def score(
            self, fitted: Fitted, ds: xr.Dataset, r: ScoreRequest, ctx: ScoreContext
        ) -> xr.DataTree:
            seen.score.append(ctx.tree)
            out: xr.DataTree = super().score(fitted, ds, r, ctx)
            return out

    reg = toy.toy_registry()
    reg.register_model(Spy, replace=True)
    return reg


@pytest.fixture
def fitted_tree_model(tmp_path: Path) -> tuple[Any, Any, xr.Dataset, xr.DataTree, _Seen]:
    seen = _Seen()
    reg = _spy_registry(seen)
    tree = toy.tree_dataset(weeks=20)
    bundle = fit_spec(toy.tree_spec(split=False, mode="train_only"), tree, registry=reg)
    flat = select_nodes(tree, bundle.spec.data.nodes)
    seen.fit.clear()
    seen.score.clear()
    return bundle, reg, flat, tree, seen


def test_explorer_and_governance_score_tree_models_with_the_tree(
    fitted_tree_model: tuple[Any, Any, xr.Dataset, xr.DataTree, _Seen],
) -> None:
    bundle, reg, flat, tree, seen = fitted_tree_model
    refitter = Refitter(bundle, reg, tree=tree)
    ModelContext(bundle, flat, reg, tree=tree, refitter=refitter).predictions()
    assert seen.score and all(t is not None for t in seen.score)
    seen.score.clear()
    GovernanceEnv(bundle, flat, reg, refitter, tree=tree).predictions()
    assert seen.score and all(t is not None for t in seen.score)
    refitter.fit(flat)  # a refit of a tree-input model on flat data keeps the tree
    assert seen.fit and seen.fit[-1] is not None


def test_workflow_model_values_carry_the_tree(
    fitted_tree_model: tuple[Any, Any, xr.Dataset, xr.DataTree, _Seen],
) -> None:
    from mapi_workflows.runtime.models import model_residuals
    from mapi_workflows.runtime.values import ModelValue

    bundle, reg, flat, tree, seen = fitted_tree_model
    model_residuals(ModelValue(bundle=bundle, data=flat, tree=tree), reg)
    assert seen.score and seen.score[-1] is not None


def test_load_data_and_tree_keeps_the_tree(tmp_path: Path) -> None:
    tree = toy.tree_dataset(weeks=8)
    path = tmp_path / "data.tree.zip"
    write_tree_zip(tree, path)
    ctx = IngestCtx(tmp_path, {"dataset": path})
    block = load_model_spec(toy.tree_spec()).data
    flat, got = load_data_and_tree(ctx, block)  # type: ignore[arg-type]
    assert got is not None and {n.path for n in got.subtree} >= {"/sales", "/media"}
    assert {"revenue", "spend"} <= set(flat.data_vars)
