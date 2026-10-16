"""``mmm.bayesinsight``: the BayesInsight math, the graph (prior draws, no sampling), and -
marked slow - the contract kit, a calibrated fit and scoring through the SDK harness."""

from __future__ import annotations

import copy
import json
from typing import Any

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import pytest
import scipy.stats
import xarray as xr
from mapi_plugin_mmm.bayesinsight import BayesInsightMMM
from mapi_plugin_mmm.bayesinsight_examples import (
    BI_L_MAX,
    BI_MODEL_DEF,
    BITruth,
    bayesinsight_dataset,
    bayesinsight_raw_schema,
    bi_spec_text,
)
from mapi_plugin_mmm.bayesinsight_math import (
    adstock_weights,
    bspline_basis,
    fourier_features,
    media_curve,
    nonzero_index,
)
from mapi_pymc.context import BuildContext
from mapi_sdk.harness import fit_spec, prepare_model_stage, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_score_request
from mapi_spec.bayesinsight_model import BayesInsightSettings
from mapi_spec.calibration import Calibration
from mapi_spec.errors import ErrorCode, MapiError
from scipy.interpolate import BSpline

RNG = np.random.default_rng(3)


# ===================================================================================== math


def test_the_spline_basis_is_patsys_bs_with_an_intercept() -> None:
    t = np.linspace(0, 1, 41)
    knots = np.concatenate([np.zeros(4), np.linspace(0, 1, 8)[1:-1], np.ones(4)])
    want = BSpline.design_matrix(t, knots, 3).toarray()
    got = bspline_basis(t, 6, 3)
    np.testing.assert_allclose(got, want, atol=1e-12)
    np.testing.assert_allclose(got.sum(axis=1), 1.0)
    # past the fit window the trend holds its last level
    np.testing.assert_allclose(bspline_basis(np.array([1.3]), 6, 3), got[-1:], atol=1e-12)


def test_fourier_features_follow_bayesinsights_time_axis() -> None:
    n, period = 30, 52.18
    f = fourier_features(np.arange(n), n, period, 2)
    t = np.linspace(0, 2 * np.pi * n / period, n)  # BayesInsight's grid
    np.testing.assert_allclose(f, np.stack([np.cos(t), np.sin(t), np.cos(2 * t),
                                            np.sin(2 * t)], axis=-1), atol=1e-12)  # fmt: skip
    ahead = fourier_features(np.arange(n, n + 3), n, period, 2)
    step = t[1] - t[0]
    np.testing.assert_allclose(ahead[:, 0], np.cos(t[-1] + step * np.arange(1, 4)), atol=1e-12)
    with pytest.raises(ValueError):
        fourier_features(np.arange(1), 1, period, 1)


def test_carry_over_weights() -> None:
    delayed = adstock_weights("delayed", 6, {"alpha": 0.5, "theta": 2.0})
    assert delayed.sum() == pytest.approx(1.0) and int(np.argmax(delayed)) == 2
    geometric = adstock_weights("geometric", 4, {"alpha": 0.5})
    np.testing.assert_allclose(geometric, np.array([1, 0.5, 0.25, 0.125]) / 1.875)
    np.testing.assert_allclose(adstock_weights("none", 3, {}), [1.0, 0.0, 0.0])
    lam, k = 2.0, 3.0
    pdf = scipy.stats.weibull_min.pdf(np.arange(1, 7), k, scale=lam)
    want = (pdf - pdf.min()) / (pdf.max() - pdf.min())
    got = adstock_weights("weibull", 6, {"lam": lam, "k": k})
    np.testing.assert_allclose(got, want / want.sum(), rtol=1e-9)
    surv = scipy.stats.weibull_min.sf(np.arange(1, 6), k, scale=lam)
    cdf = np.concatenate([[1.0], np.cumprod(surv)])
    got_cdf = adstock_weights("weibull", 6, {"lam": lam, "k": k}, weibull="CDF")
    np.testing.assert_allclose(got_cdf, cdf / cdf.sum(), rtol=1e-9)
    # the graph computes the same weights
    sym = adstock_weights("weibull", 6, {"lam": pt.constant(lam), "k": pt.constant(k)},
                          weibull="CDF", xp=pt)  # fmt: skip
    np.testing.assert_allclose(sym.eval(), got_cdf, rtol=1e-9)


