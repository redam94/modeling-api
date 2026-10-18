"""``sci.strong_lens`` (E80): lens physics, the PyTensor forward model, fit, score, contract kit."""

from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytensor
import pytensor.tensor as pt
import pytest
import xarray as xr
from mapi_plugin_science import lens_math as lm
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.lens import (
    CRITICAL_CURVES,
    Compiled,
    LensLayout,
    Problem,
    StrongLensModel,
    StrongLensSettings,
    amplitude_stats,
    components,
    marginal_loglik,
)
from mapi_plugin_science.lens_data import (
    CHALLENGES_NPZ,
    LENS_SCHEMA,
    LENS_SPEC_YAML,
    LENS_TRUTH,
    from_challenges,
    gaussian_psf,
    lens_dataset,
    render_lens,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.models import ScoreContext
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_score_request
from scipy import signal

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://astronomy/lens\n"
MASS = (1.2, 0.15, -0.08, 0.05, -0.03, 0.03, -0.02)  # theta_E, e1, e2, x0, y0, g1, g2


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(StrongLensModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def mock() -> xr.Dataset:
    return lens_dataset(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, mock: xr.Dataset) -> Bundle:
    return fit_spec(LENS_SPEC_YAML, mock, registry=registry, raw_schema=LENS_SCHEMA)


def _jacobian(x: np.ndarray, y: np.ndarray, h: float = 1e-5) -> np.ndarray:
    """Finite-difference ``d alpha / d theta`` of :data:`MASS`, shape ``(n, 2, 2)``."""
    ax1, ay1 = lm.deflection(x + h, y, *MASS)
    ax0, ay0 = lm.deflection(x - h, y, *MASS)
    bx1, by1 = lm.deflection(x, y + h, *MASS)
    bx0, by0 = lm.deflection(x, y - h, *MASS)
    return np.stack(
        [
            np.stack([(ax1 - ax0) / (2 * h), (bx1 - bx0) / (2 * h)], -1),
            np.stack([(ay1 - ay0) / (2 * h), (by1 - by0) / (2 * h)], -1),
        ],
        -2,
    )


# ============================================================================ manifest


def test_manifest_declares_grids_derived_and_no_loo() -> None:
    m = StrongLensModel.manifest()
    assert m.outputs["model_image"].grid is not None
    assert (m.outputs["model_image"].grid.y, m.outputs["model_image"].grid.x) == ("row", "col")
    assert m.outputs["model_image"].grid.colormap == "gray"
    assert m.outputs["source"].grid is not None and m.outputs["source"].grid.colormap == "magma"
    assert m.outputs["model_image"].units_of == "image"
    assert {d.name for d in m.derived} == {CRITICAL_CURVES}
    caps = m.capabilities
    assert caps.score_modes == {"predict", "evaluate", "simulate"}
    assert not caps.loo_valid and caps.loo_invalid_reason
    assert set(m.plots) == {"core.convergence", "core.posterior_forest", "sci.field_map"}
    plain = m.schema_template.resolve({})
    assert plain.vars["psf"].role.value == "constant" and "mask" not in plain.vars
    assert m.schema_template.resolve({"use_mask": True}).vars["mask"].role.value == "mask"


def test_settings_need_both_redshifts() -> None:
    with pytest.raises(ValueError, match="z_lens and z_source"):
        StrongLensSettings(z_lens=0.2)
    with pytest.raises(ValueError, match="larger"):
        StrongLensSettings(z_lens=0.5, z_source=0.3)


# ============================================================================ physics


def test_round_sie_deflects_by_theta_e_towards_the_centre() -> None:
    rng = np.random.default_rng(0)
    x, y = rng.uniform(-2, 2, (2, 200))
    ax, ay = lm.sie_deflection(x, y, 1.2, 1e-6, 0.0, 0.0, 0.0)
    np.testing.assert_allclose(np.hypot(ax, ay), 1.2, rtol=1e-5)
    np.testing.assert_allclose(ax * y - ay * x, 0.0, atol=1e-5)  # radial


def test_sie_divergence_is_twice_kappa_and_curl_free() -> None:
    rng = np.random.default_rng(1)
    x, y = rng.uniform(-2, 2, (2, 200))
    jac = _jacobian(x, y)
    kappa = lm.sie_kappa(x, y, *MASS[:5])
    div = jac[:, 0, 0] + jac[:, 1, 1]
    np.testing.assert_allclose(div / 2, kappa, rtol=1e-4)  # shear is trace-free
    np.testing.assert_allclose(jac[:, 1, 0], jac[:, 0, 1], atol=1e-5)


def test_sis_critical_curve_is_the_einstein_circle() -> None:
    cc = lm.critical_curves(0.9, 0.0, 0.0, 0.1, -0.2, n=64)
    np.testing.assert_allclose(np.hypot(cc.critical_x - 0.1, cc.critical_y + 0.2), 0.9, rtol=1e-6)
    np.testing.assert_allclose(cc.caustic_x, 0.1, atol=1e-6)  # the caustic of an SIS is a point
    np.testing.assert_allclose(np.hypot(cc.cut_x - 0.1, cc.cut_y + 0.2), 0.9, rtol=1e-4)


def test_critical_curve_has_zero_jacobian_determinant() -> None:
    cc = lm.critical_curves(*MASS, n=96)
    jac = _jacobian(cc.critical_x, cc.critical_y)
    det = (1 - jac[:, 0, 0]) * (1 - jac[:, 1, 1]) - jac[:, 0, 1] * jac[:, 1, 0]
    np.testing.assert_allclose(det, 0.0, atol=1e-7)


def test_psf_convolution_matches_scipy_and_conserves_flux() -> None:
    rng = np.random.default_rng(2)
    yy, xx = np.mgrid[-5:6, -5:6]
    psf = np.exp(-(xx**2 + 0.6 * yy**2) / 4) * (1 + 0.3 * np.cos(np.arctan2(yy, xx) * 2))
    psf /= psf.sum()
    img = rng.random((30, 26))
    exact = signal.convolve2d(img, psf, mode="same")
    f = lm.psf_factors(psf, 30, 26, tol=1e-12, max_rank=11)
    np.testing.assert_allclose(lm.convolve(img, f), exact, atol=1e-12)
    point = np.zeros((30, 26))
    point[15, 13] = 1.0
    g = lm.psf_factors(gaussian_psf(2.0, 9), 30, 26)
    assert g.rank == 1 and g.error < 1e-12  # a Gaussian is separable
    np.testing.assert_allclose(lm.convolve(point, g).sum(), 1.0, rtol=1e-12)


def test_whitening_inverts_the_separable_correlation() -> None:
    w, lo = lm.whitening(12, 0.2)
    k = np.eye(12) + 0.2 * (np.eye(12, k=1) + np.eye(12, k=-1))
    np.testing.assert_allclose(w @ k @ w.T, np.eye(12), atol=1e-12)
    np.testing.assert_allclose(lo @ lo.T, k, atol=1e-12)
    with pytest.raises(ValueError):
        lm.whitening(5, 0.6)


def test_round_lens_on_axis_gives_a_symmetric_ring() -> None:
    truth = {
        **LENS_TRUTH,
        "e_mass": (0.0, 0.0),
        "xy_mass": (0.0, 0.0),
        "e_src": (0.0, 0.0),
        "xy_src": (0.0, 0.0),
        "xy_light": (0.0, 0.0),
        "e_light": ((0.0, 0.0), (0.0, 0.0)),
    }
    img = render_lens(truth, 20, 20, gaussian_psf(), pixel_scale=0.1, shear=False)
    # exact up to the 1e-7 ellipticity floor that keeps the round SIE regular
    np.testing.assert_allclose(img, img.T, rtol=1e-5)
    np.testing.assert_allclose(img, img[::-1], rtol=1e-5)
    np.testing.assert_allclose(img, img[:, ::-1], rtol=1e-5)


def test_einstein_mass_of_slacs_j1627() -> None:
    # E80: theta_E = 1.2231" at z = 0.2076 / 0.5241 is 2.26e11 solar masses
    assert lm.einstein_mass(1.2231, 0.2076, 0.5241) / 1e11 == pytest.approx(2.258, abs=0.005)


# ============================================================================ forward model


def _problem(ds: xr.Dataset, s: StrongLensSettings) -> Problem:
    return Problem.build(ds["image"].values, ds["weight"].values, ds["psf"].values, s, mask=None)


def test_pytensor_forward_matches_numpy_and_marginal_loglik() -> None:
    ds = lens_dataset(size=16, seed=4)
    s = StrongLensSettings(
        pixel_scale=0.1, subsample=2, lens_light_components=1, noise_correlation=0.2
    )
    prob, layout = _problem(ds, s), LensLayout.from_settings(s)
    vals = {
        "xy_light": np.array([0.0, 0.01]),
        "R_light_0": 0.4,
        "n_light_0": 2.0,
        "e_light_0": np.array([0.05, 0.0]),
        "theta_E": 0.7,
        "e_mass": np.array([0.05, -0.02]),
        "xy_mass": np.array([0.01, 0.0]),
        "shear": np.array([0.01, 0.02]),
        "R_src": 0.12,
        "n_src": 1.2,
        "e_src": np.array([0.1, 0.0]),
        "xy_src": np.array([0.05, -0.03]),
        "noise_scale": 1.1,
    }
    want = np.stack(components(vals, prob, layout, np))
    got = Compiled(prob, layout).forward_values(vals)
    np.testing.assert_allclose(got, want, rtol=1e-10, atol=1e-12)
    # the closed-form marginal agrees with the NumPy least-squares solution
    u = pt.vector("u")
    p = layout.values(u, pt)
    b = prob.whiten(prob.image * prob.isig).ravel()
    ll = marginal_loglik(
        components(p, prob, layout, pt),
        prob,
        pt.constant(prob.isig),
        pt.constant(b),
        float(b @ b),
        float(prob.n_valid),
        p["noise_scale"],
    )
    value = float(pytensor.function([u], ll)(layout.pack(vals)))
    a_hat, lc, rr = amplitude_stats(want, prob)
    k = want.shape[0]
    expect = -0.5 * rr / 1.1**2 - (prob.n_valid - k) * np.log(1.1) - np.log(np.diag(lc)).sum()
    assert value == pytest.approx(expect, rel=1e-8)
    assert a_hat.shape == (k,)


def test_layout_round_trips_physical_values() -> None:
    layout = LensLayout.from_settings(StrongLensSettings(lens_light_components=2))
    u = np.random.default_rng(5).normal(0, 0.3, layout.size)
    vals = layout.values(u, np)
    np.testing.assert_allclose(layout.pack(vals), u, atol=1e-9)
    assert 0.5 < vals["n_src"] < 8.0 and np.all(np.abs(vals["e_mass"]) < 0.7)


# ============================================================================ fit and score


def test_fit_recovers_the_einstein_radius(bundle: Bundle) -> None:
    post = bundle.model.posterior
    te = post["theta_E"].values
    assert abs(te.mean() - LENS_TRUTH["theta_E"]) < 0.02
    assert abs(post["xy_src"].mean(("chain", "draw")).values - LENS_TRUTH["xy_src"]).max() < 0.03
    assert set(post["amplitude"]["component"].values) == {"lens_light_1", "source", "sky"}
    amp = post["amplitude"].sel(component="source").mean().item()
    assert amp == pytest.approx(LENS_TRUTH["amplitude"]["source"], rel=0.25)
    learned = bundle.model.learned
    assert learned["psf_rank"] == 1 and len(learned["mode"]["starts"]) == 2
    assert {"rmse", "mae", "coverage_90"} <= set(bundle.metrics.values)
    assert bundle.metrics.values["coverage_90"] > 0.6  # predictive noise covers held-out pixels


def test_fit_caches_critical_curves(bundle: Bundle) -> None:
    cc = bundle.model.derived[CRITICAL_CURVES]
    assert dict(cc.sizes) == {"sample": 20, "curve_point": 64}
    assert {"critical_x", "caustic_x", "cut_x", "critical_col", "caustic_row"} <= set(cc.data_vars)
    radius = np.hypot(cc["critical_x"], cc["critical_y"]).mean().item()
    assert radius == pytest.approx(LENS_TRUTH["theta_E"], rel=0.1)
    # pixel indices of the image grid: the centre of a 24-pixel side is 11.5
    np.testing.assert_allclose(cc["critical_col"], cc["critical_x"] / 0.1 + 11.5, rtol=1e-12)


def test_score_returns_image_source_and_radius(
    bundle: Bundle, registry: Registry, mock: xr.Dataset
) -> None:
    req = load_score_request(HEADER + "mode: predict\noutput: {draws: 5}\n")
    tree = score_bundle(bundle, req, mock, registry=registry, return_full=True)
    pred = tree["predictions"].to_dataset()
    assert dict(pred["model_image"].sizes) == {"chain": 2, "draw": 100, "row": 24, "col": 24}
    assert dict(pred["source"].sizes) == {
        "chain": 2,
        "draw": 100,
        "source_row": 16,
        "source_col": 16,
    }
    assert pred["source"].mean(("chain", "draw")).max().item() > 0.5
    assert "einstein_mass" not in pred
    resid = (mock["image"] - pred["model_image"].mean(("chain", "draw"))) * np.sqrt(mock["weight"])
    assert float(resid.std()) < 1.5
    assert "draws" in tree.children and "model_image" in tree["draws"].to_dataset()


def test_simulate_adds_noise_and_mass_needs_redshifts(bundle: Bundle, mock: xr.Dataset) -> None:
    plugin = StrongLensModel()
    canon = mock.drop_vars("image")
    req = load_score_request(HEADER + "mode: simulate\n")
    exp = plugin.score(bundle.model, canon, req, ScoreContext(mode="predict", seed=1))
    sim = plugin.score(bundle.model, canon, req, ScoreContext(mode="simulate", seed=1))
    again = plugin.score(bundle.model, canon, req, ScoreContext(mode="simulate", seed=1))
    e, s_ = exp["predictions"]["model_image"], sim["predictions"]["model_image"]
    xr.testing.assert_identical(s_, again["predictions"]["model_image"])  # seeded
    z = ((s_ - e) * np.sqrt(mock["weight"])).values
    assert 0.7 < z.std() < 1.5
    fitted = replace(
        bundle.model, settings={**bundle.model.settings, "z_lens": 0.2, "z_source": 0.6}
    )
    out = plugin.score(fitted, canon, req, ScoreContext(seed=1))["predictions"]
    mass = out["einstein_mass"].mean().item()
    assert mass == pytest.approx(float(lm.einstein_mass(0.7, 0.2, 0.6)), rel=0.05)


def test_contract_kit(registry: Registry, mock: xr.Dataset) -> None:
    report = check_model(
        StrongLensModel,
        ModelFixture(LENS_SPEC_YAML, mock, raw_schema=LENS_SCHEMA, registry=registry),
    )
    skipped = {r.name: r.detail for r in report.results if r.skipped}
    # no categorical or extend dims (an image is fixed rows x cols), no forecast and no
    # ordered holdout (the split masks pixel blocks), and nutpie is not seed-deterministic
    assert "seed_determinism" in skipped and "extend_typed_errors" in skipped
    ran = {r.name: r.detail for r in report.results if not r.skipped}
    assert {"bundle_roundtrip_no_pickle", "outputs_match_manifest", "plots_sanitize"} <= set(ran)
    plots = ran["plots_sanitize"] or ""
    assert "sci.field_map" in plots.split("not applicable")[0], plots


# ============================================================================ real data


@pytest.mark.skipif(
    challenges_file(CHALLENGES_NPZ) is None, reason="pymc-challenges data not present"
)
def test_from_challenges_matches_the_notebook() -> None:
    path = challenges_file(CHALLENGES_NPZ)
    assert path is not None
    ds = from_challenges(path)
    assert dict(ds.sizes) == {"row": 81, "col": 81, "psf_row": 31, "psf_col": 31}
    assert float(ds["psf"].sum()) == pytest.approx(1.0)
    assert 0.12 < ds["psf"].max().item() < 0.17  # "a seventh of the light in the brightest pixel"
    assert bool((ds["weight"] > 0).all())
    assert 0.1 < ds.attrs["noise_correlation"] < 0.3
    assert ds.attrs["pixel_scale"] == pytest.approx(0.05)
    f = lm.psf_factors(ds["psf"].values, 81, 81, tol=1e-3)
    assert f.rank <= 12 and f.error < 2e-3
    json.dumps({k: v for k, v in ds.attrs.items()})  # attrs stay JSON friendly


@pytest.mark.slow
def test_realistic_mock_with_nutpie(registry: Registry) -> None:
    """81 x 81 mock at HST sampling, two light components, shear, 2 x 2 rays, nutpie."""
    pytest.importorskip("nutpie")
    truth = {**LENS_TRUTH, "theta_E": 1.2, "xy_src": (0.05, -0.15), "R_light": (0.5, 1.5)}
    ds = lens_dataset(
        size=81,
        pixel_scale=0.05,
        light_components=2,
        shear=True,
        psf_fwhm_pixels=2.0,
        psf_size=15,
        truth=truth,
        seed=1,
    )
    spec = (
        LENS_SPEC_YAML.replace("pixel_scale: 0.1", "pixel_scale: 0.05")
        .replace("subsample: 1", "subsample: 2")
        .replace("lens_light_components: 1", "lens_light_components: 2")
        .replace("external_shear: false", "external_shear: true")
        .replace("theta_e_median: 0.7", "theta_e_median: 1.0")
        .replace("light_size_medians: [0.4, 1.0]", "light_size_medians: [0.5, 1.5]")
        .replace("engine: pymc", "engine: nutpie")
        .replace("n_starts: 2", "n_starts: 5")
        .replace("tune: 150, draws: 100", "tune: 300, draws: 200")
    )
    b = fit_spec(spec, ds, registry=registry, raw_schema=LENS_SCHEMA)
    te = b.model.posterior["theta_E"]
    assert abs(te.mean().item() - 1.2) < 0.02
    assert b.model.learned["engine"] == "nutpie"
