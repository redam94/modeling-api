"""``sci.ibp_features`` (E31): the summed-out IBP likelihood against brute force, the start
point, recovery of known strokes, completion of held-out halves, every score mode, typed
refusals and the real optdigits."""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.ibp import (
    DIGIT_DIM,
    FEATURE_DIM,
    IBPFeaturesModel,
    IBPFeaturesSettings,
    _Images,
    heldout_scores,
    patterns,
    row_loglik,
)
from mapi_plugin_science.ibp_data import (
    CHALLENGES_FILE,
    CHALLENGES_TEST_FILE,
    DIGITS_SPEC_YAML,
    IBP_SCHEMA,
    IBP_SPEC_YAML,
    IBP_TRUTH,
    NOTEBOOK_REFERENCE,
    from_challenges,
    ibp_dataset,
    stroke_features,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import ScoreRequest
from scipy import stats

CHALLENGES_CSV = challenges_file(CHALLENGES_FILE)
CHALLENGES_TEST_CSV = challenges_file(CHALLENGES_TEST_FILE)
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/ibp\n"
CANON = {"row": "y", "col": "x", "pixels": "ink", "visible": "shown"}


def _req(body: str) -> ScoreRequest:
    return load_score_request(HEADER + body)


def _canon(ds: xr.Dataset) -> xr.Dataset:
    return ds.rename(CANON)


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(IBPFeaturesModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def strokes() -> xr.Dataset:
    return ibp_dataset(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, strokes: xr.Dataset) -> Bundle:
    return fit_spec(IBP_SPEC_YAML, strokes, registry=registry, raw_schema=IBP_SCHEMA)


# ------------------------------------------------------------------------------- manifest


def test_manifest_declares_image_grids_and_the_mask_case() -> None:
    man = IBPFeaturesModel.manifest()
    assert man.id == "sci.ibp_features"
    assert man.capabilities.score_modes == frozenset({"predict", "evaluate"})
    assert man.capabilities.loo_valid
    ink = man.outputs["ink_hat"].grid
    assert ink is not None and (ink.y, ink.x, ink.frame, ink.origin) == (
        "y",
        "x",
        "digit",
        "upper",
    )
    feats = man.outputs["features"].grid
    assert feats is not None and feats.frame == "feature" and feats.colormap == "diverging"
    assert man.outputs["ink_hat"].units_of == "ink"
    assert man.outputs["owns"].units == "1" and man.outputs["feature_prob"].units == "1"
    plain = man.schema_template.resolve({})
    assert set(plain.vars) == {"ink"} and plain.vars["ink"].missing == "allow"
    masked = man.schema_template.resolve({"shown_mask": True})
    assert masked.vars["shown"].role.value == "mask"
    assert plain.dims["digit"].policy.value == "fixed"
    assert {"sci.field_map", "sci.ppc_image_grid", "sci.image_residuals"} <= set(man.plots)


@pytest.mark.parametrize("spec", [IBP_SPEC_YAML, DIGITS_SPEC_YAML])
def test_static_validation_resolves_the_specs(registry: Registry, spec: str) -> None:
    res = validate_spec(load_model_spec(spec), registry.catalog(), IBP_SCHEMA)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]


def test_too_many_features_is_refused_statically(registry: Registry) -> None:
    spec = IBP_SPEC_YAML.replace("n_features: 4", "n_features: 12")
    res = validate_spec(load_model_spec(spec), registry.catalog(), IBP_SCHEMA)
    assert not res.ok
    assert any(i.code is ErrorCode.SETTINGS_INVALID for i in res.errors)


# ------------------------------------------------------------------------------- the maths


def test_summed_out_likelihood_matches_brute_force() -> None:
    rng = np.random.default_rng(3)
    k, d, n, sx = 3, 5, 4, 0.3
    xc = rng.normal(size=(n, d))
    a = rng.normal(size=(k, d))
    pi = np.array([0.7, 0.4, 0.2])
    seen = np.ones((n, d), dtype=bool)
    got = row_loglik(xc, seen, a, np.log(pi), sx, patterns(k))
    want = np.zeros(n)
    for i in range(n):
        terms = []
        for z in itertools.product([0, 1], repeat=k):
            zz = np.asarray(z, dtype=float)
            prior = np.prod(np.where(zz == 1, pi, 1 - pi))
            terms.append(prior * np.prod(stats.norm(zz @ a, sx).pdf(xc[i])))
        want[i] = np.log(np.sum(terms))
    np.testing.assert_allclose(got, want, rtol=1e-10)
    # unseen pixels drop out: the same as the likelihood of the seen columns alone
    seen[1, 3:] = False
    seen[2, :] = False
    part = row_loglik(xc, seen, a, np.log(pi), sx, patterns(k))
    sub = row_loglik(xc[1:2, :3], seen[1:2, :3], a[:, :3], np.log(pi), sx, patterns(k))
    np.testing.assert_allclose(part[1], sub[0], rtol=1e-10)
    assert part[2] == pytest.approx(0.0, abs=1e-12)  # nothing seen: probability one
    np.testing.assert_allclose(part[[0, 3]], got[[0, 3]])


def test_potential_matches_the_numpy_twin(strokes: xr.Dataset) -> None:
    ds = _canon(strokes)
    s = IBPFeaturesSettings(n_features=3, noise_sd=0.2)
    images = _Images.from_dataset(ds, s)
    model = IBPFeaturesModel().build_model(images, s)
    rng = np.random.default_rng(1)
    a = rng.normal(0, 0.4, size=(3, 48))
    nu = np.array([0.8, 0.7, 0.6])
    point = {
        "alpha_log__": np.log(2.0),
        "nu_logodds__": np.log(nu / (1 - nu)),
        "sigma_A_log__": np.log(0.3),
        "A": a,
    }
    (pot,) = model.replace_rvs_by_values([model["loglik"]])
    got = model.compile_fn(pot, inputs=model.value_vars, on_unused_input="ignore")(point)
    want = row_loglik(images.centred, images.seen, a, np.cumsum(np.log(nu)), 0.2, patterns(3))
    assert float(got) == pytest.approx(float(want.sum()), rel=1e-9)


def test_average_image_and_start_point_follow_the_notebook(strokes: xr.Dataset) -> None:
    ds = _canon(strokes)
    ink = ds["ink"].values.copy()
    ink[0, :4] = np.nan  # an incomplete image does not enter the average image
    s = IBPFeaturesSettings(n_features=4)
    images = _Images.from_dataset(ds.assign(ink=(ds["ink"].dims, ink)), s)
    np.testing.assert_allclose(images.mean, ds["ink"].values[1:].reshape(59, -1).mean(0))
    start = IBPFeaturesModel().start_point(images, s, seed=5)
    assert start["A"].shape == (4, 48) and np.allclose(start["nu"], 0.8)
    assert (start["sigma_A"], start["alpha"]) == (0.3, 2.0) and "sigma_X" not in start
    rows = [int(np.argmin(np.abs(images.centred - 2 * f).sum(1))) for f in start["A"]]
    np.testing.assert_allclose(start["A"], 0.5 * images.centred[rows])
    assert 0 not in rows  # only complete images start a feature
    free = IBPFeaturesModel().start_point(images, s.model_copy(update={"kind": "free_noise"}), 5)
    assert free["sigma_X"] == 0.25


def test_heldout_scores_complete_from_the_seen_half() -> None:
    feats = stroke_features().reshape(3, -1)
    a = np.stack([feats])  # one draw, the true strokes
    logpi = np.log(np.array([[0.6, 0.45, 0.3]]))
    ink = 0.2 + feats[[0, 1]].sum(0)[None, :] + np.zeros((2, 48))
    ink[1] = 0.2 + feats[1]
    seen = np.zeros((2, 48), dtype=bool)
    seen[:, :24] = True
    res = heldout_scores(ink, seen, ~seen, np.full(48, 0.2), a, logpi, np.array([0.1]))
    assert res["n_images"] == 2 and res["lpd"].shape == (2,)
    assert res["rmse"] < 0.05  # the strokes continue into the bottom half
    np.testing.assert_allclose(res["mean"][:, :24], ink[:, :24], atol=0.1)


# ------------------------------------------------------------------------------- refusals


def test_typed_refusals_of_the_data(strokes: xr.Dataset) -> None:
    ds = _canon(strokes)
    with pytest.raises(MapiError) as err:
        _Images.from_dataset(ds.drop_vars("shown"), IBPFeaturesSettings(shown_mask=True))
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.pointer == "/data/shown"
    blank = ds.assign(ink=ds["ink"] * np.nan)
    with pytest.raises(MapiError) as err:
        _Images.from_dataset(blank, IBPFeaturesSettings())
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    hidden = ds.assign(shown=ds["shown"] & False)
    with pytest.raises(MapiError):
        _Images.from_dataset(hidden, IBPFeaturesSettings(shown_mask=True))
    with pytest.raises(MapiError) as err:
        _Images.from_dataset(ds.drop_vars("ink"), IBPFeaturesSettings())
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_from_challenges_refuses_a_file_without_64_pixels(tmp_path: Path) -> None:
    bad = tmp_path / "optdigits_train.csv"
    bad.write_text("0,1,2,3\n4,5,6,7\n")
    with pytest.raises(ValueError, match="64 pixel columns"):
        from_challenges(bad)


def test_scoring_a_digit_that_was_not_fitted_is_refused(
    bundle: Bundle, strokes: xr.Dataset, registry: Registry
) -> None:
    other = strokes.isel(digit=[0]).assign_coords(digit=["img-999"])
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, _req("mode: predict\n"), other, registry=registry)
    assert err.value.code is ErrorCode.UNSEEN_LEVELS


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("mode: forecast\nhorizon: {dim: y, periods: 2}\n", ErrorCode.MODE_NOT_SUPPORTED),
        ("mode: simulate\n", ErrorCode.MODE_NOT_SUPPORTED),
    ],
)
def test_unsupported_modes_are_refused(
    bundle: Bundle, strokes: xr.Dataset, registry: Registry, body: str, code: ErrorCode
) -> None:
    payload = None if "horizon" in body else strokes
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, _req(body), payload, registry=registry)
    assert err.value.code is code