def test_media_curves() -> None:
    z = np.array([0.0, 0.8, 5.0, 1e3])
    hill = media_curve(z, "hill", {"K": 0.8, "n": 2.0})
    assert hill[0] == pytest.approx(0.0, abs=1e-12) and hill[1] == pytest.approx(0.5)
    assert hill[-1] == pytest.approx(1.0, abs=1e-6)
    s = media_curve(np.array([0.0, 0.01, 0.2, 1.0]), "sshaped", {"alpha": 0.88, "beta": 100.0})
    assert s[0] == pytest.approx(0.0, abs=1e-12) and np.all(np.diff(s) > 0)
    assert s[-1] == pytest.approx(1.0, abs=1e-4)
    # S-shaped: the slope first grows, then shrinks
    grid = np.linspace(0, 0.6, 61)
    slope = np.diff(media_curve(grid, "sshaped", {"alpha": 0.88, "beta": 100.0}))
    assert np.argmax(slope) not in (0, slope.size - 1)
    np.testing.assert_allclose(media_curve(z, "linear", {}), z)
    idx = nonzero_index(np.array([[0.0, 2.0, 4.0, 0.0], [0.0, 0.0, 0.0, 0.0]]), "median")
    np.testing.assert_allclose(idx, [3.0, 1.0])  # no spend at all: index 1


# ==================================================================================== graph


@pytest.fixture(scope="module")
def raw() -> tuple[xr.Dataset, BITruth]:
    return bayesinsight_dataset(weeks=30, seed=2)


def _stage(
    registry: Registry, raw: xr.Dataset, model_def: dict[str, Any] | None = None, **replace: str
) -> tuple[xr.Dataset, BayesInsightSettings, Any]:
    stage = prepare_model_stage(
        bi_spec_text(model_def, **replace), raw, registry=registry,
        raw_schema=bayesinsight_raw_schema(raw),
    )  # fmt: skip
    return stage.data, BayesInsightSettings.model_validate(stage.settings), stage


def _build(
    ds: xr.Dataset, s: BayesInsightSettings, *, lift: Any = None
) -> tuple[pm.Model, BayesInsightMMM]:
    plugin = BayesInsightMMM()
    plugin._stats = plugin._fit_stats(ds, s)
    plugin._lift = lift
    ctx = BuildContext(phase="fit", ds=ds)
    return plugin.build(ds, s, ctx), plugin


def _with_likelihood(kind: str) -> dict[str, Any]:
    md = copy.deepcopy(BI_MODEL_DEF)
    lik: dict[str, Any] = {"type": kind}
    if kind in (
        "Poisson",
        "NegativeBinomial",
        "ZeroInflatedPoisson",
        "ZeroInflatedNegativeBinomial",
    ):
        md["variable_details"][0]["intercept_prior"] = {
            "coeff_dist": "Normal",
            "coeff_params": {"mu": 5.0, "sigma": 0.2},
        }
        for v in md["variable_details"][1:3]:
            v["coeff_prior"] = {
                "coeff_dist": "LogNormal",
                "coeff_params": {"mu": -2.0, "sigma": 0.3},
            }
        md["variable_details"][3]["coeff_prior"] = {
            "coeff_dist": "Normal",
            "coeff_params": {"mu": 0.0, "sigma": 0.1},
        }
        md["variable_details"][4]["coeff_prior"] = {
            "coeff_dist": "Normal",
            "coeff_params": {"mu": 0.0, "sigma": 0.05},
        }
        md["variable_details"][0]["partial_pooling_sigma"] = 0.1
    elif kind == "LogNormal":
        md["variable_details"][0]["intercept_prior"] = {
            "coeff_dist": "Normal",
            "coeff_params": {"mu": 5.0, "sigma": 0.2},
        }
        md["variable_details"][0]["partial_pooling_sigma"] = 0.1
        lik["likelihood_params"] = {"sigma": 0.2}
    md["variable_details"][0]["likelihood"] = lik
    return md  # fmt: skip


