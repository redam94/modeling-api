"""``causal.did`` end to end with tiny MCMC: ATT recovery, the event study, counterfactual
scores, staggered adoption, tracking and the contract kit."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_causal.datasets_panel import DID_SCHEMA, DID_SPEC_YAML, DiDTruth, did_dataset
from mapi_plugin_causal.did import EVENT_STUDY, DiDModel
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import FitHooks, fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_sdk.tracking import MetricRecorder

pytestmark = pytest.mark.slow

TINY_SPEC = DID_SPEC_YAML.replace(
    "tune: 500, draws: 500, chains: 4", "tune: 100, draws: 100, chains: 2"
)
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://p/m\n"


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(DiDModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def data() -> tuple[xr.Dataset, DiDTruth]:
    return did_dataset(seed=0)


@pytest.fixture(scope="module")
def fitted(registry: Registry, data: tuple[xr.Dataset, DiDTruth]) -> tuple[Bundle, MetricRecorder]:
    rec = MetricRecorder()
    bundle = fit_spec(
        TINY_SPEC, data[0], registry=registry, raw_schema=DID_SCHEMA, hooks=FitHooks(tracker=rec)
    )
    return bundle, rec


def test_recovers_the_att_and_flat_pre_trends(
    fitted: tuple[Bundle, MetricRecorder], data: tuple[xr.Dataset, DiDTruth]
) -> None:
    bundle, _ = fitted
    study = bundle.model.groups[EVENT_STUDY]
    att = study["att"].values
    lo, hi = np.quantile(att, [0.05, 0.95])
    assert lo <= data[1].att <= hi, (lo, data[1].att, hi)
    assert study["event_time"].values.tolist() == [-4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6]
    for k in (-4, -3, -2):  # no effect before the adoption
        lo, hi = np.quantile(study["coef"].sel(event_time=k).values, [0.02, 0.98])
        assert lo <= 0.0 <= hi, (k, lo, hi)
    imp = bundle.model.groups["impact"]
    assert imp.sizes["unit"] == 12 and imp.attrs["intervention_start"] == "2024-03-01"
    total = imp["effect"].where(imp["post"]).sum(("unit", "time")).values
    assert np.quantile(total, 0.05) > 0
    assert bundle.model.learned["staggered"] is False
    assert not any(d.code == "STAGGERED_ADOPTION" for d in bundle.diagnostics)


def test_tracking_keys(fitted: tuple[Bundle, MetricRecorder]) -> None:
    _, rec = fitted
    assert rec.params["causal.design"] == "event_study"
    assert rec.params["causal.staggered"] is False
    assert (rec.params["causal.leads"], rec.params["causal.lags"]) == (4, 6)
    series = rec.series()
    keys = {k for k, _ in series}
    assert {"att", "att_p05", "att_p95", "event_coef", "effect_total", "pre_rmse"} <= keys
    labels = {lk for k, lk in series if k == "event_coef"}
    assert len(labels) == 11 and any("event_time=-1" in lk for lk in labels)


def test_counterfactual_score_removes_the_effect(
    fitted: tuple[Bundle, MetricRecorder], registry: Registry, data: tuple[xr.Dataset, DiDTruth]
) -> None:
    bundle, _ = fitted
    ds, truth = data
    pred = score_bundle(bundle, REQ + "mode: predict\n", ds, registry=registry)
    cf = score_bundle(
        bundle,
        REQ + "mode: counterfactual\ninterventions: [{var: treatment, set: 0}]\n",
        ds,
        registry=registry,
        return_full=True,
    )
    assert "delta" in cf.children
    base = pred["summary"].to_dataset()["y_hat"].sel(stat="mean")
    off = cf["summary"].to_dataset()["y_hat"].sel(stat="mean")
    treated = ds["treatment"] == 1
    gap = (base - off).where(treated).mean().item()
    assert gap == pytest.approx(truth.effect, abs=1.5)
    never = ds["treatment"].max("month") == 0
    np.testing.assert_allclose(base.sel(store=never).values, off.sel(store=never).values)


def test_staggered_adoption_warns(registry: Registry) -> None:
    ds, truth = did_dataset(units=16, periods=16, adoption="staggered", seed=1)
    spec = TINY_SPEC.replace("leads: 4, lags: 6", "leads: 2, lags: 3, design: did").replace(
        "design: event_study, ", ""
    )
    bundle = fit_spec(spec, ds, registry=registry, raw_schema=DID_SCHEMA)
    assert any(
        d.code == "STAGGERED_ADOPTION" and d.severity == "warning" for d in bundle.diagnostics
    )
    study = bundle.model.groups[EVENT_STUDY]
    assert study["event_time"].values.tolist() == [-1, 0] and study.attrs["design"] == "did"
    np.testing.assert_allclose(study["coef"].sel(event_time=0).values, study["att"].values)
    lo, hi = np.quantile(study["att"].values, [0.02, 0.98])
    assert lo <= truth.att <= hi


@pytest.mark.mapi_contract
def test_did_passes_the_contract_kit(registry: Registry) -> None:
    ds, _ = did_dataset(units=16, periods=16, seed=2)
    fixture = ModelFixture(
        spec=TINY_SPEC.replace("tune: 100, draws: 100", "tune: 60, draws: 60").replace(
            "leads: 4, lags: 6", "leads: 3, lags: 3"
        ),
        data=ds,
        raw_schema=DID_SCHEMA,
        registry=registry,
    )
    report = check_model(DiDModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {"shuffled_scoring", "subset_scoring", "plots_sanitize"} <= ran


@pytest.mark.parametrize("months", [slice(18, 24), slice(10, 16)], ids=["tail", "middle"])
def test_score_on_some_periods_matches_the_full_payload(
    fitted: tuple[Bundle, MetricRecorder],
    registry: Registry,
    data: tuple[xr.Dataset, DiDTruth],
    months: slice,
) -> None:
    """Regression: the padded periods of a partial payload used to move the adoption (wrong
    event dummies) or fail the monotone check on valid data."""
    bundle, _ = fitted
    ds, _ = data
    req = REQ + "mode: predict\noutput: {kind: expected}\n"
    full = score_bundle(bundle, req, ds, registry=registry)["summary"].to_dataset()
    part = score_bundle(bundle, req, ds.isel(month=months), registry=registry)
    got = part["summary"].to_dataset()["y_hat"].sel(stat="mean")
    want = full["y_hat"].sel(stat="mean").sel(month=got["month"])
    np.testing.assert_allclose(got.transpose(*want.dims).values, want.values, rtol=1e-6)
