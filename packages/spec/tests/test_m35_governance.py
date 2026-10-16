"""M3.5 governance: the exhaustive validation state machine, approvals, policies (matching,
merging, check parameters), check statuses, the promotion gate and the evidence pack."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.governance import (
    GOVERNANCE_CHECKS,
    TRANSITIONS,
    ApprovalCreate,
    CheckResult,
    CheckSeverity,
    CheckStatus,
    EvidencePack,
    GovernancePolicy,
    GovernancePolicyCreate,
    ModelValidation,
    PolicyCheck,
    PolicyMatch,
    ValidationEvent,
    ValidationStatus,
    after_checks,
    check_approver,
    check_status,
    effective_checks,
    policies_for,
    requires_validation,
    rollup_results,
    transition,
)
from mapi_spec.scorecard import (
    CheckThresholds,
    GateOutcome,
    Scorecard,
    authorize_promotion,
    promotion_gate,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit
S, E = ValidationStatus, ValidationEvent

EXPECTED: dict[tuple[ValidationStatus, ValidationEvent], ValidationStatus] = {
    (S.DRAFT, E.SUBMIT): S.CHECKS_RUNNING,
    (S.CHECKS_RUNNING, E.CHECKS_PASSED): S.CHECKS_PASSED,
    (S.CHECKS_RUNNING, E.CHECKS_FAILED): S.CHECKS_FAILED,
    (S.CHECKS_RUNNING, E.CANCEL): S.DRAFT,
    (S.CHECKS_PASSED, E.REQUEST_APPROVAL): S.AWAITING_APPROVAL,
    (S.CHECKS_PASSED, E.VALIDATE): S.VALIDATED,
    (S.CHECKS_FAILED, E.RESUBMIT): S.CHECKS_RUNNING,
    (S.CHECKS_FAILED, E.REJECT): S.REJECTED,
    (S.AWAITING_APPROVAL, E.APPROVE): S.VALIDATED,
    (S.AWAITING_APPROVAL, E.REJECT): S.REJECTED,
    (S.REJECTED, E.RESUBMIT): S.CHECKS_RUNNING,
    (S.VALIDATED, E.DEPRECATE): S.DEPRECATED,
}


@pytest.mark.parametrize(("status", "event"), list(itertools.product(S, E)), ids=lambda x: x.value)
def test_every_status_event_pair(status: ValidationStatus, event: ValidationEvent) -> None:
    want = EXPECTED.get((status, event))
    approval = event is E.REQUEST_APPROVAL  # the policy guard for checks_passed
    if want is None:
        with pytest.raises(MapiError) as exc:
            transition(status, event, require_approval=approval)
        assert exc.value.code is ErrorCode.VALIDATION_TRANSITION_INVALID
    else:
        assert transition(status, event, require_approval=approval) is want
        assert transition(status.value, event.value, require_approval=approval) is want


def test_the_table_and_the_approval_guard() -> None:
    assert dict(TRANSITIONS) == EXPECTED
    with pytest.raises(MapiError, match="approval"):
        transition(S.CHECKS_PASSED, E.VALIDATE, require_approval=True)
    with pytest.raises(MapiError, match="does not require approval"):
        transition(S.CHECKS_PASSED, E.REQUEST_APPROVAL, require_approval=False)
    assert S.DEPRECATED.terminal and not any(s.terminal for s in S if s is not S.DEPRECATED)
    # deprecated has no way out; every other status can be left
    assert not [e for (s, e) in EXPECTED if s is S.DEPRECATED]
    assert {s for (s, _) in EXPECTED} == set(S) - {S.DEPRECATED}


def test_full_paths_through_the_machine() -> None:
    s = S.DRAFT
    for e in (E.SUBMIT, *after_checks(True, require_approval=True), E.APPROVE, E.DEPRECATE):
        s = transition(s, e, require_approval=True)
    assert s is S.DEPRECATED
    s = transition(S.DRAFT, E.SUBMIT)
    for e in after_checks(True, require_approval=False):
        s = transition(s, e)
    assert s is S.VALIDATED
    s = transition(transition(S.DRAFT, E.SUBMIT), *after_checks(False, require_approval=True))
    assert s is S.CHECKS_FAILED
    assert transition(transition(s, E.REJECT), E.RESUBMIT) is S.CHECKS_RUNNING


def test_approvals_need_a_different_reviewer() -> None:
    check_approver(submitter_id="usr_a", approver_id="usr_b", is_reviewer=True)
    with pytest.raises(MapiError) as exc:
        check_approver(submitter_id="usr_a", approver_id="usr_a", is_reviewer=True)
    assert exc.value.code is ErrorCode.SELF_APPROVAL_FORBIDDEN and exc.value.status == 403
    with pytest.raises(MapiError) as exc:
        check_approver(submitter_id="usr_a", approver_id="usr_b", is_reviewer=False)
    assert exc.value.code is ErrorCode.FORBIDDEN
    with pytest.raises(MapiError) as exc:
        check_approver(
            submitter_id="usr_a", approver_id="usr_b", is_reviewer=True, already_approved=["usr_b"]
        )
    assert exc.value.code is ErrorCode.CONFLICT
    with pytest.raises(ValidationError, match="comment"):
        ApprovalCreate(decision="reject")
    assert ApprovalCreate(decision="approve").comment is None


def _policy(
    pid: str, project: str | None, checks: list[dict[str, Any]], **kw: Any
) -> GovernancePolicy:
    return GovernancePolicy.model_validate(
        {
            "id": pid,
            "tenant_id": "acme",
            "project_id": project,
            "scope": "project" if project else "tenant",
            "version": 1,
            "name": pid,
            "checks": checks,
            "created_at": "2026-10-15T09:30:00Z",
            **kw,
        }
    )


def test_policies_match_merge_and_require_validation() -> None:
    floor = _policy(
        "pol_floor",
        None,
        [
            {
                "check": "holdout.accuracy",
                "severity": "warning",
                "thresholds": {"warn": 0.2, "fail": 0.4},
            },
            {"check": "convergence"},
        ],
    )
    ts = _policy(
        "pol_ts",
        "growth",
        [
            {
                "check": "holdout.accuracy",
                "severity": "blocking",
                "thresholds": {"warn": 0.1, "fail": 0.5},
            },
            {"check": "baseline.beats_naive", "thresholds": {"warn": 0.1, "fail": 0.02}},
        ],
        applies_to={"families": ["ts"]},
        require_validated_for_promotion=False,
    )
    other = _policy("pol_other", "other", [{"check": "data.quality"}])
    off = _policy("pol_off", "growth", [{"check": "data.quality"}], active=False)
    got = policies_for(
        [other, ts, off, floor], project_id="growth", family="ts", model_id="ts.sarimax"
    )
    assert [p.id for p in got] == ["pol_floor", "pol_ts"]
    mmm = policies_for([floor, ts], project_id="growth", family="mmm", model_id="mmm.bayesian")
    assert [p.id for p in mmm] == ["pol_floor"]
    # the statsmodels forecasters declare family "forecast": `families: [ts]` (the docs'
    # example) still covers them through the id's namespace, and nothing else does
    sm = policies_for([ts], project_id="growth", family="forecast", model_id="ts.sarimax")
    assert [p.id for p in sm] == ["pol_ts"]
    var = policies_for([ts], project_id="growth", family="ts_multivariate", model_id="ts.var")
    assert [p.id for p in var] == ["pol_ts"]
    assert not policies_for([ts], project_id="growth", family="forecast", model_id="tsx.arima")
    fc = ts.model_copy(update={"applies_to": PolicyMatch(families=["forecast"])})
    assert not policies_for([fc], project_id="growth", family="ts", model_id="ts.hsgp_trend")
    merged = {c.check: c for c in effective_checks(got)}
    acc = merged["holdout.accuracy"]
    assert acc.severity is CheckSeverity.BLOCKING
    assert acc.thresholds == CheckThresholds(warn=0.1, fail=0.4)  # stricter of each (max dir)
    assert set(merged) == {"holdout.accuracy", "convergence", "baseline.beats_naive"}
    assert requires_validation(got) and not requires_validation([ts])
    with pytest.raises(ValidationError, match="tenant policies"):
        _policy("x", "growth", [{"check": "convergence"}], scope="tenant")


def test_a_project_policy_cannot_relax_tenant_check_params() -> None:
    """Regression: the project's params used to overwrite the tenant's, so a project admin
    could switch off tenant-mandated data limits and sign constraints."""
    tenant = _policy(
        "pol_floor",
        None,
        [
            {"check": "data.quality", "params": {"max_missing": 0.01, "min_history_periods": 104}},
            {"check": "plausibility.signs", "params": {"expected": {"tv": "positive"}}},
            {"check": "null.permutation_target", "params": {"n_permutations": 50, "metric": "mae"}},
            {"check": "baseline.beats_naive", "params": {"methods": ["seasonal_naive"]}},
            {"check": "null.noise_regressors", "params": {"n_noise": 5}},
            {"check": "null.placebo_time_shift", "params": {"shifts": [-26]}},
            {"check": "stability.rolling_params", "params": {"window": 52, "params": ["tv"]}},
        ],
    )
    project = _policy(
        "pol_proj",
        "growth",
        [
            {"check": "data.quality", "params": {"max_missing": 1.0, "min_history_periods": 1}},
            {
                "check": "plausibility.signs",
                "params": {"expected": {"tv": "negative", "price": "negative"}},
            },
            {
                "check": "null.permutation_target",
                "params": {"n_permutations": 5, "metric": "crps", "block": 4},
            },
            {"check": "baseline.beats_naive", "params": {"methods": ["naive"]}},
            {"check": "null.noise_regressors", "params": {"n_noise": 1}},
            {"check": "null.placebo_time_shift", "params": {"shifts": [52]}},
            {"check": "stability.rolling_params", "params": {"window": 8, "params": []}},
        ],
    )
    got = policies_for([project, tenant], project_id="growth", family="mmm", model_id="mmm.x")
    merged = {c.check: c.params for c in effective_checks(got)}
    assert merged["data.quality"] == {"max_missing": 0.01, "min_history_periods": 104}
    # the tenant's sign wins a conflict; the project may add expectations
    assert merged["plausibility.signs"] == {"expected": {"tv": "positive", "price": "negative"}}
    # more permutations is stricter; the tenant's metric stays; a key the tenant left
    # unset may be added by the project
    assert merged["null.permutation_target"] == {
        "n_permutations": 50,
        "metric": "mae",
        "block": 4,
    }
    assert merged["baseline.beats_naive"] == {"methods": ["seasonal_naive", "naive"]}
    assert merged["null.noise_regressors"] == {"n_noise": 5}
    assert merged["null.placebo_time_shift"] == {"shifts": [-26, 52]}
    assert merged["stability.rolling_params"] == {"window": 52, "params": ["tv"]}


@pytest.mark.parametrize(
    ("check", "message"),
    [
        ({"check": "nope"}, "unknown governance check"),
        ({"check": "null.permutation_target", "params": {"n_permutations": 1}}, "minimum"),
        ({"check": "null.permutation_target", "params": {"bogus": 1}}, "Additional"),
        (
            {"check": "baseline.beats_naive", "thresholds": {"warn": 0.0, "fail": 0.1}},
            "at least as strict",
        ),
    ],
)
def test_policy_checks_are_validated(check: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        PolicyCheck.model_validate(check)


def test_policy_create_rules_and_example() -> None:
    ex = GovernancePolicyCreate.model_validate(
        GovernancePolicyCreate.model_json_schema()["examples"][0]
    )
    assert ex.require_approval and len(ex.checks) == 4
    with pytest.raises(ValidationError, match="once"):
        GovernancePolicyCreate(name="x", checks=[PolicyCheck(check="convergence")] * 2)


@pytest.mark.parametrize(
    ("check", "value", "status"),
    [
        ("null.permutation_target", 0.01, "pass"),
        ("null.permutation_target", 0.07, "warn"),
        ("null.permutation_target", 0.2, "fail"),
        ("baseline.beats_naive", 0.3, "pass"),
        ("baseline.beats_naive", 0.03, "warn"),
        ("baseline.beats_naive", -0.1, "fail"),
        ("residuals.whiteness", 0.5, "pass"),
        ("residuals.whiteness", 0.001, "fail"),
        ("recovery.simulated", 0.88, "pass"),
        ("recovery.simulated", 0.75, "warn"),
        ("recovery.simulated", 0.5, "fail"),
        ("convergence", 1.003, "pass"),
        ("convergence", float("inf"), "error"),
        ("data.quality", 0, "pass"),
        ("data.quality", 1, "warn"),
        ("data.quality", 3, "fail"),
    ],
)
def test_check_statuses(check: str, value: float, status: str) -> None:
    assert check_status(check, value) is CheckStatus(status)


def test_the_check_catalog() -> None:
    assert set(GOVERNANCE_CHECKS) == {
        "null.permutation_target", "null.noise_regressors", "null.placebo_time_shift",
        "baseline.beats_naive", "residuals.whiteness", "stability.rolling_params",
        "plausibility.signs", "sensitivity.prior", "recovery.simulated", "convergence",
        "holdout.accuracy", "data.quality",
    }  # fmt: skip
    assert "bayesian" in GOVERNANCE_CHECKS["sensitivity.prior"].requires
    assert check_status("holdout.accuracy", 0.12, CheckThresholds(warn=0.1, fail=0.2)) is (
        CheckStatus.WARN
    )


def test_rollup_counts_missing_blocking_results_as_failures() -> None:
    required = [PolicyCheck(check="convergence"),
                PolicyCheck(check="residuals.whiteness", severity="warning"),
                PolicyCheck(check="data.quality")]  # fmt: skip
    results = [
        CheckResult(check="convergence", status="pass", severity="blocking", message_plain="ok"),
        CheckResult(check="residuals.whiteness", status="fail", severity="warning",
                    message_plain="pattern left"),
    ]  # fmt: skip
    assert rollup_results(results, required) == (False, ["data.quality"])
    results.append(
        CheckResult(
            check="data.quality", status="error", severity="blocking", message_plain="could not run"
        )
    )
    assert rollup_results(results, required) == (False, ["data.quality"])
    results[-1] = results[-1].model_copy(update={"status": CheckStatus.WARN})
    assert rollup_results(results, required) == (True, [])


def test_the_promotion_gate_requires_validated_versions_when_a_policy_says_so() -> None:
    good = Scorecard(overall="pass", items=[])
    allowed = promotion_gate(good, fit_mode="train_only", allow_train_only=True)
    assert allowed.outcome is GateOutcome.ALLOWED
    blocked = promotion_gate(
        good,
        fit_mode="train_only",
        allow_train_only=True,
        require_validated=True,
        validation_status="awaiting_approval",
    )
    assert blocked.outcome is GateOutcome.BLOCKED
    assert [r.code for r in blocked.reasons] == ["NOT_VALIDATED"]
    assert "awaiting approval" in blocked.reasons[0].message_plain
    with pytest.raises(MapiError) as exc:
        authorize_promotion(blocked, override=True, reason="ship it", can_override=True)
    assert exc.value.code is ErrorCode.NOT_VALIDATED and exc.value.status == 409
    ok = promotion_gate(
        good,
        fit_mode="train_only",
        allow_train_only=True,
        require_validated=True,
        validation_status="validated",
    )
    assert ok.outcome is GateOutcome.ALLOWED


def test_dto_examples_round_trip() -> None:
    for model in (ModelValidation, EvidencePack, CheckResult, GovernancePolicy):
        ex = model.model_json_schema()["examples"][0]
        assert model.model_validate(ex).model_dump(mode="json", exclude_none=True)
