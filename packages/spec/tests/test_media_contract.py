"""Run media (v0.14): header sniffing, the table body, names, byte checks, the ``media`` run
event, the RunSpec media policy and the finalizer's tolerance of ``media/`` objects."""

from __future__ import annotations

import json
import struct
import zlib
from datetime import UTC, datetime
from typing import Any

import pytest
from mapi_spec import media as m
from mapi_spec.executor import RunEventType
from mapi_spec.run import (
    EVENT_PAYLOAD_MODELS,
    OUTPUT_MEDIA,
    MediaPayload,
    OutputManifest,
    RunSpec,
    make_event,
    parse_payload,
    verify_output_objects,
)
from mapi_spec.storage import (
    ObjectMeta,
    run_checkpoint_prefix,
    run_media_prefix,
    run_media_store_key,
    run_prefix,
)
from mapi_spec.tracking import TrackingLimits
from pydantic import ValidationError

pytestmark = pytest.mark.unit

SHA = "ab" * 32


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width: int = 3, height: int = 2, color: int = 0) -> bytes:
    """A tiny valid PNG (8-bit gray or RGB), built by hand."""
    channels = 3 if color == 2 else 1
    raw = b"".join(b"\x00" + bytes(i % 256 for i in range(width * channels)) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def jpeg(width: int = 5, height: int = 4, *, sof: int = 0xC0, app_bytes: int = 16) -> bytes:
    """A JPEG header: SOI, an APP0 segment, a DQT segment, then a SOF frame header."""
    app0 = b"\xff\xe0" + struct.pack(">H", app_bytes + 2) + b"J" * app_bytes
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + bytes(64)
    frame = (
        b"\xff" + bytes([sof]) + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    )
    return b"\xff\xd8" + app0 + dqt + frame + b"\xff\xda\x00\x08" + bytes(6) + b"\xff\xd9"


# ===================================================================================== sniffing


def test_sniff_png_and_jpeg_headers() -> None:
    assert m.sniff_image(png(3, 2)) == m.ImageInfo("png", 3, 2)
    assert m.sniff_image(png(7, 9, color=2)) == m.ImageInfo("png", 7, 9)
    assert m.sniff_image(jpeg(5, 4)) == m.ImageInfo("jpeg", 5, 4)
    # progressive (SOF2) frames and fill bytes before a marker
    assert m.sniff_image(jpeg(640, 480, sof=0xC2)) == m.ImageInfo("jpeg", 640, 480)
    filled = jpeg(8, 8).replace(b"\xff\xdb", b"\xff\xff\xff\xdb", 1)
    assert m.sniff_image(filled) == m.ImageInfo("jpeg", 8, 8)
    # DHT (C4) shares the SOF range and is skipped, not read as a frame
    dht = b"\xff\xc4" + struct.pack(">H", 5) + b"\x00\x01\x02"
    with_dht = jpeg(3, 3).replace(b"\xff\xdb", dht + b"\xff\xdb", 1)
    assert m.sniff_image(with_dht) == m.ImageInfo("jpeg", 3, 3)


@pytest.mark.parametrize(
    "data",
    [
        b"GIF89a\x01\x00\x01\x00\x00\x00\x00",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"RIFF\x00\x00\x00\x00WEBPVP8 ",
        png()[:20],  # truncated in the IHDR
        jpeg()[:30],  # truncated before the frame header
        b"\xff\xd8\xff\xd9",  # SOI then EOI: no frame
        b"\xff\xd8" + b"\xff\xe0\x00\x01",  # a segment length below 2
        b"",
    ],
)
def test_sniff_refuses_other_and_truncated_data(data: bytes) -> None:
    assert m.sniff_image(data) is None


def test_sniff_jpeg_scan_is_bounded() -> None:
    # a frame header after the first MiB is not read
    big = b"\xff\xe0\xff\xff" + bytes(0xFFFD)
    data = b"\xff\xd8" + big * 17 + jpeg(4, 4)[2:]
    assert len(data) > m._JPEG_SCAN_BYTES
    assert m.sniff_image(data) is None
    assert m.sniff_image(b"\xff\xd8" + big + jpeg(4, 4)[2:]) == m.ImageInfo("jpeg", 4, 4)


def test_sniff_zero_size_png_is_refused() -> None:
    zero = png(1, 1).replace(struct.pack(">II", 1, 1), struct.pack(">II", 0, 1), 1)
    assert m.sniff_image(zero) is None


# ======================================================================================= tables


def _table(**over: Any) -> dict[str, Any]:
    return {"columns": ["class", "n", "share"], "rows": [["cat", 3, 0.5], ["dog", 3, None]]} | over


def test_media_table_validates_shape_and_limits() -> None:
    t = m.MediaTable.model_validate(_table())
    assert t.rows[1] == ["dog", 3, None]
    for bad, match in (
        (_table(rows=[["cat", 3]]), "has 2 cells"),
        (_table(columns=["a", "a", "b"]), "unique"),
        (_table(columns=[]), "at least 1"),
        (_table(rows=[["x" * (m.MAX_TABLE_CELL_CHARS + 1), 1, 1.0]]), "at most"),
        (_table(rows=[["cat", 3, float("nan")]]), "not finite"),
        (_table(rows=[["cat", [1], 1.0]]), "valid"),
    ):
        with pytest.raises(ValidationError, match=match):
            m.MediaTable.model_validate(bad)
    cols = [f"c{i}" for i in range(m.MAX_TABLE_COLUMNS)]
    rows = [[0] * len(cols) for _ in range(m.MAX_TABLE_CELLS // len(cols) + 1)]
    with pytest.raises(ValidationError, match="cells"):
        m.MediaTable.model_validate({"columns": cols, "rows": rows})


# ================================================================================ names, checks


def test_media_name_and_key() -> None:
    assert m.media_name(SHA, "image/png") == SHA + ".png"
    assert m.media_name(SHA, "image/jpeg") == SHA + ".jpg"
    assert m.media_name(SHA, "application/json") == SHA + ".json"
    for sha, ctype in ((SHA.upper(), "image/png"), ("ab", "image/png"), (SHA, "image/gif")):
        with pytest.raises(ValueError):
            m.media_name(sha, ctype)
    assert m.check_media_key("samples") == "samples"
    for bad in ("train/samples", "Samples", "a.b", ""):
        with pytest.raises(ValueError, match="media key"):
            m.check_media_key(bad)
    assert set(m.MEDIA_EXT) == set(m.MEDIA_KINDS)


def test_check_media_accepts_images_and_tables() -> None:
    assert m.check_media("image", "image/png", png(4, 3)) == m.ImageInfo("png", 4, 3)
    assert m.check_media("image", "image/jpeg", jpeg(2, 2)) == m.ImageInfo("jpeg", 2, 2)
    assert m.check_media("table", "application/json", json.dumps(_table()).encode()) is None


@pytest.mark.parametrize(
    ("kind", "ctype", "data", "match"),
    [
        ("table", "image/png", png(), "does not match"),
        ("image", "image/gif", b"GIF89a", "not one of"),
        ("image", "image/png", b"", "empty"),
        ("image", "image/png", b"\x00" * (m.MAX_IMAGE_BYTES + 1), "maximum"),
        ("table", "application/json", b" " * (m.MAX_TABLE_BYTES + 1), "maximum"),
        ("image", "image/png", jpeg(), "not a PNG"),
        ("image", "image/jpeg", png(), "not a JPEG"),
        ("image", "image/png", png(m.MAX_IMAGE_SIDE + 1, 1), "maximum side"),
        ("table", "application/json", b"\xff\xfe", "not UTF-8 JSON"),
        ("table", "application/json", b"[1, 2]", "not valid"),
        ("table", "application/json", b'{"columns": ["a"], "rows": [[NaN]]}', "not valid"),
    ],
)
def test_check_media_refusals(kind: str, ctype: str, data: bytes, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        m.check_media(kind, ctype, data)


# ============================================================================== run protocol


def _payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key": "samples",
        "kind": "image",
        "content_type": "image/png",
        "name": SHA + ".png",
        "sha256": SHA,
        "size": 1200,
        "crc32c": "4waSgw==",
        "step": 3,
        "labels": {"split": "val"},
        "width": 16,
        "height": 16,
        "caption": "epoch 3",
    }
    return base | over


def test_media_payload_validators() -> None:
    assert EVENT_PAYLOAD_MODELS[RunEventType.MEDIA] is MediaPayload
    p = MediaPayload.model_validate(_payload())
    ev = make_event("run_01jaf3q2x8m9v7k5h4g3f2e1d0", 1, 5, p)
    assert ev.type is RunEventType.MEDIA and parse_payload(ev) == p
    table = _payload(
        kind="table", content_type="application/json", name=SHA + ".json", width=None, height=None
    )
    assert MediaPayload.model_validate(table).kind == "table"
    for bad, match in (
        (_payload(name="cd" * 32 + ".png"), "name must be"),
        (_payload(name=SHA + ".jpg"), "name must be"),
        (_payload(kind="table"), "does not match"),
        (_payload(width=None), "width and a height"),
        (dict(table, width=4), "no width"),
        (_payload(size=m.MAX_IMAGE_BYTES + 1), "less than or equal"),
        (_payload(width=m.MAX_IMAGE_SIDE + 1), "less than or equal"),
        (_payload(caption="x" * (m.MAX_MEDIA_CAPTION_CHARS + 1)), "at most"),
        (_payload(key="a/b"), "pattern"),
        (_payload(name="../x.png"), "pattern"),
    ):
        with pytest.raises(ValidationError, match=match):
            MediaPayload.model_validate(bad)


def test_tracking_limits_media_budgets() -> None:
    lim = TrackingLimits()
    assert (lim.media_items, lim.media_bytes, lim.media_item_bytes) == (
        1000,
        256 * 1024 * 1024,
        m.MAX_IMAGE_BYTES,
    )
    with pytest.raises(ValidationError):
        TrackingLimits(media_item_bytes=m.MAX_IMAGE_BYTES + 1)


def test_storage_media_keys() -> None:
    run = "run_01jaf3q2x8m9v7k5h4g3f2e1d0"
    assert run_media_prefix("acme", run, 2) == run_prefix("acme", run, 2) + "media/"
    assert run_media_prefix("acme", run, 1, 3).endswith("/a1/tasks/3/media/")
    assert run_media_prefix("acme", run, 1) != run_checkpoint_prefix("acme", run, 1)
    assert run_media_store_key("acme", run, SHA + ".png") == f"t/acme/run-media/{run}/{SHA}.png"
    for bad in ("../x.png", SHA + ".gif", "x.png"):
        with pytest.raises(ValueError):
            run_media_store_key("acme", run, bad)


def _runspec(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = RunSpec.model_json_schema()["examples"][0]
    doc = json.loads(json.dumps(doc))
    prefix = doc["outputs"]["artifacts"]["prefix"] + "media/"
    policy = {**doc["outputs"]["artifacts"], "prefix": prefix}
    doc["outputs"][OUTPUT_MEDIA] = policy
    doc["media_prefix"] = prefix
    return doc | over


def test_runspec_media_policy() -> None:
    rs = RunSpec.model_validate(_runspec())
    assert rs.media_prefix is not None and rs.media_prefix.endswith("/media/")
    plain = RunSpec.model_json_schema()["examples"][0]
    assert "media_prefix" not in RunSpec.model_validate(plain).model_dump(mode="json")
    doc = _runspec()
    with pytest.raises(ValidationError, match="POST policy on media_prefix"):
        RunSpec.model_validate(doc | {"media_prefix": None})
    with pytest.raises(ValidationError, match="POST policy on media_prefix"):
        RunSpec.model_validate(doc | {"media_prefix": doc["media_prefix"] + "x/"})
    put = doc["outputs"]["manifest"]
    with pytest.raises(ValidationError, match="POST policy on media_prefix"):
        RunSpec.model_validate(doc | {"outputs": {**doc["outputs"], "media": put}})
    with pytest.raises(ValidationError, match="must end with '/'"):
        RunSpec.model_validate(doc | {"media_prefix": doc["media_prefix"].rstrip("/")})


def _meta(name: str) -> ObjectMeta:
    return ObjectMeta(
        bucket="mapi-local-runs",
        name=name,
        generation=1,
        size=3,
        crc32c="4waSgw==",
        md5_hash="rL0Y20zC+Fzt72VPzMSk2A==",
        time_created=datetime.now(UTC),
    )


def test_verify_output_objects_accepts_media_objects() -> None:
    example = OutputManifest.model_json_schema()["examples"][0]
    manifest = OutputManifest.model_validate({**example, "objects": {}})
    prefix = manifest.prefix
    observed = [
        _meta(prefix + "manifest.json"),
        _meta(prefix + "media/" + SHA + ".png"),
        _meta(prefix + "checkpoints/seg-0.zarr.zip"),
    ]
    assert verify_output_objects(manifest, observed, expected_prefix=prefix).ok
    strict = verify_output_objects(
        manifest, observed, expected_prefix=prefix, allowed_extra_prefixes=("checkpoints/",)
    )
    assert not strict.ok and "media/" in strict.issues[0].message
