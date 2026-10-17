"""sm_arimax data: synthetic weekly ``sales(geo, date)`` driven by ``price`` and ``promo``
with AR(1) noise (:func:`mapi_plugin_statsmodels.datasets.sales_dataset`)."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_statsmodels.datasets import sales_dataset, sales_schema
from mapi_spec.schema import DataSchema

GEOS = ["north", "south", "west"]
SIZES = {"tiny": 96, "full": 208}
AHEAD = 12


def _full(seed: int, size: str) -> xr.Dataset:
    return sales_dataset(periods=SIZES[size] + AHEAD, geos=GEOS, seed=seed)


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return _full(seed, size).isel(date=slice(0, SIZES[size])), {}


def schema() -> DataSchema:
    return sales_schema(geo=True)


def payload_future(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Planned prices and promotions for the next eight weeks."""
    n = SIZES[size]
    return _full(seed, size).isel(date=slice(n, n + 8)).drop_vars("sales")


def payload_future_without_price(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Future promotions only: ``price`` is declared future-required."""
    return payload_future(data, seed, size).drop_vars("price")


def payload_gapped(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Future weeks that skip the first week after the fit."""
    return payload_future(data, seed, size).isel(date=slice(1, None))


def payload_new_geo(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """A geo the model never saw (``geo`` is a fixed dim: one model per fitted series)."""
    fut = payload_future(data, seed, size)
    return fut.isel(geo=[0]).assign_coords(geo=["east"])


def payload_scenario_promo(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The planned weeks with a promotion every week (a what-if on a future driver)."""
    fut = payload_future(data, seed, size)
    return fut.assign(promo=xr.ones_like(fut["promo"]))


__all__ = [
    "AHEAD",
    "GEOS",
    "SIZES",
    "generate",
    "payload_future",
    "payload_future_without_price",
    "payload_gapped",
    "payload_new_geo",
    "payload_scenario_promo",
    "schema",
]
