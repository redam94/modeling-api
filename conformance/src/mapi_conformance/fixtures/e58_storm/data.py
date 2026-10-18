"""E58 data: a raw STORM movie ``frames(frame, row, col)`` in ADU simulated with the real
recording's camera (truth in :func:`truth`)."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_science.storm_data import STORM_SCHEMA, simulate_storm
from mapi_spec.schema import DataSchema

SIZES = {"tiny": (60, 24, 0.6), "full": (200, 40, 1.5)}


def _sim(seed: int, size: str) -> tuple[xr.Dataset, xr.Dataset]:
    frames, side, rate = SIZES[size]
    return simulate_storm(frames=frames, size=side, rate=rate, seed=seed)


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return _sim(seed, size)[0], {}


def truth(seed: int = 0, size: str = "tiny") -> xr.Dataset:
    """The true blinks (``frame``, ``x``, ``y`` in pixels, ``photons``) of :func:`generate`."""
    return _sim(seed, size)[1]


def schema() -> DataSchema:
    return STORM_SCHEMA


def payload_window(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Ten frames and the top half of the rows."""
    return data.isel(frame=slice(0, 10), row=slice(0, data.sizes["row"] // 2))


def payload_outside(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Rows past the camera chip (the movie has no such pixels)."""
    n = data.sizes["row"]
    return data.isel(row=slice(0, 3)).assign_coords(row=[n + 5, n + 6, n + 7])
