"""``python -I -u -m mapi_runner.worker --channel-fd N``: the untrusted worker process.

Started only by the supervisor. It reads one start message from the channel, runs the run
kind through the SDK harness and reports exactly one ``result`` or ``error`` message. SIGTERM
cancels the run cooperatively (checked between phases and inside samplers); checkpoints
already written stay with the supervisor.
"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import traceback
from collections.abc import Sequence
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


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mapi_runner.worker")
    p.add_argument("--channel-fd", type=int, required=True)
    p.add_argument("--registry", default=None)
    args = p.parse_args(argv)
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

    from mapi_runner.worker.context import WorkerContext
    from mapi_runner.worker.kinds import run_kind

    token = CancelToken()

    def on_term(_sig: int, _frame: FrameType | None) -> None:
        token.cancel("terminated by the supervisor")

    signal.signal(signal.SIGTERM, on_term)
    ctx = WorkerContext(start, sock, cancel=token, registry_ref=args.registry)
    try:
        run_kind(ctx)
    except BaseException as exc:  # report everything; the supervisor classifies it
        if isinstance(exc, KeyboardInterrupt | SystemExit) and not token.cancelled:
            raise
        msg = _error(exc)
        if token.cancelled:
            msg = ErrorMessage(code=ErrorCode.CANCELLED, message="the run was cancelled")
        send_frame(sock, msg)
        return 0
    send_frame(sock, ResultMessage())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
