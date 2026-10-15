"""Supervisor diagnostics as structured, stdlib-only log lines (plan §8.7).

The supervisor runs inside the execution, where the only channel out is its HMAC-signed
stdout (run events) — telemetry must never egress (zero-egress exec project, §8, §9). So the
supervisor's *own* diagnostics go to **stderr**, which the executor captures out of band and
the control plane turns into ``mapi_log`` lines. This module formats those diagnostics as one
JSON object per line carrying the run id, attempt and a per-run correlation id, so an
operator reading the captured stderr can join a supervisor message to its run and trace.

It deliberately uses only the standard library: the supervisor's dependency surface is
``mapi-spec`` plus ``httpx`` (import-linter), and no OpenTelemetry or structlog is pulled
into the sandbox image. ``MAPI_LOG_JSON`` (default on) selects JSON; anything else prints the
legacy ``mapi-runner: <message>`` line so local output stays readable.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Final

__all__ = ["ENV_LOG_JSON", "ENV_TRACEPARENT", "StderrLog", "trace_id_from_env"]

ENV_LOG_JSON: Final = "MAPI_LOG_JSON"
ENV_TRACEPARENT: Final = "MAPI_TRACEPARENT"

_TRACEPARENT_LEN = 55  # version(2)-traceid(32)-spanid(16)-flags(2) with dashes


def trace_id_from_env(environ: dict[str, str] | None = None) -> str | None:
    """The W3C trace id (hex) from a ``traceparent`` the executor passed, if well-formed.

    Lets a run's supervisor diagnostics carry the same trace id as the control-plane span
    that dispatched it, without importing a tracing library into the sandbox.
    """
    env = os.environ if environ is None else environ
    value = env.get(ENV_TRACEPARENT, "")
    if len(value) != _TRACEPARENT_LEN:
        return None
    parts = value.split("-")
    if len(parts) != 4 or len(parts[1]) != 32:
        return None
    try:
        int(parts[1], 16)
    except ValueError:
        return None
    return parts[1]


class StderrLog:
    """A minimal JSON logger writing one line per message to file descriptor 2."""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        attempt: int | None = None,
        trace_id: str | None = None,
        json_output: bool | None = None,
        fd: int = 2,
    ) -> None:
        self.run_id = run_id
        self.attempt = attempt
        self.trace_id = trace_id
        self.fd = fd
        if json_output is None:
            json_output = os.environ.get(ENV_LOG_JSON, "1") != "0"
        self.json_output = json_output

    def bind(self, *, run_id: str, attempt: int, trace_id: str | None = None) -> None:
        """Attach the run identity once the bootstrap object has been read."""
        self.run_id = run_id
        self.attempt = attempt
        if trace_id is not None:
            self.trace_id = trace_id

    def log(self, message: str, *, level: str = "info", **fields: object) -> None:
        if self.json_output:
            record: dict[str, object] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "level": level,
                "logger": "mapi_runner.supervisor",
                "event": message,
            }
            if self.run_id is not None:
                record["run_id"] = self.run_id
            if self.attempt is not None:
                record["attempt"] = self.attempt
            if self.trace_id is not None:
                record["trace_id"] = self.trace_id
            record.update({k: v for k, v in fields.items() if v is not None})
            line = json.dumps(record, default=str)
        else:
            line = f"mapi-runner: {message}"
        self._write(line)

    def _write(self, line: str) -> None:
        with contextlib.suppress(OSError):
            os.write(self.fd, (line + "\n").encode("utf-8", errors="replace"))
