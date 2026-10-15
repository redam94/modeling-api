"""CloudRunJobsExecutor against FakeCloudRun (no cloud calls), the status mapping, the
google-cloud-run adapter (with test doubles, and the real request/response types when the
library is installed), the fan-out helpers and the rate limiter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from mapi_server.executors import (
    ExecutorRefused,
    PlacementRefused,
    build_cloudrun_executor,
    build_executor,
    check_placement,
)
from mapi_server.executors.cloudrun import (
    CLOUDRUN_EXECUTOR,
    Base,
    CloudRunError,
    CloudRunFailedPrecondition,
    CloudRunJobsExecutor,
    CloudRunNotFound,
    CloudRunRateLimited,
    CloudRunUnavailable,
    ConditionInfo,
    ExecutionInfo,
    GoogleCloudRunApi,
    JobInventory,
    TaskInfo,
    aggregate_exit_code,
    execution_info,
    execution_status,
    map_api_error,
    run_job_request,
    same_execution,
    task_exit_code,
    task_info,
)
from mapi_server.executors.fake_cloudrun import FakeCloudRun, FakeLatency, FakeOOM, FakeTask
from mapi_server.executors.tasks import (
    ENV_TASK_COUNT,
    ENV_TASK_INDEX,
    TaskEnv,
    batch_key,
    child_for_task,
    group_homogeneous,
    task_nonce,
    task_nonces,
)
from mapi_server.ratelimit import TokenBucket, WindowCounter
from mapi_server.settings import ExecutorName, Settings
from mapi_spec._types import Duration
from mapi_spec.admin import ExecutorStatus
from mapi_spec.errors import ErrorCode
from mapi_spec.executor import PLUGIN_LABEL, ExecState, Isolation, RunKind, RunSubmit
from mapi_spec.resources import ResourceClass
from mapi_spec.run import FailureClass, RunnerExit, failure_class_for_exit

pytestmark = pytest.mark.unit

NONCE = "a" * 64
NONCE2 = "b" * 64
INV = JobInventory(project="mapi-test", region="europe-west1", env="prod")


class FakeClock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


def _rs(
    run_id: str = "run_1",
    *,
    attempt: int = 1,
    timeout: int = 600,
    task_count: int = 1,
    rc: ResourceClass = ResourceClass.M,
    plugin: str | None = "mmm",
) -> RunSubmit:
    labels = {PLUGIN_LABEL: plugin} if plugin else {}
    return RunSubmit(
        run_id=run_id,
        attempt=attempt,
        kind=RunKind.FIT,
        resource_class=rc,
        timeout=Duration(timeout),
        task_count=task_count,
        labels=labels,
    )


def _executor(fake: FakeCloudRun, **kw: Any) -> CloudRunJobsExecutor:
    kw.setdefault("jitter", lambda: 0.0)
    kw.setdefault("submits_per_minute", 180.0)
    kw.setdefault("burst", 100)
    return CloudRunJobsExecutor(fake, INV, **kw)


async def _until_terminal(ex: CloudRunJobsExecutor, h: Any, timeout: float = 5.0) -> Any:
    async def loop() -> Any:
        while True:
            st = await ex.status(h)
            if st.state.terminal or st.state is ExecState.UNKNOWN:
                return st
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(loop(), timeout)


# ------------------------------------------------------------------- inventory


def test_inventory_is_bases_times_classes_named_by_convention() -> None:
    jobs = INV.jobs()
    assert len(jobs) == 9 and len(set(jobs)) == 9
    assert INV.job_name(Base.BAYES, ResourceClass.M) == (
        "projects/mapi-test/locations/europe-west1/jobs/mapi-prod-base-bayes-m"
    )
    assert INV.select(_rs(plugin="sklearn", rc=ResourceClass.L)).endswith("/mapi-prod-base-ml-l")
    assert INV.select(_rs(plugin="ts", rc=ResourceClass.S)).endswith("/mapi-prod-base-forecast-s")
    assert INV.select(_rs(plugin=None)).endswith("/mapi-prod-base-bayes-m")  # ingest, describe
    assert INV.select(_rs(plugin="unknown")).endswith("/mapi-prod-base-bayes-m")
    with pytest.raises(ValueError, match="no class XL"):
        INV.select(_rs(rc=ResourceClass.XL))
    with pytest.raises(ValueError, match="project id"):
        JobInventory(project="X", region="europe-west1")
    with pytest.raises(ValueError, match="region"):
        JobInventory(project="mapi-test", region="mars")
    with pytest.raises(ValueError, match="template"):
        JobInventory(project="mapi-test", region="europe-west1", template="{nope}")
    with pytest.raises(ValueError, match="job id"):
        JobInventory(project="mapi-test", region="europe-west1", template="Mapi_{base}_{class}")


def test_caps_and_placement() -> None:
    ex = _executor(FakeCloudRun.for_inventory(INV))
    caps = ex.caps()
    assert caps.isolation is Isolation.MICROVM and caps.max_tasks == 10_000
    assert caps.checkpoint_storage and caps.regions == ("europe-west1",)
    assert caps.classes == [ResourceClass.S, ResourceClass.M, ResourceClass.L]
    assert caps.max_runtime.seconds == 24 * 3600
    # DNS enforcement is [verify M0]: shared tiers (verified, public) are refused until it
    # is asserted (mapi_spec.tenancy.TIER_PLACEMENT); the internal tier runs anywhere
    for tier in ("public", "verified"):
        with pytest.raises(PlacementRefused):
            check_placement(caps, trust_tier=tier, resource_class=ResourceClass.S)
    check_placement(caps, trust_tier="internal", resource_class=ResourceClass.S)
    trusted = _executor(FakeCloudRun.for_inventory(INV), dns_enforced=True).caps()
    for tier in ("public", "verified"):
        check_placement(trusted, trust_tier=tier, resource_class=ResourceClass.S)


# ---------------------------------------------------------------------- submit


async def test_submit_overrides_only_env_task_count_and_timeout() -> None:
    fake = FakeCloudRun.for_inventory(INV)
    ex = _executor(fake, timeout_margin_s=120)
    h = await ex.submit(_rs(timeout=600), NONCE)
    assert len(fake.run_job_calls) == 1
    call = fake.run_job_calls[0]
    assert call.job == INV.job_name(Base.BAYES, ResourceClass.M)
    assert dict(call.env) == {"MAPI_RUN_ID": "run_1", "MAPI_NONCE": NONCE}
    assert call.task_count == 1 and call.timeout_s == 720
    assert h.executor == CLOUDRUN_EXECUTOR and h.nonce == NONCE
    assert h.external_id == fake.executions()[0].name
    assert ex.operation_for(NONCE) and ex.operation_for(NONCE2) is None
    # the duplicate submit returns the same execution; nothing new is created
    assert await ex.submit(_rs(timeout=600), NONCE) == h
    assert len(fake.run_job_calls) == 1
    # the timeout is capped at the backend maximum
    await ex.submit(_rs("run_2", timeout=40 * 3600), NONCE2)
    assert fake.run_job_calls[-1].timeout_s == 24 * 3600
    await fake.wait_idle()


def test_run_job_request_shape() -> None:
    req = run_job_request(
        "projects/p/locations/r/jobs/j",
        env={"MAPI_RUN_ID": "r", "MAPI_NONCE": NONCE},
        task_count=4,
        timeout_s=90,
    )
    assert req == {
        "name": "projects/p/locations/r/jobs/j",
        "overrides": {
            "container_overrides": [
                {
                    "env": [
                        {"name": "MAPI_NONCE", "value": NONCE},
                        {"name": "MAPI_RUN_ID", "value": "r"},
                    ]
                }
            ],
            "task_count": 4,
            "timeout": {"seconds": 90},
        },
    }
    assert "args" not in req["overrides"]["container_overrides"][0]


async def test_429_backs_off_exponentially_and_raises_the_alarm() -> None:
    fake = FakeCloudRun.for_inventory(INV)
    fake.rate_limit(3)
    clock = FakeClock()
    ex = _executor(fake, clock=clock, sleep=clock.sleep, backoff_base_s=1.0)
    h = await ex.submit(_rs(), NONCE)
    outcomes = [c.outcome for c in fake.run_job_calls]
    assert outcomes == ["throttled", "throttled", "throttled", "created"]
    assert len(fake.executions()) == 1 and h.external_id == fake.executions()[0].name
    # exponential, shared through the bucket's cool-down, and nothing on top of it
    assert [round(s, 6) for s in clock.sleeps] == [1.0, 2.0, 4.0]
    assert ex.throttled.count() == 3 and ex.throttled.total == 3
    health = ex.health()
    assert health.throttled_last_hour == 3 and health.status is ExecutorStatus.DEGRADED
    assert health.submit_rate_limit_per_minute == 180 and health.submits_last_minute == 4
    await fake.wait_idle()


async def test_retry_after_is_honoured_and_retries_are_capped() -> None:
    fake = FakeCloudRun.for_inventory(INV)
    fake.rate_limit(1, retry_after=7.5)
    clock = FakeClock()
    ex = _executor(fake, clock=clock, sleep=clock.sleep, max_rate_limit_retries=2)
    await ex.submit(_rs(), NONCE)
    assert clock.sleeps == [7.5]
    fake.rate_limit(5)
    with pytest.raises(CloudRunRateLimited):
        await ex.submit(_rs("run_2"), NONCE2)
    assert [c.outcome for c in fake.run_job_calls].count("throttled") == 4
    assert ex.failures.count() == 1
    await fake.wait_idle()


async def test_concurrent_submitters_share_the_cooldown() -> None:
    fake = FakeCloudRun.for_inventory(INV)
    fake.rate_limit(1)
    clock = FakeClock()
    ex = _executor(fake, clock=clock, sleep=clock.sleep)
    nonces = [f"{i:x}" * 64 for i in range(1, 5)]
    handles = await asyncio.gather(
        *(ex.submit(_rs(f"run_{i}"), n[:64]) for i, n in enumerate(nonces))
    )
    assert len({h.external_id for h in handles}) == 4
    first_ok = next(c.at for c in fake.run_job_calls if c.outcome == "created")
    assert first_ok >= fake.run_job_calls[0].at
    await fake.wait_idle()


async def test_token_bucket_keeps_submits_under_the_rate() -> None:
    fake = FakeCloudRun.for_inventory(INV)
    clock = FakeClock()
    ex = _executor(fake, clock=clock, sleep=clock.sleep, submits_per_minute=60.0, burst=2)
    start = clock.t
    for i in range(6):
        await ex.submit(_rs(f"run_{i}"), f"{i:x}" * 64)
    # 2 from the burst, then one per second
    assert clock.t - start == pytest.approx(4.0, abs=0.01)
    await fake.wait_idle()


async def test_lost_response_is_adopted_not_resubmitted() -> None:
    fake = FakeCloudRun.for_inventory(INV)
    fake.lose_response()
    ex = _executor(fake)
    with pytest.raises(CloudRunUnavailable):
        await ex.submit(_rs(), NONCE)
    assert len(fake.executions()) == 1  # it was created anyway
    adopted = await ex.adopt(NONCE)
    assert adopted is not None and adopted.external_id == fake.executions()[0].name
    assert adopted.run_id == "run_1"
    assert await ex.submit(_rs(), NONCE) == adopted  # never a second execution
    assert len(fake.run_job_calls) == 1
    await fake.wait_idle()


async def test_ambiguous_submit_retried_without_adoption_is_resolved_first() -> None:
    fake = FakeCloudRun.for_inventory(INV)
    fake.lose_response()
    ex = _executor(fake)
    with pytest.raises(CloudRunUnavailable):
        await ex.submit(_rs(), NONCE)
    h = await ex.submit(_rs(), NONCE)  # the dispatcher retries: submit adopts first
    assert h.external_id == fake.executions()[0].name and len(fake.run_job_calls) == 1
    await fake.wait_idle()


async def test_restarted_executor_adopts_by_nonce_and_cancels_duplicates() -> None:
    hang_until_term = _hang()
    fake = FakeCloudRun.for_inventory(INV, workload=hang_until_term, cancel_grace_s=0.05)
    first = _executor(fake)
    h = await first.submit(_rs(attempt=2), NONCE)
    # a second execution of the same nonce (e.g. two dispatchers raced): must not survive
    job = INV.job_name(Base.BAYES, ResourceClass.M)
    await asyncio.sleep(0.01)
    await fake.run_job(
        job, env={"MAPI_RUN_ID": "run_1", "MAPI_NONCE": NONCE}, task_count=1, timeout_s=60
    )
    restarted = _executor(fake)
    adopted = await restarted.adopt(NONCE)
    assert adopted is not None and adopted.external_id == h.external_id
    assert adopted.attempt == 1  # unknown to the backend; the reconciler restores it
    assert restarted.duplicates_cancelled == 1
    assert fake.api_calls["list_executions"] == 9  # one listing per inventory Job
    assert await restarted.adopt("c" * 64) is None
    assert fake.api_calls["list_executions"] == 9  # the index is cached for a moment
    dup = fake.executions()[1]
    await asyncio.sleep(0.2)
    assert (await restarted.status(adopted.model_copy(update={"external_id": dup.name}))).state in (
        ExecState.CANCELLED,
        ExecState.RUNNING,
    )
    await restarted.cancel(adopted)
    st = await _until_terminal(restarted, adopted)
    assert st.state is ExecState.CANCELLED
    await fake.aclose()


async def test_adopt_ignores_old_and_unknown_jobs() -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    fake = FakeCloudRun.for_inventory(INV, now=lambda: old)
    ex = _executor(FakeCloudRun.for_inventory(INV))
    await _executor(fake).submit(_rs(), NONCE)
    fake.jobs.discard(INV.jobs()[0])  # a Job that is not deployed is skipped, not an error
    ex.api = fake
    assert await ex.adopt(NONCE) is None  # created long before the lookback window
    await fake.wait_idle()


# ----------------------------------------------------------------------- status


def _hang(exit_on_term: int = 143) -> Callable[[FakeTask], Any]:
    async def workload(task: FakeTask) -> int:
        await task.sigterm.wait()
        return exit_on_term

    return workload


@pytest.mark.parametrize(
    ("workload_exit", "state", "code", "fc"),
    [
        (0, ExecState.SUCCEEDED, 0, None),
        (10, ExecState.FAILED, 10, FailureClass.USER),
        (16, ExecState.FAILED, 16, None),
        (12, ExecState.FAILED, 12, FailureClass.INFRA),
    ],
)
async def test_status_reports_exit_codes(
    workload_exit: int, state: ExecState, code: int, fc: FailureClass | None
) -> None:
    async def workload(task: FakeTask) -> int:
        await asyncio.sleep(0.02)
        return workload_exit

    fake = FakeCloudRun.for_inventory(INV, workload=workload, latency=FakeLatency(provision_s=0.02))
    ex = _executor(fake)
    h = await ex.submit(_rs(), NONCE)
    assert (await ex.status(h)).state is ExecState.PENDING  # provisioning
    st = await _until_terminal(ex, h)
    assert st.state is state and st.exit_code == code
    assert failure_class_for_exit(code) is fc
    assert st.started_at is not None and st.finished_at is not None
    calls = fake.api_calls["list_tasks"]
    assert await ex.status(h) == st  # terminal statuses are cached
    assert fake.api_calls["list_tasks"] == calls


async def test_oom_is_resource_exhausted() -> None:
    async def bomb(task: FakeTask) -> int:
        raise FakeOOM

    fake = FakeCloudRun.for_inventory(INV, workload=bomb)
    ex = _executor(fake)
    h = await ex.submit(_rs(), NONCE)
    st = await _until_terminal(ex, h)
    assert st.state is ExecState.FAILED and st.exit_code == 137
    assert failure_class_for_exit(137) is FailureClass.RESOURCE_EXHAUSTED
    assert st.problem is not None and st.problem.code is ErrorCode.RESOURCE_EXHAUSTED
    assert st.message is not None and "Memory limit" in st.message


async def test_task_timeout_is_a_timeout_failure() -> None:
    fake = FakeCloudRun.for_inventory(INV, workload=_hang(), cancel_grace_s=0.05)
    ex = _executor(fake, timeout_margin_s=0)
    h = await ex.submit(_rs(timeout=1), NONCE)
    st = await _until_terminal(ex, h, timeout=5)
    # the supervisor handled SIGTERM itself (143): a timeout, never OOM
    assert st.state is ExecState.FAILED and st.exit_code == 143
    assert failure_class_for_exit(143) is FailureClass.TIMEOUT


async def test_cancel_sigterm_then_kill() -> None:
    fake = FakeCloudRun.for_inventory(INV, workload=_hang(16), cancel_grace_s=0.05)
    ex = _executor(fake)
    h = await ex.submit(_rs(), NONCE)
    await asyncio.sleep(0.02)
    assert (await ex.status(h)).state is ExecState.RUNNING
    await ex.cancel(h)
    await ex.cancel(h)  # idempotent: not re-sent within the resend window
    assert fake.api_calls["cancel_execution"] == 1
    st = await _until_terminal(ex, h)
    assert st.state is ExecState.CANCELLED and st.exit_code == 16  # flushed a partial bundle
    await ex.cancel(h)  # already finished: nothing to do
    assert fake.api_calls["cancel_execution"] == 1

    async def stubborn(task: FakeTask) -> int:
        await asyncio.sleep(3600)
        return 0

    fake.workload = stubborn
    h2 = await ex.submit(_rs("run_2"), NONCE2)
    await asyncio.sleep(0.02)
    await ex.cancel(h2)
    st2 = await _until_terminal(ex, h2)
    assert st2.state is ExecState.CANCELLED and st2.exit_code == 137  # SIGKILL after grace


async def test_cancel_while_provisioning_runs_nothing() -> None:
    ran: list[int] = []

    async def workload(task: FakeTask) -> int:
        ran.append(task.index)
        return 0

    fake = FakeCloudRun.for_inventory(INV, workload=workload, latency=FakeLatency(provision_s=0.05))
    ex = _executor(fake)
    h = await ex.submit(_rs(), NONCE)
    await ex.cancel(h)
    st = await _until_terminal(ex, h)
    assert st.state is ExecState.CANCELLED and ran == []


async def test_missing_execution_is_unknown_and_errors_propagate() -> None:
    fake = FakeCloudRun.for_inventory(INV, workload=_hang())
    ex = _executor(fake)
    h = await ex.submit(_rs(), NONCE)
    fake.hide_execution(str(h.external_id))
    assert (await ex.status(h)).state is ExecState.UNKNOWN
    assert (await ex.status(h)).state in (ExecState.RUNNING, ExecState.PENDING)
    fake.fail_api("get_execution", CloudRunUnavailable("503"))
    with pytest.raises(CloudRunUnavailable):  # transient: the reconciler retries next pass
        await ex.status(h)
    nothing = h.model_copy(update={"external_id": None, "nonce": NONCE2})
    assert (await ex.status(nothing)).state is ExecState.UNKNOWN
    await ex.cancel(nothing)  # no execution: nothing to cancel
    await fake.aclose()


async def test_event_source_is_silent_until_closed() -> None:
    ex = _executor(FakeCloudRun.for_inventory(INV))
    source = ex.event_source()
    nxt = asyncio.ensure_future(source.__anext__())
    await asyncio.sleep(0.02)
    assert not nxt.done()
    await ex.aclose()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(nxt, 1)


# ---------------------------------------------------------------------- fan-out


async def test_fanout_runs_task_count_tasks_in_one_execution() -> None:
    seen: list[tuple[int, int, str]] = []

    async def workload(task: FakeTask) -> int:
        env = TaskEnv.from_environ(task.env)
        seen.append((env.index, env.count, env.bootstrap_nonce()))
        await asyncio.sleep(0.01)
        return 0

    fake = FakeCloudRun.for_inventory(INV, workload=workload)
    ex = _executor(fake)
    h = await ex.submit(_rs(task_count=4), NONCE)
    st = await _until_terminal(ex, h)
    assert st.state is ExecState.SUCCEEDED and len(fake.executions()) == 1
    assert sorted(i for i, _, _ in seen) == [0, 1, 2, 3] and {c for _, c, _ in seen} == {4}
    assert sorted(n for _, _, n in seen) == sorted(task_nonces(NONCE, 4))
    assert fake.run_job_calls[0].task_count == 4


async def test_fanout_failure_and_cancel_cover_every_task() -> None:
    async def one_fails(task: FakeTask) -> int:
        return 10 if task.index == 2 else 0

    fake = FakeCloudRun.for_inventory(INV, workload=one_fails)
    ex = _executor(fake)
    h = await ex.submit(_rs(task_count=3), NONCE)
    st = await _until_terminal(ex, h)
    assert st.state is ExecState.FAILED and st.exit_code == 10
    stopped: list[int] = []

    async def hang(task: FakeTask) -> int:
        await task.sigterm.wait()
        stopped.append(task.index)
        return 16

    fake.workload = hang
    h2 = await ex.submit(_rs("run_2", task_count=3), NONCE2)
    await asyncio.sleep(0.02)
    await ex.cancel(h2)
    st2 = await _until_terminal(ex, h2)
    assert st2.state is ExecState.CANCELLED and sorted(stopped) == [0, 1, 2]
    with pytest.raises(ValueError, match="task_count"):
        await ex.submit(
            RunSubmit.model_construct(**{**_rs().model_dump(), "task_count": 20_000}), "e" * 64
        )


def test_task_helpers() -> None:
    assert task_nonce(NONCE, 0, 1) == NONCE  # a single task keeps the execution nonce
    nonces = task_nonces(NONCE, 5)
    assert len(set(nonces)) == 5 and all(len(n) == 64 for n in nonces)
    assert nonces == task_nonces(NONCE, 5)  # deterministic
    with pytest.raises(ValueError):
        task_nonce(NONCE, 5, 5)
    with pytest.raises(ValueError):
        task_nonce("xyz", 0, 2)
    env = TaskEnv.from_environ({"MAPI_NONCE": NONCE, ENV_TASK_INDEX: "2", ENV_TASK_COUNT: "3"})
    assert (env.index, env.count) == (2, 3) and env.bootstrap_nonce() == task_nonce(NONCE, 2, 3)
    assert TaskEnv.from_environ({"MAPI_NONCE": NONCE}).count == 1
    for bad in ({ENV_TASK_INDEX: "3", ENV_TASK_COUNT: "3"}, {ENV_TASK_INDEX: "x"}):
        with pytest.raises(ValueError):
            TaskEnv.from_environ({"MAPI_NONCE": NONCE, **bad})
    assert child_for_task(["a", "b", "c"], env) == "c"
    with pytest.raises(ValueError, match="children"):
        child_for_task(["a"], env)
    subs = [_rs(f"r{i}", plugin="ts" if i % 2 else "mmm") for i in range(5)]
    batches = group_homogeneous(subs, batch_key, max_tasks=2)
    assert [[s.run_id for s in b] for b in batches] == [["r0", "r2"], ["r1", "r3"], ["r4"]]
    with pytest.raises(ValueError):
        group_homogeneous(subs, batch_key, max_tasks=0)


def test_aggregate_exit_code_prefers_real_failures() -> None:
    assert aggregate_exit_code([16, 10, None]) == 10
    assert aggregate_exit_code([16, None]) == 16
    assert aggregate_exit_code([None]) is None
    assert aggregate_exit_code([]) is None


# --------------------------------------------------------------- status mapping


def _info(**kw: Any) -> ExecutionInfo:
    base: dict[str, Any] = {"name": "projects/p/locations/r/jobs/j/executions/j-1", "job": "j"}
    return ExecutionInfo(**{**base, **kw})


def test_execution_status_mapping_table() -> None:
    t0 = datetime(2026, 10, 15, tzinfo=UTC)
    assert execution_status(_info()).state is ExecState.PENDING
    running = execution_status(_info(start_time=t0, running_count=1))
    assert running.state is ExecState.RUNNING and running.started_at == t0
    cancelling = _info(start_time=t0, conditions=(ConditionInfo("Completed", "X", "CANCELLING"),))
    assert execution_status(cancelling).message == "cancelling"
    assert execution_status(_info(delete_time=t0)).state is ExecState.UNKNOWN
    done = {"start_time": t0, "completion_time": t0 + timedelta(seconds=5)}
    ok = execution_status(_info(**done, succeeded_count=1))
    assert ok.state is ExecState.SUCCEEDED and ok.exit_code == 0
    no_tasks = execution_status(_info(**done, failed_count=1))
    assert no_tasks.state is ExecState.FAILED and no_tasks.exit_code is None
    msg = execution_status(
        _info(
            **done,
            failed_count=1,
            conditions=(ConditionInfo("Completed", "CONDITION_FAILED", "", "boom"),),
        ),
        [TaskInfo(name="t", index=0, exit_code=10, succeeded=False)],
    )
    assert msg.exit_code == 10 and msg.message == "boom"


def test_task_exit_code_classification() -> None:
    def t(**kw: Any) -> TaskInfo:
        return TaskInfo(name="t", index=0, **kw)

    assert task_exit_code(t(exit_code=10, succeeded=False)) == 10
    assert task_exit_code(t(exit_code=0, term_signal=9, message="Memory limit exceeded")) == 137
    assert task_exit_code(t(term_signal=9, message="The task timed out")) == 143
    assert task_exit_code(t(term_signal=15)) == 143
    assert task_exit_code(t(exit_code=0, succeeded=False)) is None  # never started
    assert task_exit_code(t(exit_code=0, succeeded=True)) == 0
    assert failure_class_for_exit(int(RunnerExit.KILLED)) is FailureClass.RESOURCE_EXHAUSTED


def test_same_execution_accepts_full_and_short_names() -> None:
    full = "projects/p/locations/r/jobs/j/executions/j-x7k2p"
    assert same_execution(full, "j-x7k2p") and same_execution(full, full)
    assert same_execution(None, "x") and same_execution(full, None)
    assert not same_execution(full, "j-other")


# -------------------------------------------------------------------- adapter


def _ns_execution(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "name": "projects/p/locations/r/jobs/j/executions/j-1",
        "job": "",
        "template": SimpleNamespace(
            containers=[
                SimpleNamespace(
                    env=[
                        SimpleNamespace(name="MAPI_NONCE", value=NONCE),
                        SimpleNamespace(name="MAPI_RUN_ID", value="run_1"),
                    ]
                )
            ]
        ),
        "task_count": 2,
        "create_time": datetime(2026, 10, 15, tzinfo=UTC),
        "start_time": None,
        "completion_time": None,
        "delete_time": None,
        "running_count": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "cancelled_count": 0,
        "conditions": [
            SimpleNamespace(
                type_="Completed",
                state=SimpleNamespace(name="CONDITION_FAILED"),
                execution_reason=SimpleNamespace(name="CANCELLED"),
                reason=SimpleNamespace(name="COMMON_REASON_UNDEFINED"),
                message="cancelled by user",
            )
        ],
    }
    return SimpleNamespace(**{**base, **kw})


def test_execution_and_task_conversion_from_doubles() -> None:
    info = execution_info(_ns_execution())
    assert info.env == {"MAPI_NONCE": NONCE, "MAPI_RUN_ID": "run_1"} and info.nonce == NONCE
    assert info.job == "projects/p/locations/r/jobs/j" and info.task_count == 2
    assert info.conditions == (
        ConditionInfo("Completed", "CONDITION_FAILED", "CANCELLED", "cancelled by user"),
    )
    assert info.short_name == "j-1"
    task = task_info(
        SimpleNamespace(
            name="t",
            index=3,
            last_attempt_result=SimpleNamespace(
                exit_code=0, term_signal=9, status=SimpleNamespace(message="Memory limit hit")
            ),
            conditions=[],
            completion_time=datetime(2026, 10, 15, tzinfo=UTC),
        )
    )
    assert task.index == 3 and task.term_signal == 9 and task.succeeded is False
    assert task_exit_code(task) == 137
    never_ran = task_info(SimpleNamespace(name="t", index=0, last_attempt_result=None))
    assert never_ran.exit_code is None and never_ran.succeeded is None


def test_real_google_types_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    run_v2 = pytest.importorskip("google.cloud.run_v2")
    run_v2.RunJobRequest(
        run_job_request("projects/p/locations/r/jobs/j", env={"A": "1"}, task_count=2, timeout_s=9)
    )  # the dict is a valid RunJobRequest (unknown fields would raise)
    e = run_v2.Execution(
        name="projects/p/locations/r/jobs/j/executions/j-abc",
        task_count=2,
        failed_count=1,
        template={"containers": [{"env": [{"name": "MAPI_NONCE", "value": NONCE}]}]},
        conditions=[
            {"type_": "Completed", "state": "CONDITION_FAILED", "execution_reason": "CANCELLED"}
        ],
    )
    info = execution_info(e)
    assert info.nonce == NONCE and info.start_time is None and info.failed_count == 1
    assert info.conditions[0].reason == "CANCELLED" and info.conditions[0].state.endswith("FAILED")
    t = task_info(run_v2.Task(name="t", index=1, last_attempt_result={"exit_code": 16}))
    assert t.exit_code == 16 and t.term_signal is None
    unset = task_info(run_v2.Task(name="t", index=0))
    assert unset.exit_code is None  # proto3 would read 0: presence is checked


class _Pager:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for item in self._items:
                yield item

        return gen()


class _Clients:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.metadata: Any = _ns_execution()
        self.error: BaseException | None = None

    async def run_job(self, *, request: dict[str, Any], timeout: float) -> Any:
        self.requests.append(("run_job", request))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            metadata=self.metadata, operation=SimpleNamespace(name="operations/op-1")
        )

    async def get_execution(self, *, request: dict[str, Any], timeout: float) -> Any:
        self.requests.append(("get_execution", request))
        if self.error is not None:
            raise self.error
        return _ns_execution()

    async def list_executions(self, *, request: dict[str, Any], timeout: float) -> Any:
        self.requests.append(("list_executions", request))
        return _Pager([_ns_execution(), _ns_execution(), _ns_execution()])

    async def list_tasks(self, *, request: dict[str, Any], timeout: float) -> Any:
        self.requests.append(("list_tasks", request))
        return _Pager([SimpleNamespace(name="t", index=0, last_attempt_result=None)])

    async def cancel_execution(self, *, request: dict[str, Any], timeout: float) -> Any:
        self.requests.append(("cancel_execution", request))
        if self.error is not None:
            raise self.error
        return SimpleNamespace()


class _ApiError(Exception):
    """Looks like a ``google.api_core`` error: an HTTP ``code`` and a gRPC status name."""

    _GRPC: ClassVar[dict[str, str]] = {
        "FailedPrecondition": "FAILED_PRECONDITION",
        "PermissionDenied": "PERMISSION_DENIED",
    }

    def __init__(self, code: int, name: str = "X", headers: dict[str, str] | None = None) -> None:
        super().__init__(name)
        self.code = code
        self.grpc_status_code = SimpleNamespace(name=self._GRPC.get(name, "UNKNOWN_TEST"))
        self.response = SimpleNamespace(headers=headers or {})


async def test_google_adapter_with_doubles() -> None:
    c = _Clients()
    api = GoogleCloudRunApi(jobs_client=c, executions_client=c, tasks_client=c)
    result = await api.run_job(
        "projects/p/locations/r/jobs/j", env={"A": "1"}, task_count=1, timeout_s=5
    )
    assert result.operation == "operations/op-1" and result.execution.nonce == NONCE
    assert c.requests[0][1]["overrides"]["timeout"] == {"seconds": 5}
    assert len(await api.list_executions("projects/p/locations/r/jobs/j", limit=2)) == 2
    assert (await api.get_execution("x")).task_count == 2
    assert [t.index for t in await api.list_tasks("x")] == [0]
    await api.cancel_execution("x")
    c.metadata = None  # the operation did not say which execution: ambiguous
    with pytest.raises(CloudRunUnavailable):
        await api.run_job("j", env={}, task_count=1, timeout_s=5)
    c.error = _ApiError(429, "TooManyRequests", {"Retry-After": "12"})
    with pytest.raises(CloudRunRateLimited) as rl:
        await api.run_job("j", env={}, task_count=1, timeout_s=5)
    assert rl.value.retry_after == 12.0
    c.error = _ApiError(404, "NotFound")
    with pytest.raises(CloudRunNotFound):
        await api.get_execution("x")
    c.error = _ApiError(400, "FailedPrecondition")
    with pytest.raises(CloudRunFailedPrecondition):
        await api.cancel_execution("x")


@pytest.mark.parametrize(
    ("exc", "cls"),
    [
        (_ApiError(429, "ResourceExhausted"), CloudRunRateLimited),
        (_ApiError(404, "NotFound"), CloudRunNotFound),
        (_ApiError(503, "ServiceUnavailable"), CloudRunUnavailable),
        (_ApiError(504, "DeadlineExceeded"), CloudRunUnavailable),
        (TimeoutError("t"), CloudRunUnavailable),
        (_ApiError(403, "PermissionDenied"), CloudRunError),
    ],
)
def test_map_api_error(exc: BaseException, cls: type[CloudRunError]) -> None:
    mapped = map_api_error(exc, "jobs.run")
    assert type(mapped) is cls and "jobs.run" in str(mapped)


def test_map_api_error_reads_retry_info_details() -> None:
    exc = _ApiError(429, "TooManyRequests")
    exc.response = SimpleNamespace(headers={})
    exc.details = [SimpleNamespace(retry_delay=SimpleNamespace(seconds=3, nanos=500_000_000))]  # type: ignore[attr-defined]
    mapped = map_api_error(exc, "jobs.run")
    assert isinstance(mapped, CloudRunRateLimited) and mapped.retry_after == 3.5


def test_real_google_exceptions_map(monkeypatch: pytest.MonkeyPatch) -> None:
    exceptions = pytest.importorskip("google.api_core.exceptions")
    assert isinstance(map_api_error(exceptions.ResourceExhausted("q"), "x"), CloudRunRateLimited)
    assert isinstance(map_api_error(exceptions.TooManyRequests("q"), "x"), CloudRunRateLimited)
    assert isinstance(map_api_error(exceptions.NotFound("q"), "x"), CloudRunNotFound)
    assert isinstance(
        map_api_error(exceptions.FailedPrecondition("q"), "x"), CloudRunFailedPrecondition
    )
    assert isinstance(map_api_error(exceptions.ServiceUnavailable("q"), "x"), CloudRunUnavailable)


# --------------------------------------------------------------------- settings


def test_build_from_settings(tmp_path: Path) -> None:
    common: dict[str, Any] = {
        "database_url": "postgresql://x/y",
        "executor": ExecutorName.CLOUDRUN_JOBS,
        "data_dir": tmp_path,
        "env": "prod",
    }
    with pytest.raises(ExecutorRefused, match="MAPI_CLOUDRUN_PROJECT"):
        build_executor(Settings(**common))
    settings = Settings(
        **common,
        cloudrun_project="mapi-exec-paid",
        cloudrun_region="us-central1",
        cloudrun_submits_per_minute=120,
        cloudrun_dns_enforced=True,
    )
    fake = FakeCloudRun()
    ex = build_cloudrun_executor(settings, api=fake)
    assert ex.inventory.project == "mapi-exec-paid" and ex.caps().allows_public_tenants()
    assert ex.inventory.jobs()[0].startswith("projects/mapi-exec-paid/locations/us-central1/")
    assert ex.bucket.per_minute == pytest.approx(120)
    with pytest.raises(ValueError):
        Settings(**common, cloudrun_submits_per_minute=500)  # above the regional quota


# -------------------------------------------------------------------- ratelimit


async def test_token_bucket_penalize_and_window_counter() -> None:
    clock = FakeClock()
    b = TokenBucket(1.0, 2, clock=clock, sleep=clock.sleep)
    assert b.try_acquire() and b.try_acquire() and not b.try_acquire()
    b.penalize(5.0)
    assert b.cooldown_left() == 5.0 and not b.try_acquire()
    b.penalize(1.0)  # never shortens an active cool-down
    assert b.cooldown_left() == 5.0
    await b.acquire()
    assert clock.t >= 1005.0
    b.penalize(0)
    with pytest.raises(ValueError):
        TokenBucket(0, 1)
    w = WindowCounter(10.0, clock=clock)
    w.record()
    clock.t += 5
    w.record()
    assert w.count() == 2
    clock.t += 6
    assert w.count() == 1 and w.total == 2
    with pytest.raises(ValueError):
        WindowCounter(0)


async def test_bookkeeping_stays_bounded_in_a_long_lived_worker() -> None:
    """Submitted and cancelled executions are forgotten once they finish (a bounded record
    still answers duplicate submits), and anything never polled here is dropped after the
    adopt lookback: memory and health() cost do not grow with every run since start."""
    fake = FakeCloudRun.for_inventory(INV)
    clock = [0.0]
    ex = _executor(fake, clock=lambda: clock[0], adopt_lookback_s=3600)
    handles = [await ex.submit(_rs(f"run_{i}"), f"{i:064x}") for i in range(20)]
    for h in handles[:10]:
        await ex.cancel(h)
        assert (await _until_terminal(ex, h)).state.terminal
    assert len(ex._submitted) == 10 and len(ex._cancel_sent) == 0
    assert ex.health().running_executions == 10
    created = len(fake.executions())
    # a duplicate submit of a finished nonce is still the same execution
    assert await ex.submit(_rs("run_0"), f"{0:064x}") == handles[0]
    assert len(fake.executions()) == created
    for h in handles[10:]:
        await ex.cancel(h)
    assert len(ex._cancel_sent) == 10
    clock[0] += 3601  # never polled by this process: dropped after the adopt lookback
    assert ex.health().running_executions == 0
    assert ex._submitted == {} and ex._cancel_sent == {}
    await fake.aclose()
