"""Quantised rasters for the field viewer (Model Explorer ``fields``, Data Explorer
``images``): uint16 encoding, NaN-aware block means, even thinning and arrow grids; v0.14:
RGB rasters of colour pictures and their luminance."""

from __future__ import annotations

import base64
import math
import warnings
from typing import Any

import numpy as np
from mapi_spec.views import (
    FIELD_LEVELS,
    FIELD_NAN,
    MAX_FIELD_ARROWS,
    FieldArrows,
    FieldRaster,
    RgbRaster,
)

__all__ = [
    "LUMA",
    "arrows",
    "block_labels",
    "block_mean",
    "encode_raster",
    "encode_rgb",
    "even",
    "luminance",
    "to_uint8",
]

LUMA = (0.299, 0.587, 0.114)
"""ITU-R BT.601 weights of red, green and blue in the brightness of a colour."""


def _fv(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def encode_raster(values: np.ndarray, encoding: str = "linear") -> FieldRaster:
    """A 2-D array as a :class:`~mapi_spec.views.FieldRaster` (uint16 codes over [lo, hi])."""
    x = np.asarray(values, dtype=float)
    if encoding == "log10":
        pos = np.isfinite(x) & (x > 0)
        x = np.where(pos, np.log10(np.where(pos, x, 1.0)), np.nan)
    ok = np.isfinite(x)
    codes = np.full(x.shape, FIELD_NAN, dtype="<u2")
    lo = hi = None
    if ok.any():
        lo, hi = float(x[ok].min()), float(x[ok].max())
        span = hi - lo
        if span > 0 and math.isfinite(span):
            q = np.rint((x[ok] - lo) / span * FIELD_LEVELS)
            codes[ok] = np.clip(q, 0, FIELD_LEVELS).astype("<u2")
        else:
            codes[ok] = 0
    return FieldRaster(lo=lo, hi=hi, data=base64.b64encode(codes.tobytes()).decode("ascii"))


def block_mean(a: np.ndarray, fy: int, fx: int) -> np.ndarray:
    """Mean over ``fy`` x ``fx`` blocks of the last two axes (NaN-aware; edge blocks are
    partial)."""
    if fy <= 1 and fx <= 1:
        return np.asarray(a, dtype=float)
    h, w = a.shape[-2:]
    ny, nx = -(-h // fy), -(-w // fx)
    pad = [(0, 0)] * (a.ndim - 2) + [(0, ny * fy - h), (0, nx * fx - w)]
    p = np.pad(np.asarray(a, dtype=float), pad, constant_values=np.nan)
    p = p.reshape(*a.shape[:-2], ny, fy, nx, fx)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.asarray(np.nanmean(p, axis=(-3, -1)))


def block_labels(labels: list[Any], f: int) -> list[Any]:
    """The label at the middle of each block of ``f`` labels."""
    n = len(labels)
    return [labels[min(i + f // 2, n - 1)] for i in range(0, n, f)]


def even(n: int, k: int) -> list[int]:
    """At most ``k`` evenly spaced indices of ``range(n)`` (first and last kept)."""
    if n <= k:
        return list(range(n))
    return sorted({int(i) for i in np.linspace(0, n - 1, k).round()})


def arrows(mean: np.ndarray) -> FieldArrows:
    """Arrows of the mean components ``(2, h, w)`` on a grid of at most MAX_FIELD_ARROWS."""
    h, w = mean.shape[-2:]
    a = max(1, -(-max(h, w) // MAX_FIELD_ARROWS))
    coarse = block_mean(mean, a, a)

    def centres(n: int) -> list[float]:
        return [(i + min(i + a, n) - 1) / 2 for i in range(0, n, a)]

    return FieldArrows(
        x=centres(w),
        y=centres(h),
        u=[[_fv(v) for v in row] for row in coarse[0]],
        v=[[_fv(v) for v in row] for row in coarse[1]],
    )


def luminance(values: np.ndarray) -> np.ndarray:
    """The brightness ``0.299 r + 0.587 g + 0.114 b`` of RGB values ``(..., 3)`` (float32;
    NaN where a channel is missing)."""
    x = np.asarray(values, dtype=np.float32)
    return np.asarray(x[..., 0] * LUMA[0] + x[..., 1] * LUMA[1] + x[..., 2] * LUMA[2])


def to_uint8(values: np.ndarray, lo: float = 0.0, hi: float = 255.0) -> np.ndarray:
    """Values mapped from ``[lo, hi]`` onto 0..255 (clipped and rounded; NaN -> 0). The
    default range keeps 8-bit pixels as they are."""
    x = np.asarray(values, dtype=np.float64)
    span = hi - lo
    if span > 0 and math.isfinite(span):
        x = (x - lo) * (255.0 / span)
    else:
        x = np.where(np.isfinite(x), 0.0, x)
    return np.asarray(np.clip(np.nan_to_num(np.rint(x), nan=0.0), 0, 255), dtype=np.uint8)


def encode_rgb(values: np.ndarray, lo: float = 0.0, hi: float = 255.0) -> RgbRaster:
    """RGB values ``(height, width, 3)`` as a :class:`~mapi_spec.views.RgbRaster` (3 bytes a
    cell, row-major), mapped from ``[lo, hi]`` (default: 8-bit values as they are)."""
    x = np.asarray(values)
    if x.ndim != 3 or x.shape[-1] != 3:
        raise ValueError(f"an rgb raster needs (height, width, 3) values, not {x.shape}")
    data = to_uint8(x, lo, hi) if x.dtype != np.uint8 else x
    return RgbRaster(data=base64.b64encode(np.ascontiguousarray(data).tobytes()).decode("ascii"))
