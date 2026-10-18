"""E60 data: ``displacement(component, row, col)`` in um from a traction field drawn from the
model's Matern prior, with a central ``force_mask`` (truth in :func:`truth`)."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_science.traction_data import TRACTION_SCHEMA, simulate_traction
from mapi_spec.schema import DataSchema

SIZES = {"tiny": 32, "full": 64}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    n = SIZES[size]
    return simulate_traction(ny=n, nx=n, seed=seed)[0], {}


def truth(seed: int = 0, size: str = "tiny") -> xr.Dataset:
    """The true traction field and summaries of :func:`generate`."""
    n = SIZES[size]
    return simulate_traction(ny=n, nx=n, seed=seed)[1]


def schema() -> DataSchema:
    return TRACTION_SCHEMA


def payload_corner(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """An 8 x 8 window of pixels (the field is still drawn on the whole fitted grid)."""
    return data.isel(row=slice(0, 8), col=slice(0, 8))


def payload_outside(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Rows past the fitted raster (a traction map exists only where the gel was imaged)."""
    n = data.sizes["row"]
    return data.isel(row=slice(0, 3)).assign_coords(row=[n + 5, n + 6, n + 7])


def payload_z_component(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """A third displacement component (the model is 2-D)."""
    extra = data.isel(component=[0]).assign_coords(component=["z"])
    return xr.concat([data, extra], dim="component", data_vars="minimal", coords="minimal")
