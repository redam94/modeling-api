"""The corpus' stack mode without a stack: how specs and score requests are rewritten to the
uploaded datasets and the registered model (the runs themselves are in ``tests/e2e``)."""

from __future__ import annotations

import pytest
from mapi_conformance.runner import load_fixture
from mapi_conformance.stack import StackRefs, dataset_name, stack_request, stack_spec_yaml
from mapi_spec import ScoreRequest, load_model_spec

pytestmark = pytest.mark.unit


def _refs() -> StackRefs:
    return StackRefs(
        data="dataset://default/corpus-e33-abc-data@v1",
        side_tables={
            "fx": "dataset://default/corpus-e33-abc-fx@v1",
            "holidays": "dataset://default/corpus-e33-abc-holidays@v1",
        },
        payloads={
            "future": "dataset://default/corpus-e33-abc-future@v1",
            "window": "dataset://default/corpus-e33-abc-window@v1",
            "window_tv_plan": "dataset://default/corpus-e33-abc-window-tv-plan@v1",
            "weekly_plan": "dataset://default/corpus-e33-abc-weekly-plan@v1",
        },
    )


def test_dataset_names_are_valid_and_unique() -> None:
    names = {dataset_name("corpus", "xgb_store_demand", "abc123", p) for p in ("data", "fx")}
    assert names == {"corpus-xgb-store-demand-abc123-data", "corpus-xgb-store-demand-abc123-fx"}


def test_spec_points_at_the_uploads_and_has_a_unique_name() -> None:
    fx = load_fixture("e33")
    text = stack_spec_yaml(fx, "tiny", _refs(), name="corpus-e33-abc")
    spec = load_model_spec(text)
    assert str(spec.data.dataset) == _refs().data
    assert {k: str(v.ref) for k, v in spec.data.side_tables.items()} == _refs().side_tables
    assert spec.metadata.name == "corpus-e33-abc" and spec.metadata.project == "default"
    assert spec.model.fit == fx.spec("tiny").model.fit  # the tiny overrides are applied


def test_score_requests_use_the_model_and_the_uploaded_payloads() -> None:
    fx = load_fixture("e33")
    spec = fx.spec("tiny")
    by_name = {c.name: c for c in fx.cases}
    model = "model://default/m_1@1"
    req = stack_request(by_name["counterfactual_tv_plan"], spec, _refs(), model)
    assert req["model"] == model
    assert req["data"] == _refs().payloads["window"]
    assert req["scenario"] == _refs().payloads["window_tv_plan"]
    assert req["side_tables"] == {"fx": _refs().side_tables["fx"]}
    ScoreRequest.model_validate(req)
    plan = stack_request(by_name["weekly_eur_plan"], spec, _refs(), model)
    assert plan["data"] == _refs().payloads["weekly_plan"]
    assert "side_tables" not in plan  # side_tables: false in the fixture
    ScoreRequest.model_validate(plan)
