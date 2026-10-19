"""E31 data: ``pixels(digit, row, col)`` of 8 x 6 images made of three known strokes.

``tiny`` has 60 images; ``full`` has 300 images, as many as the notebook's training digits.
"""

from __future__ import annotations

import xarray as xr
from mapi_plugin_science.ibp_data import IBP_SCHEMA, ibp_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": 60, "full": 300}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return ibp_dataset(n=SIZES[size], seed=seed), {}


def schema() -> DataSchema:
    return IBP_SCHEMA


def payload_two_digits(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The first two images of the fit."""
    return data.isel(digit=[0, 1])


def payload_bottom_half(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The bottom four rows of every image (the half that ``mask_block`` hides)."""
    return data.isel(row=slice(4, 8))


def payload_new_digit(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """An image that the fit did not see (``digit`` is a fixed dim)."""
    return data.isel(digit=[0]).assign_coords(digit=["img-new"])
