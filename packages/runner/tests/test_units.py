"""Unit tests of the supervisor's building blocks (no subprocesses)."""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mapi_runner._text import LineSplitter, clean_line, truncate_utf8
from mapi_runner.channel import (
    MAX_WORKER_FRAME,
    CheckpointMessage,
    ErrorMessage,
    FrameReader,
    ProgressMessage,
    ProtocolError,
    ResultMessage,
    StartMessage,
    encode_frame,
    parse_worker_message,
    recv_frame,
    send_frame,
)
from mapi_runner.events import EventEmitter, LogFlood, LogGate, ProgressCoalescer
from mapi_runner.jobs import (
    DeriveJob,
    DescribeJob,
    EvaluateJob,
    IngestJob,
    RenderJob,
    ScoreJob,
    parse_job,
)
from mapi_runner.outputs import OutputViolation, collect_outputs, content_type_for
from mapi_runner.sandbox import WORKER_UID, plan_isolation
from mapi_runner.supervisor import EXIT_FOR_CLASS
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.run import (
    FailureClass,
    LogPayload,
    ProgressPayload,
    RunnerExit,
    RunnerPhase,
    StatePayload,
    decode_event_line,
    failure_class_for_exit,
    new_hmac_key,
    verify_event,
)
from pydantic import BaseModel

pytestmark = pytest.mark.unit

# =========================================================================== text


def test_clean_line_strips_ansi_osc_and_controls() -> None:
    raw = "\x1b[1;31mred\x1b[0m \x1b]0;title\x07ok\x00\x08 tab\there ‮evil"
    assert clean_line(raw) == "red ok tab here evil"


def test_truncate_utf8_keeps_character_boundaries() -> None:
    text, cut = truncate_utf8("é" * 10, 5)
    assert cut and text == "éé" and len(text.encode()) <= 5
    assert truncate_utf8("abc", 5) == ("abc", False)


def test_line_splitter_handles_partial_long_and_cr_lines() -> None:
    sp = LineSplitter(8)
    assert sp.feed(b"ab") == []
    assert sp.feed(b"c\nde\rf") == [(b"abc", False), (b"de", False)]
    out = sp.feed(b"0123456789abc")  # 'f' + 13 bytes: cut at 8, rest discarded
    assert out == [(b"f0123456", True)]
    assert sp.feed(b"more\nnext\n") == [(b"next", False)]
    assert sp.feed(b"tail") == [] and sp.flush() == [(b"tail", False)]


@given(st.binary(max_size=2000), st.integers(min_value=1, max_value=64))
def test_line_splitter_never_emits_lines_over_the_cap(data: bytes, cap: int) -> None:
    sp = LineSplitter(cap)
    lines = sp.feed(data) + sp.flush()
    assert all(len(line) <= cap for line, _ in lines)
    assert all(b"\n" not in line and b"\r" not in line for line, _ in lines)


# ======================================================================== channel


def test_frames_round_trip_over_a_socketpair() -> None:
    a, b = socket.socketpair()
    with a, b:
        send_frame(a, ResultMessage())
        body = recv_frame(b, max_bytes=MAX_WORKER_FRAME)
        assert body is not None
        assert isinstance(parse_worker_message(body), ResultMessage)
        a.close()
        assert recv_frame(b, max_bytes=MAX_WORKER_FRAME) is None


def test_frame_reader_reassembles_and_caps() -> None:
    frame = encode_frame(ProgressMessage(payload=ProgressPayload(phase=RunnerPhase.FIT)))
    reader = FrameReader()
    assert reader.feed(frame[:3]) == [] and reader.pending
    bodies = reader.feed(frame[3:] + frame)
    assert len(bodies) == 2 and not reader.pending
    with pytest.raises(ProtocolError):
        FrameReader(max_bytes=10).feed(struct.pack(">I", 11) + b"x" * 11)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"type": "complete"}',
        b'{"type": "result", "status": "partial"}',
        b'{"type": "checkpoint", "files": ["../../etc/passwd"], "segment": 0}',
        b'{"type": "state", "payload": {"phase": "nope"}}',
        b'{"type": "error", "code": "NOT_A_CODE", "message": "x"}',
    ],
)
def test_invalid_worker_messages_are_protocol_errors(body: bytes) -> None:
    with pytest.raises(ProtocolError):
        parse_worker_message(body)


