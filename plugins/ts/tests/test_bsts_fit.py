"""``ts.bsts`` with MCMC (nutpie, tiny settings): recovery, forecast, filter, tracking and
the contract kit (plan v0.13 §2.2)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_ts import BSTSModel
from mapi_plugin_ts.bsts import COMPONENTS, ONE_STEP, TERMINAL_STATE
from mapi_plugin_ts.bsts_data import BSTS_SCHEMA, BSTS_SPEC_YAML, BSTS_TRUTH, bsts_dataset
from mapi_pymc.engines import engine_available
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.models import FitContext
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_sdk.tracking import MetricRecorder
from mapi_spec import load_score_request
from mapi_spec.spec import FitConfig

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not engine_available("nutpie"), reason="nutpie is not installed"),
]

N_FIT = 60
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://ts/bsts\n"
SPEC = BSTS_SPEC_YAML.replace("mode: evaluate_then_refit", "mode: train_only").replace(
    "  split: {kind: time_holdout, dim: t, last: 12}\n", ""
)


def _req(body: str) -> Any:
    return load_score_request(HEADER + body)


@pytest.fixture(scope="module")
def full() -> xr.Dataset:
    return bsts_dataset(periods=N_FIT + 12, seed=3)


@pytest.fixture(scope="module")
def bundle(registry: Registry, full: xr.Dataset) -> Bundle:
    return fit_spec(
        SPEC, full.isel(date=slice(0, N_FIT)), registry=registry, raw_schema=BSTS_SCHEMA
    )


def test_fit_recovers_the_price_effect(bundle: Bundle) -> None:
    sc = bundle.model.learned["scaling"]
    assert isinstance(sc, dict)
    beta = bundle.model.posterior["beta_reg"].sel(state_reg="price")
    effect = beta * sc["y_scale"] / sc["x_scale"][0]
    lo, hi = (float(v) for v in effect.quantile([0.05, 0.95]))
    assert lo <= BSTS_TRUTH["price"] <= hi, (lo, hi)
    groups = bundle.model.groups
    assert {ONE_STEP, COMPONENTS, "observed_data", "constant_data", "sample_stats"} <= set(groups)
    assert "P0" not in bundle.model.posterior
    term = bundle.model.terminal_state
    assert term is not None and term.attrs["n_fit"] == N_FIT
    assert term.attrs["last_label"] == "2019-12-01"
    assert bundle.model.learned["missing_periods"] == 3
    assert "log_likelihood" not in groups


def test_forecast_twelve_periods_with_future_prices(
    bundle: Bundle, registry: Registry, full: xr.Dataset
) -> None:
    future = full.isel(date=slice(N_FIT, None)).drop_vars("sales")
    out = score_bundle(
        bundle,
        _req("mode: forecast\nhorizon: {dim: t, periods: 12}\noutput: {kind: predictive}\n"),
        future,
        registry=registry,
        return_full=True,
    )
    pred = out["predictions"].to_dataset()
    assert pred.sizes["date"] == 12 and pred["y_hat"].attrs["units"] == "units"
    mean = pred["y_hat"].mean(("chain", "draw")).values
    actual = full["sales"].isel(date=slice(N_FIT, None)).values
    assert np.nanmean(np.abs(mean - actual)) < 4 * BSTS_TRUTH["noise_sd"] + 40
    # a higher price lowers the forecast by the estimated effect
    dear = future.assign(price=future["price"] + 1.0)
    out2 = score_bundle(
        bundle,
        _req("mode: forecast\nhorizon: {dim: t, periods: 12}\n"),
        dear,
        registry=registry,
        return_full=True,
    )
    base = score_bundle(
        bundle,
        _req("mode: forecast\nhorizon: {dim: t, periods: 12}\n"),
        future,
        registry=registry,
        return_full=True,
    )
    diff = (out2["predictions"]["y_hat"] - base["predictions"]["y_hat"]).mean().item()
    assert diff < 0


def test_filter_returns_a_terminal_state_one_period_later(
    bundle: Bundle, registry: Registry, full: xr.Dataset
) -> None:
    out = score_bundle(
        bundle,
        _req("mode: filter\ndata: dataset://retail/monthly_sales_live@v1\n"),
        full.isel(date=slice(N_FIT, N_FIT + 1)),
        registry=registry,
        return_full=True,
    )
    term = out[TERMINAL_STATE].to_dataset()
    assert term.attrs["n_fit"] == N_FIT + 1
    assert term.attrs["last_label"] == "2020-01-01"
    assert term.attrs["previous_label"] == "2019-12-01"
    assert out["predictions"].to_dataset().sizes["date"] == 1


def test_fit_streams_chains_step_metrics_and_tracking_keys(full: xr.Dataset) -> None:
    plugin = BSTSModel()
    rec = MetricRecorder()
    events: list[tuple[float, str]] = []
    ctx = FitContext(seed=1, tracker=rec, on_progress=lambda f, m: events.append((f, m)))
    ds = full.isel(date=slice(0, 40)).rename({"date": "t", "sales": "y"})
    s = plugin.parse_settings({"trend": "level", "regressors": ["price"]})
    cfg = FitConfig(engine="nutpie", tune=60, draws=40, chains=2, seed=2)
    out = plugin.fit(ds, s, cfg, ctx)
    assert out.terminal_state is not None
    series = rec.series()
    assert series[("step_size", "chain=0")] and series[("divergences", "chain=1")]
    assert len(rec.last_chains) == 2
    assert {("sigma_obs", ""), ("sigma_level", ""), ("one_step_rmse", "")} <= set(series)
    assert rec.params["bsts.trend"] == "level" and rec.params["bsts.regressors"] == "price"
    assert rec.params["bsts.n_periods"] == 40 and rec.params["bsts.n_states"] == 1
    assert events[-1][0] == 1.0
    assert pd.Timestamp(out.learned["last_label"]) == pd.Timestamp(ds["t"].values[-1])  # type: ignore[arg-type]


@pytest.mark.mapi_contract
def test_bsts_passes_the_contract_kit(registry: Registry, full: xr.Dataset) -> None:
    spec = BSTS_SPEC_YAML.replace("tune: 100, draws: 100", "tune: 60, draws: 40")
    fixture = ModelFixture(
        spec=spec, data=full.isel(date=slice(0, 48)), raw_schema=BSTS_SCHEMA, registry=registry
    )
    report = check_model(BSTSModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
