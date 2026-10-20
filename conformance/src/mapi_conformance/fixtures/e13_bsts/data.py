"""E13 data: monthly ``sales(date)`` with a few missing months and its ``price(date)``
driver (:func:`mapi_plugin_ts.bsts_data.bsts_dataset`)."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_ts.bsts_data import BSTS_SCHEMA, bsts_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": 60, "full": 120}
AHEAD = 12


def _full(seed: int, size: str) -> xr.Dataset:
    return bsts_dataset(periods=SIZES[size] + AHEAD, seed=seed)


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return _full(seed, size).isel(date=slice(0, SIZES[size])), {}


def schema() -> DataSchema:
    return BSTS_SCHEMA


def payload_future(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Planned prices for the next twelve months."""
    n = SIZES[size]
    return _full(seed, size).isel(date=slice(n, n + AHEAD)).drop_vars("sales")


def payload_future_without_price(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The next months without prices (``price`` is declared future-required)."""
    return payload_future(data, seed, size).drop_vars("price")


def payload_live(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Sales and prices of the three months after the fit (new data to filter)."""
    n = SIZES[size]
    return _full(seed, size).isel(date=slice(n, n + 3))


__all__ = [
    "AHEAD",
    "SIZES",
    "generate",
    "payload_future",
    "payload_future_without_price",
    "payload_live",
    "schema",
]
