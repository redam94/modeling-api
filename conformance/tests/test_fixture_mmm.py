"""The mmm-framework MMM fixtures (E33 on mmm.bayesian, mmm.frequentist): corpus layout and
request semantics.

Both fixtures run end to end (fit, every score request with the fixture's expectations,
evaluate, contract kit and plots) in ``test_corpus.py``; these fast tests pin what each
fixture asks of the platform.
"""

from __future__ import annotations

import inspect

import pytest
from mapi_conformance.fixtures import FIXTURES
from mapi_conformance.runner import load_fixture

pytestmark = [pytest.mark.conformance]


def test_fixtures_are_registered() -> None:
    assert {"e33", "mmm_frequentist", "guided_mmm"} <= set(FIXTURES)
    assert "pymc_marketing" not in FIXTURES


def test_e33_requests_cover_horizon_new_market_plan_and_counterfactual() -> None:
    fx = load_fixture("e33")
    cases = {c.name: c for c in fx.cases}
    assert list(cases) == [
        "forecast_8w",
        "new_market",
        "weekly_eur_plan",
        "counterfactual_tv",
        "counterfactual_tv_plan",
    ]
    stages = {c.score_request().inputs_stage for c in fx.cases}
    assert stages == {"raw", "weekly"}
    # a planner's weekly EUR budget needs no FX table: it enters after the conversion
    assert not cases["weekly_eur_plan"].side_tables
    assert all(c.side_tables for n, c in cases.items() if n != "weekly_eur_plan")
    assert cases["new_market"].expect["issues"] == ["UNSEEN_LEVELS"]
    assert cases["counterfactual_tv"].score_request().mode == "counterfactual"
    # the same uplift as a scenario dataset, scored against the baseline data
    plan = cases["counterfactual_tv_plan"]
    assert plan.scenario == "window_tv_plan" and plan.score_request().scenario is not None


def test_e33_payloads() -> None:
    fx = load_fixture("e33")
    data, tables = fx.generate(0, "tiny")
    assert set(tables) == {"fx", "holidays"}
    spec, schema = fx.spec("tiny"), fx.raw_schema(data)
    assert schema.dims["market"].policy == "new_levels"
    cases = {c.name: c for c in fx.cases}
    future = fx.payload(cases["forecast_8w"], data, spec, schema, 0, "tiny")
    assert future is not None and "revenue_local" not in future
    assert future["date"].values.min() > data["date"].values.max()
    new = fx.payload(cases["new_market"], data, spec, schema, 0, "tiny")
    assert new is not None and "FR" in new["market"].values
    plan = fx.payload(cases["weekly_eur_plan"], data, spec, schema, 0, "tiny")
    assert plan is not None and plan["spend_local"].attrs["units"] == "EUR"
    assert plan.sizes["date"] == 8


def test_mmm_frequentist_requests() -> None:
    fx = load_fixture("mmm_frequentist")
    names = [c.name for c in fx.cases]
    assert names == ["forecast_4w", "recent_window", "full_series", "counterfactual_search"]
    data, _ = fx.generate(0, "tiny")
    schema = fx.raw_schema(data)
    # the ridge estimator has no geo spread to draw an unseen market from
    assert schema.dims["market"].policy == "fixed"


@pytest.mark.parametrize("fixture_id", ["e33", "mmm_frequentist"])
def test_mmm_framework_owns_scaling_and_carryover(fixture_id: str) -> None:
    """``owns: [scaling, carryover]``: platform scaling/carry-over on the target or media
    is rejected statically instead of silently double-scaling (plan §4.3)."""
    from mapi_sdk.registry import default_registry
    from mapi_spec import load_model_spec, validate_spec
    from mapi_spec.errors import ErrorCode

    fx = load_fixture(fixture_id)
    data, _ = fx.generate(0, "tiny")
    schema = fx.raw_schema(data)
    catalog = default_registry().catalog()
    assert validate_spec(fx.spec("tiny"), catalog, schema).ok
    extra = {
        "scaling": "  - {id: scale_y, use: core.mean_scale@^1, with: {vars: [y], over: [time]}}\n",
        "carryover": "  - {id: ads, use: core.adstock@^1, with: {vars: [spend], along: time, "
        "alpha: 0.5, l_max: 3}}\n",
    }
    for what, step in extra.items():
        text = fx.spec_text.replace("\nmodel:\n", "\n" + step + "\nmodel:\n", 1)
        res = validate_spec(load_model_spec(text), catalog, schema)
        errs = [i for i in res.errors if i.code is ErrorCode.CAPABILITY_UNSUPPORTED]
        assert errs and f"owns {what}" in errs[0].message, res.issues


@pytest.mark.parametrize("fixture_id", ["e33", "mmm_frequentist", "guided_mmm"])
def test_expectation_hooks_take_the_runner_context(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    for name in ("check_fit", "check_score"):
        hook = fx.hook(name)
        assert callable(hook)
        assert "ctx" in inspect.signature(hook).parameters