def test_valid_checkpoint_and_error_messages_parse() -> None:
    msg = parse_worker_message(
        json.dumps(
            {"type": "checkpoint", "files": ["ckpt-" + "a" * 32 + ".meta.json"], "segment": 2}
        ).encode()
    )
    assert isinstance(msg, CheckpointMessage) and msg.segment == 2
    err = parse_worker_message(b'{"type": "error", "code": "USER_CODE_ERROR", "message": "x"}')
    assert isinstance(err, ErrorMessage) and err.code is ErrorCode.USER_CODE_ERROR


def test_start_message_never_carries_the_hmac_key() -> None:
    fields = set(StartMessage.model_fields)
    assert not fields & {"hmac_key", "outputs", "nonce", "checkpoint_prefix"}
    schema = json.dumps(StartMessage.model_json_schema())
    assert "SignedPut" not in schema and "PostPolicy" not in schema


# ========================================================================= events


def _read_lines(fd: int) -> list[str]:
    os.set_blocking(fd, False)
    try:
        data = os.read(fd, 1 << 20)
    except BlockingIOError:
        data = b""
    return [line for line in data.decode().split("\n") if line]


def test_emitter_signs_sequenced_lines() -> None:
    r, w = os.pipe()
    key = new_hmac_key()
    emitter = EventEmitter(
        w, run_id="run_x", attempt=2, key=base64.b64decode(key), max_event_bytes=65536
    )
    emitter.emit(StatePayload(phase=RunnerPhase.BOOTSTRAP))
    emitter.emit(LogPayload(stream="system", line="hello"))
    lines = _read_lines(r)
    events = [decode_event_line(line) for line in lines]
    assert [e.seq for e in events if e] == [0, 1]
    assert all(e is not None and e.attempt == 2 and verify_event(e, key) for e in events)
    os.close(r)
    os.close(w)


def test_emitter_refuses_oversized_events() -> None:
    r, w = os.pipe()
    emitter = EventEmitter(w, run_id="run_x", attempt=1, key=b"k" * 32, max_event_bytes=1024)
    with pytest.raises(MapiError) as exc:
        emitter.emit(LogPayload(stream="stdout", line="x" * 2000))
    assert exc.value.code is ErrorCode.SANDBOX_VIOLATION
    assert emitter.next_seq == 0
    os.close(r)
    os.close(w)


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_progress_is_coalesced_to_one_per_interval() -> None:
    sent: list[BaseModel] = []
    clock = _Clock()
    pc = ProgressCoalescer(lambda p: sent.append(p) or 0, 2.0, clock=clock)
    for i in range(10):
        pc.offer(ProgressPayload(phase=RunnerPhase.SAMPLE, fraction=i / 10))
        clock.t += 0.3
    # the first is sent at once, then at most one per 2 s: t=100 and t=102.1
    assert [p.fraction for p in sent] == [0.0, 0.7]  # type: ignore[attr-defined]
    assert pc.due_in() is not None
    clock.t += 5
    pc.tick()
    assert [p.fraction for p in sent][-1] == 0.9  # type: ignore[attr-defined]
    pc.offer(ProgressPayload(phase=RunnerPhase.SAMPLE, fraction=1.0))
    pc.flush()  # not due yet: dropped, never sent early
    assert len(sent) == 3 and pc.due_in() is None


def test_log_gate_caps_lines_and_total() -> None:
    sent: list[LogPayload] = []
    gate = LogGate(lambda p: sent.append(p) or 0, line_bytes=16, total_bytes=100)  # type: ignore[arg-type,func-returns-value]
    gate.feed("stderr", b"short\n" + b"y" * 40 + b"\n\n")
    assert [(p.line, p.truncated) for p in sent] == [("short", False), ("y" * 16, True)]
    with pytest.raises(LogFlood):
        gate.feed("stdout", b"z" * 80)
    gate.system("supervisor says hi")
    assert sent[-1].stream == "system"


# =========================================================================== jobs


