"""E21 data: ``cowork(lawyer, lawyer_)`` of a simulated firm with pair covariates.

``tiny`` has 30 nodes (435 pairs); ``full`` has the 71 nodes of the Lazega firm (2485
pairs) in three offices. Both come from the model's own forward process
(:func:`~mapi_plugin_science.network_data.network_dataset`).
"""

from __future__ import annotations

import xarray as xr
from mapi_plugin_science.network_data import NETWORK_SCHEMA, network_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": 30, "full": 71}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return network_dataset(n_nodes=SIZES[size], seed=seed), {}


def schema() -> DataSchema:
    return NETWORK_SCHEMA


def payload_one_covariate(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Only the first pair covariate: a pair needs every covariate of the fit."""
    return data.isel(covariate=[0])


def payload_first_ten(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The pairs among the first ten nodes."""
    return data.isel(lawyer=slice(0, 10), lawyer_=slice(0, 10))
