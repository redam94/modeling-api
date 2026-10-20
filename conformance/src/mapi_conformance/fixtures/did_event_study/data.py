"""DiD data: monthly ``sales(store, month)`` and ``treatment(store, month)`` (common adoption)."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_causal.datasets_panel import DID_SCHEMA, did_dataset
from mapi_spec.schema import DataSchema

UNITS = {"tiny": 20, "full": 120}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return did_dataset(units=UNITS[size], periods=24, seed=seed)[0], {}


def schema() -> DataSchema:
    return DID_SCHEMA


def payload_new_month(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """A month after the fit (the time dim is fixed: time effects exist for fitted months only)."""
    last = data.isel(month=[-1])
    return last.assign_coords(
        month=[last["month"].values[0] + (data["month"].values[1] - data["month"].values[0])]
    )


def payload_without_treatment(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    return data.drop_vars("treatment")