@pytest.mark.parametrize(
    "kind",
    ["Normal", "LogNormal", "Poisson", "NegativeBinomial", "ZeroInflatedPoisson",
     "ZeroInflatedNegativeBinomial"],
)  # fmt: skip
def test_contributions_sum_to_the_expected_kpi_for_every_likelihood(
    kind: str, registry: Registry, raw: tuple[xr.Dataset, BITruth]
) -> None:
    data = raw[0].copy()
    if kind not in ("Normal", "LogNormal"):
        data["Sales"] = np.round(data["Sales"])  # counts
    ds, s, _ = _stage(registry, data, _with_likelihood(kind))
    model, _ = _build(ds, s)
    mu, contrib = pm.draw([model["mu"], model["contributions"]], draws=8, random_seed=1)
    assert np.isfinite(mu).all()
    if kind != "Normal":
        assert (mu > 0).all()
    np.testing.assert_allclose(contrib.sum(axis=1), mu, rtol=1e-6, atol=1e-6)
    comps = list(model.coords["component"])
    assert comps == ["tv", "search", "baseline", "price", "annual_seasonality"]
    # media contributions are never negative (positive coefficients, positive curves)
    assert (contrib[:, :2] >= -1e-9).all()
    assert model["y"] in model.observed_RVs


def test_signs_hold_for_every_level(registry: Registry, raw: tuple[xr.Dataset, BITruth]) -> None:
    md = copy.deepcopy(BI_MODEL_DEF)
    md["variable_details"][3].update(random_coeff_dims=["Geography", "Product"],
                                     partial_pooling_sigma=2.0)  # fmt: skip
    ds, s, _ = _stage(registry, raw[0], md)
    model, plugin = _build(ds, s)
    contrib = pm.draw(model["contributions"], draws=50, random_seed=2)[:, 3]  # price
    stats = plugin._fit_stats(ds, s)
    xc = plugin._control_input(
        ds["price"].transpose("geo", "product", "time").values,
        s.model_def.controls[0],
        stats["controls"]["price"],
    )
    coef = contrib / np.where(np.abs(xc) > 1e-9, xc, np.nan)
    assert np.nanmax(coef) <= 1e-9  # negative for every draw, geo and product
    assert np.nanstd(np.nanmean(coef, axis=-1), axis=(1, 2)).mean() > 0  # levels differ
    assert pm.draw(model["price__coef"], draws=50, random_seed=3).max() <= 0  # truncated


@pytest.mark.parametrize(
    ("adstock", "curve", "prior"),
    [
        ("geometric", "linear", {}),
        ("weibull", "hill", {"adstock_prior": {"type": "Weibull", "kind": "CDF"}}),
        ("none", "sshaped", {}),
        ("weibull", "sshaped", {}),
    ],
)
def test_every_curve_and_carry_over_builds(
    adstock: str, curve: str, prior: dict[str, Any], registry: Registry,
    raw: tuple[xr.Dataset, BITruth],
) -> None:  # fmt: skip
    md = copy.deepcopy(BI_MODEL_DEF)
    tv = md["variable_details"][1]
    tv.update(adstock=adstock, media_transform=curve, **prior)
    tv.pop("media_transform_prior")
    if adstock == "weibull" and "adstock_prior" not in prior:
        tv.pop("adstock_prior")
    ds, s, _ = _stage(registry, raw[0], md)
    model, _ = _build(ds, s)
    contrib = pm.draw(model["contributions"], draws=6, random_seed=4)
    assert np.isfinite(contrib).all() and (contrib[:, 0] >= -1e-9).all()
    names = {v.name for v in model.free_RVs}
    assert ("tv__w_lam" in names) == (adstock == "weibull")
    assert ("tv__s_alpha" in names) == (curve == "sshaped")


