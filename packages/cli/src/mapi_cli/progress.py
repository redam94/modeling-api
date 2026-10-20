"""Rendering run events (local executor ``RunEvent``s and API ``RunEventView``s) on stderr.

Payloads are read defensively: the typed M1 runner payloads (``mapi_spec.run``) and the
in-process executor's simpler ones (``{"fraction", "message"}``) are both understood.
Every payload string is untrusted and passes through :func:`~mapi_cli.output.safe`.

Run tracking (v0.11): ``metrics`` events and the SSE ``tracking`` frames update the newest
value per series, shown as one ``metrics ...`` line at most every :data:`METRICS_EVERY_S`
seconds (and once more at the end); ``params``/``tags`` events (and, for a remote run, the
params and tags that changed after a ``tracking`` frame) print one line each; user log lines
(stream ``user``) print as ``[level] line``.

Media (v0.14): a ``media`` event prints ``media image samples step 3 (12 KB)``, at most one
line every :data:`METRICS_EVERY_S` seconds (the newest item is shown; once more at the end);
a ``tracking`` frame with ``media`` set prints that the run logged new images or tables.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Final, Protocol

from mapi_spec.api import TrackingFrame
from mapi_spec.executor import RunEventType
from rich.progress import BarColumn, Progress, TaskID, TextColumn, TimeElapsedColumn
from rich.text import Text

from mapi_cli.output import Output, safe

__all__ = ["METRICS_EVERY_S", "EventLike", "EventRenderer"]

METRICS_EVERY_S: Final = 5.0
_METRICS_SHOWN: Final = 4
_LINE_CHARS: Final = 200
_LEVEL_STYLE: Final = {"warning": "yellow", "error": "red", "debug": "dim"}


class EventLike(Protocol):
    @property
    def type(self) -> RunEventType: ...
    @property
    def payload(self) -> Mapping[str, Any]: ...


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _series_name(key: Any, labels: Any) -> str:
    if isinstance(labels, Mapping) and labels:
        inner = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{key}{{{inner}}}"
    return str(key)


def _value(v: Any) -> str:
    return f"{v:.4g}" if isinstance(v, float) else str(v)


def _size(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _pairs(items: Any, value_key: str = "value") -> str:
    """``k=v, ...`` of ``[{"key", "value"}]`` items, cut to 200 characters."""
    parts = [
        f"{i.get('key')}={_value(i.get(value_key))}" for i in items or [] if isinstance(i, Mapping)
    ]
    text = ", ".join(parts)
    return text if len(text) <= _LINE_CHARS else text[: _LINE_CHARS - 3] + "..."


class EventRenderer:
    """A progress bar plus log/warning lines; a no-op in ``--json`` mode."""

    def __init__(
        self,
        output: Output,
        title: str,
        *,
        show_logs: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.output = output
        self.show_logs = show_logs
        self.enabled = not output.json_mode
        self._clock = clock
        # series name -> (value, step); the newest update is last
        self._latest: dict[str, tuple[float, int | None]] = {}
        self._metrics_at: float | None = None
        self._metrics_dirty = False
        self._media_at: float | None = None
        self._media_text: Text | None = None  # the newest media line not printed yet
        self._progress = Progress(
            # Titles and messages are shown verbatim: markup=False, so a "[link=...]" or a
            # stray "[/]" in a worker-supplied message is text, never rich markup.
            TextColumn("{task.description}", style="bold", markup=False),
            BarColumn(),
            TextColumn("{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            TextColumn("{task.fields[message]}", markup=False),
            console=output.err,
            transient=False,
        )
        self._task: TaskID | None = None
        self._title = title

    def __enter__(self) -> EventRenderer:
        if self.enabled:
            self._progress.start()
            self._task = self._progress.add_task(self._title, total=1.0, message="")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.enabled:
            if self._metrics_dirty:
                self._metrics_line()
            if self._media_text is not None:
                self._media_line()
            self._progress.stop()

    def _set(self, *, fraction: float | None = None, message: str | None = None) -> None:
        if self._task is None:
            return
        fields: dict[str, Any] = {}
        if message is not None:
            fields["message"] = safe(message[:120]).plain
        if fraction is not None:
            self._progress.update(self._task, completed=max(0.0, min(1.0, fraction)), **fields)
        elif fields:
            self._progress.update(self._task, **fields)

    def _line(self, text: Text) -> None:
        self._progress.console.print(text)

    # ---- tracking

    def _update_metric(self, name: str, value: float, step: int | None) -> None:
        self._latest.pop(name, None)
        self._latest[name] = (value, step)
        self._metrics_dirty = True

    def _maybe_metrics_line(self) -> None:
        now = self._clock()
        if self._metrics_at is None or now - self._metrics_at >= METRICS_EVERY_S:
            self._metrics_line()

    def _metrics_line(self) -> None:
        self._metrics_at = self._clock()
        self._metrics_dirty = False
        newest = list(self._latest.items())[-_METRICS_SHOWN:][::-1]
        parts = [
            f"{name}={value:.4g}" + (f" (step {step})" if step is not None else "")
            for name, (value, step) in newest
        ]
        if parts:
            self._line(Text("metrics ", style="cyan") + safe(", ".join(parts)[:400]))

    def _metrics_event(self, p: Mapping[str, Any]) -> None:
        for s in p.get("series") or []:
            if not isinstance(s, Mapping):
                continue
            values, steps = s.get("values") or [], s.get("steps") or []
            if not values:
                continue
            value = _num(values[-1])
            if value is None:
                continue
            step = steps[-1] if steps and isinstance(steps[-1], int) else None
            self._update_metric(_series_name(s.get("key"), s.get("labels")), value, step)
        self._maybe_metrics_line()

    def tracking(self, frame: TrackingFrame) -> None:
        """An SSE ``tracking`` frame: the newest values of the series that changed."""
        if not self.enabled:
            return
        for s in frame.series:
            self._update_metric(_series_name(s.key, s.labels), s.last, s.last_step)
        if frame.series:
            self._maybe_metrics_line()
        if frame.media:
            self._media_update(Text("media ", style="cyan") + Text("new images or tables"))

    # ---- media (v0.14)

    def _media_update(self, text: Text) -> None:
        self._media_text = text
        now = self._clock()
        if self._media_at is None or now - self._media_at >= METRICS_EVERY_S:
            self._media_line()

    def _media_line(self) -> None:
        self._media_at = self._clock()
        if self._media_text is not None:
            self._line(self._media_text)
        self._media_text = None

    def _media_event(self, p: Mapping[str, Any]) -> None:
        name = _series_name(p.get("key"), p.get("labels"))
        parts = [str(p.get("kind") or "item"), name]
        step = p.get("step")
        if isinstance(step, int) and not isinstance(step, bool):
            parts.append(f"step {step}")
        size = _num(p.get("size"))
        text = " ".join(parts) + (f" ({_size(size)})" if size is not None else "")
        self._media_update(Text("media ", style="cyan") + safe(text[:_LINE_CHARS]))

    def params(self, params: Mapping[str, Any]) -> None:
        """One ``params:`` line (a remote watch reads them after a ``tracking`` frame)."""
        if self.enabled and params:
            items = [{"key": k, "value": v} for k, v in params.items()]
            self._line(Text("params: ", style="cyan") + safe(_pairs(items)))

    def tags(self, tags: Mapping[str, str]) -> None:
        """One ``tags:`` line (a remote watch reads them after a ``tracking`` frame)."""
        if self.enabled and tags:
            items = [{"key": k, "value": v} for k, v in tags.items()]
            self._line(Text("tags: ", style="cyan") + safe(_pairs(items)))

    def _user_line(self, p: Mapping[str, Any]) -> None:
        level = str(p.get("level") or "info")
        text = safe(f"[{level}] ", _LEVEL_STYLE.get(level, "")) + safe(
            p.get("line", ""), _LEVEL_STYLE.get(level, "")
        )
        fields = p.get("fields")
        if isinstance(fields, Mapping) and fields:
            text += safe(" " + " ".join(f"{k}={_value(v)}" for k, v in fields.items()), "dim")
        self._line(text)

    def handle(self, event: EventLike) -> None:
        if not self.enabled:
            return
        p = event.payload
        kind = event.type
        if kind is RunEventType.PROGRESS:
            msg = p.get("message") or p.get("phase")
            self._set(fraction=_num(p.get("fraction")), message=str(msg) if msg else None)
        elif kind is RunEventType.STATE:
            label = p.get("phase") or p.get("state")
            if label:
                self._set(message=str(p.get("message") or label))
        elif kind is RunEventType.LOG and self.show_logs:
            if p.get("stream") == "user":
                self._user_line(p)
            else:
                line = p.get("line", p.get("message", ""))
                self._line(safe(line, "dim"))
        elif kind is RunEventType.METRICS:
            self._metrics_event(p)
        elif kind is RunEventType.MEDIA:
            self._media_event(p)
        elif kind is RunEventType.PARAMS:
            self._line(Text("params: ", style="cyan") + safe(_pairs(p.get("params"))))
        elif kind is RunEventType.TAGS:
            self._line(Text("tags: ", style="cyan") + safe(_pairs(p.get("tags"))))
        elif kind is RunEventType.WARNING:
            self._line(
                Text("warning ", style="yellow")
                + safe(p.get("code", ""))
                + Text(": ")
                + safe(p.get("message", ""))
            )
        elif kind is RunEventType.DIAGNOSTIC:
            self._line(
                Text("diagnostic ", style="cyan")
                + safe(p.get("code", ""))
                + Text(": ")
                + safe(p.get("message_expert") or p.get("message_plain") or "")
            )
        elif kind is RunEventType.METRIC:
            self._line(Text("metric ", style="cyan") + safe(f"{p.get('name')}={p.get('value')}"))
        elif kind is RunEventType.COMPLETE:
            self._set(fraction=1.0, message=str(p.get("status") or p.get("state") or "complete"))
        elif kind is RunEventType.ERROR:
            self._line(
                Text("error ", style="bold red")
                + safe(p.get("code", ""))
                + Text(" ")
                + safe(p.get("message", ""))
            )
