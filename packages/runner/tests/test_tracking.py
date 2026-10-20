"""Run tracking in the runner (v0.11): the worker's batching tracker over a real socketpair,
the supervisor's TrackingGate (budgets, flood cap, event splitting, cleaning) and the four
tracking channel messages. No subprocess."""

from __future__ import annotations

import base64
import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mapi_runner.channel import (
    MAX_WORKER_FRAME,
    MetricsMessage,
    ParamsMessage,
    ProgressMessage,
    ProtocolError,
    StartMessage,
    TagsMessage,
    UserLogMessage,
    WarningMessage,
    encode_frame,
    parse_worker_message,
    recv_frame,
)
from mapi_runner.events import EventEmitter, TrackingFlood, TrackingGate
from mapi_runner.worker.context import WorkerContext
from mapi_spec.errors import ErrorCode
from mapi_spec.executor import RunKind
from mapi_spec.run import (
    ChainProgress,
    LogPayload,
    MetricBatchPayload,
    MetricSeriesBatch,
    ParamItem,
    ParamsPayload,
    TagItem,
    TagsPayload,
    decode_event_line,
    new_hmac_key,
    verify_event,
)
from mapi_spec.tracking import TrackingLimits
from pydantic import BaseModel

pytestmark = pytest.mark.unit

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# ================================================================== worker tracker


class _Chan:
    """A WorkerContext on one end of a socketpair; a thread collects what arrives."""

    def __init__(self, tmp: str, limits: TrackingLimits | None = None) -> None:
        self.a, self.b = socket.socketpair()
        start = StartMessage(
            run_id="run_t",
            attempt=1,
            kind=RunKind.FIT,
            tenant_id="t",
            project_id="p",
            spec={},
            scratch=tmp,
            max_output_bytes=1 << 20,
            max_output_objects=10,
            deadline=datetime.now(UTC) + timedelta(hours=1),
            tracking=limits or TrackingLimits(),
        )
        self.ctx = WorkerContext(start, self.a, cancel=None)
        self.msgs: list[Any] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        while (body := recv_frame(self.b, max_bytes=MAX_WORKER_FRAME)) is not None:
            self.msgs.append(parse_worker_message(body))

    def finish(self) -> list[Any]:
        self.ctx.close()
        self.a.shutdown(socket.SHUT_WR)
        self._reader.join(5)
        self.a.close()
        self.b.close()
        return self.msgs

    def of(self, kind: type) -> list[Any]:
        return [m for m in self.msgs if isinstance(m, kind)]


def _points(msgs: list[Any]) -> list[tuple[str, dict[str, str], int | None, float]]:
    return [
        (k, labels, step, v)
        for m in msgs
        if isinstance(m, MetricsMessage)
        for k, labels, step, v, _ in m.payload.points()
    ]


def test_points_go_out_in_batches_of_at_most_1000(tmp_path: Any) -> None:
    ch = _Chan(str(tmp_path), TrackingLimits(flush_interval_s=30))
    tr = ch.ctx.tracker
    for i in range(2500):
        tr.log_metrics({"loss": 1.0 / (i + 1), "acc": 0.5}, step=i, labels={"chain": "0"})
    msgs = ch.finish()
    batches = [m for m in msgs if isinstance(m, MetricsMessage)]
    assert all(sum(len(s.steps) for s in m.payload.series) <= 1000 for m in batches)
    pts = _points(msgs)
    assert len(pts) == 5000
    loss = [p for p in pts if p[0] == "loss"]
    assert [p[2] for p in loss] == list(range(2500))  # order kept across batches
    assert loss[0][1] == {"chain": "0"}
    assert not ch.of(WarningMessage)


def test_the_flush_thread_sends_without_an_explicit_flush(tmp_path: Any) -> None:
    ch = _Chan(str(tmp_path), TrackingLimits(flush_interval_s=0.1))
    ch.ctx.tracker.log_metrics({"loss": 2.0}, step=1)
    ch.ctx.tracker.log_params({"model": "m@1"})
    ch.ctx.tracker.set_tags({"team": "a"})
    deadline = time.monotonic() + 5
    while not (ch.of(MetricsMessage) and ch.of(ParamsMessage)) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _points(ch.msgs) == [("loss", {}, 1, 2.0)]
    assert ch.of(TagsMessage)[0].payload.tags == [TagItem(key="team", value="a")]
    ch.finish()


