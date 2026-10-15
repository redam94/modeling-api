"""``mapi-runner``: start the trusted supervisor of one execution.

All run configuration arrives through the environment (``MAPI_RUN_ID``, ``MAPI_NONCE``,
``MAPI_BOOTSTRAP_URL`` and, locally, ``MAPI_EXEC_TOKEN``); the options below only describe
the image (where the worker interpreter is, which plugin registry it loads) and never carry
secrets. Real stdout receives nothing but signed ``{"mapi_event": ...}`` lines: the original
stdout descriptor is kept for the event writer and fd 1 is pointed at stderr before anything
else runs.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from mapi_runner import __version__
from mapi_runner.sandbox import harden_supervisor
from mapi_runner.supervisor import Supervisor, SupervisorConfig

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mapi-runner",
        description="Run one modeling-api execution (supervisor + sandboxed worker).",
    )
    p.add_argument("--version", action="version", version=f"mapi-runner {__version__}")
    p.add_argument(
        "--worker-python",
        default=os.environ.get("MAPI_WORKER_PYTHON") or sys.executable,
        help="interpreter of the worker environment (default: this interpreter)",
    )
    p.add_argument(
        "--worker-registry",
        default=None,
        metavar="MODULE:CALLABLE",
        help="plugin registry factory for the worker (default: installed entry points)",
    )
    p.add_argument("--scratch-dir", default=None, help="parent directory for the run scratch")
    p.add_argument("--keep-scratch", action="store_true", help="do not delete the scratch dir")
    p.add_argument("--term-grace", type=float, default=5.0, metavar="SECONDS")
    p.add_argument("--deadline-reserve", type=float, default=15.0, metavar="SECONDS")
    p.add_argument("--base-image", default=os.environ.get("MAPI_BASE_IMAGE") or None)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sys.stdout.flush()
    event_fd = os.dup(1)
    os.set_inheritable(event_fd, False)
    os.dup2(2, 1)  # stray prints (libraries, warnings) go to stderr, never to the event stream
    harden_supervisor()
    config = SupervisorConfig(
        worker_python=args.worker_python,
        worker_registry=args.worker_registry,
        scratch_root=args.scratch_dir,
        keep_scratch=args.keep_scratch,
        term_grace_s=args.term_grace,
        deadline_reserve_s=args.deadline_reserve,
        base_image=args.base_image,
    )
    try:
        return Supervisor(config, os.environ, event_fd=event_fd).run()
    finally:
        os.close(event_fd)


if __name__ == "__main__":
    raise SystemExit(main())
