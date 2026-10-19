"""``mapi_plugin_science.export``: the case registry, the file formats and the CLI."""

from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
import yaml
from mapi_plots.recipes import render_plots
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
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.tree import TREE_SCHEMA_ATTR, read_tree_zip
from mapi_sdk.wire import read_zarr_zip
from mapi_spec import load_model_spec, validate_spec
from mapi_spec.capabilities import REPRESENTATION_RULES
from mapi_spec.refs import DatasetRef
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ModelSpec, ScoreRequest
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


def test_the_ported_cases_are_registered(tmp_path: Path) -> None:
    assert {
        "e07",
        "e18",
        "e19",
        "e21",
        "e23",
        "e31",
        "e50",
        "e51",
        "e58",
        "e60",
        "e61",
        "e77",
        "e79",
        "e80",
        "e81",
        "e81b",
    } == set(EXPORTS)
    models = {p.id for p in PLUGINS}
    for cid, case in EXPORTS.items():
        number = cid[1:].rstrip("bcd")  # e81b: a second dataset of notebook E81
        assert case.case == cid and cid[0] == "e" and number.isdigit()
        assert case.model in models
        assert case.notebook.startswith(f"E{number}_")
        # a notebook that simulates its data (E51, E61 part A) has no file: its loader
        # builds the data from an empty directory
        assert case.files or case.load(tmp_path) is not None


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


def _exported_schema(cid: str, tmp_path: Path, data: xr.Dataset | xr.DataTree) -> DataSchema:
    """The raw schema that the exported file carries (what the ingest reads)."""
    case = EXPORTS[cid]
    assert isinstance(case.schema, DataSchema)
    path = write_export(data, tmp_path / f"{cid}.nc", schema=case.schema)
    with xr.open_dataset(path, engine="h5netcdf") as back:
        return DataSchema.model_validate_json(back.attrs[SCHEMA_ATTR])


@pytest.mark.parametrize("cid", CASES)
def test_suggested_spec_validates_against_the_exported_schema(
    cid: str, tmp_path: Path, registry: Registry, synthetic: dict[str, xr.Dataset | xr.DataTree]
) -> None:
    """The spec of ``--spec`` validates against the schema in the file of ``--out``, and an
    ``elpd_*`` metric is listed only for a model with a pointwise log-likelihood (the slow
    test below checks that the fit keeps it)."""
    case = EXPORTS[cid]
    data = synthetic[cid]
    spec = load_model_spec(case.spec_yaml(data))
    res = validate_spec(spec, registry.catalog(), _exported_schema(cid, tmp_path, data))
    assert res.ok, [(i.pointer, i.message) for i in res.errors]
    if any(m.startswith("elpd_") for m in spec.evaluate.metrics):
        plugin = registry.model(spec.model.use.id)
        assert plugin.capabilities.loo_valid, f"{case.model} has no pointwise log-likelihood"


def test_e50_spec_keeps_the_log_likelihood_for_elpd_loo() -> None:
    """``sci.fisher_kpp`` stores the log-likelihood only on request, and E50 compares the
    models with and without the delay by ``elpd_loo``."""
    spec = load_model_spec(EXPORTS["e50"].spec_yaml(EXPORTS["e50"].synthetic()))
    assert "elpd_loo" in spec.evaluate.metrics and spec.model.fit.keep.log_likelihood


def test_e60_and_e80_specs_draw_their_images() -> None:
    """``sci.field_map`` cannot draw the vector output ``traction`` and, for the lens, draws
    the parameter ``e_light``: the specs use the recipes for these outputs."""
    traction = load_model_spec(EXPORTS["e60"].spec_yaml(EXPORTS["e60"].synthetic()))
    lens = load_model_spec(EXPORTS["e80"].spec_yaml(EXPORTS["e80"].synthetic()))
    plots = {
        cid: [str(p).split("@")[0] for p in s.outputs.plots]
        for cid, s in (("e60", traction), ("e80", lens))
    }
    assert "sci.vector_field" in plots["e60"] and "sci.field_map" not in plots["e60"]
    assert {"sci.image_residuals", "sci.ppc_image_grid"} <= set(plots["e80"])
    assert "sci.field_map" not in plots["e80"]


def test_synthetic_exports_get_a_spec_that_fits_them() -> None:
    """E77 and E79: the real spec names the notebook's held-out speakers and bandpower bins
    that the synthetic data does not have, so a synthetic export gets the tiny settings."""
    e77 = EXPORTS["e77"].spec_yaml(EXPORTS["e77"].synthetic())
    assert "n: 2, seed: 1" in e77 and "s27" not in e77
    e79 = EXPORTS["e79"].spec_yaml(EXPORTS["e79"].synthetic())
    assert "bin_edges: [0, 300," in e79


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


