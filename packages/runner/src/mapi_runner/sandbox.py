"""Worker process isolation inside the execution (plan §8.1, §9).

When the supervisor runs as root (Cloud Run gen2 / docker), the worker is started as uid/gid
``10001`` with no supplementary groups, ``PR_SET_NO_NEW_PRIVS`` and no core dumps, and the
supervisor makes itself non-dumpable so the worker cannot ptrace it. Anywhere else (macOS,
a developer's non-root shell) the uid split is impossible: the run proceeds in *dev mode*
and the supervisor says so in a ``warning`` event. Dev mode is never a security boundary.

v0.19 (D18, G9): the worker also gets resource limits in its preexec
(:func:`worker_rlimits`): ``RLIMIT_NOFILE`` 4096 and ``RLIMIT_FSIZE`` = the class's scratch
budget (both per process), and ``RLIMIT_NPROC`` from the class (S 256, M 512, L 1024) **only
where the worker's uid is private to this execution** (Cloud Run Jobs, which runs every
execution in its own gVisor sandbox). ``RLIMIT_NPROC`` counts every process of the real uid
on the kernel: on docker all run containers share uid 10001 (one run could stop the others
from starting) and on local-process the uid is the developer's, so there the per-container
``pids_limit`` is the bound instead (WP0 probe P1). ``RLIMIT_AS`` stays off: JAX and PyTensor
reserve large address ranges.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import resource
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass

__all__ = [
    "NOFILE_LIMIT",
    "NPROC_LIMITS",
    "WORKER_GID",
    "WORKER_UID",
    "IsolationPlan",
    "harden_supervisor",
    "plan_isolation",
    "private_uid",
    "worker_rlimits",
]

WORKER_UID: Final = 10001
WORKER_GID: Final = 10001
_PR_SET_DUMPABLE: Final = 4
_PR_SET_NO_NEW_PRIVS: Final = 38

NOFILE_LIMIT: Final = 4096
"""Open files of the worker (per process)."""
NPROC_LIMITS: Final[Mapping[ResourceClass, int]] = {
    ResourceClass.S: 256,
    ResourceClass.M: 512,
    ResourceClass.L: 1024,
    ResourceClass.XL: 1024,
    ResourceClass.GPU: 1024,
}
"""Processes and threads of the worker's uid, per class (only applied for a private uid)."""
_PRIVATE_UID_ENV: Final = ("CLOUD_RUN_EXECUTION",)
"""Set by Cloud Run Jobs: every execution has its own sandbox, so uid 10001 is ours alone."""


def private_uid(environ: Mapping[str, str] | None = None) -> bool:
    """True if no other execution can run under the worker's uid on this kernel."""
    env = os.environ if environ is None else environ
    return any(env.get(k) for k in _PRIVATE_UID_ENV)


def _capped(limit: int, which: int) -> tuple[int, int]:
    """``(limit, limit)``, lowered to the current hard limit (an unprivileged process
    cannot raise it)."""
    try:
        _soft, hard = resource.getrlimit(which)
    except (OSError, ValueError):
        return limit, limit
    if hard != resource.RLIM_INFINITY and hard < limit:
        return hard, hard
    return limit, limit


def worker_rlimits(
    resource_class: ResourceClass, *, private: bool | None = None
) -> dict[int, tuple[int, int]]:
    """The ``setrlimit`` values for the worker of a ``resource_class`` run (module docs).

    ``private`` says whether the worker's uid is private to this execution (default:
    :func:`private_uid` of this process's environment); only then is ``RLIMIT_NPROC`` set."""
    out: dict[int, tuple[int, int]] = {}
    out[resource.RLIMIT_NOFILE] = _capped(NOFILE_LIMIT, resource.RLIMIT_NOFILE)
    scratch = RESOURCE_CLASSES[resource_class].scratch_bytes
    if scratch is not None:
        out[resource.RLIMIT_FSIZE] = _capped(scratch, resource.RLIMIT_FSIZE)
    if (private_uid() if private is None else private) and hasattr(resource, "RLIMIT_NPROC"):
        out[resource.RLIMIT_NPROC] = _capped(NPROC_LIMITS[resource_class], resource.RLIMIT_NPROC)
    return out


@dataclass(frozen=True, slots=True)
class IsolationPlan:
    """How the worker will be started."""

    drop_privileges: bool
    no_new_privs: bool
    reason: str | None = None
    """Why isolation is reduced (dev mode); ``None`` when fully isolated."""
    rlimits: Mapping[int, tuple[int, int]] = field(default_factory=dict)
    """v0.19: ``setrlimit`` values for the worker (:func:`worker_rlimits`)."""

    @property
    def dev_mode(self) -> bool:
        return self.reason is not None

    @property
    def user(self) -> int | None:
        return WORKER_UID if self.drop_privileges else None

    @property
    def group(self) -> int | None:
        return WORKER_GID if self.drop_privileges else None

    @property
    def extra_groups(self) -> list[int] | None:
        return [] if self.drop_privileges else None

    def preexec(self) -> Callable[[], None]:
        """Runs in the forked worker before exec: no core dumps, the resource limits,
        no-new-privs."""
        return _preexec(self.no_new_privs, dict(self.rlimits))

    def with_rlimits(self, rlimits: Mapping[int, tuple[int, int]]) -> IsolationPlan:
        """The same plan with the worker's resource limits."""
        return IsolationPlan(
            drop_privileges=self.drop_privileges,
            no_new_privs=self.no_new_privs,
            reason=self.reason,
            rlimits=dict(rlimits),
        )


def _prctl() -> Callable[..., int] | None:
    if not sys.platform.startswith("linux"):
        return None
    name = ctypes.util.find_library("c")
    if name is None:
        return None
    try:
        libc = ctypes.CDLL(name, use_errno=True)
    except OSError:
        return None
    fn: Callable[..., int] = libc.prctl
    return fn


def _preexec(
    no_new_privs: bool, rlimits: Mapping[int, tuple[int, int]] | None = None
) -> Callable[[], None]:
    prctl = _prctl() if no_new_privs else None
    limits = list((rlimits or {}).items())

    def run() -> None:  # pragma: no cover - runs in the forked child
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        for which, value in limits:
            try:
                resource.setrlimit(which, value)
            except (OSError, ValueError):
                os._exit(126)  # never start the worker with weaker limits than planned
        if prctl is not None and prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            os._exit(126)

    return run


def plan_isolation(*, euid: int | None = None, platform: str | None = None) -> IsolationPlan:
    """Decide how to isolate the worker on this host."""
    euid = os.geteuid() if euid is None else euid
    platform = sys.platform if platform is None else platform
    linux = platform.startswith("linux")
    has_prctl = linux and _prctl() is not None
    if euid == 0 and linux:
        return IsolationPlan(drop_privileges=True, no_new_privs=has_prctl)
    if euid == 0:
        return IsolationPlan(
            drop_privileges=True,
            no_new_privs=False,
            reason=f"no-new-privs is unavailable on {platform}",
        )
    reason = (
        "the supervisor is not root, so the worker runs under the same uid"
        if linux
        else f"running on {platform} as a regular user: no uid split and no no-new-privs"
    )
    return IsolationPlan(drop_privileges=False, no_new_privs=has_prctl, reason=reason)


def harden_supervisor() -> None:
    """Mark the supervisor non-dumpable (no ptrace/core from same-uid processes)."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    prctl = _prctl()
    if prctl is not None:
        prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
