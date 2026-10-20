"""Count and proportion likelihoods: settings, the observation blocks, link-scale replay,
KPI-unit parts, removal-based channel totals, predictive draws, data checks and the
calibration refusals (prior-draw fits; one ``slow`` real fit)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_mmm.bayesian import MMMBayesian
from mapi_plugin_mmm.calibration import check_calibration
from mapi_plugin_mmm.derived import FitWindow, contributions_dataset
from mapi_plugin_mmm.forward import LIKELIHOOD_LINK, history_arrays, inverse_link
from mapi_plugin_mmm.framework import (
    PanelData,
    _platform_mmm_class,
    build_configs,
    detect_controls,
    make_model,
    quiet,
)
from mapi_plugin_mmm.settings import BayesianSettings
from mapi_sdk.models import PREDICTIONS, FitContext, Fitted, ScoreContext
from mapi_spec.calibration import BusinessPriors, Calibration, LiftTest, PriorTranslationJob
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import ValidationError

BASE = {"adstock": {"kind": "geometric", "l_max": 3}, "trend": {"kind": "linear"}}
_COMPONENTS = (
    "intercept_component",
    "trend_component",
    "seasonality_component",
    "geo_component",
    "product_component",
    "media_total",
    "controls_total",
)


def counts(stage: xr.Dataset) -> xr.Dataset:
    return stage.assign(y=np.round(stage["y"] / 10.0))


def shares(stage: xr.Dataset) -> xr.Dataset:
    y = stage["y"]
    return stage.assign(y=(y - y.min() + 50.0) / (float(y.max() - y.min()) + 100.0))


@pytest.fixture(scope="module")
def national(stage_factory: Callable[..., xr.Dataset]) -> xr.Dataset:
    return stage_factory(geos=("all",)).isel(geo=0, drop=True)


@pytest.fixture(scope="module")
def fits(
    national: xr.Dataset,
    geo_stage: xr.Dataset,
    prior_fit_factory: Callable[..., Any],
) -> dict[str, Any]:
    return {
        "poisson": prior_fit_factory(
            counts(geo_stage),
            {**BASE, "likelihood": "poisson", "hierarchy": {"pool_over": ["geo"]}},
        ),
        "negative_binomial": prior_fit_factory(
            counts(national), {**BASE, "likelihood": "negative_binomial"}
        ),
        "beta": prior_fit_factory(shares(national), {**BASE, "likelihood": "beta"}),
    }


def _graph_eta(pf: Any) -> np.ndarray:
    total = 0.0
    for name in _COMPONENTS:
        if name in pf.prior:
            arr = np.asarray(pf.prior[name].values, dtype=float)
            total = total + arr.reshape(arr.shape[0] * arr.shape[1], -1)
    m = pf.model
    eta = np.asarray(total)
    return eta.reshape(eta.shape[0], m.n_periods, m.n_cells).transpose(0, 2, 1)


def _comps(pf: Any) -> Any:
    rp, fit = pf.plugin.replay(pf.fitted)
    spend, ctrl = history_arrays(fit)
    return rp, fit, rp.evaluate(spend, ctrl, np.arange(fit.n_periods), rp.fit_cells())


# ------------------------------------------------------------------------ settings


def test_settings_accept_the_families_and_refuse_roi_priors() -> None:
    for fam in ("poisson", "negative_binomial", "beta"):
        s = BayesianSettings.model_validate({"likelihood": fam})
        assert not s.identity_link and s.media_prior == "roi"
    assert BayesianSettings.model_validate({"likelihood": "student_t"}).identity_link
    with pytest.raises(ValidationError, match="link scale"):
        BayesianSettings.model_validate(
            {"likelihood": "poisson", "priors": {"roi": {"tv": {"mu": 0, "sigma": 1}}}}
        )
    with pytest.raises(ValidationError, match="link scale"):
        BayesianSettings.model_validate(
            {"likelihood": "beta", "priors": {"roi_default": {"mu": 0, "sigma": 1}}}
        )
    with pytest.raises(ValidationError):
        BayesianSettings.model_validate({"likelihood": "binomial"})
    with pytest.raises(ValidationError):
        BayesianSettings.model_validate({"media_effect_sigma": 0})


def test_the_configs_use_coefficients_and_a_link_scale_intercept(national: xr.Dataset) -> None:
    stage = counts(national)
    s = BayesianSettings.model_validate({"likelihood": "poisson", "media_effect_sigma": 0.7})
    data = PanelData.from_dataset(stage, [], detect_controls(stage, None), with_target=True)
    mff, cfg, _ = build_configs(data, s, frequency="W")
    assert cfg.media_prior_mode == "coefficient"
    assert cfg.likelihood.family.value == "poisson"
    assert cfg.intercept_prior_mu == pytest.approx(np.log(float(stage.y.mean())))
    assert cfg.intercept_prior_sigma == 1.0
    for ch in mff.media_channels:
        assert "coefficient_prior" in ch.model_fields_set
        assert ch.coefficient_prior.params == {"sigma": 0.7}
        assert ch.roi_prior_mu is None


@pytest.mark.parametrize("family", ["normal", "student_t"])
def test_gaussian_graphs_are_unchanged(national: xr.Dataset, family: str) -> None:
    from mmm_framework.model import BayesianMMM

    s = BayesianSettings.model_validate({**BASE, "likelihood": family})
    data = PanelData.from_dataset(national, [], detect_controls(national, None), with_target=True)
    mff, cfg, trend = build_configs(data, s, frequency="W")
    draws = []
    for cls in (BayesianMMM, _platform_mmm_class()):
        with quiet():
            m = cls(data.to_panel(mff, "W"), cfg, trend)
            draws.append(m.get_prior(samples=10, random_seed=3)["prior"].to_dataset())
    assert type(make_model(data, s, frequency="W")) is _platform_mmm_class()
    xr.testing.assert_equal(draws[0], draws[1])


# ------------------------------------------------------------------------ replay


@pytest.mark.parametrize("family", ["poisson", "negative_binomial", "beta"])
def test_the_replay_reproduces_the_link_scale_graph(fits: dict[str, Any], family: str) -> None:
    pf = fits[family]
    rp, _, comps = _comps(pf)
    assert rp.link == LIKELIHOOD_LINK[family] and comps.family == family
    assert rp.y_std == 1.0 and rp.y_mean == 0.0
    np.testing.assert_allclose(comps.eta, _graph_eta(pf), rtol=1e-9, atol=1e-6)
    np.testing.assert_allclose(comps.mu, inverse_link(comps.eta, rp.link))
    if family != "poisson":
        assert comps.dispersion is not None and comps.dispersion.shape == (rp.n,)


@pytest.mark.parametrize("family", ["poisson", "negative_binomial", "beta"])
def test_kpi_parts_add_up_to_the_expected_kpi(fits: dict[str, Any], family: str) -> None:
    _, _, comps = _comps(fits[family])
    channel, terms, controls = comps.kpi_parts()
    total = channel.sum(axis=1) + terms["baseline"] + terms["trend"] + terms["seasonality"]
    total = total + controls.sum(axis=1)
    np.testing.assert_allclose(total, comps.mu, rtol=1e-9)
    assert not terms["trend"].any() and not controls.any()
    assert (channel >= -1e-12).all()


def test_channel_totals_are_removal_based_on_a_log_link(fits: dict[str, Any]) -> None:
    pf = fits["poisson"]
    rp, fit, comps = _comps(pf)
    spend, ctrl = history_arrays(fit)
    pos = np.arange(fit.n_periods)
    got = rp.channel_totals(spend, ctrl, pos, rp.fit_cells(), 1.5)
    scaled = rp.channel_at(spend * 1.5, pos, rp.fit_cells())
    rest = comps.eta - comps.channel[:, 0]
    want = (np.exp(rest + scaled[:, 0]) - np.exp(rest)).sum(axis=2)
    np.testing.assert_allclose(got[:, 0], want, rtol=1e-9)
    win = FitWindow(rp, fit)
    np.testing.assert_allclose(
        win.baseline_cells() + win.channel_totals(1.0).sum(axis=1), win.expected_cells()
    )


def test_steady_state_allocates_the_joint_increment(fits: dict[str, Any]) -> None:
    rp, _, _ = _comps(fits["negative_binomial"])
    level = np.array([[300.0, 150.0]])
    got = rp.steady_state(level, rp.fit_cells())
    rp.link = "identity"  # the link-scale terms themselves (y_std is 1)
    try:
        terms = rp.steady_state(level, rp.fit_cells())
    finally:
        rp.link = "log"
    base = rp._media_free_eta(rp.fit_cells())
    joint = np.exp(base + terms.sum(axis=2)) - np.exp(base)
    np.testing.assert_allclose(got.sum(axis=2), joint, rtol=1e-9)
    np.testing.assert_allclose(
        got / got.sum(axis=2, keepdims=True), terms / terms.sum(axis=2, keepdims=True)
    )


@pytest.mark.parametrize("family", ["poisson", "negative_binomial", "beta"])
def test_predictive_draws_follow_the_family(fits: dict[str, Any], family: str) -> None:
    _, _, comps = _comps(fits[family])
    a = comps.predictive(7)
    np.testing.assert_array_equal(a, comps.predictive(7))
    assert not np.array_equal(a, comps.predictive(8))
    assert np.isfinite(a).all()
    if family == "beta":
        assert ((a > 0) & (a < 1)).all()
    else:
        assert (a >= 0).all() and np.array_equal(a, np.round(a))


def test_identity_channel_totals_keep_the_old_formula(geo_prior_fit: Any) -> None:
    rp, fit = geo_prior_fit.plugin.replay(geo_prior_fit.fitted)
    spend, ctrl = history_arrays(fit)
    pos = np.arange(fit.n_periods)
    got = rp.channel_totals(spend, ctrl, pos, rp.fit_cells(), 0.7)
    old = rp.channel_at(spend * 0.7, pos, rp.fit_cells()).sum(axis=3) * rp.y_std
    np.testing.assert_array_equal(got, old)


# ------------------------------------------------------------------------ outputs


def _score(pf: Any, ds: xr.Dataset, **kw: Any) -> tuple[xr.Dataset, list[str]]:
    schema = DataSchema.model_validate(
        pf.plugin.manifest().schema_template.render(pf.fitted.settings)
    )
    notes: list[str] = []
    req = ScoreRequest.model_validate(
        {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": "endpoint://p/m",
         "mode": "predict"}
    )  # fmt: skip
    tree = pf.plugin.score(pf.fitted, ds, req, ScoreContext(schema=schema, log=notes.append, **kw))
    return tree[PREDICTIONS].to_dataset(), notes


def test_scores_have_channels_and_a_baseline(fits: dict[str, Any]) -> None:
    pf = fits["poisson"]
    pred, notes = _score(pf, pf.stage.drop_vars("y"))
    assert list(map(str, pred.component.values)) == ["tv", "search", "baseline"]
    total = pred.contributions.sum("component")
    xr.testing.assert_allclose(total, pred.y_hat.transpose(*total.dims))
    assert any("poisson likelihood" in n and "part of the baseline" in n for n in notes)
    draws, _ = _score(pf, pf.stage.drop_vars("y"), kind="predictive", seed=2)
    assert np.array_equal(draws.y_hat.values, np.round(draws.y_hat.values))


def test_contributions_dataset_has_channels_and_a_baseline(fits: dict[str, Any]) -> None:
    rp, fit, comps = _comps(fits["beta"])
    ds = contributions_dataset(FitWindow(rp, fit), None)
    assert set(ds.data_vars) == {"channel_contribution", "baseline"}
    total = ds["channel_contribution"].sum("channel") + ds["baseline"]
    np.testing.assert_allclose(total.values, comps.mu[:, 0], rtol=1e-9)


# ------------------------------------------------------------------------ checks


@pytest.mark.parametrize(
    ("family", "transform"),
    [
        ("poisson", lambda y: y + 0.5),
        ("negative_binomial", lambda y: -y),
        ("beta", lambda y: y),
    ],
)
def test_the_data_must_suit_the_family(
    national: xr.Dataset, family: str, transform: Callable[[Any], Any]
) -> None:
    stage = counts(national)
    stage = stage.assign(y=transform(stage["y"]))
    plugin = MMMBayesian()
    with pytest.raises(MapiError) as err:
        plugin.fit(stage, plugin.parse_settings({"likelihood": family}), FitConfig(), FitContext())
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.pointer == "/data"


def test_calibration_is_refused_with_a_link_scale_kpi(national: xr.Dataset) -> None:
    s = BayesianSettings.model_validate({"likelihood": "poisson"})
    check_calibration(BayesianSettings(), business_priors=True, lift_tests=True)
    with pytest.raises(MapiError) as err:
        check_calibration(s, lift_tests=True)
    assert err.value.code is ErrorCode.CALIBRATION_UNSUPPORTED
    assert err.value.pointer == "/calibration/lift_tests"
    plugin = MMMBayesian()
    beliefs = BusinessPriors.model_validate({"channels": {"tv": {"roi": [0.5, 1.5]}}})
    stage = counts(national)
    ctx = FitContext(calibration=Calibration(business_priors=beliefs))
    with pytest.raises(MapiError) as err:
        plugin.fit(stage, s, FitConfig(), ctx)
    assert err.value.code is ErrorCode.CALIBRATION_UNSUPPORTED
    assert err.value.pointer == "/calibration/business_priors"
    test = LiftTest(channel="tv", start=date(2024, 3, 3), end=date(2024, 3, 24),
                    spend_delta=400.0, kpi_delta=30.0, se=6.0)  # fmt: skip
    with pytest.raises(MapiError) as err:
        plugin.fit(stage, s, FitConfig(), FitContext(calibration=Calibration(lift_tests=[test])))
    assert err.value.pointer == "/calibration/lift_tests"
    job = PriorTranslationJob(spec={}, business_priors=beliefs, draws=100, seed=1)
    with pytest.raises(MapiError) as err:
        plugin.translate_priors(stage, s, job, FitContext())
    assert err.value.code is ErrorCode.CALIBRATION_UNSUPPORTED


# ------------------------------------------------------------------------ real fit


@pytest.mark.slow
def test_a_poisson_fit_scores_additive_contributions(national: xr.Dataset) -> None:
    stage = counts(national)
    plugin = MMMBayesian()
    s = plugin.parse_settings({**BASE, "likelihood": "poisson"})
    cfg = FitConfig(engine="pymc", chains=2, tune=30, draws=30, seed=1)
    out = plugin.fit(stage, s, cfg, FitContext(seed=1))
    assert out.learned["media_prior"] == "coefficient"
    fitted = Fitted(manifest=MMMBayesian.manifest(), settings=s.model_dump(mode="json"),
                    fit_config=cfg, groups=out.groups, learned=out.learned)  # fmt: skip

    class _PF:
        pass

    pf: Any = _PF()
    pf.plugin, pf.fitted = plugin, fitted
    pred, _ = _score(pf, stage.drop_vars("y"))
    total = pred.contributions.sum("component")
    xr.testing.assert_allclose(total, pred.y_hat.transpose(*total.dims))
    mean = pred.y_hat.mean(("chain", "draw"))
    assert float(abs(mean - stage.y).mean() / stage.y.mean()) < 0.5
