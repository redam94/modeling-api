"""``sci.turing_pattern`` (E61 part A): kinetics, the mode-growth likelihood, fit, scores."""

from __future__ import annotations

import json
import math
import os

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science.challenges import CHALLENGES_ENV
from mapi_plugin_science.turing import (
    PARAMETERS,
    SpectralGrid,
    TuringPatternModel,
    TuringPatternSettings,
    _Data,
    conditional_pattern,
    d_critical,
    expm2,
    fastest_mode,
    growth_rate,
    is_turing,
    mode_terms,
    pointwise_loglik,
)
from mapi_plugin_science.turing_data import (
    CHALLENGES_FILE,
    NOTEBOOK_SPEC_YAML,
    TURING_SCHEMA,
    TURING_SPEC_YAML,
    TURING_TRUTH,
    challenges_reference,
    from_challenges,
    simulate_turing,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import ErrorCode, MapiError, load_model_spec, load_score_request, validate_spec
from mapi_spec.capabilities import GridDecl
from scipy.linalg import expm

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://science/turing\n"
REAL = os.environ.get(CHALLENGES_ENV)
"""E61 part A has no data file: the real-data tests run when the pymc-challenges data
directory is configured (they repeat the notebook's own simulation)."""


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(TuringPatternModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def synthetic() -> tuple[xr.Dataset, dict[str, float]]:
    return simulate_turing(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, synthetic: tuple[xr.Dataset, dict[str, float]]) -> Bundle:
    data, _ = synthetic
    return fit_spec(TURING_SPEC_YAML, data, registry=registry, raw_schema=TURING_SCHEMA)


def _score(bundle: Bundle, registry: Registry, body: str, data: xr.Dataset | None) -> xr.DataTree:
    req = load_score_request(HEADER + body)
    return score_bundle(bundle, req, data, registry=registry, return_full=True)


def _predictions(tree: xr.DataTree) -> xr.Dataset:
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


# ------------------------------------------------------------------------------ manifest


def test_manifest_declares_grids_modes_and_loo() -> None:
    m = TuringPatternModel.manifest()
    caps = m.capabilities
    assert caps.score_modes == frozenset({"predict", "simulate", "evaluate"})
    assert caps.loo_valid and caps.deterministic_given_seed
    pattern = m.outputs["pattern"]
    assert isinstance(pattern.grid, GridDecl)
    assert (pattern.grid.y, pattern.grid.x, pattern.grid.frame) == ("y", "x", "t")
    assert pattern.units_of == "activator" and pattern.grid.colormap == "magma"
    assert isinstance(m.outputs["settled"].grid, GridDecl)
    space = m.outputs["turing_space"]
    assert isinstance(space.grid, GridDecl) and space.units == "probability"
    assert m.outputs["growth"].dims == ["wavenumber"]
    assert m.outputs["p_turing"].dims == [] and m.outputs["wavelength"].units == "length"
    assert {"sci.field_map", "sci.ppc_image_grid", "sci.profile_bands"} <= set(m.plots)


def test_schema_template_and_static_validation(registry: Registry) -> None:
    plain = TuringPatternModel.manifest().schema_template.resolve({"max_extension": 3})
    assert plain.vars["activator"].dims == ("t", "y", "x")
    assert plain.dims["t"].policy.value == "extend" and plain.dims["t"].max_extension == 3
    assert plain.dims["y"].policy.value == "fixed"
    for text in (TURING_SPEC_YAML, NOTEBOOK_SPEC_YAML):
        res = validate_spec(load_model_spec(text), registry.catalog(), TURING_SCHEMA)
        assert res.ok, [i.message for i in res.errors]


# ------------------------------------------------------------------------------ kinetics


def test_linear_stability_matches_the_notebook() -> None:
    assert d_critical(1.0, 0.1) == pytest.approx(8.57, abs=0.01)
    assert np.isinf(d_critical(1.0, 0.6))
    km, lm = fastest_mode(1.0, 0.1, 20.0, 1.0)
    assert km[0] == pytest.approx(0.485, abs=0.005)
    assert lm[0] == pytest.approx(0.263, abs=0.001)
    assert bool(is_turing(1.0, 0.1, 20.0)) and not bool(is_turing(1.0, 0.1, 5.0))
    # below d_c every mode decays; the k = 0 mode always does
    q = np.linspace(0.0, 2.0, 200)
    assert float(np.max(growth_rate(q, 1.0, 0.1, 5.0, 1.0))) < 0
    assert float(growth_rate(0.0, 1.0, 0.1, 20.0, 1.0)) < 0


@pytest.mark.parametrize("a", [(-0.3, 0.8, -1.8, -1.2), (0.6, 1.0, -1.8, -1.0)])
def test_closed_form_expm_matches_scipy(a: tuple[float, float, float, float]) -> None:
    for t in (0.5, 4.0, 16.0):
        got = np.array(expm2(*a, t)).reshape(2, 2)
        np.testing.assert_allclose(got, expm(np.array(a).reshape(2, 2) * t), rtol=1e-9)


def test_potential_matches_the_numpy_twin_and_the_pointwise_sum(
    synthetic: tuple[xr.Dataset, dict[str, float]],
) -> None:
    data, truth = synthetic
    d = _Data.from_dataset(data)
    s = TuringPatternSettings()
    stats = d.statistics(s)
    point = {k: truth[k] for k in PARAMETERS}
    model = TuringPatternModel().build_model(stats, s)
    ip = model.initial_point()
    for name, value in point.items():
        key = next(k for k in ip if k.startswith(f"{name}_"))
        ip[key] = np.log(value / (1 - value)) if "logodds" in key else np.log(value)
    pot = float(model.compile_fn(model.potentiallogp, point_fn=True)(ip))
    logdet, quad = mode_terms(*point.values(), stats.q_group, stats.s_group, stats.times)
    n_t = len(stats.times)
    want = float(np.sum(-stats.n_group * (logdet + n_t * math.log(math.pi)) - quad))
    np.testing.assert_allclose(pot, want, rtol=1e-10)
    pw = pointwise_loglik({k: np.array([v]) for k, v in point.items()}, stats)
    assert pw.shape == (1, stats.n_modes)
    np.testing.assert_allclose(pw.sum(), want, rtol=1e-10)
    # the whitened parameterisation is the same density (plus the Jacobian of the transform)
    u = np.array([np.log(point["s"]), np.log(point["rho"] / (1 - point["rho"]))])
    u = np.concatenate([u, np.log([point[k] for k in ("d", "g", "s0", "sigma")])])
    flat = TuringPatternModel().build_whitened(stats, s, u, np.eye(len(PARAMETERS)))
    nat = float(model.compile_logp(jacobian=True)(ip))
    white = float(flat.compile_logp()({"z": np.zeros(len(PARAMETERS))}))
    assert math.isfinite(white) and math.isfinite(nat)
    other = dict(ip)
    other["d_log__"] = np.log(25.0)
    u2 = u.copy()
    u2[2] = np.log(25.0)
    nat2 = float(model.compile_logp(jacobian=True)(other))
    white2 = float(flat.compile_logp()({"z": u2 - u}))
    assert white2 - white == pytest.approx(nat2 - nat, rel=1e-8, abs=1e-6)


def test_linear_theory_predicts_a_held_out_image(
    synthetic: tuple[xr.Dataset, dict[str, float]],
) -> None:
    data, truth = synthetic
    d = _Data.from_dataset(data)
    grid = SpectralGrid.of(d.n, TURING_TRUTH["box_length"])
    p = {k: truth[k] for k in PARAMETERS}
    pred = conditional_pattern(d.images[:3], d.times[:3], [16.0], grid, p, np.random.default_rng(0))
    rmse = float(np.sqrt(np.mean((pred[0] - d.images[3]) ** 2)))
    assert rmse < 3 * truth["sigma"], f"t = 16 from t = 4..12: rmse {rmse:.2e}"
    assert rmse < 0.1 * float(d.images[3].std())


# ------------------------------------------------------------------------------ typed errors


def test_typed_refusals(synthetic: tuple[xr.Dataset, dict[str, float]]) -> None:
    data, _ = synthetic
    ds = data
    holed = ds.copy(deep=True)
    holed["activator"][1, 3, 4] = np.nan
    with pytest.raises(MapiError) as exc:
        _Data.from_dataset(holed)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(MapiError) as exc:
        _Data.from_dataset(ds.isel(y=slice(0, 30)))
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(MapiError) as exc:
        _Data.from_dataset(ds.assign_coords(t=[0.0, 4.0, 8.0, 12.0]))
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(MapiError) as exc:
        _Data.from_dataset(ds).statistics(TuringPatternSettings(k2_max=1e-4))
    assert exc.value.code is ErrorCode.SETTINGS_INVALID
    assert exc.value.pointer == "/model/settings/k2_max"


def test_score_refusals(bundle: Bundle, registry: Registry) -> None:
    data = bundle_data(bundle)
    last = float(data["t"].max())
    gap = data.isel(t=[0]).assign_coords(t=[last + 8.0]).drop_vars("activator")
    with pytest.raises(MapiError) as exc:
        _score(bundle, registry, "mode: predict\n", gap)
    assert exc.value.code is ErrorCode.NON_CONTIGUOUS_EXTENSION
    far = data.isel(t=[0, 0, 0]).assign_coords(t=last + 4.0 * np.arange(1, 4))
    with pytest.raises(MapiError) as exc:
        _score(bundle, registry, "mode: predict\n", far.drop_vars("activator"))
    assert exc.value.code is ErrorCode.HORIZON_EXCEEDS_FIT
    with pytest.raises(MapiError) as exc:
        _score(
            bundle,
            registry,
            "mode: counterfactual\ninterventions: [{var: activator, multiply: 1.1}]\n",
            data,
        )
    assert exc.value.code is ErrorCode.MODE_NOT_SUPPORTED


# ------------------------------------------------------------------------------ fit + score


def test_fit_recovers_the_kinetics(
    bundle: Bundle, synthetic: tuple[xr.Dataset, dict[str, float]]
) -> None:
    _, truth = synthetic
    post = bundle.model.posterior
    for name in ("d", "g", "a", "b", "s0", "sigma"):
        lo, hi = np.quantile(post[name].values, [0.01, 0.99])
        assert lo < truth[name] < hi, f"{name}: [{lo:.4g}, {hi:.4g}] vs {truth[name]:.4g}"
    assert float(post["p_turing"].mean()) > 0.9
    assert abs(float(post["wavelength"].median()) / truth["wavelength"] - 1) < 0.05
    assert abs(float(post["max_growth"].median()) / truth["max_growth"] - 1) < 0.02
    learned = bundle.model.learned
    assert learned["precondition"] and learned["n_modes"] > 300
    assert learned["max_relative_deviation"] < 0.1
    ll = bundle.model.groups["log_likelihood"]["activator"]
    assert ll.dims == ("chain", "draw", "mode") and ll.sizes["mode"] == learned["n_modes"]
    assert bundle.model.groups["observed_data"]["activator"].sizes["t"] == 4
    assert "prior" in bundle.model.groups


def test_holdout_and_loo_metrics(bundle: Bundle, synthetic: tuple[xr.Dataset, dict]) -> None:
    _, truth = synthetic
    values = bundle.metrics.values
    # the last image predicted from the first three by linear theory, about the camera noise
    assert values["rmse"] is not None and values["rmse"] < 3 * truth["sigma"]
    assert values["coverage_90"] is not None and values["coverage_90"] > 0.7
    assert values["elpd_loo"] is not None and math.isfinite(values["elpd_loo"])
    detail = bundle.metrics.elpd["elpd_loo"]
    assert detail.n == bundle.model.learned["n_modes"]


def test_predict_draws_shapes_and_seed(bundle: Bundle, registry: Registry) -> None:
    data = bundle_data(bundle)
    a = _predictions(_score(bundle, registry, "mode: predict\nseed: 4\n", data))
    b = _predictions(_score(bundle, registry, "mode: predict\nseed: 4\n", data))
    c = _predictions(_score(bundle, registry, "mode: predict\nseed: 5\n", data))
    xr.testing.assert_identical(a, b)
    assert not np.allclose(a["pattern"].values, c["pattern"].values)
    # field_draws 8 over 2 chains -> 4 evenly spaced draws per chain
    assert dict(a["pattern"].sizes) == {"chain": 2, "draw": 4, "t": 4, "y": 32, "x": 32}
    assert a["pattern"].dtype == np.float32
    assert a["growth"].dims == ("chain", "draw", "wavenumber")
    assert a["turing_space"].dims == ("chain", "draw", "b_supply", "a_supply")
    assert "settled" not in a
    # a pixel subset selects the same draws (the field is drawn on the whole raster)
    sub = data.isel(y=slice(2, 7), x=[0, 9, 31])
    s = _predictions(_score(bundle, registry, "mode: predict\nseed: 4\n", sub))
    want = a["pattern"].sel(y=sub["y"].values, x=sub["x"].values)
    np.testing.assert_array_equal(s["pattern"].values, want.values)


def test_predictions_track_the_images_and_predictive_adds_noise(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, dict[str, float]]
) -> None:
    data, truth = synthetic
    exp = _predictions(_score(bundle, registry, "mode: predict\nseed: 2\n", data))
    pre = _predictions(
        _score(bundle, registry, "mode: predict\nseed: 2\noutput: {kind: predictive}\n", data)
    )
    obs = data["activator"]
    err = exp["pattern"].astype(float).mean(("chain", "draw")) - obs
    assert float(np.sqrt((err**2).mean())) < 2 * truth["sigma"]
    noise = (pre["pattern"] - exp["pattern"]).astype(float)
    assert abs(float(noise.std()) / truth["sigma"] - 1) < 0.15


def test_in_sample_evaluate(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, dict[str, float]]
) -> None:
    data, truth = synthetic
    tree = score_bundle(
        bundle,
        load_score_request(HEADER + "mode: evaluate\ndata: dataset://science/t@v1\nseed: 3\n"),
        data,
        registry=registry,
        metrics=["rmse", "mae"],
    )
    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["rmse"] < 2 * truth["sigma"]


