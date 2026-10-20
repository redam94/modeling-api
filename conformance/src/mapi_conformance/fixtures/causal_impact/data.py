"""causal_impact data: weekly ``sales(week)`` that follow ``control_a`` and ``control_b``,
10% higher from the first week of 2024 on (:func:`mapi_plugin_causal.datasets.impact_dataset`)."""

from __future__ import annotations

import xarray as xr
from mapi_plugin_causal.datasets import IMPACT_SCHEMA, impact_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": 64, "full": 104}
START_WEEK = 52
"""Week 52 is 2024-01-01, the ``intervention_start`` of spec.yaml."""
LIFT = 0.1
AHEAD = 4


def _full(seed: int, size: str) -> xr.Dataset:
    ds, _ = impact_dataset(weeks=SIZES[size] + AHEAD, start_week=START_WEEK, lift=LIFT, seed=seed)
    return ds


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    return _full(seed, size).isel(week=slice(0, SIZES[size])), {}


def schema() -> DataSchema:
    return IMPACT_SCHEMA


def payload_future(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The controls of the next four weeks (no sales yet)."""
    n = SIZES[size]
    return _full(seed, size).isel(week=slice(n, n + AHEAD)).drop_vars("sales")


def payload_future_without_control(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The next four weeks without ``control_b`` (declared future-required)."""
    return payload_future(data, seed, size).drop_vars("control_b")


__all__ = [
    "AHEAD",
    "LIFT",
    "SIZES",
    "START_WEEK",
    "generate",
    "payload_future",
    "payload_future_without_control",
    "schema",
]
