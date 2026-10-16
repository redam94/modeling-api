"""M3.5 governance runs: model kinds, check applicability, the governance job and report,
the default policies and the comparison request."""

from __future__ import annotations

import pytest
from mapi_spec.errors import MapiError
from mapi_spec.executor import RUN_KIND_MAX_LEN, RunKind
from mapi_spec.explore import ModelCompareRequest
from mapi_spec.governance import (
    DEFAULT_POLICIES,
    GOVERNANCE_CHECKS,
    GOVERNANCE_OUTPUT,
    CheckResult,
    CheckSeverity,
    GovernanceJob,
    GovernanceReport,
    check_applies,
    model_kind,
)
from mapi_spec.jobs import parse_job
from mapi_spec.quotas import PriorityClass, default_priority
from pydantic import ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("inference", "rep", "model_id", "kind"),
    [
        ("mcmc", "draws", "mmm.bayesian", "bayesian"),
        ("vi", "draws", "ts.hsgp_trend", "bayesian"),
        ("smc", "draws", "x.y", "bayesian"),
        ("point", "point", "ts.sarimax", "frequentist"),
        ("kalman", "draws", "ts.unobserved_components", "frequentist"),
        ("map", "point", "mmm.frequentist", "frequentist"),
        ("point", "point", "sklearn.tabular_regressor", "ml"),
        ("point", "quantiles", "tabular.regressor", "ml"),
        ("bootstrap", "draws", "choice.multinomial_logit", "ml"),
        ("custom", "interval", "acme.conformal", "ml"),
    ],
)
def test_model_kind_from_capabilities(inference: str, rep: str, model_id: str, kind: str) -> None:
    assert model_kind(inference, representation=rep, model_id=model_id) == kind


def test_check_applicability_names_the_missing_requirement() -> None:
    assert check_applies("convergence", kind="ml", family="tabular") is None
    why = check_applies("sensitivity.prior", kind="frequentist", family="ts")
    assert why is not None and "Bayesian" in why
    assert check_applies("sensitivity.prior", kind="bayesian", family="mmm") is None
    why = check_applies("baseline.beats_naive", kind="ml", family="tabular", has=["holdout"])
    assert why is not None and "time" in why
    assert (
        check_applies(
            "baseline.beats_naive", kind="ml", family="ts", has=["holdout", "time_series"]
        )
        is None
    )


def test_governance_runs_are_a_sandbox_run_kind() -> None:
    assert RunKind.GOVERNANCE.value == "governance"
    assert len(RunKind.GOVERNANCE.value) <= RUN_KIND_MAX_LEN
    assert not RunKind.GOVERNANCE.is_parent
    assert default_priority(RunKind.GOVERNANCE) is PriorityClass.BATCH
    assert default_priority(RunKind.EXPLORE) is PriorityClass.INTERACTIVE
    job = parse_job(RunKind.GOVERNANCE, GovernanceJob.model_json_schema()["examples"][0])
    assert isinstance(job, GovernanceJob) and job.budget.max_refits == 60
    with pytest.raises(MapiError):
        parse_job(RunKind.GOVERNANCE, {"model_ref": "model://p/m@v1", "checks": []})
    with pytest.raises(ValidationError):
        GovernanceJob(
            model_ref="model://p/m@v1",
            checks=[{"check": "convergence"}, {"check": "convergence"}],  # type: ignore[list-item]
        )
    assert GOVERNANCE_OUTPUT == "governance.json"


def test_report_and_result_carry_a_suggested_action() -> None:
    r = CheckResult(
        check="residuals.whiteness",
        status="warn",
        severity=CheckSeverity.WARNING,
        message_plain="A weekly pattern is left in the errors.",
        suggested_action="Add a seasonal term and refit.",
    )
    report = GovernanceReport(
        model_ref="model://p/m@v1", model_id="ts.sarimax", family="ts",
        model_kind="frequentist", results=[r],
    )  # fmt: skip
    again = GovernanceReport.model_validate_json(report.model_dump_json())
    assert again.results[0].suggested_action == "Add a seasonal term and refit."
    assert again.format == "mapi.governance-report/v1"


def test_default_policies_cover_every_family_and_never_gate_promotion() -> None:
    families = {f for p in DEFAULT_POLICIES for f in p.applies_to.families}
    assert {"mmm", "ts", "forecast", "tabular", "choice"} <= families
    for p in DEFAULT_POLICIES:
        assert not p.require_validated_for_promotion and not p.require_approval
        assert {c.check for c in p.checks} <= set(GOVERNANCE_CHECKS)
        assert any(c.severity is CheckSeverity.BLOCKING for c in p.checks)
        assert "data.quality" in {c.check for c in p.checks}
    mmm = next(p for p in DEFAULT_POLICIES if "mmm" in p.applies_to.families)
    assert {"null.permutation_target", "null.noise_regressors", "plausibility.signs"} <= {
        c.check for c in mmm.checks
    }


def test_compare_request_becomes_an_explore_request_on_the_first_model() -> None:
    req = ModelCompareRequest(models=["model://p/a@v1", "model://p/b@v2", "model://p/c@v1"])
    ex = req.explore_request()
    assert ex.compare_with == ["model://p/b@v2", "model://p/c@v1"]
    assert ex.sections == ["comparison"]
    with pytest.raises(ValidationError):
        ModelCompareRequest(models=["model://p/a@v1"])
    with pytest.raises(ValidationError):
        ModelCompareRequest(models=["model://p/a@v1", "model://p/a@v1"])
    with pytest.raises(ValidationError):
        ModelCompareRequest(models=["model://p/a@v1", "model://p/b@v1"], sections=["params"])
