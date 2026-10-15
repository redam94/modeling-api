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
    "sleeping_registry",
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


def sleeping_registry() -> Any:
    """Takes a long time (ignores SIGTERM-driven cancellation: it never checks)."""
    time.sleep(60)
    return empty_registry()
