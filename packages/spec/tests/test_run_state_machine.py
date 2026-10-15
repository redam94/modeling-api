"""The run state machine (plan §8.3), checked exhaustively against an independent table."""

from __future__ import annotations

import itertools
from collections import deque

import pytest
from mapi_spec.errors import ErrorCode
from mapi_spec.run import (
    TRANSITIONS,
    Actor,
    FailureClass,
    IllegalTransition,
    RunState,
    RunTrigger,
    allowed_triggers,
    can_transition,
    transition,
)

S, T = RunState, RunTrigger

LIVE = (S.PROVISIONING, S.RUNNING, S.STALLED)
EXEC_FAILS = {
    T.FAIL_USER: FailureClass.USER,
    T.FAIL_RESOURCE_EXHAUSTED: FailureClass.RESOURCE_EXHAUSTED,
    T.FAIL_INFRA: FailureClass.INFRA,
    T.FAIL_TIMEOUT: FailureClass.TIMEOUT,
    T.FAIL_SANDBOX: FailureClass.SANDBOX,
}

# (state, trigger) -> target.  Written out by hand from the plan, not derived from the code.
EXPECTED: dict[tuple[RunState, RunTrigger], RunState] = {
    (S.CREATED, T.ACCEPT): S.QUEUED,
    (S.CREATED, T.REJECT): S.REJECTED,
    (S.CREATED, T.CANCEL): S.CANCELLING,
    (S.QUEUED, T.LEASE): S.DISPATCHING,
    (S.QUEUED, T.CANCEL): S.CANCELLING,
    (S.DISPATCHING, T.SUBMITTED): S.PROVISIONING,
    (S.DISPATCHING, T.INFRA_RETRY): S.QUEUED,
    (S.DISPATCHING, T.FAIL_INFRA): S.FAILED,
    (S.DISPATCHING, T.CANCEL): S.CANCELLING,
    (S.RUNNING, T.SILENCE): S.STALLED,
    (S.STALLED, T.SILENCE): S.STALLED,
    (S.FINALIZING, T.EVENT): S.FINALIZING,
    (S.FINALIZING, T.VERIFIED): S.SUCCEEDED,
    (S.FINALIZING, T.VERIFIED_PARTIAL): S.PARTIAL,
    (S.FINALIZING, T.FAIL_INTEGRITY): S.FAILED,
    (S.FINALIZING, T.FAIL_INFRA): S.FAILED,
    (S.FINALIZING, T.CANCEL): S.CANCELLING,
    (S.CANCELLING, T.CANCEL): S.CANCELLING,
    (S.CANCELLING, T.EVENT): S.CANCELLING,
    (S.CANCELLING, T.CANCEL_CONFIRMED): S.CANCELLED,
    (S.PARTIAL, T.RESUME): S.QUEUED,
}
for _live in LIVE:
    EXPECTED[(_live, T.EVENT)] = S.RUNNING
    EXPECTED[(_live, T.COMPLETE)] = S.FINALIZING
    EXPECTED[(_live, T.INFRA_RETRY)] = S.QUEUED
    EXPECTED[(_live, T.CANCEL)] = S.CANCELLING
    for _trig in EXEC_FAILS:
        EXPECTED[(_live, _trig)] = S.FAILED

ALL_PAIRS = list(itertools.product(RunState, RunTrigger))


def test_table_matches_the_independent_specification() -> None:
    assert {k: r.target for k, r in TRANSITIONS.items()} == EXPECTED


@pytest.mark.parametrize(("state", "trigger"), ALL_PAIRS, ids=lambda v: v.value)
def test_every_pair_is_allowed_or_illegal(state: RunState, trigger: RunTrigger) -> None:
    if (state, trigger) in EXPECTED:
        rule = transition(state, trigger)
        assert rule.target is EXPECTED[(state, trigger)]
        assert rule.source is state and rule.trigger is trigger
        assert can_transition(state.value, trigger.value)
        assert trigger in allowed_triggers(state)
    else:
        with pytest.raises(IllegalTransition) as info:
            transition(state, trigger)
        assert info.value.code is ErrorCode.CONFLICT
        assert info.value.state is state and info.value.trigger is trigger
        assert not can_transition(state, trigger)
        assert trigger not in allowed_triggers(state)


