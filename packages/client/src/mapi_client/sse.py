"""Server-sent events: a WHATWG-conformant line decoder and typed run-stream items.

``GET /v1/runs/{run_id}/events`` (``Accept: text/event-stream``) sends ``snapshot`` (the
full :class:`~mapi_spec.api.Run`, no id) first on every connection, then the persisted run
events (``id:`` = :attr:`RunEventView.id`) after ``Last-Event-ID``, then live events, and
finally ``end`` (the final run) before closing. ``: keepalive`` comments are heartbeats.
``progress`` and ``log`` frames are live and unpersisted: they carry no ``id:`` and are
never replayed after a reconnect (:class:`RunLiveFrame`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from mapi_spec.api import Id, Run, RunEventView, SseEventName
from mapi_spec.executor import RunEventType
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError

from mapi_client.errors import UnexpectedResponse

__all__ = [
    "RunEnd",
    "RunEventFrame",
    "RunLiveFrame",
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


class LiveEventView(BaseModel):
    """Data of a live ``progress``/``log`` frame: a run event that is never persisted."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    run_id: Id
    attempt: int = Field(..., ge=1)
    seq: int = Field(..., ge=0)
    ts: AwareDatetime
    type: RunEventType
    payload: dict[str, JsonValue] = Field(default_factory=dict)


#: Frames the server sends live, without an ``id:`` (not persisted, never replayed).
LIVE_FRAMES: Final = frozenset({SseEventName.PROGRESS, SseEventName.LOG})


@dataclass(frozen=True, slots=True)
class RunLiveFrame:
    """A live ``progress`` or ``log`` frame. It has no event id, so it does not move the
    ``Last-Event-ID`` cursor; after a reconnect the ``snapshot`` carries the latest progress."""

    name: SseEventName
    event: LiveEventView

    @property
    def type(self) -> RunEventType:
        return self.event.type

    @property
    def payload(self) -> Mapping[str, JsonValue]:
        return self.event.payload


@dataclass(frozen=True, slots=True)
class RunEnd:
    """The run is terminal; the server closes the stream after this frame."""

    run: Run


RunStreamItem = RunSnapshot | RunEventFrame | RunLiveFrame | RunEnd


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
        if name in LIVE_FRAMES and frame.id is None:
            return RunLiveFrame(name, LiveEventView.model_validate_json(frame.data))
        return RunEventFrame(name, RunEventView.model_validate_json(frame.data))
    except ValidationError as exc:
        raise UnexpectedResponse(
            f"invalid {name.value!r} frame on the run event stream ({exc.error_count()} errors)",
            untrusted_text=frame.data[:2000],
        ) from None