def test_per_series_cap_drops_and_warns_once(tmp_path: Any) -> None:
    ch = _Chan(str(tmp_path), TrackingLimits(metric_points_per_series=5, flush_interval_s=30))
    tr = ch.ctx.tracker
    for i in range(12):
        tr.log_metrics({"loss": float(i), "bad": float("nan")}, step=i, labels={"fold": "1"})
    assert tr.dropped == {"metric loss{fold=1}: points over the limit of 5": 7}
    ch.ctx.close()  # idempotent: the second close sends nothing more
    msgs = ch.finish()
    assert [p[2] for p in _points(msgs)] == [0, 1, 2, 3, 4]
    (warning,) = [m for m in msgs if isinstance(m, WarningMessage)]
    assert warning.payload.code == "TRACKING_BUDGET"
    assert warning.payload.message == (
        "The run logged more than its tracking budget allows. Some points were not kept: "
        "metric loss{fold=1}: points over the limit of 5 (7 dropped)"
    )


def test_repeated_scalar_updates_count_once_against_the_budgets(tmp_path: Any) -> None:
    """A scalar (no step) is one stored point (the newest value wins): updating it more
    often than the per-series cap drops nothing and leaves the total budget for steps."""
    limits = TrackingLimits(metric_points_per_series=5, metric_points_total=6, flush_interval_s=30)
    ch = _Chan(str(tmp_path), limits)
    tr = ch.ctx.tracker
    for i in range(20):
        tr.log_metrics({"best_loss": 1.0 / (i + 1)})
    for i in range(5):
        tr.log_metrics({"loss": float(i)}, step=i)
    assert tr.dropped == {}
    msgs = ch.finish()
    best = [p[3] for p in _points(msgs) if p[0] == "best_loss"]
    assert best[-1] == pytest.approx(1.0 / 20)
    assert [p[2] for p in _points(msgs) if p[0] == "loss"] == [0, 1, 2, 3, 4]
    assert not ch.of(WarningMessage)
    gate = TrackingGate(_Sink(), limits, max_event_bytes=65536)
    for m in msgs:
        if isinstance(m, MetricsMessage):
            gate.feed(m, 100)
    assert gate.warning() is None


