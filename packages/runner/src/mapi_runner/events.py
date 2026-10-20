"""The supervisor's event stream: HMAC-signed ``{"mapi_event": ...}`` JSONL on real stdout.

Only :class:`EventEmitter` writes to the supervisor's stdout; everything else the process
might print is redirected to stderr at start-up. Each line carries ``seq`` (0, 1, 2, ... per
attempt) and an HMAC under the per-run key, so neither worker output (which is wrapped in
``log`` events) nor anything else can forge an event.

:class:`LogGate` turns captured worker output into capped ``log`` events (4 KB per line,
20 MB per run by default; exceeding the total is ``SANDBOX_VIOLATION(log_flood)``),
:class:`ProgressCoalescer` keeps at most one ``progress`` event per interval and
:class:`TrackingGate` turns the worker's tracking messages (``metrics``, ``params``, ``tags``,
``user_log``) into budgeted events split under the event cap. :class:`TrackingBudget` holds
the per-run tracking budgets; the worker keeps the same budget, so a well-behaved worker
drops (and counts) points before the supervisor has to.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import (
    LogPayload,
    MetricBatchPayload,
    ParamsPayload,
    ProgressPayload,
    TagsPayload,
    WarningPayload,
    encode_event_line,
    make_event,
    split_for_events,
)
from mapi_spec.tracking import ParamValue, TrackingLimits, labels_key
from pydantic import BaseModel, ValidationError

from mapi_runner._text import LineSplitter, clean_line, clean_tree, truncate_utf8
from mapi_runner.channel import (
    MetricsMessage,
    ParamsMessage,
    ProtocolError,
    TagsMessage,
    UserLogMessage,
)

__all__ = [
    "TRACKING_BUDGET",
    "EventEmitter",
    "LogFlood",
    "LogGate",
    "ProgressCoalescer",
    "TrackingBudget",
    "TrackingFlood",
    "TrackingGate",
    "clean_payload",
    "log_size",
]

TRACKING_BUDGET: Final = "TRACKING_BUDGET"
"""Warning code when tracking points, params, tags or log lines were dropped."""
_MULTILINE: Final = frozenset({"untrusted_text"})

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


def clean_payload[M: BaseModel](payload: M) -> M:
    """A worker-authored payload with every string stripped of ANSI escapes, control
    characters and bidi overrides (as captured log lines are), so it cannot spoof text in
    the web app or the CLI. ``untrusted_text`` keeps its line breaks."""
    raw = clean_tree(payload.model_dump(mode="json", by_alias=True), multiline_keys=_MULTILINE)
    try:
        return type(payload).model_validate(raw)
    except ValidationError as exc:
        raise ProtocolError(f"invalid worker message after text cleaning: {exc}") from exc


# ------------------------------------------------------------------------------ tracking


def log_size(payload: LogPayload) -> int:
    """Bytes a user log line counts against ``TrackingLimits.log_bytes`` (line + fields)."""
    n = len(payload.line.encode("utf-8"))
    if payload.fields:
        n += len(json.dumps(payload.fields, separators=(",", ":"), ensure_ascii=False).encode())
    return n


class TrackingBudget:
    """The per-run tracking budgets of :class:`~mapi_spec.tracking.TrackingLimits`.

    Not thread-safe (callers hold their own lock). Every refusal is counted under a reason
    in :attr:`dropped`; :meth:`summary` names the largest ones.
    """

    def __init__(self, limits: TrackingLimits) -> None:
        self.limits = limits
        self.per_series: dict[tuple[str, str], int] = {}
        self.scalars: set[tuple[str, str]] = set()
        self.points = 0
        self.params: dict[str, ParamValue] = {}
        self.tags: set[str] = set()
        self.log_lines = 0
        self.log_bytes = 0
        self.dropped: Counter[str] = Counter()

    def point(self, key: str, lkey: str, step: int | None = None) -> bool:
        """Account one metric point of series ``(key, lkey)``; ``False`` = drop it.

        A scalar (``step=None``) is stored once per series (the newest value wins), so only
        its first update counts against the budgets; later updates are always kept.
        """
        lim = self.limits
        series = (key, lkey)
        if step is None and series in self.scalars:
            return True
        n = self.per_series.get(series)
        if n is None:
            if len(self.per_series) >= lim.metric_series:
                self.dropped[f"metric series over the limit of {lim.metric_series}"] += 1
                return False
            n = 0
        if n >= lim.metric_points_per_series:
            name = f"{key}{{{lkey}}}" if lkey else key
            self.dropped[
                f"metric {name}: points over the limit of {lim.metric_points_per_series}"
            ] += 1
            return False
        if self.points >= lim.metric_points_total:
            self.dropped[f"metric points over the limit of {lim.metric_points_total}"] += 1
            return False
        self.per_series[series] = n + 1
        self.points += 1
        if step is None:
            self.scalars.add(series)
        return True

    def param(self, key: str, value: ParamValue) -> bool:
        """Account one param; the first value of a key wins (``False`` = drop it)."""
        if key in self.params:
            if repr(self.params[key]) != repr(value):
                self.dropped["parameters sent again with a different value"] += 1
            return False
        if len(self.params) >= self.limits.params:
            self.dropped[f"parameters over the limit of {self.limits.params}"] += 1
            return False
        self.params[key] = value
        return True

    def tag(self, key: str) -> bool:
        """Account one tag (the last value of a key wins; only new keys count)."""
        if key in self.tags:
            return True
        if len(self.tags) >= self.limits.tags:
            self.dropped[f"tags over the limit of {self.limits.tags}"] += 1
            return False
        self.tags.add(key)
        return True

    def log(self, nbytes: int) -> bool:
        """Account one user log line of ``nbytes`` (``False`` = drop it)."""
        lim = self.limits
        if self.log_lines >= lim.log_lines:
            self.dropped[f"log lines over the limit of {lim.log_lines}"] += 1
            return False
        if self.log_bytes + nbytes > lim.log_bytes:
            self.dropped[f"log text over the limit of {lim.log_bytes} bytes"] += 1
            return False
        self.log_lines += 1
        self.log_bytes += nbytes
        return True

    def summary(self, top: int = 5) -> str | None:
        """``None`` when nothing was dropped, else the ``top`` largest reasons."""
        if not self.dropped:
            return None
        parts = [f"{reason} ({n} dropped)" for reason, n in self.dropped.most_common(top)]
        if len(self.dropped) > top:
            parts.append(f"{len(self.dropped) - top} other reasons")
        return "; ".join(parts)

    def warning(self) -> WarningPayload | None:
        """The one ``TRACKING_BUDGET`` warning of a run that dropped something."""
        text = self.summary()
        if text is None:
            return None
        return WarningPayload(
            code=TRACKING_BUDGET,
            message=(
                "The run logged more than its tracking budget allows. Some points were not "
                f"kept: {text}"
            )[:2000],
        )


class TrackingFlood(MapiError):
    """The worker sent more than ``tracking_bytes`` of tracking messages."""

    default_code = ErrorCode.SANDBOX_VIOLATION


TrackingMessage = MetricsMessage | ParamsMessage | TagsMessage | UserLogMessage


class TrackingGate:
    """Worker tracking messages -> budgeted, cleaned events, each under the event cap.

    The total size of tracking frames is capped (``tracking_bytes``: above it the run is a
    ``SANDBOX_VIOLATION``); points, params, tags and user log lines over a budget are dropped
    and counted (:meth:`warning` reports them once, at the end of the run).
    """

    def __init__(
        self, emit: Callable[[BaseModel], int], limits: TrackingLimits, *, max_event_bytes: int
    ) -> None:
        self._emit = emit
        self.limits = limits
        self.max_event_bytes = max_event_bytes
        self.received = 0
        self.budget = TrackingBudget(limits)

    def feed(self, msg: TrackingMessage, nbytes: int) -> None:
        """Account one tracking frame of ``nbytes`` and emit what fits the budgets."""
        self.received += nbytes
        if self.received > self.limits.tracking_bytes:
            raise TrackingFlood(
                f"the worker sent more than {self.limits.tracking_bytes} bytes of tracking "
                "data (tracking flood)"
            )
        if isinstance(msg, UserLogMessage):
            self._user_log(msg.payload)
            return
        if isinstance(msg, MetricsMessage):
            self._split(self._metrics(clean_payload(msg.payload)))
        elif isinstance(msg, ParamsMessage):
            self._split(self._params(clean_payload(msg.payload)))
        else:
            self._split(self._tags(clean_payload(msg.payload)))

    def _split[P: (MetricBatchPayload, ParamsPayload, TagsPayload)](
        self, payload: P | None
    ) -> None:
        if payload is None:
            return
        try:
            parts = split_for_events(payload, self.max_event_bytes)
        except ValueError:  # one item cannot fit in an event at all
            self.budget.dropped["items too large for one event"] += 1
            return
        for part in parts:
            self._emit(part)

    def _metrics(self, p: MetricBatchPayload) -> MetricBatchPayload | None:
        kept = []
        for s in p.series:
            lkey = labels_key(s.labels)
            idx = [i for i in range(len(s.steps)) if self.budget.point(s.key, lkey, s.steps[i])]
            if len(idx) == len(s.steps):
                kept.append(s)
            elif idx:
                kept.append(
                    s.model_copy(
                        update={
                            "steps": [s.steps[i] for i in idx],
                            "values": [s.values[i] for i in idx],
                            "ts_ms": [s.ts_ms[i] for i in idx],
                        }
                    )
                )
        return p.model_copy(update={"series": kept}) if kept else None

    def _params(self, p: ParamsPayload) -> ParamsPayload | None:
        kept = [i for i in p.params if self.budget.param(i.key, i.value)]
        return p.model_copy(update={"params": kept}) if kept else None

    def _tags(self, p: TagsPayload) -> TagsPayload | None:
        kept = [t for t in p.tags if self.budget.tag(t.key)]
        return p.model_copy(update={"tags": kept}) if kept else None

    def _user_log(self, payload: LogPayload) -> None:
        if payload.stream != "user":
            raise ProtocolError("a user_log message must use the stream 'user'")
        clean = clean_payload(payload)
        if self.budget.log(log_size(clean)):
            self._emit(clean)

    def warning(self) -> WarningPayload | None:
        """The ``TRACKING_BUDGET`` warning when the gate dropped anything (else ``None``)."""
        return self.budget.warning()
