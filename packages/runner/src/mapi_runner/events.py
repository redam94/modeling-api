"""The supervisor's event stream: HMAC-signed ``{"mapi_event": ...}`` JSONL on real stdout.

Only :class:`EventEmitter` writes to the supervisor's stdout; everything else the process
might print is redirected to stderr at start-up. Each line carries ``seq`` (0, 1, 2, ... per
attempt) and an HMAC under the per-run key, so neither worker output (which is wrapped in
``log`` events) nor anything else can forge an event.

:class:`LogGate` turns captured worker output into capped ``log`` events (4 KB per line,
20 MB per run by default; exceeding the total is ``SANDBOX_VIOLATION(log_flood)``) and
:class:`ProgressCoalescer` keeps at most one ``progress`` event per interval.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import (
    LogPayload,
    ProgressPayload,
    encode_event_line,
    make_event,
)
from pydantic import BaseModel

from mapi_runner._text import LineSplitter, clean_line, truncate_utf8

__all__ = ["EventEmitter", "LogFlood", "LogGate", "ProgressCoalescer"]

_LOG_LINE_MAX_CHARS = 4096  # LogPayload.line max_length


class EventEmitter:
    """Signs events and writes them, one line each, to ``fd`` (thread-safe)."""

    def __init__(
        self,
        fd: int,
        *,
        run_id: str,
        attempt: int,
        key: bytes,
        max_event_bytes: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.fd = fd
        self.run_id = run_id
        self.attempt = attempt
        self._key = key
        self.max_event_bytes = max_event_bytes
        self._clock = clock
        self._seq = 0
        self._lock = threading.Lock()

    @property
    def next_seq(self) -> int:
        return self._seq

    def emit(self, payload: BaseModel) -> int:
        """Sign and write one event; returns its ``seq``.

        Raises ``MapiError(SANDBOX_VIOLATION)`` when the encoded line exceeds the run's
        ``max_event_bytes`` (the seq is not consumed then).
        """
        with self._lock:
            event = make_event(
                self.run_id, self.attempt, self._seq, payload, ts=self._clock(), key=self._key
            )
            try:
                line = encode_event_line(event)
            except ValueError as exc:
                raise MapiError(str(exc), code=ErrorCode.SANDBOX_VIOLATION) from None
            raw = (line + "\n").encode("utf-8")
            if len(raw) - 1 > self.max_event_bytes:
                raise MapiError(
                    f"event of {len(raw) - 1} bytes exceeds max_event_bytes",
                    code=ErrorCode.SANDBOX_VIOLATION,
                )
            _write_all(self.fd, raw)
            seq = self._seq
            self._seq += 1
            return seq


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


class LogFlood(MapiError):
    """The worker wrote more than ``log_total_bytes``."""

    default_code = ErrorCode.SANDBOX_VIOLATION


class LogGate:
    """Captured worker stdout/stderr -> capped, cleaned ``log`` events."""

    def __init__(self, emit: Callable[[BaseModel], int], *, line_bytes: int, total_bytes: int):
        self._emit = emit
        self.line_bytes = line_bytes
        self.total_bytes = total_bytes
        self.received = 0
        self._splitters: dict[str, LineSplitter] = {}

    def _splitter(self, stream: str) -> LineSplitter:
        sp = self._splitters.get(stream)
        if sp is None:
            # keep a little slack: a cut line is reported as truncated anyway
            sp = self._splitters[stream] = LineSplitter(self.line_bytes)
        return sp

    def feed(self, stream: str, data: bytes) -> None:
        """Account for ``data`` and emit every complete line (raises :class:`LogFlood`)."""
        self.received += len(data)
        if self.received > self.total_bytes:
            raise LogFlood(
                f"the worker wrote more than {self.total_bytes} bytes of output (log_flood)"
            )
        for raw, cut in self._splitter(stream).feed(data):
            self._line(stream, raw, cut)

    def close(self, stream: str) -> None:
        for raw, cut in self._splitter(stream).flush():
            self._line(stream, raw, cut)

    def _line(self, stream: str, raw: bytes, cut: bool) -> None:
        text = clean_line(raw.decode("utf-8", errors="replace"))
        if not text.strip():
            return
        text, cut2 = truncate_utf8(text, self.line_bytes)
        if len(text) > _LOG_LINE_MAX_CHARS:
            text, cut2 = text[:_LOG_LINE_MAX_CHARS], True
        self._emit(LogPayload(stream=stream, line=text, truncated=cut or cut2))

    def system(self, message: str) -> None:
        """A supervisor-authored log line (not counted against the worker's budget)."""
        text, cut = truncate_utf8(clean_line(message), min(self.line_bytes, _LOG_LINE_MAX_CHARS))
        self._emit(LogPayload(stream="system", line=text, truncated=cut))


class ProgressCoalescer:
    """At most one ``progress`` event per ``interval`` seconds; the newest one wins."""

    def __init__(
        self,
        emit: Callable[[BaseModel], int],
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit = emit
        self.interval = interval
        self._clock = clock
        self._last: float | None = None
        self._pending: ProgressPayload | None = None

    def offer(self, payload: ProgressPayload) -> None:
        now = self._clock()
        if self._last is None or now - self._last >= self.interval:
            self._pending = None
            self._last = now
            self._emit(payload)
        else:
            self._pending = payload

    def due_in(self) -> float | None:
        """Seconds until a pending event may be sent (``None`` when nothing is pending)."""
        if self._pending is None or self._last is None:
            return None
        return max(0.0, self.interval - (self._clock() - self._last))

    def tick(self) -> None:
        """Send the pending event if its interval has elapsed."""
        if (
            self._pending is not None
            and self._last is not None
            and self._clock() - self._last >= self.interval
        ):
            self._flush()

    def flush(self) -> None:
        """Before a state change or the end: send the pending event if it is due, else drop
        it (the interval is a hard limit; state events carry the phase anyway)."""
        self.tick()
        self._pending = None

    def _flush(self) -> None:
        payload, self._pending = self._pending, None
        self._last = self._clock()
        assert payload is not None
        self._emit(payload)
