"""``FakeCloudRun``: an in-memory Cloud Run Admin API for tests and local experiments.

It implements :class:`~mapi_server.executors.cloudrun.CloudRunApi` with the behaviour the
executor relies on — executions with generated names, env overrides visible in the execution
template, ``taskCount`` tasks with ``CLOUD_RUN_TASK_INDEX``, task timeouts, cancel (SIGTERM,
then SIGKILL after a grace period), memory-limit kills (exit 137) — and the faults the
conformance suite injects: 429s with or without ``Retry-After``, lost ``jobs.run`` responses
(the execution exists but the caller never hears about it: an orphan), phantom ``NotFound``
from ``executions.get`` and arbitrary API errors.

Each task runs a **workload** coroutine (default: exit 0 at once) that receives the task's
environment, a SIGTERM event and a ``print`` that writes a stdout line. Lines go through the
Cloud Logging sink filter (``jsonPayload.mapi_event``) to an optional ``log_sink`` as Cloud
Logging ``LogEntry`` dicts — what the Pub/Sub push to ``/internal/events`` carries.

Deterministic: execution names and latency jitter come from a seeded RNG; time is the event
loop's clock plus an injectable wall clock for timestamps.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mapi_spec.run import ENV_NONCE, ENV_RUN_ID, STDOUT_EVENT_KEY

from mapi_server.executors.cloudrun import (
    EXECUTION_NAME_LABEL,
    CloudRunError,
    CloudRunFailedPrecondition,
    CloudRunNotFound,
    CloudRunRateLimited,
    CloudRunUnavailable,
    ConditionInfo,
    ExecutionInfo,
    JobInventory,
    RunJobResult,
    TaskInfo,
    short_execution_name,
)
from mapi_server.executors.tasks import ENV_TASK_ATTEMPT, ENV_TASK_COUNT, ENV_TASK_INDEX

__all__ = [
    "FakeCloudRun",
    "FakeLatency",
    "FakeOOM",
    "FakeTask",
    "LogSink",
    "RunJobCall",
    "Workload",
    "log_entry",
]

LogSink = Callable[[dict[str, Any]], Awaitable[None]]
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
MEMORY_LIMIT_MESSAGE = (
    "Memory limit of 8192 MiB exceeded with 8200 MiB used. Consider increasing the memory limit."
)
TIMEOUT_MESSAGE = "The configured timeout was reached and the task timed out (killed)."
CANCEL_KILL_MESSAGE = "The task was cancelled and killed after the grace period."


class FakeOOM(Exception):
    """Raised by a workload to simulate the container's memory limit being hit (exit 137)."""


@dataclass
class FakeTask:
    """What a workload sees: one task of one execution."""

    execution: str
    job: str
    index: int
    count: int
    env: Mapping[str, str]
    sigterm: asyncio.Event
    _print: Callable[[str], Awaitable[None]]

    @property
    def run_id(self) -> str:
        return self.env[ENV_RUN_ID]

    @property
    def nonce(self) -> str:
        return self.env[ENV_NONCE]

    async def print(self, line: str) -> None:
        """Write one stdout line (Cloud Logging picks it up)."""
        await self._print(line)

    async def wait_sigterm(self, timeout: float | None = None) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.sigterm.wait(), timeout)
        return self.sigterm.is_set()


Workload = Callable[[FakeTask], Coroutine[Any, Any, int]]


async def _exit_zero(task: FakeTask) -> int:
    return 0


@dataclass
class FakeLatency:
    """Simulated delays (seconds). ``provision_jitter_s`` adds a seeded uniform extra."""

    run_job_s: float = 0.0
    api_s: float = 0.0
    provision_s: float = 0.0
    provision_jitter_s: float = 0.0


@dataclass(frozen=True)
class RunJobCall:
    at: float
    """Event-loop time of the call."""
    job: str
    env: Mapping[str, str]
    task_count: int
    timeout_s: int
    outcome: str
    """``created`` | ``throttled`` | ``failed`` | ``lost`` (created, response lost)."""