def test_simulate_grows_new_patterns(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, dict[str, float]]
) -> None:
    data, _ = synthetic
    pred = _predictions(_score(bundle, registry, "mode: simulate\nseed: 7\n", data))
    # simulate_draws 2 over 2 chains -> one run per chain
    assert dict(pred["pattern"].sizes) == {"chain": 2, "draw": 1, "t": 4, "y": 32, "x": 32}
    assert pred["pattern"].attrs["kind"] == "predictive"
    sim = pred["pattern"].astype(float)
    obs = data["activator"]
    # a new pattern: same statistics as the data, other spots
    ratio = float(sim.isel(t=-1).std(("y", "x")).mean() / obs.isel(t=-1).std())
    assert 0.3 < ratio < 3.0
    r = np.corrcoef(sim.isel(chain=0, draw=0, t=-1).values.ravel(), obs[-1].values.ravel())
    assert abs(r[0, 1]) < 0.5
    settled = pred["settled"].astype(float)
    assert settled.dims == ("chain", "draw", "y", "x")
    assert float(settled.std(("y", "x")).min()) > 5 * float(obs.isel(t=-1).std())
    again = _predictions(_score(bundle, registry, "mode: simulate\nseed: 7\n", data))
    xr.testing.assert_identical(pred, again)


def test_contract_kit(registry: Registry, synthetic: tuple[xr.Dataset, dict[str, float]]) -> None:
    data, _ = synthetic
    report = check_model(
        TuringPatternModel,
        ModelFixture(spec=TURING_SPEC_YAML, data=data, raw_schema=TURING_SCHEMA, registry=registry),
        raise_on_failure=False,
    )
    failed = [(r.name, r.detail) for r in report.results if not r.passed]
    assert not failed, failed
    passed = {r.name for r in report.results if r.passed and not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "unsupported_mode",
        "evaluate_path_equals_score_path",
        "output_space_and_units",
        "outputs_match_manifest",
        "seed_determinism",
        "plots_sanitize",
    } <= passed


