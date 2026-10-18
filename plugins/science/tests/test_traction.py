"""``sci.traction_force`` (E60): Fourier maths, fit, exact field draws, recovery, contract kit."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.traction import (
    FORCE_MASK,
    FourierGrid,
    TractionForceModel,
    TractionForceSettings,
    _Data,
    forward,
    from_modes,
    green,
    to_modes,
)
from mapi_plugin_science.traction_data import (
    CHALLENGES_FILE,
    TRACTION_SCHEMA,
    TRACTION_SPEC_YAML,
    TRACTION_TRUTH,
    challenges_reference,
    challenges_settings,
    from_challenges,
    simulate_traction,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import ErrorCode, MapiError, load_model_spec, load_score_request, validate_spec
from mapi_spec.capabilities import GridDecl

CHALLENGES = challenges_file(CHALLENGES_FILE)
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://science/tfm\n"


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(TractionForceModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def synthetic() -> tuple[xr.Dataset, xr.Dataset]:
    return simulate_traction(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]) -> Bundle:
    data, _ = synthetic
    return fit_spec(TRACTION_SPEC_YAML, data, registry=registry, raw_schema=TRACTION_SCHEMA)


def _score(bundle: Bundle, registry: Registry, body: str, data: xr.Dataset) -> xr.DataTree:
    req = load_score_request(HEADER + body)
    return score_bundle(bundle, req, data, registry=registry, return_full=True)


def _predictions(tree: xr.DataTree) -> xr.Dataset:
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


# ------------------------------------------------------------------------------ manifest


def test_manifest_declares_fields_modes_and_no_loo() -> None:
    m = TractionForceModel.manifest()
    caps = m.capabilities
    assert caps.score_modes == frozenset({"predict", "evaluate"})
    assert not caps.loo_valid and caps.loo_invalid_reason
    assert caps.deterministic_given_seed
    trac = m.outputs["traction"]
    assert trac.units == "Pa" and isinstance(trac.grid, GridDecl)
    assert (trac.grid.y, trac.grid.x, trac.grid.component) == ("row", "col", "component")
    assert trac.grid.colormap == "magma"
    assert m.outputs["displacement_hat"].units_of == "displacement"
    assert m.outputs["strain_energy"].dims == [] and m.outputs["contractility"].units == "uN"
    assert set(m.plots) == {"core.convergence", "core.posterior_forest", "sci.vector_field"}


def test_schema_template_adds_the_force_mask_only_when_asked() -> None:
    template = TractionForceModel.manifest().schema_template
    plain = template.resolve({})
    assert set(plain.vars) == {"displacement"}
    assert plain.vars["displacement"].dims == ("component", "row", "col")
    assert all(d.policy.value == "fixed" for d in plain.dims.values())
    masked = template.resolve({"force_mask": True})
    assert masked.vars[FORCE_MASK].role.value == "mask"


def test_spec_validates_statically(registry: Registry) -> None:
    res = validate_spec(load_model_spec(TRACTION_SPEC_YAML), registry.catalog(), TRACTION_SCHEMA)
    assert res.ok, [i.message for i in res.errors]


# ------------------------------------------------------------------------------ maths


def test_modes_round_trip_and_green_matches_the_white_prior_closed_form() -> None:
    rng = np.random.default_rng(1)
    g = FourierGrid.of(12, 10, 0.5)
    fx, fy = rng.standard_normal((2, 12, 10))
    fx, fy = fx - fx.mean(), fy - fy.mean()
    bx, by = from_modes(*to_modes(fx, fy, g), g)
    np.testing.assert_allclose(bx, fx, atol=1e-12)
    np.testing.assert_allclose(by, fy, atol=1e-12)
    gl, gt = green(g, 8000.0, 0.49)
    np.testing.assert_allclose(gl[g.keep] / gt[g.keep], 1 - 0.49)
    # a stiffer gel moves less, in proportion
    ux, _ = forward(fx, fy, g, 8000.0, 0.49)
    ux2, _ = forward(fx, fy, g, 16000.0, 0.49)
    np.testing.assert_allclose(ux2, ux / 2, atol=1e-15)


def test_marginal_likelihood_graph_matches_numpy(synthetic: tuple[xr.Dataset, xr.Dataset]) -> None:
    data, _ = synthetic
    s = TractionForceSettings(grid_um=1.0, young_pa=8000.0, prior="matern")
    d = _Data.from_dataset(data, s)
    model = TractionForceModel().build_model(d, s)
    assert {rv.name for rv in model.free_RVs} == {
        "noise_sd",
        "traction_sd",
        "lengthscale",
        "smoothness",
    }
    point = {"noise_sd": 0.01, "traction_sd": 400.0, "lengthscale": 3.0, "smoothness": 1.5}
    ip = model.initial_point()
    for name, value in point.items():
        ip[f"{name}_log__"] = np.log(value)
    pot = model.compile_fn(model.potentiallogp, point_fn=True)(ip)
    gl, gt = green(d.grid, 8000.0, 0.49)
    k = d.grid.k
    s2 = 400.0**2 * (1 + (k * 3.0) ** 2) ** -1.5
    want = 0.0
    for gk, u in ((gl, d.ul), (gt, d.ut)):
        c = (gk**2 * s2 + 0.01**2)[d.grid.keep]
        want -= 0.5 * np.sum(np.abs(u[d.grid.keep]) ** 2 / c + np.log(2 * np.pi * c))
    np.testing.assert_allclose(float(pot), want, rtol=1e-9)
    white = TractionForceModel().build_model(d, s.model_copy(update={"prior": "white"}))
    assert {rv.name for rv in white.free_RVs} == {"noise_sd", "traction_sd"}


def test_missing_displacements_are_refused(synthetic: tuple[xr.Dataset, xr.Dataset]) -> None:
    data, _ = synthetic
    holed = data.copy(deep=True)
    holed["displacement"][0, 3, 4] = np.nan
    with pytest.raises(MapiError) as exc:
        _Data.from_dataset(holed, TractionForceSettings())
    assert exc.value.code is ErrorCode.SCHEMA_INVALID


# ------------------------------------------------------------------------------ fit + score


def test_fit_recovers_the_hyperparameters(bundle: Bundle) -> None:
    post = bundle.model.posterior
    assert set(post.data_vars) == {"noise_sd", "traction_sd", "lengthscale", "smoothness"}
    noise = float(post["noise_sd"].mean())
    assert abs(noise / TRACTION_TRUTH["noise_sd"] - 1) < 0.1
    for name in ("lengthscale", "smoothness"):
        lo, hi = np.quantile(post[name].values, [0.01, 0.99])
        assert lo < TRACTION_TRUTH[name] < hi, name
    groups = bundle.model.groups
    assert groups["observed_data"]["displacement"].shape == (2, 32, 32)
    assert int(groups["constant_data"][FORCE_MASK].sum()) > 100
    assert "prior" in groups and "log_likelihood" not in groups
    codes = {d.code for d in bundle.diagnostics}
    assert "DIVERGENCES" in codes and "NOISE_SPECTRUM_MISMATCH" not in codes
    assert bundle.model.learned["modes"] == 2 * (32 * 32 - 1)


def test_field_draws_have_the_declared_shapes(bundle: Bundle, registry: Registry) -> None:
    data = bundle_data(bundle)
    tree = _score(bundle, registry, "mode: predict\nseed: 5\noutput: {draws: 6}\n", data)
    pred = _predictions(tree)
    # field_draws: 40 over 2 chains -> 20 evenly spaced draws per chain
    assert dict(pred["traction"].sizes) == {
        "chain": 2,
        "draw": 20,
        "component": 2,
        "row": 32,
        "col": 32,
    }
    assert pred["traction"].dtype == np.float32
    assert pred["strain_energy"].dims == ("chain", "draw")
    assert int(pred["draw"].values[-1]) == 199
    assert tree["draws"]["traction"].sizes["sample"] == 6
    assert "epicentre_x_um" in pred["contractility"].attrs
    summary = tree["summary"].to_dataset()
    assert summary["traction"].attrs["units"] == "Pa"


def test_recovery_of_the_traction_field(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, truth = synthetic
    pred = _predictions(_score(bundle, registry, "mode: predict\nseed: 1\n", data))
    t = pred["traction"].astype(np.float64)
    mean = t.mean(("chain", "draw"))
    true = truth["traction"]
    r = np.corrcoef(mean.values.ravel(), true.values.ravel())[0, 1]
    assert r > 0.8, f"posterior mean vs truth correlation {r:.2f}"
    lo, hi = t.quantile([0.05, 0.95], dim=("chain", "draw"))
    cover = float(((true >= lo) & (true <= hi)).mean())
    assert 0.8 <= cover <= 0.98, f"90% pixelwise coverage {cover:.2f}"
    # linear summaries are close to the truth (strain energy is biased by the smoothing)
    c = pred["contractility"].values
    assert abs(c.mean() / float(truth["contractility"]) - 1) < 0.15
    e = pred["strain_energy"].values
    assert abs(e.mean() / float(truth["strain_energy"]) - 1) < 0.15


def test_field_draws_are_deterministic_given_the_seed(bundle: Bundle, registry: Registry) -> None:
    data = bundle_data(bundle)
    a = _predictions(_score(bundle, registry, "mode: predict\nseed: 4\n", data))
    b = _predictions(_score(bundle, registry, "mode: predict\nseed: 4\n", data))
    c = _predictions(_score(bundle, registry, "mode: predict\nseed: 5\n", data))
    xr.testing.assert_identical(a, b)
    assert not np.allclose(a["traction"].values, c["traction"].values)
    # a subset of pixels selects the same draws (the field is drawn on the fit grid)
    sub = data.isel(row=slice(4, 9), col=[0, 7, 30])
    s = _predictions(_score(bundle, registry, "mode: predict\nseed: 4\n", sub))
    want = a["traction"].sel(row=sub["row"].values, col=sub["col"].values)
    np.testing.assert_array_equal(s["traction"].values, want.values)
    np.testing.assert_array_equal(s["strain_energy"].values, a["strain_energy"].values)


def test_predictive_displacements_add_the_noise(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, truth = synthetic
    exp = _predictions(_score(bundle, registry, "mode: predict\nseed: 2\n", data))
    pre = _predictions(
        _score(bundle, registry, "mode: predict\nseed: 2\noutput: {kind: predictive}\n", data)
    )
    np.testing.assert_array_equal(exp["traction"].values, pre["traction"].values)
    resid = (pre["displacement_hat"] - exp["displacement_hat"]).values
    assert abs(resid.std() / TRACTION_TRUTH["noise_sd"] - 1) < 0.1
    # the expected displacement (plus drift) tracks the noiseless field
    drift = np.asarray(TRACTION_TRUTH["drift_um"])[:, None, None]
    err = exp["displacement_hat"].mean(("chain", "draw")).values - truth["displacement"].values
    assert np.sqrt(np.mean((err - drift) ** 2)) < TRACTION_TRUTH["noise_sd"]


def test_in_sample_evaluate_compares_displacements(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = synthetic
    tree = score_bundle(
        bundle,
        load_score_request(HEADER + "mode: evaluate\ndata: dataset://science/tfm@v1\nseed: 3\n"),
        data,
        registry=registry,
        metrics=["rmse", "mae"],
    )
    values = json.loads(tree.attrs["metrics"])["values"]
    # the posterior mean displacement leaves about the tracking noise
    assert 0.5 * TRACTION_TRUTH["noise_sd"] < values["rmse"] < 1.5 * TRACTION_TRUTH["noise_sd"]


def test_contract_kit(registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]) -> None:
    data, _ = synthetic
    report = check_model(
        TractionForceModel,
        ModelFixture(
            spec=TRACTION_SPEC_YAML, data=data, raw_schema=TRACTION_SCHEMA, registry=registry
        ),
    )
    passed = {r.name for r in report.results if r.passed and not r.skipped}
    assert {
        "bundle_roundtrip_no_pickle",
        "shuffled_scoring",
        "subset_scoring",
        "new_level_policies",
        "unsupported_mode",
        "output_space_and_units",
        "outputs_match_manifest",
        "seed_determinism",
        "plots_sanitize",
    } <= passed


def test_young_modulus_uncertainty_scales_the_tractions(
    registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = synthetic
    spec = TRACTION_SPEC_YAML.replace("field_draws: 40", "field_draws: 10\n    young_cv: 0.15")
    spec = spec.replace("tune: 300, draws: 200", "tune: 150, draws: 60")
    b = fit_spec(spec, data, registry=registry, raw_schema=TRACTION_SCHEMA)
    young = b.model.posterior["young_pa"]
    assert young.dims == ("chain", "draw")
    assert 0.08 < float(np.log(young).std()) < 0.25
    fixed = TRACTION_SPEC_YAML.replace("field_draws: 40", "field_draws: 10")
    fixed = fixed.replace("tune: 300, draws: 200", "tune: 150, draws: 60")
    b0 = fit_spec(fixed, data, registry=registry, raw_schema=TRACTION_SCHEMA)
    p = _predictions(_score(b, registry, "mode: predict\nseed: 1\n", data))
    p0 = _predictions(_score(b0, registry, "mode: predict\nseed: 1\n", data))
    factor = young.sel(chain=p["chain"], draw=p["draw"]) / 8000.0
    np.testing.assert_allclose(p["traction"], p0["traction"] * factor, rtol=1e-5, atol=1e-3)
    np.testing.assert_allclose(p["displacement_hat"], p0["displacement_hat"], rtol=1e-6)


# ------------------------------------------------------------------------------ real data


@pytest.mark.skipif(CHALLENGES is None, reason="pymc-challenges data are not present")
def test_from_challenges_reads_the_pytfm_colony() -> None:
    assert CHALLENGES is not None
    ds = from_challenges(CHALLENGES)
    assert dict(ds["displacement"].sizes) == {"component": 2, "row": 189, "col": 193}
    assert ds["displacement"].dtype == np.float64
    assert ds[FORCE_MASK].dtype == bool and 0 < int(ds[FORCE_MASK].sum()) < 189 * 193
    assert ds.attrs["young_pa"] == 49_000.0
    settings = TractionForceSettings.model_validate(challenges_settings(ds))
    assert settings.force_mask and settings.prior == "matern"
    ref = challenges_reference(CHALLENGES.parent)
    assert ref["traction"].shape == (2, 189, 193)
    assert 200 < ref.attrs["strain_energy_fj"] < 300
    d = _Data.from_dataset(ds, settings)
    assert d.has_mask and d.grid.k.shape == (189, 193)


def bundle_data(bundle: Bundle) -> xr.Dataset:
    """The fit data back from the bundle (dataset names equal canonical names here)."""
    obs: Any = bundle.model.groups["observed_data"]["displacement"]
    mask = bundle.model.groups["constant_data"][FORCE_MASK]
    return xr.Dataset({"displacement": obs, FORCE_MASK: mask})
