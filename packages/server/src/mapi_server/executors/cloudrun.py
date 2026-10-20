"""``cloudrun-jobs``: the primary executor, Cloud Run Jobs gen2 (plan §8.1, §8.3, §8.4, §8.8).

**Fixed inventory.** Execution overrides cannot change the image, CPU or memory, so the
platform deploys a fixed set of Jobs — ``{base-bayes, base-ml, base-forecast} × {S, M, L}`` —
named by convention (:class:`JobInventory`, default ``mapi-{env}-{base}-{class}``; XL lives on
GKE in M6). Terraform creates them with ``max-retries=0``, the execution service account, the
deny-all network and the static ``MAPI_BOOTSTRAP_URL=gs://mapi-{env}-bootstrap/{nonce}``.

**Submit.** The dispatcher writes the intent row ``(run, attempt, nonce)`` and the bootstrap
object first; :meth:`CloudRunJobsExecutor.submit` then calls ``jobs.run`` with **only** env
overrides (``MAPI_RUN_ID``, ``MAPI_NONCE``), the task count and the task timeout — nothing in
args, nothing secret. The returned operation's metadata names the execution; the execution
name becomes :attr:`Handle.external_id` (the dispatcher stores it) and the operation name is
logged.

**Adopt.** Cloud Run picks execution names itself, so orphans are found by nonce:
:meth:`~CloudRunJobsExecutor.adopt` lists recent executions of every inventory Job and matches
the ``MAPI_NONCE`` env override in the execution template (an index cached for a moment so a
restarted reconciler does not list once per run). Duplicate executions of one nonce are
cancelled, keeping the oldest.

**Status** comes from ``executions.get`` (the liveness source of truth) plus ``tasks.list``
once the execution finished, for the exit codes (:func:`execution_status`): exit 137 or a
memory-limit termination is ``RESOURCE_EXHAUSTED``. **Cancel** is ``executions.cancel``
(SIGTERM to every task, then SIGKILL after the grace period).

**Rate limits.** ``jobs.run`` is capped per project and region (180/min **[verify M0]**). A
token bucket keeps submissions below a configurable rate; a 429 penalizes the bucket for every
submitter (exponential back-off honouring ``Retry-After``, :func:`mapi_spec.quotas.backoff_seconds`)
and is counted as an alarm (:meth:`~CloudRunJobsExecutor.health`). A 429 means the execution was
not created, so retrying the same nonce is safe; an ambiguous failure (timeout, 5xx) is
resolved by ``adopt`` instead of a blind retry.

**Fan-out.** ``RunSubmit.task_count`` becomes the ``taskCount`` override; see
:mod:`mapi_server.executors.tasks` for the per-task contract.

Events never come through the executor: the supervisor's stdout goes Cloud Logging → Pub/Sub
→ ``POST /internal/events`` (:mod:`mapi_server.services.pubsub`).

The Google client library (``google-cloud-run``, the ``gcp`` extra) is only imported by
:class:`GoogleCloudRunApi`; everything else works against the :class:`CloudRunApi` protocol,
which :class:`~mapi_server.executors.fake_cloudrun.FakeCloudRun` implements for tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from mapi_spec._types import Duration
from mapi_spec.admin import ExecutorHealth, ExecutorStatus
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.executor import (
    PLUGIN_LABEL,
    ExecState,
    ExecStatus,
    ExecutorCaps,
    Handle,
    Isolation,
    RunEvent,
    RunSubmit,
)
from mapi_spec.quotas import JOBS_RUN_PER_MINUTE, backoff_seconds
from mapi_spec.resources import ResourceClass
from mapi_spec.run import ENV_NONCE, ENV_RUN_ID, RunnerExit

from mapi_server.observability import metrics as obs_metrics
from mapi_server.ratelimit import TokenBucket, WindowCounter

__all__ = [
    "CLOUDRUN_EXECUTOR",
    "DEFAULT_PLUGIN_BASES",
    "DEFAULT_SUBMITS_PER_MINUTE",
    "EXECUTION_NAME_LABEL",
    "INVENTORY_CLASSES",
    "MAX_TASK_COUNT",
    "MAX_TASK_TIMEOUT_S",
    "Base",
    "CloudRunApi",
    "CloudRunError",
    "CloudRunFailedPrecondition",
    "CloudRunJobsExecutor",
    "CloudRunNotFound",
    "CloudRunRateLimited",
    "CloudRunUnavailable",
    "ConditionInfo",
    "ExecutionInfo",
    "GoogleCloudRunApi",
    "JobInventory",
    "RunJobResult",
    "TaskInfo",
    "aggregate_exit_code",
    "execution_info",
    "execution_status",
    "map_api_error",
    "run_job_request",
    "same_execution",
    "short_execution_name",
    "task_exit_code",
    "task_info",
]

log = logging.getLogger(__name__)

CLOUDRUN_EXECUTOR: Final = "cloudrun-jobs"
EXECUTION_NAME_LABEL: Final = "run.googleapis.com/execution_name"
"""Cloud Logging label carrying the (short) execution name of a Cloud Run Jobs log entry."""
MAX_TASK_COUNT: Final = 10_000
MAX_TASK_TIMEOUT_S: Final = 24 * 3600
"""Longest task timeout the executor asks for (Cloud Run Jobs GA limit) [verify M0]."""
DEFAULT_SUBMITS_PER_MINUTE: Final = 150.0
"""Default submit rate: headroom below the 180 ``jobs.run``/min quota [verify M0]."""
INVENTORY_CLASSES: Final = (ResourceClass.S, ResourceClass.M, ResourceClass.L)

_JOB_ID = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]+-[a-z]+[0-9]$")
_MEMORY_HINTS = ("memory limit", "out of memory", "oom")
_TIMEOUT_HINTS = ("timed out", "timeout", "deadline exceeded")


class Base(StrEnum):
    """The curated base images (plan §7, T0)."""

    BAYES = "base-bayes"
    ML = "base-ml"
    FORECAST = "base-forecast"
    DL = "base-dl"
    """v0.15: ``mapi-jax`` with the CUDA 12 wheels (the only base with a GPU Job)."""


DEFAULT_PLUGIN_BASES: Final[Mapping[str, Base]] = {
    "mmm": Base.BAYES,
    "sci": Base.BAYES,
    "tabular": Base.BAYES,
    "sklearn": Base.ML,
    "ts": Base.FORECAST,
    "causal": Base.FORECAST,
    "dl": Base.DL,
}
"""Plugin namespace (:data:`~mapi_spec.executor.PLUGIN_LABEL`) → the base that ships it
(mirrors :data:`mapi_server.settings.DEFAULT_CLOUDRUN_BASES`)."""


# ======================================================================= inventory


@dataclass(frozen=True)
class JobInventory:
    """The fixed Job inventory of one exec project and region, named by convention."""

    project: str
    region: str
    env: str = "prod"
    template: str = "mapi-{env}-{base}-{class}"
    plugin_bases: Mapping[str, Base] = field(default_factory=lambda: dict(DEFAULT_PLUGIN_BASES))
    default_base: Base = Base.BAYES
    classes: tuple[ResourceClass, ...] = INVENTORY_CLASSES
    gpu_bases: tuple[Base, ...] = (Base.DL,)
    """Bases that also have a ``GPU`` Job (one NVIDIA L4; v0.15). Empty: no GPU class."""

    def __post_init__(self) -> None:
        if not _PROJECT.match(self.project):
            raise ValueError(f"{self.project!r} is not a valid Google Cloud project id")
        if not _REGION.match(self.region):
            raise ValueError(f"{self.region!r} is not a valid Cloud Run region")
        if any(rc.is_gpu for rc in self.classes):
            raise ValueError("the GPU class comes from gpu_bases, not classes")
        for base in Base:
            for rc in self.classes:
                self.job_id(base, rc)  # validates the template once
        for base in self.gpu_bases:
            self.job_id(base, ResourceClass.GPU)

    @property
    def offered_classes(self) -> tuple[ResourceClass, ...]:
        """The classes some Job of the inventory runs: the CPU classes, plus ``GPU`` when a
        base has a GPU Job."""
        return (*self.classes, ResourceClass.GPU) if self.gpu_bases else self.classes

    @property
    def parent(self) -> str:
        return f"projects/{self.project}/locations/{self.region}"

    def job_id(self, base: Base, resource_class: ResourceClass) -> str:
        try:
            job = self.template.format_map(
                {"env": self.env, "base": base.value, "class": resource_class.value.lower()}
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"bad Cloud Run job name template {self.template!r}") from exc
        if not _JOB_ID.match(job):
            raise ValueError(f"{job!r} is not a valid Cloud Run job id")
        return job

    def job_name(self, base: Base, resource_class: ResourceClass) -> str:
        return f"{self.parent}/jobs/{self.job_id(base, resource_class)}"

    def jobs(self) -> list[str]:
        """Every Job of the inventory (bases × CPU classes, then GPU bases × ``GPU``)."""
        cpu = [self.job_name(b, rc) for b in Base for rc in self.classes]
        return cpu + [self.job_name(b, ResourceClass.GPU) for b in self.gpu_bases]

    def base_for(self, rs: RunSubmit) -> Base:
        plugin = rs.labels.get(PLUGIN_LABEL)
        if plugin is None:
            return self.default_base
        return self.plugin_bases.get(plugin, self.default_base)

    def select(self, rs: RunSubmit) -> str:
        """The Job an attempt runs on (plugin → base, resource class → size). Only the
        GPU bases (``dl`` models) have a ``GPU`` Job."""
        base = self.base_for(rs)
        if rs.resource_class.is_gpu:
            if base not in self.gpu_bases:
                raise ValueError(
                    "only dl models run on the GPU class "
                    f"(this run's base {base.value} has no GPU job)"
                )
            return self.job_name(base, rs.resource_class)
        if rs.resource_class not in self.classes:
            raise ValueError(f"the Cloud Run inventory has no class {rs.resource_class.value}")
        return self.job_name(base, rs.resource_class)


# ===================================================================== API shapes


@dataclass(frozen=True)
class ConditionInfo:
    type: str
    state: str
    """``CONDITION_SUCCEEDED``, ``CONDITION_FAILED``, ``CONDITION_PENDING``, ..."""
    reason: str = ""
    """Execution or common reason name (``CANCELLED``, ``CANCELLING``, ``NON_ZERO_EXIT_CODE``)."""
    message: str = ""


@dataclass(frozen=True)
class ExecutionInfo:
    """The fields of a Cloud Run ``Execution`` the executor relies on."""

    name: str
    job: str
    env: Mapping[str, str] = field(default_factory=dict)
    task_count: int = 1
    create_time: datetime | None = None
    start_time: datetime | None = None
    completion_time: datetime | None = None
    delete_time: datetime | None = None
    running_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    cancelled_count: int = 0
    conditions: tuple[ConditionInfo, ...] = ()
    reconciling: bool = False

    @property
    def short_name(self) -> str:
        return short_execution_name(self.name)

    @property
    def nonce(self) -> str | None:
        return self.env.get(ENV_NONCE)

    @property
    def finished(self) -> bool:
        return self.completion_time is not None

    def reason(self, reason: str) -> bool:
        return any(c.reason == reason for c in self.conditions)


@dataclass(frozen=True)
class TaskInfo:
    name: str
    index: int
    exit_code: int | None = None
    term_signal: int | None = None
    succeeded: bool | None = None
    """``True``/``False`` once the task finished, ``None`` while it runs."""
    message: str = ""


@dataclass(frozen=True)
class RunJobResult:
    operation: str
    execution: ExecutionInfo


class CloudRunError(Exception):
    """A Cloud Run Admin API call failed."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CloudRunRateLimited(CloudRunError):
    """HTTP 429 / ``RESOURCE_EXHAUSTED``: nothing was created; retry after a back-off."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


class CloudRunNotFound(CloudRunError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=404)


class CloudRunFailedPrecondition(CloudRunError):
    """E.g. cancelling an execution that already finished."""


class CloudRunUnavailable(CloudRunError):
    """Transient (5xx, deadline): for ``jobs.run`` the outcome is unknown."""


@runtime_checkable
class CloudRunApi(Protocol):
    """The slice of the Cloud Run Admin API v2 the executor uses."""

    async def run_job(
        self, job: str, *, env: Mapping[str, str], task_count: int, timeout_s: int
    ) -> RunJobResult: ...

    async def get_execution(self, name: str) -> ExecutionInfo: ...

    async def list_executions(self, job: str, *, limit: int) -> list[ExecutionInfo]:
        """Newest first, at most ``limit``."""
        ...

    async def list_tasks(self, execution: str) -> list[TaskInfo]: ...

    async def cancel_execution(self, name: str) -> None: ...


def short_execution_name(name: str) -> str:
    """``projects/p/locations/r/jobs/j/executions/j-x7k2p`` → ``j-x7k2p``."""
    return name.rsplit("/", 1)[-1]


def same_execution(external_id: str | None, label: str | None) -> bool:
    """Whether a log entry's execution label names the stored execution (full or short)."""
    if external_id is None or label is None:
        return True
    return external_id == label or short_execution_name(external_id) == label


