"""The executor conformance suite, as data (plan §8.4, §13 "Executor conformance").

An executor (``local-process``, ``docker``, ``docker-gvisor``, ``cloudrun-jobs``, ...) is
enabled only after it passes every applicable case of :data:`EXECUTOR_CONFORMANCE`. Each
:class:`ConformanceCase` names the behaviour, the fault the harness injects, what the
backend must support for the case to apply, and the :class:`Expectation` on the run's
observed trajectory. The server's pytest suite parametrizes over this tuple and checks each
observation with :func:`check_expectation`; the admin API reports results as a
:class:`ConformanceReport`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal

from pydantic import AwareDatetime, Field

from mapi_spec._dto import ResponseModel, dto_examples
from mapi_spec._types import StrictModel
from mapi_spec.executor import ExecutorCaps
from mapi_spec.run import FailureClass, RunState

__all__ = [
    "CONFORMANCE_SUITE_VERSION",
    "EXECUTOR_CONFORMANCE",
    "CaseOutcome",
    "ConformanceCase",
    "ConformanceCaseId",
    "ConformanceReport",
    "ConformanceResult",
    "ConformanceTarget",
    "Expectation",
    "Fault",
    "ObservedRun",
    "Requirement",
    "applicable_cases",
    "case",
    "check_expectation",
]

CONFORMANCE_SUITE_VERSION: Final = "1"


class ConformanceCaseId(StrEnum):
    SUBMIT = "submit"
    ADOPT_AFTER_DISPATCHER_CRASH = "adopt_after_dispatcher_crash"
    DUPLICATE_SUBMIT = "duplicate_submit"
    CANCEL_MID_RUN = "cancel_mid_run"
    TIMEOUT = "timeout"
    STALLED_NOT_LOST = "stalled_not_lost"
    CONFIRMED_CANCEL_BEFORE_RETRY = "confirmed_cancel_before_retry"
    CHECKPOINT_RESUME = "checkpoint_resume"
    OOM_CLASSIFICATION = "oom_classification"
    FANOUT_TASK_COUNT = "fanout_task_count"
    GENERATION_VERIFICATION = "generation_verification"
    RATE_LIMIT_BACKOFF = "rate_limit_backoff"


class Fault(StrEnum):
    """What the harness does to the system under test."""

    NONE = "none"
    DISPATCHER_CRASH_AFTER_SUBMIT = "dispatcher_crash_after_submit"
    """Kill the dispatcher after the executor accepted the attempt, before the handle is
    stored (the intent row ``(run, attempt, nonce)`` exists)."""
    DUPLICATE_SUBMIT = "duplicate_submit"
    """Call ``submit`` twice with the same ``(run, attempt, nonce)``."""
    CANCEL_WHILE_RUNNING = "cancel_while_running"
    EXCEED_TIMEOUT = "exceed_timeout"
    EVENT_SILENCE = "event_silence"
    """Drop events for longer than the STALLED threshold while the execution keeps running."""
    INFRA_FAILURE_LIVE_EXECUTION = "infra_failure_live_execution"
    """Report an infrastructure failure while the old execution is still alive."""
    SIGTERM_WITH_CHECKPOINTS = "sigterm_with_checkpoints"
    MEMORY_BOMB = "memory_bomb"
    TAMPER_OUTPUT_GENERATION = "tamper_output_generation"
    """Overwrite an output object after the runner's manifest was signed."""
    BACKEND_429 = "backend_429"
    """The backend's control API answers HTTP 429 to the first submissions."""


class Requirement(StrEnum):
    """What a backend must support for a case to apply."""

    FANOUT = "fanout"
    """``ExecutorCaps.max_tasks > 1``."""
    CHECKPOINT_STORAGE = "checkpoint_storage"
    REMOTE_CONTROL_API = "remote_control_api"
    """A rate-limited remote API (Cloud Run Admin API, ...)."""
    MEMORY_LIMIT = "memory_limit"
    """Memory is capped per run, so a memory bomb is killed (exit 137) instead of swapping."""


class Expectation(StrictModel):
    """Constraints on the observed trajectory of the run under test."""

    final_state: RunState
    failure_class: FailureClass | None = None
    must_visit: tuple[RunState, ...] = ()
    must_not_visit: tuple[RunState, ...] = ()
    attempts: int | None = Field(None, ge=1, description="Exact number of attempts")
    max_live_executions: int = Field(1, ge=1, description="Never more at the same time")
    executions_created: int | None = Field(None, ge=1, description="Exact backend executions")
    offers_next_class: bool = False


