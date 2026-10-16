"""``mmm.hierarchical_geo`` through the SDK harness: fit, derived quantities, scoring
(forecast, new market, weekly EUR plan via inputs_stage, counterfactual), objective."""

# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_mmm.examples import E33Truth, future_daily
from mapi_plugin_mmm.hierarchical_geo import HierarchicalGeoMMM
from mapi_pymc.testing import check_fast_score
from mapi_sdk.binding import BindingMap
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import score_bundle
from mapi_sdk.models import ScoreContext
from mapi_sdk.registry import Registry
from mapi_spec import ScoreRequest
from mapi_spec.errors import ErrorCode, MapiError

pytestmark = pytest.mark.integration

MARKETS = ("NL", "DE", "UK")
CHANNELS = ("tv", "search")
WEEKS = 30
Req = Callable[[str], ScoreRequest]


def _future(weeks: int, *, markets: tuple[str, ...] = MARKETS) -> xr.Dataset:
    return future_daily(weeks=WEEKS, future_weeks=weeks, markets=markets, channels=CHANNELS, seed=1)


def _with_new_market(ds: xr.Dataset, name: str = "FR") -> xr.Dataset:
    new = ds.sel(market=["NL"]).assign_coords(market=[name]) * 1.1
    return xr.concat([ds, new], dim="market")


def _pred(tree: xr.DataTree, group: str = "predictions") -> xr.Dataset:
    node = tree[group]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


def test_fit_evaluates_refits_and_reports(bundle: Bundle) -> None:
    assert bundle.evaluation is not None
    assert {"coverage_90", "mape", "rmse"} <= set(bundle.metrics.values)
    assert bundle.metrics.output_space == "weekly"
    assert bundle.metrics.values["mape"] < 10.0  # percent, on weekly EUR revenue
    assert {d.code for d in bundle.diagnostics} >= {"RHAT", "ESS_BULK"}
    post = bundle.model.posterior
    # time-indexed deterministics are dropped from the stored posterior
    assert "mu" not in post and "contributions" not in post
    assert {"alpha", "lam", "beta", "beta_channel", "intercept"} <= set(post.data_vars)
    assert bundle.model.learned["controls"] == ["fourier", "holiday"]
    assert bundle.states["lags"].context is not None
    assert set(bundle.model.derived) == {"contributions", "response_curves", "marginal_cost"}
    assert bundle.evaluation.model.derived == {}


def test_derived_contributions_sum_to_the_expected_target(
    bundle: Bundle,
    registry: Registry,
    small: tuple[xr.Dataset, E33Truth],
    tables: dict[str, xr.Dataset],
    request_for: Req,
) -> None:
    data = small[0]
    out = score_bundle(
        bundle,
        request_for("mode: predict\noutput: {names: canonical}\n"),
        data,
        side_tables=tables,
        registry=registry,
        return_full=True,
    )
    y = _pred(out)["y_hat"]
    contrib = bundle.model.derived["contributions"]
    assert contrib["channel_contribution"].attrs["output_space"] == "weekly"
    assert contrib["channel_contribution"].attrs["units"] == "EUR"
    total = (
        contrib["channel_contribution"].sum("channel")
        + contrib["baseline"]
        + contrib["fourier"]
        + contrib["holiday"]
    )
    assert bundle.thin is not None
    pairs = bundle.thin[: contrib.sizes["sample"]]
    same = y.isel(
        chain=xr.DataArray(pairs[:, 0], dims="sample"),
        draw=xr.DataArray(pairs[:, 1], dims="sample"),
    ).drop_vars(["chain", "draw"])
    xr.testing.assert_allclose(
        total.transpose("sample", "geo", "time"),
        same.assign_coords(sample=total["sample"]).transpose("sample", "geo", "time"),
        rtol=1e-8,
    )


