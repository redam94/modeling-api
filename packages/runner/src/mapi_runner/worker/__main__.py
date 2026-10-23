"""``python -I -u -m mapi_runner.worker --channel-fd N``: the untrusted worker process.

Started only by the supervisor. It reads one start message from the channel, runs the run
kind through the SDK harness and reports exactly one ``result`` or ``error`` message. SIGTERM
cancels the run cooperatively (checked between phases and inside samplers); checkpoints
already written stay with the supervisor.

v0.19: ``--overlay DIR=NS=PVID`` (repeatable) names an upload the supervisor verified and
extracted read-only. Each directory is **appended** to ``sys.path`` (after the standard
library and site-packages; ``-I`` already ignores ``PYTHONPATH`` and ``site.addsitedir`` is
never called, so a ``.pth`` file in an overlay never runs) and recorded with
:func:`mapi_sdk.registry.set_process_overlays` before any entry point is loaded. Only the
overlays on the command line are mounted.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import os
import re
import signal
import socket
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from mapi_spec.errors import ErrorCode, MapiError

from mapi_runner.channel import (
    MAX_START_FRAME,
    ErrorMessage,
    ProtocolError,
    ResultMessage,
    StartMessage,
    recv_frame,
    send_frame,
)


def _error(exc: BaseException) -> ErrorMessage:
    if isinstance(exc, MapiError):
        return ErrorMessage(
            code=exc.code,
            message=exc.detail[:2000],
            untrusted_text=None if exc.untrusted_text is None else exc.untrusted_text[:65536],
            issues=list(exc.issues[:100]),
        )
    if isinstance(exc, MemoryError):
        return ErrorMessage(code=ErrorCode.RESOURCE_EXHAUSTED, message="the run ran out of memory")
    if isinstance(exc, NotImplementedError):
        return ErrorMessage(
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            message=(str(exc) or "this run kind is not implemented")[:2000],
        )
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return ErrorMessage(
        code=ErrorCode.USER_CODE_ERROR,
        message="the run failed with an unexpected error",
        untrusted_text=tb[-65536:],
    )


_NAMESPACE = re.compile(r"^t_[a-z2-7]{6}_[a-z][a-z0-9]{1,15}$")
_PVID = re.compile(r"^plv_[0-9a-z]{26}$")
_MAX_OVERLAYS = 8


def parse_overlay(value: str) -> tuple[Path, str, str]:
    """``DIR=NS=PVID`` -> ``(dir, namespace, pvid)``; the directory may hold ``=``."""
    head, sep1, pvid = value.rpartition("=")
    path, sep2, ns = head.rpartition("=")
    if not (sep1 and sep2 and path):
        raise ValueError("--overlay must look like DIR=NAMESPACE=PLUGIN_VERSION_ID")
    if not _NAMESPACE.match(ns) or not _PVID.match(pvid):
        raise ValueError("--overlay names an invalid namespace or plugin version id")
    d = Path(path)
    if not d.is_absolute() or not d.is_dir():
        raise ValueError("--overlay must name an existing absolute directory")
    return d, ns, pvid


def mount_overlays(values: Sequence[str]) -> list[tuple[Path, str, str]]:
    """Append each overlay directory to ``sys.path`` and record the overlays for the
    registry and the workflow loaders (before anything loads an entry point)."""
    overlays = [parse_overlay(v) for v in values]
    if not overlays:
        return []
    from mapi_sdk.registry import set_process_overlays

    if len(overlays) > _MAX_OVERLAYS:
        raise ValueError(f"a run mounts at most {_MAX_OVERLAYS} overlays")
    for d, _ns, _pvid in overlays:
        entry = os.fspath(d)
        if entry not in sys.path:
            sys.path.append(entry)  # last: never shadows the stdlib or site-packages
    importlib.invalidate_caches()
    set_process_overlays(overlays)
    return overlays


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mapi_runner.worker")
    p.add_argument("--channel-fd", type=int, required=True)
    p.add_argument("--registry", default=None)
    p.add_argument("--overlay", action="append", default=[], metavar="DIR=NS=PVID")
    args = p.parse_args(argv)
    try:
        overlays = mount_overlays(args.overlay)
    except ValueError as exc:
        print(f"worker: invalid overlay: {exc}", file=sys.stderr)
        return 3
    sock = socket.socket(fileno=args.channel_fd)
    try:
        body = recv_frame(sock, max_bytes=MAX_START_FRAME)
        if body is None:
            return 3
        start = StartMessage.model_validate_json(body)
    except (ProtocolError, ValueError) as exc:
        print(f"worker: invalid start message: {exc}", file=sys.stderr)
        return 3

    from mapi_sdk.steps import CancelToken

    from mapi_runner.worker.context import OverlaySpec, WorkerContext
    from mapi_runner.worker.kinds import run_kind

    token = CancelToken()

    def on_term(_sig: int, _frame: FrameType | None) -> None:
        token.cancel("terminated by the supervisor")

    signal.signal(signal.SIGTERM, on_term)
    ctx = WorkerContext(
        start,
        sock,
        cancel=token,
        registry_ref=args.registry,
        overlays=[OverlaySpec(d, ns, pvid) for d, ns, pvid in overlays],
    )
    try:
        run_kind(ctx)
        ctx.close()  # the last tracking points go out before the result
    except BaseException as exc:  # report everything; the supervisor classifies it
        if isinstance(exc, KeyboardInterrupt | SystemExit) and not token.cancelled:
            raise
        with contextlib.suppress(Exception):
            ctx.close()
        msg = _error(exc)
        if token.cancelled:
            msg = ErrorMessage(code=ErrorCode.CANCELLED, message="the run was cancelled")
        send_frame(sock, msg)
        return 0
    send_frame(sock, ResultMessage())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
