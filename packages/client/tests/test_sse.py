"""The SSE decoder follows the WHATWG event-stream parsing rules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mapi_client import RunEnd, RunEventFrame, RunSnapshot, UnexpectedResponse
from mapi_client.sse import SseDecoder, SseEvent, to_stream_item
from mapi_spec.api import Run, RunEventView, SseEventName, encode_sse


def feed(lines: list[str]) -> tuple[list[SseEvent], SseDecoder]:
    dec = SseDecoder()
    out = [e for line in lines if (e := dec.feed(line)) is not None]
    return out, dec


def test_basic_fields_comments_and_retry() -> None:
    events, dec = feed(
        ["retry: 3000", ": keepalive", "id: 7", "event: state", 'data: {"a":1}', "", ""]
    )
    assert events == [SseEvent(event="state", data='{"a":1}', id="7", retry=3000)]
    assert dec.last_event_id == "7" and dec.retry == 3000


def test_multiline_data_and_default_event_name() -> None:
    events, _ = feed(["data: one", "data:two", "data", ""])
    assert events == [SseEvent(event="message", data="one\ntwo\n")]


def test_id_persists_and_nul_ids_are_ignored() -> None:
    events, dec = feed(["id: 3", "data: a", "", "data: b", "", "id: 4\x00", "data: c", ""])
    assert [e.id for e in events] == ["3", None, None]
    assert dec.last_event_id == "3"


def test_unknown_fields_and_bad_retry_are_ignored() -> None:
    events, dec = feed(["foo: bar", "retry: soon", "data: x", ""])
    assert events == [SseEvent(event="message", data="x")]
    assert dec.retry is None


def test_oversized_events_are_rejected() -> None:
    dec = SseDecoder()
    with pytest.raises(UnexpectedResponse):
        for _ in range(2000):
            dec.feed("data: " + "x" * 1024)


@given(st.lists(st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=40)))
def test_encode_sse_roundtrips_through_the_decoder(values: list[str]) -> None:
    frames = [encode_sse("log", {"v": v}, id=i + 1) for i, v in enumerate(values)]
    lines = "".join(frames).split("\n")
    events, _ = feed(lines)
    assert [e.id for e in events] == [str(i + 1) for i in range(len(values))]
    assert all(e.event == "log" for e in events)


def test_typed_stream_items(example: Callable[..., dict[str, Any]]) -> None:
    run = example(Run)
    view = example(RunEventView)
    snap = to_stream_item(SseEvent("snapshot", Run.model_validate(run).model_dump_json()))
    assert isinstance(snap, RunSnapshot) and snap.run.id == run["id"]
    ev = to_stream_item(SseEvent("state", RunEventView.model_validate(view).model_dump_json()))
    assert isinstance(ev, RunEventFrame) and ev.id == 17 and ev.name is SseEventName.STATE
    end = to_stream_item(SseEvent("end", Run.model_validate(run).model_dump_json()))
    assert isinstance(end, RunEnd)
    assert to_stream_item(SseEvent("something-new", "{}")) is None
    with pytest.raises(UnexpectedResponse):
        to_stream_item(SseEvent("state", "{}"))