def test_response_curves_and_marginal_cost(
    bundle: Bundle, small: tuple[xr.Dataset, E33Truth]
) -> None:
    rc = bundle.model.derived["response_curves"]
    contrib = bundle.model.derived["contributions"]
    assert rc.sizes["sample"] == 50 and rc.sizes["multiplier"] == 21
    curve = rc["contribution"]
    assert float(abs(curve.sel(multiplier=0.0)).max()) == 0.0
    assert bool((curve.diff("multiplier") >= -1e-9).all())  # logistic: monotone
    at_one = curve.sel(multiplier=1.0, drop=True)
    xr.testing.assert_allclose(
        at_one.transpose("sample", "channel", "geo"),
        contrib["channel_contribution"].sum("time").transpose("sample", "channel", "geo"),
        rtol=1e-8,
    )
    # spend on the grid is in weekly EUR: current spend is the simulated EUR total
    truth = small[1].weekly["spend_eur"].sum("week").rename(market="geo")
    np.testing.assert_allclose(
        rc["current_spend"].sel(channel=list(CHANNELS)),
        truth.sum("geo").sel(channel=list(CHANNELS)),
    )
    np.testing.assert_allclose(rc["spend"].sel(multiplier=2.0), 2 * rc["current_spend"], rtol=1e-12)
    assert rc["spend"].attrs["output_space"] == "weekly"
    # M3: the baseline (intercepts + controls) closes the expected KPI over the window
    unexplained = (contrib["baseline"] + contrib["fourier"] + contrib["holiday"]).sum("time")
    np.testing.assert_allclose(
        rc["baseline_by_geo"].transpose("sample", "geo"),
        unexplained.transpose("sample", "geo"),
        rtol=1e-8,
    )
    np.testing.assert_allclose(rc["baseline"], rc["baseline_by_geo"].sum("geo"), rtol=1e-12)
    assert rc["baseline"].attrs["units"] == "EUR"
    mc = bundle.model.derived["marginal_cost"]
    cost = mc["marginal_cost"]
    assert bool((cost > 0).all()) and bool(np.isfinite(cost).all())
    np.testing.assert_allclose(mc["marginal_roi"] * cost, 1.0, rtol=1e-9)
    # central difference agrees with the slope of the cached grid at m = 1
    grid_slope = (curve.sel(multiplier=1.1) - curve.sel(multiplier=0.9)) / (
        0.2 * rc["spend_by_geo"].sel(multiplier=1.0)
    )
    np.testing.assert_allclose(1 / grid_slope.transpose(*cost.dims), cost, rtol=0.05)


def test_derive_without_the_platform_stays_in_model_units(
    bundle: Bundle, registry: Registry, small: tuple[xr.Dataset, E33Truth], tables: dict
) -> None:
    raw = BindingMap(bundle.resolved.binding).apply(small[0])
    model_ds = (
        bundle.pipeline(registry)
        .transform(raw, bundle.states, side_tables=tables, replay=False)
        .data
    )
    plugin = HierarchicalGeoMMM()
    ctx = ScoreContext(draw_index=bundle.thin[:5] if bundle.thin is not None else None)
    ds = plugin.derive(bundle.model, "response_curves", model_ds, ctx)
    assert ds["contribution"].attrs["output_space"] == "model"
    assert ds.sizes["sample"] == 5
    np.testing.assert_allclose(ds["target_scale"], 1.0)
    with pytest.raises(MapiError) as err:
        plugin.derive(bundle.model, "nope", model_ds, ctx)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_forecast_with_a_new_market_from_the_hyperprior(
    bundle: Bundle,
    registry: Registry,
    tables: dict[str, xr.Dataset],
    request_for: Req,
    tmp_path: Path,
) -> None:
    req = request_for(
        "mode: forecast\nhorizon: {dim: time, periods: 8}\noutput: {draws: 40, summary: [mean, hdi_90]}\n"
    )
    payload = _with_new_market(_future(8))
    out = score_bundle(
        bundle, req, payload, side_tables=tables, registry=registry, return_full=True
    )
    pred = _pred(out)["y_hat"]
    assert pred.sizes["date"] == 8 and "FR" in pred["market"].values
    assert bool(np.isfinite(pred).all())
    assert pred.attrs["units"] == "EUR" and pred.attrs["output_space"] == "weekly"
    issues = json.loads(out.attrs["issues"])
    assert any(i["code"] == "UNSEEN_LEVELS" for i in issues)
    # a new market's level is drawn from the hyperprior for every draw: wider than NL's,
    # relative to its level
    rel = pred.std(["chain", "draw"]) / pred.mean(["chain", "draw"])
    assert float(rel.sel(market="FR").mean()) > float(rel.sel(market="NL").mean())
    contrib = _pred(out)["contributions"]
    np.testing.assert_allclose(contrib.sum("component").transpose(*pred.dims), pred, rtol=1e-8)
    assert list(contrib["component"].values) == ["tv", "search", "baseline", "fourier", "holiday"]
    assert set(_pred(out, "summary")["y_hat"]["stat"].values) == {
        "mean",
        "hdi_90_lower",
        "hdi_90_upper",
    }
    # the bundle round trip scores identically
    bundle.write(tmp_path)
    back = Bundle.read(tmp_path, registry)
    again = score_bundle(
        back, req, payload, side_tables=tables, registry=registry, return_full=True
    )
    xr.testing.assert_allclose(_pred(again)["y_hat"], pred)
    xr.testing.assert_allclose(
        back.model.derived["response_curves"]["contribution"],
        bundle.model.derived["response_curves"]["contribution"],
    )


