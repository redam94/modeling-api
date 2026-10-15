"""Shared fixtures: the synthetic demand panel, its schema, specs and a registry."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import xarray as xr
from mapi_sdk.registry import Registry
from mapi_sklearn.testing import demand_dataset, demand_registry, demand_schema, demand_spec
from mapi_spec import DataSchema, ModelSpec


@pytest.fixture
def registry() -> Registry:
    return demand_registry()


@pytest.fixture
def demand() -> xr.Dataset:
    return demand_dataset()


@pytest.fixture
def schema(demand: xr.Dataset) -> DataSchema:
    return demand_schema(demand)


@pytest.fixture
def spec_factory() -> Callable[..., ModelSpec]:
    return demand_spec
