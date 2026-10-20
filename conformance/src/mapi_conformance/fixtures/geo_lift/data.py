"""Geo-lift data: weekly ``sales(geo, week)``; ``geo_03`` is a mix of other markets plus a lift."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_causal.datasets_panel import GEO_LIFT_SCHEMA, geo_lift_dataset
from mapi_spec.schema import DataSchema

GEOS = {"tiny": 12, "full": 30}
"""Markets per size; the weeks (78) and the test window stay those of the spec."""


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return geo_lift_dataset(geos=GEOS[size], seed=seed)[0], {}


def schema() -> DataSchema:
    return GEO_LIFT_SCHEMA


def payload_new_geo(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The fitted markets plus a market the fit did not see (the geo dim is fixed)."""
    extra = data.isel(geo=[0]).assign_coords(geo=["geo_new"])
    return xr.concat([data, extra], dim="geo")


def payload_donor_gap(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The fit data with one missing donor value."""
    out = data.copy(deep=True)
    out["sales"][0, 10] = float("nan")
    return out
