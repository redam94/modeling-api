"""Worker process isolation inside the execution (plan §8.1, §9).

When the supervisor runs as root (Cloud Run gen2 / docker), the worker is started as uid/gid
``10001`` with no supplementary groups, ``PR_SET_NO_NEW_PRIVS`` and no core dumps, and the
supervisor makes itself non-dumpable so the worker cannot ptrace it. Anywhere else (macOS,
a developer's non-root shell) the uid split is impossible: the run proceeds in *dev mode*
and the supervisor says so in a ``warning`` event. Dev mode is never a security boundary.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import resource
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "WORKER_GID",
    "WORKER_UID",
    "IsolationPlan",
    "harden_supervisor",
    "plan_isolation",
]

WORKER_UID: Final = 10001
WORKER_GID: Final = 10001
_PR_SET_DUMPABLE: Final = 4
_PR_SET_NO_NEW_PRIVS: Final = 38


@dataclass(frozen=True, slots=True)
class IsolationPlan:
    """How the worker will be started."""

    drop_privileges: bool
    no_new_privs: bool
    reason: str | None = None
    """Why isolation is reduced (dev mode); ``None`` when fully isolated."""

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
        """Runs in the forked worker before exec: no core dumps, no-new-privs."""
        return _preexec(self.no_new_privs)


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


def _preexec(no_new_privs: bool) -> Callable[[], None]:
    prctl = _prctl() if no_new_privs else None

    def run() -> None:  # pragma: no cover - runs in the forked child
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
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