@pytest.mark.parametrize(
    "trend",
    [
        {"random_coeff_dims": None},
        {},
        {"random_coeff_dims": ["Geography"]},
        {"random_coeff_dims": ["Geography"], "grouping_name": "region",
         "grouping_map": {"americas": ["US"], "europe": ["DE"]}},
    ],
)  # fmt: skip
def test_local_trends(
    trend: dict[str, Any], registry: Registry, raw: tuple[xr.Dataset, BITruth]
) -> None:
    md = copy.deepcopy(BI_MODEL_DEF)
    md["variable_details"].append({"variable_name": "local trend", "variable_type": "localtrend",
                                   "num_knots": 3, **trend})  # fmt: skip
    ds, s, _ = _stage(registry, raw[0], md)
    model, _ = _build(ds, s)
    contrib = pm.draw(model["contributions"], draws=6, random_seed=5)
    part = contrib[:, -1]  # (draw, geo, product, time)
    assert np.isfinite(part).all()
    varies_by_geo = not np.allclose(part[:, 0], part[:, 1])
    assert varies_by_geo == bool(trend.get("random_coeff_dims"))
    np.testing.assert_allclose(part[:, :, 0], part[:, :, 1])  # products share the trend
    assert "local_trend__betas" in model.named_vars


def test_a_grouping_map_must_cover_every_level(
    registry: Registry, raw: tuple[xr.Dataset, BITruth]
) -> None:
    md = copy.deepcopy(BI_MODEL_DEF)
    md["variable_details"].append({"variable_name": "trend", "variable_type": "localtrend",
                                   "random_coeff_dims": ["Geography"], "grouping_name": "g",
                                   "grouping_map": {"a": ["US"]}})  # fmt: skip
    ds, s, _ = _stage(registry, raw[0], md)
    with pytest.raises(MapiError) as exc:
        _build(ds, s)
    assert exc.value.code is ErrorCode.SETTINGS_INVALID and "DE" in exc.value.detail


def test_new_levels_pool_random_effects_and_refuse_fixed_ones(
    registry: Registry, raw: tuple[xr.Dataset, BITruth]
) -> None:
    md = copy.deepcopy(BI_MODEL_DEF)
    ds, s, _ = _stage(registry, raw[0], md)
    plugin = BayesInsightMMM()
    plugin._stats = plugin._fit_stats(ds, s)
    new = xr.concat([ds, ds.isel(geo=[0]).assign_coords(geo=["FR"])], dim="geo")
    new = new.assign_coords(geo__is_new=("geo", [False, False, True]))
    fit_coords = {d: np.asarray(ds[d].values) for d in ds.dims if d in ds.coords}
    ctx = BuildContext(phase="score", ds=new, fit_coords=fit_coords)
    model = plugin.build(new, s, ctx)
    assert "geo__fit" in model.coords and "geo__new" in model.coords
    z = model["intercept__coef_z"]
    assert model.named_vars_to_dims[z.name] == ("geo__fit",)
    fixed = copy.deepcopy(md)
    fixed["variable_details"][3]["fixed_ind_coeff_dims"] = ["Geography"]
    ds2, s2, _ = _stage(registry, raw[0], fixed)
    plugin._stats = plugin._fit_stats(ds2, s2)
    with pytest.raises(MapiError) as exc:
        plugin.build(new, s2, BuildContext(phase="score", ds=new, fit_coords=fit_coords))
    assert exc.value.code is ErrorCode.UNSEEN_LEVELS and "fixed_ind_coeff_dims" in exc.value.detail


