"""Same-uid hardening of the long-running control-plane processes.

Under the ``local-process`` executor (``make dev``, the default compose worker) every run's
supervisor and worker share the orchestrator's uid. :func:`harden_process` makes the calling
process non-dumpable (Linux ``PR_SET_DUMPABLE=0``) and disables core dumps, so a same-uid
process can neither ptrace it nor read its ``/proc/<pid>/environ`` (``DATABASE_URL`` with the
password, ``MAPI_EXEC_TOKEN``) or ``/proc/<pid>/mem`` (keys loaded in memory). The flag is
reset by ``execve``, so runner subprocesses are unaffected (the supervisor hardens itself).

This does **not** protect files: key files readable by the orchestrator's uid stay readable
by run code under the same uid; the local-process executor names them in its start-up
warning. The real boundary is a separate uid or container (docker executor, M2 uid split).
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import resource
import sys
from collections.abc import Callable
from typing import Final

__all__ = ["PR_SET_DUMPABLE", "harden_process"]

PR_SET_DUMPABLE: Final = 4


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


def harden_process() -> bool:
    """No core dumps; non-dumpable on Linux. ``True`` if the process is now non-dumpable."""
    with contextlib.suppress(ValueError, OSError):  # a hard limit we may not lower
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    prctl = _prctl()
    if prctl is None:
        return False
    return prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
