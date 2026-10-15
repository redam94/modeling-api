"""The corpus runner itself: fixture loading, score-case parsing, reports and the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import xarray as xr
from mapi_conformance.__main__ import main
from mapi_conformance.fixtures import FIXTURES
from mapi_conformance.runner import (
    CorpusReport,
    FixtureReport,
    ScoreCase,
    StepResult,
    load_fixture,
    run_corpus,
    run_fixture,
)
from mapi_spec import ScoreRequest


@pytest.mark.parametrize("fixture_id", FIXTURES)
def test_every_fixture_loads(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    data, tables = fx.generate(0, "tiny")
    assert isinstance(data, xr.Dataset) and data.sizes
    assert all(isinstance(t, xr.Dataset) for t in tables.values())
    spec = fx.spec("tiny")
    assert spec.kind == "ModelSpec"
    schema = fx.raw_schema(data)
    assert set(schema.dims) <= set(data.dims)
    assert fx.cases, "every fixture should exercise score requests"
    for case in fx.cases:
        assert isinstance(case.score_request(), ScoreRequest)
        payload = fx.payload(case, data, spec, schema, 0, "tiny")
        if payload is not None and case.drop_targets:
            assert not set(schema.targets) & set(payload.data_vars)


def test_tiny_overrides_shrink_the_fit() -> None:
    fx = load_fixture("c08")
    full, tiny = fx.spec("full"), fx.spec("tiny")
    assert full.model.fit.draws == 600 and tiny.model.fit.draws == 100
    assert tiny.model.fit.target_accept is None
    xgb = load_fixture("xgb_store_demand")
    est = xgb.spec("tiny").model.plugin_settings["estimator"]
    assert est["params"]["n_estimators"] == 80 and est["kind"] == "xgboost.XGBRegressor"
    assert xgb.spec("full").model.plugin_settings["estimator"]["params"]["n_estimators"] == 600


def test_score_case_parsing() -> None:
    case = ScoreCase.parse({"name": "f", "request": {"mode": "predict"}}, "x_y")
    assert case.request["kind"] == "ScoreRequest"
    assert case.request["model"] == "endpoint://conformance/x-y"
    assert case.payload == "none" and case.drop_targets
    with pytest.raises(ValueError, match="unknown keys"):
        ScoreCase.parse({"name": "f", "request": {}, "oops": 1}, "x")
    with pytest.raises(ValueError, match="need"):
        ScoreCase.parse({"request": {}}, "x")
    with pytest.raises(ValueError):
        ScoreCase.parse({"name": "f", "request": {}, "expect_error": "NOT_A_CODE"}, "x")


def test_unknown_payload_builder_is_reported() -> None:
    fx = load_fixture("c08")
    data, _ = fx.generate(0, "tiny")
    case = ScoreCase.parse({"name": "f", "request": {}, "payload": "nope"}, "c08")
    with pytest.raises(ValueError, match="payload_nope"):
        fx.payload(case, data, fx.spec("tiny"), fx.raw_schema(data), 0, "tiny")


def test_report_table_and_json() -> None:
    ok = FixtureReport("a", "tiny", model="m@1", seconds=1.0)
    ok.steps = [StepResult("validate", True), StepResult("score", True, passed=2, total=2)]
    ok.metrics = {"rmse": 0.5}
    bad = FixtureReport("b", "tiny", seconds=2.0)
    bad.steps = [StepResult("fit", False, detail="boom"), StepResult("score", True, skipped=True)]
    report = CorpusReport([ok, bad])
    table = report.table()
    lines = table.splitlines()
    assert lines[0].split()[:3] == ["fixture", "model", "validate"]
    assert "ok 2/2" in table and "FAIL" in lines[3] and "skip" in lines[3]
    assert "rmse=0.5" in table and "1/2 fixtures passed" in table
    assert all(line == line.rstrip() for line in lines)
    assert not report.ok and report.failure_details() == "[b] fit: boom"
    doc = report.to_json()
    assert doc["ok"] is False and doc["fixtures"][0]["steps"][1]["passed"] == 2


def test_run_corpus_rejects_unknown_fixtures() -> None:
    with pytest.raises(ValueError, match="unknown fixtures"):
        run_corpus(["nope"])


def test_failing_expectation_fails_the_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    fx = load_fixture("xgb_store_demand")
    assert fx.expectations is not None

    def broken(bundle: object, data: object) -> None:
        raise AssertionError("deliberately broken")

    monkeypatch.setattr(fx.expectations, "check_fit", broken)
    report = run_fixture("xgb_store_demand", contract=False)
    step = report.step("expectations")
    assert step is not None and not step.ok and "deliberately broken" in step.detail
    assert not report.ok
    assert report.step("contract") is not None and report.step("contract").skipped  # type: ignore[union-attr]


def test_cli_list_and_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    assert capsys.readouterr().out.split() == FIXTURES
    out = tmp_path / "report.json"
    code = main(["run", "--fixture", "xgb_store_demand", "--no-contract", "-q", "--json", str(out)])
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert "xgb_store_demand" in printed and "1/1 fixtures passed" in printed
    doc = json.loads(out.read_text())
    assert doc["ok"] and doc["fixtures"][0]["model"] == "tabular.regressor@1.0.0"
    with pytest.raises(SystemExit):
        main(["run", "--fixture", "not_a_fixture"])
