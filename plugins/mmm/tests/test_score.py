"""Scoring, derived quantities and the decision objective (prior-draw fits, no MCMC)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_mmm.framework import BASELINE, SEASONALITY, TREND
from mapi_plugin_mmm.settings import GridSettings
from mapi_sdk.models import PREDICTIONS, ScoreContext
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ScoreRequest


def _req() -> ScoreRequest:
    return ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": "endpoint://p/m",
            "mode": "predict",
        }
    )


def _ctx(pf: Any, **kw: Any) -> ScoreContext:
    schema = DataSchema.model_validate(
        pf.plugin.manifest().schema_template.render(pf.fitted.settings)
    )
    return ScoreContext(schema=schema, **kw)


def _score(pf: Any, ds: xr.Dataset, **kw: Any) -> xr.Dataset:
    tree = pf.plugin.score(pf.fitted, ds, _req(), _ctx(pf, **kw))
    out = tree[PREDICTIONS]
    assert isinstance(out, xr.DataTree)
    return out.to_dataset()


def test_in_window_scores_reproduce_the_fit(geo_prior_fit: Any) -> None:
    pf = geo_prior_fit
    pred = _score(pf, pf.stage.drop_vars("y"))
    assert set(pred.y_hat.dims) == {"chain", "draw", "geo", "time"}
    assert pred.y_hat.attrs["kind"] == "expected"
    total = pred.contributions.sum("component")
    xr.testing.assert_allclose(total, pred.y_hat.transpose(*total.dims))
    comps = list(map(str, pred.component.values))
    assert comps == ["tv", "search", BASELINE, TREND, SEASONALITY, "price", "season"]


def test_forecasts_need_future_spend_and_extend_contiguously(geo_prior_fit: Any) -> None:
    pf = geo_prior_fit
    stage = pf.stage
    future_t = pd.date_range(stage.time.values[-1], periods=5, freq="W-SUN")[1:].values
    future = stage.isel(time=slice(-4, None)).assign_coords(time=future_t).drop_vars("y")
    pred = _score(pf, future)
    assert pred.y_hat.sizes["time"] == 4 and np.isfinite(pred.y_hat.values).all()
    with pytest.raises(MapiError) as err:
        _score(pf, future.drop_vars("spend"))
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE
    gap = future.assign_coords(time=future_t + np.timedelta64(21, "D"))
    with pytest.raises(MapiError) as err:
        _score(pf, gap)
    assert err.value.code is ErrorCode.NON_CONTIGUOUS_EXTENSION


def test_predictive_scores_add_seeded_noise(geo_prior_fit: Any) -> None:
    pf = geo_prior_fit
    ds = pf.stage.drop_vars("y").isel(time=slice(-6, None))
    a = _score(pf, ds, kind="predictive", seed=1)
    b = _score(pf, ds, kind="predictive", seed=1)
    e = _score(pf, ds)
    xr.testing.assert_identical(a.y_hat, b.y_hat)
    assert not np.allclose(a.y_hat.values, e.y_hat.values)
    assert a.y_hat.attrs["noise"] == "crn_seeded"


def test_counterfactual_scenarios_move_only_the_channel(geo_prior_fit: Any) -> None:
    pf = geo_prior_fit
    ds = pf.stage.drop_vars("y").isel(time=slice(-10, None))
    base = _score(pf, ds)
    more = ds.copy()
    more["spend"] = ds.spend * xr.where(ds.channel == "tv", 1.5, 1.0)
    scen = _score(pf, more)
    diff = (scen.contributions - base.contributions).sel(component=["search", BASELINE, "price"])
    assert np.abs(diff.values).max() < 1e-6
    assert (
        scen.contributions.sel(component="tv") >= base.contributions.sel(component="tv") - 1e-9
    ).all()


def test_shuffled_time_order_is_restored(geo_prior_fit: Any) -> None:
    pf = geo_prior_fit
    ds = pf.stage.drop_vars("y").isel(time=slice(-6, None))
    a = _score(pf, ds)
    b = _score(pf, ds.isel(time=[5, 3, 1, 0, 2, 4]))
    xr.testing.assert_allclose(b.y_hat.sel(time=a.time), a.y_hat)


def test_new_geos_need_a_strategy(geo_prior_fit: Any) -> None:
    pf = geo_prior_fit
    ds = pf.stage.drop_vars("y").isel(time=slice(-4, None))
    new = ds.isel(geo=[0]).assign_coords(geo=["east"])
    pred = _score(pf, new)
    assert np.isfinite(pred.y_hat.values).all()
    schema = DataSchema.model_validate(
        pf.plugin.manifest().schema_template.render(pf.fitted.settings)
    )
    strict = schema.model_copy(
        update={
            "dims": {
                **schema.dims,
                "geo": schema.dims["geo"].model_copy(update={"new_level_strategy": "error"}),
            }
        }
    )
    with pytest.raises(MapiError) as err:
        pf.plugin.score(pf.fitted, new, _req(), ScoreContext(schema=strict))
    assert err.value.code is ErrorCode.UNSEEN_LEVELS


def test_derived_contributions_sum_to_the_expected_kpi(geo_prior_fit: Any) -> None:
    pf = geo_prior_fit
    ctx = ScoreContext(draw_index=np.array([[0, i] for i in range(10)]))
    ds = pf.plugin.derive(pf.fitted, "contributions", pf.stage, ctx)
    assert ds.channel_contribution.dims == ("sample", "channel", "geo", "time")
    total = ds.channel_contribution.sum("channel") + ds.baseline + ds.trend + ds.seasonality
    total = total + ds.price + ds.season
    pred = _score(pf, pf.stage.drop_vars("y"))
    want = pred.y_hat.isel(chain=0, draw=slice(0, 10)).rename(draw="sample")
    np.testing.assert_allclose(
        total.transpose("sample", "geo", "time").values,
        want.transpose("sample", "geo", "time").values,
        rtol=1e-9,
    )


def test_response_curves_hold_today_and_the_baseline(geo_prior_fit: Any) -> None:
    pf = geo_prior_fit
    ctx = ScoreContext(draw_index=np.array([[0, i] for i in range(8)]))
    rc = pf.plugin.derive(pf.fitted, "response_curves", pf.stage, ctx)
    contrib = pf.plugin.derive(pf.fitted, "contributions", pf.stage, ctx)
    assert set(rc.contribution.dims) == {"sample", "channel", "multiplier", "geo"}
    assert 1.0 in rc.multiplier.values and rc.multiplier.size == GridSettings().points
    at_one = rc.contribution.sel(multiplier=1.0)
    np.testing.assert_allclose(
        at_one.transpose("sample", "channel", "geo").values,
        contrib.channel_contribution.sum("time").transpose("sample", "channel", "geo").values,
    )
    # more spend never lowers a channel's contribution (saturation curves are monotone)
    assert (rc.contribution.diff("multiplier") >= -1e-9).all()
    spend = pf.stage.spend.sum(("geo", "time"))
    np.testing.assert_allclose(rc.current_spend.values, spend.values)
    np.testing.assert_allclose(rc.spend.sel(multiplier=2.0).values, 2 * spend.values)
    expected = (rc.baseline + at_one.sum(("channel", "geo"))).values
    total = contrib.channel_contribution.sum(("channel", "geo", "time")) + (
        contrib.baseline + contrib.trend + contrib.seasonality + contrib.price + contrib.season
    ).sum(("geo", "time"))
    np.testing.assert_allclose(expected, total.values, rtol=1e-9)
    assert rc.attrs["periods"] == 52
    assert float(rc.max_period_spend.sel(channel="tv")) == pytest.approx(
        float(pf.stage.spend.sel(channel="tv").max())
    )


def test_marginal_cost(geo_prior_fit: Any) -> None:
    pf = geo_prior_fit
    ctx = ScoreContext(draw_index=np.array([[0, i] for i in range(5)]))
    mc = pf.plugin.derive(pf.fitted, "marginal_cost", pf.stage, ctx)
    roi = mc.marginal_roi.values
    cost = mc.marginal_cost.values
    ok = np.isfinite(roi) & np.isfinite(cost)
    np.testing.assert_allclose(roi[ok] * cost[ok], 1.0, rtol=1e-9)
    assert mc.marginal_roi_total.dims == ("sample", "channel")
    with pytest.raises(MapiError):
        pf.plugin.derive(pf.fitted, "nope", pf.stage, ctx)


def test_the_objective_values_a_budget_per_draw(geo_prior_fit: Any) -> None:
    pf = geo_prior_fit
    spend = xr.Dataset({"spend": ("channel", [200.0, 100.0])}, coords={"channel": ["tv", "search"]})
    spend.attrs["periods"] = 4
    obj = pf.plugin.objective(pf.fitted, spend, ScoreContext())
    assert obj.dims == ("chain", "draw") and (obj.values >= 0).all()
    more = spend.copy()
    more["spend"] = spend.spend * 2
    more.attrs["periods"] = 4
    assert (pf.plugin.objective(pf.fitted, more, ScoreContext()).values >= obj.values - 1e-9).all()
    by_geo = xr.Dataset(
        {"spend": (("geo", "channel"), [[100.0, 50.0]] * 3)},
        coords={"geo": ["north", "south", "west"], "channel": ["tv", "search"]},
    )
    assert np.isfinite(pf.plugin.objective(pf.fitted, by_geo, ScoreContext()).values).all()
    bad = xr.Dataset({"spend": ("channel", [1.0])}, coords={"channel": ["radio"]})
    with pytest.raises(MapiError) as err:
        pf.plugin.objective(pf.fitted, bad, ScoreContext())
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
