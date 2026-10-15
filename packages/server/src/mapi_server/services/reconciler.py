"""The reconciler (plan §8.3): a level-triggered loop that owns execution-time transitions.

Every ``reconcile_interval_s`` it looks at each run that may have an execution and compares
the database with the **executor's status API — the liveness source of truth**:

* ``DISPATCHING`` with an expired lease → adopt the orphan by nonce (``submitted``) or, if
  no execution exists, retry with a new attempt;
* ``PROVISIONING``/``RUNNING``/``STALLED`` → exit 0/16 + ``complete`` event → ``FINALIZING``;
  any other exit is classified with :func:`~mapi_spec.run.failure_class_for_exit`; infra
  failures retry (capped, and never after 30 min) once the old execution is confirmed gone;
  event silence only raises ``STALLED`` (a warning, never a failure); deadlines are enforced
  by cancelling the execution, recorded on the attempt so the stop is classified as
  ``TIMEOUT`` (or finalized as ``PARTIAL`` when checkpoints were flushed), never as an
  execution stopped outside the platform;
* ``CANCELLING`` → cancel at the executor, then ``cancel_confirmed`` once it is gone —
  unless the supervisor flushed its checkpoints and exited ``PARTIAL`` with a ``partial``
  complete event: that run is finalized (→ ``PARTIAL``, resumable);
* ``FINALIZING`` → make sure a finalize job is queued.

The first time this process looks at an attempt it calls ``executor.adopt(nonce)`` even
when a handle is stored: after an orchestrator restart that re-registers a local child's pid
and re-attaches a container's log follower (the event dedupe on ``(run, attempt, seq)``
makes the replay safe). An execution counts as lost only once a cancel has been requested
and the executor no longer reports it pending or running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.executor import ExecState, ExecStatus, Executor, Handle, RunEventType
from mapi_spec.resources import ResourceClass
from mapi_spec.run import (
    Actor,
    ErrorPayload,
    FailureClass,
    RunLimits,
    RunnerExit,
    RunState,
    RunTrigger,
    WarningPayload,
    failure_class_for_exit,
)
from mapi_spec.storage import bootstrap_key
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.observability import logging as obs_logging
from mapi_server.observability import metrics as obs_metrics
from mapi_server.services.runs import append_event, apply_transition, now

__all__ = ["Reconciler"]

log = logging.getLogger(__name__)

_ACTIVE = (
    RunState.DISPATCHING,
    RunState.PROVISIONING,
    RunState.RUNNING,
    RunState.STALLED,
    RunState.CANCELLING,
    RunState.FINALIZING,
)
_LIVE = frozenset({RunState.PROVISIONING, RunState.RUNNING, RunState.STALLED})
_FAIL_TRIGGERS = {
    FailureClass.USER: RunTrigger.FAIL_USER,
    FailureClass.RESOURCE_EXHAUSTED: RunTrigger.FAIL_RESOURCE_EXHAUSTED,
    FailureClass.INFRA: RunTrigger.FAIL_INFRA,
    FailureClass.TIMEOUT: RunTrigger.FAIL_TIMEOUT,
    FailureClass.SANDBOX: RunTrigger.FAIL_SANDBOX,
}


DEADLINE_STOP = "stopped by the platform at the run deadline"
"""``run_attempts.exec_message`` of an attempt the reconciler cancelled at its deadline."""


def _handle(att: m.RunAttempt) -> Handle | None:
    return Handle.model_validate(att.handle) if att.handle else None


class Reconciler:
    def __init__(self, ctx: ServerContext, executor: Executor) -> None:
        self.ctx = ctx
        self.executor = executor
        self._attached: set[tuple[str, int]] = set()
        """Attempts this process has re-attached to (``adopt``) since it started."""

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("reconciler started (every %ss)", self.ctx.settings.reconcile_interval_s)
        while not stop.is_set():
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.ctx.settings.reconcile_interval_s)

    async def reconcile_once(self) -> int:
        async with self.ctx.db.session() as session:
            rows = (
                await session.execute(
                    select(m.Run.id, m.Run.state)
                    .where(m.Run.state.in_([s.value for s in _ACTIVE]))
                    .order_by(m.Run.updated_at)
                )
            ).all()
        ids = [r[0] for r in rows]
        counts: dict[str, int] = {}
        for _rid, state in rows:
            counts[state] = counts.get(state, 0) + 1
        obs_metrics.set_active_runs(counts, states=[s.value for s in _ACTIVE])
        for run_id in ids:
            try:
                await self.reconcile_run(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconciling run %s failed", run_id)
        return len(ids)

    # ------------------------------------------------------------------ per run
    async def _load(self, run_id: str) -> tuple[m.Run, m.RunAttempt | None]:
        async with self.ctx.db.session() as session:
            run = await session.get(m.Run, run_id)
            assert run is not None
            att = await session.get(m.RunAttempt, (run_id, run.attempt))
            session.expunge_all()
        return run, att

    async def reconcile_run(self, run_id: str) -> None:
        run, att = await self._load(run_id)
        state = RunState(run.state)
        with obs_logging.bound_context(run_id=run_id, tenant_id=run.tenant_id, attempt=run.attempt):
            if state is RunState.CANCELLING:
                await self._cancelling(run, att)
            elif state is RunState.DISPATCHING:
                await self._dispatching(run, att)
            elif state in _LIVE and att is not None:
                await self._live(run, att)
            elif state is RunState.FINALIZING:
                await self._finalizing(run)

    async def _locked(self, session: AsyncSession, run_id: str) -> m.Run:
        return (
            await session.execute(select(m.Run).where(m.Run.id == run_id).with_for_update())
        ).scalar_one()

    async def _attach(self, att: m.RunAttempt) -> Handle | None:
        """The attempt's handle, re-attached through ``adopt`` once per process."""
        stored = _handle(att)
        key = (att.run_id, att.attempt)
        if key in self._attached:
            return stored or await self.executor.adopt(att.nonce)
        adopted = await self.executor.adopt(att.nonce)
        self._attached.add(key)
        return adopted or stored

    async def _delete_bootstrap(self, nonce: str) -> None:
        with contextlib.suppress(Exception):
            await self.ctx.store.delete(self.ctx.buckets.bootstrap, bootstrap_key(nonce))

    async def _transition(
        self,
        run: m.Run,
        trigger: RunTrigger,
        *,
        expect_state: RunState,
        problem: Problem | None = None,
        attempt_update: dict[str, object] | None = None,
        retry: bool = False,
    ) -> bool:
        """Apply ``trigger`` if the run is still in ``expect_state`` with the same attempt."""
        async with self.ctx.db.session() as session:
            row = await self._locked(session, run.id)
            if row.state != expect_state.value or row.attempt != run.attempt:
                return False
            if attempt_update:
                att = await session.get(m.RunAttempt, (run.id, run.attempt))
                if att is not None:
                    for k, v in attempt_update.items():
                        setattr(att, k, v)
            if retry:
                row.infra_retries += 1
            await apply_transition(session, row, trigger, Actor.RECONCILER, problem=problem)
        self._attached.discard((run.id, run.attempt))  # the attempt left this state
        return True

    async def _cancelling(self, run: m.Run, att: m.RunAttempt | None) -> None:
        handle: Handle | None = None
        if att is not None and att.status != "abandoned":
            handle = await self._attach(att)
            if handle is None and att.status == "intent":
                lease = run.lease_expires_at
                if lease is not None and lease > now():
                    return  # the dispatcher may still be submitting; look again next pass
        status: ExecStatus | None = None
        if handle is not None:
            await self.executor.cancel(handle)
            status = await self.executor.status(handle)
            if not (status.state.terminal or status.state is ExecState.UNKNOWN):
                return  # still stopping; the hard timeout is the backstop
            if (
                att is not None
                and status.exit_code == RunnerExit.PARTIAL
                and await self._cancelled_partial(run, att, status)
            ):
                return
        update: dict[str, object] = {}
        if att is not None:
            update = {"status": "finished" if handle else "abandoned", "finished_at": now()}
            if status is not None:
                update["exit_code"] = status.exit_code
                update["exec_state"] = status.state.value
            await self._delete_bootstrap(att.nonce)
        await self._transition(
            run,
            RunTrigger.CANCEL_CONFIRMED,
            expect_state=RunState.CANCELLING,
            attempt_update=update,
        )

    async def _dispatching(self, run: m.Run, att: m.RunAttempt | None) -> None:
        lease = run.lease_expires_at
        if lease is not None and lease > now():
            return
        if att is None:  # leased without an intent row cannot happen; recover anyway
            await self._retry_or_fail(run, RunState.DISPATCHING, "the dispatch was interrupted")
            return
        handle = _handle(att) or await self.executor.adopt(att.nonce)
        if handle is not None:
            log.info("adopted orphan execution of %s/%s by nonce", run.id, run.attempt)
            await self._transition(
                run,
                RunTrigger.SUBMITTED,
                expect_state=RunState.DISPATCHING,
                attempt_update={
                    "status": "submitted",
                    "handle": handle.model_dump(mode="json"),
                    "external_id": handle.external_id,
                    "submitted_at": att.submitted_at or now(),
                },
            )
            return
        await self._delete_bootstrap(att.nonce)
        await self._retry_or_fail(
            run,
            RunState.DISPATCHING,
            "the dispatcher stopped before the execution was submitted",
            attempt_update={"status": "abandoned", "finished_at": now()},
        )

    async def _cancelled_partial(self, run: m.Run, att: m.RunAttempt, status: ExecStatus) -> bool:
        """Finalize a cancelled run whose supervisor wrote a partial bundle (True = handled).

        Returns True while the ``partial`` complete event may still be on its way (within
        ``complete_event_grace_s`` of the exit) and once the run moved to ``FINALIZING``.
        """
        payload = att.complete_payload
        if payload is None and await self._complete_arrived(att):
            async with self.ctx.db.session() as session:
                fresh = await session.get(m.RunAttempt, (att.run_id, att.attempt))
                payload = fresh.complete_payload if fresh is not None else None
        if payload is None:
            finished = status.finished_at or now()
            waited = (now() - finished).total_seconds()
            return waited < self.ctx.settings.complete_event_grace_s
        if payload.get("status") != "partial":
            return False  # a finished fit the user cancelled anyway: plain cancellation
        await self._delete_bootstrap(att.nonce)
        moved = await self._transition(
            run,
            RunTrigger.COMPLETE,
            expect_state=RunState.CANCELLING,
            attempt_update={
                "status": "finished",
                "finished_at": att.finished_at or status.finished_at or now(),
                "exit_code": status.exit_code,
                "exec_state": status.state.value,
            },
        )
        if moved:
            await self._defer_finalize(run.id, run.attempt)
        return True

    def _can_retry(self, run: m.Run) -> bool:
        s = self.ctx.settings
        started = run.started_at or run.queued_at or run.created_at
        elapsed = (now() - started).total_seconds()
        return run.infra_retries < s.max_infra_retries and elapsed <= s.auto_retry_window_s

    async def _retry_or_fail(
        self,
        run: m.Run,
        state: RunState,
        detail: str,
        *,
        attempt_update: dict[str, object] | None = None,
    ) -> None:
        if self._can_retry(run):
            await self._transition(
                run,
                RunTrigger.INFRA_RETRY,
                expect_state=state,
                attempt_update=attempt_update,
                retry=True,
            )
        else:
            await self._transition(
                run,
                RunTrigger.FAIL_INFRA,
                expect_state=state,
                problem=Problem.from_code(ErrorCode.INFRA_ERROR, detail),
                attempt_update=attempt_update,
            )

    async def _live(self, run: m.Run, att: m.RunAttempt) -> None:
        state = RunState(run.state)
        handle = await self._attach(att)
        if handle is None:
            await self._lost(run, att, state, "the execution handle is missing", None)
            return
        status = await self.executor.status(handle)
        if status.state in (ExecState.PENDING, ExecState.RUNNING):
            await self._check_running(run, att, handle, state)
            return
        if status.state is ExecState.UNKNOWN:
            why = status.message or "the execution disappeared"
            await self._lost(run, att, state, why, handle)
            return
        await self._finished(run, att, state, status)

    async def _lost(
        self, run: m.Run, att: m.RunAttempt, state: RunState, why: str, handle: Handle | None
    ) -> None:
        since = att.submitted_at or att.created_at
        if (now() - since).total_seconds() < self.ctx.settings.lost_execution_grace_s:
            return
        if handle is not None:
            # confirm the old execution is gone before a new attempt can run next to it
            await self.executor.cancel(handle)
            after = await self.executor.status(handle)
            if after.state in (ExecState.PENDING, ExecState.RUNNING):
                log.warning("run %s: execution reported lost is still running", run.id)
                return
        await self._delete_bootstrap(att.nonce)
        await self._retry_or_fail(
            run,
            state,
            f"the execution was lost: {why}"[:2000],
            attempt_update={"status": "abandoned", "finished_at": now()},
        )

    async def _check_running(
        self, run: m.Run, att: m.RunAttempt, handle: Handle, state: RunState
    ) -> None:
        s = self.ctx.settings
        if now() > att.deadline + timedelta(seconds=s.deadline_grace_s):
            if att.exec_message != DEADLINE_STOP:
                log.warning("run %s passed its deadline; cancelling the execution", run.id)
                async with self.ctx.db.session() as session:
                    await session.execute(
                        update(m.RunAttempt)
                        .where(m.RunAttempt.run_id == run.id, m.RunAttempt.attempt == att.attempt)
                        .values(exec_message=DEADLINE_STOP)
                    )
            await self.executor.cancel(handle)  # SIGTERM → partial flush; see _finished
            return
        if state is not RunState.RUNNING:
            return
        interval = RunLimits.model_fields["progress_interval_s"].default
        silence = timedelta(seconds=s.stall_intervals * float(interval))
        last: datetime | None = run.last_event_at or run.started_at
        if last is None or now() - last <= silence:
            return
        async with self.ctx.db.session() as session:
            row = await self._locked(session, run.id)
            if row.state != RunState.RUNNING.value or row.attempt != run.attempt:
                return
            await apply_transition(session, row, RunTrigger.SILENCE, Actor.RECONCILER)
            warning = WarningPayload(
                code="STALLED",
                message=(
                    f"no progress for more than {int(silence.total_seconds())} s; the executor "
                    "still reports the execution as running"
                ),
            )
            await append_event(
                session, row, type=RunEventType.WARNING, payload=warning.model_dump(mode="json")
            )

    async def _finished(
        self, run: m.Run, att: m.RunAttempt, state: RunState, status: ExecStatus
    ) -> None:
        code = status.exit_code
        finished = status.finished_at or now()
        # the platform itself stopped it at the deadline: not "stopped outside the platform"
        deadline_stop = att.exec_message == DEADLINE_STOP
        stopped_outside = status.state is ExecState.CANCELLED and not deadline_stop
        base_update: dict[str, object] = {
            "exit_code": code,
            "exec_state": status.state.value,
            "exec_message": DEADLINE_STOP if deadline_stop else (status.message or None),
            "status": "finished",
            "finished_at": att.finished_at or finished,
        }
        await self._delete_bootstrap(att.nonce)
        if code in (RunnerExit.OK, RunnerExit.PARTIAL) and not stopped_outside:
            waited = (now() - finished).total_seconds()
            grace_over = waited >= self.ctx.settings.complete_event_grace_s
            # before giving up, re-read the row: the event may have been ingested after this
            # pass loaded the attempt (the executor reports the exit only once drained)
            if att.complete_payload is not None or (
                grace_over and await self._complete_arrived(att)
            ):
                moved = await self._transition(
                    run, RunTrigger.COMPLETE, expect_state=state, attempt_update=base_update
                )
                if moved:
                    await self._defer_finalize(run.id, run.attempt)
                return
            if not grace_over:
                return  # events can lag the exit (log pipeline); look again next pass
            await self._transition(
                run,
                RunTrigger.FAIL_INFRA,
                expect_state=state,
                attempt_update=base_update,
                problem=Problem.from_code(
                    ErrorCode.INFRA_ERROR,
                    f"the supervisor exited with {code} but no 'complete' event arrived",
                ),
            )
            return
        if deadline_stop:
            payload = self._error_payload(att)
            detail = f"the run passed its {run.timeout_s} s deadline and was stopped"
            if payload is not None:
                detail = f"{detail}: {payload.message}"
            await self._transition(
                run,
                RunTrigger.FAIL_TIMEOUT,
                expect_state=state,
                attempt_update=base_update,
                problem=Problem.from_code(
                    ErrorCode.TIMEOUT,
                    detail[:2000],
                    untrusted_text=payload.untrusted_text if payload else None,
                ),
            )
            return
        if status.state is ExecState.CANCELLED or code is None:
            await self._retry_or_fail(
                run,
                state,
                "the execution was stopped outside the platform",
                attempt_update=base_update,
            )
            return
        fc = failure_class_for_exit(code)
        if fc is FailureClass.INFRA:
            await self._retry_or_fail(
                run,
                state,
                f"infrastructure failure (supervisor exit {code})",
                attempt_update=base_update,
            )
            return
        problem = self._problem(run, att, code, fc)
        trigger = _FAIL_TRIGGERS.get(fc or FailureClass.INFRA, RunTrigger.FAIL_INFRA)
        await self._transition(
            run, trigger, expect_state=state, attempt_update=base_update, problem=problem
        )

    async def _complete_arrived(self, att: m.RunAttempt) -> bool:
        async with self.ctx.db.session() as session:
            payload = (
                await session.execute(
                    select(m.RunAttempt.complete_payload).where(
                        m.RunAttempt.run_id == att.run_id, m.RunAttempt.attempt == att.attempt
                    )
                )
            ).scalar_one_or_none()
        return payload is not None

    @staticmethod
    def _error_payload(att: m.RunAttempt) -> ErrorPayload | None:
        if att.error_payload:
            with contextlib.suppress(ValidationError):
                return ErrorPayload.model_validate(att.error_payload)
        return None

    def _problem(
        self, run: m.Run, att: m.RunAttempt, code: int, fc: FailureClass | None
    ) -> Problem:
        payload = self._error_payload(att)
        if fc is FailureClass.INTEGRITY:
            # an input did not match its size/crc32c: the transition table has no
            # live → fail_integrity edge, so the run fails as infra with the integrity code
            return Problem.from_code(
                ErrorCode.INTEGRITY_FAILURE,
                "an input object did not match its expected size or CRC32C",
                untrusted_text=payload.untrusted_text if payload else None,
            )
        error_code = (fc or FailureClass.INFRA).error_code
        detail = payload.message if payload else f"the run failed (supervisor exit {code})"
        if fc is FailureClass.RESOURCE_EXHAUSTED:
            up = ResourceClass(run.resource_class).next_up()
            if up is not None:
                detail = f"{detail}; retry at class {up.value}"
        return Problem.from_code(
            error_code, detail[:2000], untrusted_text=payload.untrusted_text if payload else None
        )

    async def _finalizing(self, run: m.Run) -> None:
        await self._defer_finalize(run.id, run.attempt)

    async def _defer_finalize(self, run_id: str, attempt: int) -> None:
        queue = self.ctx.queue
        if queue is None:
            raise RuntimeError("no finalize queue is configured")
        await queue.defer_finalize(run_id, attempt)