# ================================================================ status mapping


def task_exit_code(task: TaskInfo) -> int | None:
    """The supervisor exit code of a finished task, as :class:`RunnerExit` understands it.

    A memory-limit termination is 137 (``RESOURCE_EXHAUSTED``) even without an exit code; a
    timeout kill is 143 (``TIMEOUT``); other signals are ``128 + signal``. A task that failed
    without any code (it never started) is ``None``: the reconciler treats it as an execution
    stopped outside the platform (infra, retried).
    """
    message = task.message.lower()
    if task.exit_code:
        return task.exit_code
    if any(h in message for h in _MEMORY_HINTS):
        return int(RunnerExit.KILLED)
    if task.term_signal:
        if any(h in message for h in _TIMEOUT_HINTS):
            return int(RunnerExit.TERMINATED)
        return 128 + task.term_signal
    if task.succeeded is False:
        return None
    return task.exit_code


def aggregate_exit_code(codes: Sequence[int | None]) -> int | None:
    """One exit code for an execution from its non-succeeded tasks (in index order).

    A real failure beats ``PARTIAL`` (16), which beats "no code": a fan-out where one task
    flushed checkpoints and another crashed is a crash.
    """
    real = [c for c in codes if c is not None and c not in (RunnerExit.OK, RunnerExit.PARTIAL)]
    if real:
        return real[0]
    if RunnerExit.PARTIAL in codes:
        return int(RunnerExit.PARTIAL)
    return None


