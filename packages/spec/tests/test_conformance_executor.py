"""The executor conformance suite contract (plan §13): coverage, applicability, checks."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mapi_spec.conformance_executor import (
    CONFORMANCE_SUITE_VERSION,
    EXECUTOR_CONFORMANCE,
    CaseOutcome,
    ConformanceCase,
    ConformanceCaseId,
    ConformanceReport,
    ConformanceResult,
    ConformanceTarget,
    Fault,
    ObservedRun,
    Requirement,
    applicable_cases,
    case,
    check_expectation,
)
from mapi_spec.executor import ExecutorCaps, Isolation
from mapi_spec.run import TRANSITIONS, FailureClass, RunState, RunTrigger

S = RunState
NOW = datetime(2026, 10, 15, tzinfo=UTC)

REQUIRED_BEHAVIOURS = {
    "submit": ConformanceCaseId.SUBMIT,
    "adopt after dispatcher crash": ConformanceCaseId.ADOPT_AFTER_DISPATCHER_CRASH,
    "duplicate-submit safety": ConformanceCaseId.DUPLICATE_SUBMIT,
    "cancel mid-run": ConformanceCaseId.CANCEL_MID_RUN,
    "timeout": ConformanceCaseId.TIMEOUT,
    "STALLED without false LOST": ConformanceCaseId.STALLED_NOT_LOST,
    "confirmed cancel before retry": ConformanceCaseId.CONFIRMED_CANCEL_BEFORE_RETRY,
    "checkpoint resume": ConformanceCaseId.CHECKPOINT_RESUME,
    "OOM classification": ConformanceCaseId.OOM_CLASSIFICATION,
    "fan-out taskCount": ConformanceCaseId.FANOUT_TASK_COUNT,
    "generation verification": ConformanceCaseId.GENERATION_VERIFICATION,
    "429 back-off": ConformanceCaseId.RATE_LIMIT_BACKOFF,
}


def test_suite_covers_every_required_behaviour_exactly_once() -> None:
    ids = [c.id for c in EXECUTOR_CONFORMANCE]
    assert len(ids) == len(set(ids)) == len(ConformanceCaseId) == 12
    assert set(REQUIRED_BEHAVIOURS.values()) == set(ids)
    assert CONFORMANCE_SUITE_VERSION == "1"


@pytest.mark.parametrize("c", EXECUTOR_CONFORMANCE, ids=lambda c: c.id.value)
def test_cases_are_well_formed(c: ConformanceCase) -> None:
    assert c.title and c.behaviour and c.steps and c.plan_refs
    e = c.expect
    assert not set(e.must_visit) & set(e.must_not_visit)
    assert (e.final_state is S.FAILED) == (e.failure_class is not None)
    assert e.final_state.terminal
    assert case(c.id.value) is c
    assert ConformanceCase.model_validate_json(c.model_dump_json()) == c


def test_expected_failure_classes_are_reachable_in_the_state_machine() -> None:
    fail_rules = {
        r.failure_class for r in TRANSITIONS.values() if r.target is S.FAILED and r.failure_class
    }
    for c in EXECUTOR_CONFORMANCE:
        if c.expect.failure_class is not None:
            assert c.expect.failure_class in fail_rules, c.id
    assert (S.STALLED, RunTrigger.EVENT) in TRANSITIONS


def _caps(**kw: object) -> ExecutorCaps:
    return ExecutorCaps.model_validate({"isolation": Isolation.PROCESS, **kw})


def test_applicability_follows_requirements() -> None:
    local = ConformanceTarget(executor="local-process", caps=_caps())
    names = {c.id for c in applicable_cases(local)}
    assert ConformanceCaseId.FANOUT_TASK_COUNT not in names
    assert ConformanceCaseId.RATE_LIMIT_BACKOFF not in names
    assert ConformanceCaseId.CHECKPOINT_RESUME not in names
    assert ConformanceCaseId.SUBMIT in names and ConformanceCaseId.STALLED_NOT_LOST in names
    cloud = ConformanceTarget(
        executor="cloudrun-jobs",
        caps=_caps(isolation=Isolation.MICROVM, max_tasks=10_000, checkpoint_storage=True),
        remote_control_api=True,
        enforces_memory_limit=True,
    )
    assert len(applicable_cases(cloud)) == 12
    assert all(cloud.supports(r) for r in Requirement)


def _observed(c: ConformanceCase, **kw: object) -> ObservedRun:
    e = c.expect
    states = [S.QUEUED, S.DISPATCHING, *e.must_visit, e.final_state]
    base: dict[str, object] = {
        "states": tuple(dict.fromkeys(states)),
        "failure_class": e.failure_class,
        "attempts": e.attempts or 1,
        "executions_created": e.executions_created or 1,
        "max_live_executions": 1,
        "offered_next_class": e.offers_next_class,
    }
    base.update(kw)
    return ObservedRun.model_validate(base)


@pytest.mark.parametrize("c", EXECUTOR_CONFORMANCE, ids=lambda c: c.id.value)
def test_a_conforming_observation_passes(c: ConformanceCase) -> None:
    assert check_expectation(c, _observed(c)) == []


def test_violations_are_reported() -> None:
    stalled = case(ConformanceCaseId.STALLED_NOT_LOST)
    bad = _observed(
        stalled,
        states=(S.RUNNING, S.FAILED),
        failure_class=FailureClass.INFRA,
        attempts=2,
        executions_created=2,
        max_live_executions=2,
    )
    problems = check_expectation(stalled, bad)
    joined = "\n".join(problems)
    for needle in (
        "final state failed",
        "failure class infra",
        "never visited stalled",
        "visited failed",
        "2 attempts",
        "2 executions live",
        "2 executions created",
    ):
        assert needle in joined, needle  # fmt: skip
    oom = case(ConformanceCaseId.OOM_CLASSIFICATION)
    assert "no offer" in "".join(check_expectation(oom, _observed(oom, offered_next_class=False)))
    assert oom.fault is Fault.MEMORY_BOMB


def test_report_passes_only_with_every_applicable_case() -> None:
    target = ConformanceTarget(executor="local-process", caps=_caps())
    cases = applicable_cases(target)
    results = [ConformanceResult(case_id=c.id, outcome=CaseOutcome.PASSED) for c in cases]
    report = ConformanceReport(executor="local-process", started_at=NOW, results=results)
    assert report.passed_for(target) and report.missing(target) == []
    partial = report.model_copy(update={"results": results[:-1]})
    assert not partial.passed_for(target) and partial.missing(target) == [cases[-1].id]
    failed = report.model_copy(
        update={
            "results": [*results[:-1], ConformanceResult(case_id=cases[-1].id, outcome="failed")]
        }
    )
    assert not failed.passed_for(target)
    assert not report.model_copy(update={"suite_version": "0"}).passed_for(target)
    other = ConformanceTarget(executor="docker", caps=_caps())
    assert not report.passed_for(other)
