"""Shared fixtures: reference plugins, synthetic data and fitted radon/trend bundles.

Fits use tiny settings (tune/draws of 100-150, 2 chains) with the seed-deterministic
``pymc`` engine; module-scoped fixtures fit once per test module.
"""

from __future__ import annotations

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from mapi_pymc.examples import (
    RADON_SCHEMA,
    RADON_SPEC_YAML,
    TREND_SCHEMA,
    TREND_SPEC_YAML,
    PanelModel,
    RadonModel,
    TrendModel,
    radon_dataset,
    trend_dataset,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import Registry
from mapi_spec import ModelSpec, load_model_spec


@pytest.fixture(scope="session")
def registry() -> Registry:
    reg = Registry()
    reg.register_model(RadonModel)
    reg.register_model(TrendModel)
    reg.register_model(PanelModel)
    return reg


@pytest.fixture(scope="session")
def radon_data() -> xr.Dataset:
    return radon_dataset(counties=6, per_county=8, seed=0)


@pytest.fixture(scope="session")
def radon_spec() -> ModelSpec:
    return load_model_spec(RADON_SPEC_YAML)


@pytest.fixture(scope="session")
def radon_bundle(registry: Registry, radon_data: xr.Dataset, radon_spec: ModelSpec) -> Bundle:
    return fit_spec(radon_spec, radon_data, registry=registry, raw_schema=RADON_SCHEMA)


@pytest.fixture(scope="session")
def trend_data() -> xr.Dataset:
    return trend_dataset(weeks=60, seed=0)


@pytest.fixture(scope="session")
def trend_bundle(registry: Registry, trend_data: xr.Dataset) -> Bundle:
    return fit_spec(
        load_model_spec(TREND_SPEC_YAML), trend_data, registry=registry, raw_schema=TREND_SCHEMA
    )


@pytest.fixture
def linear_model() -> tuple[pm.Model, np.ndarray, np.ndarray]:
    """A small linear regression with dims (``x`` data, ``mu`` deterministic, ``y`` obs)."""
    rng = np.random.default_rng(42)
    x = rng.standard_normal(20)
    y = 1.0 + 2.0 * x + 0.5 * rng.standard_normal(20)
    with pm.Model(coords={"obs": np.arange(20)}) as model:
        xd = pm.Data("x", x, dims="obs")
        a = pm.Normal("alpha", 0.0, 2.0)
        b = pm.Normal("beta", 0.0, 2.0)
        s = pm.HalfNormal("sigma", 1.0)
        mu = pm.Deterministic("mu", a + b * xd, dims="obs")
        pm.Normal("y", mu, s, observed=y, dims="obs")
    return model, x, y
