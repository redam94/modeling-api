"""Run media (v0.14): images, matplotlib figures and small tables a run logs.

The helpers here turn what model code has at hand into a :class:`MediaItem`, the checked,
encoded payload a :class:`~mapi_sdk.tracking.MediaTracker` receives:

* :func:`image_item` - a numpy array (encoded with :func:`encode_png`), PNG/JPEG bytes, a
  Pillow image or an xarray ``DataArray`` with dims ``(y, x[, channel])``;
* :func:`figure_item` - a matplotlib figure rendered to PNG (never SVG: SVG can hold script);
* :func:`table_item` - a pandas ``DataFrame``, a mapping of columns or a list of records as a
  :class:`~mapi_spec.media.MediaTable` JSON document.

Every helper validates the key, labels, step and caption and runs
:func:`mapi_spec.media.check_media` on the encoded bytes, so a bad call raises
:class:`ValueError` where it was written. :func:`encode_png` needs only numpy and zlib and
writes the same bytes for the same pixels; Pillow, matplotlib and pandas are never imported
here (objects are recognised by their methods).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import math
import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, cast

import numpy as np
from mapi_spec.media import (
    MAX_IMAGE_SIDE,
    MAX_MEDIA_CAPTION_CHARS,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    MediaContentType,
    MediaKind,
    check_media,
    check_media_key,
    media_name,
    sniff_image,
)
from mapi_spec.tracking import check_labels

__all__ = [
    "MediaItem",
    "encode_png",
    "figure_item",
    "image_item",
    "table_item",
]

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_COLOR_TYPE: Final = {1: 0, 3: 2, 4: 6}  # channels -> PNG colour type (gray, RGB, RGBA)
_ZLIB_LEVEL: Final = 6
_CONTENT_TYPE: Final[dict[str, MediaContentType]] = {"png": "image/png", "jpeg": "image/jpeg"}


@dataclass(frozen=True, slots=True)
class MediaItem:
    """One encoded image or table, checked against the :mod:`mapi_spec.media` limits."""

    key: str
    kind: MediaKind
    content_type: MediaContentType
    data: bytes
    sha256: str
    """Hex SHA-256 of :attr:`data`."""
    step: int | None = None
    labels: dict[str, str] = field(default_factory=dict)
    caption: str | None = None
    width: int | None = None
    height: int | None = None

    @property
    def name(self) -> str:
        """``<sha256>.<ext>``: the object name under the run's media prefix."""
        return media_name(self.sha256, self.content_type)

    @property
    def size(self) -> int:
        return len(self.data)


# --------------------------------------------------------------------------- validation


def check_media_args(
    key: str,
    step: int | None,
    labels: Mapping[str, str] | None,
    caption: str | None,
) -> tuple[str, int | None, dict[str, str], str | None]:
    """Validate the metadata of a media call; returns ``(key, step, labels, caption)``.

    Raises :class:`ValueError` (the tracking mixin calls it even without a tracker).
    """
    key = check_media_key(key)
    if step is not None:
        if isinstance(step, bool) or not isinstance(step, int | np.integer):
            raise ValueError(f"media step must be an int >= 0 or None, got {step!r}")
        if step < 0:
            raise ValueError(f"media step must be >= 0, got {step}")
        step = int(step)
    lab = check_labels(labels)
    if caption is not None:
        if not isinstance(caption, str):
            raise ValueError(f"media caption must be a str, got {type(caption).__name__}")
        if len(caption) > MAX_MEDIA_CAPTION_CHARS:
            raise ValueError(
                f"media caption has {len(caption)} characters (at most {MAX_MEDIA_CAPTION_CHARS})"
            )
    return key, step, lab, caption


def _item(
    key: str,
    content_type: MediaContentType,
    data: bytes,
    *,
    step: int | None,
    labels: Mapping[str, str] | None,
    caption: str | None,
) -> MediaItem:
    key, step, lab, caption = check_media_args(key, step, labels, caption)
    kind: MediaKind = "table" if content_type == "application/json" else "image"
    try:
        info = check_media(kind, content_type, data)
    except ValueError as exc:
        raise ValueError(f"media {key!r}: {exc}") from None
    return MediaItem(
        key=key,
        kind=kind,
        content_type=content_type,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        step=step,
        labels=lab,
        caption=caption,
        width=None if info is None else info.width,
        height=None if info is None else info.height,
    )


# --------------------------------------------------------------------------------- PNG


