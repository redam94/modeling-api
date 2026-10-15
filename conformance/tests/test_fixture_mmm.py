"""The E33 and pymc-marketing MMM fixtures: corpus layout and request semantics.

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
    assert {"e33", "pymc_marketing"} <= set(FIXTURES)


def test_e33_requests_cover_horizon_new_market_plan_and_counterfactual() -> None:
    fx = load_fixture("e33")
    cases = {c.name: c for c in fx.cases}
    assert list(cases) == ["forecast_8w", "new_market", "weekly_eur_plan", "counterfactual_tv"]
    stages = {c.score_request().inputs_stage for c in fx.cases}
    assert stages == {"raw", "weekly"}
    # a planner's weekly EUR budget needs no FX table: it enters after the conversion
    assert not cases["weekly_eur_plan"].side_tables
    assert all(c.side_tables for n, c in cases.items() if n != "weekly_eur_plan")
    assert cases["new_market"].expect["issues"] == ["UNSEEN_LEVELS"]
    assert cases["counterfactual_tv"].score_request().mode == "counterfactual"


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


def test_pymc_marketing_requests() -> None:
    fx = load_fixture("pymc_marketing")
    names = [c.name for c in fx.cases]
    assert names == ["forecast_4w", "recent_window", "full_series", "counterfactual_search"]
    data, _ = fx.generate(0, "tiny")
    schema = fx.raw_schema(data)
    # pymc-marketing cannot score panel levels unseen at fit
    assert schema.dims["market"].policy == "fixed"


@pytest.mark.parametrize("fixture_id", ["e33", "pymc_marketing"])
def test_expectation_hooks_take_the_runner_context(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    for name in ("check_fit", "check_score"):
        hook = fx.hook(name)
        assert callable(hook)
        assert "ctx" in inspect.signature(hook).parameters
