"""Store-demand data: ``units_sold``/``price``/``promo_flag`` over ``(store_id, sku, week)``."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_tabular.datasets import STORE_DEMAND_SCHEMA, store_demand_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": (5, 3, 60), "full": (30, 10, 156)}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    stores, skus, weeks = SIZES[size]
    return store_demand_dataset(stores=stores, skus=skus, weeks=weeks, seed=seed), {}


def schema() -> DataSchema:
    return STORE_DEMAND_SCHEMA


def _next(seed: int, size: str, weeks: int = 4) -> xr.Dataset:
    stores, skus, n = SIZES[size]
    longer = store_demand_dataset(stores=stores, skus=skus, weeks=n + weeks, seed=seed)
    return longer.isel(week=slice(n, None))


def payload_future(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Planned prices and promotions for four weeks, plus a store opening (``s_new``)."""
    nxt = _next(seed, size)
    newbie = nxt.isel(store_id=[0]).assign_coords(store_id=["s_new"])
    return xr.concat([nxt, newbie], dim="store_id")


def payload_future_without_promo(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Future prices only: ``promo_flag`` is declared future-required."""
    return _next(seed, size).drop_vars("promo_flag")


def payload_new_sku(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """A product the model never saw (``sku`` is a fixed dim)."""
    nxt = _next(seed, size)
    return nxt.isel(sku=[0]).assign_coords(sku=["k_new"])
