"""Rendering run events (local executor ``RunEvent``s and API ``RunEventView``s) on stderr.

Payloads are read defensively: the typed M1 runner payloads (``mapi_spec.run``) and the
in-process executor's simpler ones (``{"fraction", "message"}``) are both understood.
Every payload string is untrusted and passes through :func:`~mapi_cli.output.safe`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import Any, Protocol

from mapi_spec.executor import RunEventType
from rich.progress import BarColumn, Progress, TaskID, TextColumn, TimeElapsedColumn
from rich.text import Text

from mapi_cli.output import Output, safe

__all__ = ["EventLike", "EventRenderer"]


class EventLike(Protocol):
    @property
    def type(self) -> RunEventType: ...
    @property
    def payload(self) -> Mapping[str, Any]: ...


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class EventRenderer:
    """A progress bar plus log/warning lines; a no-op in ``--json`` mode."""

    def __init__(self, output: Output, title: str, *, show_logs: bool = True) -> None:
        self.output = output
        self.show_logs = show_logs
        self.enabled = not output.json_mode
        self._progress = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            TextColumn("{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            TextColumn("{task.fields[message]}"),
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
            line = p.get("line", p.get("message", ""))
            self._line(safe(line, "dim"))
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
