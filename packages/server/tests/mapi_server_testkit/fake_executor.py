"""A scriptable in-memory :class:`~mapi_spec.executor.Executor` (tests only)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from mapi_spec.executor import (
    ExecState,
    ExecStatus,
    ExecutorCaps,
    Handle,
    Isolation,
    RunEvent,
    RunSubmit,
)
from mapi_spec.resources import ResourceClass


@dataclass
class FakeExecution:
    submit: RunSubmit
    handle: Handle
    status: ExecStatus
    cancelled: int = 0


@dataclass
class FakeExecutor:
    """Records submissions; tests set statuses and push events by hand."""

    name: str = "fake"
    isolation: Isolation = Isolation.PROCESS
    fail_submit: Exception | None = None
    on_submit: Callable[[RunSubmit, str], None] | None = None
    executions: dict[str, FakeExecution] = field(default_factory=dict)
    orphans: dict[str, Handle] = field(default_factory=dict)
    events: asyncio.Queue[RunEvent] = field(default_factory=asyncio.Queue)
    submit_calls: int = 0

    def caps(self) -> ExecutorCaps:
        return ExecutorCaps(
            isolation=self.isolation,
            classes=[ResourceClass.S, ResourceClass.M, ResourceClass.L],
        )

    async def submit(self, rs: RunSubmit, nonce: str) -> Handle:
        self.submit_calls += 1
        if self.on_submit is not None:
            self.on_submit(rs, nonce)
        if self.fail_submit is not None:
            raise self.fail_submit
        if nonce in self.executions:
            return self.executions[nonce].handle
        handle = Handle(
            executor=self.name,
            run_id=rs.run_id,
            attempt=rs.attempt,
            nonce=nonce,
            external_id=f"fake-{len(self.executions)}",
        )
        self.executions[nonce] = FakeExecution(
            rs, handle, ExecStatus(state=ExecState.RUNNING, started_at=datetime.now(UTC))
        )
        return handle

    async def adopt(self, nonce: str) -> Handle | None:
        if nonce in self.orphans:
            return self.orphans[nonce]
        ex = self.executions.get(nonce)
        return ex.handle if ex else None

    async def status(self, h: Handle) -> ExecStatus:
        ex = self.executions.get(h.nonce)
        if ex is None:
            return ExecStatus(state=ExecState.UNKNOWN, message="unknown")
        return ex.status

    async def cancel(self, h: Handle) -> None:
        ex = self.executions.get(h.nonce)
        if ex is not None:
            ex.cancelled += 1
            ex.status = ExecStatus(
                state=ExecState.CANCELLED, exit_code=143, finished_at=datetime.now(UTC)
            )

    def event_source(self) -> AsyncIterator[RunEvent]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[RunEvent]:
        while True:
            yield await self.events.get()

    # ------------------------------------------------------------------ helpers
    def finish(self, nonce: str, exit_code: int) -> None:
        state = ExecState.SUCCEEDED if exit_code == 0 else ExecState.FAILED
        self.executions[nonce].status = ExecStatus(
            state=state, exit_code=exit_code, finished_at=datetime.now(UTC)
        )

    def only(self) -> FakeExecution:
        assert len(self.executions) == 1, list(self.executions)
        return next(iter(self.executions.values()))
