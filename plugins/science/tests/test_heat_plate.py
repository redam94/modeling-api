"""``sci.heat_plate`` (E51): the finite-volume heat equation solved in the model, sparse
thermocouple targets, new heater layouts at score time, typed refusals and the notebook's
calibration experiment."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science.export import EXPORTS, export_case
from mapi_plugin_science.heat_plate import (
    FIELD,
    INSULATION,
    OBS_DIM,
    HeatPlateModel,
    HeatPlateSettings,
    grid_laplacian,
    heat_operator,
    spread_length_cm,
    steady_rise,
)
from mapi_plugin_science.heat_plate_data import (
    CHALLENGES_FILE,
    E51_SPEC_YAML,
    HEAT_SCHEMA,
    HEAT_SPEC_YAML,
    HEAT_TRUTH,
    NOTEBOOK_REFERENCE,
    from_challenges,
    heat_plate_dataset,
)
from mapi_pymc.context import BuildContext
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/heat\n"
CANON = {"y_cm": "y", "x_cm": "x", "heater_w": "heater_power", "reading_c": "temperature"}
DELTA = 0.005


def _req(body: str) -> object:
    return load_score_request(HEADER + body)


def _canon(ds: xr.Dataset) -> xr.Dataset:
    return ds.rename(CANON)


def _layout(ds: xr.Dataset, label: str, cells: list[tuple[int, int, float]]) -> xr.Dataset:
    """A score payload with one new run: ``(row, col, watts)`` heaters, no readings."""
    power = np.zeros((1, ds.sizes["y_cm"], ds.sizes["x_cm"]))
    for j, i, w in cells:
        power[0, j, i] = w
    return xr.Dataset(
        {"heater_w": (("run", "y_cm", "x_cm"), power)},
        coords={"run": [label], "y_cm": ds["y_cm"].values, "x_cm": ds["x_cm"].values},
    )


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(HeatPlateModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def plate() -> xr.Dataset:
    return heat_plate_dataset(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, plate: xr.Dataset) -> Bundle:
    return fit_spec(HEAT_SPEC_YAML, plate, registry=registry, raw_schema=HEAT_SCHEMA)


# ------------------------------------------------------------------------------- manifest


def test_manifest_declares_a_field_per_run_and_the_spread_length() -> None:
    man = HeatPlateModel.manifest()
    assert man.id == "sci.heat_plate"
    assert man.capabilities.score_modes == frozenset({"predict", "simulate", "evaluate"})
    assert man.capabilities.loo_valid
    grid = man.outputs[FIELD].grid
    assert grid is not None
    assert (grid.y, grid.x, grid.frame, grid.origin) == ("y", "x", "run", "lower")
    assert man.outputs[FIELD].units_of == "temperature"
    assert man.outputs["spread_length"].dims == [] and man.outputs["spread_length"].units == "cm"
    schema = man.schema_template.resolve({})
    assert schema.dims["run"].policy.value == "new_levels"
    assert schema.dims["y"].policy.value == "fixed" and INSULATION not in schema.vars
    assert schema.vars["temperature"].missing == "allow"
    assert INSULATION in man.schema_template.resolve({"use_insulation": True}).vars
    assert set(man.plots) == {
        "core.convergence",
        "core.posterior_forest",
        "core.pair",
        "sci.field_map",
        "sci.image_residuals",
    }


@pytest.mark.parametrize("text", [HEAT_SPEC_YAML, E51_SPEC_YAML])
def test_static_validation_resolves_the_specs(registry: Registry, text: str) -> None:
    res = validate_spec(load_model_spec(text), registry.catalog(), HEAT_SCHEMA)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]


# ------------------------------------------------------------------------------- numerics


def test_laplacian_and_edges_of_a_small_grid() -> None:
    lap, n_edge = grid_laplacian(2, 3)
    np.testing.assert_allclose(lap.sum(axis=1), 0.0)  # no flux leaves through L
    np.testing.assert_array_equal(n_edge, [2, 1, 2, 2, 1, 2])
    assert lap[0, 1] == 1 and lap[0, 3] == 1 and lap[0, 4] == 0 and lap[1, 1] == -3
    a = heat_operator(60.0, 9.0, 15.0, lap=lap, n_edge=n_edge, dx_m=0.01, thickness_m=DELTA)
    np.testing.assert_allclose(a, a.T)
    assert np.all(np.linalg.eigvalsh(a) > 0)


def test_field_is_linear_in_heater_powers() -> None:
    rng = np.random.default_rng(3)
    p1, p2 = rng.uniform(0, 5, (2, 6, 12))
    kw = {"k": 55.0, "h": 8.0, "h_edge": 12.0, "eta": 0.9, "dx_m": 0.02, "thickness_m": DELTA}
    np.testing.assert_allclose(
        steady_rise(2 * p1 + 3 * p2, **kw),
        2 * steady_rise(p1, **kw) + 3 * steady_rise(p2, **kw),
        rtol=1e-10,
    )
    # all the power leaves through the faces and the edges at steady state
    lap, n_edge = grid_laplacian(6, 12)
    u = steady_rise(p1, **kw).reshape(-1)
    loss = 2 * 8.0 * 0.02**2 * u + 12.0 * DELTA * 0.02 * n_edge * u
    assert loss.sum() == pytest.approx(0.9 * p1.sum(), rel=1e-10)
    assert spread_length_cm(60.0, 9.0, DELTA) == pytest.approx(12.91, abs=0.01)


@pytest.mark.parametrize("insulated", [False, True])
def test_graph_matches_the_numpy_twin(insulated: bool) -> None:
    ds = _canon(heat_plate_dataset(nx=8, ny=4, seed=2))
    s = HeatPlateSettings(use_insulation=insulated, insulated_back_loss=0.1)
    back = None
    if insulated:
        ins = np.zeros((4, 8), dtype=bool)
        ins[:, 6:] = True
        ds = ds.assign({INSULATION: (("y", "x"), ins)})
        back = np.where(ins, 0.1, 1.0)
    model = HeatPlateModel().build(ds, s, BuildContext(phase="fit", ds=ds))
    point = {"k_log__": math.log(58.0), "h_log__": math.log(9.5), "h_edge_log__": math.log(14.0)}
    point |= {"eta_logodds__": math.log(0.92 / 0.08), "sigma_log__": math.log(0.2)}
    graphs = model.replace_rvs_by_values([model[FIELD], model["spread_length"]])
    fn = model.compile_fn(graphs, inputs=model.value_vars, on_unused_input="ignore")
    field, spread = fn(point)
    want = 20.0 + steady_rise(
        ds["heater_power"].values, k=58.0, h=9.5, h_edge=14.0, eta=0.92, dx_m=0.05,
        thickness_m=DELTA, back=back,
    )  # fmt: skip
    np.testing.assert_allclose(field, want, rtol=1e-8)
    assert float(spread) == pytest.approx(spread_length_cm(58.0, 9.5, DELTA), rel=1e-10)


def test_fit_likelihood_runs_over_the_readings_only() -> None:
    ds = _canon(heat_plate_dataset(nx=8, ny=4, n_sensors=5, seed=1))
    ctx = BuildContext(phase="fit", ds=ds)
    model = HeatPlateModel().build(ds, HeatPlateSettings(), ctx)
    assert model.named_vars_to_dims["temperature"] == (OBS_DIM,)
    assert len(model.coords[OBS_DIM]) == 3 * 5
    assert ctx.observed_vars["temperature"] == ("run", "y", "x")  # observed_data keeps the grid


# ------------------------------------------------------------------------------- refusals


def _build(ds: xr.Dataset, **settings: object) -> None:
    s = HeatPlateSettings.model_validate(settings)
    HeatPlateModel().build(ds, s, BuildContext(phase="fit", ds=ds))


def test_rectangular_cells_are_refused() -> None:
    ds = _canon(heat_plate_dataset(nx=8, ny=4, seed=0))
    with pytest.raises(MapiError) as err:
        _build(ds.assign_coords(y=ds["y"].values * 1.5))
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "square" in str(err.value)


def test_uneven_positions_are_refused() -> None:
    ds = _canon(heat_plate_dataset(nx=8, ny=4, seed=0))
    x = ds["x"].values.copy()
    x[-1] += 1.0
    with pytest.raises(MapiError) as err:
        _build(ds.assign_coords(x=x))
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.pointer == "/data/x"


def test_a_fit_without_readings_is_refused() -> None:
    ds = _canon(heat_plate_dataset(nx=8, ny=4, seed=0))
    ds["temperature"][:] = np.nan
    with pytest.raises(MapiError) as err:
        _build(ds)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.pointer == "/data/temperature"


def test_insulation_setting_needs_the_variable() -> None:
    ds = _canon(heat_plate_dataset(nx=8, ny=4, seed=0))
    with pytest.raises(MapiError) as err:
        _build(ds, use_insulation=True)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and INSULATION in str(err.value)


def test_missing_heater_powers_are_refused() -> None:
    ds = _canon(heat_plate_dataset(nx=8, ny=4, seed=0))
    ds["heater_power"][0, 0, 0] = np.nan
    with pytest.raises(MapiError) as err:
        _build(ds)
    assert err.value.code is ErrorCode.SCHEMA_INVALID


# ------------------------------------------------------------------------------- fit / score


@pytest.mark.slow
def test_fit_recovers_the_plate(bundle: Bundle, plate: xr.Dataset) -> None:
    groups = bundle.model.groups
    obs = groups["observed_data"]["temperature"]
    assert obs.dims == ("run", "y", "x") and int(np.isfinite(obs).sum()) == 36
    post = groups["posterior"]
    for name in ("k", "h", "eta", "sigma"):
        lo, hi = np.quantile(post[name].values, [0.01, 0.99])
        assert lo < HEAT_TRUTH[name] < hi, (name, lo, hi)
    spread = spread_length_cm(HEAT_TRUTH["k"], HEAT_TRUTH["h"], DELTA)
    assert abs(float(post["spread_length"].mean()) - spread) < 0.05 * spread
    assert post[FIELD].dims == ("chain", "draw", "run", "y", "x")
    corr = bundle.model.learned["posterior_correlations"]
    assert corr["k-eta"] > 0.5  # rises depend on eta / k: the two are tied
    # the held-out run (a heater the fit never saw) is predicted within the noise
    assert bundle.metrics.values["rmse"] < 3 * HEAT_TRUTH["sigma"]
    assert bundle.evaluation is not None


@pytest.mark.slow
def test_predict_a_new_heater_layout(
    bundle: Bundle, plate: xr.Dataset, registry: Registry
) -> None:
    layout = _layout(plate, "design", [(1, 10, 8.0), (4, 10, 8.0), (3, 2, 3.0)])
    out = score_bundle(bundle, _req("mode: predict\n"), layout, registry=registry, return_full=True)
    pred = out["predictions"].to_dataset()
    field = pred[FIELD]
    assert field.dims == ("chain", "draw", "run", "y_cm", "x_cm")
    assert list(field["run"].values) == ["design"] and field.attrs["kind"] == "expected"
    post = bundle.model.groups["posterior"]
    k, h, he, eta = (float(post[v].mean()) for v in ("k", "h", "h_edge", "eta"))
    mean = field.mean(("chain", "draw")).values[0]
    twin = 20.0 + steady_rise(
        layout["heater_w"].values[0], k=k, h=h, h_edge=he, eta=eta, dx_m=40.0 / 12 / 100,
        thickness_m=DELTA,
    )  # fmt: skip
    np.testing.assert_allclose(mean, twin, atol=0.5)
    assert mean[:, 10].mean() > mean[:, 2].mean() > 20.0  # hotter near the strong heaters
    assert out["summary"].to_dataset()[FIELD].attrs["units"] == "degC"


@pytest.mark.slow
def test_simulate_adds_thermocouple_noise(
    bundle: Bundle, plate: xr.Dataset, registry: Registry
) -> None:
    layout = _layout(plate, "design", [(3, 6, 10.0)])
    exp = score_bundle(bundle, _req("mode: predict\n"), layout, registry=registry, return_full=True)
    sim = score_bundle(
        bundle, _req("mode: simulate\nseed: 4\n"), layout, registry=registry, return_full=True
    )
    again = score_bundle(
        bundle, _req("mode: simulate\nseed: 4\n"), layout, registry=registry, return_full=True
    )
    f_exp = exp["predictions"].to_dataset()[FIELD]
    f_sim = sim["predictions"].to_dataset()[FIELD]
    assert f_sim.attrs["kind"] == "predictive"
    resid = (f_sim - f_exp).values
    sigma = float(bundle.model.groups["posterior"]["sigma"].mean())
    assert 0.7 * sigma < resid.std() < 1.3 * sigma
    np.testing.assert_array_equal(f_sim.values, again["predictions"].to_dataset()[FIELD].values)


@pytest.mark.slow
def test_predictive_kind_and_in_sample_evaluate(
    bundle: Bundle, plate: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        bundle, _req("mode: predict\noutput: {kind: predictive}\n"), plate.drop_vars("reading_c"),
        registry=registry, return_full=True,
    )  # fmt: skip
    assert out["predictions"].to_dataset()[FIELD].attrs["kind"] == "predictive"
    tree = score_bundle(
        bundle,
        _req(
            "mode: evaluate\ndata: dataset://engineering/heat_plate_calibration@v1\nseed: 3\n"
            "output: {kind: predictive}\n"
        ),
        plate,
        registry=registry,
        metrics=["rmse", "mae", "coverage_90"],
    )
    values = json.loads(tree.attrs["metrics"])["values"]
    # 36 readings: the posterior mean field leaves about the sensor noise
    assert values["rmse"] < 2 * HEAT_TRUTH["sigma"]
    assert values["coverage_90"] >= 0.7


@pytest.mark.slow
def test_unsupported_modes_and_unseen_cells_are_typed(
    bundle: Bundle, plate: xr.Dataset, registry: Registry
) -> None:
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, _req("mode: forecast\nhorizon: {dim: x, periods: 2}\n"),
                     registry=registry)  # fmt: skip
    assert err.value.code is ErrorCode.MODE_NOT_SUPPORTED
    layout = _layout(plate, "design", [(1, 1, 5.0)])
    off_grid = layout.assign_coords(x_cm=layout["x_cm"].values + 100.0)
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, _req("mode: predict\n"), off_grid, registry=registry)
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, _req("mode: predict\n"), layout.drop_vars("heater_w"),
                     registry=registry)  # fmt: skip
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


@pytest.mark.slow
def test_log_likelihood_gives_elpd_loo(registry: Registry, plate: xr.Dataset) -> None:
    text = HEAT_SPEC_YAML.replace("mode: evaluate_then_refit", "mode: train_only").replace(
        "engine: pymc}", "engine: pymc, keep: {log_likelihood: true}}"
    ).replace("[rmse, mae, coverage_90]", "[elpd_loo]")
    b = fit_spec(text, plate, registry=registry, raw_schema=HEAT_SCHEMA)
    ll = b.model.groups["log_likelihood"]["temperature"]
    assert ll.dims == ("chain", "draw", OBS_DIM) and ll.sizes[OBS_DIM] == 24  # 2 runs x 12
    assert b.metrics.values["elpd_loo"] is not None
    assert b.metrics.elpd["elpd_loo"].pointwise is not None


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_heat_plate_passes_the_contract_kit(registry: Registry, plate: xr.Dataset) -> None:
    fixture = ModelFixture(spec=HEAT_SPEC_YAML, data=plate, raw_schema=HEAT_SCHEMA, registry=registry)
    report = check_model(HeatPlateModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "shuffled_scoring",
        "subset_scoring",
        "new_level_policies",
        "unsupported_mode",
        "plots_sanitize",
        "output_space_and_units",
        "outputs_match_manifest",
        "seed_determinism",
    } <= ran


# ------------------------------------------------------------------------------- E51 data


def test_from_challenges_rebuilds_the_notebook_experiment() -> None:
    """E51 simulates its calibration: there is no file, and the loader needs none."""
    assert CHALLENGES_FILE is None
    ds = from_challenges()
    assert validate_dataset(HEAT_SCHEMA, ds) == []
    assert dict(ds.sizes) == {"run": 3, "y_cm": 12, "x_cm": 24}
    assert int(np.isfinite(ds["reading_c"]).sum()) == 36
    np.testing.assert_allclose(np.diff(ds["x_cm"].values), 40.0 / 24)
    # one 10 W heater per run, at the notebook's test sites (cells 147, 84 and 211)
    heaters = ds["heater_w"].values.reshape(3, -1)
    assert [int(np.argmax(r)) for r in heaters] == [147, 84, 211] and heaters.sum() == 30.0
    readings = ds["reading_c"].values
    # the same twelve thermocouples in each run
    np.testing.assert_array_equal(np.isfinite(readings[0]), np.isfinite(readings[2]))
    np.testing.assert_allclose(from_challenges("/no/such/dir")["reading_c"], readings)


def test_export_case_needs_no_data_file(tmp_path: Path) -> None:
    case = EXPORTS["e51"]
    assert case.files == () and case.model == "sci.heat_plate"
    path, data = export_case("e51", tmp_path / "plate.nc", data_dir=tmp_path)
    assert path.is_file() and isinstance(data, xr.Dataset)
    np.testing.assert_allclose(data["reading_c"], from_challenges()["reading_c"])
    path, _ = export_case("e51", tmp_path / "plate2.nc")
    assert path.is_file()


@pytest.mark.slow
def test_e51_calibration_matches_the_notebook(registry: Registry) -> None:
    """The notebook's calibration (3 runs, 12 thermocouples) at reduced sampler settings:
    the posterior means sit near the notebook's printout and h_edge stays mostly prior."""
    text = E51_SPEC_YAML.replace("tune: 1000", "tune: 300").replace("draws: 1000", "draws: 300")
    text = text.replace("chains: 4", "chains: 2")
    b = fit_spec(text, from_challenges(), registry=registry, raw_schema=HEAT_SCHEMA)
    post = b.model.groups["posterior"]
    ref = NOTEBOOK_REFERENCE["summary"]
    for name in ("k", "h", "eta", "sigma"):
        got = float(post[name].mean())
        assert abs(got - ref[name]["mean"]) < 0.5 * ref[name]["sd"], (name, got)
    # the edge coefficient is weakly identified: wide, and below the true 15
    he = post["h_edge"].values.ravel()
    assert he.std() > 2.0 and float(np.quantile(he, 0.945)) < 20.0
    assert float(post["spread_length"].mean()) == pytest.approx(12.9, abs=0.5)
    assert b.model.learned["posterior_correlations"]["k-eta"] > 0.85
    assert b.metrics.values["elpd_loo"] is not None
