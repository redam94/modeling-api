"""The worker's run tracker: buffers metric points, params and tags and sends them in batches.

:class:`WorkerTracker` implements :class:`mapi_sdk.tracking.Tracker` for a sandboxed run.
Metric points are buffered per series and flushed by a daemon thread every
``TrackingLimits.flush_interval_s`` (and inline once 1000 points are buffered) as
``metrics`` messages of at most ``MAX_POINTS_PER_EVENT`` points; params and tags go out with
the same flush; user log lines are sent at once. The worker keeps the run's tracking budgets
itself (:class:`~mapi_runner.events.TrackingBudget`, the same one the supervisor applies), so
a well-behaved run drops and counts excess points here and :meth:`WorkerTracker.close` reports
them in one ``TRACKING_BUDGET`` warning.
"""

from __future__ import annotations

import contextlib
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mapi_spec.run import (
    ChainProgress,
    LogPayload,
    MetricBatchPayload,
    MetricSeriesBatch,
    ParamItem,
    ParamsPayload,
    TagItem,
    TagsPayload,
    split_for_events,
)
from mapi_spec.tracking import (
    MAX_PARAMS_PER_EVENT,
    MAX_POINTS_PER_EVENT,
    MAX_TAGS_PER_RUN,
    LogFieldValue,
    LogLevel,
    ParamValue,
    TrackingLimits,
    check_labels,
    check_metric_key,
    labels_key,
)
from pydantic import BaseModel

from mapi_runner.channel import (
    MAX_WORKER_FRAME,
    MetricsMessage,
    ParamsMessage,
    TagsMessage,
    UserLogMessage,
)
from mapi_runner.events import TrackingBudget, log_size

if TYPE_CHECKING:
    from mapi_runner.worker.context import WorkerContext

__all__ = ["WorkerTracker"]

_MAX_SERIES_PER_EVENT = 64  # MetricBatchPayload.series max_length
_LINE_MAX_CHARS = 4096  # LogPayload.line max_length


@dataclass(slots=True)
class _Series:
    key: str
    labels: dict[str, str]
    steps: list[int | None] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    ts: list[float] = field(default_factory=list)


