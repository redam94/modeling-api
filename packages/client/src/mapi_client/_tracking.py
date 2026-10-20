"""Buffered run tracking from notebooks and scripts: ``client.runs.track(run_id)``.

A :class:`RunTracker` collects metric points, params and tags and sends them in batches
(``POST /v1/runs/{run_id}/metrics`` of at most 1000 points, ``.../params`` of at most 500,
one ``PATCH`` for the tags) when ``flush_every`` points wait and when the ``with`` block
ends. Keys, labels and values are validated when they are logged, so a bad key fails at
the call site, not at the flush. Images and tables (v0.14) are sent at once, one request
each. :class:`AsyncRunTracker` is the ``async with`` twin.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mapi_spec.api import MetricPointIn, RunMedia
from mapi_spec.tracking import (
    MAX_TAG_VALUE_CHARS,
    ParamValue,
    check_param_key,
    check_tag_key,
    param_text,
)

from mapi_client._calls_tracking import metric_points

if TYPE_CHECKING:
    from mapi_client._async import AsyncRuns
    from mapi_client._sync import Runs

__all__ = ["AsyncRunTracker", "RunTracker"]


class _Buffer:
    """What waits to be sent; thread-safe. Params are write-once per tracker."""

    def __init__(self, flush_every: int) -> None:
        if flush_every < 1:
            raise ValueError("flush_every must be >= 1")
        self.flush_every = flush_every
        self._lock = threading.Lock()
        self._points: list[MetricPointIn] = []
        self._params: dict[str, ParamValue] = {}
        self._tags: dict[str, str | None] = {}
        self._seen: dict[str, ParamValue] = {}

    def add_points(
        self,
        values: Mapping[str, float],
        step: int | None,
        labels: Mapping[str, str] | None,
        ts: datetime | None,
    ) -> bool:
        """Buffer the points; ``True`` when ``flush_every`` points wait."""
        if step is not None and step < 0:
            raise ValueError(f"metric step must be >= 0, got {step}")
        points = metric_points(values, step=step, labels=labels, ts=ts or datetime.now(UTC))
        with self._lock:
            self._points.extend(points)
            return len(self._points) >= self.flush_every

    def add_params(self, params: Mapping[str, ParamValue]) -> None:
        clean = {check_param_key(k): v for k, v in params.items()}
        with self._lock:
            for key, value in clean.items():
                old = self._seen.get(key)
                if old is not None and param_text(old) != param_text(value):
                    raise ValueError(
                        f"param {key!r} is already {old!r}; params are write-once "
                        f"(cannot change it to {value!r})"
                    )
            for key, value in clean.items():
                if key not in self._seen:
                    self._seen[key] = value
                    self._params[key] = value

    def add_tags(self, tags: Mapping[str, str | None]) -> None:
        clean: dict[str, str | None] = {}
        for k, v in tags.items():
            if v is not None and len(v) > MAX_TAG_VALUE_CHARS:
                raise ValueError(f"tag {k!r}: the value has more than {MAX_TAG_VALUE_CHARS} chars")
            clean[check_tag_key(k)] = v
        with self._lock:
            self._tags.update(clean)

    def take(self) -> tuple[list[MetricPointIn], dict[str, ParamValue], dict[str, str | None]]:
        with self._lock:
            out = (self._points, self._params, self._tags)
            self._points, self._params, self._tags = [], {}, {}
            return out


class RunTracker:
    """Buffered metrics, params and tags of one run (see :meth:`Runs.track`)."""

    def __init__(self, runs: Runs, run_id: str, *, flush_every: int = 500) -> None:
        self.run_id = run_id
        self._runs = runs
        self._buf = _Buffer(flush_every)

    def __enter__(self) -> RunTracker:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.flush()

    def log_metric(
        self,
        key: str,
        value: float,
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        ts: datetime | None = None,
    ) -> None:
        """Buffer one point (``step=None``: a scalar, the newest value wins)."""
        self.log_metrics({key: value}, step=step, labels=labels, ts=ts)

    def log_metrics(
        self,
        values: Mapping[str, float],
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        ts: datetime | None = None,
    ) -> None:
        """Buffer points sharing one step, label set and time (non-finite values dropped)."""
        if self._buf.add_points(values, step, labels, ts):
            self.flush()

    def log_param(self, key: str, value: ParamValue) -> None:
        self.log_params({key: value})

    def log_params(self, params: Mapping[str, ParamValue]) -> None:
        """Buffer params (write-once: a different value for a logged key raises)."""
        self._buf.add_params(params)

    def set_tag(self, key: str, value: str | None) -> None:
        self.set_tags({key: value})

    def set_tags(self, tags: Mapping[str, str | None]) -> None:
        """Buffer tags (the last value wins; ``None`` deletes the tag)."""
        self._buf.add_tags(tags)

    def log_image(
        self,
        key: str,
        image: Any,
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        caption: str | None = None,
    ) -> RunMedia:
        """Send one image now (see :meth:`Runs.log_image`)."""
        return self._runs.log_image(
            self.run_id, key, image, step=step, labels=labels, caption=caption
        )

    def log_table(
        self,
        key: str,
        table: Any,
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        caption: str | None = None,
    ) -> RunMedia:
        """Send one table now (see :meth:`Runs.log_table`)."""
        return self._runs.log_table(
            self.run_id, key, table, step=step, labels=labels, caption=caption
        )

    def flush(self) -> None:
        """Send everything buffered (metrics, then params, then tags)."""
        points, params, tags = self._buf.take()
        if points:
            self._runs.log_metrics(self.run_id, points)
        if params:
            self._runs.log_params(self.run_id, params)
        if tags:
            self._runs.set_tags(self.run_id, tags)


class AsyncRunTracker:
    """The ``async with`` twin of :class:`RunTracker` (see :meth:`AsyncRuns.track`)."""

    def __init__(self, runs: AsyncRuns, run_id: str, *, flush_every: int = 500) -> None:
        self.run_id = run_id
        self._runs = runs
        self._buf = _Buffer(flush_every)

    async def __aenter__(self) -> AsyncRunTracker:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.flush()

    async def log_metric(
        self,
        key: str,
        value: float,
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        ts: datetime | None = None,
    ) -> None:
        await self.log_metrics({key: value}, step=step, labels=labels, ts=ts)

    async def log_metrics(
        self,
        values: Mapping[str, float],
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        ts: datetime | None = None,
    ) -> None:
        if self._buf.add_points(values, step, labels, ts):
            await self.flush()

    def log_param(self, key: str, value: ParamValue) -> None:
        self.log_params({key: value})

    def log_params(self, params: Mapping[str, ParamValue]) -> None:
        self._buf.add_params(params)

    def set_tag(self, key: str, value: str | None) -> None:
        self.set_tags({key: value})

    def set_tags(self, tags: Mapping[str, str | None]) -> None:
        self._buf.add_tags(tags)

    async def log_image(
        self,
        key: str,
        image: Any,
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        caption: str | None = None,
    ) -> RunMedia:
        return await self._runs.log_image(
            self.run_id, key, image, step=step, labels=labels, caption=caption
        )

    async def log_table(
        self,
        key: str,
        table: Any,
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        caption: str | None = None,
    ) -> RunMedia:
        return await self._runs.log_table(
            self.run_id, key, table, step=step, labels=labels, caption=caption
        )

    async def flush(self) -> None:
        points, params, tags = self._buf.take()
        if points:
            await self._runs.log_metrics(self.run_id, points)
        if params:
            await self._runs.log_params(self.run_id, params)
        if tags:
            await self._runs.set_tags(self.run_id, tags)