def _pixels(array: Any) -> np.ndarray:
    """``array`` as a C-contiguous ``(H, W, C)`` uint8 array (C in 1, 3, 4)."""
    arr = np.asarray(array)
    if arr.ndim == 2:
        arr = arr[:, :, None]
    if arr.ndim != 3 or arr.shape[2] not in _COLOR_TYPE:
        raise ValueError(
            f"an image array must have shape (H, W), (H, W, 1), (H, W, 3) or (H, W, 4), "
            f"got {arr.shape}"
        )
    h, w = arr.shape[:2]
    if not (1 <= h <= MAX_IMAGE_SIDE and 1 <= w <= MAX_IMAGE_SIDE):
        raise ValueError(f"an image side must be 1..{MAX_IMAGE_SIDE} pixels, got {h} x {w}")
    kind = arr.dtype.kind
    if kind == "u" and arr.dtype.itemsize == 1:
        out = arr
    elif kind == "b":
        out = arr.astype(np.uint8) * np.uint8(255)
    elif kind in "iu":
        lo, hi = int(arr.min()), int(arr.max())
        if lo < 0 or hi > 255:
            raise ValueError(
                f"integer pixel values must lie in 0..255, got {lo}..{hi} "
                "(scale them, or pass floats in [0, 1])"
            )
        out = arr.astype(np.uint8)
    elif kind == "f":
        x = np.nan_to_num(arr.astype(np.float64), nan=0.0, posinf=np.inf, neginf=-np.inf)
        if not np.isfinite(x).all() or (x.size and (x.min() < 0.0 or x.max() > 1.0)):
            raise ValueError(
                "float pixel values must lie in [0, 1] (NaN is drawn as 0); "
                "scale them or pass uint8 values"
            )
        out = np.clip(np.rint(x * 255.0), 0, 255).astype(np.uint8)
    else:
        raise ValueError(f"an image array must hold numbers or bools, got dtype {arr.dtype}")
    return np.ascontiguousarray(out)


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def encode_png(array: Any) -> tuple[bytes, int, int]:
    """Encode an image array as an 8-bit PNG; returns ``(png, width, height)``.

    Shapes: ``(H, W)`` or ``(H, W, 1)`` gray, ``(H, W, 3)`` RGB, ``(H, W, 4)`` RGBA. dtypes:
    uint8 as is; bool as 0/255; other integers must lie in 0..255; floats must lie in
    ``[0, 1]`` (NaN is 0) and are scaled by 255. Each side is 1..``MAX_IMAGE_SIDE``. Filter
    type 0 on every row and zlib level 6, so the same pixels always give the same bytes.
    """
    px = _pixels(array)
    h, w, c = px.shape
    raw = np.zeros((h, 1 + w * c), dtype=np.uint8)  # leading 0: filter type "none"
    raw[:, 1:] = px.reshape(h, w * c)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, _COLOR_TYPE[c], 0, 0, 0)
    png = (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw.tobytes(), _ZLIB_LEVEL))
        + _chunk(b"IEND", b"")
    )
    return png, w, h


# ------------------------------------------------------------------------------ images


def _is_pil_image(obj: Any) -> bool:
    return callable(getattr(obj, "save", None)) and isinstance(getattr(obj, "mode", None), str)


