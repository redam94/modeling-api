"""Run media in the runner (v0.14): the worker's ``log_media`` over a real socketpair and the
supervisor's media path (safe read, sha256 + magic check, budgets, dedupe, upload through the
media POST policy of the GCS shim, ``media`` events, finalize with media objects present).

The supervisor runs in-process (its media handler, upload pool and emitter); no worker
subprocess is needed to exercise the path.
"""

from __future__ import annotations

import hashlib
import os
import socket
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from mapi_runner.channel import (
    MAX_WORKER_FRAME,
    MediaMessage,
    StartMessage,
    UserLogMessage,
    parse_worker_message,
    recv_frame,
)
from mapi_runner.events import EventEmitter
from mapi_runner.supervisor import Supervisor, SupervisorConfig
from mapi_runner.transfer import new_client
from mapi_runner.worker.context import WorkerContext
from mapi_sdk.media import MediaItem, encode_png, image_item, table_item
from mapi_sdk.models import FitContext
from mapi_spec.executor import RunEvent, RunEventType, RunKind
from mapi_spec.run import (
    OUTPUT_MEDIA,
    MediaPayload,
    OutputManifest,
    RunSpec,
    WarningPayload,
    decode_event_line,
    verify_event,
    verify_output_objects,
)
from mapi_spec.storage import crc32c, crc32c_to_base64, run_media_prefix
from mapi_spec.tracking import TrackingLimits
from runner_helpers import TENANT, make_run
from shimgcs import ShimGcs

pytestmark = pytest.mark.unit


def _png(value: int, size: int = 4) -> bytes:
    return encode_png(np.full((size, size), value, np.uint8))[0]


# ================================================================== in-process supervisor