def execution_status(info: ExecutionInfo, tasks: Sequence[TaskInfo] | None = None) -> ExecStatus:
    """Map a Cloud Run execution (and, once finished, its tasks) to :class:`ExecStatus`."""
    if info.delete_time is not None:
        return ExecStatus(state=ExecState.UNKNOWN, message="the execution was deleted")
    cancelled = info.cancelled_count > 0 or info.reason("CANCELLED")
    if not info.finished:
        running = info.start_time is not None or info.running_count > 0
        return ExecStatus(
            state=ExecState.RUNNING if running else ExecState.PENDING,
            started_at=info.start_time,
            message="cancelling" if info.reason("CANCELLING") or cancelled else None,
        )
    if not cancelled and info.failed_count == 0 and info.succeeded_count >= info.task_count:
        return ExecStatus(
            state=ExecState.SUCCEEDED,
            exit_code=int(RunnerExit.OK),
            started_at=info.start_time,
            finished_at=info.completion_time,
        )
    failing = sorted((t for t in tasks or () if t.succeeded is not True), key=lambda t: t.index)
    code = aggregate_exit_code([task_exit_code(t) for t in failing])
    messages = [t.message for t in failing if t.message]
    messages += [c.message for c in info.conditions if c.type == "Completed" and c.message]
    message = messages[0][:2000] if messages else None
    problem = None
    if code == RunnerExit.KILLED:
        problem = Problem.from_code(
            ErrorCode.RESOURCE_EXHAUSTED,
            "the execution was killed for exceeding its memory limit (exit 137)",
        )
    return ExecStatus(
        state=ExecState.CANCELLED if cancelled else ExecState.FAILED,
        exit_code=code,
        started_at=info.start_time,
        finished_at=info.completion_time,
        message=message,
        problem=problem,
    )