def _pil_png(image: Any) -> bytes:
    if image.mode not in ("1", "L", "LA", "P", "RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.mode else "RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def image_item(
    key: str,
    image: Any,
    *,
    step: int | None = None,
    labels: Mapping[str, str] | None = None,
    caption: str | None = None,
) -> MediaItem:
    """A checked image item from an array, PNG/JPEG bytes, a Pillow image or a DataArray.

    Arrays (and the values of a ``DataArray`` with dims ``(y, x[, channel])``) are encoded
    with :func:`encode_png`. Bytes must be a PNG or a JPEG; they are sent as they are.
    """
    check_media_args(key, step, labels, caption)  # fail before any encoding
    content_type: MediaContentType
    if isinstance(image, bytes | bytearray | memoryview):
        data = bytes(image)
        info = sniff_image(data)
        if info is None:
            raise ValueError(f"media {key!r}: the bytes are not a PNG or a JPEG image")
        content_type = _CONTENT_TYPE[info.format]
    elif _is_pil_image(image):
        data, content_type = _pil_png(image), "image/png"
    else:
        values = image.values if hasattr(image, "dims") and hasattr(image, "values") else image
        arr = np.asarray(values)
        if arr.dtype == object:
            raise ValueError(
                f"media {key!r}: log_image takes a numpy array, PNG/JPEG bytes, a Pillow "
                f"image or a DataArray, got {type(image).__name__}"
            )
        data, _, _ = encode_png(arr)
        content_type = "image/png"
    return _item(key, content_type, data, step=step, labels=labels, caption=caption)


def figure_item(
    key: str,
    figure: Any,
    *,
    step: int | None = None,
    labels: Mapping[str, str] | None = None,
    caption: str | None = None,
    dpi: int = 100,
) -> MediaItem:
    """A checked PNG item rendered from a matplotlib figure (or anything with ``savefig``)."""
    check_media_args(key, step, labels, caption)
    if not callable(getattr(figure, "savefig", None)):
        get_figure = getattr(figure, "get_figure", None)  # an Axes
        figure = get_figure() if callable(get_figure) else None
    if figure is None or not callable(getattr(figure, "savefig", None)):
        raise ValueError(
            f"media {key!r}: log_figure takes a matplotlib figure; use log_image for arrays "
            "and images"
        )
    buf = io.BytesIO()
    figure.savefig(buf, format="png", dpi=dpi)
    return _item(key, "image/png", buf.getvalue(), step=step, labels=labels, caption=caption)


# ------------------------------------------------------------------------------ tables


def _cell(value: Any) -> Any:
    """A JSON-safe table cell: Python scalars, ISO text for datetimes, None for NaN/NaT."""
    if value is None:
        return None
    if isinstance(value, np.datetime64 | np.timedelta64):
        return None if np.isnat(value) else str(value)
    if isinstance(value, np.generic):
        value = value.item()
    if type(value).__name__ == "NaTType":  # pandas.NaT (a datetime subclass)
        return None
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, _dt.datetime | _dt.date | _dt.time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _check_shape(n_rows: int, n_cols: int) -> None:
    if n_cols < 1:
        raise ValueError("a table needs at least one column")
    if n_cols > MAX_TABLE_COLUMNS:
        raise ValueError(f"a table has at most {MAX_TABLE_COLUMNS} columns, got {n_cols}")
    if n_rows > MAX_TABLE_ROWS:
        raise ValueError(f"a table has at most {MAX_TABLE_ROWS} rows, got {n_rows}")


def _table_doc(table: Any) -> tuple[list[str], list[list[Any]]]:
    if hasattr(table, "columns") and hasattr(table, "iloc") and hasattr(table, "index"):
        df = table  # pandas DataFrame: the index is kept only when it has a name
        names = getattr(df.index, "names", None) or [getattr(df.index, "name", None)]
        if any(n is not None for n in names):
            df = df.reset_index()
        _check_shape(len(df), len(df.columns))
        columns = [str(c) for c in df.columns]
        cols = [[_cell(v) for v in df.iloc[:, i].tolist()] for i in range(len(columns))]
        return columns, [list(r) for r in zip(*cols, strict=True)]
    if isinstance(table, Mapping):
        columns = [str(c) for c in table]
        values = [list(v) for v in table.values()]
        lengths = {len(v) for v in values}
        if len(lengths) > 1:
            raise ValueError(f"every column of a table must have the same length, got {lengths}")
        _check_shape(lengths.pop() if lengths else 0, len(columns))
        return columns, [[_cell(v) for v in row] for row in zip(*values, strict=True)]
    if isinstance(table, Sequence) and not isinstance(table, str | bytes):
        records = list(table)
        if not all(isinstance(r, Mapping) for r in records):
            raise ValueError("a list table must hold mappings (one record per row)")
        if len(records) > MAX_TABLE_ROWS:
            _check_shape(len(records), 1)
        seen: dict[str, None] = {}  # union of the record keys, first-seen order
        for r in cast(list[Mapping[Any, Any]], records):
            seen.update(dict.fromkeys(str(k) for k in r))
        columns = list(seen)
        _check_shape(len(records), len(columns))
        rows = [
            [_cell(r.get(c)) for c in columns]
            for r in ({str(k): v for k, v in rec.items()} for rec in records)
        ]
        return columns, rows
    raise ValueError(
        "log_table takes a pandas DataFrame, a mapping of columns or a list of records, "
        f"got {type(table).__name__}"
    )


def table_item(
    key: str,
    table: Any,
    *,
    step: int | None = None,
    labels: Mapping[str, str] | None = None,
    caption: str | None = None,
) -> MediaItem:
    """A checked table item (``application/json`` :class:`~mapi_spec.media.MediaTable`).

    numpy scalars become Python values, NaN and infinities become ``None`` and datetimes
    become ISO text. A ``DataFrame`` index is dropped unless it has a name.
    """
    check_media_args(key, step, labels, caption)
    try:
        columns, rows = _table_doc(table)
    except ValueError as exc:
        raise ValueError(f"media {key!r}: {exc}") from None
    data = json.dumps(
        {"columns": columns, "rows": rows},
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return _item(key, "application/json", data, step=step, labels=labels, caption=caption)
