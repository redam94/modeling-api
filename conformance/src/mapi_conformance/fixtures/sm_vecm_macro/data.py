"""sm_vecm_macro data: ``value(series, quarter)`` - log real GDP, consumption and investment,
1959Q1-2006Q4 (192 quarters, before the 2008 crisis), from statsmodels' bundled
``macrodata`` (public domain; FRED). Both sizes use the same quarters; the data ship with
statsmodels, nothing is stored here."""

from __future__ import annotations

import pandas as pd
import xarray as xr
from mapi_plugin_statsmodels.datasets import MACRO_SCHEMA, macro_dataset
from mapi_spec.schema import DataSchema

QUARTERS = 192


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return macro_dataset(quarters=QUARTERS), {}


def schema() -> DataSchema:
    return MACRO_SCHEMA


def payload_new_series(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """A fourth series the model was not fitted on (``variable`` is a fixed dim)."""
    tail = data.isel(quarter=slice(-4, None))
    extra = tail.isel(series=[0]).assign_coords(series=["realgovt"])
    return xr.concat([tail, extra], dim="series")


def payload_gapped(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Quarters that skip the first quarter after the fit."""
    last = pd.Timestamp(data["quarter"].values[-1])
    dates = pd.date_range(last, periods=4, freq="QS-JAN")[2:].as_unit("ns")
    return xr.Dataset(coords={"quarter": dates.values, "series": data["series"].values})