def test_concurrent_logging_keeps_every_point(tmp_path: Any) -> None:
    ch = _Chan(str(tmp_path), TrackingLimits(flush_interval_s=0.1))
    tr = ch.ctx.tracker

    def work(c: int) -> None:
        for i in range(1000):
            tr.log_metrics({"step_size": 0.1}, step=i + 1, labels={"chain": str(c)})

    threads = [threading.Thread(target=work, args=(c,)) for c in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pts = _points(ch.finish())
    assert len(pts) == 8000
    for c in range(8):
        steps = [p[2] for p in pts if p[1] == {"chain": str(c)}]
        assert steps == list(range(1, 1001))


def test_params_tags_and_user_lines(tmp_path: Any) -> None:
    ch = _Chan(str(tmp_path), TrackingLimits(log_lines=2, flush_interval_s=30))
    tr = ch.ctx.tracker
    tr.log_params({f"settings.p{i}": i for i in range(450)})
    tr.log_params({"settings.p0": "other", "model": "m@1"})  # first value wins
    tr.set_tags({"stage": "a"})
    tr.set_tags({"stage": "b"})  # last value wins
    with pytest.raises(ValueError):
        tr.set_tags({"mapi.kind": "x"})
    with pytest.raises(ValueError):
        tr.log_metrics({"train/loss": 1.0})
    tr.log_line("x" * 5000, level="warning", fields={"epoch": 3})
    tr.log_line("second")
    tr.log_line("third: over the budget")
    msgs = ch.finish()
    params = [i for m in msgs if isinstance(m, ParamsMessage) for i in m.payload.params]
    assert all(len(m.payload.params) <= 200 for m in msgs if isinstance(m, ParamsMessage))
    assert len(params) == 451 and params[0] == ParamItem(key="settings.p0", value=0)
    assert params[-1].key == "model"
    assert [i.value for m in ch.of(TagsMessage) for i in m.payload.tags] == ["b"]
    lines = [m.payload for m in msgs if isinstance(m, UserLogMessage)]
    assert [(p.stream, p.level, p.truncated, len(p.line)) for p in lines] == [
        ("user", "warning", True, 4096),
        ("user", "info", False, 6),
    ]
    assert lines[0].fields == {"epoch": 3}
    (warning,) = ch.of(WarningMessage)
    assert "parameters sent again with a different value (1 dropped)" in warning.payload.message
    assert "log lines over the limit of 2 (1 dropped)" in warning.payload.message


def test_chains_reach_the_next_progress_message(tmp_path: Any) -> None:
    ch = _Chan(str(tmp_path))
    ch.ctx.progress(0.4, "sampling")
    time.sleep(0.3)
    chains = [
        ChainProgress(chain=c, draws_done=10 * c, draws_total=100, step_size=0.3, num_steps=7)
        for c in range(2)
    ]
    ch.ctx.tracker.chains(chains)  # sends a throttled progress with the last fraction
    ch.ctx.progress(0.5, "sampling", force=True)
    msgs = ch.finish()
    prog = [m.payload for m in msgs if isinstance(m, ProgressMessage)]
    assert prog[0].chains == []
    assert prog[1].chains == chains and prog[1].fraction == 0.4
    assert prog[2].chains == chains and prog[2].fraction == 0.5


def test_metric_carries_a_step(tmp_path: Any) -> None:
    ch = _Chan(str(tmp_path))
    ch.ctx.metric("rmse", 1.5, step=3, labels={"split": "test"})
    (m,) = [m for m in ch.finish() if type(m).__name__ == "MetricMessage"]
    assert m.payload.step == 3 and m.payload.labels == {"split": "test"}


# ==================================================================== tracking gate


class _Sink:
    def __init__(self) -> None:
        self.events: list[BaseModel] = []

    def __call__(self, payload: BaseModel) -> int:
        self.events.append(payload)
        return len(self.events) - 1


def _batch(n: int, *, key: str = "loss", labels: dict[str, str] | None = None) -> MetricsMessage:
    return MetricsMessage(
        payload=MetricBatchPayload(
            t0=T0,
            series=[
                MetricSeriesBatch(
                    key=key,
                    labels=labels or {},
                    steps=list(range(n)),
                    values=[float(i) for i in range(n)],
                    ts_ms=[i for i in range(n)],
                )
            ],
        )
    )


def test_gate_mirrors_the_budgets_and_warns() -> None:
    sink = _Sink()
    gate = TrackingGate(
        sink,
        TrackingLimits(metric_points_per_series=8, metric_series=2, params=1, tags=1),
        max_event_bytes=65536,
    )
    gate.feed(_batch(5), 100)
    gate.feed(_batch(5), 100)  # 3 of these 5 are over the per-series cap
    gate.feed(_batch(1, key="b"), 100)
    gate.feed(_batch(1, key="c"), 100)  # a third series
    gate.feed(ParamsMessage(payload=ParamsPayload(params=[ParamItem(key="a", value=1)])), 10)
    gate.feed(ParamsMessage(payload=ParamsPayload(params=[ParamItem(key="b", value=1)])), 10)
    gate.feed(TagsMessage(payload=TagsPayload(tags=[TagItem(key="t", value="1")])), 10)
    gate.feed(TagsMessage(payload=TagsPayload(tags=[TagItem(key="u", value="1")])), 10)
    metrics = [e for e in sink.events if isinstance(e, MetricBatchPayload)]
    assert [len(e.series[0].steps) for e in metrics] == [5, 3, 1]
    assert sum(isinstance(e, ParamsPayload) for e in sink.events) == 1
    assert sum(isinstance(e, TagsPayload) for e in sink.events) == 1
    warning = gate.warning()
    assert warning is not None and warning.code == "TRACKING_BUDGET"
    assert "metric loss: points over the limit of 8 (2 dropped)" in warning.message
    assert "metric series over the limit of 2 (1 dropped)" in warning.message
    assert TrackingGate(sink, TrackingLimits(), max_event_bytes=65536).warning() is None


def test_gate_flood_is_a_sandbox_violation() -> None:
    gate = TrackingGate(_Sink(), TrackingLimits(tracking_bytes=2048), max_event_bytes=65536)
    gate.feed(_batch(1), 2000)
    with pytest.raises(TrackingFlood) as exc:
        gate.feed(_batch(1), 100)
    assert exc.value.code is ErrorCode.SANDBOX_VIOLATION


def test_gate_splits_every_event_under_the_cap() -> None:
    r, w = os.pipe()
    os.set_blocking(r, False)
    key = new_hmac_key()
    cap = 8192
    emitter = EventEmitter(
        w, run_id="run_x", attempt=1, key=base64.b64decode(key), max_event_bytes=cap
    )
    gate = TrackingGate(emitter.emit, TrackingLimits(), max_event_bytes=cap)
    labels = {f"l{i}": "v" * 60 for i in range(8)}
    msg = _batch(1000, key="k" * 60, labels=labels)
    gate.feed(msg, len(encode_frame(msg)))
    big = ParamsPayload(params=[ParamItem(key=f"p{i}", value="x" * 3000) for i in range(6)])
    gate.feed(ParamsMessage(payload=big), 20000)
    data = b""
    while True:
        try:
            chunk = os.read(r, 1 << 20)
        except BlockingIOError:
            break
        if not chunk:
            break
        data += chunk
    lines = [line for line in data.split(b"\n") if line]
    events = [decode_event_line(line) for line in lines]
    assert all(len(line) <= cap for line in lines) and len(lines) > 2
    assert all(e is not None and verify_event(e, key) for e in events)
    bodies: list[tuple[str, Any]] = [(e.type, e.payload) for e in events if e is not None]
    steps = [s for t, p in bodies if t == "metrics" for s in p["series"][0]["steps"]]
    assert steps == list(range(1000))
    assert sum(len(p["params"]) for t, p in bodies if t == "params") == 6
    os.close(r)
    os.close(w)


def test_gate_refuses_non_user_streams_and_cleans_strings() -> None:
    sink = _Sink()
    gate = TrackingGate(sink, TrackingLimits(log_lines=1), max_event_bytes=65536)
    with pytest.raises(ProtocolError):
        gate.feed(UserLogMessage(payload=LogPayload(stream="stdout", line="spoof")), 10)
    gate.feed(
        UserLogMessage(payload=LogPayload(stream="user", line="\x1b[31mred\x1b[0m", level="info")),
        10,
    )
    gate.feed(UserLogMessage(payload=LogPayload(stream="user", line="over")), 10)
    gate.feed(TagsMessage(payload=TagsPayload(tags=[TagItem(key="t", value="a\x1b[2Jb")])), 10)
    log, tags = sink.events
    assert isinstance(log, LogPayload) and log.line == "red" and log.stream == "user"
    assert isinstance(tags, TagsPayload) and tags.tags[0].value == "ab"
    warning = gate.warning()
    assert warning is not None and "log lines over the limit of 1 (1 dropped)" in warning.message


# ========================================================================= channel


def test_the_four_tracking_messages_parse() -> None:
    msgs: list[Any] = [
        _batch(3),
        ParamsMessage(payload=ParamsPayload(params=[ParamItem(key="fit.draws", value=50)])),
        TagsMessage(payload=TagsPayload(tags=[TagItem(key="team", value="a")])),
        UserLogMessage(
            payload=LogPayload(stream="user", line="hi", level="debug", fields={"k": 1})
        ),
    ]
    for m in msgs:
        assert parse_worker_message(encode_frame(m)[4:]) == m
    with pytest.raises(ProtocolError):
        parse_worker_message(
            b'{"type":"tags","payload":{"tags":[{"key":"mapi.kind","value":"x"}]}}'
        )
    assert StartMessage.model_fields["tracking"].default_factory is TrackingLimits
