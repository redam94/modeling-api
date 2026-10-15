"""Fan-out with ``taskCount`` (plan §8.3): one execution runs N homogeneous children.

Cloud Run Jobs starts ``task_count`` copies of the container and tells each one its index in
``CLOUD_RUN_TASK_INDEX`` (and the total in ``CLOUD_RUN_TASK_COUNT``). The platform therefore
submits *homogeneous* children of a fan-out parent (same kind, resource class, base image and
timeout) as **one** execution, and each task maps its index to one child spec:

* :func:`group_homogeneous` packs children into batches that may share an execution;
* :func:`task_nonce` derives the per-task bootstrap nonce from the execution nonce, so every
  task reads (and deletes) its *own* ``bootstrap/<nonce>`` object — the first task deleting a
  shared object would leave the others without their RunSpec;
* :func:`child_for_task` / :class:`TaskEnv` are what a supervisor uses to find its child;
* outputs of task ``i`` go under ``.../a{n}/tasks/{i}/`` (:func:`mapi_spec.storage.run_prefix`
  with ``task_index``), so the finalizer can verify each child separately;
* task ``i`` numbers its events from :func:`task_seq` ``(i, 0)``, so ``(run, attempt, seq)``
  deduplication never drops another task's events.

Fan-out *parents* (``Sweep``, ``SBC``, ``Backtest``) arrive in M4; this module is the executor
side of that contract.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from mapi_spec.executor import PLUGIN_LABEL, RunSubmit
from mapi_spec.run import ENV_NONCE

__all__ = [
    "ENV_TASK_ATTEMPT",
    "ENV_TASK_COUNT",
    "ENV_TASK_INDEX",
    "TASK_SEQ_STRIDE",
    "TaskEnv",
    "batch_key",
    "child_for_task",
    "group_homogeneous",
    "task_nonce",
    "task_nonces",
    "task_seq",
]

ENV_TASK_INDEX: Final = "CLOUD_RUN_TASK_INDEX"
ENV_TASK_COUNT: Final = "CLOUD_RUN_TASK_COUNT"
ENV_TASK_ATTEMPT: Final = "CLOUD_RUN_TASK_ATTEMPT"

_NONCE = re.compile(r"^[0-9a-f]{64}$")
_TASK_CONTEXT = b"mapi.task-nonce.v1\x00"


def task_nonce(nonce: str, index: int, count: int) -> str:
    """The bootstrap nonce of task ``index`` of a ``count``-task execution.

    A single-task execution uses the execution nonce itself (the M1 contract is unchanged);
    task ``i`` of a fan-out uses ``sha256(context || nonce || i)``. Anyone holding the
    execution nonce can derive it — the nonce is not a secret, the bootstrap object it names
    is (and the object is deleted on first read).
    """
    if not _NONCE.match(nonce):
        raise ValueError("nonce must be 64 lowercase hex characters")
    if count < 1 or not 0 <= index < count:
        raise ValueError(f"task index {index} is outside 0..{count - 1}")
    if count == 1:
        return nonce
    return hashlib.sha256(_TASK_CONTEXT + nonce.encode() + str(index).encode()).hexdigest()


TASK_SEQ_STRIDE: Final = 1 << 32
"""Event ``seq`` space per task: events are deduplicated on ``(run, attempt, seq)``, so the
tasks of one execution must not reuse each other's sequence numbers."""


def task_seq(index: int, seq: int) -> int:
    """The event ``seq`` of a task's ``seq``-th event (task 0 keeps the M1 numbering)."""
    if index < 0 or not 0 <= seq < TASK_SEQ_STRIDE:
        raise ValueError("index must be >= 0 and seq within the task's stride")
    return index * TASK_SEQ_STRIDE + seq


def task_nonces(nonce: str, count: int) -> list[str]:
    """Every task's bootstrap nonce, in index order."""
    return [task_nonce(nonce, i, count) for i in range(count)]


@dataclass(frozen=True, slots=True)
class TaskEnv:
    """What one task learns from its environment (no secrets)."""

    nonce: str
    index: int = 0
    count: int = 1
    attempt: int = 0

    @classmethod
    def from_environ(cls, env: Mapping[str, str]) -> TaskEnv:
        """Parse ``MAPI_NONCE`` plus the Cloud Run task variables (absent = one task)."""

        def _int(name: str, default: int) -> int:
            raw = env.get(name)
            if raw is None or raw == "":
                return default
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(f"{name} must be an integer") from None
            if value < 0:
                raise ValueError(f"{name} must be >= 0")
            return value

        nonce = env.get(ENV_NONCE, "")
        index, count = _int(ENV_TASK_INDEX, 0), _int(ENV_TASK_COUNT, 1)
        if count < 1 or index >= count:
            raise ValueError(f"{ENV_TASK_INDEX}={index} is outside 0..{count - 1}")
        task = cls(nonce=nonce, index=index, count=count, attempt=_int(ENV_TASK_ATTEMPT, 0))
        task.bootstrap_nonce()  # validates the nonce
        return task

    def bootstrap_nonce(self) -> str:
        return task_nonce(self.nonce, self.index, self.count)


def child_for_task[T](children: Sequence[T], task: TaskEnv) -> T:
    """The child spec task ``task.index`` runs (the batch must have exactly one per task)."""
    if len(children) != task.count:
        raise ValueError(
            f"the batch has {len(children)} children but the execution runs {task.count} tasks"
        )
    return children[task.index]


def batch_key(rs: RunSubmit) -> tuple[Hashable, ...]:
    """What children must share to run as tasks of one execution.

    The executor overrides only env, task count and timeout: everything else (image, CPU,
    memory) is fixed per Job, so kind, resource class, plugin (→ base image) and timeout must
    agree. Labels other than the plugin label may differ (they never reach the backend).
    """
    return (
        rs.kind.value,
        rs.resource_class.value,
        rs.labels.get(PLUGIN_LABEL),
        rs.timeout.seconds,
    )


def group_homogeneous[T](
    children: Iterable[T],
    key: Callable[[T], Hashable],
    *,
    max_tasks: int,
) -> list[list[T]]:
    """Pack ``children`` into batches of identical ``key`` with at most ``max_tasks`` each.

    Order is preserved inside a batch, and batches appear in the order of their first child,
    so task indexes are stable for a given input.
    """
    if max_tasks < 1:
        raise ValueError("max_tasks must be >= 1")
    open_batches: dict[Hashable, list[T]] = {}
    out: list[list[T]] = []
    for child in children:
        k = key(child)
        batch = open_batches.get(k)
        if batch is None or len(batch) >= max_tasks:
            batch = []
            open_batches[k] = batch
            out.append(batch)
        batch.append(child)
    return out