# ====================================================================== executor


@dataclass
class _Submitted:
    handle: Handle
    job: str
    operation: str
    at: float = 0.0
    """``clock()`` when submitted or adopted (entries older than the adopt lookback go)."""


_FINISHED_MEMORY: Final = 10_000
"""Finished executions remembered (status cache, duplicate-submit answers) per process."""


class _SilentEvents:
    """Cloud events arrive over HTTP (Pub/Sub push): this source only ends on close."""

    def __init__(self, closed: asyncio.Event) -> None:
        self._closed = closed

    def __aiter__(self) -> _SilentEvents:
        return self

    async def __anext__(self) -> RunEvent:
        await self._closed.wait()
        raise StopAsyncIteration


class CloudRunJobsExecutor:
    """:class:`~mapi_spec.executor.Executor` over the Cloud Run Admin API (see module doc)."""

    name = CLOUDRUN_EXECUTOR

    def __init__(
        self,
        api: CloudRunApi,
        inventory: JobInventory,
        *,
        submits_per_minute: float = DEFAULT_SUBMITS_PER_MINUTE,
        burst: int = 10,
        max_rate_limit_retries: int = 6,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 64.0,
        timeout_margin_s: int = 120,
        max_runtime_s: int = MAX_TASK_TIMEOUT_S,
        egress_enforced: bool = True,
        dns_enforced: bool = False,
        adopt_lookback_s: float = 26 * 3600,
        adopt_limit_per_job: int = 500,
        adopt_refresh_s: float = 2.0,
        cancel_resend_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        jitter: Callable[[], float] | None = None,
    ) -> None:
        if submits_per_minute > JOBS_RUN_PER_MINUTE:
            log.warning(
                "cloudrun-jobs: %s submits/min exceeds the %s jobs.run/min quota [verify M0]",
                submits_per_minute,
                JOBS_RUN_PER_MINUTE,
            )
        if max_rate_limit_retries < 0 or timeout_margin_s < 0:
            raise ValueError("retries and margins must be >= 0")
        self.api = api
        self.inventory = inventory
        self.bucket = TokenBucket(submits_per_minute / 60.0, burst, clock=clock, sleep=sleep)
        self._max_retries = max_rate_limit_retries
        self._backoff_base = backoff_base_s
        self._backoff_cap = backoff_cap_s
        self._margin = timeout_margin_s
        self._max_runtime = min(max_runtime_s, MAX_TASK_TIMEOUT_S)
        self._egress = egress_enforced
        self._dns = dns_enforced
        self._lookback = timedelta(seconds=adopt_lookback_s)
        self._adopt_limit = adopt_limit_per_job
        self._adopt_refresh = adopt_refresh_s
        self._cancel_resend = cancel_resend_s
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._jitter = jitter or (lambda: random.uniform(0.0, 0.25))
        self._submitted: dict[str, _Submitted] = {}
        """Executions this process submitted or adopted that are not known to be finished."""
        self._finished: OrderedDict[str, _Submitted] = OrderedDict()
        self._ambiguous: set[str] = set()
        self._index: dict[str, list[ExecutionInfo]] = {}
        self._index_at = float("-inf")
        self._index_lock = asyncio.Lock()
        self._terminal: OrderedDict[str, ExecStatus] = OrderedDict()
        self._cancel_sent: dict[str, float] = {}
        self._closed = asyncio.Event()
        self.submits = WindowCounter(60.0, clock=clock)
        self.throttled = WindowCounter(3600.0, clock=clock)
        self.failures = WindowCounter(3600.0, clock=clock)
        self.duplicates_cancelled = 0
        self.last_success_at: datetime | None = None

    # ------------------------------------------------------------------ protocol
    def caps(self) -> ExecutorCaps:
        return ExecutorCaps(
            isolation=Isolation.MICROVM,
            egress_enforced=self._egress,
            dns_enforced=self._dns,
            max_runtime=Duration(self._max_runtime),
            classes=list(self.inventory.offered_classes),
            max_tasks=MAX_TASK_COUNT,
            regions=(self.inventory.region,),
            checkpoint_storage=True,
        )

    def _timeout_s(self, rs: RunSubmit) -> int:
        """Task timeout: the run's deadline plus a margin so the supervisor can flush (the
        reconciler cancels at the deadline; this is the backstop)."""
        return max(1, min(rs.timeout.seconds + self._margin, self._max_runtime))

    async def submit(self, rs: RunSubmit, nonce: str) -> Handle:
        done = self._known(nonce)
        if done is not None:  # duplicate submit: the same execution
            return done.handle
        if nonce in self._ambiguous:  # an earlier call may have created it
            adopted = await self.adopt(nonce)
            if adopted is not None:
                return adopted
        if rs.task_count > MAX_TASK_COUNT:
            raise ValueError(f"task_count {rs.task_count} exceeds {MAX_TASK_COUNT}")
        job = self.inventory.select(rs)
        env = {ENV_RUN_ID: rs.run_id, ENV_NONCE: nonce}
        timeout_s = self._timeout_s(rs)
        throttled = 0
        while True:
            await self.bucket.acquire()
            self.submits.record()
            try:
                result = await self.api.run_job(
                    job, env=env, task_count=rs.task_count, timeout_s=timeout_s
                )
            except CloudRunRateLimited as exc:
                throttled += 1
                self.throttled.record()
                obs_metrics.record_executor_throttled(self.name)
                delay = backoff_seconds(
                    throttled,
                    base=self._backoff_base,
                    cap=self._backoff_cap,
                    # honoured, but capped: one answer must not park every submit for long
                    retry_after=(
                        None if exc.retry_after is None else min(exc.retry_after, self._backoff_cap)
                    ),
                ) * (1.0 + self._jitter())
                log.warning(
                    "ALARM cloudrun-jobs: jobs.run answered 429 for run %s (%s in a row, "
                    "%s in the last hour); backing off %.2fs",
                    rs.run_id,
                    throttled,
                    self.throttled.count(),
                    delay,
                )
                if throttled > self._max_retries:
                    self.failures.record()
                    raise
                self.bucket.penalize(delay)
                continue
            except CloudRunUnavailable:
                # the request may have created the execution: never retry blindly
                self._ambiguous.add(nonce)
                self.failures.record()
                raise
            except CloudRunError:
                self.failures.record()
                raise
            break
        execution = result.execution
        handle = Handle(
            executor=self.name,
            run_id=rs.run_id,
            attempt=rs.attempt,
            nonce=nonce,
            external_id=execution.name,
        )
        self._prune()
        self._submitted[nonce] = _Submitted(
            handle=handle, job=job, operation=result.operation, at=self._clock()
        )
        self._ambiguous.discard(nonce)
        self.last_success_at = self._now()
        log.info(
            "jobs.run %s → execution %s (operation %s) for run %s/%s, %s task(s)",
            job,
            execution.name,
            result.operation,
            rs.run_id,
            rs.attempt,
            rs.task_count,
        )
        return handle

    def operation_for(self, nonce: str) -> str | None:
        """The ``jobs.run`` operation name of an attempt submitted by this process."""
        done = self._known(nonce)
        return done.operation if done else None

    def _known(self, nonce: str) -> _Submitted | None:
        return self._submitted.get(nonce) or self._finished.get(nonce)

    def _finish(self, nonce: str, name: str) -> None:
        """``name`` (of ``nonce``) reached a terminal state: stop tracking it as live."""
        done = self._submitted.pop(nonce, None)
        if done is not None:
            self._finished[nonce] = done
            while len(self._finished) > _FINISHED_MEMORY:
                self._finished.popitem(last=False)
        self._cancel_sent.pop(name, None)

    def _prune(self) -> None:
        """Forget executions older than the adopt lookback this process never saw finish
        (another process reconciled them): the bookkeeping stays bounded."""
        horizon = self._clock() - self._lookback.total_seconds()
        for nonce in [n for n, s in self._submitted.items() if s.at < horizon]:
            del self._submitted[nonce]
        for name in [n for n, t in self._cancel_sent.items() if t < horizon]:
            del self._cancel_sent[name]

    async def _refresh_index(self, *, force: bool = False) -> None:
        async with self._index_lock:
            if not force and self._clock() - self._index_at < self._adopt_refresh:
                return
            horizon = self._now() - self._lookback
            index: dict[str, list[ExecutionInfo]] = {}
            for job in self.inventory.jobs():
                try:
                    executions = await self.api.list_executions(job, limit=self._adopt_limit)
                except CloudRunNotFound:
                    continue  # this Job is not deployed (e.g. a class not used in this env)
                for ex in executions:
                    if ex.create_time is not None and ex.create_time < horizon:
                        continue
                    if ex.nonce:
                        index.setdefault(ex.nonce, []).append(ex)
            self._index = index
            self._index_at = self._clock()

    async def adopt(self, nonce: str) -> Handle | None:
        """Find the execution of ``nonce`` (orphan recovery); cancel any duplicates."""
        done = self._known(nonce)
        if done is not None:
            return done.handle
        force = nonce in self._ambiguous
        await self._refresh_index(force=force)
        found = [e for e in self._index.get(nonce, []) if e.delete_time is None]
        if not found:
            return None
        found.sort(key=lambda e: (e.create_time or datetime.max.replace(tzinfo=UTC), e.name))
        keep, extra = found[0], found[1:]
        for dup in extra:
            if not dup.finished:
                log.error(
                    "nonce %s… has %s executions; cancelling duplicate %s",
                    nonce[:12],
                    len(found),
                    dup.name,
                )
                with contextlib.suppress(CloudRunError):
                    await self.api.cancel_execution(dup.name)
                self.duplicates_cancelled += 1
        run_id = keep.env.get(ENV_RUN_ID)
        if not run_id:
            return None
        handle = Handle(
            executor=self.name,
            run_id=run_id,
            attempt=1,  # not in the env; the reconciler keeps the attempt of its intent row
            nonce=nonce,
            external_id=keep.name,
        )
        self._submitted[nonce] = _Submitted(
            handle=handle, job=keep.job, operation="", at=self._clock()
        )
        self._ambiguous.discard(nonce)
        log.info("adopted Cloud Run execution %s by nonce", keep.name)
        return handle

    async def _execution_name(self, h: Handle) -> str | None:
        if h.external_id:
            return h.external_id
        adopted = await self.adopt(h.nonce)
        return adopted.external_id if adopted else None

    async def status(self, h: Handle) -> ExecStatus:
        name = await self._execution_name(h)
        if name is None:
            return ExecStatus(state=ExecState.UNKNOWN, message="no execution for this nonce")
        cached = self._terminal.get(name)
        if cached is not None:
            self._finish(h.nonce, name)
            return cached
        try:
            info = await self.api.get_execution(name)
        except CloudRunNotFound:
            return ExecStatus(state=ExecState.UNKNOWN, message="the execution does not exist")
        tasks = await self.api.list_tasks(name) if info.finished else None
        st = execution_status(info, tasks)
        if st.state.terminal:
            self._terminal[name] = st
            while len(self._terminal) > _FINISHED_MEMORY:
                self._terminal.popitem(last=False)
            self._finish(h.nonce, name)
        return st

    async def cancel(self, h: Handle) -> None:
        """``executions.cancel`` (idempotent; re-sent at most every ``cancel_resend_s``)."""
        name = await self._execution_name(h)
        if name is None or name in self._terminal:
            return
        sent = self._cancel_sent.get(name)
        if sent is not None and self._clock() - sent < self._cancel_resend:
            return
        # NotFound: gone; FailedPrecondition: already finished. status() tells which.
        with contextlib.suppress(CloudRunNotFound, CloudRunFailedPrecondition):
            await self.api.cancel_execution(name)
        self._cancel_sent[name] = self._clock()

    def event_source(self) -> AsyncIterator[RunEvent]:
        return _SilentEvents(self._closed)

    async def aclose(self) -> None:
        self._closed.set()
        close = getattr(self.api, "aclose", None)
        if close is not None:
            await close()

    # ------------------------------------------------------------------ health
    def health(self, *, enabled: bool = True, queued_runs: int = 0) -> ExecutorHealth:
        """The admin console's view of this executor (429 alarm, submit rate, failures)."""
        throttled = self.throttled.count()
        failures = self.failures.count()
        status = ExecutorStatus.OK
        message = None
        if not enabled:
            status = ExecutorStatus.DISABLED
        elif self.bucket.cooldown_left() > 0 or throttled:
            status = ExecutorStatus.DEGRADED
            message = f"jobs.run answered 429 {throttled} time(s) in the last hour"
        elif failures:
            status = ExecutorStatus.DEGRADED
            message = f"{failures} submit failure(s) in the last hour"
        caps = self.caps()
        self._prune()
        # executions this process submitted or adopted and has not seen finish (the admin
        # console counts dispatched runs from the database)
        live = len(self._submitted)
        return ExecutorHealth(
            name=self.name,
            enabled=enabled,
            status=status,
            caps=caps,
            regions=list(caps.regions),
            allows_public_tenants=caps.allows_public_tenants(),
            running_executions=live,
            queued_runs=queued_runs,
            submits_last_minute=self.submits.count(),
            submit_rate_limit_per_minute=max(1, int(self.bucket.per_minute)),
            throttled_last_hour=throttled,
            failures_last_hour=failures,
            last_success_at=self.last_success_at,
            checked_at=self._now(),
            message=message,
        )


