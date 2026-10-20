"""``causal.impact`` end to end with tiny MCMC: lift recovery, scoring and the contract kit."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_causal.datasets import (
    IMPACT_SCHEMA,
    IMPACT_SPEC_YAML,
    ImpactTruth,
    impact_dataset,
)
from mapi_plugin_causal.impact import CausalImpactModel
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import FitHooks, fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_sdk.tracking import MetricRecorder

pytestmark = pytest.mark.slow

TINY_SPEC = IMPACT_SPEC_YAML.replace(
    "tune: 300, draws: 300, chains: 2", "tune: 100, draws: 100, chains: 2"
)


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(CausalImpactModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def data() -> tuple[xr.Dataset, ImpactTruth]:
    return impact_dataset(seed=0)


@pytest.fixture(scope="module")
def fitted(
    registry: Registry, data: tuple[xr.Dataset, ImpactTruth]
) -> tuple[Bundle, MetricRecorder]:
    rec = MetricRecorder()
    bundle = fit_spec(
        TINY_SPEC, data[0], registry=registry, raw_schema=IMPACT_SCHEMA, hooks=FitHooks(tracker=rec)
    )
    return bundle, rec


def test_recovers_the_lift(
    fitted: tuple[Bundle, MetricRecorder], data: tuple[xr.Dataset, ImpactTruth]
) -> None:
    bundle, rec = fitted
    truth = data[1]
    imp = bundle.model.groups["impact"]
    total = imp["effect"].where(imp["post"]).sum("time").values
    lo, hi = np.quantile(total, [0.05, 0.95])
    assert lo <= truth.effect_total <= hi, (lo, truth.effect_total, hi)
    assert lo > 0
    beta = bundle.model.posterior
    assert beta.sizes["chain"] == 2
    params = rec.params
    assert params["causal.n_pre"] == 80 and params["bsts.trend"] == "level"
    keys = {k for k, _ in rec.series()}
    assert {"effect_total", "effect_total_p05", "effect_total_p95", "pre_rmse"} <= keys


def test_score_predict_and_forecast(
    fitted: tuple[Bundle, MetricRecorder], registry: Registry, data: tuple[xr.Dataset, ImpactTruth]
) -> None:
    bundle, _ = fitted
    req = (
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://p/m\n"
        "mode: predict\noutput: {summary: [mean]}\n"
    )
    tree = score_bundle(bundle, req, data[0], registry=registry)
    summary = tree["summary"].to_dataset()
    effect = summary["effect"].sel(stat="mean").values
    assert np.nanmean(effect[80:]) > 5 * abs(np.nanmean(effect[:80]))
    longer, _ = impact_dataset(weeks=108, seed=0)
    future = longer.isel(week=slice(104, None)).drop_vars("sales")
    req = (
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://p/m\n"
        "mode: forecast\nhorizon: {dim: time, periods: 4}\noutput: {summary: [mean]}\n"
    )
    tree = score_bundle(bundle, req, future, registry=registry)
    assert tree["summary"].to_dataset().sizes["week"] == 4


@pytest.mark.mapi_contract
def test_causal_impact_passes_the_contract_kit(
    registry: Registry, data: tuple[xr.Dataset, ImpactTruth]
) -> None:
    fixture = ModelFixture(
        spec=TINY_SPEC, data=data[0], raw_schema=IMPACT_SCHEMA, registry=registry
    )
    report = check_model(CausalImpactModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()


def test_score_effect_is_a_difference_in_target_units(
    registry: Registry, data: tuple[xr.Dataset, ImpactTruth]
) -> None:
    """Regression: the scored effect ``y - y_hat`` went through the target's inverse as if it
    were a level (with ``core.log1p`` the result was about ``y / y_hat``, not a difference)."""
    ds = data[0].isel(week=slice(0, 96))
    spec = TINY_SPEC.replace(
        "model:\n",
        "pipeline:\n- {id: t, use: core.log1p@^1, with: {vars: [y]}}\nmodel:\n",
        1,
    ).replace("tune: 100, draws: 100", "tune: 40, draws: 40")
    bundle = fit_spec(spec, ds, registry=registry, raw_schema=IMPACT_SCHEMA)
    req = (
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://p/m\n"
        "mode: predict\noutput: {units: original, kind: predictive}\n"
    )
    tree = score_bundle(bundle, req, ds, registry=registry, return_full=True)
    pred = tree["predictions"].to_dataset()
    want = ds["sales"] - pred["y_hat"]
    np.testing.assert_allclose(pred["effect"].values, want.transpose(*pred["effect"].dims).values)
    assert "effect__ref" not in pred
    # the score agrees with the fit's impact group (the same counterfactual draws in the
    # pre-period: one-step-ahead with the fit seed)
    imp = bundle.model.groups["impact"]
    pre = pred["effect"].isel(week=slice(0, 80)).mean(("chain", "draw")).values
    np.testing.assert_allclose(
        pre, imp["effect"].isel(time=slice(0, 80)).mean("sample").values, rtol=0.2, atol=5.0
    )