class ConformanceCase(StrictModel):
    id: ConformanceCaseId
    title: str
    behaviour: str
    steps: tuple[str, ...]
    fault: Fault = Fault.NONE
    requires: frozenset[Requirement] = frozenset()
    expect: Expectation
    plan_refs: tuple[str, ...] = ()
    budget_seconds: int = Field(120, ge=1, description="Wall-clock budget of the case")


_C, _E = ConformanceCaseId, Expectation
_S = RunState

EXECUTOR_CONFORMANCE: Final[tuple[ConformanceCase, ...]] = (
    ConformanceCase(
        id=_C.SUBMIT,
        title="Submit runs one execution to success",
        behaviour="A submitted attempt starts with identifiers in the environment only, "
        "reports status through the executor API, emits verified events and succeeds.",
        steps=(
            "submit a tiny fit with a fresh nonce",
            "poll status until terminal; collect events",
            "assert argv carries no run id, nonce or credential",
        ),
        expect=_E(
            final_state=_S.SUCCEEDED,
            must_visit=(_S.PROVISIONING, _S.RUNNING, _S.FINALIZING),
            attempts=1,
            executions_created=1,
        ),
        plan_refs=("§8.1", "§8.3", "§8.4"),
    ),
    ConformanceCase(
        id=_C.ADOPT_AFTER_DISPATCHER_CRASH,
        title="Orphan adoption after a dispatcher crash",
        behaviour="If the dispatcher dies between the executor accepting an attempt and the "
        "handle being stored, the reconciler adopts the execution by nonce instead of "
        "starting a second one.",
        steps=(
            "write the intent row, submit, crash before storing the handle",
            "restart the orchestrator; reconciler calls adopt(nonce)",
            "the adopted execution finishes the run",
        ),
        fault=Fault.DISPATCHER_CRASH_AFTER_SUBMIT,
        expect=_E(final_state=_S.SUCCEEDED, attempts=1, executions_created=1),
        plan_refs=("§8.3",),
    ),
    ConformanceCase(
        id=_C.DUPLICATE_SUBMIT,
        title="Duplicate submit is safe",
        behaviour="Submitting the same (run, attempt, nonce) twice never yields two live "
        "executions; the second call returns or adopts the first, or its execution is "
        "cancelled before it does any work.",
        steps=("submit twice with the same nonce", "wait for the run to finish"),
        fault=Fault.DUPLICATE_SUBMIT,
        expect=_E(final_state=_S.SUCCEEDED, attempts=1, max_live_executions=1),
        plan_refs=("§8.3",),
    ),
    ConformanceCase(
        id=_C.CANCEL_MID_RUN,
        title="Cancel while running",
        behaviour="A cancel during RUNNING moves the run to CANCELLING; the supervisor gets "
        "SIGTERM and flushes; the run is CANCELLED only after the executor confirms the "
        "execution is gone.",
        steps=(
            "submit a long fit; wait for RUNNING",
            "cancel; poll executor status",
            "assert CANCELLED is recorded after the executor reports a terminal state",
        ),
        fault=Fault.CANCEL_WHILE_RUNNING,
        expect=_E(
            final_state=_S.CANCELLED,
            must_visit=(_S.RUNNING, _S.CANCELLING),
            must_not_visit=(_S.SUCCEEDED, _S.FAILED),
        ),
        plan_refs=("§8.3", "§8.7"),
    ),
    ConformanceCase(
        id=_C.TIMEOUT,
        title="Timeout is enforced and classified",
        behaviour="A run that exceeds its timeout is stopped by the backend (hard task "
        "timeout as backstop) and ends FAILED with failure class timeout.",
        steps=("submit a run that sleeps past a short timeout", "wait for the run to end"),
        fault=Fault.EXCEED_TIMEOUT,
        expect=_E(final_state=_S.FAILED, failure_class=FailureClass.TIMEOUT, attempts=1),
        plan_refs=("§8.3", "§8.7"),
    ),
    ConformanceCase(
        id=_C.STALLED_NOT_LOST,
        title="Event silence raises STALLED, never a false LOST",
        behaviour="When events stop but the executor still reports the execution running, "
        "the run becomes STALLED (a warning) and returns to RUNNING when events resume; it "
        "is never failed or retried because of silence alone.",
        steps=(
            "submit; drop events beyond K x interval while the execution runs",
            "resume events; let the run finish",
        ),
        fault=Fault.EVENT_SILENCE,
        expect=_E(
            final_state=_S.SUCCEEDED,
            must_visit=(_S.STALLED,),
            must_not_visit=(_S.FAILED,),
            attempts=1,
            executions_created=1,
        ),
        plan_refs=("§8.3", "Key decisions: liveness"),
    ),
    ConformanceCase(
        id=_C.CONFIRMED_CANCEL_BEFORE_RETRY,
        title="Confirmed cancel before an infrastructure retry",
        behaviour="After an infrastructure failure the reconciler cancels the old execution "
        "and waits for a confirmed terminal status before re-queueing attempt + 1, so two "
        "attempts never run at once.",
        steps=(
            "submit; report an infra failure while the old execution is alive",
            "observe cancel → confirmed terminal → QUEUED(attempt 2)",
            "attempt 2 succeeds",
        ),
        fault=Fault.INFRA_FAILURE_LIVE_EXECUTION,
        expect=_E(
            final_state=_S.SUCCEEDED,
            must_visit=(_S.QUEUED,),
            attempts=2,
            max_live_executions=1,
            executions_created=2,
        ),
        plan_refs=("§8.3",),
    ),
    ConformanceCase(
        id=_C.CHECKPOINT_RESUME,
        title="Checkpoint, PARTIAL and resume",
        behaviour="A SIGTERM after checkpoints were written yields a PARTIAL bundle; resume "
        "starts a new attempt from the checkpoints and completes without redoing them.",
        steps=(
            "submit a segmented fit; wait for the first checkpoint",
            "SIGTERM the execution; expect PARTIAL",
            "resume; expect SUCCEEDED with the earlier segments reused",
        ),
        fault=Fault.SIGTERM_WITH_CHECKPOINTS,
        requires=frozenset({Requirement.CHECKPOINT_STORAGE}),
        expect=_E(final_state=_S.SUCCEEDED, must_visit=(_S.PARTIAL,), attempts=2),
        plan_refs=("§8.3", "§5 segmented sampling"),
        budget_seconds=300,
    ),
    ConformanceCase(
        id=_C.OOM_CLASSIFICATION,
        title="Out-of-memory is classified as resource exhaustion",
        behaviour="A worker killed for memory (exit 137) ends FAILED with failure class "
        "resource_exhausted and an offer to retry one class up.",
        steps=("submit a memory bomb at class S", "wait for the run to end"),
        fault=Fault.MEMORY_BOMB,
        requires=frozenset({Requirement.MEMORY_LIMIT}),
        expect=_E(
            final_state=_S.FAILED,
            failure_class=FailureClass.RESOURCE_EXHAUSTED,
            attempts=1,
            offers_next_class=True,
        ),
        plan_refs=("§8.5",),
    ),
    ConformanceCase(
        id=_C.FANOUT_TASK_COUNT,
        title="Fan-out with taskCount",
        behaviour="One execution with task_count N runs N tasks, each mapping its task index "
        "to a child spec and writing under tasks/{i}/; status aggregates all tasks and a "
        "cancel stops all of them.",
        steps=(
            "submit task_count=4",
            "assert 4 distinct task indexes and per-task output prefixes",
            "assert one backend execution",
        ),
        requires=frozenset({Requirement.FANOUT}),
        expect=_E(final_state=_S.SUCCEEDED, attempts=1, executions_created=1),
        plan_refs=("§8.3", "§8.6"),
    ),
    ConformanceCase(
        id=_C.GENERATION_VERIFICATION,
        title="Output generations are verified at FINALIZING",
        behaviour="An output overwritten after the runner's manifest (new generation or "
        "hash) or an unexpected extra object fails the run with failure class integrity; "
        "nothing is copied into the bundles bucket.",
        steps=(
            "submit; after the complete event, overwrite one output object",
            "let the finalizer run",
        ),
        fault=Fault.TAMPER_OUTPUT_GENERATION,
        expect=_E(
            final_state=_S.FAILED,
            failure_class=FailureClass.INTEGRITY,
            must_visit=(_S.FINALIZING,),
            must_not_visit=(_S.SUCCEEDED,),
        ),
        plan_refs=("§8.6", "§9 artifact swap"),
    ),
    ConformanceCase(
        id=_C.RATE_LIMIT_BACKOFF,
        title="429 back-off without duplicates",
        behaviour="When the backend's control API answers 429, the dispatcher backs off "
        "(exponential, honouring Retry-After), raises the 429 alarm counter and submits "
        "exactly once when the limit clears.",
        steps=(
            "make the control API return 429 to the first 3 submit calls",
            "assert growing delays between calls",
            "assert one execution and a SUCCEEDED run",
        ),
        fault=Fault.BACKEND_429,
        requires=frozenset({Requirement.REMOTE_CONTROL_API}),
        expect=_E(final_state=_S.SUCCEEDED, attempts=1, executions_created=1),
        plan_refs=("§8.3", "§8.8"),
    ),
)
"""Every case, in suite order."""


