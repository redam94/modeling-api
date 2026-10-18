"""E61 data: ``activator(t, y, x)``, four early images of a growing pattern.

``tiny`` is a 32 x 32 raster of the notebook's square (side 100, noise scaled so the images
are as small as the notebook's); ``full`` is the notebook's 128 x 128 geometry. Both image
the pattern at t = 4, 8, 12 and 16.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import xarray as xr
from mapi_plugin_science.turing_data import TURING_SCHEMA, simulate_turing
from mapi_spec.schema import DataSchema

SIZES = {"tiny": 32, "full": 128}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return simulate_turing(n=SIZES[size], seed=seed)[0], {}


def truth(seed: int = 0, size: str = "tiny") -> dict[str, Any]:
    """The generating parameters of :func:`generate` (and the derived wavelength)."""
    return simulate_turing(n=SIZES[size], seed=seed)[1]


def schema() -> DataSchema:
    return TURING_SCHEMA


def _after(data: xr.Dataset, steps: list[int]) -> xr.Dataset:
    t = data["t"].values
    step = float(np.diff(t).min())
    new = t.max() + step * np.asarray(steps, dtype=float)
    return data.isel(t=[0] * len(steps)).assign_coords(t=new)


def payload_next(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The next image time after the fit (linear theory, within max_extension)."""
    return _after(data, [1])


def payload_gapped(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """An image time that skips the first time after the fit (a gap)."""
    return _after(data, [2])


def payload_too_far(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Three image times after the fit (max_extension is 2)."""
    return _after(data, [1, 2, 3])