class WorkerTracker:
    """Thread-safe :class:`~mapi_sdk.tracking.Tracker` that batches into channel messages."""

    def __init__(
        self,
        ctx: WorkerContext,
        limits: TrackingLimits,
        *,
        clock: Callable[[], float] = time.time,
        send: Callable[[BaseModel], None] | None = None,
    ) -> None:
        self._ctx = ctx
        self._send_msg = send or ctx.send
        self.limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._budget = TrackingBudget(limits)
        self._series: dict[tuple[str, str], _Series] = {}
        self._buffered = 0
        self._params: dict[str, ParamItem] = {}
        self._tags: dict[str, TagItem] = {}
        self._checked: set[tuple[str, str]] = set()
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------- Tracker
    def log_metrics(
        self,
        values: Mapping[str, float],
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if not values:
            return
        if step is not None:
            step = int(step)
            if step < 0:
                raise ValueError(f"metric step must be >= 0, got {step}")
        lbl = check_labels(labels)
        lkey = labels_key(lbl)
        now = self._clock()
        with self._lock:
            if self._closed:
                return
            for key, raw in values.items():
                if (key, lkey) not in self._checked:
                    check_metric_key(key)
                    self._checked.add((key, lkey))
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(value):
                    continue  # non-finite values are dropped, as in the SDK
                if not self._budget.point(key, lkey, step):
                    continue
                s = self._series.get((key, lkey))
                if s is None:
                    s = self._series[(key, lkey)] = _Series(key, lbl)
                s.steps.append(step)
                s.values.append(value)
                s.ts.append(now)
                self._buffered += 1
            full = self._buffered >= MAX_POINTS_PER_EVENT
        self._ensure_thread()
        if full:
            self.flush()

    def log_params(self, params: Mapping[str, ParamValue]) -> None:
        items = [ParamItem(key=k, value=v) for k, v in params.items()]  # validates here
        with self._lock:
            if self._closed:
                return
            for item in items:
                if self._budget.param(item.key, item.value):
                    self._params[item.key] = item
        self._ensure_thread()

    def set_tags(self, tags: Mapping[str, str]) -> None:
        items = [TagItem(key=k, value=v) for k, v in tags.items()]
        with self._lock:
            if self._closed:
                return
            for item in items:
                if self._budget.tag(item.key):
                    self._tags[item.key] = item
        self._ensure_thread()

    def log_line(
        self,
        message: str,
        *,
        level: LogLevel = "info",
        fields: Mapping[str, LogFieldValue] | None = None,
    ) -> None:
        text = str(message)
        payload = LogPayload(
            stream="user",
            line=text[:_LINE_MAX_CHARS],
            truncated=len(text) > _LINE_MAX_CHARS,
            level=level,
            fields=dict(fields) if fields else None,
        )
        with self._lock:
            if self._closed or not self._budget.log(log_size(payload)):
                return
        self._send_msg(UserLogMessage(payload=payload))

    def chains(self, chains: Sequence[ChainProgress]) -> None:
        self._ctx.set_chains(chains)

    def flush(self) -> None:
        """Send every buffered point, param and tag (in order; one flush at a time)."""
        with self._flush_lock:
            with self._lock:
                series, self._series, self._buffered = self._series, {}, 0
                params, self._params = self._params, {}
                tags, self._tags = self._tags, {}
            for batch in _metric_batches(list(series.values())):
                for part in split_for_events(batch, MAX_WORKER_FRAME):
                    self._send_msg(MetricsMessage(payload=part))
            items = list(params.values())
            for i in range(0, len(items), MAX_PARAMS_PER_EVENT):
                chunk = ParamsPayload(params=items[i : i + MAX_PARAMS_PER_EVENT])
                for pp in split_for_events(chunk, MAX_WORKER_FRAME):
                    self._send_msg(ParamsMessage(payload=pp))
            titems = list(tags.values())
            for i in range(0, len(titems), MAX_TAGS_PER_RUN):
                tchunk = TagsPayload(tags=titems[i : i + MAX_TAGS_PER_RUN])
                for tp in split_for_events(tchunk, MAX_WORKER_FRAME):
                    self._send_msg(TagsMessage(payload=tp))

    # ------------------------------------------------------------------ lifecycle
    @property
    def dropped(self) -> dict[str, int]:
        with self._lock:
            return dict(self._budget.dropped)

    def close(self) -> None:
        """Final flush; stops the flush thread; one ``TRACKING_BUDGET`` warning when points
        were dropped. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, 2 * self.limits.flush_interval_s))
        self.flush()
        warning = self._budget.warning()
        if warning is not None:
            self._ctx.warning(warning.code, warning.message)

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        with self._lock:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(target=self._run, name="mapi-tracking", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.limits.flush_interval_s):
            try:
                self.flush()
            except OSError:
                return  # the channel is gone: the run is ending
            except Exception as exc:  # never kill the run from the background thread
                with contextlib.suppress(Exception):
                    self._ctx.log(f"tracking: flush failed: {type(exc).__name__}: {exc}")


def _metric_batches(series: list[_Series]) -> list[MetricBatchPayload]:
    """Columnar payloads of at most ``MAX_POINTS_PER_EVENT`` points and 64 series each."""
    out: list[MetricBatchPayload] = []
    cur: list[tuple[_Series, int, int]] = []
    n = 0

    def emit() -> None:
        nonlocal cur, n
        if not cur:
            return
        t0 = min(min(s.ts[a:b]) for s, a, b in cur)
        out.append(
            MetricBatchPayload(
                t0=datetime.fromtimestamp(t0, UTC),
                series=[
                    MetricSeriesBatch(
                        key=s.key,
                        labels=s.labels,
                        steps=s.steps[a:b],
                        values=s.values[a:b],
                        ts_ms=[max(0, int((t - t0) * 1000)) for t in s.ts[a:b]],
                    )
                    for s, a, b in cur
                ],
            )
        )
        cur, n = [], 0

    for s in series:
        a = 0
        while a < len(s.steps):
            if n >= MAX_POINTS_PER_EVENT or len(cur) >= _MAX_SERIES_PER_EVENT:
                emit()
            b = min(len(s.steps), a + MAX_POINTS_PER_EVENT - n)
            cur.append((s, a, b))
            n += b - a
            a = b
    emit()
    return out