# ------------------------------------------------------------------------------- fit / score


def test_fit_finds_the_strokes_and_completes_held_out_halves(
    bundle: Bundle, strokes: xr.Dataset
) -> None:
    groups = bundle.model.groups
    post = groups["posterior"]
    assert {"alpha", "nu", "logpi", "sigma_A", "features"} <= set(post.data_vars)
    assert post["features"].dims == ("chain", "draw", "feature", "y", "x")
    # label-free: in the last draw of every chain the features span each true stroke
    truth = stroke_features().reshape(3, -1)
    for c in range(post.sizes["chain"]):
        a = post["features"].isel(chain=c, draw=-1).values.reshape(4, -1)
        coef, *_ = np.linalg.lstsq(a.T, truth.T, rcond=None)
        r2 = 1 - ((a.T @ coef - truth.T) ** 2).sum(0) / (truth**2).sum(1)
        assert (r2 > 0.85).all(), r2.round(2)
    # pi is decreasing within every draw (stick-breaking)
    assert bool((post["logpi"].diff(FEATURE_DIM) <= 0).all())
    # the held-out halves: much better than the average image
    obs = strokes["pixels"]
    noise = IBP_TRUTH["noise_sd"]
    base = float(np.sqrt(((obs - obs.mean("digit")) ** 2).mean()))
    values: dict[str, Any] = dict(bundle.metrics.values)
    assert values["rmse"] < 0.75 * base, (values["rmse"], base)
    assert values["rmse"] < 2.0 * noise
    assert 0.75 <= values["coverage_90"] <= 1.0
    assert np.isfinite(values["elpd_loo"])
    detail = bundle.metrics.elpd["elpd_loo"]
    assert detail.n == strokes.sizes["digit"] and detail.pointwise is not None
    ll = groups["log_likelihood"]["ink"]
    assert ll.dims == ("chain", "draw", DIGIT_DIM)
    assert groups["constant_data"]["pixel_mean"].dims == ("y", "x")
    learned = bundle.model.learned
    assert learned["start"] == "digits" and learned["patterns"] == 16
    assert "heldout" not in learned  # no shown mask