# ============================================================ google-cloud-run adapter


def _import_run_v2() -> Any:
    try:
        from google.cloud import run_v2  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "the cloudrun-jobs executor needs google-cloud-run: pip install 'mapi-server[gcp]'"
        ) from exc
    return run_v2


def run_job_request(
    job: str, *, env: Mapping[str, str], task_count: int, timeout_s: int
) -> dict[str, Any]:
    """The ``RunJobRequest`` (as a dict the GAPIC client accepts): env, task count, timeout."""
    return {
        "name": job,
        "overrides": {
            "container_overrides": [
                {"env": [{"name": k, "value": v} for k, v in sorted(env.items())]}
            ],
            "task_count": task_count,
            "timeout": {"seconds": timeout_s},
        },
    }


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    return str(name) if name is not None else str(value or "")


def _reason(condition: Any) -> str:
    for attr in ("execution_reason", "reason", "revision_reason"):
        name = _enum_name(getattr(condition, attr, ""))
        if name and not name.endswith(("_UNDEFINED", "_UNSPECIFIED")) and name != "0":
            return name
    return ""


def _has(message: Any, field_name: str) -> bool:
    try:
        return field_name in message
    except TypeError:  # not a proto-plus message (a test double)
        return getattr(message, field_name, None) is not None


def _dt(value: Any) -> datetime | None:
    if value is None or not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def execution_info(execution: Any) -> ExecutionInfo:
    """Convert a ``google.cloud.run_v2.Execution`` (duck-typed) to :class:`ExecutionInfo`.

    The env overrides of an execution appear in ``execution.template`` (a ``TaskTemplate``)
    — this is how :meth:`CloudRunJobsExecutor.adopt` matches ``MAPI_NONCE`` [verify M0].
    """
    env: dict[str, str] = {}
    template = getattr(execution, "template", None)
    for container in getattr(template, "containers", None) or ():
        for var in getattr(container, "env", None) or ():
            name = getattr(var, "name", "")
            if name:
                env[str(name)] = str(getattr(var, "value", "") or "")
    conditions = tuple(
        ConditionInfo(
            type=str(getattr(c, "type_", "") or getattr(c, "type", "") or ""),
            state=_enum_name(getattr(c, "state", "")),
            reason=_reason(c),
            message=str(getattr(c, "message", "") or ""),
        )
        for c in getattr(execution, "conditions", None) or ()
    )
    name = str(execution.name)
    job = name.split("/executions/", 1)[0] if "/executions/" in name else ""
    return ExecutionInfo(
        name=name,
        job=str(getattr(execution, "job", "") or "") or job,
        env=env,
        task_count=int(getattr(execution, "task_count", 0) or 1),
        create_time=_dt(getattr(execution, "create_time", None)),
        start_time=_dt(getattr(execution, "start_time", None)),
        completion_time=_dt(getattr(execution, "completion_time", None)),
        delete_time=_dt(getattr(execution, "delete_time", None)),
        running_count=int(getattr(execution, "running_count", 0) or 0),
        succeeded_count=int(getattr(execution, "succeeded_count", 0) or 0),
        failed_count=int(getattr(execution, "failed_count", 0) or 0),
        cancelled_count=int(getattr(execution, "cancelled_count", 0) or 0),
        conditions=conditions,
        reconciling=bool(getattr(execution, "reconciling", False)),
    )


