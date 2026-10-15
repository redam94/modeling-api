# ruff: noqa: E501
"""A synthetic store × sku × week demand panel and a matching spec (plan §6.2 shape).

Used by this package's tests, the docs and conformance fixtures. The spec uses the SDK's
toy steps (``toy.lag``, ``toy.fourier``, ``toy.log1p``) so it runs without
``mapi_steps``; register them with :func:`demand_registry`.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.registry import Registry
from mapi_sdk.schema import infer_schema
from mapi_spec import DataSchema, ModelSpec, load_model_spec

__all__ = [
    "DEMAND_SPEC",
    "XGB_SMALL",
    "demand_dataset",
    "demand_registry",
    "demand_schema",
    "demand_spec",
]

DEMAND_SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: store-demand, project: retail}
data:
  dataset: dataset://retail/store_sku_weekly@latest
  bind:
    dims: {time: week}
    vars: {y: units_sold, price: price, promo: promo_flag}
  split: {kind: time_holdout, dim: time, last: 8}
pipeline:
  - {id: plags, use: toy.lag@^1, with: {vars: [promo], along: time, within: [store, sku], lags: [1, 2], fill: 0.0}}
  - {id: season, use: toy.fourier@^1, with: {along: time, period: 52.18, order: 2}}
  - {id: logy, use: toy.log1p@^1, with: {vars: [y]}}
model:
  use: __MODEL__
  settings: __SETTINGS__
  fit: {seed: 7, mode: __MODE__}
evaluate: {metrics: [__METRICS__]}
"""

XGB_SMALL: dict[str, Any] = {
    "kind": "xgboost.XGBRegressor",
    "params": {"n_estimators": 40, "max_depth": 3, "learning_rate": 0.2},
}
"""A small, fast xgboost choice for tests and examples."""

BASE_SETTINGS: dict[str, Any] = {
    "rows": ["store", "sku", "time"],
    "columns": {"roles": ["covariate"]},
    "coord_features": {"store": "category", "sku": "category"},
    "forecast_strategy": "direct",
}


def demand_dataset(
    *,
    stores: int = 5,
    skus: int = 3,
    weeks: int = 60,
    seed: int = 0,
    start: str = "2024-01-07",
) -> xr.Dataset:
    """Weekly unit sales per store and sku: price elasticity -1.2, promo lift, seasonality.

    Variables: ``units_sold`` (Poisson counts), ``price`` (EUR) and ``promo_flag`` (0/1),
    all over ``(store, sku, week)``.
    """
    rng = np.random.default_rng(seed)
    week = pd.date_range(start, periods=weeks, freq="W-SUN").as_unit("ns")
    store_fx = rng.normal(0.0, 0.4, stores)
    sku_fx = np.linspace(-0.3, 0.3, skus)
    base_price = rng.uniform(2.0, 6.0, (stores, skus, 1))
    price = base_price * np.exp(rng.normal(0, 0.1, (stores, skus, weeks)))
    promo = (rng.uniform(size=(stores, skus, weeks)) < 0.2).astype(float)
    season = 0.3 * np.sin(2 * np.pi * np.arange(weeks) / 52.18)
    log_mu = (
        2.5
        + store_fx[:, None, None]
        + sku_fx[None, :, None]
        - 1.2 * np.log(price / base_price)
        + 0.5 * promo
        + season[None, None, :]
    )
    units = rng.poisson(np.exp(log_mu)).astype(float)
    return xr.Dataset(
        {
            "units_sold": (("store", "sku", "week"), units, {"units": "units"}),
            "price": (("store", "sku", "week"), price, {"units": "EUR"}),
            "promo_flag": (("store", "sku", "week"), promo),
        },
        coords={
            "store": [f"s{i}" for i in range(stores)],
            "sku": [f"k{j}" for j in range(skus)],
            "week": week,
        },
    )


def demand_schema(data: xr.Dataset) -> DataSchema:
    """Raw schema: new stores allowed (``pooled``), skus fixed, weeks extend (W-SUN)."""
    schema = infer_schema(data, targets=["units_sold"], policies={"store": "new_levels"})
    store = schema.dims["store"].model_copy(update={"new_level_strategy": "pooled"})
    return schema.model_copy(update={"dims": {**schema.dims, "store": store}})


def demand_spec(
    model: str = "sklearn.tabular_quantiles@^1",
    settings: dict[str, Any] | None = None,
    *,
    mode: str = "evaluate_then_refit",
    metrics: str = "rmse, mape",
    estimator: dict[str, Any] | None = None,
) -> ModelSpec:
    """The demand spec for ``model``; ``settings`` are merged over the defaults."""
    body = {**BASE_SETTINGS, "estimator": estimator or XGB_SMALL, **(settings or {})}
    text = (
        DEMAND_SPEC.replace("__MODEL__", model)
        .replace("__SETTINGS__", json.dumps(body))
        .replace("__MODE__", mode)
        .replace("__METRICS__", metrics)
    )
    return load_model_spec(text)


def demand_registry() -> Registry:
    """A registry with the SDK toy steps and every ``sklearn.*`` plugin."""
    from mapi_sdk.testing import toy

    from mapi_sklearn.plugins import PLUGINS

    reg = toy.toy_registry()
    reg.register(list(PLUGINS))
    return reg