def test_log_likelihood_is_the_numpy_twin(bundle: Bundle) -> None:
    images = _Images.from_fitted(bundle.model)
    post = bundle.model.groups["posterior"]
    draw = post.isel(chain=1, draw=7)
    want = row_loglik(
        images.centred,
        images.seen,
        draw["features"].values.reshape(4, -1),
        draw["logpi"].values,
        0.2,
        patterns(4),
    )
    got = bundle.model.groups["log_likelihood"]["ink"].isel(chain=1, draw=7).values
    np.testing.assert_allclose(got, want, rtol=1e-10)


def test_predict_returns_images_features_and_ownership(
    bundle: Bundle, strokes: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        bundle, _req("mode: predict\n"), strokes, registry=registry, return_full=True
    )
    pred = out["predictions"].to_dataset()
    assert pred["ink_hat"].dims == ("chain", "draw", "digit", "row", "col")
    assert pred.sizes["chain"] * pred.sizes["draw"] == 100  # score_draws
    assert pred["features"].dims == ("chain", "draw", "feature", "row", "col")
    assert pred["owns"].dims == ("chain", "draw", "digit", "feature")
    own = pred["owns"].values
    assert own.min() >= 0.0 and own.max() <= 1.0
    # the expected image of a fully seen digit is close to the digit
    err = (pred["ink_hat"].mean(("chain", "draw")) - strokes["pixels"]).values
    assert float(np.sqrt(np.mean(err**2))) < 1.5 * IBP_TRUTH["noise_sd"]
    prob = pred["feature_prob"]
    assert bool((prob.diff("feature") <= 1e-12).all())
    assert out["summary"].to_dataset()["ink_hat"].attrs["units"] == "ink"
    sub = score_bundle(
        bundle,
        _req("mode: predict\n"),
        strokes.isel(digit=[3, 5], row=slice(4, 8)),
        registry=registry,
        return_full=True,
    )["predictions"].to_dataset()
    assert dict(sub["ink_hat"].sizes) | {"chain": 0, "draw": 0} == {
        "chain": 0,
        "draw": 0,
        "digit": 2,
        "row": 4,
        "col": 6,
    }
    np.testing.assert_allclose(
        sub["ink_hat"].values, pred["ink_hat"].isel(digit=[3, 5], row=slice(4, 8)).values
    )


