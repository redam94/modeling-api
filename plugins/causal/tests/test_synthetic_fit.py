"""``causal.synthetic_control`` end to end with tiny MCMC: lift and donor recovery, tracking,
scoring and the contract kit."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_causal.datasets_panel import (
    GEO_LIFT_SCHEMA,
    GEO_LIFT_SPEC_YAML,
    GeoTruth,
    geo_lift_dataset,
)
from mapi_plugin_causal.synthetic import DONOR_WEIGHTS, SyntheticControlModel
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import FitHooks, fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_sdk.tracking import MetricRecorder

pytestmark = pytest.mark.slow

TINY_SPEC = GEO_LIFT_SPEC_YAML.replace(
    "tune: 500, draws: 500, chains: 4", "tune: 100, draws: 100, chains: 2"
)
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://p/m\n"


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(SyntheticControlModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def data() -> tuple[xr.Dataset, GeoTruth]:
    return geo_lift_dataset(seed=0)


@pytest.fixture(scope="module")
def fitted(registry: Registry, data: tuple[xr.Dataset, GeoTruth]) -> tuple[Bundle, MetricRecorder]:
    rec = MetricRecorder()
    bundle = fit_spec(
        TINY_SPEC,
        data[0],
        registry=registry,
        raw_schema=GEO_LIFT_SCHEMA,
        hooks=FitHooks(tracker=rec),
    )
    return bundle, rec


def test_recovers_the_lift_and_the_donor_mix(
    fitted: tuple[Bundle, MetricRecorder], data: tuple[xr.Dataset, GeoTruth]
) -> None:
    bundle, _ = fitted
    truth = data[1]
    imp = bundle.model.groups["impact"]
    total = imp["effect"].where(imp["post"]).sum(("unit", "time")).values
    lo, hi = np.quantile(total, [0.05, 0.95])
    assert lo <= truth.effect_total <= hi, (lo, truth.effect_total, hi)
    assert lo > 0
    # weights act on the normalised series: back on the scale of sales they are the true mix
    w = bundle.model.groups[DONOR_WEIGHTS]["weight"].mean("sample").sel(unit="geo_03")
    scales = bundle.model.learned["scales"]
    assert isinstance(scales, dict)
    raw = {
        str(d): float(v) * float(scales["geo_03"]) / float(scales[str(d)])  # type: ignore[arg-type]
        for d, v in zip(w["donor"].values, w.values, strict=True)
    }
    top = max(truth.weights["geo_03"], key=truth.weights["geo_03"].__getitem__)
    assert max(raw, key=raw.__getitem__) == top
    assert raw[top] == pytest.approx(truth.weights["geo_03"][top], abs=0.2)
    post = bundle.model.posterior
    assert set(post.data_vars) == {"weights", "alpha", "sigma"}
    assert {"impact", "donor_weights", "observed_data", "constant_data"} <= set(bundle.model.groups)
    learned = bundle.model.learned
    assert learned["treated"] == ["geo_03"] and learned["n_pre"] == 60
    assert learned["n_post"] == 12 and learned["intervention_end"] == truth.end


def test_tracking_keys(fitted: tuple[Bundle, MetricRecorder]) -> None:
    _, rec = fitted
    assert rec.params["causal.n_donors"] == 11
    assert rec.params["causal.weights"] == "simplex"
    assert rec.params["causal.intervention_start"] == "2024-02-26"
    keys = {k for k, _ in rec.series()}
    assert {"effect_total", "effect_total_p05", "effect_total_p95", "pre_rmse"} <= keys
    assert "donor_weight" in keys
    donors = [lk for k, lk in rec.series() if k == "donor_weight"]
    assert len(donors) == 10 and all("unit=geo_03" in lk for lk in donors)


def test_score_predict(
    fitted: tuple[Bundle, MetricRecorder], registry: Registry, data: tuple[xr.Dataset, GeoTruth]
) -> None:
    bundle, _ = fitted
    tree = score_bundle(bundle, REQ + "mode: predict\n", data[0], registry=registry)
    y_hat = tree["summary"].to_dataset()["y_hat"].sel(stat="mean")
    treated = y_hat.sel(geo="geo_03").values
    np.testing.assert_allclose(treated, data[1].counterfactual[0], rtol=0.06)
    assert np.isnan(y_hat.sel(geo="geo_00").values).all()
    pred = score_bundle(
        bundle, REQ + "mode: predict\noutput: {kind: predictive}\n", data[0], registry=registry
    )
    assert np.isfinite(pred["summary"].to_dataset()["y_hat"].sel(geo="geo_03")).all()


def test_horseshoe_student_t_fit_runs(registry: Registry) -> None:
    ds, truth = geo_lift_dataset(geos=8, weeks=50, start_week=40, seed=2)
    spec = (
        TINY_SPEC.replace('intervention_end: "2024-05-13"', "")
        .replace(
            "treated: [geo_03]",
            "treated: [geo_03]\n    weights: horseshoe\n    likelihood: student_t\n    "
            "normalize: none",
        )
        .replace("2024-02-26", truth.start)
    )
    bundle = fit_spec(spec, ds, registry=registry, raw_schema=GEO_LIFT_SCHEMA)
    assert {"weights", "tau", "nu"} <= set(bundle.model.posterior.data_vars)
    assert bundle.model.groups[DONOR_WEIGHTS].attrs["prior"] == "horseshoe"
    assert bundle.model.groups["impact"].attrs["intervention_end"] == ""


@pytest.mark.mapi_contract
def test_synthetic_control_passes_the_contract_kit(
    registry: Registry, data: tuple[xr.Dataset, GeoTruth]
) -> None:
    fixture = ModelFixture(
        spec=TINY_SPEC.replace("tune: 100, draws: 100", "tune: 60, draws: 60"),
        data=data[0],
        raw_schema=GEO_LIFT_SCHEMA,
        registry=registry,
    )
    report = check_model(SyntheticControlModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {"shuffled_scoring", "subset_scoring", "plots_sanitize"} <= ran


def test_donor_weight_labels_stay_unique(
    registry: Registry, data: tuple[xr.Dataset, GeoTruth]
) -> None:
    """Regression: donors 'geo 01' and 'geo_01' got one tracking label, so one weight
    silently replaced the other."""
    ds = data[0].isel(geo=slice(0, 6))
    labels = [str(g) for g in ds["geo"].values]
    labels[1] = labels[2].replace("_", " ")  # 'geo 02' next to 'geo_02'
    ds = ds.assign_coords(geo=labels)
    spec = TINY_SPEC.replace("tune: 100, draws: 100", "tune: 30, draws: 30")
    rec = MetricRecorder()
    fit_spec(spec, ds, registry=registry, raw_schema=GEO_LIFT_SCHEMA, hooks=FitHooks(tracker=rec))
    donors = {lk for k, lk in rec.series() if k == "donor_weight"}
    assert len(donors) == 5  # every donor keeps its own series
    assert any("donor=geo_02_2" in lk for lk in donors)