def test_weekly_eur_plan_via_inputs_stage_matches_the_raw_path(
    bundle: Bundle, registry: Registry, tables: dict[str, xr.Dataset], request_for: Req
) -> None:
    raw_req = request_for("mode: forecast\nhorizon: {dim: time, periods: 4}\n")
    raw_out = score_bundle(
        bundle, raw_req, _future(4), side_tables=tables, registry=registry, return_full=True
    )
    # the same plan, as weekly EUR spend entering after the resample step
    raw = BindingMap(bundle.resolved.binding).apply(_future(4), missing="ignore")
    weekly = (
        bundle.pipeline(registry)
        .transform(raw, bundle.states, end="weekly", side_tables=tables, replay=False)
        .data.drop_vars("y", errors="ignore")
    )
    plan = BindingMap(bundle.resolved.binding).reverse(weekly)
    plan_req = request_for(
        "mode: forecast\nhorizon: {dim: time, periods: 4}\ninputs_stage: weekly\n"
    )
    plan_out = score_bundle(bundle, plan_req, plan, registry=registry, return_full=True)
    xr.testing.assert_allclose(_pred(plan_out)["y_hat"], _pred(raw_out)["y_hat"], rtol=1e-8)
    # an 8-week plan that only exists in weekly EUR (no daily, no fx needed)
    weeks = pd.date_range(weekly["time"].values[-1], periods=5, freq="W-SUN")[1:]
    longer = xr.concat(
        [weekly, weekly.assign_coords(time=weeks.as_unit("ns").values) * 1.5], dim="time"
    )
    plan8 = request_for("mode: forecast\nhorizon: {dim: time, periods: 8}\ninputs_stage: weekly\n")
    out8 = score_bundle(
        bundle, plan8, BindingMap(bundle.resolved.binding).reverse(longer), registry=registry
    )
    assert _pred(out8, "summary")["y_hat"].sizes["date"] == 8


