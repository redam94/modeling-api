"""Fitted C08 and E24 bundles shared by the model tests (tiny MCMC, seed-deterministic)."""

from __future__ import annotations

import pytest
import xarray as xr
from mapi_plugin_ts.datasets import (
    CO2_SCHEMA,
    CO2_SPEC_YAML,
    TRACKS_SCHEMA,
    TRACKS_SPEC_YAML,
    co2_dataset,
    tracks_dataset,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import Registry, default_registry


@pytest.fixture(scope="session")
def registry() -> Registry:
    return default_registry(reload=True)


@pytest.fixture(scope="session")
def co2() -> xr.Dataset:
    return co2_dataset(years=6, seed=0)


@pytest.fixture(scope="session")
def co2_bundle(registry: Registry, co2: xr.Dataset) -> Bundle:
    return fit_spec(CO2_SPEC_YAML, co2, registry=registry, raw_schema=CO2_SCHEMA)


@pytest.fixture(scope="session")
def tracks() -> xr.Dataset:
    return tracks_dataset(animals=4, steps=40, seed=0)


@pytest.fixture(scope="session")
def hmm_bundle(registry: Registry, tracks: xr.Dataset) -> Bundle:
    return fit_spec(TRACKS_SPEC_YAML, tracks, registry=registry, raw_schema=TRACKS_SCHEMA)
