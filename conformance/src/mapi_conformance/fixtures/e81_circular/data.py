"""E81 data: ``wind_from(report)`` with ``hour_lst`` and ``day_of_year`` (simulated).

The directions come from the model's own von Mises regression with the canonical link
(:data:`~mapi_plugin_science.circular_data.WIND_TRUTH`). ``tiny`` has 480 reports at random
times of one year; ``full`` has 4,000 (a third of the notebook's training reports).
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_science.circular_data import WIND_SCHEMA, wind_dataset
from mapi_spec.schema import DataSchema


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return wind_dataset(n=480 if size == "tiny" else 4000, seed=seed), {}


def schema() -> DataSchema:
    return WIND_SCHEMA


def payload_gapped(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Two reports that skip the first label after the fit (a gap)."""
    last = int(data["report"].values.max())
    out = data.isel(report=slice(-2, None)).drop_vars("wind_from")
    return out.assign_coords(report=np.array([last + 2, last + 3]))


def payload_no_hour(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The last three reports without their hour of day."""
    return data.isel(report=slice(-3, None)).drop_vars(["wind_from", "hour_lst"])


def payload_next_day(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """24 new reports after the fit, one for each hour of the next day."""
    last = int(data["report"].values.max())
    day = float(np.floor(data["day_of_year"].values.max())) + 1.0
    hours = np.arange(24, dtype=float) + 0.5
    return xr.Dataset(
        {
            "hour_lst": (("report",), hours),
            "day_of_year": (("report",), day + hours / 24.0),
            "year": (("report",), np.full(24, 2021, dtype=np.int64)),
        },
        coords={"report": np.arange(last + 1, last + 25, dtype=np.int64)},
    )
