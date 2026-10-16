"""M3 calibration: business priors, lift tests, the spec's calibration block, its static
check and prior translation (job, result, application)."""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import pytest
from mapi_spec import Catalog, DataSchema, ModelSpec
from mapi_spec.calibration import (
    PRIORS_POINTER,
    BusinessPriors,
    Calibration,
    ChannelBelief,
    DistributionSummary,
    FanChart,
    LiftTest,
    PriorTranslationJob,
    PriorTranslationResult,
    apply_prior_translation,
    check_business_priors,
    logitnormal_from_range,
    lognormal_from_range,
)
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.examples import E33_YAML, SCORE_YAML
from mapi_spec.executor import RunKind
from mapi_spec.jobs import parse_job
from mapi_spec.loader import load_model_spec, parse_yaml, spec_hash
from mapi_spec.validate import validate_spec
from pydantic import ValidationError

pytestmark = pytest.mark.unit

EX_PRIORS = BusinessPriors.model_json_schema()["examples"][0]
EX_LIFT = LiftTest.model_json_schema()["examples"][0]


# ------------------------------------------------------------------------------ beliefs


def test_a_belief_is_exactly_one_of_roi_or_share() -> None:
    assert ChannelBelief(roi=(1.0, 3.0)).credibility == 0.8
    for bad in ({}, {"roi": [1, 2], "contribution_share": [0.1, 0.2]}):
        with pytest.raises(ValidationError, match="exactly one"):
            ChannelBelief.model_validate(bad)