def test_cli_as_a_module_lists_every_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m`` runs ``main()`` at the ``__main__`` guard, so a case registered
    below the guard is missing from the command line though an import finds it."""
    monkeypatch.delenv(CHALLENGES_ENV, raising=False)
    run = subprocess.run(
        [sys.executable, "-m", "mapi_plugin_science.export", "--list"],
        capture_output=True,
        text=True,
        check=True,
    )
    missing = [c for c in CASES if c not in run.stdout]
    assert not missing, f"cases missing from `-m ... --list`: {missing}"
    assert "e79" in run.stdout and "sci.cmb_field" in run.stdout


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
    first = challenges_file(case.files[0]) if case.files else None
    assert first is not None or not case.files
    data_dir = first.parent if first is not None else tmp_path  # E51, E61: no file to read
    path, data = export_case(cid, tmp_path / f"{cid}.nc", data_dir=data_dir)
    assert isinstance(data, xr.Dataset) and isinstance(case.schema, DataSchema)
    back = xr.open_dataset(path, engine="h5netcdf").load()
    assert validate_dataset(case.schema, back) == []
    spec = load_model_spec(case.spec_yaml(data))
    assert validate_spec(spec, registry.catalog(), case.schema).ok


# ------------------------------------------------------------------------- tiny fit, plots

_TINY_FIT: dict[str, Any] = {"tune": 30, "draws": 30, "chains": 2}
"""The fit settings that the plot check puts in each suggested spec (the rest is kept)."""


def _tiny_spec(case: ExportCase, data: xr.Dataset | xr.DataTree) -> ModelSpec:
    doc = yaml.safe_load(case.spec_yaml(data))
    doc["model"].setdefault("fit", {}).update(_TINY_FIT)
    return load_model_spec(yaml.safe_dump(doc, sort_keys=False))


def _evaluation_tree(
    spec: ModelSpec, bundle: Bundle, data: xr.Dataset, registry: Registry
) -> xr.DataTree:
    """The predictions that the runner gives to the plots after a fit: the ``fit`` worker
    scores the data when the bundle alone cannot draw a plot."""
    target = bundle.evaluation or bundle
    caps = target.model.manifest.capabilities
    request = ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": f"model://{spec.metadata.project}/{spec.metadata.name}@latest",
            "mode": "evaluate" if "evaluate" in caps.score_modes else "predict",
            "data": "dataset://runner/holdout@latest",
            "output": {
                "names": "canonical",
                "kind": "predictive"
                if REPRESENTATION_RULES[caps.representation].predictive
                else "expected",
            },
            "seed": spec.model.fit.seed or 0,
        }
    )
    metrics = [m for m in spec.evaluate.metrics if not m.startswith("elpd_")] or ["rmse"]
    tree = score_bundle(target, request, data, registry=registry, metrics=metrics, return_full=True)
    assert isinstance(tree, xr.DataTree)
    return tree


def _output_names(registry: Registry, model: str) -> set[str]:
    """The names of the model's outputs and of the variables that hold them."""
    plugin: Any = registry.model(model)
    names = set(getattr(plugin, "outputs", None) or {})
    for attr in ("expected", "predictive"):
        names |= set((getattr(plugin, attr, None) or {}).values())
    return names


@pytest.mark.slow
@pytest.mark.parametrize("cid", CASES)
def test_suggested_spec_plots_render_on_a_tiny_fit(
    cid: str, tmp_path: Path, registry: Registry, synthetic: dict[str, xr.Dataset | xr.DataTree]
) -> None:
    """Fit the suggested spec (with few draws) on the synthetic export, then draw every plot
    of ``outputs.plots`` as the runner does: from the bundle, else from the predictions.
    A map recipe must draw a model output (not, for example, a parameter with two dims),
    and the fit must calculate each ``elpd_*`` metric that the spec lists."""
    case = EXPORTS[cid]
    data = synthetic[cid]
    assert isinstance(data, xr.Dataset)
    schema = _exported_schema(cid, tmp_path, data)
    spec = _tiny_spec(case, data)
    refs = [str(p).split("@")[0] for p in spec.outputs.plots]
    drawn: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        bundle = fit_spec(spec, data, registry=registry, raw_schema=schema)
        for source in (bundle.to_tree, lambda: _evaluation_tree(spec, bundle, data, registry)):
            todo = [r for r in refs if r not in drawn]
            if not todo:
                break
            report = render_plots(source(), todo, registry=registry)
            drawn |= {p.id: p for p in report.plots}
            for issue in report.issues:
                ref = todo[int(str(issue.pointer).rsplit("/", 1)[-1])]
                reasons[ref] = f"{issue.message} {issue.untrusted_text or ''}"
    missing = {r: reasons.get(r) for r in refs if r not in drawn}
    assert not missing, missing
    outputs = _output_names(registry, case.model)
    for rid in ("sci.field_map", "sci.vector_field"):
        if rid in drawn:
            var = drawn[rid].title.rsplit(" of ", 1)[-1]
            assert var in outputs, f"{rid} draws {var!r}, not an output ({sorted(outputs)})"
    skipped = bundle.metrics.skipped or {}
    for name in spec.evaluate.metrics:
        if name.startswith("elpd_"):
            assert "log_likelihood" not in str(skipped.get(name, "")), skipped[name]
