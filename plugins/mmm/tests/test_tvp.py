"""Time-varying channel effects: settings, bundle layout, replay parity, forecast policies
and the ``effectiveness`` / ``adstock`` derived quantities (prior-draw fits; one ``slow``
real fit)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_mmm.bayesian import MMMBayesian
from mapi_plugin_mmm.calibration import (
    UnitScales,
    lift_measurements,
    lift_test_group,
    translate_business_priors,
    with_priors,
)
from mapi_plugin_mmm.derived import FitWindow, adstock_dataset, effectiveness_dataset
from mapi_plugin_mmm.forward import TVP_PERIOD, Replay, history_arrays
from mapi_plugin_mmm.framework import PanelData, detect_controls, make_model, quiet
from mapi_plugin_mmm.settings import BayesianSettings
from mapi_pymc.engines import engine_available
from mapi_sdk.models import PREDICTIONS, FitContext, Fitted, ScoreContext
from mapi_spec.calibration import BusinessPriors, LiftTest
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.schema import DataSchema
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import ValidationError

TVP = {
    "adstock": {"kind": "geometric", "l_max": 4},
    "trend": {"kind": "linear"},
    "time_varying": {"channels": ["tv"], "innovation_sigma": 0.3},
}


PriorFit = Any


@pytest.fixture(scope="module")
def national(stage_factory: Callable[..., xr.Dataset]) -> xr.Dataset:
    return stage_factory(geos=("all",)).isel(geo=0, drop=True)


@pytest.fixture(scope="module")
def tvp_fit(national: xr.Dataset, prior_fit_factory: Callable[..., Any]) -> PriorFit:
    return prior_fit_factory(national, TVP)


def _replay(pf: PriorFit, forecast: str = "random_walk") -> tuple[Replay, Any]:
    s = pf.plugin.parse_settings(
        {**pf.fitted.settings, "time_varying": {**TVP["time_varying"], "forecast": forecast}}
    )
    fitted = Fitted(
        manifest=pf.fitted.manifest,
        settings=s.model_dump(mode="json"),
        fit_config=pf.fitted.fit_config,
        groups=pf.fitted.groups,
        learned=pf.fitted.learned,
    )
    return pf.plugin.replay(fitted)


# ------------------------------------------------------------------------ settings


def test_settings_validate_the_time_varying_channels() -> None:
    s = BayesianSettings.model_validate({"time_varying": {"channels": ["tv"]}})
    assert s.time_varying.forecast == "random_walk" and s.time_varying.innovation_sigma == 0.15
    with pytest.raises(ValidationError):
        BayesianSettings.model_validate({"time_varying": {"channels": ["tv", "tv"]}})
    with pytest.raises(ValidationError):
        BayesianSettings.model_validate({"time_varying": {"forecast": "linear"}})
    with pytest.raises(ValidationError):
        BayesianSettings.model_validate({"time_varying": {"innovation_sigma": 0}})
    with pytest.raises(ValidationError, match="time-varying"):
        BayesianSettings.model_validate(
            {"time_varying": {"channels": ["tv"]}, "priors": {"roi": {"tv": {"mu": 0, "sigma": 1}}}}
        )


def test_unknown_time_varying_channels_are_refused(national: xr.Dataset) -> None:
    stage = national
    s = BayesianSettings.model_validate({"time_varying": {"channels": ["radio"]}})
    data = PanelData.from_dataset(stage, [], detect_controls(stage, None), with_target=True)
    with pytest.raises(MapiError) as err:
        make_model(data, s, frequency="W")
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    assert err.value.pointer == "/model/settings/time_varying/channels"


# ------------------------------------------------------------------------ bundle + replay


def test_the_bundle_keeps_the_coefficient_path_on_national_data(tvp_fit: PriorFit) -> None:
    post = tvp_fit.fitted.posterior
    assert post["beta_tv_tv"].dims == ("chain", "draw", TVP_PERIOD)
    assert post["beta_tv_tvpz"].sizes[TVP_PERIOD] == 52
    assert "beta_tv_tvpsigma" in post and "beta_search" in post
    rp, _ = tvp_fit.plugin.replay(tvp_fit.fitted)
    assert set(rp.tvp) == {0}


def test_the_replay_reproduces_the_graphs_channel_contributions(tvp_fit: PriorFit) -> None:
    rp, fit = tvp_fit.plugin.replay(tvp_fit.fitted)
    spend, ctrl = history_arrays(fit)
    comps = rp.evaluate(spend, ctrl, np.arange(fit.n_periods), rp.fit_cells())
    ref = np.asarray(tvp_fit.prior["channel_contributions"].values, dtype=float)
    ref = ref.reshape(rp.n, fit.n_periods, fit.n_cells, -1).transpose(0, 3, 2, 1)
    np.testing.assert_allclose(comps.channel, ref * tvp_fit.model.y_std, rtol=1e-9, atol=1e-6)
    # the time-average summary stays the graph's beta_tv
    np.testing.assert_allclose(rp.tvp[0].mean(axis=1), rp.beta[0], rtol=1e-9)


def test_forecast_policies(tvp_fit: PriorFit) -> None:
    beyond = np.arange(52, 72)
    hold, _ = _replay(tvp_fit, "hold_last")
    np.testing.assert_allclose(hold.beta_at(0, beyond), np.repeat(hold.tvp[0][:, -1:], 20, 1))
    mean, _ = _replay(tvp_fit, "mean")
    np.testing.assert_allclose(
        mean.beta_at(0, beyond), np.repeat(mean.tvp[0].mean(axis=1, keepdims=True), 20, 1)
    )
    walk, _ = _replay(tvp_fit, "random_walk")
    path = walk.beta_at(0, beyond, seed=3)
    np.testing.assert_allclose(path, walk.beta_at(0, beyond, seed=3))
    assert not np.allclose(path, walk.beta_at(0, beyond, seed=4))
    step = np.log(path) - np.log(walk.tvp[0][:, -1:])
    assert step[:, -1].std() > 2 * step[:, 0].std()
    # a longer horizon extends the same path; inside the window the fitted value
    np.testing.assert_allclose(walk.beta_at(0, beyond[:5], seed=3), path[:, :5])
    np.testing.assert_allclose(walk.beta_at(0, np.array([3, 40])), walk.tvp[0][:, [3, 40]])


def test_scenarios_share_the_forecast_path_and_say_so(tvp_fit: PriorFit) -> None:
    rp, fit = _replay(tvp_fit)
    spend, ctrl = history_arrays(fit)
    full = np.concatenate([spend, spend[-6:]])
    ctrl_full = np.concatenate([ctrl, ctrl[-6:]])
    pos = np.arange(52, 58)
    base = rp.evaluate(full, ctrl_full, pos, rp.fit_cells(), seed=9)
    other = full.copy()
    other[52:, :, 1] = 0.0  # no search spend in the scenario
    scen = rp.evaluate(other, ctrl_full, pos, rp.fit_cells(), seed=9)
    np.testing.assert_allclose(scen.channel[:, 0], base.channel[:, 0])
    assert any("the effect of tv after the fit window follows a random walk" in n
               for n in base.notes)  # fmt: skip


def test_steady_state_uses_the_coefficient_of_a_period(tvp_fit: PriorFit) -> None:
    rp, _ = tvp_fit.plugin.replay(tvp_fit.fitted)
    level = np.array([[300.0, 150.0]])
    first = rp.steady_state(level, rp.fit_cells(), position=0)
    last = rp.steady_state(level, rp.fit_cells())
    ratio = rp.tvp[0][:, 0] / rp.tvp[0][:, -1]
    np.testing.assert_allclose(first[:, 0, 0], last[:, 0, 0] * ratio, rtol=1e-9)
    np.testing.assert_allclose(first[:, 0, 1], last[:, 0, 1])


def test_effectiveness_varies_only_for_time_varying_channels(tvp_fit: PriorFit) -> None:
    rp, fit = tvp_fit.plugin.replay(tvp_fit.fitted)
    ds = effectiveness_dataset(FitWindow(rp, fit), None)
    assert ds["effectiveness"].dims == ("sample", "channel", "time")
    assert ds["time_varying"].values.tolist() == [True, False]
    eff = ds["effectiveness"].values
    assert (eff[:, 0].std(axis=1) > 0).all()
    np.testing.assert_allclose(eff[:, 1].std(axis=1), 0.0, atol=1e-12)
    assert np.isfinite(eff).all()


def test_adstock_weights_sum_to_one(tvp_fit: PriorFit) -> None:
    rp, fit = tvp_fit.plugin.replay(tvp_fit.fitted)
    ds = adstock_dataset(FitWindow(rp, fit))
    np.testing.assert_allclose(ds["weight"].sum("lag").values, 1.0)
    assert ds.attrs["frequency"] == "W" and ds.sizes["lag"] == 4


def test_the_derive_hook_serves_both(tvp_fit: PriorFit) -> None:
    ctx = ScoreContext(draw_index=np.array([[0, k] for k in range(5)]))
    for name in ("adstock", "effectiveness"):
        out = tvp_fit.plugin.derive(tvp_fit.fitted, name, tvp_fit.stage, ctx)
        assert out.sizes["sample"] == 5, name


# ------------------------------------------------------------------------ calibration


def test_business_priors_on_a_time_varying_channel_are_errors(national: xr.Dataset) -> None:
    stage = national
    s = BayesianSettings.model_validate(TVP)
    data = PanelData.from_dataset(stage, [], detect_controls(stage, None), with_target=True)
    out = translate_business_priors(
        BusinessPriors.model_validate({"channels": {"tv": {"roi": [0.5, 1.5]}}}),
        data,
        s,
        UnitScales(),
        model_factory=lambda st: make_model(data, st, frequency="W"),
    )
    (issue,) = out.issues
    assert issue.severity is Severity.ERROR and "varies over time" in issue.message
    assert "roi" not in out.priors
    with pytest.raises(MapiError) as err:
        with_priors(s, {"roi": {"tv": {"mu": 0.0, "sigma": 1.0}}})
    assert err.value.code is ErrorCode.BUSINESS_PRIORS_INVALID


def test_lift_tests_on_a_time_varying_channel_are_used(national: xr.Dataset) -> None:
    stage = national
    s = BayesianSettings.model_validate(TVP)
    data = PanelData.from_dataset(stage, [], detect_controls(stage, None), with_target=True)
    test = LiftTest(channel="tv", start=date(2024, 3, 3), end=date(2024, 3, 24),
                    spend_delta=400.0, kpi_delta=300.0, se=60.0)  # fmt: skip
    plan = lift_measurements([test], data, UnitScales())
    assert plan.rows == [
        {"id": "lift_0", "channel": "tv", "estimand": "mroas", "start": "2024-03-03",
         "end": "2024-03-24", "geo": ""}
    ]  # fmt: skip
    model = make_model(data, s, frequency="W")
    model.add_experiment_calibration(plan.measurements)
    with quiet():
        prior = model.get_prior(samples=20, random_seed=1)["prior"].to_dataset(inherit=False)
    group = lift_test_group(plan, prior)
    assert group is not None
    assert group["model"].dims == ("chain", "draw", "test")
    assert group["observed"].values.tolist() == [pytest.approx(0.75)]
    assert group["estimand"].values.tolist() == ["mroas"]


# ------------------------------------------------------------------------ real fit


@pytest.mark.slow
@pytest.mark.skipif(not engine_available("nutpie"), reason="nutpie is not installed")
def test_a_time_varying_fit_forecasts_four_weeks(national: xr.Dataset) -> None:
    stage = national
    plugin = MMMBayesian()
    s = plugin.parse_settings({**TVP, "adstock": {"l_max": 3}})
    cfg = FitConfig(engine="nutpie", chains=2, tune=30, draws=30, seed=1)
    out = plugin.fit(stage, s, cfg, FitContext(seed=1))
    assert out.groups["posterior"]["beta_tv_tv"].sizes[TVP_PERIOD] == 52
    fitted = Fitted(manifest=MMMBayesian.manifest(), settings=s.model_dump(mode="json"),
                    fit_config=cfg, groups=out.groups, learned=out.learned)  # fmt: skip
    future_t = pd.date_range(stage.time.values[-1], periods=5, freq="W-SUN")[1:].values
    future = stage.isel(time=slice(-4, None)).assign_coords(time=future_t).drop_vars("y")
    schema = DataSchema.model_validate(
        MMMBayesian.manifest().schema_template.render(fitted.settings)
    )
    notes: list[str] = []
    req = ScoreRequest.model_validate(
        {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": "endpoint://p/m",
         "mode": "forecast", "horizon": {"dim": "time", "periods": 4}}
    )  # fmt: skip
    tree = plugin.score(fitted, future, req, ScoreContext(schema=schema, log=notes.append))
    pred = tree[PREDICTIONS].to_dataset()
    assert pred.y_hat.sizes["time"] == 4 and np.isfinite(pred.y_hat.values).all()
    total = pred.contributions.sum("component")
    xr.testing.assert_allclose(total, pred.y_hat.transpose(*total.dims))
