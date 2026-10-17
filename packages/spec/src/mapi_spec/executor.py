"""Executor protocol shapes (plan §8.4), shared by the control plane and the runtimes.

The control plane drives every backend (in-process, local process, docker, Cloud Run Jobs,
...) through one :class:`Executor` protocol. Identifiers travel in the environment only; the
executor's status API is the liveness source of truth and events are advisory progress.
These are pure Pydantic shapes: no numpy, xarray or plugin code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Final, Protocol, runtime_checkable

from pydantic import Field, JsonValue, field_validator

from mapi_spec._types import Duration, LabelKey, LabelValue, StrictModel
from mapi_spec.errors import Problem
from mapi_spec.resources import ResourceClass

__all__ = [
    "PLUGIN_LABEL",
    "ExecState",
    "ExecStatus",
    "Executor",
    "ExecutorCaps",
    "Handle",
    "Isolation",
    "RunEvent",
    "RunEventType",
    "RunKind",
    "RunSubmit",
]

_RUN_ID = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$"

PLUGIN_LABEL: Final = "mapi.dev/plugin"
""":attr:`RunSubmit.labels` key the dispatcher sets to the model plugin's namespace (``mmm``,
``ts``, ``sklearn``, ...) so an executor can pick the base image that ships the plugin. Users
cannot set ``mapi.dev/`` labels on a submission."""


class Isolation(IntEnum):
    """How strongly a backend isolates a run (ordered: higher is stronger)."""

    NONE = 0
    PROCESS = 1
    GVISOR = 2
    MICROVM = 3
    DEDICATED_VM = 4


class ExecutorCaps(StrictModel):
    """What an executor can do; placement rules are enforced against these."""

    isolation: Isolation
    egress_enforced: bool = False
    dns_enforced: bool = False
    max_runtime: Duration = Field(default_factory=lambda: Duration(3600))
    classes: list[ResourceClass] = Field(default_factory=lambda: [ResourceClass.S])
    max_tasks: int = Field(1, ge=1)
    gpus: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    checkpoint_storage: bool = False

    def allows_public_tenants(self) -> bool:
        """Public tenants need isolation >= gVisor with egress and DNS enforced."""
        return self.isolation >= Isolation.GVISOR and self.egress_enforced and self.dns_enforced


class RunKind(StrEnum):
    """What a run does. ``validate`` is the explicit sandboxed dry-run of a spec.

    ``sweep``, ``backtest``, ``sbc`` and ``campaign`` are fan-out *parents* (plan §6.2,
    M4): they own child runs and never execute on an executor themselves.
    """

    INGEST = "ingest"
    DESCRIBE = "describe"
    VALIDATE = "validate"
    FIT = "fit"
    SCORE = "score"
    EVALUATE = "evaluate"
    DERIVE = "derive"
    RENDER = "render"
    OPTIMIZE = "optimize"
    SWEEP = "sweep"
    BACKTEST = "backtest"
    SBC = "sbc"
    CAMPAIGN = "campaign"
    TRANSLATE_PRIORS = "translate_priors"
    """M3: business priors → model-space priors plus a prior-predictive preview in business
    units (sandboxed; :class:`mapi_spec.calibration.PriorTranslationJob`)."""
    WORKFLOW = "workflow"
    """M3.5: a workflow run (a parent: the orchestrator walks the DAG and starts
    ``workflow_node`` runs and child fits; :mod:`mapi_spec.workflow`)."""
    WORKFLOW_NODE = "workflow_node"
    """M3.5: one sandbox node of a workflow (:class:`mapi_spec.workflow.WorkflowNodeJob`)."""
    EXPLORE = "explore"
    """M3.5: Data or Model Explorer views (:class:`mapi_spec.explore.ExploreJob`)."""
    GOVERNANCE = "governance"
    """M3.5: the governance checks of one model version in the sandbox
    (:class:`mapi_spec.governance.GovernanceJob`; output ``governance.json``)."""

    @property
    def is_parent(self) -> bool:
        """True for fan-out parents, which only aggregate their children."""
        return self in _PARENT_KINDS


_PARENT_KINDS = frozenset(
    {RunKind.SWEEP, RunKind.BACKTEST, RunKind.SBC, RunKind.CAMPAIGN, RunKind.WORKFLOW}
)
RUN_KIND_MAX_LEN: Final = 16
"""``runs.kind`` is ``VARCHAR(16)``: every run kind value fits (a test checks it)."""


class RunSubmit(StrictModel):
    """One attempt of a run, as handed to an executor (JSON only)."""

    run_id: str = Field(..., pattern=_RUN_ID)
    attempt: int = Field(1, ge=1)
    kind: RunKind
    resource_class: ResourceClass = ResourceClass.S
    timeout: Duration = Field(default_factory=lambda: Duration(1800))
    task_count: int = Field(1, ge=1, le=10_000)
    labels: dict[LabelKey, LabelValue] = Field(default_factory=dict, max_length=64)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class Handle(StrictModel):
    """Executor-side identity of a submitted attempt."""

    executor: str
    run_id: str = Field(..., pattern=_RUN_ID)
    attempt: int = Field(1, ge=1)
    nonce: str = Field(..., min_length=8, max_length=128)
    external_id: str | None = None


class ExecState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in (ExecState.SUCCEEDED, ExecState.FAILED, ExecState.CANCELLED)


class ExecStatus(StrictModel):
    """Executor-reported status of an attempt (the liveness source of truth)."""

    state: ExecState
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = Field(None, max_length=2000)
    problem: Problem | None = None


class RunEventType(StrEnum):
    """Event types written by the supervisor (see :mod:`mapi_spec.run` for payloads)."""

    STATE = "state"
    PROGRESS = "progress"
    LOG = "log"
    DIAGNOSTIC = "diagnostic"
    WARNING = "warning"
    CHECKPOINT = "checkpoint"
    METRIC = "metric"
    COMPLETE = "complete"
    ERROR = "error"


class RunEvent(StrictModel):
    """One progress event; ``(run_id, attempt, seq)`` is the dedupe key."""

    run_id: str = Field(..., pattern=_RUN_ID)
    attempt: int = Field(1, ge=1)
    seq: int = Field(..., ge=0)
    type: RunEventType
    ts: datetime
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    hmac: str | None = Field(None, max_length=128)

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("ts must be timezone-aware")
        return v.astimezone(UTC)


@runtime_checkable
class Executor(Protocol):
    """A backend driver. Identifiers reach the run through the environment only."""

    name: str

    def caps(self) -> ExecutorCaps: ...

    async def submit(self, rs: RunSubmit, nonce: str) -> Handle: ...

    async def adopt(self, nonce: str) -> Handle | None: ...

    async def status(self, h: Handle) -> ExecStatus: ...

    async def cancel(self, h: Handle) -> None: ...

    def event_source(self) -> AsyncIterator[RunEvent]: ...
