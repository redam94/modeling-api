"""Guided forecasts and presets on ``mmm.hierarchical_geo``: future spend extended from the
fitted history (``future_covariates``) and the "budget x k" / "channel off" presets."""

from __future__ import annotations

import json

import pytest
import xarray as xr
from mapi_plugin_mmm.examples import E33Truth
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import score_bundle
from mapi_sdk.registry import Registry
from mapi_spec import ScoreRequest, load_score_request
from mapi_spec.spec import FutureCovariates, Horizon
from mapi_spec.whatif import BudgetScale, ChannelOff, counterfactual_request, forecast_request

pytestmark = pytest.mark.integration

MODEL = "model://growth/eu-geo-mmm@v1"


def _with_fx(req: ScoreRequest) -> ScoreRequest:
    body = req.model_dump(mode="json", by_alias=True, exclude_none=True)
    body["side_tables"] = {"fx": "dataset://growth/fx_daily@latest"}
    return load_score_request(json.dumps(body))


def _total(tree: xr.DataTree) -> float:
    node = tree["summary"]
    assert isinstance(node, xr.DataTree)
    return float(node.to_dataset()["y_hat"].sel(stat="mean").sum())


def test_forecasts_extend_future_spend_from_the_fitted_history(
    bundle: Bundle,
    registry: Registry,
    small: tuple[xr.Dataset, E33Truth],
    tables: dict[str, xr.Dataset],
) -> None:
    data = small[0]
    totals = {}
    for policy in (FutureCovariates.CARRY_LAST, FutureCovariates.ZERO):
        req = _with_fx(forecast_request(MODEL, periods=6, future=policy))
        tree = score_bundle(bundle, req, None, side_tables=tables, registry=registry, history=data)
        y = tree["summary"]
        assert isinstance(y, xr.DataTree)
        assert y.to_dataset()["y_hat"].sizes["date"] == 6
        issues = json.loads(str(tree.attrs["issues"]))
        assert any(policy.value in i["message"] for i in issues)
        totals[policy] = _total(tree)
    # no spend planned forecasts less revenue than carrying the last spend forward
    assert totals[FutureCovariates.ZERO] < totals[FutureCovariates.CARRY_LAST]
    scaled = _with_fx(
        counterfactual_request(
            BudgetScale(k=1.5),
            MODEL,
            spend_var="spend_local",
            horizon=Horizon(dim="time", periods=6),
            future=FutureCovariates.CARRY_LAST,
        )
    )
    assert scaled.mode == "forecast" and scaled.interventions[0].multiply == 1.5
    tree = score_bundle(bundle, scaled, None, side_tables=tables, registry=registry, history=data)
    assert _total(tree) > totals[FutureCovariates.CARRY_LAST]


def test_channel_off_over_the_fit_window_uses_common_random_numbers(
    bundle: Bundle,
    registry: Registry,
    small: tuple[xr.Dataset, E33Truth],
    tables: dict[str, xr.Dataset],
) -> None:
    req = _with_fx(counterfactual_request(ChannelOff(channel="tv"), MODEL, spend_var="spend_local"))
    body = req.model_dump(mode="json", by_alias=True, exclude_none=True)
    body["output"] = {"kind": "predictive", "draws": 50}
    req = load_score_request(json.dumps(body))
    tree = score_bundle(bundle, req, small[0], side_tables=tables, registry=registry)
    delta = tree["delta/draws"]
    assert isinstance(delta, xr.DataTree)
    d = delta.to_dataset()["y_hat"].sum(["market", "date"])
    # predictive draws, yet every draw loses revenue: scenario and baseline share their noise
    assert float(d.max()) < 0
