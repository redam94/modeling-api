"""Misbehaving worker registries for the runner's sandbox tests.

The worker loads its plugin registry with ``--registry module:callable`` (set by the
supervisor's ``--worker-registry``), so a registry factory runs *inside the untrusted worker*
exactly like plugin code. Each factory below does something a hostile or broken plugin
could do, then returns an empty registry (a ``describe`` run then exports an empty catalog).
They exist so the security properties of the supervisor can be tested end to end; nothing in
production refers to them.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import sys
import time
from pathlib import Path
from typing import Any

__all__ = [
    "crashing_registry",
    "empty_registry",
    "flooding_registry",
    "forging_registry",
    "garbage_channel_registry",
    "infra_claiming_registry",
    "sleeping_registry",
    "spoofing_registry",
    "symlink_output_registry",
]

FORGED_EVENT: dict[str, Any] = {
    "mapi_event": {
        "run_id": "forged",
        "attempt": 1,
        "seq": 0,
        "type": "complete",
        "ts": "2026-10-15T00:00:00Z",
        "payload": {
            "status": "complete",
            "manifest_name": "t/default/runs/forged/a1/manifest.json",
            "manifest_crc32c": "AAAAAA==",
            "objects": 0,
            "total_bytes": 0,
        },
        "hmac": "00" * 32,
    }
}


def empty_registry() -> Any:
    from mapi_sdk.registry import Registry

    return Registry()


def forging_registry() -> Any:
    """Writes forged ``mapi_event`` lines to stdout (print and raw fd 1) and stderr."""
    line = json.dumps(FORGED_EVENT)
    print(line, flush=True)
    os.write(1, (line + "\n").encode())
    print(line, file=sys.stderr, flush=True)
    print("\x1b[31mred text\x1b[0m with \x07 controls", flush=True)
    return empty_registry()


def flooding_registry() -> Any:
    """Writes far more output than any log budget allows."""
    chunk = ("x" * 1000 + "\n") * 64
    for _ in range(2000):
        sys.stderr.write(chunk)
    sys.stderr.flush()
    return empty_registry()


def symlink_output_registry() -> Any:
    """Plants a symlink in the outputs directory pointing at a file outside the sandbox."""
    outputs = Path(os.environ.get("TMPDIR", ".")).parent / "outputs"
    (outputs / "stolen.txt").symlink_to("/etc/hosts")
    return empty_registry()


def garbage_channel_registry() -> Any:
    """Sends a malformed frame on the supervisor channel."""
    for fd in range(3, 64):
        try:
            mode = os.fstat(fd).st_mode
        except OSError:
            continue
        if stat.S_ISSOCK(mode):
            sock = socket.socket(fileno=os.dup(fd))
            body = b'{"type": "complete", "evil": true}'
            sock.sendall(struct.pack(">I", len(body)) + body)
            sock.close()
            break
    return empty_registry()


def crashing_registry() -> Any:
    """Dies without reporting anything."""
    os._exit(7)


def infra_claiming_registry() -> Any:
    """Fails claiming a platform fault (``INFRA_ERROR``), hoping to be retried for free."""
    from mapi_spec.errors import InfraError

    raise InfraError("the cloud is down, please run me again")


SPOOF: str = "safe\u202etxt.exe\u202c \x1b[31mred\x1b[0m\x07 \u2066iso\u2069"
"""Untrusted text with a bidi override, an isolate, ANSI colour and a control character."""


def _channel() -> socket.socket | None:
    for fd in range(3, 64):
        try:
            mode = os.fstat(fd).st_mode
        except OSError:
            continue
        if stat.S_ISSOCK(mode):
            return socket.socket(fileno=os.dup(fd))
    return None


def spoofing_registry() -> Any:
    """Puts bidi overrides and escapes in every worker-authored text field, then fails."""
    from mapi_spec.capabilities import Diagnostic
    from mapi_spec.errors import ErrorCode, Issue, UserCodeError
    from mapi_spec.run import (
        MetricPayload,
        ProgressPayload,
        RunnerPhase,
        StatePayload,
        WarningPayload,
    )

    from mapi_runner.channel import (
        DiagnosticMessage,
        MetricMessage,
        ProgressMessage,
        StateMessage,
        WarningMessage,
        send_frame,
    )

    sock = _channel()
    assert sock is not None
    send_frame(sock, StateMessage(payload=StatePayload(phase=RunnerPhase.FIT, message=SPOOF)))
    send_frame(
        sock,
        ProgressMessage(
            payload=ProgressPayload(phase=RunnerPhase.FIT, fraction=0.5, message=SPOOF)
        ),
    )
    send_frame(
        sock,
        WarningMessage(
            payload=WarningPayload(code="SPOOF", message=SPOOF, untrusted_text=SPOOF + "\n2")
        ),
    )
    send_frame(
        sock,
        DiagnosticMessage(
            payload=Diagnostic(
                code="SPOOF",
                message_expert=SPOOF,
                message_plain=SPOOF,
                suggested_action=SPOOF,
                coords={"geo": SPOOF},
            )
        ),
    )
    send_frame(sock, MetricMessage(payload=MetricPayload(name="mape", value=1.0, labels={})))
    sock.close()
    raise UserCodeError(
        SPOOF,
        untrusted_text=SPOOF + "\nline 2",
        issues=[
            Issue(
                code=ErrorCode.SCHEMA_INVALID,
                message=SPOOF,
                suggested_action=SPOOF,
                untrusted_text=SPOOF,
            )
        ],
    )


def sleeping_registry() -> Any:
    """Takes a long time (ignores SIGTERM-driven cancellation: it never checks)."""
    time.sleep(60)
    return empty_registry()