def test_the_lift_likelihood_matches_its_numpy_twin(
    registry: Registry, raw: tuple[xr.Dataset, BITruth]
) -> None:
    ds, s, stage = _stage(registry, raw[0])
    plugin = BayesInsightMMM()
    plugin._stats = plugin._fit_stats(ds, s)
    cal = Calibration.model_validate(
        {"lift_tests": [{"id": "tv-us", "channel": "TV_Total_Total_Total", "geo": "US",
                         "start": "2023-03-06", "end": "2023-04-02", "spend_delta": 200.0,
                         "kpi_delta": 40.0, "se": 10.0},
                        {"id": "search", "channel": "search", "start": "2023-02-06",
                         "end": "2023-02-12", "spend_delta": -10.0, "kpi_delta": -3.0,
                         "se": 2.0}]}
    )  # fmt: skip
    lift = plugin._lift_design(cal.lift_tests, ds, s, stage.fit_context)
    assert lift is not None and lift.ids == ["tv-us", "search"]
    assert set(lift.cell[0][lift.test == 0].tolist()) == {0}  # US only
    assert len(set(lift.cell[1][lift.test == 0].tolist())) == 2  # both products
    model, _ = _build(ds, s, lift=lift)
    names = ["lift_tests__model", "tv__coef", "tv__K", "tv__n", "tv__retention", "tv__lag",
             "search__coef", "search__K", "search__n", "search__retention"]  # fmt: skip
    draws = pm.draw([model[n] for n in names], draws=5, random_seed=6)
    for d in range(5):
        got = dict(zip(names, (v[d] for v in draws), strict=True))
        want = np.zeros(2)
        for j, (name, kind) in enumerate((("tv", "delayed"), ("search", "geometric"))):
            w = adstock_weights(
                kind,
                BI_L_MAX,
                {"alpha": got[f"{name}__retention"], "theta": got.get(f"{name}__lag", 0.0)},
            )
            sel = lift.media == j
            curve = {"K": got[f"{name}__K"], "n": got[f"{name}__n"]}
            gain = (
                (
                    media_curve(lift.z1[sel], "hill", curve)
                    - media_curve(lift.z0[sel], "hill", curve)
                )
                * lift.valid[sel]
                * w
            ).sum(-1)
            np.add.at(want, lift.test[sel], got[f"{name}__coef"] * gain)  # identity link
        np.testing.assert_allclose(got["lift_tests__model"], want, rtol=1e-6)
    assert draws[0][:, 0].mean() > 0 > draws[0][:, 1].mean()  # extra spend lifts, a cut costs
    with pytest.raises(MapiError) as exc:
        bad = cal.model_copy(update={"lift_tests": [cal.lift_tests[0].model_copy(
            update={"channel": "radio"})]})  # fmt: skip
        plugin._lift_design(bad.lift_tests, ds, s, stage.fit_context)
    assert exc.value.pointer == "/calibration/lift_tests/0/channel"


# ================================================================================ fit, kit