def test_predictive_draws_are_seeded_and_noisy(
    bundle: Bundle, strokes: xr.Dataset, registry: Registry
) -> None:
    body = "mode: predict\noutput: {kind: predictive}\nseed: 4\n"
    one = score_bundle(bundle, _req(body), strokes, registry=registry, return_full=True)
    two = score_bundle(bundle, _req(body), strokes, registry=registry, return_full=True)
    a = one["predictions"].to_dataset()["ink_hat"]
    b = two["predictions"].to_dataset()["ink_hat"]
    np.testing.assert_array_equal(a.values, b.values)
    expected = score_bundle(
        bundle, _req("mode: predict\n"), strokes, registry=registry, return_full=True
    )["predictions"].to_dataset()["ink_hat"]
    spread = float((a - expected).std())
    assert 0.5 * 0.2 < spread < 2.0 * 0.2  # noise_sd of the fit


def test_evaluate_mode_scores_the_held_out_cells(
    bundle: Bundle, strokes: xr.Dataset, registry: Registry
) -> None:
    body = "mode: evaluate\ndata: dataset://vision/stroke_images@latest\n"
    out = score_bundle(bundle, _req(body), strokes, registry=registry, metrics=["rmse", "mae"])
    values = json.loads(out.attrs["metrics"])["values"]
    assert values["rmse"] < 2.0 * IBP_TRUTH["noise_sd"]


def test_diagnostics_flag_the_truncation_and_collapsed_chains(
    bundle: Bundle, strokes: xr.Dataset
) -> None:
    model = IBPFeaturesModel()
    post = bundle.model.groups["posterior"]
    k = post.sizes[FEATURE_DIM]

    def with_pi(pi_first: list[float], pi_last: float) -> list[str]:
        lp = np.zeros_like(post["logpi"].values)
        for c, p1 in enumerate(pi_first):
            lp[c] = np.log(np.linspace(p1, pi_last if p1 > 0.01 else 1e-4, k))[None, :]
        groups = {**bundle.model.groups, "posterior": post.assign(logpi=(post["logpi"].dims, lp))}
        return [d.code for d in model.diagnostics(replace(bundle.model, groups=groups), strokes)]

    assert "TRUNCATION_BINDS" in with_pi([0.6, 0.6], 0.3)
    assert "TRUNCATION_BINDS" not in with_pi([0.6, 0.6], 0.001)
    assert "FEATURES_COLLAPSED" in with_pi([0.6, 0.001], 0.3)
    assert "FEATURES_COLLAPSED" not in with_pi([0.6, 0.6], 0.3)


def test_free_noise_learns_sigma_x_and_keeps_replicates(
    registry: Registry, strokes: xr.Dataset
) -> None:
    spec = (
        IBP_SPEC_YAML.replace("noise_sd: 0.2", "noise_sd: 0.2, kind: free_noise")
        .replace("mode: evaluate_then_refit", "mode: train_only")
        .replace("log_likelihood: true, ", "")
    )
    b = fit_spec(spec, strokes, registry=registry, raw_schema=IBP_SCHEMA)
    post = b.model.groups["posterior"]
    lo, hi = np.quantile(post["sigma_X"].values, [0.01, 0.99])
    assert lo < IBP_TRUTH["noise_sd"] < hi * 1.1, (lo, hi)
    pp = b.model.groups["posterior_predictive"]["ink"]
    assert pp.dims == ("chain", "draw", "digit", "y", "x") and pp.sizes["draw"] == 10
    assert "log_likelihood" not in b.model.groups
    assert b.model.learned["kind"] == "free_noise"


