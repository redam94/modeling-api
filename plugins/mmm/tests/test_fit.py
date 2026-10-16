"""Real mmm-framework fits through the SDK harness (tiny settings; ``slow``)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_mmm.bayesian import MMMBayesian
from mapi_plugin_mmm.examples import (
    E33_SPEC_YAML,
    NATIONAL_SPEC_YAML,
    e33_dataset,
    e33_raw_schema,
    e33_side_tables,
    national_dataset,
    national_raw_schema,
)
from mapi_plugin_mmm.frequentist import MMMFrequentist
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request

CORE_CHECKS = {
    "manifest",
    "about",
    "fit",
    "bundle_roundtrip_no_pickle",
    "shuffled_scoring",
    "new_level_policies",
    "extend_typed_errors",
    "missing_future_covariate",
    "unsupported_mode",
    "context_window_equivalence",
    "evaluate_path_equals_score_path",
    "output_space_and_units",
    "outputs_match_manifest",
    "representation_aggregation",
    "additive_contributions",
    "plots_sanitize",
}


ALL_CHECKS = (
    "manifest", "about", "fit", "bundle_roundtrip_no_pickle", "shuffled_scoring",
    "subset_scoring", "new_level_policies", "extend_typed_errors", "horizon_exceeds_fit",
    "missing_future_covariate", "recursive_unsupported", "unsupported_mode",
    "context_window_equivalence", "evaluate_path_equals_score_path", "output_space_and_units",
    "outputs_match_manifest", "representation_aggregation", "additive_contributions",
    "seed_determinism", "fast_score_matches_exact", "plots_sanitize",
)  # fmt: skip
NATIONAL_CHECKS = [c for c in ALL_CHECKS if c != "subset_scoring"]
"""A national model's only categorical dim is ``channel``, which its outputs do not carry:
scoring a subset of channels means the others have no spend, so the kit's per-level
equality does not apply."""


def _tiny(text: str) -> str:
    return text.replace("tune: 100, draws: 100", "tune: 40, draws: 40").replace(
        "tune: 100\n    draws: 100", "tune: 40\n    draws: 40"
    )


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_national_bayesian_passes_the_contract_kit(registry: Registry) -> None:
    ds, _ = national_dataset(weeks=60)
    fixture = ModelFixture(
        spec=_tiny(NATIONAL_SPEC_YAML), data=ds, raw_schema=national_raw_schema(), registry=registry
    )
    report = check_model(
        MMMBayesian, fixture, raise_on_failure=False, require_about=True, checks=NATIONAL_CHECKS
    )
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert ran >= CORE_CHECKS, sorted(CORE_CHECKS - ran)


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_geo_bayesian_passes_the_contract_kit(registry: Registry) -> None:
    data, _ = e33_dataset(markets=("NL", "DE", "UK"), channels=("tv", "search"), weeks=30, seed=1)
    text = (
        _tiny(E33_SPEC_YAML)
        .replace("last: 8", "last: 4")
        .replace("ui_draws: 100", "ui_draws: 40")
        .replace("score_policy: supplied", "score_policy: pinned")
        .replace("  derived: [contributions, response_curves, marginal_cost]\n", "")
    )
    fixture = ModelFixture(
        spec=text,
        data=data,
        side_tables=e33_side_tables(),
        raw_schema=e33_raw_schema(data),
        registry=registry,
        window=28,  # four whole W-SUN weeks of daily data
    )
    report = check_model(MMMBayesian, fixture, raise_on_failure=False, require_about=True)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert ran >= CORE_CHECKS | {"subset_scoring"}, sorted(CORE_CHECKS - ran)
    assert "market: new level scored" in report.get("new_level_policies").detail


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_frequentist_passes_the_contract_kit(registry: Registry) -> None:
    ds, _ = national_dataset(weeks=60)
    text = (
        NATIONAL_SPEC_YAML.replace("mmm.bayesian@^1", "mmm.frequentist@^1")
        .replace(
            "fit: {mode: evaluate_then_refit, engine: pymc, tune: 100, draws: 100, chains: 2, "
            "seed: 7}",
            "fit: {mode: evaluate_then_refit, seed: 7}",
        )
        .replace("    seasonality: {yearly_order: 1}\n",
                 "    seasonality: {yearly_order: 1}\n    bootstrap: 30\n    search_budget: 20\n")
    )  # fmt: skip
    fixture = ModelFixture(spec=text, data=ds, raw_schema=national_raw_schema(), registry=registry)
    report = check_model(
        MMMFrequentist, fixture, raise_on_failure=False, require_about=True, checks=NATIONAL_CHECKS
    )
    assert report.ok, report.summary()
    assert not report.get("seed_determinism").skipped


@pytest.fixture(scope="module")
def e33_fit(registry: Registry) -> tuple[Any, Any, xr.Dataset]:
    data, truth = e33_dataset(weeks=104, seed=0)
    spec = load_model_spec(
        _tiny(E33_SPEC_YAML).replace("mode: evaluate_then_refit", "mode: train_only")
    )
    bundle = fit_spec(spec, data, e33_side_tables(), registry, raw_schema=e33_raw_schema(data))
    return bundle, truth, data


@pytest.mark.slow
def test_e33_recovers_the_channel_contributions(e33_fit: tuple[Any, Any, xr.Dataset]) -> None:
    bundle, truth, _ = e33_fit
    contrib = bundle.model.derived["contributions"]["channel_contribution"]
    got = contrib.mean("sample").sum(("time", "geo"))
    want = truth.channel_contribution_eur().sum("market")
    share_got = got / got.sum()
    share_want = want / want.sum()
    np.testing.assert_allclose(share_got.values, share_want.values, atol=0.12)
    rc = bundle.model.derived["response_curves"]
    assert set(rc.contribution.dims) == {"sample", "channel", "multiplier", "geo"}
    assert bundle.model.learned["mmm_framework"]
    assert bundle.model.learned["panel"] == ["geo"]


@pytest.mark.slow
def test_e33_scores_forecasts_and_counterfactuals(
    e33_fit: tuple[Any, Any, xr.Dataset], registry: Registry
) -> None:
    bundle, _, data = e33_fit
    # the bundle is fitted on the training weeks: the 8 held-out weeks follow it directly
    future = data.isel(date=slice(-56, None)).drop_vars("revenue_local")
    req = load_score_request(
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/eu\nmode: forecast\n"
        "side_tables: {fx: dataset://growth/fx_daily@latest}\n"
        "horizon: {dim: time, periods: 8}\noutput: {kind: expected, summary: [mean, hdi_90]}\n"
    )
    tree = score_bundle(bundle, req, future, side_tables=e33_side_tables(), registry=registry)
    summary = tree["summary"].to_dataset()
    assert np.isfinite(summary["y_hat"].values).all()
    cf = load_score_request(
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/eu\n"
        "mode: counterfactual\n"
        "side_tables: {fx: dataset://growth/fx_daily@latest}\n"
        "interventions: [{var: spend_local, sel: {channel: tv}, multiply: 0.0}]\n"
        "output: {kind: expected, summary: [mean]}\n"
    )
    window = data.isel(date=slice(-84, -56))
    tree = score_bundle(bundle, cf, window, side_tables=e33_side_tables(), registry=registry)
    delta = tree["delta/summary"].to_dataset()["y_hat"].sel(stat="mean")
    assert float(delta.sum()) < 0


@pytest.mark.slow
def test_a_calibrated_fit_uses_business_priors_and_lift_tests(registry: Registry) -> None:
    ds, _ = national_dataset(weeks=60)
    text = _tiny(NATIONAL_SPEC_YAML).replace("mode: evaluate_then_refit", "mode: train_only") + (
        "calibration:\n"
        "  business_priors:\n"
        "    channels:\n"
        "      tv: {roi: [0.2, 0.6], credibility: 0.9}\n"
        "  lift_tests:\n"
        "    - {channel: search, start: 2023-06-04, end: 2023-07-02, spend_delta: 800.0,\n"
        "       kpi_delta: 250.0, se: 80.0}\n"
    )
    bundle = fit_spec(load_model_spec(text), ds, {}, registry, raw_schema=national_raw_schema())
    cal = bundle.model.learned["calibration"]
    assert set(cal["translated_priors"]["roi"]) == {"tv"}
    assert cal["lift_tests"]["used"] == ["lift_0"]
    roi = bundle.model.posterior["roi_tv"].values
    assert 0.1 < float(np.median(roi)) < 1.0