EXPECTED = {
    "manifest",
    "fit",
    "bundle_roundtrip_no_pickle",
    "shuffled_scoring",
    "subset_scoring",
    "new_level_policies",
    "extend_typed_errors",
    "context_window_equivalence",
    "evaluate_path_equals_score_path",
    "output_space_and_units",
    "representation_aggregation",
    "additive_contributions",
    "plots_sanitize",
}
_TINY = {"tune: 300, draws: 200, chains: 2": "tune: 40, draws: 40, chains: 2"}


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_bayesinsight_passes_the_contract_kit(
    registry: Registry, raw: tuple[xr.Dataset, BITruth]
) -> None:
    fixture = ModelFixture(
        spec=bi_spec_text(**_TINY),
        data=raw[0],
        raw_schema=bayesinsight_raw_schema(raw[0]),
        registry=registry,
        window=4,
    )
    report = check_model(BayesInsightMMM, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert ran >= EXPECTED, sorted(EXPECTED - ran)
    detail = report.get("new_level_policies").detail
    assert "geo: new level scored (pooled)" in detail


CALIBRATION = """
calibration:
  lift_tests:
    - {id: tv-us, channel: tv, geo: US, start: 2023-03-06, end: 2023-04-02,
       spend_delta: 400, kpi_delta: KPI, se: 5}
"""


@pytest.mark.slow
def test_a_calibrated_fit_forecasts_and_counterfactuals(registry: Registry) -> None:
    data, truth = bayesinsight_dataset(weeks=40, future_weeks=4, seed=4)
    fit_data = data.isel(date=slice(0, 40))
    schema = bayesinsight_raw_schema(fit_data)
    base = bi_spec_text(**_TINY)
    # the lift the true model implies for the test (numpy twin of the lift likelihood)
    stage = prepare_model_stage(base + CALIBRATION.replace("KPI", "0"), fit_data,
                                registry=registry, raw_schema=schema)  # fmt: skip
    s = BayesInsightSettings.model_validate(stage.settings)
    plugin = BayesInsightMMM()
    plugin._stats = plugin._fit_stats(stage.data, s)
    cal = stage.spec.calibration
    assert cal is not None
    lift = plugin._lift_design(cal.lift_tests, stage.data, s, stage.fit_context)
    assert lift is not None
    w = adstock_weights(
        "delayed", BI_L_MAX, {"alpha": truth.retention["TV"], "theta": truth.lag["TV"]}
    )
    curve = {"K": truth.K["TV"], "n": truth.n["TV"]}
    gain = (media_curve(lift.z1, "hill", curve) - media_curve(lift.z0, "hill", curve)) * lift.valid
    implied = float(truth.coef["TV"] * (gain * w).sum())
    spec = base + CALIBRATION.replace("KPI", f"{implied:.4f}")
    bundle = fit_spec(spec, fit_data, registry=registry, raw_schema=schema)
    learned = bundle.model.learned["calibration"]
    assert learned == {"lift_tests": {"used": ["tv-us"], "skipped": []}}  # type: ignore[comparison-overlap]
    post = bundle.model.posterior
    assert "lift_tests__model" in post and "y" not in post
    modelled = float(post["lift_tests__model"].mean())
    assert abs(modelled - implied) < 4 * 5 + 0.5 * abs(implied)
    assert float(post["price__coef"].max()) <= 0  # the sign constraint
    assert bundle.metrics.values.get("mape") is not None
    # forecast the next four weeks from the stored context
    future = data.isel(date=slice(40, 44)).drop_vars("Sales")
    req = load_score_request(json.dumps({
        "apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": "endpoint://retail/bi",
        "mode": "forecast", "horizon": {"dim": "time", "periods": 4},
        "output": {"kind": "expected", "summary": ["mean", "hdi_90"], "draws": 20},
    }))  # fmt: skip
    tree = score_bundle(bundle, req, future, registry=registry)
    y_hat = tree["summary"].to_dataset()["y_hat"].sel(stat="mean")
    assert y_hat.sizes["date"] == 4 and np.isfinite(y_hat).all()
    truth_y = data["Sales"].isel(date=slice(40, 44))
    assert float(abs(y_hat - truth_y).mean() / truth_y.mean()) < 0.15
    # turning TV off lowers the KPI
    cf = load_score_request(json.dumps({
        "apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": "endpoint://retail/bi",
        "mode": "counterfactual", "interventions": [{"var": "TV", "set": 0.0}],
        "output": {"kind": "expected", "summary": ["mean"]},
    }))  # fmt: skip
    window = fit_data.isel(date=slice(30, 40))
    delta = score_bundle(bundle, cf, window, registry=registry)["delta/summary"].to_dataset()
    assert float(delta["y_hat"].sel(stat="mean").sum()) < 0
