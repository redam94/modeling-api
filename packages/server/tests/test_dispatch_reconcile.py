"""Dispatcher and reconciler against a scriptable executor (no subprocesses)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher, TokenBucket
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.queue import InlineFinalizeQueue
from mapi_server.services.reconciler import Reconciler
from mapi_server.services.runs import apply_transition, load_run, now
from mapi_server_testkit.builders import queued_fit
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.executor import ExecState, ExecStatus, Handle, RunSubmit
from mapi_spec.run import (
    Actor,
    ErrorPayload,
    FailureClass,
    RunSpec,
    RunState,
    RunTrigger,
)
from mapi_spec.storage import NotFound, PostPolicy, bootstrap_key
from sqlalchemy import select, update


async def _queued_fit(ctx: ServerContext) -> str:
    return await queued_fit(ctx)


async def _state(ctx: ServerContext, run_id: str) -> m.Run:
    async with ctx.db.session() as s:
        return await load_run(s, None, run_id)


async def _attempt(ctx: ServerContext, run_id: str, attempt: int = 1) -> m.RunAttempt:
    async with ctx.db.session() as s:
        row = await s.get(m.RunAttempt, (run_id, attempt))
        assert row is not None
        return row


@pytest.fixture
def executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture
def wired(ctx: ServerContext, executor: FakeExecutor) -> tuple[Dispatcher, Reconciler]:
    ctx.queue = InlineFinalizeQueue(Finalizer(ctx))
    return Dispatcher(ctx, executor), Reconciler(ctx, executor)


async def test_token_bucket() -> None:
    b = TokenBucket(rate_per_s=1000.0, burst=2)
    assert b.try_acquire() and b.try_acquire() and not b.try_acquire()
    await b.acquire()  # refills quickly at 1000/s
    with pytest.raises(ValueError):
        TokenBucket(0, 1)


async def test_dispatch_writes_intent_row_and_bootstrap_before_submit(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, _ = wired
    run_id = await _queued_fit(ctx)
    seen: dict[str, object] = {}

    def on_submit(rs: RunSubmit, nonce: str) -> None:
        seen["rs"] = rs
        seen["nonce"] = nonce
        seen["bootstrap"] = ctx.store.snapshot().get((ctx.buckets.bootstrap, nonce))  # type: ignore[attr-defined]

    executor.on_submit = on_submit
    assert await dispatcher.dispatch_once() == 1
    nonce = str(seen["nonce"])
    assert seen["bootstrap"] is not None, "the RunSpec must exist before the executor call"
    rs = seen["rs"]
    assert isinstance(rs, RunSubmit) and rs.payload == {}  # nothing secret travels in submit
    att = await _attempt(ctx, run_id)
    assert att.nonce == nonce and att.status == "submitted" and att.handle is not None
    run = await _state(ctx, run_id)
    assert run.state == RunState.PROVISIONING.value and run.executor == "fake"
    spec = RunSpec.model_validate_json(
        await ctx.store.get(ctx.buckets.bootstrap, bootstrap_key(nonce))
    )
    assert spec.run_id == run_id and spec.nonce == nonce and spec.hmac_key == att.hmac_key
    assert set(spec.inputs) == {"dataset"} and set(spec.outputs) == {"manifest", "artifacts"}
    assert spec.outputs["manifest"].name == f"t/default/runs/{run_id}/a1/manifest.json"  # type: ignore[union-attr]
    artifacts = spec.outputs["artifacts"]
    assert isinstance(artifacts, PostPolicy) and artifacts.create_only  # no overwrites
    assert artifacts.fields["x-goog-if-generation-match"] == "0"
    assert spec.spec["model"]["use"] == "ts.hsgp_trend@1.0.0"  # type: ignore[index]
    assert await dispatcher.dispatch_once() == 0  # nothing left to lease


async def test_submit_failure_retries_with_a_new_attempt_then_fails(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, _ = wired
    ctx.settings.max_infra_retries = 1
    executor.fail_submit = RuntimeError("quota")
    run_id = await _queued_fit(ctx)
    await dispatcher.dispatch_once()
    run = await _state(ctx, run_id)
    assert run.state == RunState.QUEUED.value and run.attempt == 2 and run.infra_retries == 1
    first = await _attempt(ctx, run_id, 1)
    assert first.status == "abandoned"
    with pytest.raises(NotFound):  # the bootstrap object was removed
        await ctx.store.stat(ctx.buckets.bootstrap, first.nonce)
    await dispatcher.dispatch_once()
    run = await _state(ctx, run_id)
    assert run.state == RunState.FAILED.value and run.failure_class == "infra"
    assert run.problem is not None and run.problem["code"] == "INFRA_ERROR"


async def test_failed_submit_that_did_start_is_adopted(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, _ = wired
    run_id = await _queued_fit(ctx)

    def started_then_timeout(rs: RunSubmit, nonce: str) -> None:
        executor.orphans[nonce] = Handle(
            executor="fake", run_id=rs.run_id, attempt=rs.attempt, nonce=nonce, external_id="x"
        )

    executor.on_submit = started_then_timeout
    executor.fail_submit = TimeoutError("lost the response")
    await dispatcher.dispatch_once()
    run = await _state(ctx, run_id)
    assert run.state == RunState.PROVISIONING.value and run.attempt == 1


async def test_orphan_adoption_after_a_dispatcher_crash(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, reconciler = wired
    run_id = await _queued_fit(ctx)
    leased = await dispatcher.lease()  # crash before submit: only the intent row exists
    assert leased == [(run_id, 1)]
    att = await _attempt(ctx, run_id)
    assert att.status == "intent"
    await reconciler.reconcile_run(run_id)  # lease still valid → leave it alone
    assert (await _state(ctx, run_id)).state == RunState.DISPATCHING.value
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Run).where(m.Run.id == run_id).values(lease_expires_at=now() - timedelta(1))
        )
    executor.orphans[att.nonce] = Handle(
        executor="fake", run_id=run_id, attempt=1, nonce=att.nonce, external_id="orphan-1"
    )
    await reconciler.reconcile_run(run_id)
    run = await _state(ctx, run_id)
    assert run.state == RunState.PROVISIONING.value
    assert (await _attempt(ctx, run_id)).external_id == "orphan-1"


async def test_crash_without_execution_retries(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, reconciler = wired
    run_id = await _queued_fit(ctx)
    await dispatcher.lease()
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Run).where(m.Run.id == run_id).values(lease_expires_at=now() - timedelta(1))
        )
    await reconciler.reconcile_run(run_id)
    run = await _state(ctx, run_id)
    assert run.state == RunState.QUEUED.value and run.attempt == 2


async def _provisioned(ctx: ServerContext, dispatcher: Dispatcher) -> tuple[str, m.RunAttempt]:
    run_id = await _queued_fit(ctx)
    await dispatcher.dispatch_once()
    return run_id, await _attempt(ctx, run_id)


@pytest.mark.parametrize(
    ("code", "failure", "fragment"),
    [
        (10, "user", "plugin raised"),
        (137, "resource_exhausted", "retry at class M"),
        (14, "timeout", "exit 14"),
        (15, "sandbox", "exit 15"),
    ],
)
async def test_exit_codes_are_classified(
    ctx: ServerContext,
    executor: FakeExecutor,
    wired: tuple[Dispatcher, Reconciler],
    code: int,
    failure: str,
    fragment: str,
) -> None:
    dispatcher, reconciler = wired
    run_id, att = await _provisioned(ctx, dispatcher)
    if code == 10:
        async with ctx.db.session() as s:
            row = await s.get(m.RunAttempt, (run_id, 1))
            assert row is not None
            row.error_payload = ErrorPayload(
                code="USER_CODE_ERROR",  # type: ignore[arg-type]
                failure_class=FailureClass.USER,
                message="the model plugin raised",
                untrusted_text="Traceback ...",
            ).model_dump(mode="json")
    executor.finish(att.nonce, code)
    await reconciler.reconcile_run(run_id)
    run = await _state(ctx, run_id)
    assert run.state == RunState.FAILED.value and run.failure_class == failure
    assert run.problem is not None and fragment in run.problem["detail"]
    if code == 10:
        assert run.problem["untrusted_text"] == "Traceback ..."
        assert "Traceback" not in run.problem["detail"]
    finished = await _attempt(ctx, run_id)
    assert finished.exit_code == code and finished.status == "finished"


async def test_infra_exit_retries_until_the_cap(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, reconciler = wired
    ctx.settings.max_infra_retries = 1
    run_id, att = await _provisioned(ctx, dispatcher)
    executor.finish(att.nonce, 12)
    await reconciler.reconcile_run(run_id)
    run = await _state(ctx, run_id)
    assert run.state == RunState.QUEUED.value and run.attempt == 2
    await dispatcher.dispatch_once()
    att2 = await _attempt(ctx, run_id, 2)
    assert att2.nonce != att.nonce
    executor.finish(att2.nonce, 1)  # unknown codes are infra
    await reconciler.reconcile_run(run_id)
    run = await _state(ctx, run_id)
    assert run.state == RunState.FAILED.value and run.failure_class == "infra"


async def test_no_auto_retry_after_the_window(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, reconciler = wired
    run_id, att = await _provisioned(ctx, dispatcher)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Run).where(m.Run.id == run_id).values(started_at=now() - timedelta(hours=1))
        )
    executor.finish(att.nonce, 12)
    await reconciler.reconcile_run(run_id)
    assert (await _state(ctx, run_id)).state == RunState.FAILED.value


async def test_exit_zero_needs_the_complete_event(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, reconciler = wired
    ctx.settings.complete_event_grace_s = 3600
    run_id, att = await _provisioned(ctx, dispatcher)
    executor.finish(att.nonce, 0)
    await reconciler.reconcile_run(run_id)
    assert (await _state(ctx, run_id)).state == RunState.PROVISIONING.value  # waits for events
    ctx.settings.complete_event_grace_s = 0
    await reconciler.reconcile_run(run_id)
    run = await _state(ctx, run_id)
    assert run.state == RunState.FAILED.value and "no 'complete' event" in run.problem["detail"]  # type: ignore[index]


async def test_silence_only_stalls_and_events_revive(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, reconciler = wired
    run_id, _att = await _provisioned(ctx, dispatcher)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        run.last_event_at = now() - timedelta(hours=1)
    await reconciler.reconcile_run(run_id)
    run = await _state(ctx, run_id)
    assert run.state == RunState.STALLED.value  # a warning, never a failure
    async with ctx.db.session() as s:
        events = list(
            (await s.execute(select(m.RunEvent).where(m.RunEvent.run_id == run_id))).scalars()
        )
    assert [(e.type, e.payload["code"], e.source) for e in events] == [
        ("warning", "STALLED", "platform")
    ]
    await reconciler.reconcile_run(run_id)  # still stalled, no duplicate transition
    assert (await _state(ctx, run_id)).state == RunState.STALLED.value


async def test_deadline_cancels_the_execution(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, reconciler = wired
    ctx.settings.deadline_grace_s = 0
    run_id, _att = await _provisioned(ctx, dispatcher)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.RunAttempt)
            .where(m.RunAttempt.run_id == run_id)
            .values(deadline=now() - timedelta(minutes=1))
        )
    await reconciler.reconcile_run(run_id)
    assert executor.only().cancelled == 1


async def test_cancel_flow_confirms_after_the_executor_stops(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, reconciler = wired
    run_id, _att = await _provisioned(ctx, dispatcher)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.CANCEL, Actor.API)
    await reconciler.reconcile_run(run_id)
    run = await _state(ctx, run_id)
    assert run.state == RunState.CANCELLED.value
    assert executor.only().cancelled == 1


async def test_cancel_of_a_queued_run_is_confirmed_without_an_execution(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    _, reconciler = wired
    run_id = await _queued_fit(ctx)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.CANCEL, Actor.API)
    assert await reconciler.reconcile_once() == 1
    assert (await _state(ctx, run_id)).state == RunState.CANCELLED.value
    assert executor.submit_calls == 0


async def test_lost_execution_is_retried(
    ctx: ServerContext, executor: FakeExecutor, wired: tuple[Dispatcher, Reconciler]
) -> None:
    dispatcher, reconciler = wired
    run_id, att = await _provisioned(ctx, dispatcher)
    executor.executions[att.nonce].status = ExecStatus(state=ExecState.UNKNOWN)
    await reconciler.reconcile_run(run_id)
    run = await _state(ctx, run_id)
    assert run.state == RunState.QUEUED.value and run.attempt == 2
