"""E18 data: a simulated disease map ``cases(district)`` with expected counts, a covariate,
the adjacency and the polygons of square districts plus one offshore island.

``tiny`` is a 6 x 7 grid (43 districts); ``full`` is 7 x 8 (57 districts, about the 56 of the
Scottish data), with the E18 posterior means as the truth.
"""

from __future__ import annotations

import xarray as xr
from mapi_plugin_science.areal_data import AREAL_SCHEMA, areal_dataset
from mapi_spec.schema import DataSchema

FULL_TRUTH = {"b0": 0.1, "b1": 0.4, "sigma": 0.52, "rho": 0.82}
"""Generating parameters at size ``full`` (the E18 BYM2 posterior means)."""


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    if size == "tiny":
        return areal_dataset(seed=seed), {}
    return areal_dataset(n_row=7, n_col=8, truth=FULL_TRUTH, seed=seed), {}


def schema() -> DataSchema:
    return AREAL_SCHEMA


def payload_two_areas(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Only the labels of two districts: the expected counts and covariate come from the fit."""
    return xr.Dataset(coords={"district": data["district"].values[[3, 0]]})


def payload_new_area(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """A district that is not on the fitted map."""
    one = data.drop_vars("cases").isel(district=[0])
    return one.assign_coords(district=["Atlantis"])
