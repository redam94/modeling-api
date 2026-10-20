"""Images the trainer logs (``ctx.log_image``): validation predictions and the confusion matrix.

Both return uint8 RGB arrays; :func:`mapi_sdk.media.encode_png` (through ``log_image``)
encodes them. Labels are drawn with Pillow's built-in bitmap font (no font files), truncated
to the tile width.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from mapi_spec.media import MAX_IMAGE_SIDE

__all__ = ["MAX_GRID_IMAGES", "confusion_image", "prediction_grid", "thumbnail"]

MAX_GRID_IMAGES = 16
_COLUMNS = 4
_TILE = 64
_TEXT = 12
_BORDER = 2
_GOOD = (40, 160, 70)
_BAD = (210, 50, 50)


def thumbnail(px: np.ndarray, side: int) -> np.ndarray:
    """``(H, W, C)`` uint8 scaled by nearest neighbour so the longer side is at most ``side``
    (integer up-scaling for small images)."""
    h, w = px.shape[:2]
    scale = side / max(h, w)
    nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
    ys = np.minimum((np.arange(nh) / scale).astype(int), h - 1)
    xs = np.minimum((np.arange(nw) / scale).astype(int), w - 1)
    return px[ys][:, xs]


def _rgb(px: np.ndarray) -> np.ndarray:
    if px.ndim == 2:
        px = px[..., None]
    if px.shape[-1] == 1:
        px = np.repeat(px, 3, axis=-1)
    return np.asarray(px[..., :3], dtype=np.uint8)


def prediction_grid(
    pixels: np.ndarray, true: Sequence[str], pred: Sequence[str], *, tile: int = _TILE
) -> np.ndarray:
    """A grid of at most 16 images with a green (right) or red (wrong) frame and the text
    ``pred / true`` under each one."""
    from PIL import Image, ImageDraw

    n = min(len(pixels), MAX_GRID_IMAGES)
    cols = min(_COLUMNS, max(n, 1))
    rows = max(1, -(-n // cols))
    cell_w, cell_h = tile + 2 * _BORDER, tile + 2 * _BORDER + _TEXT
    canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    for i in range(n):
        r, c = divmod(i, cols)
        x0, y0 = c * cell_w, r * cell_h
        colour = _GOOD if pred[i] == true[i] else _BAD
        draw.rectangle([x0, y0, x0 + cell_w - 1, y0 + tile + 2 * _BORDER - 1], fill=colour)
        thumb = _rgb(thumbnail(np.asarray(pixels[i]), tile))
        th, tw = thumb.shape[:2]
        canvas.paste(
            Image.fromarray(thumb, "RGB"),
            (x0 + _BORDER + (tile - tw) // 2, y0 + _BORDER + (tile - th) // 2),
        )
        text = f"{pred[i]} / {true[i]}"
        max_chars = max(1, cell_w // 6)
        draw.text((x0 + 1, y0 + tile + 2 * _BORDER), text[:max_chars], fill=(20, 20, 20))
    out = np.asarray(canvas, dtype=np.uint8)
    return out[:MAX_IMAGE_SIDE, :MAX_IMAGE_SIDE]


def confusion_image(matrix: np.ndarray) -> np.ndarray:
    """A heatmap of a ``(K, K)`` count matrix (rows = true class, columns = predicted),
    each row scaled to its total: white = 0, dark blue = all of the row."""
    k = int(matrix.shape[0])
    cell = max(4, min(24, 480 // max(k, 1)))
    totals = matrix.sum(axis=1, keepdims=True).astype(float)
    share = np.divide(matrix, totals, out=np.zeros(matrix.shape, dtype=float), where=totals > 0)
    lo, hi = np.array([255.0, 255.0, 255.0]), np.array([8.0, 48.0, 107.0])
    rgb = lo + share[..., None] * (hi - lo)
    img = np.repeat(np.repeat(rgb, cell, axis=0), cell, axis=1)
    # 1 px grid lines between cells
    img[::cell, :, :] = 200
    img[:, ::cell, :] = 200
    return np.clip(img, 0, 255).astype(np.uint8)[:MAX_IMAGE_SIDE, :MAX_IMAGE_SIDE]
