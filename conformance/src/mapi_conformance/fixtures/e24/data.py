"""E24 data: padded ``step_length(animal, step)`` with an ``observed`` mask (ragged tracks)."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_ts.datasets import TRACKS_SCHEMA, tracks_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": (4, 40), "full": (16, 150)}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    animals, steps = SIZES[size]
    return tracks_dataset(animals=animals, steps=steps, seed=seed), {}


def schema() -> DataSchema:
    return TRACKS_SCHEMA


def payload_live(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The next six steps of every track (new telemetry to filter)."""
    animals, steps = SIZES[size]
    longer = tracks_dataset(animals=animals, steps=steps + 6, seed=seed)
    return longer.isel(step=slice(steps, None))


def payload_new_animal(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The first steps of the fitted animals plus a newly tagged one."""
    window = data.isel(step=slice(0, 4))
    newbie = window.isel(animal=[0]).assign_coords(animal=["tagged_new"])
    return xr.concat([window, newbie], dim="animal")
