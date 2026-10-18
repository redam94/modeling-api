"""``sci.cmb_field`` (E79): flat-sky maths, the Gibbs sampler, scores, recovery, contract kit."""

from __future__ import annotations

import json
import time

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.cmb import (
    NOISE,
    REFERENCE,
    SKY_MOMENTS,
    TARGET,
    CMBFieldModel,
    CMBFieldSettings,
    Instrument,
    _Data,
    amplitude_tilt,
    box,
    folded_power,
    hartley,
    projection_stretch,
)
from mapi_plugin_science.cmb_data import (
    CHALLENGES_FILE,
    CMB_SCHEMA,
    CMB_SPEC_YAML,
    CMB_TRUTH,
    NOTEBOOK_REFERENCE,
    PLANCK_SPEC_YAML,
    THEORY_FILE,
    challenges_reference,
    from_challenges,
    simulate_cmb,
    toy_spectrum,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import ErrorCode, MapiError, load_model_spec, load_score_request, validate_spec
from mapi_spec.capabilities import GridDecl
from scipy import ndimage

CHALLENGES = challenges_file(CHALLENGES_FILE)
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://science/cmb\n"
TINY = CMBFieldSettings(
    pixel_subsamples=1, bin_edges=list(CMB_TRUTH["edges"]), border=CMB_TRUTH["border"]
)


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(CMBFieldModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def synthetic() -> tuple[xr.Dataset, xr.Dataset]:
    return simulate_cmb(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]) -> Bundle:
    data, _ = synthetic
    return fit_spec(CMB_SPEC_YAML, data, registry=registry, raw_schema=CMB_SCHEMA)


def _score(bundle: Bundle, registry: Registry, body: str, data: xr.Dataset) -> xr.DataTree:
    req = load_score_request(HEADER + body)
    return score_bundle(bundle, req, data, registry=registry, return_full=True)


def _predictions(tree: xr.DataTree) -> xr.Dataset:
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


# ------------------------------------------------------------------------------ manifest


def test_manifest_declares_grids_modes_and_no_loo() -> None:
    m = CMBFieldModel.manifest()
    caps = m.capabilities
    assert caps.score_modes == frozenset({"predict", "evaluate", "simulate"})
    assert not caps.loo_valid and caps.loo_invalid_reason
    assert caps.deterministic_given_seed
    for name in ("sky", "temperature_hat"):
        grid = m.outputs[name].grid
        assert isinstance(grid, GridDecl)
        assert (grid.y, grid.x, grid.origin, grid.colormap) == ("row", "col", "lower", "diverging")
    assert m.outputs["temperature_hat"].units_of == TARGET
    assert m.outputs["bandpower"].dims == ["bin"] and m.outputs["bandpower"].units == "uK^2"
    assert m.outputs["tilt"].dims == []
    assert [d.name for d in m.derived] == [SKY_MOMENTS]
    assert {"sci.power_spectrum", "sci.field_map", "sci.image_residuals"} <= set(m.plots)


def test_schema_template_adds_the_pixel_window_only_when_asked() -> None:
    template = CMBFieldModel.manifest().schema_template
    plain = template.resolve({})
    assert set(plain.vars) == {TARGET, NOISE, REFERENCE}
    assert plain.vars[TARGET].missing == "allow"
    assert all(d.policy.value == "fixed" for d in plain.dims.values())
    assert "pixel_window" in template.resolve({"pixel_window": True}).vars


@pytest.mark.parametrize("spec", [CMB_SPEC_YAML, PLANCK_SPEC_YAML], ids=["tiny", "planck"])
def test_specs_validate_statically(registry: Registry, spec: str) -> None:
    res = validate_spec(load_model_spec(spec), registry.catalog(), CMB_SCHEMA)
    assert res.ok, [i.message for i in res.errors]


# ------------------------------------------------------------------------------ maths


def test_hartley_is_its_own_inverse_and_the_box_window_is_one_at_zero() -> None:
    a = np.random.default_rng(0).standard_normal((2, 12, 10))
    np.testing.assert_allclose(hartley(hartley(a)) / 120, a, atol=1e-12)
    k = np.array([0.0, 1e-9, 100.0])
    np.testing.assert_allclose(box(k, 0.002, 8)[:2], 1.0)
    np.testing.assert_allclose(box(k, 0.002, 1), 1.0)
    assert 1.0 < projection_stretch(128, 128, 7.5 * np.pi / 180 / 60) < 1.02


def test_prior_power_matches_the_pixel_variance_of_the_reference() -> None:
    """With no beam, no window and one sample per pixel, the folded power sums to the
    variance of the band-limited sky (a check of the Hartley normalisation)."""
    ref = toy_spectrum()
    inst = Instrument.from_dl(ref, None, 0.0, 7.5, 1)
    n = 64
    s0 = folded_power(n, n, inst, 1.0, nalias=0) / (inst.dx**2 * n * n)
    ky, kx = np.meshgrid(np.fft.fftfreq(n, inst.dx), np.fft.fftfreq(n, inst.dx), indexing="ij")
    ell = 2 * np.pi * np.hypot(ky, kx)
    want = np.sum(inst.cl_of(ell)) / (inst.dx**2 * n * n)
    np.testing.assert_allclose(s0.sum(), want, rtol=1e-12)
    # a sky drawn with these variances has that pixel variance
    rng = np.random.default_rng(1)
    maps = hartley(np.sqrt(s0) * rng.standard_normal((200, n, n)))
    assert abs(maps.var() / s0.sum() - 1) < 0.05


def test_wiener_filter_matches_a_dense_solve() -> None:
    """PCG against the dense posterior mean on a small masked torus."""
    data, _ = simulate_cmb(n=12, border=2, seed=3, edges=(0, 600, 1200, 2100))
    s = CMBFieldSettings(pixel_subsamples=1, bin_edges=[0, 600, 1200, 2100], border=2)
    d = _Data.from_dataset(data, s)
    smp = d.sampler(s)
    q = np.array([[0.1, -0.2, 0.3]])
    y = smp.embed(d.y)[None]
    x, _ = smp.solve(q, y, tol=1e-12)
    n = smp.ty * smp.tx
    eye = np.eye(n).reshape(n, smp.ty, smp.tx)
    h = hartley(eye).reshape(n, n)
    ninv = smp.Ninv.ravel()
    prec = np.diag(1 / smp.prior_var(q)[0].ravel()) + h.T @ (ninv[:, None] * h)
    want = np.linalg.solve(prec, h.T @ (ninv * y[0].ravel()))
    np.testing.assert_allclose(x[0].ravel(), want, rtol=1e-6, atol=1e-8)


def test_amplitude_tilt_is_the_conjugate_gaussian_posterior() -> None:
    rng = np.random.default_rng(2)
    ell = np.array([100.0, 300.0, 600.0, 900.0, 1500.0])
    true = 0.1 + -0.2 * np.log(ell / 500)
    q = true + 0.01 * rng.standard_normal((4000, ell.size))
    mean, cov = amplitude_tilt(q, ell, pivot=500.0, max_ell=1450.0, prior_sd=0.5)
    np.testing.assert_allclose(mean, [0.1, -0.2], atol=0.003)
    assert cov.shape == (2, 2) and np.all(np.diag(cov) > 0)
    assert np.sqrt(cov[0, 0]) < 0.01


# ------------------------------------------------------------------------------ typed refusals


def test_bad_data_and_settings_are_refused(synthetic: tuple[xr.Dataset, xr.Dataset]) -> None:
    data, _ = synthetic
    cases = []
    allmasked = data.copy(deep=True)
    allmasked[TARGET][:] = np.nan
    cases.append((allmasked, TINY, ErrorCode.SCHEMA_INVALID))
    badnoise = data.copy(deep=True)
    badnoise[NOISE][5, 5] = 0.0
    cases.append((badnoise, TINY, ErrorCode.SCHEMA_INVALID))
    badref = data.copy(deep=True)
    badref[REFERENCE][:] = 0.0
    cases.append((badref, TINY, ErrorCode.SCHEMA_INVALID))
    cases.append((data.drop_vars(NOISE), TINY, ErrorCode.SCHEMA_INVALID))
    cases.append((data.isel(row=slice(0, 6)), TINY, ErrorCode.SCHEMA_INVALID))
    nowin = data.drop_vars("pixel_window")
    cases.append((nowin, TINY.model_copy(update={"pixel_window": True}), ErrorCode.SCHEMA_INVALID))
    fine = TINY.model_copy(update={"bin_edges": [0, 10, 20, 2100]})
    cases.append((data, fine, ErrorCode.SETTINGS_INVALID))
    dense = TINY.model_copy(update={"ell_dense": 1e6, "border": 20})
    cases.append((data, dense, ErrorCode.SETTINGS_INVALID))
    for i, (ds, s, code) in enumerate(cases):
        with pytest.raises(MapiError) as exc:
            _Data.from_dataset(ds, s).sampler(s)
            pytest.fail(f"case {i} was not refused")
        assert exc.value.code is code, (i, exc.value)


def test_decreasing_bin_edges_are_refused() -> None:
    with pytest.raises(ValueError):
        CMBFieldSettings(bin_edges=[0, 300, 200])


# ------------------------------------------------------------------------------ fit + score


def test_fit_stores_the_gibbs_draws_and_recovers_the_spectrum(
    bundle: Bundle, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    _, truth = synthetic
    post = bundle.model.posterior
    assert set(post.data_vars) == {"q", "bandpower", "bandpower_ratio", "log_amplitude", "tilt"}
    assert dict(post["q"].sizes) == {"chain": 2, "draw": 60, "bin": 6}
    assert "ell" in post.coords and post["ell"].dims == ("bin",)
    stats = bundle.model.groups["sample_stats"]
    assert {"cg_iterations", "rescale_acceptance"} <= set(stats.data_vars)
    groups = bundle.model.groups
    assert groups["observed_data"][TARGET].isnull().any()
    assert "reference_bandpower" in groups["constant_data"]
    assert "log_likelihood" not in groups
    learned = bundle.model.learned
    assert learned["engine"] == "gibbs" and learned["torus"] == [40, 40]
    # the true q of most bins is inside the 1-99 % range; the summary is close to the truth
    q = post["q"].values.reshape(-1, 6)
    lo, hi = np.quantile(q, [0.01, 0.99], axis=0)
    inside = (truth["q"].values > lo) & (truth["q"].values < hi)
    assert inside.sum() >= 5, (truth["q"].values, lo, hi)
    np.testing.assert_allclose(
        np.exp(post["q"]) * groups["constant_data"]["reference_bandpower"],
        post["bandpower"],
        rtol=1e-12,
    )
    moments = bundle.model.derived[SKY_MOMENTS]["sky_summary"]
    obs = groups["observed_data"][TARGET].notnull().values
    sd = moments.sel(stat="sd").values
    # the data pin the sky down where there are data; the hole is much less sure
    assert np.median(sd[obs]) < 6.0 < 3 * 6.0 < np.median(sd[~obs])
    err = moments.sel(stat="mean").values - truth["sky"].values
    assert np.sqrt(np.mean(err[obs] ** 2)) < 6.0


def test_sky_draws_have_the_declared_shapes(bundle: Bundle, registry: Registry) -> None:
    data = _fit_data(bundle)
    tree = _score(bundle, registry, "mode: predict\nseed: 5\noutput: {draws: 4}\n", data)
    pred = _predictions(tree)
    # field_draws: 8 over 2 chains -> 4 evenly spaced draws per chain
    assert dict(pred["sky"].sizes) == {"chain": 2, "draw": 4, "row": 32, "col": 32}
    assert pred["sky"].dtype == np.float32
    assert pred["bandpower"].dims == ("chain", "draw", "bin")
    assert int(pred["draw"].values[-1]) == 59
    assert tree["draws"]["sky"].sizes["sample"] == 4
    np.testing.assert_array_equal(pred["sky"].values, pred["temperature_hat"].values)


def test_constrained_skies_cover_the_truth_behind_the_mask(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, truth = synthetic
    pred = _predictions(_score(bundle, registry, "mode: predict\nseed: 1\n", data))
    sky = pred["sky"].astype(np.float64)
    obs = data[TARGET].notnull().values
    lo, hi = sky.quantile([0.01, 0.99], dim=("chain", "draw"))
    true = truth["sky"]
    hole = ~obs
    cover = float(((true >= lo) & (true <= hi)).values[hole].mean())
    assert cover > 0.7, f"98% coverage behind the mask {cover:.2f}"
    resid = (sky.mean(("chain", "draw")) - true).values
    assert np.sqrt(np.mean(resid[obs] ** 2)) < 6.0


def test_draws_are_deterministic_and_independent_of_the_payload(
    bundle: Bundle, registry: Registry
) -> None:
    data = _fit_data(bundle)
    a = _predictions(_score(bundle, registry, "mode: predict\nseed: 4\n", data))
    b = _predictions(_score(bundle, registry, "mode: predict\nseed: 4\n", data))
    c = _predictions(_score(bundle, registry, "mode: predict\nseed: 5\n", data))
    xr.testing.assert_identical(a, b)
    assert not np.allclose(a["sky"].values, c["sky"].values)
    sub = data.isel(row=slice(3, 9), col=[0, 7, 30])
    s = _predictions(_score(bundle, registry, "mode: predict\nseed: 4\n", sub))
    want = a["sky"].sel(row=sub["row"].values, col=sub["col"].values)
    np.testing.assert_array_equal(s["sky"].values, want.values)


def test_predictive_adds_the_pixel_noise(bundle: Bundle, registry: Registry) -> None:
    data = _fit_data(bundle)
    exp = _predictions(_score(bundle, registry, "mode: predict\nseed: 2\n", data))
    pre = _predictions(
        _score(bundle, registry, "mode: predict\nseed: 2\noutput: {kind: predictive}\n", data)
    )
    np.testing.assert_array_equal(exp["sky"].values, pre["sky"].values)
    z = (pre["temperature_hat"] - exp["temperature_hat"]) / data[NOISE]
    assert abs(float(z.std()) - 1) < 0.1


def test_simulate_gives_unconstrained_skies_with_the_posterior_spectrum(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, truth = synthetic
    sim = _predictions(_score(bundle, registry, "mode: simulate\nseed: 3\n", data))
    pre = _predictions(_score(bundle, registry, "mode: predict\nseed: 3\n", data))
    obs = data[TARGET].notnull().values
    # a simulated sky does not follow the data; a constrained one does
    r_sim = np.corrcoef(sim["sky"].values[0, 0][obs], truth["sky"].values[obs])[0, 1]
    r_pre = np.corrcoef(pre["sky"].values[0, 0][obs], truth["sky"].values[obs])[0, 1]
    assert abs(r_sim) < 0.5 < 0.99 < r_pre
    assert sim["temperature_hat"].attrs["kind"] == "predictive"
    ratio = float(sim["sky"].std() / truth["sky"].std())
    assert 0.5 < ratio < 2.0


def test_evaluate_compares_the_unmasked_pixels(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = synthetic
    tree = score_bundle(
        bundle,
        load_score_request(HEADER + "mode: evaluate\ndata: dataset://science/cmb@v1\nseed: 3\n"),
        data,
        registry=registry,
        metrics=["rmse", "coverage_90"],
    )
    report = json.loads(tree.attrs["metrics"])
    assert report["n_holdout"] == int(data[TARGET].notnull().sum())
    # the posterior sky leaves less than the pixel noise
    assert report["values"]["rmse"] < float(data[NOISE].mean())


def test_unsupported_modes_are_refused(bundle: Bundle, registry: Registry) -> None:
    data = _fit_data(bundle)
    with pytest.raises(MapiError) as exc:
        _score(
            bundle,
            registry,
            "mode: counterfactual\ninterventions: [{var: temperature, multiply: 1.1}]\n",
            data,
        )
    assert exc.value.code is ErrorCode.MODE_NOT_SUPPORTED


def test_declared_plots_render(bundle: Bundle, registry: Registry) -> None:
    """The bundle feeds the spectrum and convergence plots; the scored skies with the
    observed map (as the workflow ``plot.render`` node builds it) feed the image plots."""
    from mapi_plots.recipes import render_plots

    data = _fit_data(bundle)
    tree = _score(bundle, registry, "mode: predict\nseed: 1\n", data)
    tree["observed_data"] = xr.DataTree(data[[TARGET]])
    sources = {
        "core.convergence": bundle.to_tree(),
        "sci.power_spectrum": bundle.to_tree(),
        "sci.field_map": tree,
        "sci.image_residuals": tree,
        "sci.ppc_image_grid": tree,
    }
    assert set(sources) == set(CMBFieldModel.plots)
    for ref, src in sources.items():
        out = render_plots(src, [ref])
        assert len(out.plots) == 1, (ref, [i.message for i in out.issues])


@pytest.mark.slow
def test_contract_kit(registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]) -> None:
    data, _ = synthetic
    report = check_model(
        CMBFieldModel,
        ModelFixture(spec=CMB_SPEC_YAML, data=data, raw_schema=CMB_SCHEMA, registry=registry),
    )
    passed = {r.name for r in report.results if r.passed and not r.skipped}
    assert {
        "about",
        "fit",
        "bundle_roundtrip_no_pickle",
        "unsupported_mode",
        "output_space_and_units",
        "outputs_match_manifest",
        "seed_determinism",
        "plots_sanitize",
    } <= passed
    plots = next(r for r in report.results if r.name == "plots_sanitize")
    assert "sci.power_spectrum" in plots.detail and "sci.field_map" in plots.detail


# ------------------------------------------------------------------------------ real data


@pytest.mark.skipif(CHALLENGES is None, reason="pymc-challenges data are not present")
def test_from_challenges_reads_the_lmc_patch() -> None:
    assert CHALLENGES is not None
    assert (CHALLENGES.parent / THEORY_FILE).is_file()
    ds = from_challenges(CHALLENGES)
    assert dict(ds[TARGET].sizes) == {"row": 128, "col": 128}
    assert ds.sizes["multipole"] == 4097
    masked = float(ds[TARGET].isnull().mean())
    assert abs(masked - NOTEBOOK_REFERENCE["masked_fraction"]) < 0.005
    obs = ds[TARGET].notnull()
    sig = ds[NOISE].where(obs)
    assert 1.4 < float(sig.min()) < float(sig.max()) < 3.3  # notebook: 1.5-3.2 uK
    assert 2000 < float(ds[REFERENCE].sel(multipole=220)) < 6500
    assert float(ds["pixel_window"].isel(multipole=0)) == pytest.approx(1.0)
    north = from_challenges(CHALLENGES.parent, patch="north")
    assert float(north[TARGET].isnull().mean()) < 0.01
    ref = challenges_reference(CHALLENGES.parent)
    assert ref["inpainted"].shape == (128, 128) and ref.sizes["planck_bin"] == 83
    with pytest.raises(ValueError):
        from_challenges(CHALLENGES, patch="south")
    s = CMBFieldSettings(pixel_window=True)
    smp = _Data.from_dataset(ds, s).sampler(s)
    assert (smp.ty, smp.tx, smp.nb, len(smp.low)) == (144, 144, 26, 681)


@pytest.mark.slow
@pytest.mark.skipif(CHALLENGES is None, reason="pymc-challenges data are not present")
def test_planck_lmc_patch_matches_the_notebook(registry: Registry) -> None:
    """The notebook's fit (4 chains x 80 iterations): amplitude and tilt, pixel sds."""
    assert CHALLENGES is not None
    ds = from_challenges(CHALLENGES)
    t0 = time.time()
    b = fit_spec(PLANCK_SPEC_YAML, ds, registry=registry, raw_schema=CMB_SCHEMA)
    assert time.time() - t0 < 600
    learned = b.model.learned
    assert learned["dense_modes"] == 681 and learned["torus"] == [144, 144]
    la, la_sd = NOTEBOOK_REFERENCE["log_amplitude"]
    ti, ti_sd = NOTEBOOK_REFERENCE["tilt"]
    assert abs(learned["log_amplitude_mean"] - la) < la_sd
    assert abs(learned["tilt_mean"] - ti) < ti_sd
    assert abs(learned["log_amplitude_sd"] / la_sd - 1) < 0.25
    q = b.model.posterior["q"].mean(("chain", "draw"))
    assert abs(float(q.sel(bin=1)) - NOTEBOOK_REFERENCE["q_mean_bin_80"]) < 0.15
    sd = b.model.derived[SKY_MOMENTS]["sky_summary"].sel(stat="sd").values
    obs = ds[TARGET].notnull().values
    assert abs(np.median(sd[obs]) - NOTEBOOK_REFERENCE["sd_observed_uK"]) < 0.3
    lab, _ = ndimage.label(~obs)
    big = lab == np.bincount(lab.ravel())[1:].argmax() + 1
    assert abs(np.median(sd[big]) - NOTEBOOK_REFERENCE["sd_lmc_hole_uK"]) < 10


def _fit_data(bundle: Bundle) -> xr.Dataset:
    """The fit data back from the bundle (dataset names equal canonical names here)."""
    obs = bundle.model.groups["observed_data"][TARGET]
    const = bundle.model.groups["constant_data"]
    return xr.Dataset(
        {TARGET: obs, NOISE: const[NOISE], REFERENCE: const[REFERENCE]},
    )
