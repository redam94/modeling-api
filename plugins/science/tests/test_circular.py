"""``sci.circular_regression`` (E81): the canonical-link von Mises regression, its projected
normal and regime-mixture variants, the log-Fourier activity densities, the score modes, the
typed errors, recovery on synthetic data and the real Santa Barbara and Barro Colorado data."""

from __future__ import annotations

import math

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.circular import (
    _KEEP_VARS,
    KINDS,
    TARGET,
    THETA,
    TWO_PI,
    CircularRegressionModel,
    CircularRegressionSettings,
    _Draws,
    _read,
    build_model,
    harmonic_features,
    log_fourier_density,
    pn_logpdf,
    pn_resultant,
    vm_logpdf,
)
from mapi_plugin_science.circular_data import (
    ACTIVITY_FILE,
    ACTIVITY_SCHEMA,
    ACTIVITY_SPEC_YAML,
    BCI_SPEC_YAML,
    BCI_SPECIES,
    CHALLENGES_FILE,
    WIND_SCHEMA,
    WIND_SPEC_YAML,
    WIND_TRUTH,
    activity_dataset,
    activity_truth,
    from_challenges,
    from_challenges_activity,
    ksba_spec_yaml,
    wind_dataset,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError
from scipy import integrate

WIND_FILE = challenges_file(CHALLENGES_FILE)
CAMERA_FILE = challenges_file(ACTIVITY_FILE)
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/circular\n"
CANON = {"report": "obs", "wind_from": TARGET, "hour_lst": "hour", "day_of_year": "day"}


def _req(body: str) -> object:
    return load_score_request(HEADER + body)


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(CircularRegressionModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def winds() -> xr.Dataset:
    return wind_dataset(seed=0)


@pytest.fixture(scope="module")
def wind_bundle(registry: Registry, winds: xr.Dataset) -> Bundle:
    return fit_spec(WIND_SPEC_YAML, winds, registry=registry, raw_schema=WIND_SCHEMA)


@pytest.fixture(scope="module")
def records() -> xr.Dataset:
    return activity_dataset(seed=0)


@pytest.fixture(scope="module")
def activity_bundle(registry: Registry, records: xr.Dataset) -> Bundle:
    return fit_spec(ACTIVITY_SPEC_YAML, records, registry=registry, raw_schema=ACTIVITY_SCHEMA)


def _canon(ds: xr.Dataset) -> xr.Dataset:
    return ds.rename({k: v for k, v in CANON.items() if k in ds.variables or k in ds.dims})


# ------------------------------------------------------------------------------- manifest


def test_manifest_declares_frames_grids_and_the_modes() -> None:
    man = CircularRegressionModel.manifest()
    assert man.id == "sci.circular_regression"
    assert man.capabilities.score_modes == frozenset({"predict", "simulate", "evaluate"})
    assert man.capabilities.loo_valid
    dens = man.outputs["density"]
    assert dens.dims == ["frame", "angle"] and dens.grid is not None
    assert (dens.grid.y, dens.grid.x) == ("frame", "angle")
    ov = man.outputs["overlap"].grid
    assert ov is not None and (ov.y, ov.x) == ("frame", "frame_")
    assert man.outputs["direction_hat"].units_of == TARGET
    assert man.outputs["mean_direction"].units_of == TARGET
    assert {"sci.rose", "sci.profile_bands", "sci.field_map"} <= set(man.plots)


@pytest.mark.parametrize("kind", KINDS)
def test_the_schema_template_follows_the_kind(kind: str) -> None:
    man = CircularRegressionModel.manifest()
    schema = man.schema_template.resolve({"kind": kind, "group_dim": "species"})
    assert schema.dims["obs"].policy.value == "extend"
    assert schema.vars[TARGET].role.value == "target"
    if kind == "activity":
        assert schema.dims["species"].policy.value == "fixed"
        assert schema.vars["species_idx"].index_of == "species"
        assert "hour" not in schema.vars
    else:
        assert {"hour", "day"} <= set(schema.vars) and "species" not in schema.dims


@pytest.mark.parametrize(
    ("spec", "schema"), [(WIND_SPEC_YAML, WIND_SCHEMA), (ACTIVITY_SPEC_YAML, ACTIVITY_SCHEMA)]
)
def test_static_validation_resolves_the_specs(
    registry: Registry, spec: str, schema: object
) -> None:
    res = validate_spec(load_model_spec(spec), registry.catalog(), schema)  # type: ignore[arg-type]
    assert res.ok, [(i.pointer, i.message) for i in res.errors]


def test_settings_refuse_inconsistent_harmonics() -> None:
    with pytest.raises(MapiError) as err:
        CircularRegressionModel().parse_settings({"daily_harmonics": 1, "interaction_harmonics": 2})
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    with pytest.raises(MapiError) as err:
        CircularRegressionModel().parse_settings({"annual_harmonics": 0})
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    s = CircularRegressionModel().parse_settings(
        {"annual_harmonics": 0, "interaction_harmonics": 0}
    )
    assert s.daily_harmonics == 3


# ------------------------------------------------------------------------------- numerics


def test_the_densities_integrate_to_one() -> None:
    for e1, e2 in ((0.0, 0.0), (1.3, -0.4), (6.0, 2.0)):
        for fn in (vm_logpdf, pn_logpdf):
            got, _ = integrate.quad(
                lambda t, f=fn, a=e1, b=e2: math.exp(f(t, a, b)), -math.pi, math.pi
            )
            assert got == pytest.approx(1.0, abs=1e-8)
    for r in (0.2, 1.1, 3.0):
        rho, _ = integrate.quad(
            lambda t, m=r: math.cos(t) * math.exp(pn_logpdf(t, m, 0.0)), 0, TWO_PI
        )
        assert float(pn_resultant(r)) == pytest.approx(rho, rel=1e-6)
    rng = np.random.default_rng(1)
    a, b = rng.normal(size=(2, 6)) / np.arange(1, 7)
    f = log_fourier_density(a, b, np.arange(512) * TWO_PI / 512, 256)
    assert f.sum() * TWO_PI / 512 == pytest.approx(1.0, abs=1e-10)


def test_the_e81_design_has_19_features() -> None:
    hour = np.array([0.0, 6.0, 15.5])
    day = np.array([0.0, 100.0, 300.0])
    feats, names = harmonic_features(hour, day, daily=3, annual=2, interactions=2)
    assert feats.shape == (3, 19) and names[0] == "1"
    assert names[1:3] == ["cos1h", "sin1h"] and names[7:9] == ["cos1d", "sin1d"]
    assert names[-1] == "sin2h*sin1d"
    np.testing.assert_allclose(feats[1, 1:3], [math.cos(math.pi / 2), 1.0], atol=1e-12)


@pytest.mark.parametrize("kind", KINDS)
def test_the_fit_potential_equals_the_pointwise_log_density(kind: str) -> None:
    """The PyTensor potential and the NumPy pointwise log density (stored for LOO) agree."""
    if kind == "activity":
        ds = activity_dataset(sizes=(40, 30, 20), seed=2).rename(
            {"record": "obs", "clock_time": TARGET, "species_idx": "group_idx", "species": "group"}
        )
        s = CircularRegressionSettings(kind="activity", units="hours", harmonics=5)
    else:
        ds = _canon(wind_dataset(n=90, seed=3))
        s = CircularRegressionSettings(kind=kind)  # type: ignore[arg-type]
    data = _read(ds, s, score=False)
    model = build_model(data, s)
    rng = np.random.default_rng(4)
    point = {k: rng.normal(scale=0.5, size=np.shape(v)) for k, v in model.initial_point().items()}
    (pot,) = model.replace_rvs_by_values(model.potentials)
    graph = model.compile_fn(pot, inputs=model.value_vars, on_unused_input="ignore")(point)
    values: dict[str, np.ndarray] = {}
    for name in _KEEP_VARS[kind]:
        rv = model[name]
        values[name] = np.asarray(
            model.compile_fn(
                model.replace_rvs_by_values([rv])[0],
                inputs=model.value_vars,
                on_unused_input="ignore",
            )(point)
        )[None]
    draws = _Draws(values, 1)
    ll = CircularRegressionModel._pointwise(s, data, draws)
    assert float(graph) == pytest.approx(float(ll.sum()), rel=1e-9)


# ------------------------------------------------------------------------------- typed errors


def test_fit_data_errors_are_typed() -> None:
    s = CircularRegressionSettings()
    ds = _canon(wind_dataset(n=30, seed=0))
    with pytest.raises(MapiError) as err:
        _read(ds.drop_vars("hour"), s, score=False)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    bad = ds.assign(hour=ds["hour"] + 30.0)
    with pytest.raises(MapiError) as err:
        _read(bad, s, score=False)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "hour" in str(err.value)
    nan = ds.assign({TARGET: ds[TARGET].where(ds["obs"] != 3)})
    with pytest.raises(MapiError) as err:
        _read(nan, s, score=False)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "calm" in str(err.value)
    act = activity_dataset(sizes=(5, 5, 5)).rename(
        {"record": "obs", "clock_time": TARGET, "species_idx": "group_idx", "species": "group"}
    )
    act = act.assign(group_idx=act["group_idx"] + 3)
    with pytest.raises(MapiError) as err:
        _read(act, CircularRegressionSettings(kind="activity", units="hours"), score=False)
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_score_without_the_hour_is_a_missing_covariate(
    wind_bundle: Bundle, winds: xr.Dataset, registry: Registry
) -> None:
    payload = winds.isel(report=slice(0, 5)).drop_vars(["wind_from", "hour_lst"])
    with pytest.raises(MapiError) as err:
        score_bundle(wind_bundle, _req("mode: predict\n"), payload, registry=registry)
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


@pytest.mark.parametrize(
    "body", ["mode: forecast\nhorizon: {dim: report, periods: 2}\n", "mode: counterfactual\n"]
)
def test_unsupported_modes_are_refused(
    wind_bundle: Bundle, winds: xr.Dataset, registry: Registry, body: str
) -> None:
    if "counterfactual" in body:
        body += "interventions: [{var: hour_lst, add: 1.0}]\n"
    with pytest.raises(MapiError) as err:
        score_bundle(wind_bundle, _req(body), winds.isel(report=slice(0, 4)), registry=registry)
    assert err.value.code is ErrorCode.MODE_NOT_SUPPORTED


# ------------------------------------------------------------------------------- wind fit


def test_wind_fit_recovers_the_coefficients(wind_bundle: Bundle, winds: xr.Dataset) -> None:
    groups = wind_bundle.model.groups
    post = groups["posterior"]["B"]
    assert list(post["feature"].values) == list(WIND_TRUTH)
    for feat, truth in WIND_TRUTH.items():
        for c, want in zip(("eta1", "eta2"), truth, strict=True):
            draws = post.sel(feature=feat, component=c).values.ravel()
            lo, hi = np.quantile(draws, [0.01, 0.99])
            assert lo < want < hi, (feat, c, lo, hi, want)
    ll = groups["log_likelihood"][TARGET]
    assert ll.dims == ("chain", "draw", "obs") and ll.sizes["obs"] == winds.sizes["report"]
    assert bool(np.isfinite(ll).all())
    rep = groups["posterior_predictive"][THETA]
    assert float(rep.min()) >= 0.0 and float(rep.max()) < TWO_PI
    obs = groups["observed_data"]
    np.testing.assert_allclose(obs[TARGET].values, winds["wind_from"].values)
    np.testing.assert_allclose(obs[THETA].values, np.deg2rad(winds["wind_from"].values) % TWO_PI)
    elpd = wind_bundle.metrics.elpd["elpd_loo"]
    assert elpd.n == winds.sizes["report"] and elpd.elpd < 0
    assert wind_bundle.evaluation is not None


def test_predict_gives_directions_frames_and_a_normalised_density(
    wind_bundle: Bundle, winds: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        wind_bundle, _req("mode: predict\n"), winds.drop_vars("wind_from"), registry=registry,
        return_full=True,
    )  # fmt: skip
    pred = out["predictions"].to_dataset()
    d = pred["direction_hat"]
    assert d.attrs["kind"] == "expected"
    assert float(d.min()) >= 0.0 and float(d.max()) < 360.0
    r = pred["resultant"]
    assert float(r.min()) >= 0.0 and float(r.max()) <= 1.0
    np.testing.assert_allclose(pred["frame"].values, [0.0, 6.0, 12.0, 18.0])
    dens = pred["density"]
    assert dens.dims == ("chain", "draw", "frame", "angle")
    mass = dens.sum("angle") * float(np.diff(pred["angle"].values)[0])
    np.testing.assert_allclose(mass.values, 1.0, atol=1e-6)
    # the mean resultant vector of a frame points where the density of the frame peaks
    mean = dens.mean(("chain", "draw"))
    peak = mean["angle"].values[mean.argmax("angle").values]
    md = pred["mean_direction"].mean(("chain", "draw")).values
    gap = np.abs((peak - md + 180.0) % 360.0 - 180.0)
    strong = pred["resultant_length"].mean(("chain", "draw")).values > 0.4
    assert strong.any() and (gap[strong] < 45.0).all(), (peak, md)
    summary = out["summary"].to_dataset()
    assert summary["direction_hat"].attrs["units"] == "deg"


def test_predictive_and_simulate_draw_angles_with_a_seed(
    wind_bundle: Bundle, winds: xr.Dataset, registry: Registry
) -> None:
    payload = winds.drop_vars("wind_from")

    def run(body: str) -> xr.DataArray:
        out = score_bundle(wind_bundle, _req(body), payload, registry=registry, return_full=True)
        return out["predictions"].to_dataset()["direction_hat"]

    a = run("mode: predict\noutput: {kind: predictive}\nseed: 3\n")
    b = run("mode: simulate\nseed: 3\n")
    c = run("mode: simulate\nseed: 4\n")
    assert a.attrs["kind"] == "predictive" and b.attrs["noise"] == "seeded_stream"
    xr.testing.assert_identical(a.drop_attrs(), b.drop_attrs())
    assert not np.allclose(b.values, c.values)
    # simulated directions spread around the predicted mean direction
    exp = run("mode: predict\n")
    gap = np.deg2rad(b.values - exp.values)
    assert np.mean(np.cos(gap)) > 0.2


def test_evaluate_scores_the_holdout(
    wind_bundle: Bundle, winds: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        wind_bundle,
        _req("mode: evaluate\ndata: dataset://sci/holdout@v1\n"),
        winds.isel(report=slice(-60, None)),
        registry=registry,
        return_full=True,
    )
    assert "metrics" in out.attrs and "predictions" in out
    model = CircularRegressionModel()
    lpd = model.holdout_log_score(wind_bundle.model, _canon(winds.isel(report=slice(-60, None))))
    assert lpd.shape == (60,) and np.isfinite(lpd).all()
    # better than knowing nothing (the uniform density, -log 2 pi per radian)
    assert lpd.mean() > -math.log(TWO_PI)


@pytest.mark.mapi_contract
def test_circular_regression_passes_the_contract_kit(registry: Registry, winds: xr.Dataset) -> None:
    fixture = ModelFixture(
        spec=WIND_SPEC_YAML, data=winds, raw_schema=WIND_SCHEMA, registry=registry
    )
    report = check_model(CircularRegressionModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "unsupported_mode",
        "outputs_match_manifest",
        "output_space_and_units",
        "seed_determinism",
    } <= ran


# ------------------------------------------------------------------------------- activity


def test_activity_recovers_levels_and_overlaps(
    activity_bundle: Bundle, records: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        activity_bundle,
        _req("mode: predict\n"),
        records.isel(record=slice(0, 20)).drop_vars("clock_time"),
        registry=registry,
        return_full=True,
    )
    pred = out["predictions"].to_dataset()
    truth = activity_truth()
    assert list(pred["frame"].values) == truth["species"]
    np.testing.assert_allclose(pred["angle"].values[:3], [0.0, 1 / 3, 2 / 3])
    mass = pred["density"].sum("angle") * (24.0 / 72)
    np.testing.assert_allclose(mass.values, 1.0, atol=1e-6)
    ov = pred["overlap"].median(("chain", "draw")).values
    np.testing.assert_allclose(np.diag(ov), 1.0, atol=1e-9)
    assert np.abs(ov - truth["overlap"]).max() < 0.12, (ov, truth["overlap"])
    act = pred["activity_level"].median(("chain", "draw")).values
    assert np.abs(act - truth["activity_level"]).max() < 0.12, (act, truth["activity_level"])
    # a record's mean direction is the mean direction of its species
    idx = records["species_idx"].values[:20]
    md = pred["mean_direction"].values[..., idx]
    np.testing.assert_allclose(pred["direction_hat"].values, md, atol=1e-9)
    ll = activity_bundle.model.groups["log_likelihood"][TARGET]
    assert ll.sizes["obs"] == records.sizes["record"]


def test_activity_simulates_times_of_day(
    activity_bundle: Bundle, records: xr.Dataset, registry: Registry
) -> None:
    payload = records.drop_vars("clock_time")
    out = score_bundle(
        activity_bundle, _req("mode: simulate\nseed: 1\n"), payload, registry=registry,
        return_full=True,
    )  # fmt: skip
    sim = out["predictions"].to_dataset()["direction_hat"]
    assert float(sim.min()) >= 0.0 and float(sim.max()) < 24.0
    # the night species (owl) is simulated at night, the day species (lark) by day
    idx = records["species_idx"].values
    night = np.cos(TWO_PI * sim.values[..., idx == 0] / 24.0).mean()
    day = np.cos(TWO_PI * sim.values[..., idx == 1] / 24.0).mean()
    assert night > 0.3 and day < -0.2


# ------------------------------------------------------------------------------- variants


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["projected_normal", "regime_mixture"])
def test_the_variants_fit_and_store_pointwise_log_densities(
    registry: Registry, winds: xr.Dataset, kind: str
) -> None:
    spec = WIND_SPEC_YAML.replace("kind: von_mises", f"kind: {kind}").replace(
        "mode: evaluate_then_refit", "mode: train_only"
    )
    b = fit_spec(spec, winds, registry=registry, raw_schema=WIND_SCHEMA)
    post = b.model.groups["posterior"]
    if kind == "regime_mixture":
        assert set(post.data_vars) == {"B0", "Bh", "g"}
        assert list(post["regime"].values) == ["easterly", "westerly"]
    else:
        assert set(post.data_vars) == {"B"}
    elpd = b.metrics.elpd["elpd_loo"]
    assert elpd.n == winds.sizes["report"] - 60  # train_only: the holdout is not fitted
    out = score_bundle(b, _req("mode: predict\n"), winds.drop_vars("wind_from"), registry=registry,
                       return_full=True)  # fmt: skip
    dens = out["predictions"].to_dataset()["density"]
    mass = dens.sum("angle") * 5.0
    np.testing.assert_allclose(mass.values, 1.0, atol=1e-6)


# ------------------------------------------------------------------------------- real data


@pytest.mark.skipif(WIND_FILE is None, reason="pymc-challenges data not present")
def test_from_challenges_reads_the_santa_barbara_winds() -> None:
    assert WIND_FILE is not None
    ds = from_challenges(WIND_FILE)
    assert validate_dataset(WIND_SCHEMA, ds) == []
    years = ds["year"].values
    # E81: 12,023 reports in 2021-2022 and 6,026 in 2023 once calms are dropped
    assert int((years < 2023).sum()) == 12_023 and int((years == 2023).sum()) == 6_026
    assert bool((ds["wind_from"].values % 10 == 0).all())
    assert float(ds["hour_lst"].max()) < 24.0 and float(ds["day_of_year"].max()) < 366.0
    assert "last: 6026" in ksba_spec_yaml(ds)


@pytest.mark.skipif(CAMERA_FILE is None, reason="pymc-challenges data not present")
def test_from_challenges_activity_reads_the_camera_traps() -> None:
    assert CAMERA_FILE is not None
    ds = from_challenges_activity(CAMERA_FILE)
    assert validate_dataset(ACTIVITY_SCHEMA, ds) == []
    counts = np.bincount(ds["species_idx"].values, minlength=len(BCI_SPECIES))
    assert dict(zip(BCI_SPECIES, counts.tolist(), strict=True)) == {
        "ocelot": 317, "agouti": 10_292, "paca": 999, "rat": 893, "brocket": 816,
        "peccary": 2_965,
    }  # fmt: skip
    assert float(ds["clock_time"].min()) >= 0.0 and float(ds["clock_time"].max()) < 24.0


@pytest.mark.slow
@pytest.mark.skipif(WIND_FILE is None, reason="pymc-challenges data not present")
def test_real_winds_fit_beats_one_direction_on_the_held_out_year(registry: Registry) -> None:
    """E81 part D at reduced sampling: the 2023 log score is about -1.52 per report (the
    notebook: -1.517; one direction for the whole year: -1.799)."""
    assert WIND_FILE is not None
    ds = from_challenges(WIND_FILE)
    spec = ksba_spec_yaml(ds).replace("tune: 1000, draws: 1000", "tune: 300, draws: 200")
    spec = spec.replace("keep: {log_likelihood: true, posterior_predictive: {thin: 20}}", "")
    b = fit_spec(spec, ds, registry=registry, raw_schema=WIND_SCHEMA)
    test = _canon(ds.isel(report=np.flatnonzero(ds["year"].values == 2023)))
    lpd = CircularRegressionModel().holdout_log_score(b.model, test)
    assert lpd.mean() == pytest.approx(-1.517, abs=0.01)


@pytest.mark.slow
@pytest.mark.skipif(CAMERA_FILE is None, reason="pymc-challenges data not present")
def test_real_camera_traps_give_the_notebook_overlaps(registry: Registry) -> None:
    """E81 part E: the ocelot overlaps least with the agouti (0.30) and most with the
    brocket (0.75); the agouti is active about a quarter of the day."""
    assert CAMERA_FILE is not None
    ds = from_challenges_activity(CAMERA_FILE)
    spec = BCI_SPEC_YAML.replace("tune: 1000, draws: 1000", "tune: 300, draws: 250")
    b = fit_spec(spec, ds, registry=registry, raw_schema=ACTIVITY_SCHEMA)
    out = score_bundle(
        b, _req("mode: predict\n"), ds.isel(record=slice(0, 6)).drop_vars("clock_time"),
        registry=registry, return_full=True,
    )  # fmt: skip
    pred = out["predictions"].to_dataset()
    ov = pred["overlap"].sel(frame="ocelot").median(("chain", "draw"))
    assert float(ov.sel(frame_="agouti")) == pytest.approx(0.303, abs=0.03)
    assert float(ov.sel(frame_="brocket")) == pytest.approx(0.750, abs=0.03)
    act = pred["activity_level"].median(("chain", "draw"))
    assert float(act.sel(frame="agouti")) == pytest.approx(0.262, abs=0.02)
