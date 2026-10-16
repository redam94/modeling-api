"""E84-lite data: ``brand(household, trip)`` chosen, ``price``/``promo(household, trip, product)``.

A ragged panel: households make between ``min_trips`` and ``max_trips`` trips (the cells of
trips never made are empty), and the store brand is off the shelf on some trips.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_tabular.datasets import CHOICE_SCHEMA, choice_panel_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": (40, 8, 4), "full": (300, 12, 4)}
"""households, most trips, fewest trips"""


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    households, most, fewest = SIZES[size]
    data = choice_panel_dataset(households=households, max_trips=most, min_trips=fewest, seed=seed)
    return data, {}


def schema() -> DataSchema:
    return CHOICE_SCHEMA


def payload_new_household(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The first trips of a fitted household and of one the model never saw."""
    rows = data.isel(household=[0, 0], trip=slice(0, 3)).drop_vars("brand")
    return rows.assign_coords(household=[str(data["household"].values[0]), "h_new"])


def payload_next_trips(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Shelf prices for the next two trips of three fitted households."""
    last = int(data["trip"].values.max())
    rows = data.isel(household=[0, 1, 2], trip=[0, 1]).drop_vars("brand")
    rows = rows.assign_coords(trip=np.arange(last + 1, last + 3, dtype=np.int64))
    return rows.fillna(1.5)