@dataclass
class _Task:
    index: int
    sigterm: asyncio.Event = field(default_factory=asyncio.Event)
    started: bool = False
    done: bool = False
    exit_code: int | None = None
    term_signal: int | None = None
    message: str = ""
    killed: bool = False
    work: asyncio.Task[int] | None = None


@dataclass
class _Execution:
    name: str
    job: str
    env: dict[str, str]
    task_count: int
    timeout_s: int
    create_time: datetime
    tasks: list[_Task]
    start_time: datetime | None = None
    completion_time: datetime | None = None
    cancel_requested: bool = False
    runner: asyncio.Task[None] | None = None
    log_seq: int = 0

    @property
    def run_id(self) -> str:
        return self.env.get(ENV_RUN_ID, "")

    @property
    def nonce(self) -> str:
        return self.env.get(ENV_NONCE, "")


def log_entry(
    *,
    project: str,
    region: str,
    job: str,
    execution: str,
    task_index: int,
    line: str,
    insert_id: str,
    timestamp: datetime,
) -> dict[str, Any]:
    """The Cloud Logging ``LogEntry`` a Cloud Run Jobs stdout line becomes."""
    ts = timestamp.isoformat().replace("+00:00", "Z")
    entry: dict[str, Any] = {
        "insertId": insert_id,
        "logName": f"projects/{project}/logs/run.googleapis.com%2Fstdout",
        "resource": {
            "type": "cloud_run_job",
            "labels": {
                "project_id": project,
                "job_name": job.rsplit("/", 1)[-1],
                "location": region,
            },
        },
        "labels": {
            EXECUTION_NAME_LABEL: short_execution_name(execution),
            "run.googleapis.com/task_index": str(task_index),
            "run.googleapis.com/task_attempt": "0",
        },
        "timestamp": ts,
        "receiveTimestamp": ts,
        "severity": "DEFAULT",
    }
    try:
        parsed = json.loads(line)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        entry["jsonPayload"] = parsed
    else:
        entry["textPayload"] = line
    return entry