class _Sup:
    """A supervisor with a bootstrapped RunSpec, scratch and an event file (no worker)."""

    def __init__(
        self,
        gcs: ShimGcs,
        tmp: Path,
        *,
        media: bool = True,
        tracking: dict[str, Any] | None = None,
        media_max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        run = make_run(gcs, RunKind.FIT, limits={"tracking": TrackingLimits(**(tracking or {}))})
        self.run = run
        rs = run.rs
        if media:
            prefix = run_media_prefix(TENANT, rs.run_id, rs.attempt)
            outputs = dict(rs.outputs)
            outputs[OUTPUT_MEDIA] = gcs.post_policy(
                gcs.buckets.runs, prefix, max_bytes=media_max_bytes, create_only=True
            )
            rs = RunSpec.model_validate(
                {**rs.model_dump(), "outputs": outputs, "media_prefix": prefix}
            )
        self.rs = rs
        self.events_path = tmp / "events.jsonl"
        self.fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        sup = Supervisor(
            SupervisorConfig(scratch_root=str(tmp)), {}, event_fd=self.fd, client=new_client()
        )
        sup.rs = rs
        sup.emitter = EventEmitter(
            self.fd,
            run_id=rs.run_id,
            attempt=rs.attempt,
            key=rs.hmac_key_bytes(),
            max_event_bytes=rs.limits.max_event_bytes,
        )
        sup._resolve_outputs(rs)
        sup._prepare_scratch()
        self.sup = sup
        assert sup.scratch is not None
        self.media_dir = Path(sup.scratch) / "media"

    def write(self, data: bytes, name: str | None = None) -> str:
        name = name or _name(data)
        (self.media_dir / name).write_bytes(data)
        return name

    def handle(self, msg: Any) -> None:
        self.sup._handle(msg)

    def drain(self) -> list[RunEvent]:
        if self.sup._uploads is not None:
            self.sup._uploads.shutdown(wait=True)
            self.sup._uploads = None
        events = [
            ev
            for line in self.events_path.read_text().splitlines()
            if (ev := decode_event_line(line)) is not None
        ]
        assert all(verify_event(ev, self.rs.hmac_key_bytes()) for ev in events)
        return events

    def close(self) -> None:
        os.close(self.fd)
        self.sup._cleanup()


def _name(data: bytes, ext: str = ".png") -> str:
    return hashlib.sha256(data).hexdigest() + ext


def _msg(file: str, key: str = "samples", **kw: Any) -> MediaMessage:
    kw.setdefault("kind", "image")
    kw.setdefault("content_type", "image/png")
    return MediaMessage(file=file, key=key, **kw)


def _media(events: list[RunEvent]) -> list[MediaPayload]:
    return [MediaPayload.model_validate(e.payload) for e in events if e.type == RunEventType.MEDIA]


def _warnings(events: list[RunEvent]) -> list[WarningPayload]:
    return [
        WarningPayload.model_validate(e.payload) for e in events if e.type == RunEventType.WARNING
    ]


@pytest.fixture
def sup(gcs: ShimGcs, tmp_path: Path) -> Any:
    s = _Sup(gcs, tmp_path)
    yield s
    s.close()


# ============================================================ worker -> supervisor path


class _Worker:
    """A WorkerContext on one end of a socketpair (scratch = the supervisor's)."""

    def __init__(self, scratch: str, *, media: bool, limits: TrackingLimits) -> None:
        self.a, self.b = socket.socketpair()
        start = StartMessage(
            run_id="run_m",
            attempt=1,
            kind=RunKind.FIT,
            tenant_id="t",
            project_id="p",
            spec={},
            scratch=scratch,
            max_output_bytes=1 << 20,
            max_output_objects=10,
            deadline=datetime.now(UTC) + timedelta(hours=1),
            tracking=limits,
            media=media,
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


def test_worker_images_and_a_table_reach_the_store_once_per_sha(gcs: ShimGcs, sup: _Sup) -> None:
    assert sup.sup.scratch is not None
    worker = _Worker(sup.sup.scratch, media=True, limits=TrackingLimits())
    ctx = FitContext(tracker=worker.ctx.tracker)
    assert ctx.tracking_media
    img0, img1 = np.zeros((6, 5), np.uint8), np.full((6, 5), 200, np.uint8)
    ctx.log_image("samples", img0, step=0, caption="first")
    ctx.log_image("samples", img1, step=1)
    ctx.log_image("samples", img0, step=2, labels={"split": "test"})  # same bytes as step 0
    ctx.log_table("scores", {"class": ["a", "b"], "n": [3, 4]})
    msgs = worker.finish()
    media = [m for m in msgs if isinstance(m, MediaMessage)]
    assert len(media) == 4
    for m in msgs:
        sup.handle(m)
    events = sup.drain()
    payloads = _media(events)
    assert [(p.key, p.step, p.labels) for p in payloads] == [
        ("samples", 0, {}),
        ("samples", 1, {}),
        ("samples", 2, {"split": "test"}),
        ("scores", None, {}),
    ]
    assert payloads[0].caption == "first"
    assert payloads[0].name == payloads[2].name
    assert (payloads[0].width, payloads[0].height) == (5, 6)
    assert payloads[3].kind == "table" and payloads[3].width is None
    prefix = sup.rs.media_prefix
    assert prefix is not None
    stored = {m.name: m for m in gcs.list(gcs.buckets.runs, prefix)}
    assert len(stored) == 3  # 2 images + 1 table
    for p in payloads:
        data = gcs.get(gcs.buckets.runs, prefix + p.name)
        assert hashlib.sha256(data).hexdigest() == p.sha256
        assert p.size == len(data)
        assert p.crc32c == crc32c_to_base64(crc32c(data)) == stored[prefix + p.name].crc32c
    assert not list(sup.media_dir.iterdir())  # processed files are deleted
    assert not _warnings(events)


def test_worker_without_a_media_policy_skips_with_one_line(tmp_path: Path) -> None:
    worker = _Worker(str(tmp_path), media=False, limits=TrackingLimits())
    ctx = FitContext(tracker=worker.ctx.tracker)
    for step in range(3):
        ctx.log_image("samples", np.zeros((2, 2), np.uint8), step=step)
    msgs = worker.finish()
    assert not [m for m in msgs if isinstance(m, MediaMessage)]
    lines = [m.payload for m in msgs if isinstance(m, UserLogMessage)]
    assert len(lines) == 1 and lines[0].level == "warning"
    assert not (tmp_path / "media").exists()


def test_worker_keeps_the_media_budget_and_never_raises(tmp_path: Path) -> None:
    worker = _Worker(
        str(tmp_path), media=True, limits=TrackingLimits(media_items=2, media_bytes=10_000)
    )
    tracker = worker.ctx.tracker
    for step in range(5):
        tracker.log_media(image_item("samples", _png(step), step=step))
    (tmp_path / "media").chmod(0o500)  # the next write fails: one line, no exception
    try:
        worker2_item = image_item("other", _png(99))
        tracker._media_items = 0  # room again, so the write is attempted
        tracker.log_media(worker2_item)
    finally:
        (tmp_path / "media").chmod(0o700)
    msgs = worker.finish()
    assert len([m for m in msgs if isinstance(m, MediaMessage)]) == 2
    lines = [m.payload.line for m in msgs if isinstance(m, UserLogMessage)]
    assert any("media budget reached" in line for line in lines)
    assert sum("media budget reached" in line for line in lines) == 1
    assert any("was not logged" in line for line in lines)


def test_worker_reuses_a_waiting_file_of_the_same_bytes(tmp_path: Path) -> None:
    worker = _Worker(str(tmp_path), media=True, limits=TrackingLimits())
    item = image_item("samples", _png(7))
    worker.ctx.tracker.log_media(item)
    worker.ctx.tracker.log_media(MediaItem(**{**_fields(item), "step": 3}))
    msgs = worker.finish()
    files = [m.file for m in msgs if isinstance(m, MediaMessage)]
    assert files == [item.name, item.name]
    assert (tmp_path / "media" / item.name).read_bytes() == item.data


def _fields(item: MediaItem) -> dict[str, Any]:
    return {f: getattr(item, f) for f in MediaItem.__dataclass_fields__}


# ======================================================================== supervisor checks


def test_symlink_in_media_is_refused(sup: _Sup, tmp_path: Path) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(_png(1))
    name = _name(secret.read_bytes())
    (sup.media_dir / name).symlink_to(secret)
    sup.handle(_msg(name))
    events = sup.drain()
    assert not _media(events)
    [w] = _warnings(events)
    assert w.code == "MEDIA_INVALID" and "cannot be opened safely" in w.message
    assert sup.sup.failure is None  # the run continues
    assert secret.exists()


def test_oversize_file_is_dropped_with_a_warning(gcs: ShimGcs, tmp_path: Path) -> None:
    s = _Sup(gcs, tmp_path, tracking={"media_item_bytes": 1024})
    try:
        big = encode_png(np.random.default_rng(0).integers(0, 255, (64, 64), np.uint8))[0]
        assert len(big) > 1024
        s.handle(_msg(s.write(big)))
        events = s.drain()
        assert not _media(events)
        assert [w.code for w in _warnings(events)] == ["MEDIA_INVALID"]
        assert not list(s.media_dir.iterdir())
    finally:
        s.close()


def test_budget_warns_once_and_drops(gcs: ShimGcs, tmp_path: Path) -> None:
    s = _Sup(gcs, tmp_path, tracking={"media_items": 2})
    try:
        for i in range(5):
            s.handle(_msg(s.write(_png(i)), step=i))
        events = s.drain()
        assert [p.step for p in _media(events)] == [0, 1]
        assert [w.code for w in _warnings(events)] == ["MEDIA_BUDGET"]
        assert not list(s.media_dir.iterdir())
    finally:
        s.close()


def test_byte_budget_counts_distinct_bytes(gcs: ShimGcs, tmp_path: Path) -> None:
    a, b = _png(1), _png(2)
    s = _Sup(gcs, tmp_path, tracking={"media_bytes": len(a) + 1})
    try:
        s.handle(_msg(s.write(a), step=0))
        s.handle(_msg(s.write(a), step=1))  # the same bytes: no new bytes
        s.handle(_msg(s.write(b), step=2))
        events = s.drain()
        assert [p.step for p in _media(events)] == [0, 1]
        assert [w.code for w in _warnings(events)] == ["MEDIA_BUDGET"]
    finally:
        s.close()


@pytest.mark.parametrize(
    ("data", "content_type", "kind"),
    [
        (b"GIF89a" + b"\0" * 40, "image/png", "image"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/png", "image"),
        (b'{"columns": []}', "application/json", "table"),
    ],
)
def test_bad_magic_or_table_is_dropped(
    sup: _Sup, data: bytes, content_type: str, kind: str
) -> None:
    ext = ".json" if kind == "table" else ".png"
    name = _name(data, ext)
    sup.write(data, name)
    sup.handle(_msg(name, kind=kind, content_type=content_type))
    events = sup.drain()
    assert not _media(events)
    [w] = _warnings(events)
    assert w.code == "MEDIA_INVALID" and w.untrusted_text


def test_a_name_that_lies_about_the_sha_is_dropped(sup: _Sup) -> None:
    name = "0" * 64 + ".png"
    sup.write(_png(3), name)
    sup.handle(_msg(name))
    events = sup.drain()
    assert not _media(events)
    assert [w.code for w in _warnings(events)] == ["MEDIA_INVALID"]


def test_missing_file_is_a_warning_not_a_violation(sup: _Sup) -> None:
    sup.handle(_msg(_name(b"nothing")))
    events = sup.drain()
    assert [w.code for w in _warnings(events)] == ["MEDIA_INVALID"]
    assert sup.sup.failure is None


def test_no_media_policy_warns_once(gcs: ShimGcs, tmp_path: Path) -> None:
    s = _Sup(gcs, tmp_path, media=False)
    try:
        for i in range(3):
            s.handle(_msg(s.write(_png(i))))
        events = s.drain()
        assert not _media(events)
        assert [w.code for w in _warnings(events)] == ["MEDIA_IGNORED"]
        assert not list(s.media_dir.iterdir())
    finally:
        s.close()


def test_upload_failure_is_a_warning(gcs: ShimGcs, tmp_path: Path) -> None:
    data = _png(5)
    s = _Sup(gcs, tmp_path)
    try:
        prefix = s.rs.media_prefix
        assert prefix is not None
        gcs.put(gcs.buckets.runs, prefix + _name(data), b"taken", "image/png")  # create-only
        s.sup.transfers.attempts = 1
        s.handle(_msg(s.write(data)))
        s.handle(_msg(s.write(data), step=1))  # the duplicate of a failed upload: no event
        events = s.drain()
        assert not _media(events)
        assert [w.code for w in _warnings(events)] == ["MEDIA_UPLOAD_FAILED"]
        assert s.sup.failure is None and s.sup.pending.async_failure is None
    finally:
        s.close()


def test_start_message_tells_the_worker_about_the_policy(sup: _Sup) -> None:
    assert sup.sup.media_policy is not None
    assert sup.sup.media_policy.prefix == sup.rs.media_prefix
    assert sup.media_dir.is_dir()


def test_finalize_accepts_media_objects_under_the_run_prefix(gcs: ShimGcs, sup: _Sup) -> None:
    sup.handle(_msg(sup.write(_png(9)), step=0))
    table = table_item("scores", [{"a": 1}])
    sup.write(table.data, table.name)
    sup.handle(_msg(table.name, key="scores", kind="table", content_type="application/json"))
    sup.drain()
    assert sup.sup._complete("complete") == 0
    raw = gcs.get(gcs.buckets.runs, sup.run.prefix + "manifest.json")
    manifest = OutputManifest.model_validate_json(raw)
    listing = gcs.list(gcs.buckets.runs, sup.run.prefix)
    assert any("/media/" in m.name for m in listing)
    result = verify_output_objects(manifest, listing, expected_prefix=sup.run.prefix)
    assert result.ok, result.issues
