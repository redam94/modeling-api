"""E51 data: ``reading_c(run, y_cm, x_cm)`` of a simulated plate calibration.

``tiny`` is a coarse 12 x 6 grid of 3.3 cm cells; ``full`` is the notebook's 24 x 12 grid of
1.67 cm cells (at ``seed=0`` it is the E51 experiment itself). Both have three runs with one
10 W test heater each and the same twelve thermocouples in every run.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_science.heat_plate_data import HEAT_SCHEMA, heat_plate_dataset
from mapi_spec.schema import DataSchema

GRIDS = {"tiny": (12, 6), "full": (24, 12)}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    nx, ny = GRIDS[size]
    return heat_plate_dataset(nx=nx, ny=ny, seed=seed), {}


def schema() -> DataSchema:
    return HEAT_SCHEMA


def payload_layout(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """One new run: two 8 W heaters at the right end and a 3 W heater at the left end."""
    ny, nx = data.sizes["y_cm"], data.sizes["x_cm"]
    power = np.zeros((1, ny, nx))
    power[0, ny // 4, nx - 2] = 8.0
    power[0, 3 * ny // 4, nx - 2] = 8.0
    power[0, ny // 2, 1] = 3.0
    return xr.Dataset(
        {"heater_w": (("run", "y_cm", "x_cm"), power)},
        coords={"run": ["design"], "y_cm": data["y_cm"].values, "x_cm": data["x_cm"].values},
    )


def payload_off_grid(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The layout on columns that are not on the plate."""
    out = payload_layout(data, seed, size)
    return out.assign_coords(x_cm=out["x_cm"].values + 100.0)


def payload_no_power(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """A new run without its heater powers."""
    return payload_layout(data, seed, size).drop_vars("heater_w")
