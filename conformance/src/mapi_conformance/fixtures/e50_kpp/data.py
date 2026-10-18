"""E50 data: ``cells(hours, position)`` of a simulated scratch assay (Poisson counts).

``tiny`` is a coarse grid (16 columns of 100 um, every 4 h for 28 h, no start-up delay);
``full`` is the notebook's geometry (38 columns of 50 um over 48 h, every 6 h) with a delay.
Both keep at least eight times, so the contract kit's context window holds two labels.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_science.kpp_data import KPP_SCHEMA, KPP_TRUTH, kpp_dataset
from mapi_spec.schema import DataSchema

FULL_TRUTH = {**KPP_TRUTH, "D": 1200.0, "lam": 0.12, "K": 115.0, "beta": 0.05}
"""Generating parameters at size ``full`` (of the order of the E50 posterior, with delay)."""


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    if size == "tiny":
        return kpp_dataset(times=tuple(range(0, 29, 4)), seed=seed), {}
    data = kpp_dataset(
        columns=38,
        dx=50.0,
        times=tuple(range(0, 49, 6)),
        steps_per_hour=10,
        truth=FULL_TRUTH,
        seed=seed,
    )
    return data, {}


def schema() -> DataSchema:
    return KPP_SCHEMA


def payload_gapped(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Two future times that skip the first period after the fit (a gap)."""
    hours = data["hours"].values
    step = int(np.diff(hours).min())
    future = hours.max() + step * np.array([2, 3])
    return xr.Dataset(coords={"hours": future, "position": data["position"].values})
