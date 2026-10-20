"""Run media (v0.14): the images and tables a run logs, their names, limits and checks.

A run logs media with ``ctx.log_image`` / ``log_figure`` / ``log_table`` (runner) or
``POST /v1/runs/{run_id}/media`` (API). Every writer and reader agrees on what lives here:

* :data:`MediaKind` / :data:`MediaContentType` and the maps between them
  (:data:`MEDIA_KINDS`, :data:`MEDIA_EXT`);
* :func:`media_name`, the content-addressed object name ``<sha256>.<ext>`` under a run's
  ``media/`` prefix;
* :func:`sniff_image`, a bounded pure-Python header reader for PNG and JPEG (no Pillow);
* :class:`MediaTable`, the JSON body of a table;
* :func:`check_media`, the one verification the supervisor, the server ingest and the API
  write all run on the bytes.

SVG and GIF are not accepted: SVG can carry script, and the web UI shows only PNG/JPEG.
Pure Python + Pydantic: no numpy, xarray or PIL.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    model_validator,
)

from mapi_spec._types import StrictModel
from mapi_spec.tracking import METRIC_KEY_RE

__all__ = [
    "MAX_API_MEDIA_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_SIDE",
    "MAX_MEDIA_CAPTION_CHARS",
    "MAX_TABLE_BYTES",
    "MAX_TABLE_CELLS",
    "MAX_TABLE_CELL_CHARS",
    "MAX_TABLE_COLUMNS",
    "MAX_TABLE_ROWS",
    "MEDIA_EXT",
    "MEDIA_KEY_RE",
    "MEDIA_KINDS",
    "MEDIA_NAME_RE",
    "ImageInfo",
    "MediaContentType",
    "MediaKind",
    "MediaTable",
    "check_media",
    "check_media_key",
    "media_name",
    "sniff_image",
]

MediaKind = Literal["image", "table"]
MediaContentType = Literal["image/png", "image/jpeg", "application/json"]

MEDIA_EXT: Final[dict[str, str]] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/json": ".json",
}
MEDIA_KINDS: Final[dict[str, MediaKind]] = {
    "image/png": "image",
    "image/jpeg": "image",
    "application/json": "table",
}
MEDIA_KEY_RE: Final = METRIC_KEY_RE
"""Media keys follow metric keys; qualifiers (split, class, ...) are labels."""
MEDIA_NAME_RE: Final = r"^[0-9a-f]{64}\.(png|jpg|json)$"
"""Object name of a media item under the run's ``media/`` prefix: ``<sha256 hex>.<ext>``."""

MAX_IMAGE_BYTES: Final = 8 * 1024 * 1024
MAX_TABLE_BYTES: Final = 1024 * 1024
MAX_API_MEDIA_BYTES: Final = 4 * 1024 * 1024
"""Decoded bytes one ``POST /v1/runs/{run_id}/media`` carries."""
MAX_IMAGE_SIDE: Final = 4096
MAX_MEDIA_CAPTION_CHARS: Final = 500
MAX_TABLE_ROWS: Final = 10_000
MAX_TABLE_COLUMNS: Final = 64
MAX_TABLE_CELLS: Final = 200_000
MAX_TABLE_CELL_CHARS: Final = 1000

_MEDIA_KEY = re.compile(MEDIA_KEY_RE)
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_JPEG_SCAN_BYTES: Final = 1024 * 1024
# SOF0..SOF15 carry the frame size; C4 (DHT), C8 (JPG extension) and CC (DAC) share the range.
_JPEG_SOF: Final = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Markers without a length field: TEM, RST0..RST7 (SOI/EOI end or start the stream).
_JPEG_STANDALONE: Final = frozenset({0x01, *range(0xD0, 0xD8)})


# ===================================================================================== sniffing


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """Format and size of an image, read from its header."""

    format: Literal["png", "jpeg"]
    width: int
    height: int


def _sniff_png(data: bytes) -> ImageInfo | None:
    # Signature (8) + IHDR chunk: length (4) + type (4) + width (4) + height (4) + ...
    if len(data) < 33 or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width < 1 or height < 1:
        return None
    return ImageInfo(format="png", width=width, height=height)


def _sniff_jpeg(data: bytes) -> ImageInfo | None:
    end = min(len(data), _JPEG_SCAN_BYTES)
    i = 2
    while i < end:
        if data[i] != 0xFF:
            return None
        # Fill bytes: any number of 0xFF before the marker code.
        while i < end and data[i] == 0xFF:
            i += 1
        if i >= end:
            return None
        marker = data[i]
        i += 1
        if marker in _JPEG_STANDALONE:
            continue
        if marker in (0x00, 0xD8, 0xD9, 0xDA):
            # Stuffed byte, a second SOI, EOI or the scan data: no frame header before it.
            return None
        if i + 2 > end:
            return None
        length = int.from_bytes(data[i : i + 2], "big")
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            # length (2) + precision (1) + height (2) + width (2)
            if length < 7 or i + 7 > len(data):
                return None
            height = int.from_bytes(data[i + 3 : i + 5], "big")
            width = int.from_bytes(data[i + 5 : i + 7], "big")
            if width < 1 or height < 1:
                return None
            return ImageInfo(format="jpeg", width=width, height=height)
        i += length
    return None