@pytest.mark.mapi_contract
def test_ibp_features_passes_the_contract_kit(registry: Registry, strokes: xr.Dataset) -> None:
    fixture = ModelFixture(
        spec=IBP_SPEC_YAML, data=strokes, raw_schema=IBP_SCHEMA, registry=registry
    )
    report = check_model(IBPFeaturesModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "output_space_and_units",
        "outputs_match_manifest",
        "seed_determinism",
    } <= ran


# ------------------------------------------------------------------------------- real data


@pytest.mark.skipif(
    CHALLENGES_CSV is None or CHALLENGES_TEST_CSV is None,
    reason="pymc-challenges data not present",
)
def test_from_challenges_reads_the_notebook_digits() -> None:
    assert CHALLENGES_CSV is not None
    ds = from_challenges(CHALLENGES_CSV)
    assert validate_dataset(IBP_SCHEMA, ds) == []
    assert dict(ds.sizes) == {"digit": 500, "row": 8, "col": 6}
    writers = ds["writers"].values
    assert (writers == "train").sum() == 300 and (writers == "test").sum() == 200
    labels = ds["label"].values
    assert np.bincount(labels[:300]).tolist() == [30] * 10
    assert np.bincount(labels[300:]).tolist() == [20] * 10
    px = ds["pixels"].values
    assert px.min() == 0.0 and px.max() == 1.0
    np.testing.assert_allclose(px * 16, np.round(px * 16))
    vis = ds["visible"].values
    assert vis[:300].all() and vis[300:, :4].all() and not vis[300:, 4:].any()
    # the notebook's selection: rng(2005), 30 of each class from the training file
    raw = np.loadtxt(CHALLENGES_CSV, delimiter=",", dtype=np.int64)
    rng = np.random.default_rng(2005)
    first = rng.choice(np.flatnonzero(raw[:, 64] == 0), 30, replace=False)[0]
    np.testing.assert_allclose(px[0], raw[first, :64].reshape(8, 8)[:, 1:7] / 16)
    # without the test file: the training digits only
    alone = from_challenges(CHALLENGES_CSV, test_path=Path("/nonexistent.csv"))
    assert alone.sizes["digit"] == 300 and bool(alone["visible"].all())


@pytest.mark.slow
@pytest.mark.skipif(
    CHALLENGES_CSV is None or CHALLENGES_TEST_CSV is None,
    reason="pymc-challenges data not present",
)
def test_real_digits_fit_and_complete_the_test_writers(registry: Registry) -> None:
    """A short fit of the E31 setup (K = 8, sigma_X = 0.25): no chain collapses and the
    hidden bottom halves of the 200 test digits are completed about as in the notebook."""
    assert CHALLENGES_CSV is not None
    ds = from_challenges(CHALLENGES_CSV)
    spec = DIGITS_SPEC_YAML.replace("tune: 1000", "tune: 300").replace(
        "draws: 1000", "draws: 150"
    ).replace("chains: 4", "chains: 2")  # fmt: skip
    b = fit_spec(spec, ds, registry=registry, raw_schema=IBP_SCHEMA)
    post = b.model.posterior
    pi = xr.apply_ufunc(np.exp, post["logpi"]).mean("draw")
    assert float(pi.isel(feature=0).min()) > 0.2  # no chain switched the features off
    assert 1.0 < float(post["alpha"].mean()) < 10.0
    assert abs(float(post["sigma_A"].mean()) - NOTEBOOK_REFERENCE["sigma_A_mean"]) < 0.05
    held: Any = b.model.learned["heldout"]
    assert held["images"] == 200
    assert held["rmse"] < 0.31  # notebook 0.283; PPCA-10 0.291, the average digit ~0.37
    assert held["lpd_per_image"] > -3.0  # notebook -0.3 nats per digit
    codes = {d.code for d in IBPFeaturesModel().diagnostics(b.model, ds)}
    assert "TRUNCATION_BINDS" in codes  # pi_8 ~ 0.3: K = 8 is too few, as in the notebook
