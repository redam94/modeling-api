"""Field maps of a layout 2 readout (v0.17, I-6): a run's ``ui/fields.json`` as a capped
``field`` block.

A score run of a model with a ``grid`` output writes :class:`~mapi_spec.views.FieldsView`:
uint16 rasters of up to 256 x 256 cells per frame. A client page draws a field block of at
most :data:`~mapi_spec.dashboard_v2.MAX_FIELD_SIDE` x ``MAX_FIELD_SIDE`` uint8 codes and
:data:`~mapi_spec.dashboard_v2.MAX_FIELD_FRAMES` frames, so :func:`field_block`

1. decodes the posterior-mean raster of each frame (stdlib ``base64``/``array`` through
   :func:`mapi_spec.views.decode_raster`; no numpy);
2. block-averages it to at most 64 x 64 cells (the mean of the present cells of each
   block; a block with none stays missing) and keeps at most 8 frames (even thinning,
   first and last kept);
3. re-quantises every frame on one shared range to uint8 codes: code ``c`` (0 … 254) is
   ``vmin + c / 254 * (vmax - vmin)``. When the entry draws ``log10`` of the value, the
   range and the codes are on ``log10`` (``vmin``/``vmax`` are then ``log10`` values).
   A missing cell (or a value <= 0 on a log map) gets the block's ``missing_code`` (255):
   it is drawn transparent and left out of the statistics, and the frame keeps the shape.

Row 0 of a block is the top row: a raster with ``origin == "lower"`` is flipped.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Sequence

from mapi_spec.dashboard_v2 import (
    FIELD_MISSING_CODE,
    FIELD_VALUE_CODES,
    MAX_FIELD_FRAMES,
    MAX_FIELD_SIDE,
    FieldBlock,
    FieldFrame,
)
from mapi_spec.views import FieldEntry, FieldsView, decode_raster

__all__ = [
    "block_mean",
    "field_block",
    "field_entry",
    "pick_frames",
    "quantise",
]


def field_entry(
    view: FieldsView | None, output: str, select: dict[str, str | int] | None = None
) -> FieldEntry | None:
    """The entry of ``output`` (the first whose facet agrees with ``select``)."""
    if view is None:
        return None
    sel = {k: str(v) for k, v in (select or {}).items()}
    for entry in view.fields:
        if entry.output != output:
            continue
        facet = {k: str(v) for k, v in entry.facet.items()}
        if all(facet.get(k, v) == v for k, v in sel.items()):
            return entry
    return None


def block_mean(
    values: Sequence[float | None], height: int, width: int, side: int = MAX_FIELD_SIDE
) -> tuple[list[float | None], int, int]:
    """``values`` (row-major ``height`` x ``width``) averaged over blocks so that neither side
    is above ``side``. Returns the cells and the new height and width."""
    if len(values) != height * width:
        raise ValueError("the raster does not hold height x width cells")
    fy = max(1, math.ceil(height / side))
    fx = max(1, math.ceil(width / side))
    if fy == 1 and fx == 1:
        return list(values), height, width
    out_h, out_w = math.ceil(height / fy), math.ceil(width / fx)
    out: list[float | None] = []
    for by in range(out_h):
        for bx in range(out_w):
            total, n = 0.0, 0
            for y in range(by * fy, min(height, (by + 1) * fy)):
                row = y * width
                for x in range(bx * fx, min(width, (bx + 1) * fx)):
                    v = values[row + x]
                    if v is not None and math.isfinite(v):
                        total += v
                        n += 1
            out.append(total / n if n else None)
    return out, out_h, out_w


def pick_frames(n: int, keep: int = MAX_FIELD_FRAMES) -> list[int]:
    """Indexes of at most ``keep`` frames out of ``n``, evenly spaced (first and last kept)."""
    if n <= keep:
        return list(range(n))
    if keep == 1:
        return [n - 1]
    return sorted({round(i * (n - 1) / (keep - 1)) for i in range(keep)})


def quantise(values: Sequence[float | None], vmin: float, vmax: float) -> bytes:
    """uint8 codes of ``values`` on ``[vmin, vmax]``: ``0`` … ``254`` for values, and
    :data:`~mapi_spec.dashboard_v2.FIELD_MISSING_CODE` (255) for a missing cell (masked,
    non-finite, or not above zero on a log map), so it never reads as the minimum."""
    span = vmax - vmin
    top = FIELD_VALUE_CODES
    out = bytearray([FIELD_MISSING_CODE]) * len(values)
    for i, v in enumerate(values):
        if v is None or not math.isfinite(v):
            continue
        c = 0 if span <= 0 else round((v - vmin) / span * top)
        out[i] = min(top, max(0, c))
    return bytes(out)


def _on_scale(values: Sequence[float | None], log: bool) -> list[float | None]:
    if not log:
        return [v if v is not None and math.isfinite(v) else None for v in values]
    return [math.log10(v) if v is not None and math.isfinite(v) and v > 0 else None for v in values]


def _flip(values: list[float | None], height: int, width: int) -> list[float | None]:
    rows = [values[y * width : (y + 1) * width] for y in range(height)]
    return [v for row in reversed(rows) for v in row]


def _text(v: object, limit: int = 64) -> str | None:
    if v is None:
        return None
    s = "".join(ch for ch in str(v) if ch.isprintable() and ch not in "<>&")
    return s[:limit] or None


def field_block(
    entry: FieldEntry,
    *,
    panel_id: str,
    title: str,
    caption: str | None = None,
) -> FieldBlock:
    """The ``field`` block of one entry (raises ``ValueError`` when no frame has a value)."""
    idx = pick_frames(len(entry.frames))
    log = entry.log or entry.encoding == "log10"
    frames: list[tuple[str, list[float | None]]] = []
    height = width = 0
    for i in idx:
        frame = entry.frames[i]
        values = decode_raster(frame.mean, entry.encoding)
        if entry.origin == "lower":
            values = _flip(values, entry.height, entry.width)
        cells, height, width = block_mean(values, entry.height, entry.width)
        label = _text(frame.label) or f"Frame {i + 1}"
        frames.append((label, _on_scale(cells, log)))
    present = [v for _, cells in frames for v in cells if v is not None]
    if not present:
        raise ValueError(f"the field {entry.output!r} has no values")
    vmin, vmax = min(present), max(present)
    return FieldBlock(
        panel_id=panel_id,
        title=title[:80],
        caption=caption[:200] if caption else None,
        width=width,
        height=height,
        frames=[
            FieldFrame(
                label=label[:64],
                codes_b64=base64.b64encode(quantise(cells, vmin, vmax)).decode("ascii"),
            )
            for label, cells in frames
        ],
        vmin=vmin,
        vmax=vmax,
        missing_code=FIELD_MISSING_CODE,
        log=log,
        colormap=entry.colormap,
        units=_text(entry.units),
        x_label=_text(entry.x_dim),
        y_label=_text(entry.y_dim),
    )