def case(case_id: ConformanceCaseId | str) -> ConformanceCase:
    """Look a case up by id."""
    wanted = ConformanceCaseId(case_id)
    return next(c for c in EXECUTOR_CONFORMANCE if c.id is wanted)


class ConformanceTarget(StrictModel):
    """An executor under test: its declared caps plus facts the caps do not express."""

    executor: str = Field(..., pattern=r"^[a-z][a-z0-9-]{0,63}$")
    caps: ExecutorCaps
    remote_control_api: bool = False
    enforces_memory_limit: bool = False

    def supports(self, req: Requirement) -> bool:
        if req is Requirement.FANOUT:
            return self.caps.max_tasks > 1
        if req is Requirement.CHECKPOINT_STORAGE:
            return self.caps.checkpoint_storage
        if req is Requirement.REMOTE_CONTROL_API:
            return self.remote_control_api
        return self.enforces_memory_limit


def applicable_cases(target: ConformanceTarget) -> tuple[ConformanceCase, ...]:
    """The cases ``target`` must pass (cases whose requirements it supports)."""
    return tuple(c for c in EXECUTOR_CONFORMANCE if all(target.supports(r) for r in c.requires))


class ObservedRun(StrictModel):
    """What the harness saw while one case ran."""

    states: tuple[RunState, ...] = Field(..., min_length=1, description="In order visited")
    failure_class: FailureClass | None = None
    attempts: int = Field(..., ge=1)
    executions_created: int = Field(..., ge=0)
    max_live_executions: int = Field(..., ge=0)
    offered_next_class: bool = False

    @property
    def final_state(self) -> RunState:
        return self.states[-1]


