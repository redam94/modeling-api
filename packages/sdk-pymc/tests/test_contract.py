"""The SDK contract kit (check_model) passes on the PyMC reference plugins."""

# ruff: noqa: E501

from __future__ import annotations

import pytest
from mapi_pymc.examples import (
    RADON_SCHEMA,
    RADON_SPEC_YAML,
    TREND_SCHEMA,
    TREND_SPEC_YAML,
    PanelModel,
    RadonModel,
    TrendModel,
    radon_dataset,
    trend_dataset,
)
from mapi_sdk.schema import infer_schema
from mapi_sdk.testing import ModelFixture, check_model, toy

E33_PYMC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: pymc-geo, project: growth}
data:
  dataset: dataset://growth/eu_media_daily@latest
  side_tables:
    fx: {ref: dataset://growth/fx_daily@latest, score_policy: pinned}
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 4, after_step: weekly}
pipeline:
  - {id: to_eur, use: toy.fx@^1, with: {vars: [y, spend], table: fx, table_along: date}}
  - {id: weekly, use: toy.resample@^1, with: {dim: time, freq: W-SUN}}
  - {id: scale_spend, use: toy.scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
  - {id: scale_y, use: toy.scale@^1, with: {vars: [y], over: [time], unseen_levels: pooled}}
  - {id: lags, use: toy.lag@^1, with: {vars: [spend], along: time, within: [geo], lags: [1, 2], fill: 0.0}}
  - {id: season, use: toy.fourier@^1, with: {along: time, period: 52.18, order: 2}}
model:
  use: examples.panel@^1
  fit: {mode: evaluate_then_refit, seed: 7, tune: 100, draws: 60, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mape, coverage_90]}
"""

CORE = {
    "manifest",
    "fit",
    "bundle_roundtrip_no_pickle",
    "evaluate_path_equals_score_path",
    "representation_aggregation",
    "seed_determinism",
}


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_panel_model_passes_the_contract_kit() -> None:
    data, fx = toy.panel_dataset(days=7 * 24)
    schema = infer_schema(data, targets=["revenue_local"], policies={"market": "new_levels"})
    market = schema.dims["market"].model_copy(update={"new_level_strategy": "hyperprior"})
    schema = schema.model_copy(update={"dims": {**schema.dims, "market": market}})
    reg = toy.toy_registry()
    fixture = ModelFixture(
        spec=E33_PYMC, data=data, side_tables={"fx": fx}, raw_schema=schema, registry=reg
    )
    report = check_model(PanelModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert (
        CORE
        | {
            "shuffled_scoring",
            "subset_scoring",
            "new_level_policies",
            "extend_typed_errors",
            "context_window_equivalence",
            "output_space_and_units",
        }
        <= ran
    )
    assert "hyperprior" in report.get("new_level_policies").detail


@pytest.mark.mapi_contract
def test_radon_model_passes_the_contract_kit() -> None:
    from mapi_sdk.registry import Registry

    reg = Registry()
    fixture = ModelFixture(
        spec=RADON_SPEC_YAML, data=radon_dataset(), raw_schema=RADON_SCHEMA, registry=reg
    )
    # long layout: the kit moves ``county_idx`` with the ``county`` labels it shuffles,
    # subsets the rows of one county and adds a new county as new rows
    report = check_model(RadonModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    assert {r.name for r in report.results if not r.skipped} >= {
        "fit",
        "bundle_roundtrip_no_pickle",
        "shuffled_scoring",
        "subset_scoring",
        "new_level_policies",
        "seed_determinism",
    }
    assert "long layout" in report.get("subset_scoring").detail
    # the radon example declares fast_score: the kit compares it with exact scoring
    fast = report.get("fast_score_matches_exact")
    assert fast.passed and not fast.skipped and "MCSE" in fast.detail, fast.detail


@pytest.mark.mapi_contract
def test_trend_model_passes_the_contract_kit() -> None:
    from mapi_sdk.registry import Registry

    fixture = ModelFixture(
        spec=TREND_SPEC_YAML, data=trend_dataset(), raw_schema=TREND_SCHEMA, registry=Registry()
    )
    report = check_model(TrendModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert CORE | {"context_window_equivalence", "extend_typed_errors", "unsupported_mode"} <= ran
