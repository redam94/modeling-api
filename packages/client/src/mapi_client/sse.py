"""Server-sent events: a WHATWG-conformant line decoder and typed run-stream items.

``GET /v1/runs/{run_id}/events`` (``Accept: text/event-stream``) sends ``snapshot`` (the
full :class:`~mapi_spec.api.Run`, no id) first on every connection, then the persisted run
events (``id:`` = :attr:`RunEventView.id`) after ``Last-Event-ID``, then live events, and
finally ``end`` (the final run) before closing. ``: keepalive`` comments are heartbeats.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from mapi_spec.api import Run, RunEventView, SseEventName
from pydantic import ValidationError

from mapi_client.errors import UnexpectedResponse

__all__ = [
    "RunEnd",
    "RunEventFrame",
    "RunSnapshot",
    "RunStreamItem",
    "SseDecoder",
    "SseEvent",
    "to_stream_item",
]

MAX_EVENT_DATA_BYTES: Final = 1024 * 1024
"""Upper bound on one event's accumulated ``data`` (protects against a runaway stream)."""


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One dispatched SSE event (``id`` is the stream's last-event-id when it was sent)."""

    event: str
    data: str
    id: str | None = None
    retry: int | None = None


class SseDecoder:
    """Incremental decoder fed one line at a time (line terminators already stripped)."""

    def __init__(self) -> None:
        self._event = ""
        self._data: list[str] = []
        self._size = 0
        self._id: str | None = None
        self._has_id = False
        self.last_event_id: str | None = None
        self.retry: int | None = None

    def feed(self, line: str) -> SseEvent | None:
        """Process one line; returns an event when a blank line dispatches one."""
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None  # comment / heartbeat
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value
        elif name == "data":
            self._size += len(value) + 1
            if self._size > MAX_EVENT_DATA_BYTES:
                raise UnexpectedResponse("an SSE event exceeded the client's size limit")
            self._data.append(value)
        elif name == "id":
            if "\x00" not in value:
                self._id = value
                self._has_id = True
        elif name == "retry" and value.isdigit():
            self.retry = int(value)
        # unknown fields are ignored (WHATWG)
        return None

    def _dispatch(self) -> SseEvent | None:
        if self._has_id:
            self.last_event_id = self._id or None
        if not self._data:
            self._event = ""
            self._has_id = False
            return None
        event = SseEvent(
            event=self._event or "message",
            data="\n".join(self._data),
            id=self._id if self._has_id else None,
            retry=self.retry,
        )
        self._event, self._data, self._size = "", [], 0
        self._has_id = False
        return event


# ---------------------------------------------------------------------------- typed items


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """First frame of every connection: the run as it is now (incl. its progress)."""

    run: Run


@dataclass(frozen=True, slots=True)
class RunEventFrame:
    """A run event (``state``, ``progress``, ``log``, ``diagnostic``, ``warning``,
    ``checkpoint``, ``metric``, ``complete`` or ``error``)."""

    name: SseEventName
    event: RunEventView

    @property
    def id(self) -> int:
        return self.event.id


@dataclass(frozen=True, slots=True)
class RunEnd:
    """The run is terminal; the server closes the stream after this frame."""

    run: Run


RunStreamItem = RunSnapshot | RunEventFrame | RunEnd


def to_stream_item(frame: SseEvent) -> RunStreamItem | None:
    """Decode a frame of the run event stream (unknown event names are skipped)."""
    try:
        name = SseEventName(frame.event)
    except ValueError:
        return None
    try:
        if name is SseEventName.SNAPSHOT:
            return RunSnapshot(Run.model_validate_json(frame.data))
        if name is SseEventName.END:
            return RunEnd(Run.model_validate_json(frame.data))
        return RunEventFrame(name, RunEventView.model_validate_json(frame.data))
    except ValidationError as exc:
        raise UnexpectedResponse(
            f"invalid {name.value!r} frame on the run event stream ({exc.error_count()} errors)",
            untrusted_text=frame.data[:2000],
        ) from None
