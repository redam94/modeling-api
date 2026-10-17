"""The SDK contract kit on every statsmodels plugin (About cards required)."""

from __future__ import annotations

import pytest
from mapi_plugin_statsmodels import PLUGINS
from mapi_plugin_statsmodels.examples import examples
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model

BY_ID = {p.id: p for p in PLUGINS}
CASES = examples()


def test_every_plugin_has_a_case() -> None:
    assert set(CASES) == set(BY_ID)


@pytest.mark.parametrize("model_id", sorted(CASES))
def test_contract_kit(model_id: str, registry: Registry) -> None:
    case = CASES[model_id]
    report = check_model(
        BY_ID[model_id],
        ModelFixture(spec=case.spec, data=case.data, raw_schema=case.schema, registry=registry),
        require_about=True,
        raise_on_failure=False,
    )
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "manifest",
        "about",
        "fit",
        "bundle_roundtrip_no_pickle",
        "extend_typed_errors",
        "forecast_actuals",
        "context_window_equivalence",
        "evaluate_path_equals_score_path",
        "outputs_match_manifest",
        "representation_aggregation",
        "seed_determinism",
        "unsupported_mode",
    } <= ran, report.summary()
    if "exog" in case.spec:
        assert "missing_future_covariate" in ran, report.summary()
    actuals = next(r for r in report.results if r.name == "forecast_actuals")
    assert "reproduce the plain forecast" in actuals.detail, report.summary()