def test_failure_class_is_set_exactly_on_failures() -> None:
    for (src, trig), rule in TRANSITIONS.items():
        if rule.target is S.FAILED:
            assert rule.failure_class is not None
            assert trig.value == f"fail_{rule.failure_class.value}"
        else:
            assert rule.failure_class is None, (src, trig)
    # integrity failures only come from the finalizer
    integrity = [r for r in TRANSITIONS.values() if r.failure_class is FailureClass.INTEGRITY]
    assert [r.source for r in integrity] == [S.FINALIZING]
    assert all(r.actors == {Actor.FINALIZER} for r in integrity)


def test_new_attempt_only_when_requeued_after_an_execution() -> None:
    for rule in TRANSITIONS.values():
        assert rule.new_attempt is (rule.target is S.QUEUED and rule.source is not S.CREATED)


def test_terminal_states_are_final_except_partial_resume() -> None:
    terminal = {s for s in RunState if s.terminal}
    assert terminal == {S.REJECTED, S.SUCCEEDED, S.PARTIAL, S.FAILED, S.CANCELLED}
    for state in terminal:
        expected = {T.RESUME} if state is S.PARTIAL else set()
        assert allowed_triggers(state) == expected
    assert S.PARTIAL.resumable and not S.FAILED.resumable


def test_every_non_terminal_state_can_be_cancelled() -> None:
    for state in RunState:
        if not state.terminal:
            assert transition(state, T.CANCEL).target is S.CANCELLING


def test_every_state_is_reachable_from_created() -> None:
    seen, todo = {S.CREATED}, deque([S.CREATED])
    while todo:
        state = todo.popleft()
        for trig in allowed_triggers(state):
            nxt = transition(state, trig).target
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    assert seen == set(RunState)


def test_noop_rules_are_idempotent_redeliveries() -> None:
    noops = {(r.source, r.trigger) for r in TRANSITIONS.values() if r.noop}
    assert noops == {
        (S.RUNNING, T.EVENT),
        (S.STALLED, T.SILENCE),
        (S.FINALIZING, T.EVENT),
        (S.CANCELLING, T.CANCEL),
        (S.CANCELLING, T.EVENT),
    }


def test_actor_ownership_is_enforced() -> None:
    assert transition(S.CREATED, T.ACCEPT, actor=Actor.API).target is S.QUEUED
    with pytest.raises(IllegalTransition):
        transition(S.CREATED, T.ACCEPT, actor=Actor.RECONCILER)
    # events move runs to RUNNING; everything else after dispatch is the reconciler's
    assert transition(S.PROVISIONING, T.EVENT, actor="events").target is S.RUNNING
    with pytest.raises(IllegalTransition):
        transition(S.RUNNING, T.COMPLETE, actor=Actor.EVENTS)
    # orphan adoption: the reconciler may confirm a dispatch the crashed dispatcher started
    assert transition(S.DISPATCHING, T.SUBMITTED, actor=Actor.RECONCILER).target is S.PROVISIONING
    owners = {a for r in TRANSITIONS.values() for a in r.actors}
    assert owners == set(Actor)


def test_has_execution_flags() -> None:
    assert {s for s in RunState if s.has_execution} == {
        S.DISPATCHING,
        S.PROVISIONING,
        S.RUNNING,
        S.STALLED,
        S.CANCELLING,
    }


def test_failure_class_error_codes_round_trip() -> None:
    for fc in FailureClass:
        assert FailureClass.from_error_code(fc.error_code) is fc
        assert FailureClass.from_error_code(fc.error_code.value) is fc
    assert FailureClass.from_error_code(ErrorCode.NOT_FOUND) is FailureClass.INFRA


def test_unknown_values_are_rejected() -> None:
    with pytest.raises(ValueError):
        transition("bogus", T.ACCEPT)
    with pytest.raises(ValueError):
        transition(S.CREATED, "bogus")
