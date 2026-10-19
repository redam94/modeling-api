"""Quantised rasters for the field viewer (Model Explorer ``fields``, Data Explorer
``images``): uint16 encoding, NaN-aware block means, even thinning and arrow grids."""

from __future__ import annotations

import base64
import math
import warnings
from typing import Any

import numpy as np
from mapi_spec.views import FIELD_LEVELS, FIELD_NAN, MAX_FIELD_ARROWS, FieldArrows, FieldRaster

__all__ = ["arrows", "block_labels", "block_mean", "encode_raster", "even"]


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
