"""File checkpoints and the GPU class in the runner (v0.15): the run's checkpoint sink as a
``FileCheckpointSink`` (atomic files, a ``checkpoint`` message per save, a round trip, a
resumed attempt that finds the files the previous attempt uploaded), the channel's names
for weights and optimizer files, their upload content type, and the supervisor's one
``GPU_UNAVAILABLE`` warning for a GPU-class run without an NVIDIA device."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from collection_fixtures import Worker, put_inputs
from mapi_runner import supervisor as supervisor_mod
from mapi_runner.channel import CheckpointMessage
from mapi_runner.events import EventEmitter
from mapi_runner.supervisor import Supervisor, SupervisorConfig, _ckpt_type
from mapi_runner.transfer import new_client
from mapi_runner.worker.checkpoints import RunCheckpointSink
from mapi_sdk.checkpoints import FileCheckpointSink, checkpoint_names
from mapi_spec.errors import MapiError
from mapi_spec.executor import RunEventType, RunKind
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunSpec, WarningPayload, decode_event_line
from pydantic import ValidationError
from runner_helpers import make_run
from shimgcs import ShimGcs

pytestmark = pytest.mark.unit

KEY = "0123456789abcdef" * 2


def _sink(tmp: Path, **kw: object) -> tuple[Worker, RunCheckpointSink]:
    w = Worker(tmp, {}, kind=RunKind.FIT, checkpointing=True, **kw)  # type: ignore[arg-type]
    return w, RunCheckpointSink(w.ctx)


def test_file_sink_round_trip(tmp_path: Path) -> None:
    w, sink = _sink(tmp_path / "w")
    try:
        assert isinstance(sink, FileCheckpointSink)
        assert sink.load_files(KEY) is None
        sink.save_files(KEY, {"weights": b"w1", "opt": b"o1"}, {"epoch": 1}, 1)
        sink.save_files(KEY, {"weights": b"w2", "opt": b"o2"}, {"epoch": 2, "best": 0.5}, 2)
        got = sink.load_files(KEY)
        assert got is not None
        assert got.files == {"weights": b"w2", "opt": b"o2"}
        assert got.meta == {"epoch": 2, "best": 0.5} and got.progress == 2
        assert sink.load(KEY) is None  # never mistaken for a PyMC draws checkpoint
        with pytest.raises(MapiError):
            sink.save_files(KEY, {"model": b"x"}, {}, 1)
        with pytest.raises(MapiError):
            sink.save_files("not-hex", {"weights": b"x"}, {}, 1)
        assert not list(sink.root.glob("*.tmp"))
    finally:
        msgs = w.finish()
    names = checkpoint_names(KEY)
    ckpts = [m for m in msgs if isinstance(m, CheckpointMessage)]
    assert [m.files for m in ckpts] == [[names["weights"], names["opt"], names["meta"]]] * 2
    assert [m.segment for m in ckpts] == [1, 2] and ckpts[0].draws_done is None


def test_incomplete_checkpoint_is_not_loaded(tmp_path: Path) -> None:
    w, sink = _sink(tmp_path / "w")
    try:
        sink.save_files(KEY, {"weights": b"w", "opt": b"o"}, {}, 1)
        (sink.root / checkpoint_names(KEY)["opt"]).unlink()
        assert sink.load_files(KEY) is None
        (sink.root / checkpoint_names(KEY)["meta"]).write_text("{not json")
        assert sink.load_files(KEY) is None
    finally:
        w.finish()


def test_a_meta_too_large_to_resume_from_is_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_files`` ignores a meta above the cap, so saving one would make a resumed
    attempt start from scratch without a word: it is refused with a warning instead, and
    the previous checkpoint stays."""
    from mapi_runner.channel import WarningMessage
    from mapi_runner.worker import checkpoints as ckpt_mod

    monkeypatch.setattr(ckpt_mod, "_MAX_META_BYTES", 400)
    w, sink = _sink(tmp_path / "w")
    try:
        sink.save_files(KEY, {"weights": b"w1", "opt": b"o1"}, {"epoch": 1}, 1)
        sink.save_files(KEY, {"weights": b"w2", "opt": b"o2"}, {"history": ["x" * 500]}, 2)
        got = sink.load_files(KEY)
        assert got is not None and got.progress == 1 and got.files["weights"] == b"w1"
    finally:
        msgs = w.finish()
    assert len([m for m in msgs if isinstance(m, CheckpointMessage)]) == 1
    assert [m.payload.code for m in msgs if isinstance(m, WarningMessage)] == ["CHECKPOINT_SKIPPED"]