class FakeCloudRun:
    """In-memory :class:`~mapi_server.executors.cloudrun.CloudRunApi` (see module doc)."""

    def __init__(
        self,
        *,
        project: str = "mapi-test",
        region: str = "europe-west1",
        jobs: Iterable[str] = (),
        job_env: Mapping[str, str] | None = None,
        workload: Workload | None = None,
        log_sink: LogSink | None = None,
        latency: FakeLatency | None = None,
        seed: int = 0,
        cancel_grace_s: float = 1.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.project = project
        self.region = region
        self.jobs: set[str] = set(jobs)
        self.job_env = dict(job_env or {})
        self.workload: Workload = workload or _exit_zero
        self.log_sink = log_sink
        self.latency = latency or FakeLatency()
        self.cancel_grace_s = cancel_grace_s
        self._now = now
        self._rng = random.Random(seed)
        self._executions: dict[str, _Execution] = {}
        self._order: list[str] = []
        # fault injection
        self._rate_limits: deque[float | None] = deque()
        self._run_failures: deque[CloudRunError] = deque()
        self._lost_responses = 0
        self._hidden: Counter[str] = Counter()
        self._lost: set[str] = set()
        self._api_failures: dict[str, deque[CloudRunError]] = {}
        # inspection
        self.run_job_calls: list[RunJobCall] = []
        self.api_calls: Counter[str] = Counter()
        self.live_by_run: Counter[str] = Counter()
        self.max_live_by_run: Counter[str] = Counter()
        self.max_live = 0
        self.log_entries = 0
        self.filtered_log_lines = 0
        self.workload_errors: list[BaseException] = []

    @classmethod
    def for_inventory(cls, inventory: JobInventory, **kwargs: Any) -> FakeCloudRun:
        """A fake whose deployed Jobs are exactly ``inventory``'s."""
        return cls(
            project=inventory.project, region=inventory.region, jobs=inventory.jobs(), **kwargs
        )

    # ------------------------------------------------------------- fault injection
    def rate_limit(self, n: int = 1, *, retry_after: float | None = None) -> None:
        """Answer the next ``n`` ``jobs.run`` calls with 429 (nothing is created)."""
        self._rate_limits.extend([retry_after] * n)

    def fail_run_job(self, exc: CloudRunError, n: int = 1) -> None:
        """Fail the next ``n`` ``jobs.run`` calls with ``exc`` (nothing is created)."""
        self._run_failures.extend([exc] * n)

    def lose_response(self, n: int = 1) -> None:
        """The next ``n`` ``jobs.run`` calls create the execution, then fail as unavailable."""
        self._lost_responses += n

    def lose_execution(self, name: str) -> None:
        """The execution's host dies: its tasks stop at once (no SIGTERM, no exit code) and
        Cloud Run forgets it (``executions.get`` answers ``NotFound``; it is not listed)."""
        ex = self._executions[name]
        self._lost.add(name)
        for t in ex.tasks:
            if t.work is not None and not t.work.done():
                t.killed = True
                t.message = "the host was lost (fake)"
                t.work.cancel()

    def hide_execution(self, name: str, calls: int = 1) -> None:
        """``executions.get`` answers ``NotFound`` for ``name`` for the next ``calls`` calls."""
        self._hidden[name] += calls

    def fail_api(self, method: str, exc: CloudRunError, n: int = 1) -> None:
        """Fail the next ``n`` calls of ``method`` (``get_execution``, ``list_tasks``, ...)."""
        self._api_failures.setdefault(method, deque()).extend([exc] * n)

    # --------------------------------------------------------------- inspection
    def executions(self) -> list[ExecutionInfo]:
        """Every execution, oldest first."""
        return [self._info(self._executions[n]) for n in self._order]

    def executions_for_run(self, run_id: str) -> list[ExecutionInfo]:
        return [e for e in self.executions() if e.env.get(ENV_RUN_ID) == run_id]

    def executions_for_nonce(self, nonce: str) -> list[ExecutionInfo]:
        return [e for e in self.executions() if e.nonce == nonce]

    def live(self) -> int:
        return sum(1 for e in self._executions.values() if e.completion_time is None)

    async def wait_idle(self, timeout: float = 30.0) -> None:
        """Wait until every execution has finished."""
        runners = [e.runner for e in self._executions.values() if e.runner is not None]
        if runners:
            await asyncio.wait_for(asyncio.gather(*runners, return_exceptions=True), timeout)

    async def aclose(self) -> None:
        for ex in self._executions.values():
            for t in ex.tasks:
                if t.work is not None and not t.work.done():
                    t.work.cancel()
            if ex.runner is not None and not ex.runner.done():
                ex.runner.cancel()
        runners = [e.runner for e in self._executions.values() if e.runner is not None]
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)

    # ------------------------------------------------------------------ helpers
    async def _api(self, method: str) -> None:
        self.api_calls[method] += 1
        await asyncio.sleep(self.latency.api_s)
        failures = self._api_failures.get(method)
        if failures:
            raise failures.popleft()

    def _new_name(self, job: str) -> str:
        job_id = job.rsplit("/", 1)[-1]
        while True:
            suffix = "".join(self._rng.choice(_ALPHABET) for _ in range(5))
            name = f"{job}/executions/{job_id}-{suffix}"
            if name not in self._executions:
                return name

    def _info(self, ex: _Execution) -> ExecutionInfo:
        running = sum(1 for t in ex.tasks if t.started and not t.done)
        succeeded = failed = cancelled = 0
        conditions: list[ConditionInfo] = []
        if ex.completion_time is not None:
            for t in ex.tasks:
                ok = t.done and t.exit_code == 0 and not t.killed
                if ok and not ex.cancel_requested:
                    succeeded += 1
                elif ex.cancel_requested:
                    cancelled += 1
                else:
                    failed += 1
            if ex.cancel_requested:
                state, reason = "CONDITION_FAILED", "CANCELLED"
            elif failed:
                state, reason = "CONDITION_FAILED", "NON_ZERO_EXIT_CODE"
            else:
                state, reason = "CONDITION_SUCCEEDED", ""
            message = f"{failed} task(s) failed" if failed else ""
            conditions.append(ConditionInfo("Completed", state, reason, message))
        elif ex.cancel_requested:
            conditions.append(ConditionInfo("Completed", "CONDITION_RECONCILING", "CANCELLING"))
        return ExecutionInfo(
            name=ex.name,
            job=ex.job,
            env=dict(ex.env),
            task_count=ex.task_count,
            create_time=ex.create_time,
            start_time=ex.start_time,
            completion_time=ex.completion_time,
            running_count=running,
            succeeded_count=succeeded,
            failed_count=failed,
            cancelled_count=cancelled,
            conditions=tuple(conditions),
        )

    # ------------------------------------------------------------------- the API
    async def run_job(
        self, job: str, *, env: Mapping[str, str], task_count: int, timeout_s: int
    ) -> RunJobResult:
        loop = asyncio.get_running_loop()
        at = loop.time()
        self.api_calls["run_job"] += 1
        await asyncio.sleep(self.latency.run_job_s)

        def record(outcome: str) -> None:
            self.run_job_calls.append(
                RunJobCall(at, job, dict(env), task_count, timeout_s, outcome)
            )

        if self._rate_limits:
            retry_after = self._rate_limits.popleft()
            record("throttled")
            raise CloudRunRateLimited("429 Too Many Requests (fake)", retry_after=retry_after)
        if self._run_failures:
            record("failed")
            raise self._run_failures.popleft()
        if job not in self.jobs:
            record("failed")
            raise CloudRunNotFound(f"job {job} not found (fake)")
        if task_count < 1 or timeout_s < 1:
            record("failed")
            raise CloudRunError("invalid overrides (fake)", status=400)
        name = self._new_name(job)
        ex = _Execution(
            name=name,
            job=job,
            env=dict(env),
            task_count=task_count,
            timeout_s=timeout_s,
            create_time=self._now(),
            tasks=[_Task(index=i) for i in range(task_count)],
        )
        self._executions[name] = ex
        self._order.append(name)
        ex.runner = asyncio.create_task(self._run(ex), name=f"fake-cloudrun-{name}")
        if self._lost_responses:
            self._lost_responses -= 1
            record("lost")
            raise CloudRunUnavailable("deadline exceeded after the request was sent (fake)")
        record("created")
        op = f"projects/{self.project}/locations/{self.region}/operations/op-{len(self._order)}"
        return RunJobResult(operation=op, execution=self._info(ex))

    async def get_execution(self, name: str) -> ExecutionInfo:
        await self._api("get_execution")
        if self._hidden[name] > 0:
            self._hidden[name] -= 1
            raise CloudRunNotFound(f"execution {name} not found (fake, injected)")
        if name in self._lost:
            raise CloudRunNotFound(f"execution {name} not found (fake, host lost)")
        ex = self._executions.get(name)
        if ex is None:
            raise CloudRunNotFound(f"execution {name} not found (fake)")
        return self._info(ex)

    async def list_executions(self, job: str, *, limit: int) -> list[ExecutionInfo]:
        await self._api("list_executions")
        if job not in self.jobs:
            raise CloudRunNotFound(f"job {job} not found (fake)")
        names = [
            n
            for n in reversed(self._order)
            if self._executions[n].job == job and n not in self._lost
        ]
        return [self._info(self._executions[n]) for n in names[:limit]]

    async def list_tasks(self, execution: str) -> list[TaskInfo]:
        await self._api("list_tasks")
        ex = self._executions.get(execution)
        if ex is None:
            raise CloudRunNotFound(f"execution {execution} not found (fake)")
        out: list[TaskInfo] = []
        for t in ex.tasks:
            succeeded = None
            if t.done:
                succeeded = t.exit_code == 0 and not t.killed
            out.append(
                TaskInfo(
                    name=f"{execution}/tasks/{short_execution_name(execution)}-task{t.index}",
                    index=t.index,
                    exit_code=t.exit_code,
                    term_signal=t.term_signal,
                    succeeded=succeeded,
                    message=t.message,
                )
            )
        return out

    async def cancel_execution(self, name: str) -> None:
        await self._api("cancel_execution")
        ex = self._executions.get(name)
        if ex is None:
            raise CloudRunNotFound(f"execution {name} not found (fake)")
        if ex.completion_time is not None:
            raise CloudRunFailedPrecondition("the execution already finished (fake)", status=400)
        if ex.cancel_requested:
            return
        ex.cancel_requested = True
        for t in ex.tasks:
            if not t.done:
                self._stop(t, CANCEL_KILL_MESSAGE)

    # -------------------------------------------------------------- the backend
    def _stop(self, t: _Task, kill_message: str) -> None:
        """SIGTERM now; SIGKILL after the grace period if the task is still running."""
        t.sigterm.set()

        async def kill_later() -> None:
            await asyncio.sleep(self.cancel_grace_s)
            if t.work is not None and not t.work.done():
                t.killed = True
                t.message = kill_message
                t.work.cancel()

        if t.work is not None:
            asyncio.get_running_loop().create_task(kill_later())

    async def _run(self, ex: _Execution) -> None:
        delay = self.latency.provision_s
        if self.latency.provision_jitter_s:
            delay += self._rng.uniform(0.0, self.latency.provision_jitter_s)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(delay)
        run_id = ex.run_id
        try:
            if ex.cancel_requested:  # cancelled while provisioning: no task ever ran
                for t in ex.tasks:
                    t.done = True
                return
            ex.start_time = self._now()
            self.live_by_run[run_id] += 1
            self.max_live_by_run[run_id] = max(
                self.max_live_by_run[run_id], self.live_by_run[run_id]
            )
            self.max_live = max(self.max_live, self.live())
            await asyncio.gather(*(self._run_task(ex, t) for t in ex.tasks))
            self.live_by_run[run_id] -= 1
        finally:
            ex.completion_time = self._now()

    async def _run_task(self, ex: _Execution, t: _Task) -> None:
        env = {
            **self.job_env,
            **ex.env,
            ENV_TASK_INDEX: str(t.index),
            ENV_TASK_COUNT: str(ex.task_count),
            ENV_TASK_ATTEMPT: "0",
            "CLOUD_RUN_JOB": ex.job.rsplit("/", 1)[-1],
            "CLOUD_RUN_EXECUTION": short_execution_name(ex.name),
        }

        async def emit(line: str) -> None:
            await self._log(ex, t.index, line)

        task = FakeTask(
            execution=ex.name,
            job=ex.job,
            index=t.index,
            count=ex.task_count,
            env=env,
            sigterm=t.sigterm,
            _print=emit,
        )
        t.started = True
        t.work = asyncio.create_task(self.workload(task))
        loop = asyncio.get_running_loop()
        timer = loop.call_later(ex.timeout_s, self._stop, t, TIMEOUT_MESSAGE)
        try:
            t.exit_code = await t.work
        except FakeOOM:
            t.exit_code, t.term_signal, t.message = 137, 9, MEMORY_LIMIT_MESSAGE
        except asyncio.CancelledError:
            if not t.killed:  # the fake itself is shutting down
                t.killed = True
                t.message = CANCEL_KILL_MESSAGE
            t.exit_code, t.term_signal = None, 9
        except Exception as exc:
            self.workload_errors.append(exc)
            t.exit_code, t.message = 1, f"{type(exc).__name__}: {exc}"[:2000]
        finally:
            timer.cancel()
            t.done = True

    async def _log(self, ex: _Execution, index: int, line: str) -> None:
        ex.log_seq += 1
        entry = log_entry(
            project=self.project,
            region=self.region,
            job=ex.job,
            execution=ex.name,
            task_index=index,
            line=line,
            insert_id=f"{short_execution_name(ex.name)}-{index}-{ex.log_seq}",
            timestamp=self._now(),
        )
        payload = entry.get("jsonPayload")
        if not isinstance(payload, dict) or STDOUT_EVENT_KEY not in payload:
            self.filtered_log_lines += 1  # the sink filter jsonPayload.mapi_event:* drops it
            return
        self.log_entries += 1
        if self.log_sink is not None:
            await self.log_sink(entry)
