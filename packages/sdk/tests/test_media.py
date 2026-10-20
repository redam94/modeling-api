"""Run media in the SDK (v0.14): ``encode_png``, the item builders and their refusals."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import struct
import zlib
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.media import MediaItem, encode_png, figure_item, image_item, table_item
from mapi_spec.media import MAX_TABLE_ROWS, MediaTable, media_name, sniff_image


def _decode(png: bytes) -> tuple[int, int, int, np.ndarray]:
    """(width, height, colour type, pixels) of an 8-bit, filter-0 PNG written by encode_png."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, []
    while pos < len(png):
        (n,) = struct.unpack(">I", png[pos : pos + 4])
        tag, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + n]
        (crc,) = struct.unpack(">I", png[pos + 8 + n : pos + 12 + n])
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF
        chunks.append((tag, data))
        pos += 12 + n
    assert [t for t, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    w, h, depth, color, *_ = struct.unpack(">IIBBBBB", chunks[0][1])
    assert depth == 8
    channels = {0: 1, 2: 3, 6: 4}[color]
    raw = np.frombuffer(zlib.decompress(chunks[1][1]), np.uint8).reshape(h, 1 + w * channels)
    assert (raw[:, 0] == 0).all()  # filter type 0 on every row
    return w, h, color, raw[:, 1:].reshape(h, w, channels)


# ============================================================================ encode_png


@pytest.mark.parametrize(
    ("shape", "color"),
    [((5, 7), 0), ((5, 7, 1), 0), ((5, 7, 3), 2), ((5, 7, 4), 6)],
)
def test_encode_png_round_trips(shape: tuple[int, ...], color: int) -> None:
    px = np.random.default_rng(1).integers(0, 256, shape, dtype=np.uint8)
    png, w, h = encode_png(px)
    assert (w, h) == (7, 5)
    dw, dh, dcolor, back = _decode(png)
    assert (dw, dh, dcolor) == (7, 5, color)
    np.testing.assert_array_equal(back, px.reshape(5, 7, -1))
    assert encode_png(px)[0] == png  # deterministic
    info = sniff_image(png)
    assert info is not None and (info.format, info.width, info.height) == ("png", 7, 5)


def test_encode_png_scales_floats_bools_and_ints() -> None:
    f = np.array([[0.0, 0.5, 1.0, np.nan]])
    np.testing.assert_array_equal(_decode(encode_png(f)[0])[3][..., 0], [[0, 128, 255, 0]])
    b = np.array([[True, False]])
    np.testing.assert_array_equal(_decode(encode_png(b)[0])[3][..., 0], [[255, 0]])
    i = np.array([[0, 17, 255]], dtype=np.int64)
    np.testing.assert_array_equal(_decode(encode_png(i)[0])[3][..., 0], [[0, 17, 255]])


@pytest.mark.parametrize(
    ("array", "match"),
    [
        (np.zeros((2, 2, 2)), "shape"),
        (np.zeros(4), "shape"),
        (np.zeros((0, 3)), "side"),
        (np.zeros((4097, 1)), "side"),
        (np.array([[-1, 3]]), "0..255"),
        (np.array([[256]]), "0..255"),
        (np.array([[1.5]]), r"\[0, 1\]"),
        (np.array([[np.inf]]), r"\[0, 1\]"),
        (np.array([["a"]]), "numbers or bools"),
    ],
)
def test_encode_png_refusals(array: np.ndarray, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        encode_png(array)


# ============================================================================ image_item


def test_image_item_from_array_bytes_and_dataarray() -> None:
    px = np.full((3, 4, 3), 9, np.uint8)
    item = image_item("samples", px, step=2, labels={"split": "test"}, caption="hi")
    assert isinstance(item, MediaItem)
    assert (item.kind, item.content_type, item.width, item.height) == ("image", "image/png", 4, 3)
    assert item.sha256 == hashlib.sha256(item.data).hexdigest()
    assert item.name == media_name(item.sha256, "image/png")
    assert (item.step, item.labels, item.caption) == (2, {"split": "test"}, "hi")
    same = image_item("samples", item.data)
    assert same.data == item.data and same.width == 4
    da = xr.DataArray(px, dims=("y", "x", "channel"))
    assert image_item("samples", da).data == item.data
    jpeg = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x02\x00\x03\x03" + b"\x00" * 9 + b"\xff\xd9"
    j = image_item("photo", jpeg)
    assert (j.content_type, j.width, j.height) == ("image/jpeg", 3, 2)


def test_image_item_from_a_pillow_image() -> None:
    image_mod = pytest.importorskip("PIL.Image")
    px = np.random.default_rng(2).integers(0, 256, (6, 5, 3), dtype=np.uint8)
    item = image_item("samples", image_mod.fromarray(px))
    assert (item.width, item.height) == (5, 6)
    back = np.asarray(image_mod.open(io.BytesIO(item.data)))
    np.testing.assert_array_equal(back, px)
    # and Pillow reads what encode_png writes
    np.testing.assert_array_equal(np.asarray(image_mod.open(io.BytesIO(encode_png(px)[0]))), px)


def test_image_item_refusals() -> None:
    with pytest.raises(ValueError, match="not a PNG or a JPEG"):
        image_item("samples", b"GIF89a....")
    with pytest.raises(ValueError, match="media key"):
        image_item("bad/key", np.zeros((2, 2)))
    with pytest.raises(ValueError, match="step must be >= 0"):
        image_item("samples", np.zeros((2, 2)), step=-1)
    with pytest.raises(ValueError, match="label"):
        image_item("samples", np.zeros((2, 2)), labels={"split": "a b"})
    with pytest.raises(ValueError, match="caption"):
        image_item("samples", np.zeros((2, 2)), caption="x" * 501)
    with pytest.raises(ValueError, match="log_image takes"):
        image_item("samples", object())


# =========================================================================== figure_item


def test_figure_item_renders_a_png() -> None:
    mpl = pytest.importorskip("matplotlib")
    mpl.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(2, 1))
    ax.plot([0, 1], [1, 0])
    try:
        item = figure_item("curve", fig, dpi=50)
        assert (item.content_type, item.width, item.height) == ("image/png", 100, 50)
        assert figure_item("curve", ax, dpi=50).width == 100  # an Axes gives its figure
    finally:
        plt.close(fig)


def test_figure_item_refuses_arrays() -> None:
    with pytest.raises(ValueError, match="use log_image"):
        figure_item("curve", np.zeros((2, 2)))


# ============================================================================ table_item


def _table(item: MediaItem) -> MediaTable:
    assert (item.kind, item.content_type, item.width) == ("table", "application/json", None)
    return MediaTable.model_validate_json(item.data)


def test_table_item_from_a_mapping_and_records() -> None:
    t = _table(
        table_item(
            "scores",
            {
                "a": [np.int64(1), 2.5, float("nan")],
                "b": ["x", None, dt.date(2026, 1, 2)],
                "c": [np.float32(np.inf), np.bool_(True), np.datetime64("2026-01-02T03:04")],
            },
        )
    )
    assert t.columns == ["a", "b", "c"]
    assert t.rows == [[1, "x", None], [2.5, None, True], [None, "2026-01-02", "2026-01-02T03:04"]]
    r = _table(table_item("scores", [{"k": 1}, {"k": 2, "extra": "e"}]))
    assert r.columns == ["k", "extra"]
    assert r.rows == [[1, None], [2, "e"]]


def test_table_item_from_a_dataframe() -> None:
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame(
        {"x": [1, 2], "y": [0.5, np.nan], "when": pd.to_datetime(["2026-01-01", None])}
    )
    t = _table(table_item("scores", df))
    assert t.columns == ["x", "y", "when"]  # an unnamed index is dropped
    assert t.rows[1] == [2, None, None]
    assert str(t.rows[0][2]).startswith("2026-01-01")
    named = _table(table_item("scores", df.rename_axis("row")))
    assert named.columns[0] == "row" and named.rows[1][0] == 1


def test_table_item_bytes_are_compact_json() -> None:
    item = table_item("scores", {"a": [1]})
    assert json.loads(item.data) == {"columns": ["a"], "rows": [[1]]}
    assert b" " not in item.data


@pytest.mark.parametrize(
    ("table", "match"),
    [
        ({"a": [1, 2], "b": [1]}, "same length"),
        ({}, "at least one column"),
        ({f"c{i}": [1] for i in range(65)}, "at most 64 columns"),
        ({"a": list(range(MAX_TABLE_ROWS + 1))}, "at most 10000 rows"),
        ([1, 2], "mappings"),
        ("text", "log_table takes"),
        ({"a": ["x" * 1001]}, "not valid|1000"),
    ],
)
def test_table_item_refusals(table: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        table_item("scores", table)
