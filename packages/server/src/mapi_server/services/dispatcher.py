"""The dispatcher (plan §8.3): lease QUEUED runs and hand them to the executor.

Per run, in order — each step durable before the next:

1. **lease**: ``QUEUED → DISPATCHING`` under ``FOR UPDATE SKIP LOCKED`` (many dispatchers
   may run), and write the **intent row** ``(run, attempt, nonce, hmac_key)``; commit;
2. re-verify any bundle inputs against the model version's signed manifest (plan §8.6:
   the signature is checked whenever URLs are minted), then write the
   :class:`~mapi_spec.run.RunSpec` to ``bootstrap/<nonce>`` (write-once);
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
import re
import socket
import time
from collections import deque
from datetime import datetime, timedelta

from mapi_spec._types import Duration
from mapi_spec.admin import kill_switch_decision
from mapi_spec.errors import ErrorCode, MapiError, Problem
from mapi_spec.executor import PLUGIN_LABEL, Executor, Handle, RunKind, RunSubmit
from mapi_spec.quotas import (
    LeaseCandidate,
    PriorityClass,
    QuotaDemand,
    QuotaPolicy,
    QuotaScope,
    QuotaUsage,
    backoff_seconds,
    evaluate_quota,
    plan_leases,
)
from mapi_spec.resources import ResourceClass
from mapi_spec.run import Actor, RunState, RunTrigger, new_hmac_key, new_nonce
from mapi_spec.storage import NotFound, bootstrap_key
from mapi_spec.tenancy import TenantStatus, TrustTier
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.executors import check_placement
from mapi_server.observability import metrics as obs_metrics
from mapi_server.observability.tracing import run_span
from mapi_server.ratelimit import TokenBucket
from mapi_server.services.bundles import BundleIntegrityError, verify_bundle_inputs
from mapi_server.services.evidence import gated_tier, shared_tiers_gate
from mapi_server.services.quotas import (
    DISPATCHED_STATES,
    global_policy,
    global_spend,
    kill_switches,
    quota_usage,
    tenant_policy,
)
from mapi_server.services.runs import apply_transition, now
from mapi_server.services.runspec import build_runspec

__all__ = ["Dispatcher", "TokenBucket"]

_RESERVED_LABEL_PREFIX = "mapi.dev/"
_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

log = logging.getLogger(__name__)

MAX_RETRY_AFTER_S = 64.0
"""A backend's ``Retry-After`` is honoured up to this long (one answer cannot park the queue)."""


def _rate_limited(error: Exception | None) -> bool:
    """Is ``error`` the backend's 429 (nothing was created, so the attempt is not lost)?"""
    status = (
        getattr(error, "status_code", None)
        or getattr(error, "status", None)  # CloudRunRateLimited (after its own retries)
        or getattr(error, "code", None)
    )
    return status in (429, ErrorCode.RATE_LIMITED, "429")


def _over_budget(policy: QuotaPolicy, usage: QuotaUsage) -> bool:
    """Is the tenant past a vCPU-second limit? (Its queued runs' estimates are already in
    ``usage``: admission kept it within the limits, so this is only true when an operator
    tightened them since.)"""
    lim = policy.limits
    return (
        lim.vcpu_seconds_per_day is not None and usage.vcpu_seconds_today > lim.vcpu_seconds_per_day
    ) or (
        lim.vcpu_seconds_per_month is not None
        and usage.vcpu_seconds_month > lim.vcpu_seconds_per_month
    )


