"""E07 data: ``quakes(quarter, row, col)`` counts simulated from the LGCP with known truth."""

from __future__ import annotations

import pandas as pd
import xarray as xr
from mapi_plugin_science.lgcp_data import LGCP_SCHEMA, lgcp_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": (6, 5, 16), "full": (9, 12, 44)}  # rows, cols, quarters (E07: 9 x 12 x 44)


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    rows, cols, periods = SIZES[size]
    return lgcp_dataset(rows=rows, cols=cols, periods=periods, seed=seed), {}


def schema() -> DataSchema:
    return LGCP_SCHEMA


def payload_gapped(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Every cell for three quarters that skip the first quarter after the fit (a gap)."""
    last = pd.Timestamp(data["quarter"].values.max())
    future = pd.date_range(last, periods=5, freq="QS")[2:].as_unit("ns")
    return xr.Dataset(
        coords={"quarter": future, "row": data["row"].values, "col": data["col"].values}
    )