# ------------------------------------------------------------------------------ real data


def test_part_a_reads_no_data_file() -> None:
    assert CHALLENGES_FILE is None
    with pytest.raises(ValueError, match="activator"):
        from_challenges(_write_bad())


@pytest.mark.skipif(REAL is None, reason="pymc-challenges data are not configured")
def test_from_challenges_repeats_the_notebook_simulation() -> None:
    assert REAL is not None
    ds = from_challenges(REAL)
    assert dict(ds["activator"].sizes) == {"t": 4, "y": 128, "x": 128}
    np.testing.assert_array_equal(ds["t"].values, [4.0, 8.0, 12.0, 16.0])
    d = _Data.from_dataset(ds)
    # the notebook: the field stays within about +-2% of the uniform state
    assert d.max_relative_deviation < 0.05
    stats = d.statistics(TuringPatternSettings())
    assert 400 < stats.n_modes < 700
    sd = ds["activator"].std(("y", "x")).values
    assert np.all(np.diff(sd) > 0), "the pattern grows"


@pytest.mark.slow
@pytest.mark.skipif(REAL is None, reason="pymc-challenges data are not configured")
def test_notebook_fit_recovers_the_kinetics(registry: Registry) -> None:
    assert REAL is not None
    ds = from_challenges(REAL)
    spec = NOTEBOOK_SPEC_YAML.replace("tune: 1500", "tune: 500").replace(
        "draws: 1000", "draws: 500"
    )
    spec = spec.replace("chains: 4", "chains: 2")
    b = fit_spec(spec, ds, registry=registry, raw_schema=TURING_SCHEMA)
    post = b.model.posterior
    ref = challenges_reference()
    for name, (lo_nb, hi_nb) in ref["eti89"].items():
        lo, hi = np.quantile(post[name].values, [0.055, 0.945])
        assert lo < ref["truth"][name] < hi, f"{name}: [{lo:.3g}, {hi:.3g}]"
        # the interval overlaps the notebook's
        assert lo < hi_nb and lo_nb < hi, f"{name}: [{lo:.3g}, {hi:.3g}] vs notebook"
    q = np.quantile(post["max_growth"].values, [0.05, 0.95])
    assert q[0] < ref["truth_max_growth"] < q[1] + 0.002
    assert float(post["p_turing"].mean()) > 0.95


# ------------------------------------------------------------------------------ helpers


def bundle_data(bundle: Bundle) -> xr.Dataset:
    """The fit data back from the bundle, under the raw names of the spec."""
    obs = bundle.model.groups["observed_data"]["activator"]
    return obs.to_dataset(name="activator")


def _write_bad() -> str:
    import tempfile

    path = os.path.join(tempfile.mkdtemp(), "bad.nc")
    xr.Dataset({"other": ("t", np.arange(3.0))}).to_netcdf(path, engine="h5netcdf")
    return path