def test_counterfactual_moves_only_the_treated_channel(
    bundle: Bundle,
    registry: Registry,
    small: tuple[xr.Dataset, E33Truth],
    tables: dict[str, xr.Dataset],
    request_for: Req,
) -> None:
    window = small[0].isel(date=slice(-28, None))
    req = request_for(
        "mode: counterfactual\n"
        "interventions: [{var: spend_local, sel: {channel: tv}, multiply: 1.2}]\n"
        "output: {kind: predictive, draws: 30}\n"
    )
    out = score_bundle(bundle, req, window, side_tables=tables, registry=registry, return_full=True)
    delta = _pred(out, "delta/predictions")
    assert float(delta["y_hat"].mean()) > 0
    comp = delta["contributions"]
    assert float(comp.sel(component="tv").mean()) > 0
    others = comp.sel(component=["search", "baseline", "fourier", "holiday"])
    assert float(abs(others).max()) < 1e-6
    # predictive noise is shared (CRN): the delta of y equals the delta of its components
    np.testing.assert_allclose(
        delta["y_hat"], comp.sum("component").transpose(*delta["y_hat"].dims), atol=1e-6
    )


def test_objective_scores_budgets_per_draw(bundle: Bundle) -> None:
    plugin = HierarchicalGeoMMM()
    rc = bundle.model.derived["response_curves"]
    weekly = rc["current_spend"] / rc.attrs["periods"]  # average weekly EUR per channel
    base = xr.Dataset({"spend": weekly}, attrs={"periods": 4})
    more = xr.Dataset({"spend": weekly * 1.5}, attrs={"periods": 4})
    a = plugin.objective(bundle.model, base, ScoreContext())
    b = plugin.objective(bundle.model, more, ScoreContext())
    post = bundle.model.posterior
    assert a.dims == ("chain", "draw")
    assert a.shape == (post.sizes["chain"], post.sizes["draw"])
    assert bool((b >= a).all()) and float((b - a).mean()) > 0
    # original-unit budget == the same budget in model units, converted
    scale = rc["spend_scale"]
    by_geo = rc["spend_by_geo"].sel(multiplier=1.0) / rc.attrs["periods"]
    per_geo = xr.Dataset({"spend": by_geo}, attrs={"periods": 1})
    model_units = xr.Dataset({"spend": by_geo / scale}, attrs={"periods": 1, "units": "model"})
    orig = plugin.objective(bundle.model, per_geo, ScoreContext())
    one = {"geo": ["NL"]}
    nl_orig = plugin.objective(bundle.model, per_geo.sel(one), ScoreContext())
    nl_model = plugin.objective(bundle.model, model_units.sel(one), ScoreContext())
    # the model-unit objective is in model units of the target: NL's target scale converts
    np.testing.assert_allclose(nl_orig, nl_model * float(rc["target_scale"].sel(geo="NL")))
    assert "model units" in nl_model.attrs["units"]
    # a channel budget is split across geos by fit-window spend shares
    split = plugin.objective(
        bundle.model, xr.Dataset({"spend": by_geo.sum("geo")}, attrs={"periods": 1}), ScoreContext()
    )
    np.testing.assert_allclose(split, orig, rtol=1e-8)
    for bad, code in [
        (xr.Dataset({"budget": weekly}), ErrorCode.SCHEMA_INVALID),
        (xr.Dataset({"spend": weekly.rename(channel="ch")}), ErrorCode.SCHEMA_INVALID),
        (xr.Dataset({"spend": -weekly}), ErrorCode.SCHEMA_INVALID),
        (
            xr.Dataset({"spend": weekly.assign_coords(channel=["tv", "radio"])}),
            ErrorCode.UNSEEN_LEVELS,
        ),
        (xr.Dataset({"spend": weekly}, attrs={"units": "EUR"}), ErrorCode.SCHEMA_INVALID),
    ]:
        with pytest.raises(MapiError) as err:
            plugin.objective(bundle.model, bad, ScoreContext())
        assert err.value.code is code


def test_fast_score_matches_exact_scoring(
    bundle: Bundle, registry: Registry, small: tuple[xr.Dataset, E33Truth], tables: dict
) -> None:
    raw = BindingMap(bundle.resolved.binding).apply(small[0])
    model_ds = (
        bundle.pipeline(registry)
        .transform(raw, bundle.states, side_tables=tables, replay=False)
        .data
    )
    report = check_fast_score(HierarchicalGeoMMM(), bundle.model, model_ds, num_samples=100)
    assert report.ok, report.max_z
