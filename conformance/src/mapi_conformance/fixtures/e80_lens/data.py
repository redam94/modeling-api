"""E80 data: a mock ``image(row, col)`` of an SIE lens with its ``weight`` and ``psf``."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_science.lens_data import LENS_SCHEMA, lens_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": 24, "full": 48}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    n = SIZES[size]
    return lens_dataset(size=n, pixel_scale=0.1, seed=seed), {}  # the spec says 0.1"


def schema() -> DataSchema:
    return LENS_SCHEMA
