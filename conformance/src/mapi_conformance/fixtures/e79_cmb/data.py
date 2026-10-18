"""E79 data: a masked ``temperature(row, col)`` map in uK drawn from the model's own prior
with a known spectrum (truth in :func:`truth`), per-pixel noise and a reference spectrum."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_science.cmb import NOTEBOOK_EDGES
from mapi_plugin_science.cmb_data import CMB_SCHEMA, TINY_EDGES, simulate_cmb
from mapi_spec.schema import DataSchema

SIZES: dict[str, tuple[int, int, tuple[float, ...]]] = {
    "tiny": (32, 4, TINY_EDGES),
    "full": (64, 8, NOTEBOOK_EDGES),
}
"""Map side, torus border and bandpower edges at each size."""


def _simulate(seed: int, size: str) -> tuple[xr.Dataset, xr.Dataset]:
    n, border, edges = SIZES[size]
    return simulate_cmb(n=n, border=border, edges=edges, seed=seed)


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return _simulate(seed, size)[0], {}


def truth(seed: int = 0, size: str = "tiny") -> xr.Dataset:
    """The noiseless sky and the true ``q(bin)`` of :func:`generate`."""
    return _simulate(seed, size)[1]


def schema() -> DataSchema:
    return CMB_SCHEMA


def payload_corner(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """An 8 x 8 window of pixels (the sky is still drawn on the whole fitted torus)."""
    return data.isel(row=slice(0, 8), col=slice(0, 8))


def payload_outside(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Rows past the fitted map (the model has no sky there)."""
    n = data.sizes["row"]
    return data.isel(row=slice(0, 3)).assign_coords(row=[n + 5, n + 6, n + 7])
