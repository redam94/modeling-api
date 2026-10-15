"""The dispatcher (plan §8.3): lease QUEUED runs and hand them to the executor.

Per run, in order — each step durable before the next:

1. **lease**: ``QUEUED → DISPATCHING`` under ``FOR UPDATE SKIP LOCKED`` (many dispatchers
   may run), and write the **intent row** ``(run, attempt, nonce, hmac_key)``; commit;
2. write the :class:`~mapi_spec.run.RunSpec` to ``bootstrap/<nonce>`` (write-once);
3. ``executor.submit(RunSubmit, nonce)`` — rate-limited by a token bucket;
4. record the handle and apply ``submitted`` (``DISPATCHING → PROVISIONING``).

A crash between 1 and 4 leaves an intent row that the reconciler resolves by nonce
(``executor.adopt``). A failed submit is checked with ``adopt`` too, so an execution that
did start is never duplicated.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from datetime import timedelta

from mapi_spec._types import Duration
from mapi_spec.errors import ErrorCode, MapiError, Problem
from mapi_spec.executor import Executor, Handle, RunKind, RunSubmit
from mapi_spec.resources import ResourceClass
from mapi_spec.run import Actor, RunState, RunTrigger, new_hmac_key, new_nonce
from mapi_spec.storage import NotFound, bootstrap_key
from sqlalchemy import select

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.executors import check_placement
from mapi_server.services.runs import apply_transition, now
from mapi_server.services.runspec import build_runspec

__all__ = ["Dispatcher", "TokenBucket"]

log = logging.getLogger(__name__)


class TokenBucket:
    """Classic token bucket: ``rate`` tokens per second, at most ``burst`` saved up."""

    def __init__(self, rate_per_s: float, burst: int) -> None:
        if rate_per_s <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self.rate = rate_per_s
        self.burst = float(burst)
        self._tokens = float(burst)
        self._stamp = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        t = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (t - self._stamp) * self.rate)
        self._stamp = t

    def try_acquire(self) -> bool:
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    async def acquire(self) -> None:
        async with self._lock:
            while not self.try_acquire():
                await asyncio.sleep((1 - self._tokens) / self.rate)


class Dispatcher:
    def __init__(self, ctx: ServerContext, executor: Executor, *, name: str | None = None) -> None:
        self.ctx = ctx
        self.executor = executor
        self.name = name or f"dispatcher-{socket.gethostname()}-{os.getpid()}"
        s = ctx.settings
        self.bucket = TokenBucket(s.dispatch_rate_per_minute / 60.0, s.dispatch_burst)

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("dispatcher %s started (executor %s)", self.name, self.executor.name)
        while not stop.is_set():
            try:
                n = await self.dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dispatch pass failed")
                n = 0
            if n == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.ctx.settings.dispatch_poll_s)

    async def lease(self) -> list[tuple[str, int]]:
        """Lease up to ``dispatch_batch`` queued runs and write their intent rows."""
        s = self.ctx.settings
        leased: list[tuple[str, int]] = []
        async with self.ctx.db.session() as session:
            rows = (
                await session.execute(
                    select(m.Run)
                    .where(m.Run.state == RunState.QUEUED.value)
                    .order_by(m.Run.queued_at, m.Run.id)
                    .limit(s.dispatch_batch)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
            for run in rows:
                tenant = await session.get(m.Tenant, run.tenant_id)
                try:
                    check_placement(
                        self.executor.caps(),
                        trust_tier=tenant.trust_tier if tenant else "public",
                        resource_class=ResourceClass(run.resource_class),
                    )
                except MapiError as exc:
                    await apply_transition(session, run, RunTrigger.LEASE, Actor.DISPATCHER)
                    await apply_transition(
                        session,
                        run,
                        RunTrigger.FAIL_INFRA,
                        Actor.DISPATCHER,
                        problem=Problem.from_code(ErrorCode.INFRA_ERROR, exc.detail),
                    )
                    continue
                await apply_transition(session, run, RunTrigger.LEASE, Actor.DISPATCHER)
                run.executor = self.executor.name
                run.lease_owner = self.name
                run.lease_expires_at = now() + timedelta(seconds=s.dispatch_lease_s)
                session.add(
                    m.RunAttempt(
                        run_id=run.id,
                        attempt=run.attempt,
                        tenant_id=run.tenant_id,
                        project_id=run.project_id,
                        nonce=new_nonce(),
                        hmac_key=new_hmac_key(),
                        executor=self.executor.name,
                        status="intent",
                        deadline=now() + timedelta(seconds=run.timeout_s),
                        outputs={},
                        violations=0,
                        last_seq=-1,
                        log_bytes=0,
                    )
                )
                leased.append((run.id, run.attempt))
        return leased

    async def dispatch_once(self) -> int:
        leased = await self.lease()
        for run_id, attempt in leased:
            await self.bucket.acquire()
            try:
                await self.submit(run_id, attempt)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dispatch of %s/%s failed unexpectedly", run_id, attempt)
        return len(leased)

    async def submit(self, run_id: str, attempt_no: int) -> None:
        ctx = self.ctx
        async with ctx.db.session() as session:
            run = await session.get(m.Run, run_id)
            att = await session.get(m.RunAttempt, (run_id, attempt_no))
            if run is None or att is None:
                return
            session.expunge_all()
        if run.state != RunState.DISPATCHING.value or run.attempt != attempt_no:
            return
        handle: Handle | None = None
        error: Exception | None = None
        try:
            runspec = await build_runspec(
                ctx.store, run=run, attempt=att, runs_bucket=ctx.buckets.runs
            )
            await ctx.store.put(
                ctx.buckets.bootstrap,
                bootstrap_key(att.nonce),
                runspec.model_dump_json().encode(),
                content_type="application/json",
                if_generation_match=0,
            )
            rs = RunSubmit(
                run_id=run.id,
                attempt=attempt_no,
                kind=RunKind(run.kind),
                resource_class=ResourceClass(run.resource_class),
                timeout=Duration(run.timeout_s),
                labels={k: str(v)[:63] for k, v in list(run.labels.items())[:64]},
            )
            handle = await self.executor.submit(rs, att.nonce)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
            log.warning("submit of %s/%s failed: %r", run_id, attempt_no, exc)
            with contextlib.suppress(Exception):
                handle = await self.executor.adopt(att.nonce)
        await self._record(run_id, attempt_no, att.nonce, handle, error)

    async def _record(
        self,
        run_id: str,
        attempt_no: int,
        nonce: str,
        handle: Handle | None,
        error: Exception | None,
    ) -> None:
        ctx = self.ctx
        async with ctx.db.session() as session:
            run = (
                await session.execute(select(m.Run).where(m.Run.id == run_id).with_for_update())
            ).scalar_one()
            att = await session.get(m.RunAttempt, (run_id, attempt_no))
            assert att is not None
            if handle is not None:
                att.status = "submitted"
                att.handle = handle.model_dump(mode="json")
                att.external_id = handle.external_id
                att.submitted_at = now()
                if run.state == RunState.DISPATCHING.value and run.attempt == attempt_no:
                    await apply_transition(session, run, RunTrigger.SUBMITTED, Actor.DISPATCHER)
                    run.lease_owner = None
                    run.lease_expires_at = None
                # CANCELLING: the reconciler sees the handle and cancels the execution
                return
            att.status = "abandoned"
            att.finished_at = now()
            if run.state != RunState.DISPATCHING.value or run.attempt != attempt_no:
                return
            detail = "the executor did not accept the execution"
            if isinstance(error, MapiError):
                detail = f"{detail}: {error.detail}"
            elif error is not None:
                detail = f"{detail}: {type(error).__name__}"
            permanent = isinstance(error, NotFound)  # an input object is gone: no retry
            if not permanent and run.infra_retries < ctx.settings.max_infra_retries:
                run.infra_retries += 1
                await apply_transition(session, run, RunTrigger.INFRA_RETRY, Actor.DISPATCHER)
            else:
                await apply_transition(
                    session,
                    run,
                    RunTrigger.FAIL_INFRA,
                    Actor.DISPATCHER,
                    problem=Problem.from_code(ErrorCode.INFRA_ERROR, detail[:2000]),
                )
        with contextlib.suppress(Exception):
            await ctx.store.delete(ctx.buckets.bootstrap, bootstrap_key(nonce))