def task_info(task: Any) -> TaskInfo:
    """Convert a ``google.cloud.run_v2.Task`` (duck-typed) to :class:`TaskInfo`.

    An unset ``last_attempt_result`` reads as exit code 0 in proto3, so presence is checked
    explicitly: a task that never ran has no exit code at all.
    """
    exit_code: int | None = None
    term_signal: int | None = None
    message = ""
    if _has(task, "last_attempt_result"):
        result = task.last_attempt_result
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        term_signal = int(getattr(result, "term_signal", 0) or 0) or None
        status = getattr(result, "status", None)
        message = str(getattr(status, "message", "") or "")
    succeeded: bool | None = None
    for c in getattr(task, "conditions", None) or ():
        if str(getattr(c, "type_", "") or getattr(c, "type", "")) == "Completed":
            state = _enum_name(getattr(c, "state", ""))
            if state == "CONDITION_SUCCEEDED":
                succeeded = True
            elif state == "CONDITION_FAILED":
                succeeded = False
            message = message or str(getattr(c, "message", "") or "")
    if succeeded is None and _dt(getattr(task, "completion_time", None)) is not None:
        succeeded = exit_code == 0 and term_signal is None
    return TaskInfo(
        name=str(task.name),
        index=int(getattr(task, "index", 0) or 0),
        exit_code=exit_code,
        term_signal=term_signal,
        succeeded=succeeded,
        message=message[:2000],
    )