class Dispatcher:
    def __init__(self, ctx: ServerContext, executor: Executor, *, name: str | None = None) -> None:
        self.ctx = ctx
        self.executor = executor
        self.name = name or f"dispatcher-{socket.gethostname()}-{os.getpid()}"
        s = ctx.settings
        self.bucket = TokenBucket(s.dispatch_rate_per_minute / 60.0, s.dispatch_burst)
        self._submits: deque[float] = deque()
        self._throttles: deque[float] = deque()
        self._throttle_streak = 0
        self._backoff_until = 0.0
        self._last_success: datetime | None = None
        self._reported = 0.0

    def _throttled(self, error: Exception | None) -> float | None:
        """Seconds to back off if ``error`` is the backend's 429 (``None`` otherwise)."""
        if not _rate_limited(error):
            return None
        retry_after = getattr(error, "retry_after", None)
        self._throttle_streak += 1
        return backoff_seconds(
            self._throttle_streak,
            retry_after=(
                min(float(retry_after), MAX_RETRY_AFTER_S)
                if isinstance(retry_after, int | float)
                else None
            ),
        )

    async def report(self) -> None:
        """Upsert this executor's row in ``executor_reports`` (the admin console)."""
        t = time.monotonic()
        for q in (self._submits, self._throttles):
            horizon = 60.0 if q is self._submits else 3600.0
            while q and t - q[0] > horizon:
                q.popleft()
        ts = now()
        caps = self.executor.caps()
        # Enabled = every tier its caps admit is backed by evidence: an executor whose caps
        # allow shared tiers is enabled only with passing, unexpired conformance and security
        # reports for these caps (plan §8.4). Others serve the trusted tiers only.
        gate: str | None = None
        if caps.allows_public_tenants():
            async with self.ctx.db.session() as session:
                gate = await shared_tiers_gate(session, self.executor.name, caps, now=ts)
        values = {
            "name": self.executor.name,
            "enabled": gate is None,
            "caps": caps.model_dump(mode="json"),
            "submits_last_minute": len(self._submits),
            "submit_rate_limit_per_minute": int(self.ctx.settings.dispatch_rate_per_minute),
            "throttled_last_hour": len(self._throttles),
            "last_success_at": self._last_success,
            "reported_by": self.name,
            "checked_at": ts,
            "message": None,
        }
        own_health = getattr(self.executor, "health", None)
        if callable(own_health):
            # executors that throttle themselves (cloudrun-jobs: its jobs.run token bucket
            # and 429 alarm) know more than the dispatcher sees from the outside
            h = own_health()
            values["submits_last_minute"] = max(len(self._submits), h.submits_last_minute)
            if h.submit_rate_limit_per_minute is not None:
                values["submit_rate_limit_per_minute"] = min(
                    int(self.ctx.settings.dispatch_rate_per_minute),
                    h.submit_rate_limit_per_minute,
                )
            values["throttled_last_hour"] = max(len(self._throttles), h.throttled_last_hour)
            values["last_success_at"] = self._last_success or h.last_success_at
            values["message"] = h.message
        if gate is not None:
            values["message"] = f"verified and public tenants are held: {gate}"[:500]
        update = {k: v for k, v in values.items() if k != "name"}
        if values["last_success_at"] is None:
            update.pop("last_success_at")
        async with self.ctx.db.session() as session:
            await session.execute(
                pg_insert(m.ExecutorReport)
                .values(**values)
                .on_conflict_do_update(index_elements=["name"], set_=update)
            )
        self._reported = t

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("dispatcher %s started (executor %s)", self.name, self.executor.name)
        while not stop.is_set():
            if time.monotonic() - self._reported >= self.ctx.settings.executor_report_interval_s:
                with contextlib.suppress(Exception):
                    await self.report()
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
        """Lease queued runs by weighted fair share and write their intent rows.

        Up to ``lease_candidates`` queued runs are locked (``SKIP LOCKED``); runs the kill
        switches hold back, and runs of tenants that are not active, stay queued. The rest go
        through :func:`mapi_spec.quotas.plan_leases` with the free global capacity (the
        global ``concurrent_runs`` limit minus dispatched tasks, at most ``dispatch_batch``),
        each tenant's remaining ``concurrent_runs`` and its running tasks, so interactive
        work (weight 4) and idle tenants go first.
        """
        s = self.ctx.settings
        leased: list[tuple[str, int]] = []
        async with self.ctx.db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(m.Run)
                        .where(m.Run.state == RunState.QUEUED.value)
                        .order_by(m.Run.queued_at, m.Run.id)
                        .limit(s.lease_candidates)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            if not rows:
                return leased
            at = now()
            plan = await self._plan(session, rows, at)
            by_id = {r.id: r for r in rows}
            for pick in plan:
                run = by_id[pick.run_id]
                tenant = await session.get(m.Tenant, run.tenant_id)
                try:
                    check_placement(
                        self.executor.caps(),
                        trust_tier=tenant.trust_tier if tenant else "public",
                        resource_class=ResourceClass(run.resource_class),
                        residency=tenant.data_residency if tenant else None,
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

    async def _plan(
        self, session: AsyncSession, rows: list[m.Run], at: datetime
    ) -> list[LeaseCandidate]:
        """Which of the locked queued runs to lease now (kill switches, caps, fair share)."""
        s = self.ctx.settings
        tenants = {
            t.id: t
            for t in (
                await session.execute(
                    select(m.Tenant).where(m.Tenant.id.in_({r.tenant_id for r in rows}))
                )
            ).scalars()
        }
        switches = await kill_switches(session)
        spent = 0
        if switches.global_spend_cap_micros is not None:
            spent = await global_spend(session, switches, now=at)
        load = (
            await session.execute(
                select(m.Run.tenant_id, func.count(), func.coalesce(func.sum(m.Run.task_count), 0))
                .where(m.Run.state.in_(DISPATCHED_STATES))
                .group_by(m.Run.tenant_id)
            )
        ).all()
        running_runs = {str(t): int(n) for t, n, _ in load}
        running_tasks = {str(t): int(k) for t, _, k in load}
        global_cap = (await global_policy(session)).limits.concurrent_runs
        capacity = s.dispatch_batch
        if global_cap is not None:
            capacity = max(0, min(capacity, global_cap - sum(running_tasks.values())))
        left: dict[str, int | None] = {}
        budgets: dict[str, tuple[QuotaPolicy, QuotaUsage]] = {}
        candidates: list[LeaseCandidate] = []
        held = 0
        caps = self.executor.caps()
        shared_gate: str | None = None
        if caps.allows_public_tenants() and any(
            r.tenant_id not in tenants or gated_tier(tenants[r.tenant_id].trust_tier) for r in rows
        ):
            shared_gate = await shared_tiers_gate(session, self.executor.name, caps, now=at)
        for run in rows:
            tenant = tenants.get(run.tenant_id)
            tier = tenant.trust_tier if tenant is not None else TrustTier.PUBLIC.value
            if tenant is not None and tenant.status != TenantStatus.ACTIVE.value:
                held += 1
                continue  # suspended tenants keep their queue until reactivated
            if shared_gate is not None and gated_tier(tier):
                # plan §8.4: no shared-tier work without passing, unexpired conformance and
                # security reports for the current caps; the run waits for the evidence
                held += 1
                continue
            decision = kill_switch_decision(
                switches,
                untrusted_code=run.untrusted_code,
                tier=TrustTier(tier),
                spent_micros=spent,
                now=at,
            )
            if not decision.allowed:
                held += 1
                continue
            if run.tenant_id not in left:
                policy = await tenant_policy(session, run.tenant_id, tier)
                limit = policy.limits.concurrent_runs
                left[run.tenant_id] = (
                    None if limit is None else limit - running_runs.get(run.tenant_id, 0)
                )
                usage = await quota_usage(session, QuotaScope.TENANT, run.tenant_id, now=at)
                budgets[run.tenant_id] = (policy, usage)
            # the quota decision at dispatch: a run admitted under looser limits (an operator
            # tightened them since) waits until the window resets instead of starting
            policy, usage = budgets[run.tenant_id]
            demand = QuotaDemand(resource_class=ResourceClass(run.resource_class))
            if (
                _over_budget(policy, usage)
                or not evaluate_quota([(policy, usage)], demand, now=at).allowed
            ):
                held += 1
                continue
            candidates.append(
                LeaseCandidate(
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    priority=PriorityClass(run.priority),
                    queued_at=run.queued_at or run.created_at,
                    tasks=max(1, run.task_count),
                )
            )
        if held:
            log.debug("%d queued run(s) held back (kill switches, tenant status, evidence)", held)
            if shared_gate is not None:
                log.warning("verified and public runs are held: %s", shared_gate)
        return plan_leases(
            candidates, capacity=capacity, running_tasks=running_tasks, concurrency_left=left
        )

    async def dispatch_once(self) -> int:
        leased = await self.lease()
        if not leased:
            return 0
        # Runs are submitted one after another, and a submit may take minutes (the token
        # bucket, a 429 back-off inside the executor). Keep every lease of the batch alive
        # until its run is recorded, so the reconciler never takes a run this dispatcher is
        # still working on (a lease only lapses when the dispatcher itself is gone).
        pending = set(leased)
        keeper = asyncio.create_task(self._keep_leased(pending))
        try:
            for run_id, attempt in leased:
                await self.bucket.acquire()
                pause = self._backoff_until - time.monotonic()
                if pause > 0:  # the backend answered 429: wait it out, then submit once
                    await asyncio.sleep(pause)
                try:
                    await self.submit(run_id, attempt)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("dispatch of %s/%s failed unexpectedly", run_id, attempt)
                pending.discard((run_id, attempt))
        finally:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
        return len(leased)

    async def _keep_leased(self, pending: set[tuple[str, int]]) -> None:
        """Extend the leases of ``pending`` runs every third of a lease until cancelled."""
        lease_s = self.ctx.settings.dispatch_lease_s
        while True:
            await asyncio.sleep(lease_s / 3)
            for run_id, attempt in list(pending):
                try:
                    async with self.ctx.db.session() as session:
                        await session.execute(
                            update(m.Run)
                            .where(
                                m.Run.id == run_id,
                                m.Run.attempt == attempt,
                                m.Run.state == RunState.DISPATCHING.value,
                                m.Run.lease_owner == self.name,
                            )
                            .values(lease_expires_at=now() + timedelta(seconds=lease_s))
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("could not extend the lease of %s/%s", run_id, attempt)

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
        with run_span("dispatch.submit", run_id=run_id, attempt=attempt_no, kind=run.kind):
            await self._submit_locked(run, att, run_id, attempt_no)

    async def _submit_locked(
        self, run: m.Run, att: m.RunAttempt, run_id: str, attempt_no: int
    ) -> None:
        ctx = self.ctx
        handle: Handle | None = None
        error: Exception | None = None
        try:
            if run.model_version_id is not None:
                await self._verify_bundles(run)
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
                labels=await self._labels(run),
            )
            handle = await self.executor.submit(rs, att.nonce)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
            log.warning("submit of %s/%s failed: %r", run_id, attempt_no, exc)
            delay = self._throttled(exc)
            if delay is not None:
                self._throttles.append(time.monotonic())
                self._backoff_until = time.monotonic() + delay
            with contextlib.suppress(Exception):
                handle = await self.executor.adopt(att.nonce)
            if handle is not None and handle.attempt != attempt_no:
                handle = handle.model_copy(update={"attempt": attempt_no})
        if handle is not None:
            self._submits.append(time.monotonic())
            if error is None:
                self._throttle_streak = 0
                self._last_success = now()
        await self._record(run_id, attempt_no, att.nonce, handle, error)

    async def _labels(self, run: m.Run) -> dict[str, str]:
        """The run's labels for the executor, plus :data:`PLUGIN_LABEL` (platform-set only)."""
        labels = {
            k: str(v)[:63]
            for k, v in list(run.labels.items())[:63]
            if not k.startswith(_RESERVED_LABEL_PREFIX)
        }
        plugin = await self._plugin(run)
        if plugin:
            labels[PLUGIN_LABEL] = plugin
        return labels

    async def _plugin(self, run: m.Run) -> str | None:
        """Namespace of the model plugin the run executes (``ts`` for ``ts.hsgp_trend@1``)."""
        ref: object = None
        async with self.ctx.db.session() as session:
            if run.spec_id is not None and run.spec_revision is not None:
                rev = await session.get(m.SpecRevision, (run.spec_id, run.spec_revision))
                if rev is not None and rev.resolved:
                    ref = (rev.resolved.get("model") or {}).get("resolved")
            elif run.model_version_id is not None:
                mv = await session.get(m.ModelVersion, run.model_version_id)
                if mv is not None:
                    ref = (mv.lineage or {}).get("plugin")
        if not isinstance(ref, str) or "." not in ref:
            return None
        namespace = ref.split(".", 1)[0]
        return namespace if _NAMESPACE.match(namespace) else None

    async def _verify_bundles(self, run: m.Run) -> None:
        async with self.ctx.db.session() as session:
            mv = await session.get(m.ModelVersion, run.model_version_id)
            if mv is None or mv.tenant_id != run.tenant_id or mv.project_id != run.project_id:
                raise BundleIntegrityError("the run's model version no longer exists")
            session.expunge(mv)
        await verify_bundle_inputs(self.ctx.store, self.ctx.signer, mv, run.inputs)

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
                    if run.queued_at is not None:
                        obs_metrics.record_dispatch_latency(
                            (att.submitted_at - run.queued_at).total_seconds()
                        )
                # CANCELLING: the reconciler sees the handle and cancels the execution
                return
            att.status = "abandoned"
            att.finished_at = now()
            if run.state != RunState.DISPATCHING.value or run.attempt != attempt_no:
                return
            detail = "the executor did not accept the execution"
            code = ErrorCode.INFRA_ERROR
            if isinstance(error, BundleIntegrityError):
                # no URLs were minted; a bundle that fails verification never will verify
                detail = f"the model bundle failed verification: {error.detail}"
                code = ErrorCode.INTEGRITY_FAILURE
                log.error("run %s/%s: %s", run_id, attempt_no, detail)
            elif isinstance(error, MapiError):
                detail = f"{detail}: {error.detail}"
            elif error is not None:
                detail = f"{detail}: {type(error).__name__}"
            # an input object is gone, or the bundle does not verify: no retry
            permanent = isinstance(error, NotFound | BundleIntegrityError)
            if _rate_limited(error):
                # the backend refused to create anything (429): requeue without using up
                # an infrastructure retry; the dispatcher's back-off paces the next submit
                await apply_transition(session, run, RunTrigger.INFRA_RETRY, Actor.DISPATCHER)
            elif not permanent and run.infra_retries < ctx.settings.max_infra_retries:
                run.infra_retries += 1
                await apply_transition(session, run, RunTrigger.INFRA_RETRY, Actor.DISPATCHER)
            else:
                await apply_transition(
                    session,
                    run,
                    RunTrigger.FAIL_INFRA,
                    Actor.DISPATCHER,
                    problem=Problem.from_code(code, detail[:2000]),
                )
        with contextlib.suppress(Exception):
            await ctx.store.delete(ctx.buckets.bootstrap, bootstrap_key(nonce))