def test_a_resumed_attempt_finds_the_uploaded_files(gcs: ShimGcs, tmp_path: Path) -> None:
    names = checkpoint_names(KEY)
    w, sink = _sink(tmp_path / "first")
    try:
        sink.save_files(KEY, {"weights": b"\x00weights", "opt": b"\x01opt"}, {"step": 7}, 3)
        files = {n: (sink.root / n).read_bytes() for n in names.values()}
    finally:
        w.finish()
    resume = put_inputs(gcs, "ckpt", files)  # what the supervisor uploaded
    w2, sink2 = _sink(tmp_path / "second", resume=resume)
    try:
        assert sorted(w2.ctx.download_resume()) == sorted(names.values())
        got = sink2.load_files(KEY)
    finally:
        w2.finish()
    assert got is not None and got.progress == 3 and got.meta == {"step": 7}
    assert got.files == {"weights": b"\x00weights", "opt": b"\x01opt"}


@pytest.mark.parametrize(
    "name",
    [
        f"ckpt-{KEY}.weights.safetensors",
        f"ckpt-{KEY}.opt.safetensors",
        f"ckpt-{KEY}.meta.json",
        f"ckpt-{KEY}.draws.zarr.zip",
    ],
)
def test_channel_accepts_checkpoint_names(name: str) -> None:
    assert CheckpointMessage(files=[name], segment=0).files == [name]


@pytest.mark.parametrize(
    "name",
    [
        f"ckpt-{KEY}.model.safetensors",
        f"ckpt-{KEY}.weights.pt",
        f"ckpt-{KEY[:8]}.weights.safetensors",
        f"../ckpt-{KEY}.meta.json",
    ],
)
def test_channel_refuses_other_names(name: str) -> None:
    with pytest.raises(ValidationError):
        CheckpointMessage(files=[name], segment=0)


def test_checkpoint_content_types() -> None:
    assert _ckpt_type("ckpt-x.weights.safetensors") == "application/octet-stream"
    assert _ckpt_type("ckpt-x.meta.json") == "application/json"
    assert _ckpt_type("ckpt-x.draws.zarr.zip") == "application/zip"


# ================================================================================ GPU class


def _gpu_warnings(
    gcs: ShimGcs, tmp: Path, monkeypatch: pytest.MonkeyPatch, cls: ResourceClass, devices: set[str]
) -> list[WarningPayload]:
    monkeypatch.setattr(supervisor_mod.os.path, "exists", lambda p: p in devices)
    run = make_run(gcs, RunKind.FIT)
    rs = RunSpec.model_validate({**run.rs.model_dump(), "resource_class": cls})
    tmp.mkdir(parents=True, exist_ok=True)
    events = tmp / "events.jsonl"
    fd = os.open(events, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        sup = Supervisor(
            SupervisorConfig(scratch_root=str(tmp)), {}, event_fd=fd, client=new_client()
        )
        sup.rs = rs
        sup.emitter = EventEmitter(
            fd,
            run_id=rs.run_id,
            attempt=rs.attempt,
            key=rs.hmac_key_bytes(),
            max_event_bytes=rs.limits.max_event_bytes,
        )
        sup._warn_gpu(rs)
    finally:
        os.close(fd)
    out = []
    for line in events.read_text().splitlines():
        ev = decode_event_line(line)
        if ev is not None and ev.type == RunEventType.WARNING:
            out.append(WarningPayload.model_validate(ev.payload))
    return out


def test_gpu_run_without_a_device_warns_once(
    gcs: ShimGcs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (warning,) = _gpu_warnings(gcs, tmp_path, monkeypatch, ResourceClass.GPU, set())
    assert warning.code == "GPU_UNAVAILABLE"
    assert warning.message == (
        "The run asked for the GPU class, but no GPU is visible. It runs on the CPU."
    )


def test_gpu_run_with_a_device_or_a_cpu_run_does_not_warn(
    gcs: ShimGcs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gpu = _gpu_warnings(gcs, tmp_path / "a", monkeypatch, ResourceClass.GPU, {"/dev/nvidiactl"})
    cpu = _gpu_warnings(gcs, tmp_path / "b", monkeypatch, ResourceClass.S, set())
    assert gpu == [] and cpu == []


def test_meta_json_of_a_file_checkpoint(tmp_path: Path) -> None:
    w, sink = _sink(tmp_path / "w")
    try:
        sink.save_files(KEY, {"weights": b"w"}, {"epoch": 1}, 1)
        doc = json.loads((sink.root / checkpoint_names(KEY)["meta"]).read_text())
    finally:
        w.finish()
    assert doc == {
        "key": KEY,
        "kind": "files",
        "files": ["weights"],
        "progress": 1,
        "meta": {"epoch": 1},
    }