def _retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        with contextlib.suppress(TypeError, ValueError):
            return max(0.0, float(raw))
    for detail in getattr(exc, "details", None) or ():
        delay = getattr(detail, "retry_delay", None)  # google.rpc.RetryInfo
        if delay is not None:
            seconds = getattr(delay, "seconds", None)
            if seconds is not None:
                return float(seconds) + float(getattr(delay, "nanos", 0) or 0) / 1e9
            total = getattr(delay, "total_seconds", None)
            if callable(total):
                return float(total())
    return None


def map_api_error(exc: BaseException, what: str) -> CloudRunError:
    """Translate a ``google.api_core`` error (duck-typed on ``code``/class name)."""
    if isinstance(exc, CloudRunError):
        return exc
    code = getattr(exc, "code", None)
    status = code if isinstance(code, int) else None
    kind = type(exc).__name__
    grpc = _enum_name(getattr(exc, "grpc_status_code", None) or "")
    message = f"{what}: {kind}: {exc}"[:2000]
    if status == 429 or kind in ("TooManyRequests", "ResourceExhausted"):
        return CloudRunRateLimited(message, retry_after=_retry_after(exc))
    if status == 404 or kind == "NotFound":
        return CloudRunNotFound(message)
    if kind == "FailedPrecondition" or grpc == "FAILED_PRECONDITION":
        return CloudRunFailedPrecondition(message, status=status)
    if (status is not None and status >= 500) or kind in (
        "DeadlineExceeded",
        "ServiceUnavailable",
        "InternalServerError",
        "Aborted",
        "Unknown",
        "RetryError",
        "TimeoutError",
    ):
        return CloudRunUnavailable(message, status=status)
    return CloudRunError(message, status=status)