def sniff_image(data: bytes) -> ImageInfo | None:
    """Read the format and size of a PNG or JPEG from its header; ``None`` for anything else.

    PNG: the 8-byte signature and the IHDR chunk. JPEG: ``FF D8 FF``, then the markers
    (length-prefixed, in the first MiB only) up to the first frame header (SOF). GIF, SVG,
    WebP and truncated data give ``None``.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        return None
    raw = bytes(data[: _JPEG_SCAN_BYTES + 8])
    if raw.startswith(_PNG_SIGNATURE):
        return _sniff_png(raw)
    if raw.startswith(b"\xff\xd8\xff"):
        return _sniff_jpeg(raw)
    return None


# ======================================================================================= tables

TableCell = (
    StrictBool
    | StrictInt
    | StrictFloat
    | Annotated[StrictStr, Field(max_length=MAX_TABLE_CELL_CHARS)]
    | None
)


class MediaTable(StrictModel):
    """``application/json`` body of a table: columns + row-major cells (untrusted text)."""

    columns: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(
        ..., min_length=1, max_length=MAX_TABLE_COLUMNS
    )
    rows: list[list[TableCell]] = Field(default_factory=list, max_length=MAX_TABLE_ROWS)

    @model_validator(mode="after")
    def _shape(self) -> MediaTable:
        n = len(self.columns)
        if len(set(self.columns)) != n:
            raise ValueError("table column names must be unique")
        if len(self.rows) * n > MAX_TABLE_CELLS:
            raise ValueError(
                f"a table has at most {MAX_TABLE_CELLS:,} cells, got {len(self.rows) * n:,}"
            )
        for i, row in enumerate(self.rows):
            if len(row) != n:
                raise ValueError(f"table row {i} has {len(row)} cells; the table has {n} columns")
            for cell in row:
                if isinstance(cell, float) and not math.isfinite(cell):
                    raise ValueError(
                        f"table row {i} has a value that is not finite (write null instead)"
                    )
        return self


# ======================================================================================= checks


def check_media_key(key: str) -> str:
    """Return ``key`` if it is a valid media key, else raise :class:`ValueError`."""
    if not isinstance(key, str) or _MEDIA_KEY.match(key) is None:
        raise ValueError(
            f"media key {key!r} must match {MEDIA_KEY_RE} (use labels, not '/' or '.', "
            "for qualifiers such as the split or the class)"
        )
    return key


def media_name(sha256_hex: str, content_type: str) -> str:
    """The object name ``<sha256>.<ext>`` of a media item; :class:`ValueError` on bad input."""
    if not isinstance(sha256_hex, str) or _SHA256_HEX.match(sha256_hex) is None:
        raise ValueError(f"{sha256_hex!r} is not a lowercase hex sha256")
    ext = MEDIA_EXT.get(content_type)
    if ext is None:
        raise ValueError(
            f"media content type {content_type!r} is not one of {', '.join(MEDIA_EXT)}"
        )
    return sha256_hex + ext


def check_media(kind: str, content_type: str, data: bytes) -> ImageInfo | None:
    """Verify the bytes of a media item; return the :class:`ImageInfo` of an image.

    Raises :class:`ValueError` (the message names the problem) when the kind and the content
    type disagree, the size is 0 or above the limit of the kind, an image does not read as its
    content type or has a side above :data:`MAX_IMAGE_SIDE`, or a table is not UTF-8 JSON that
    validates as :class:`MediaTable`. Returns ``None`` for a valid table.
    """
    expected = MEDIA_KINDS.get(content_type)
    if expected is None:
        raise ValueError(
            f"media content type {content_type!r} is not one of {', '.join(MEDIA_KINDS)}"
        )
    if kind != expected:
        raise ValueError(f"media kind {kind!r} does not match content type {content_type!r}")
    size = len(data)
    cap = MAX_IMAGE_BYTES if kind == "image" else MAX_TABLE_BYTES
    if size == 0:
        raise ValueError(f"the {kind} is empty")
    if size > cap:
        raise ValueError(f"the {kind} has {size:,} bytes; the maximum is {cap:,}")
    if kind == "image":
        info = sniff_image(data)
        want = "png" if content_type == "image/png" else "jpeg"
        if info is None or info.format != want:
            raise ValueError(f"the bytes are not a {want.upper()} image")
        if info.width > MAX_IMAGE_SIDE or info.height > MAX_IMAGE_SIDE:
            raise ValueError(
                f"the image is {info.width}x{info.height} pixels; the maximum side is "
                f"{MAX_IMAGE_SIDE}"
            )
        return info
    try:
        doc: Any = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"the table is not UTF-8 JSON: {exc}") from None
    try:
        MediaTable.model_validate(doc)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = "/".join(str(p) for p in first.get("loc", ()))
        raise ValueError(
            f"the table is not valid ({where or 'table'}: {first.get('msg', 'invalid')})"
        ) from None
    return None