def test_parse_job_per_kind() -> None:
    mapping = {
        "format": "csv",
        "dims": {"d": {"column": "d", "spec": {"kind": "categorical"}}},
        "vars": {"v": {"column": "v"}},
    }
    assert isinstance(parse_job(RunKind.INGEST, mapping), IngestJob)
    assert parse_job(RunKind.INGEST, {"preset": "mff"}).preset == "mff"  # type: ignore[union-attr]
    assert isinstance(parse_job(RunKind.DESCRIBE, {}), DescribeJob)
    req = {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": "endpoint://p/m"}
    assert isinstance(parse_job(RunKind.SCORE, req), ScoreJob)
    assert isinstance(parse_job(RunKind.SCORE, {"request": req, "plots": []}), ScoreJob)
    assert isinstance(parse_job(RunKind.EVALUATE, {}), EvaluateJob)
    assert isinstance(parse_job(RunKind.DERIVE, {"names": ["contributions"]}), DeriveJob)
    assert isinstance(parse_job(RunKind.RENDER, {"format": "svg"}), RenderJob)


@pytest.mark.parametrize(
    ("kind", "spec"),
    [
        (RunKind.INGEST, {}),
        (RunKind.INGEST, {"format": "csv"}),
        (RunKind.DESCRIBE, {"x": 1}),
        (RunKind.FIT, {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": "endpoint://p/m"}),
        (RunKind.DERIVE, {"names": []}),
        (RunKind.RENDER, {"format": "gif"}),
    ],
)
def test_parse_job_rejects_bad_specs(kind: RunKind, spec: dict[str, object]) -> None:
    with pytest.raises(MapiError) as exc:
        parse_job(kind, spec)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID


def test_optimize_jobs_parse_and_parents_never_run() -> None:
    """M3.5: ``optimize`` runs carry an OptimizeJob; workflow parents have no job."""
    with pytest.raises(MapiError):
        parse_job(RunKind.OPTIMIZE, {})
    job = parse_job(RunKind.OPTIMIZE, {"request": {"total_budget": 1}, "model": "model://p/m@v1"})
    assert job.request.total_budget == 1  # type: ignore[union-attr]
    with pytest.raises(NotImplementedError):
        parse_job(RunKind.WORKFLOW, {})


# ======================================================================== outputs


def test_collect_outputs_walks_regular_files(tmp_path: Path) -> None:
    (tmp_path / "ui" / "grids").mkdir(parents=True)
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "ui" / "grids" / "g.arrow").write_bytes(b"123")
    files = collect_outputs(str(tmp_path), max_objects=10)
    try:
        assert [(f.name, f.size, f.content_type) for f in files] == [
            ("a.json", 2, "application/json"),
            ("ui/grids/g.arrow", 3, "application/vnd.apache.arrow.file"),
        ]
    finally:
        for f in files:
            os.close(f.fd)


@pytest.mark.parametrize(
    "setup",
    ["symlink", "dir_symlink", "fifo", "manifest", "checkpoints"],
)
def test_collect_outputs_refuses_unsafe_trees(tmp_path: Path, setup: str) -> None:
    (tmp_path / "ok.json").write_text("{}")
    if setup == "symlink":
        (tmp_path / "x").symlink_to("/etc/hosts")
    elif setup == "dir_symlink":
        (tmp_path / "d").symlink_to("/etc", target_is_directory=True)
    elif setup == "fifo":
        os.mkfifo(tmp_path / "pipe")
    elif setup == "manifest":
        (tmp_path / "manifest.json").write_text("{}")
    elif setup == "checkpoints":
        (tmp_path / "checkpoints").mkdir()
        (tmp_path / "checkpoints" / "c").write_text("x")
    with pytest.raises(OutputViolation):
        collect_outputs(str(tmp_path), max_objects=10)


def test_collect_outputs_too_many(tmp_path: Path) -> None:
    for i in range(3):
        (tmp_path / f"{i}.json").write_text("{}")
    with pytest.raises(OutputViolation):
        collect_outputs(str(tmp_path), max_objects=2)


def test_content_types_come_from_the_extension() -> None:
    assert content_type_for("x.zarr.zip") == "application/zip"
    assert content_type_for("r/p.SVG") == "image/svg+xml"
    assert content_type_for("blob") == "application/octet-stream"


# ======================================================================== sandbox


def test_isolation_plan() -> None:
    root_linux = plan_isolation(euid=0, platform="linux")
    assert root_linux.drop_privileges and root_linux.user == WORKER_UID
    assert root_linux.extra_groups == []
    dev = plan_isolation(euid=501, platform="darwin")
    assert dev.dev_mode and not dev.drop_privileges and dev.user is None
    assert "darwin" in (dev.reason or "")
    assert plan_isolation(euid=0, platform="darwin").dev_mode


def test_exit_codes_round_trip_through_the_contract() -> None:
    for fc, code in EXIT_FOR_CLASS.items():
        assert failure_class_for_exit(code) is fc
    assert failure_class_for_exit(RunnerExit.PARTIAL) is None
    assert set(EXIT_FOR_CLASS) == set(FailureClass)