def check_expectation(case: ConformanceCase, observed: ObservedRun) -> list[str]:
    """Every way ``observed`` violates ``case.expect`` (empty list: the case passed)."""
    e = case.expect
    problems: list[str] = []
    if observed.final_state is not e.final_state:
        problems.append(f"final state {observed.final_state.value}, expected {e.final_state.value}")
    if e.failure_class != observed.failure_class:
        want = e.failure_class.value if e.failure_class else None
        got = observed.failure_class.value if observed.failure_class else None
        problems.append(f"failure class {got}, expected {want}")
    problems += [f"never visited {s.value}" for s in e.must_visit if s not in observed.states]
    problems += [f"visited {s.value}" for s in e.must_not_visit if s in observed.states]
    if e.attempts is not None and observed.attempts != e.attempts:
        problems.append(f"{observed.attempts} attempts, expected {e.attempts}")
    if observed.max_live_executions > e.max_live_executions:
        problems.append(
            f"{observed.max_live_executions} executions live at once "
            f"(at most {e.max_live_executions})"
        )
    if e.executions_created is not None and observed.executions_created != e.executions_created:
        problems.append(
            f"{observed.executions_created} executions created, expected {e.executions_created}"
        )
    if e.offers_next_class and not observed.offered_next_class:
        problems.append("no offer to retry one resource class up")
    return problems


class CaseOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    """Not applicable to the target (its requirements are unsupported)."""
    ERROR = "error"
    """The harness itself failed; counts as not passed."""


class ConformanceResult(ResponseModel):
    case_id: ConformanceCaseId
    outcome: CaseOutcome
    duration_seconds: float | None = Field(None, ge=0)
    problems: list[str] = Field(default_factory=list)

    model_config = dto_examples(
        {"case_id": "stalled_not_lost", "outcome": "passed", "duration_seconds": 41.2}
    )


class ConformanceReport(ResponseModel):
    """One run of the suite against one executor."""

    executor: str
    suite_version: str = CONFORMANCE_SUITE_VERSION
    environment: Literal["pr", "nightly", "manual"] = "pr"
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    results: list[ConformanceResult] = Field(default_factory=list)

    model_config = dto_examples(
        {
            "executor": "cloudrun-jobs",
            "suite_version": "1",
            "environment": "nightly",
            "started_at": "2026-10-15T02:00:00Z",
            "finished_at": "2026-10-15T02:24:00Z",
            "results": [
                {"case_id": "submit", "outcome": "passed", "duration_seconds": 61.0},
                {"case_id": "rate_limit_backoff", "outcome": "passed", "duration_seconds": 33.5},
            ],
        }
    )

    def passed_for(self, target: ConformanceTarget) -> bool:
        """True if this report is for the current suite and every applicable case passed."""
        if self.suite_version != CONFORMANCE_SUITE_VERSION or self.executor != target.executor:
            return False
        outcomes = {r.case_id: r.outcome for r in self.results}
        return all(outcomes.get(c.id) is CaseOutcome.PASSED for c in applicable_cases(target))

    def missing(self, target: ConformanceTarget) -> list[ConformanceCaseId]:
        seen = {r.case_id for r in self.results}
        return [c.id for c in applicable_cases(target) if c.id not in seen]
