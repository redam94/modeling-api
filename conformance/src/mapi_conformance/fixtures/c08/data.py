"""C08 data: monthly ``co2(date)`` in ppm with ``-99.99`` for missing months."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_ts.datasets import CO2_SCHEMA, co2_dataset
from mapi_spec.schema import DataSchema

YEARS = {"tiny": 6, "full": 40}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return co2_dataset(years=YEARS[size], seed=seed), {}


def schema() -> DataSchema:
    return CO2_SCHEMA


def payload_gapped(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Future months that skip the first month after the fit (a gap)."""
    n = data.sizes["date"]
    future = co2_dataset(years=YEARS[size] + 1, seed=seed)["date"].values[n + 1 : n + 4]
    return xr.Dataset(coords={"date": np.asarray(future)})
