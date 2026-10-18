"""``mapi_plugin_science.export``: the case registry, the file formats and the CLI."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science import PLUGINS
from mapi_plugin_science.challenges import CHALLENGES_ENV, challenges_file
from mapi_plugin_science.export import (
    EXPORTS,
    SCHEMA_ATTR,
    ExportCase,
    export_case,
    main,
    write_export,
)
from mapi_plugin_science.lens_data import LENS_TREE_SCHEMA, lens_dataset, to_tree
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.tree import TREE_SCHEMA_ATTR, read_tree_zip
from mapi_sdk.wire import read_zarr_zip
from mapi_spec import load_model_spec, validate_spec
from mapi_spec.refs import DatasetRef
from mapi_spec.schema import DataSchema
from mapi_spec.tree import TreeSchema

CASES = sorted(EXPORTS)


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    for plugin in PLUGINS:
        reg.register_model(plugin, replace=True)
    return reg


@pytest.fixture(scope="module")
def synthetic() -> dict[str, xr.Dataset | xr.DataTree]:
    return {c: EXPORTS[c].synthetic() for c in CASES}


def test_the_ported_cases_are_registered() -> None:
    assert {"e07", "e19", "e50", "e60", "e80"} <= set(EXPORTS)
    models = {p.id for p in PLUGINS}
    for cid, case in EXPORTS.items():
        assert case.case == cid and cid[0] == "e" and cid[1:].isdigit()
        assert case.model in models
        assert case.files and case.notebook.startswith(f"E{cid[1:]}_")


@pytest.mark.parametrize("cid", CASES)
def test_suggested_spec_is_valid_and_reads_the_suggested_dataset(
    cid: str, registry: Registry, synthetic: dict[str, xr.Dataset | xr.DataTree]
) -> None:
    case = EXPORTS[cid]
    spec = load_model_spec(case.spec_yaml(synthetic[cid]))
    assert str(spec.model.use).startswith(case.model + "@")
    assert spec.data.dataset == DatasetRef.parse(f"dataset://{case.project}/{case.dataset}@latest")
    schema = case.schema
    flat = schema if isinstance(schema, DataSchema) else None
    res = validate_spec(spec, registry.catalog(), flat)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]


@pytest.mark.parametrize("cid", CASES)
def test_netcdf_round_trip_keeps_the_data_and_the_schema(
    cid: str, tmp_path: Path, synthetic: dict[str, xr.Dataset | xr.DataTree]
) -> None:
    case = EXPORTS[cid]
    data = synthetic[cid]
    assert isinstance(data, xr.Dataset) and isinstance(case.schema, DataSchema)
    path = write_export(data, tmp_path / f"{cid}.nc", schema=case.schema)
    back = xr.open_dataset(path, engine="h5netcdf").load()
    stored = DataSchema.model_validate_json(back.attrs[SCHEMA_ATTR])
    assert stored == case.schema
    assert validate_dataset(stored, back) == []
    for name in data.data_vars:
        np.testing.assert_array_equal(back[name].values, data[name].values)
        assert back[name].dtype == data[name].dtype
    for dim in data.dims:
        if dim in data.coords:
            np.testing.assert_array_equal(back[dim].values, data[dim].values)


def test_zarr_zip_and_tree_formats(tmp_path: Path) -> None:
    ds = lens_dataset(size=16)
    z = write_export(ds, tmp_path / "lens.zarr.zip")
    np.testing.assert_allclose(read_zarr_zip(z.read_bytes())["image"].values, ds["image"].values)
    tree = to_tree(ds)
    t = write_export(tree, tmp_path / "lens.tree.zip", schema=LENS_TREE_SCHEMA)
    back = read_tree_zip(t)
    assert TreeSchema.model_validate_json(back.attrs[TREE_SCHEMA_ATTR]) == LENS_TREE_SCHEMA
    np.testing.assert_allclose(back["psf"]["psf"].values, ds["psf"].values)
    nc = write_export(tree, tmp_path / "lens_tree.nc", schema=LENS_TREE_SCHEMA)
    opened = xr.open_datatree(nc, engine="h5netcdf")
    assert {n.path for n in opened.subtree} == {"/", "/image", "/psf"}
    np.testing.assert_allclose(opened["image"]["image"].values, ds["image"].values)
    with pytest.raises(ValueError, match="tree dataset"):
        write_export(tree, tmp_path / "x.zarr.zip")
    with pytest.raises(ValueError, match="plain dataset"):
        write_export(ds, tmp_path / "x.tree.zip")
    with pytest.raises(ValueError, match="unknown format"):
        write_export(ds, tmp_path / "x.csv")


def test_attrs_netcdf_cannot_hold_are_converted(tmp_path: Path) -> None:
    ds = xr.Dataset(
        {"y": ("t", np.arange(3.0), {"flag": True, "none": None})},
        coords={"t": np.arange(3)},
        attrs={"truth": {"a": 1}, "pair": (1, 2), "ok": True},
    )
    back = xr.open_dataset(write_export(ds, tmp_path / "a.nc"), engine="h5netcdf")
    assert back.attrs["truth"] == '{"a": 1}' and back.attrs["ok"] == 1
    assert back["y"].attrs == {"flag": 1}


def test_export_case_writes_the_spec(tmp_path: Path) -> None:
    path, data = export_case(
        "e50", tmp_path / "assay.nc", synthetic=True, spec_out=tmp_path / "spec.yaml"
    )
    assert path.is_file() and isinstance(data, xr.Dataset)
    assert str(load_model_spec((tmp_path / "spec.yaml").read_text()).model.use).startswith(
        "sci.fisher_kpp"
    )
    with pytest.raises(FileNotFoundError, match=r"scratch_assay_pc3\.csv"):
        export_case("e50", tmp_path / "x.nc", data_dir=tmp_path)
    with pytest.raises(ValueError, match="data/ directory"):
        export_case("e50", tmp_path / "x.nc")


def test_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CHALLENGES_ENV, raising=False)
    assert main(["--list"]) == 0
    listed = capsys.readouterr().out
    assert all(c in listed for c in CASES) and "sci.lgcp_grid" in listed
    out = tmp_path / "quakes.nc"
    assert main(["e07", "--synthetic", "--out", str(out), "--spec", str(tmp_path / "s.yaml")]) == 0
    assert out.is_file() and (tmp_path / "s.yaml").is_file()
    assert "geoscience/oklahoma_quakes_grid" in capsys.readouterr().out
    assert main(["e07", "--challenges", str(tmp_path), "--out", str(out)]) == 1
    assert "oklahoma_quakes.csv" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["e07"])  # no data directory and no --synthetic
    with pytest.raises(SystemExit):
        main(["e99", "--synthetic"])


def test_registering_a_case_twice_fails() -> None:
    from mapi_plugin_science.export import _register

    case = EXPORTS["e07"]
    with pytest.raises(ValueError, match="twice"):
        _register(case)
    assert isinstance(case, ExportCase) and not case.tree


# ------------------------------------------------------------------------------- real data


def _real(cid: str) -> bool:
    return all(challenges_file(f) is not None for f in EXPORTS[cid].files)


@pytest.mark.slow
@pytest.mark.parametrize("cid", CASES)
def test_real_data_export(cid: str, tmp_path: Path, registry: Registry) -> None:
    if not _real(cid):
        pytest.skip("pymc-challenges data not present")
    case = EXPORTS[cid]
    first = challenges_file(case.files[0])
    assert first is not None
    path, data = export_case(cid, tmp_path / f"{cid}.nc", data_dir=first.parent)
    assert isinstance(data, xr.Dataset) and isinstance(case.schema, DataSchema)
    back = xr.open_dataset(path, engine="h5netcdf").load()
    assert validate_dataset(case.schema, back) == []
    spec = load_model_spec(case.spec_yaml(data))
    assert validate_spec(spec, registry.catalog(), case.schema).ok