class GoogleCloudRunApi:
    """:class:`CloudRunApi` on ``google.cloud.run_v2`` async clients (``gcp`` extra).

    Exercised in this repository only against test doubles (and the real request/response
    types when the library is installed) — never against live Cloud Run.
    """

    def __init__(
        self,
        *,
        jobs_client: Any | None = None,
        executions_client: Any | None = None,
        tasks_client: Any | None = None,
        credentials: Any | None = None,
        call_timeout_s: float = 30.0,
    ) -> None:
        if jobs_client is None or executions_client is None or tasks_client is None:
            run_v2 = _import_run_v2()
            jobs_client = jobs_client or run_v2.JobsAsyncClient(credentials=credentials)
            executions_client = executions_client or run_v2.ExecutionsAsyncClient(
                credentials=credentials
            )
            tasks_client = tasks_client or run_v2.TasksAsyncClient(credentials=credentials)
        self._jobs = jobs_client
        self._executions = executions_client
        self._tasks = tasks_client
        self._timeout = call_timeout_s

    async def run_job(
        self, job: str, *, env: Mapping[str, str], task_count: int, timeout_s: int
    ) -> RunJobResult:
        request = run_job_request(job, env=env, task_count=task_count, timeout_s=timeout_s)
        try:
            operation = await self._jobs.run_job(request=request, timeout=self._timeout)
        except Exception as exc:
            raise map_api_error(exc, "jobs.run") from exc
        metadata = getattr(operation, "metadata", None)
        if metadata is None or not getattr(metadata, "name", ""):
            # created or not, we cannot tell: the dispatcher adopts by nonce
            raise CloudRunUnavailable("jobs.run returned no execution in its operation metadata")
        op_name = str(getattr(getattr(operation, "operation", None), "name", "") or "")
        return RunJobResult(operation=op_name, execution=execution_info(metadata))

    async def get_execution(self, name: str) -> ExecutionInfo:
        try:
            execution = await self._executions.get_execution(
                request={"name": name}, timeout=self._timeout
            )
        except Exception as exc:
            raise map_api_error(exc, "executions.get") from exc
        return execution_info(execution)

    async def list_executions(self, job: str, *, limit: int) -> list[ExecutionInfo]:
        out: list[ExecutionInfo] = []
        try:
            pager = await self._executions.list_executions(
                request={"parent": job, "page_size": min(max(limit, 1), 100)},
                timeout=self._timeout,
            )
            async for execution in pager:
                out.append(execution_info(execution))
                if len(out) >= limit:
                    break
        except Exception as exc:
            raise map_api_error(exc, "executions.list") from exc
        return out

    async def list_tasks(self, execution: str) -> list[TaskInfo]:
        out: list[TaskInfo] = []
        try:
            pager = await self._tasks.list_tasks(
                request={"parent": execution, "page_size": 100}, timeout=self._timeout
            )
            async for task in pager:
                out.append(task_info(task))
                if len(out) >= MAX_TASK_COUNT:
                    break
        except Exception as exc:
            raise map_api_error(exc, "tasks.list") from exc
        return out

    async def cancel_execution(self, name: str) -> None:
        try:
            await self._executions.cancel_execution(request={"name": name}, timeout=self._timeout)
        except Exception as exc:
            raise map_api_error(exc, "executions.cancel") from exc
