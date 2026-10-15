"""Executor conformance suite (plan §8.4, §13): every case of
:data:`mapi_spec.conformance_executor.EXECUTOR_CONFORMANCE`, parametrized over backends —
``fake-cloudrun`` (CloudRunJobsExecutor over FakeCloudRun, events through authenticated
Pub/Sub pushes), ``local-process`` and ``docker`` (skipped without a daemon and image).

Each case drives a real control plane (dispatcher, reconciler, event ingestion, finalizer on
Postgres), injects the case's fault and checks the exact state trajectory (recorded by a
trigger) with :func:`~mapi_spec.conformance_executor.check_expectation`. Cases whose
requirements a backend does not support are skipped (``ConformanceTarget.supports``). The
last test turns the results into a :class:`ConformanceReport` per backend and checks
``passed_for`` — the gate that decides whether an executor may be enabled.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mapi_server.context import ServerContext
from mapi_server.storage import list_all
from mapi_server_testkit.conformance import (
    TERMINAL,
    Harness,
    drop_state_log,
    harness,
    inline_queue,
    install_state_log,
    lease_for_fanout,
    trajectory,
    write_task_bootstraps,
)
from mapi_spec._types import Duration
from mapi_spec.conformance_executor import (
    EXECUTOR_CONFORMANCE,
    CaseOutcome,
    ConformanceCase,
    ConformanceCaseId,
    ConformanceReport,
    ConformanceResult,
    ObservedRun,
    applicable_cases,
    check_expectation,
)
from mapi_spec.executor import ExecState, RunKind, RunSubmit
from mapi_spec.resources import ResourceClass
from mapi_spec.run import FailureClass, RunState
from mapi_spec.storage import run_checkpoint_prefix, run_prefix

pytestmark = pytest.mark.integration

TARGETS = [
    pytest.param("fake-cloudrun", id="fake-cloudrun"),
    pytest.param("local-process", id="local-process"),
    pytest.param("docker", id="docker", marks=pytest.mark.docker),
]
S = RunState
RESULTS: dict[str, dict[ConformanceCaseId, ConformanceResult]] = {}
TARGETS_SEEN: dict[str, Any] = {}


@pytest.fixture
async def state_log(ctx: ServerContext) -> Any:
    await install_state_log(ctx)
    yield
    await drop_state_log(ctx)


# ----------------------------------------------------------------------- cases


async def _submit(h: Harness) -> ObservedRun:
    run_id = await h.fit("ok")
    await h.start()
    await h.wait(run_id, TERMINAL)
    if h.fake is not None:  # identifiers reach the backend as env overrides only
        for call in h.fake.run_job_calls:
            if set(call.env) != {"MAPI_RUN_ID", "MAPI_NONCE"}:
                h.notes.append(f"jobs.run overrode more than the identifiers: {sorted(call.env)}")
    else:
        argv = getattr(h.executor.inner, "_command", [])
        att = await h.attempt(run_id)
        assert att is not None
        if any(run_id in a or att.nonce in a for a in argv):
            h.notes.append("the run id or nonce appears in argv")
    return await h.observe(run_id)


async def _adopt_after_crash(h: Harness) -> ObservedRun:
    h.ctx.settings.dispatch_lease_s = 0.3
    await h.start()
    assert h.orch is not None
    dispatcher = h.orch.dispatcher
    original = dispatcher._record
    crashed = asyncio.Event()

    async def crash_after_submit(*args: Any) -> None:
        handle = args[3]
        if not crashed.is_set() and handle is not None:
            crashed.set()
            h.restart_driver()  # the orchestrator comes back with a fresh driver
            raise RuntimeError("simulated dispatcher crash after the executor accepted")
        await original(*args)

    dispatcher._record = crash_after_submit  # type: ignore[method-assign]
    run_id = await h.fit("ok")
    await h.wait(run_id, TERMINAL, timeout=60)
    if not crashed.is_set():
        h.notes.append("the crash fault was never injected")
    att = await h.attempt(run_id)
    if att is None or not att.external_id:
        h.notes.append("the adopted execution was not recorded on the intent row")
    return await h.observe(run_id)


async def _duplicate_submit(h: Harness) -> ObservedRun:
    h.executor.duplicate_submit = True
    run_id = await h.fit("ok")
    await h.start()
    await h.wait(run_id, TERMINAL)
    if h.executor.submit_calls < 2:
        h.notes.append("submit was not duplicated")
    return await h.observe(run_id)


async def _cancel_mid_run(h: Harness) -> ObservedRun:
    run_id = await h.fit("hang")
    await h.start()
    await h.wait(run_id, {S.RUNNING})
    r = await h.api.post(f"/v1/runs/{run_id}:cancel")
    assert r.status_code == 202, r.text
    await h.wait(run_id, TERMINAL)
    if h.fake is not None:
        (ex,) = h.fake.executions_for_run(run_id)
        cancelled_at = next(t for s, _, t in await trajectory(h.ctx, run_id) if s is S.CANCELLED)
        if ex.completion_time is None or ex.completion_time > cancelled_at:
            h.notes.append("CANCELLED was recorded before the executor confirmed the stop")
    return await h.observe(run_id)


async def _timeout(h: Harness) -> ObservedRun:
    run_id = await h.fit("hang", timeout_s=2)
    await h.start()
    await h.wait(run_id, TERMINAL, timeout=60)
    return await h.observe(run_id)


async def _stalled_not_lost(h: Harness) -> ObservedRun:
    run_id = await h.fit("gate")
    await h.start()
    await h.wait(run_id, {S.RUNNING})

    async def stalled() -> bool:
        await h.backdate_events(run_id)  # silence far beyond K x interval
        await asyncio.sleep(0.15)
        return (await h.run(run_id)).state == S.STALLED.value

    await h.until(stalled)
    h.open_gate(run_id)  # events resume
    await h.wait(run_id, TERMINAL)
    return await h.observe(run_id)


async def _confirmed_cancel_before_retry(h: Harness) -> ObservedRun:
    run_id = await h.fit("hang_first")
    await h.start()
    await h.wait(run_id, {S.RUNNING})
    att = await h.attempt(run_id)
    assert att is not None and att.external_id
    if h.fake is not None:
        h.fake.hide_execution(att.external_id)  # the status API loses it; it still runs
    else:
        h.executor.unknown_once(att.nonce)
    await h.wait(run_id, TERMINAL, timeout=60)
    return await h.observe(run_id)


async def _checkpoint_resume(h: Harness) -> ObservedRun:
    run_id = await h.fit("checkpoint", checkpointable=True)
    await h.start()
    await h.wait(run_id, {S.RUNNING})
    prefix = run_checkpoint_prefix(h.ctx.scope.tenant_id, run_id, 1)

    async def checkpointed() -> bool:
        return bool(await list_all(h.ctx.store, h.ctx.buckets.runs, prefix))

    await h.until(checkpointed)
    r = await h.api.post(f"/v1/runs/{run_id}:cancel")  # SIGTERM → flush → PARTIAL
    assert r.status_code == 202, r.text
    await h.wait(run_id, {S.PARTIAL, S.CANCELLED, S.FAILED}, timeout=60)
    r = await h.api.post(f"/v1/runs/{run_id}:resume")
    if r.status_code != 202:
        h.notes.append(f"resume answered {r.status_code}: {r.text[:200]}")
        return await h.observe(run_id)
    await h.wait(run_id, TERMINAL - {S.PARTIAL}, timeout=60)
    return await h.observe(run_id)


async def _oom(h: Harness) -> ObservedRun:
    run_id = await h.fit("oom")
    await h.start()
    await h.wait(run_id, TERMINAL)
    return await h.observe(run_id)


async def _fanout(h: Harness) -> ObservedRun:
    """Executor level: fan-out parents (Sweep, SBC) arrive in M4, so the harness plays the
    dispatcher — intent row, one bootstrap RunSpec per task, one ``task_count=4`` submit."""
    tasks = 4
    run_id = await h.fit("fanout")
    run, att = await lease_for_fanout(h, run_id)
    await write_task_bootstraps(h, run, att, tasks)
    rs = RunSubmit(
        run_id=run_id,
        attempt=1,
        kind=RunKind.FIT,
        resource_class=ResourceClass(run.resource_class),
        timeout=Duration(run.timeout_s),
        task_count=tasks,
    )
    handle = await h.executor.submit(rs, att.nonce)
    states = [S.QUEUED, S.DISPATCHING, S.PROVISIONING]
    deadline = time.monotonic() + 30
    while True:
        st = await h.executor.status(handle)
        if st.state is ExecState.RUNNING and S.RUNNING not in states:
            states.append(S.RUNNING)
        if st.state.terminal or time.monotonic() > deadline:
            break
        await asyncio.sleep(0.02)
    failure: FailureClass | None = None
    if st.state is ExecState.SUCCEEDED:
        states.append(S.FINALIZING)
        prefix = run_prefix(run.tenant_id, run_id, 1)
        found: dict[int, dict[str, Any]] = {}
        for i in range(tasks):
            key = run_prefix(run.tenant_id, run_id, 1, task_index=i) + "result.json"
            body = json.loads(await h.ctx.store.get(h.ctx.buckets.runs, key))
            found[i] = body
        if sorted(found) != list(range(tasks)) or any(
            b["task"] != i or b["count"] != tasks or b["child"] != f"child-{i}"
            for i, b in found.items()
        ):
            h.notes.append(f"task outputs do not map one child per index: {found}")
        stray = [
            o.name
            for o in await list_all(h.ctx.store, h.ctx.buckets.runs, prefix)
            if not o.name.startswith(prefix + "tasks/")
        ]
        if stray:
            h.notes.append(f"objects outside the per-task prefixes: {stray}")
        states.append(S.SUCCEEDED)
    else:
        failure = FailureClass.INFRA
        states.append(S.FAILED)
        h.notes.append(f"fan-out execution ended {st.state.value} (exit {st.exit_code})")
    return ObservedRun(
        states=tuple(states),
        failure_class=failure,
        attempts=1,
        executions_created=h.executions_created(run_id),
        max_live_executions=h.max_live(run_id),
    )


async def _generation_verification(h: Harness) -> ObservedRun:
    real = inline_queue(h.ctx)
    tampered: list[str] = []

    class TamperingQueue:
        async def defer_finalize(self, run_id: str, attempt: int) -> None:
            if not tampered:  # overwrite one output after the runner's manifest was signed
                key = run_prefix(h.ctx.scope.tenant_id, run_id, attempt) + "metrics.json"
                await h.ctx.store.put(h.ctx.buckets.runs, key, b'{"values": {"rmse": 0.0}}')
                tampered.append(key)
            await real.defer_finalize(run_id, attempt)

    h.ctx.queue = TamperingQueue()
    run_id = await h.fit("ok")
    await h.start()
    await h.wait(run_id, TERMINAL)
    if not tampered:
        h.notes.append("the output was never tampered with")
    copied = await list_all(h.ctx.store, h.ctx.buckets.bundles, f"t/{h.ctx.scope.tenant_id}/")
    if copied:
        h.notes.append(f"objects reached the bundles bucket: {[o.name for o in copied]}")
    return await h.observe(run_id)


async def _rate_limit_backoff(h: Harness) -> ObservedRun:
    assert h.fake is not None
    h.fake.rate_limit(3)
    run_id = await h.fit("ok")
    await h.start()
    await h.wait(run_id, TERMINAL)
    calls = [c for c in h.fake.run_job_calls if c.env.get("MAPI_RUN_ID") == run_id]
    outcomes = [c.outcome for c in calls]
    if outcomes != ["throttled", "throttled", "throttled", "created"]:
        h.notes.append(f"unexpected jobs.run sequence {outcomes}")
    gaps = [b.at - a.at for a, b in itertools.pairwise(calls)]
    if any(later <= earlier for earlier, later in itertools.pairwise(gaps)):
        h.notes.append(f"delays between submits did not grow: {gaps}")
    throttled = getattr(h.executor.inner, "throttled", None)
    if throttled is None or throttled.total != 3:
        h.notes.append("the 429 alarm counter did not count 3 answers")
    return await h.observe(run_id)


RUNNERS: dict[ConformanceCaseId, Callable[[Harness], Awaitable[ObservedRun]]] = {
    ConformanceCaseId.SUBMIT: _submit,
    ConformanceCaseId.ADOPT_AFTER_DISPATCHER_CRASH: _adopt_after_crash,
    ConformanceCaseId.DUPLICATE_SUBMIT: _duplicate_submit,
    ConformanceCaseId.CANCEL_MID_RUN: _cancel_mid_run,
    ConformanceCaseId.TIMEOUT: _timeout,
    ConformanceCaseId.STALLED_NOT_LOST: _stalled_not_lost,
    ConformanceCaseId.CONFIRMED_CANCEL_BEFORE_RETRY: _confirmed_cancel_before_retry,
    ConformanceCaseId.CHECKPOINT_RESUME: _checkpoint_resume,
    ConformanceCaseId.OOM_CLASSIFICATION: _oom,
    ConformanceCaseId.FANOUT_TASK_COUNT: _fanout,
    ConformanceCaseId.GENERATION_VERIFICATION: _generation_verification,
    ConformanceCaseId.RATE_LIMIT_BACKOFF: _rate_limit_backoff,
}


def test_every_case_has_a_runner() -> None:
    assert set(RUNNERS) == {c.id for c in EXECUTOR_CONFORMANCE}


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("case", EXECUTOR_CONFORMANCE, ids=lambda c: c.id.value)
async def test_executor_conformance(
    case: ConformanceCase, target: str, ctx: ServerContext, tmp_path: Path, state_log: Any
) -> None:
    async with harness(target, ctx, tmp_path) as h:
        TARGETS_SEEN[target] = h.target
        results = RESULTS.setdefault(target, {})
        missing = [r.value for r in case.requires if not h.target.supports(r)]
        if missing:
            results[case.id] = ConformanceResult(case_id=case.id, outcome=CaseOutcome.SKIPPED)
            pytest.skip(f"{target} does not support {', '.join(missing)}")
        started = time.monotonic()
        try:
            observed = await asyncio.wait_for(RUNNERS[case.id](h), case.budget_seconds)
        except Exception:
            results[case.id] = ConformanceResult(case_id=case.id, outcome=CaseOutcome.ERROR)
            raise
        problems = check_expectation(case, observed) + h.notes
        results[case.id] = ConformanceResult(
            case_id=case.id,
            outcome=CaseOutcome.FAILED if problems else CaseOutcome.PASSED,
            duration_seconds=round(time.monotonic() - started, 3),
            problems=problems,
        )
        assert not problems, f"{target}/{case.id.value}: {problems} (observed {observed})"


@pytest.mark.parametrize("target", ["fake-cloudrun", "local-process"])
def test_conformance_report_gates_the_executor(target: str) -> None:
    """Runs after the cases (file order): the suite's verdict for each backend."""
    results = RESULTS.get(target, {})
    spec = TARGETS_SEEN.get(target)
    if spec is None or len(results) < len(EXECUTOR_CONFORMANCE):
        pytest.skip("the conformance cases of this target did not all run in this session")
    report = ConformanceReport(
        executor=spec.executor,
        environment="pr",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        results=list(results.values()),
    )
    assert report.missing(spec) == []
    assert report.passed_for(spec), [r for r in report.results if r.outcome != "passed"]
    expected_skips = {c.id for c in EXECUTOR_CONFORMANCE} - {c.id for c in applicable_cases(spec)}
    assert {r.case_id for r in report.results if r.outcome == "skipped"} == expected_skips