@pytest.mark.parametrize(
    "belief",
    [
        {"roi": [3, 1]},
        {"roi": [2, 2]},
        {"roi": [-1, 2]},
        {"roi": [1, math.inf]},
        {"contribution_share": [0.1, 1.2]},
        {"roi": [1, 2], "credibility": 0.3},
        {"roi": [1, 2], "credibility": 0.999},
    ],
)
def test_bad_ranges_are_schema_errors(belief: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ChannelBelief.model_validate(belief)


def test_share_beliefs_must_fit_inside_the_kpi() -> None:
    ok = BusinessPriors.model_validate(EX_PRIORS)
    assert check_business_priors(ok) == []
    too_much = BusinessPriors.model_validate(
        {
            "channels": {
                "tv": {"contribution_share": [0.3, 0.5]},
                "search": {"contribution_share": [0.2, 0.4]},
            },
            "baseline_share": {"share": [0.6, 0.9]},
        }
    )
    [issue] = check_business_priors(too_much, "/calibration/business_priors")
    assert issue.code is ErrorCode.BUSINESS_PRIORS_INVALID
    assert issue.severity is Severity.ERROR
    assert issue.pointer == "/calibration/business_priors/channels"
    too_little = BusinessPriors.model_validate(
        {
            "channels": {"tv": {"contribution_share": [0.05, 0.1]}},
            "baseline_share": {"share": [0.2, 0.3]},
        }
    )
    [warn] = check_business_priors(too_little)
    assert warn.severity is Severity.WARNING


def test_a_baseline_belief_stands_on_its_own() -> None:
    # "Results without any media" needs no channel belief beside it (M3 review)
    only = BusinessPriors.model_validate({"baseline_share": {"share": [0.4, 0.8]}})
    assert only.channels == {}
    assert check_business_priors(only) == []  # nothing about channel shares to warn of
    for empty in ({}, {"channels": {}}, {"channels": {}, "baseline_share": None}):
        with pytest.raises(ValidationError, match="at least one belief"):
            BusinessPriors.model_validate(empty)


def test_channel_labels_are_printable() -> None:
    with pytest.raises(ValidationError):
        BusinessPriors.model_validate({"channels": {"tv\n": {"roi": [1, 2]}}})


@pytest.mark.parametrize("cred", [0.5, 0.8, 0.95])
def test_range_translations_put_the_stated_mass_inside_the_range(cred: float) -> None:
    mu, sigma = lognormal_from_range(1.5, 4.0, cred)
    nd = NormalDist(mu, sigma)
    inside = nd.cdf(math.log(4.0)) - nd.cdf(math.log(1.5))
    assert inside == pytest.approx(cred, abs=1e-9)
    mu, sigma = logitnormal_from_range(0.05, 0.15, cred)
    nd = NormalDist(mu, sigma)
    logit = lambda p: math.log(p / (1 - p))  # noqa: E731
    assert nd.cdf(logit(0.15)) - nd.cdf(logit(0.05)) == pytest.approx(cred, abs=1e-9)
    with pytest.raises(ValueError):
        lognormal_from_range(0.0, 1.0, cred)
    with pytest.raises(ValueError):
        logitnormal_from_range(0.1, 1.0, cred)


# ----------------------------------------------------------------------------- lift tests


def test_lift_test_uncertainty_and_implied_roi() -> None:
    t = LiftTest.model_validate(EX_LIFT)
    assert t.implied_roi == pytest.approx(260000 / 120000)
    z = NormalDist().inv_cdf(0.95)
    assert t.sigma == pytest.approx(240000 / (2 * z))
    se = LiftTest.model_validate({**EX_LIFT, "ci": None, "se": 50000.0})
    assert se.sigma == 50000.0


@pytest.mark.parametrize(
    "change",
    [
        {"end": "2026-04-01"},
        {"spend_delta": 0},
        {"se": 1.0},
        {"ci": None},
        {"ci": [-100.0, -200.0]},
        {"ci": [0.0, 1.0]},
        {"kpi_delta": math.nan},
        {"id": "Not A Slug"},
    ],
)
def test_lift_test_rules(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LiftTest.model_validate({**EX_LIFT, **change})


def test_calibration_block_rejects_duplicate_lift_ids() -> None:
    with pytest.raises(ValidationError, match="unique"):
        Calibration.model_validate({"lift_tests": [EX_LIFT, EX_LIFT]})
    assert Calibration().empty
    assert not Calibration.model_validate(Calibration.model_json_schema()["examples"][0]).empty


# ----------------------------------------------------------------------- spec integration


def _with_calibration(cal: dict[str, Any]) -> dict[str, Any]:
    doc = parse_yaml(E33_YAML)
    assert isinstance(doc, dict)
    doc["calibration"] = cal
    return doc


def test_a_spec_without_calibration_keeps_its_hash() -> None:
    spec = load_model_spec(E33_YAML)
    assert spec.calibration is None
    with_none = ModelSpec.model_validate({**spec.model_dump(by_alias=True), "calibration": None})
    assert spec_hash(spec) == spec_hash(with_none)


def _catalog_with_calibration(catalog: Catalog, kinds: list[str]) -> Catalog:
    data = catalog.model_dump(mode="json", by_alias=True)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("id") == "mmm.bayesian" and isinstance(node.get("capabilities"), dict):
                node["capabilities"]["calibration"] = kinds
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return Catalog.model_validate(data)


def test_calibration_needs_a_model_that_declares_it(catalog: Catalog, e33_raw: DataSchema) -> None:
    spec = ModelSpec.model_validate(
        _with_calibration({"business_priors": EX_PRIORS, "lift_tests": [EX_LIFT]})
    )
    res = validate_spec(spec, catalog, e33_raw)
    codes = {(i.code, i.pointer) for i in res.issues}
    assert (ErrorCode.CALIBRATION_UNSUPPORTED, "/calibration/business_priors") in codes
    assert (ErrorCode.CALIBRATION_UNSUPPORTED, "/calibration/lift_tests") in codes
    full = _catalog_with_calibration(catalog, ["business_priors", "lift_tests"])
    ok = validate_spec(spec, full, e33_raw)
    assert not [i for i in ok.issues if i.code is ErrorCode.CALIBRATION_UNSUPPORTED]
    assert ok.resolved is not None
    partial = _catalog_with_calibration(catalog, ["business_priors"])
    res = validate_spec(spec, partial, e33_raw)
    assert [i.pointer for i in res.issues if i.code is ErrorCode.CALIBRATION_UNSUPPORTED] == [
        "/calibration/lift_tests"
    ]


def test_inconsistent_business_priors_are_reported_by_validation(
    catalog: Catalog, e33_raw: DataSchema
) -> None:
    bad = {
        "channels": {"tv": {"contribution_share": [0.7, 0.9]}},
        "baseline_share": {"share": [0.5, 0.6]},
    }
    spec = ModelSpec.model_validate(_with_calibration({"business_priors": bad}))
    res = validate_spec(spec, _catalog_with_calibration(catalog, ["business_priors"]), e33_raw)
    assert [i.code for i in res.issues if i.severity is Severity.ERROR] == [
        ErrorCode.BUSINESS_PRIORS_INVALID
    ]


# ---------------------------------------------------------------------- prior translation


def test_translate_priors_jobs_parse_and_check_their_spec() -> None:
    doc = parse_yaml(E33_YAML)
    job = parse_job(RunKind.TRANSLATE_PRIORS, {"spec": doc, "business_priors": EX_PRIORS})
    assert isinstance(job, PriorTranslationJob)
    assert job.draws == 500
    with pytest.raises(MapiError) as exc:
        parse_job(
            RunKind.TRANSLATE_PRIORS, {"spec": parse_yaml(SCORE_YAML), "business_priors": EX_PRIORS}
        )
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(ValidationError):
        PriorTranslationJob.model_validate(
            {"spec": doc, "business_priors": EX_PRIORS, "quantiles": [0.9, 0.1]}
        )


def test_result_views_check_their_shapes() -> None:
    res = PriorTranslationResult.model_validate(
        PriorTranslationResult.model_json_schema()["examples"][0]
    )
    assert res.preview.kpi_fan is not None
    with pytest.raises(ValidationError):
        PriorTranslationResult.model_validate(
            {**res.model_dump(mode="json"), "coverage": {"tv": 1.5}}
        )
    fan = FanChart.model_json_schema()["examples"][0]
    for bad in (
        {"quantiles": [0.5, 0.05, 0.95]},
        {"values": [[1.0, 2.0]]},
        {"observed": [1.0]},
        {"values": [[1.0], [2.0], [3.0]]},
    ):
        with pytest.raises(ValidationError):
            FanChart.model_validate({**fan, **bad})
    with pytest.raises(ValidationError):
        DistributionSummary.model_validate({"mean": 1, "sd": 1, "quantiles": {"1.5": 2}})


def test_apply_prior_translation_merges_priors_and_records_lineage() -> None:
    doc = parse_yaml(E33_YAML)
    assert isinstance(doc, dict)
    res = PriorTranslationResult.model_validate(
        PriorTranslationResult.model_json_schema()["examples"][0]
    )
    run = "run_01jaf3q2x8m9v7k5h4g3f2e1d0"
    out = apply_prior_translation(
        doc, res, business_priors=BusinessPriors.model_validate(EX_PRIORS), run_id=run
    )
    assert doc["model"]["settings"]["priors"]["beta_channel"]["distribution"] == "HalfNormal"
    priors = out["model"]["settings"]["priors"]
    assert priors["beta_channel"]["distribution"] == "LogNormal"  # translated keys win
    assert priors["lam"]["distribution"] == "Gamma"  # others are kept
    assert out["calibration"]["prior_translation_run"] == run
    spec = ModelSpec.model_validate(out)
    assert spec.calibration is not None and spec.calibration.business_priors is not None
    assert PRIORS_POINTER == "/model/settings/priors"
    with pytest.raises(ValueError):
        apply_prior_translation({"model": []}, res)
