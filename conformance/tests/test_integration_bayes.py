"""E33-shaped end to end across packages: daily local-currency data through the real
``core.*`` steps (fx_convert → weekly resample → mean_scale → lag_tensor → fourier →
holidays) into a PyMC model (``mapi_pymc`` reference ``examples.panel``), fitted with
``evaluate_then_refit``; forecast with a new market drawn from the hyperprior, a CRN
counterfactual, and the standard plots rendered from the real bundle and score trees."""

# ruff: noqa: E501

from __future__ import annotations

import json

import mapi_steps
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plots.recipes import render_plots
from mapi_pymc.examples import PanelModel
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.schema import infer_schema
from mapi_spec import DataSchema, ModelSpec, load_model_spec, load_score_request, validate_spec

pytestmark = pytest.mark.integration

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: e33-pymc, project: growth}
data:
  dataset: dataset://growth/eu_media_daily@latest
  side_tables:
    fx:       {ref: dataset://growth/fx_daily@latest, score_policy: pinned}
    holidays: {ref: dataset://shared/holidays_eu@v3, score_policy: pinned}
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 4, after_step: weekly}
pipeline:
  - {id: to_eur,      use: core.fx_convert@^1, with: {vars: [y, spend], table: fx, table_along: date, currency_dim: geo, currencies: {UK: GBP}, default_currency: EUR}}
  - {id: weekly,      use: core.resample@^1,   with: {dim: time, freq: W-SUN, how: sum, full_periods_only: true}}
  - {id: scale_spend, use: core.mean_scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
  - {id: scale_y,     use: core.mean_scale@^1, with: {vars: [y], over: [time], unseen_levels: pooled}}
  - {id: lags,        use: core.lag_tensor@^1, with: {vars: [spend], along: time, within: [geo], l_max: 3, pad: first}}
  - {id: season,      use: core.fourier@^1,    with: {along: time, period: 52.18, order: 1}}
  - {id: hols,        use: core.holidays@^1,   with: {table: holidays, table_along: date, along: time}}
model:
  use: examples.panel@^1
  fit: {mode: evaluate_then_refit, seed: 7, tune: 100, draws: 60, chains: 2, engine: pymc}
outputs: {plots: [core.ppc_fan, core.convergence, core.posterior_forest]}
evaluate: {metrics: [rmse, mape, coverage_90]}
"""
WEEKS = 26
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/e33-pymc\n"


def _raw(days: int = 7 * WEEKS, seed: int = 0) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=days, freq="D").as_unit("ns")
    spend = rng.gamma(2.0, 30.0, size=(3, 2, days))
    spend[2] *= 0.85  # UK spends in GBP
    revenue = 200 + np.einsum("mcd,c->md", spend, [0.9, 1.4])
    revenue *= 1 + 0.03 * rng.standard_normal(revenue.shape)
    return xr.Dataset(
        {
            "revenue_local": (("market", "date"), revenue, {"units": "LCU"}),
            "spend_local": (("market", "channel", "date"), spend, {"units": "LCU"}),
        },
        coords={"market": ["NL", "DE", "UK"], "channel": ["tv", "search"], "date": dates},
    )


def _tables(days: int = 500) -> dict[str, xr.Dataset]:
    dates = pd.date_range("2023-12-01", periods=days, freq="D").as_unit("ns")
    t = np.arange(days)
    fx = xr.Dataset(
        {"rate": (("date", "currency"), (1.15 + 0.02 * np.sin(t / 20.0))[:, None])},
        coords={"date": dates, "currency": ["GBP"]},
    )
    flag = np.array([float((d.month, d.day) in {(1, 1), (5, 1), (12, 25)}) for d in dates])
    hols = xr.Dataset({"holiday": (("date",), flag)}, coords={"date": dates})
    return {"fx": fx, "holidays": hols}


def _future(weeks: int = 3) -> xr.Dataset:
    """Daily spend for the weeks after the fit range, plus a new market FR (no revenue)."""
    nxt = _raw(days=7 * (WEEKS + weeks)).isel(date=slice(7 * WEEKS, None))
    out = nxt.drop_vars("revenue_local")
    fr = out.sel(market=["NL"]).assign_coords(market=["FR"]) * 1.1
    return xr.concat([out, fr], dim="market")


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = mapi_steps.registry()
    reg.register_model(PanelModel)
    from mapi_plots.recipes import all_recipes

    reg.register(all_recipes())
    return reg


@pytest.fixture(scope="module")
def raw_schema() -> DataSchema:
    schema = infer_schema(_raw(), targets=["revenue_local"], policies={"market": "new_levels"})
    market = schema.dims["market"].model_copy(update={"new_level_strategy": "hyperprior"})
    return schema.model_copy(update={"dims": {**schema.dims, "market": market}})


@pytest.fixture(scope="module")
def spec() -> ModelSpec:
    return load_model_spec(SPEC)


@pytest.fixture(scope="module")
def bundle(spec: ModelSpec, registry: Registry, raw_schema: DataSchema) -> Bundle:
    return fit_spec(spec, _raw(), _tables(), registry, raw_schema=raw_schema)


def test_static_validation_against_steps_model_and_plots(
    spec: ModelSpec, registry: Registry, raw_schema: DataSchema
) -> None:
    res = validate_spec(spec, registry.catalog(), raw_schema)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]
    rs = res.raise_for_errors()
    assert rs.split_stage == "weekly"
    assert rs.model.resolved == "examples.panel@1.0.0"


def test_fit_evaluates_then_refits_with_diagnostics(bundle: Bundle) -> None:
    assert bundle.evaluation is not None
    assert {"rmse", "mape", "coverage_90"} <= set(bundle.metrics.values)
    assert bundle.metrics.output_space == "weekly"
    assert {d.code for d in bundle.diagnostics} >= {"RHAT", "ESS_BULK"}
    assert bundle.states["lags"].context is not None
    assert "posterior" in bundle.model.groups


def test_forecast_new_market_from_hyperprior_and_roundtrip(
    bundle: Bundle, registry: Registry, tmp_path: object
) -> None:
    request = load_score_request(
        REQ + "mode: forecast\nhorizon: {dim: time, periods: 3}\noutput: {draws: 20}\n"
    )
    out = score_bundle(bundle, request, _future(), registry=registry, return_full=True)
    pred = out["predictions"].to_dataset()["y_hat"]
    assert {"chain", "draw", "market", "date"} <= set(pred.dims)
    assert pred.sizes["date"] == 3 and "FR" in pred["market"].values
    assert bool(np.isfinite(pred).all())
    assert pred.attrs["units"] == "EUR" and pred.attrs["output_space"] == "weekly"
    # the new market is drawn from the hyperprior: wider than a fitted market
    sd = pred.std(["chain", "draw"]).mean("date")
    assert float(sd.sel(market="FR")) > float(sd.sel(market="NL"))
    assert any(i["code"] == "UNSEEN_LEVELS" for i in json.loads(out.attrs["issues"]))
    path = str(tmp_path)
    bundle.write(path)
    back = Bundle.read(path, registry)
    again = score_bundle(back, request, _future(), registry=registry, return_full=True)
    xr.testing.assert_allclose(again["predictions"].to_dataset()["y_hat"], pred)


def test_counterfactual_shares_noise_and_plots_render(bundle: Bundle, registry: Registry) -> None:
    window = _raw().isel(date=slice(-28, None))
    cf = score_bundle(
        bundle,
        load_score_request(
            REQ
            + "mode: counterfactual\n"
            + "interventions: [{var: spend_local, sel: {channel: tv}, multiply: 1.5}]\n"
            + "output: {kind: predictive, draws: 40}\n"
        ),
        window,
        registry=registry,
    )
    delta = cf["delta/summary"].to_dataset()["y_hat"].sel(stat="mean")
    assert float(delta.sum()) > 0  # more TV spend, more revenue
    report = render_plots(cf, ["core.counterfactual"], registry=registry)
    assert [p.id for p in report.plots] == ["core.counterfactual"], report.issues

    assert bundle.evaluation is not None
    ev = score_bundle(
        bundle.evaluation,
        load_score_request(
            REQ + "mode: evaluate\ndata: dataset://growth/holdout@v1\noutput: {draws: 40}\n"
        ),
        _raw().isel(date=slice(-28, None)),
        registry=registry,
        metrics=["rmse"],
        return_full=True,
    )
    plots = [str(p) for p in load_model_spec(SPEC).outputs.plots]
    fan = render_plots(ev, [p for p in plots if p == "core.ppc_fan"], registry=registry)
    assert [p.id for p in fan.plots] == ["core.ppc_fan"], fan.issues
    tree = bundle.to_tree()
    inference = render_plots(tree, ["core.convergence", "core.posterior_forest"], registry=registry)
    assert {p.id for p in inference.plots} == {"core.convergence", "core.posterior_forest"}, (
        inference.issues
    )
    assert all(p.provenance.bundle_id == bundle.bundle_id for p in inference.plots)
