"""E23 data: ``brain``, ``body`` and ``group_size`` of species on a tree (node arrays).

``tiny`` has 40 species on a random ultrametric tree, 4 without a brain value; ``full`` has
186 species (the size of the E23 primate data, 35 without a brain value) with Pagel-lambda
residuals near the notebook's estimate.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_science.phylo_data import PHYLO_SCHEMA, PHYLO_TRUTH, phylo_dataset
from mapi_spec.schema import DataSchema

FULL_TRUTH = {**PHYLO_TRUTH, "sigma": 0.3, "lam": 0.95}
"""Generating parameters at size ``full`` (of the order of the E23 lambda posterior)."""


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    if size == "tiny":
        return phylo_dataset(n_species=40, missing=4, seed=seed), {}
    return phylo_dataset(n_species=186, missing=35, truth=FULL_TRUTH, seed=seed), {}


def schema() -> DataSchema:
    return PHYLO_SCHEMA


def payload_first_ten(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The first ten species (with the tree)."""
    return data.isel(species=slice(0, 10))


def payload_missing(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Only the species without a brain value: their traits come from relatives."""
    return data.isel(species=np.flatnonzero(data["brain"].isnull().values))


def payload_stranger(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """A species the fit never saw (not a tip of the tree)."""
    out = data.isel(species=slice(0, 3))
    return out.assign_coords(species=["sp_new", *out["species"].values[1:]])
