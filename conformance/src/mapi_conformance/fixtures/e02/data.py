"""E02 data: ``log_radon(home)``, ``floor(home)``, ``county_idx(home)``, ``log_uranium(county)``."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_tabular.datasets import RADON_SCHEMA, radon_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": (8, 80), "full": (40, 600)}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    counties, homes = SIZES[size]
    return radon_dataset(counties=counties, homes=homes, seed=seed), {}


def schema() -> DataSchema:
    return RADON_SCHEMA


def payload_new_county(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Homes measured after the fit: two in a fitted county, four in a new county."""
    start = int(data["home"].values.max()) + 1
    return xr.Dataset(
        {
            "floor": (("home",), np.array([0.0, 1.0, 0.0, 1.0, 0.0, 0.0])),
            "county_idx": (("home",), np.array([0, 0, 1, 1, 1, 1], dtype=np.int64)),
            "log_uranium": (("county",), [float(data["log_uranium"].values[0]), 0.4]),
        },
        coords={
            "home": np.arange(start, start + 6, dtype=np.int64),
            "county": [str(data["county"].values[0]), "county_new"],
        },
    )


def payload_shuffled(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The fit data with the county labels in reverse order (``county_idx`` remapped)."""
    n = data.sizes["county"]
    perm = np.arange(n)[::-1]
    out = data.isel(county=perm)
    inv = np.argsort(perm)
    return out.assign(county_idx=(("home",), inv[data["county_idx"].values]))


def payload_new_county_without_floor(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """A new-county payload missing the observation-level covariate."""
    return payload_new_county(data, seed, size).drop_vars("floor")
