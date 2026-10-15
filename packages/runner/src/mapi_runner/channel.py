"""The supervisor <-> worker message channel (a ``socketpair``; plan §8.7).

Frames are a 4-byte big-endian length followed by one UTF-8 JSON object. Every message is
validated against the models below; the worker is untrusted, so anything malformed,
oversized or of an unknown type is a ``SANDBOX_VIOLATION`` on the supervisor side.

Supervisor -> worker: exactly one :class:`StartMessage` (no HMAC key, no output URLs).
Worker -> supervisor: ``state``, ``progress``, ``diagnostic``, ``warning``, ``metric``,
``checkpoint``, then exactly one terminal ``result`` or ``error``.
"""

from __future__ import annotations

import json
import socket
import struct
from datetime import datetime
from typing import Annotated, Final, Literal

from mapi_spec._types import StrictModel
from mapi_spec.capabilities import Diagnostic
from mapi_spec.errors import ErrorCode, Issue
from mapi_spec.executor import RunKind
from mapi_spec.run import (
    MetricPayload,
    ProgressPayload,
    StatePayload,
    WarningPayload,
)
from mapi_spec.storage import SignedGet
from pydantic import AwareDatetime, Field, JsonValue, TypeAdapter, ValidationError

__all__ = [
    "MAX_START_FRAME",
    "MAX_WORKER_FRAME",
    "CheckpointMessage",
    "DiagnosticMessage",
    "ErrorMessage",
    "FrameReader",
    "MetricMessage",
    "ProgressMessage",
    "ProtocolError",
    "ResultMessage",
    "StartMessage",
    "StateMessage",
    "WarningMessage",
    "WorkerMessage",
    "encode_frame",
    "parse_worker_message",
    "recv_frame",
    "send_frame",
]

MAX_WORKER_FRAME: Final = 256 * 1024
"""Largest frame the supervisor accepts from the worker."""
MAX_START_FRAME: Final = 16 * 1024 * 1024
"""Largest start frame the worker accepts from the supervisor."""

_HEADER = struct.Struct(">I")
_CKPT_NAME = r"^ckpt-[0-9a-f]{16,64}\.(?:meta\.json|draws\.zarr\.zip)$"


class ProtocolError(Exception):
    """A malformed, oversized or out-of-order frame."""


# ------------------------------------------------------------------------------- messages


class StartMessage(StrictModel):
    """Everything the worker needs, and nothing secret (no HMAC key, no write URLs)."""

    type: Literal["start"] = "start"
    run_id: str
    attempt: int = Field(..., ge=1)
    kind: RunKind
    tenant_id: str
    project_id: str
    spec: dict[str, JsonValue]
    inputs: dict[str, SignedGet] = Field(default_factory=dict)
    resume: dict[str, SignedGet] = Field(default_factory=dict)
    scratch: str = Field(..., description="Scratch root (inputs/, outputs/, checkpoints/, work/)")
    scratch_bytes: int | None = Field(None, ge=0)
    max_output_bytes: int = Field(..., ge=0)
    max_output_objects: int = Field(..., ge=1)
    checkpointing: bool = False
    deadline: AwareDatetime
    labels: dict[str, str] = Field(default_factory=dict)


class StateMessage(StrictModel):
    type: Literal["state"] = "state"
    payload: StatePayload


class ProgressMessage(StrictModel):
    type: Literal["progress"] = "progress"
    payload: ProgressPayload


class DiagnosticMessage(StrictModel):
    type: Literal["diagnostic"] = "diagnostic"
    payload: Diagnostic


class WarningMessage(StrictModel):
    type: Literal["warning"] = "warning"
    payload: WarningPayload


class MetricMessage(StrictModel):
    type: Literal["metric"] = "metric"
    payload: MetricPayload


class CheckpointMessage(StrictModel):
    """Checkpoint files were (re)written under ``{scratch}/checkpoints/``."""

    type: Literal["checkpoint"] = "checkpoint"
    files: list[Annotated[str, Field(pattern=_CKPT_NAME)]] = Field(..., min_length=1, max_length=8)
    segment: int = Field(..., ge=0)
    draws_done: int | None = Field(None, ge=0)


class ResultMessage(StrictModel):
    """The run finished; every output is a regular file under ``{scratch}/outputs/``."""

    type: Literal["result"] = "result"
    status: Literal["complete"] = "complete"


class ErrorMessage(StrictModel):
    """The run failed (or was cancelled). Text from the worker is untrusted."""

    type: Literal["error"] = "error"
    code: ErrorCode
    message: str = Field(..., max_length=2000)
    untrusted_text: str | None = Field(None, max_length=65536)
    issues: list[Issue] = Field(default_factory=list, max_length=100)
    at: datetime | None = None


WorkerMessage = Annotated[
    StateMessage
    | ProgressMessage
    | DiagnosticMessage
    | WarningMessage
    | MetricMessage
    | CheckpointMessage
    | ResultMessage
    | ErrorMessage,
    Field(discriminator="type"),
]
_WORKER_ADAPTER: TypeAdapter[WorkerMessage] = TypeAdapter(WorkerMessage)


def parse_worker_message(body: bytes) -> WorkerMessage:
    """Validate one frame body from the worker (raises :class:`ProtocolError`)."""
    try:
        return _WORKER_ADAPTER.validate_json(body)
    except ValidationError as exc:
        raise ProtocolError(f"invalid worker message: {exc.error_count()} error(s)") from None


# --------------------------------------------------------------------------------- framing


def encode_frame(message: StrictModel, *, max_bytes: int = MAX_WORKER_FRAME) -> bytes:
    body = json.dumps(
        message.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if len(body) > max_bytes:
        raise ProtocolError(f"frame of {len(body)} bytes exceeds {max_bytes}")
    return _HEADER.pack(len(body)) + body


def send_frame(
    sock: socket.socket, message: StrictModel, *, max_bytes: int = MAX_WORKER_FRAME
) -> None:
    sock.sendall(encode_frame(message, max_bytes=max_bytes))


def recv_frame(sock: socket.socket, *, max_bytes: int) -> bytes | None:
    """Blocking read of one frame body (``None`` on a clean EOF before a header)."""
    header = _recv_exact(sock, _HEADER.size, allow_eof=True)
    if header is None:
        return None
    (size,) = _HEADER.unpack(header)
    if size > max_bytes:
        raise ProtocolError(f"frame of {size} bytes exceeds {max_bytes}")
    body = _recv_exact(sock, size, allow_eof=False)
    assert body is not None
    return body


def _recv_exact(sock: socket.socket, n: int, *, allow_eof: bool) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if allow_eof and not buf:
                return None
            raise ProtocolError("channel closed in the middle of a frame")
        buf += chunk
    return bytes(buf)


class FrameReader:
    """Non-blocking incremental frame decoder (the supervisor's side of the channel)."""

    def __init__(self, max_bytes: int = MAX_WORKER_FRAME) -> None:
        self.max_bytes = max_bytes
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Add received bytes; return every complete frame body."""
        self._buf += data
        out: list[bytes] = []
        while len(self._buf) >= _HEADER.size:
            (size,) = _HEADER.unpack_from(self._buf)
            if size > self.max_bytes:
                raise ProtocolError(f"frame of {size} bytes exceeds {self.max_bytes}")
            if len(self._buf) < _HEADER.size + size:
                break
            out.append(bytes(self._buf[_HEADER.size : _HEADER.size + size]))
            del self._buf[: _HEADER.size + size]
        return out

    @property
    def pending(self) -> bool:
        """Bytes of an incomplete frame are buffered."""
        return bool(self._buf)
