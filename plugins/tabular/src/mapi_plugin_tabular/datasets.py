"""Synthetic datasets, raw schemas and example specs for the ``tabular.*`` plugins.

* :func:`radon_dataset` - E02-like home radon measurements in **long layout**: one row per
  home (``home`` dim), ``county_idx`` indexing ``county``, a floor indicator and the
  county-level log uranium;
* :func:`store_demand_dataset` - the plan §6.2 store x sku x week panel (units sold, price
  with gaps, promotions);
* :func:`choice_panel_dataset` - a ragged household x trip soda choice panel (E84-lite) for
  ``choice.multinomial_logit``.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = [
    "CHOICE_SCHEMA",
    "CHOICE_SPEC_YAML",
    "CHOICE_TRUTH",
    "RADON_SCHEMA",
    "RADON_SPEC_YAML",
    "RADON_TRUTH",
    "STORE_DEMAND_SCHEMA",
    "STORE_DEMAND_SPEC_YAML",
    "choice_panel_dataset",
    "radon_dataset",
    "store_demand_dataset",
]

RADON_TRUTH: dict[str, float] = {
    "intercept": 1.4,
    "gamma_uranium": 0.7,
    "county_sd": 0.3,
    "floor": -0.65,
    "noise_sd": 0.4,
}
"""Generating parameters of :func:`radon_dataset`."""

RADON_SCHEMA = DataSchema.model_validate(
    {
        "layout": "long",
        "dims": {
            "home": {"kind": "integer", "policy": "extend"},
            "county": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "hyperprior",
            },
        },
        "vars": {
            "log_radon": {
                "dims": ["home"],
                "dtype": "float64",
                "role": "target",
                "units": "log_pCi_L",
            },
            "floor": {"dims": ["home"], "dtype": "float64", "role": "covariate"},
            "county_idx": {
                "dims": ["home"],
                "dtype": "int64",
                "role": "index",
                "index_of": "county",
            },
            "log_uranium": {"dims": ["county"], "dtype": "float64", "role": "covariate"},
        },
    }
)

RADON_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: radon-hierarchical, project: environment}
data:
  dataset: dataset://environment/radon_homes@latest
  bind:
    dims: {obs: home}
    vars: {y: log_radon}
model:
  use: tabular.hierarchical_glm@^1
  settings: {group_dim: county, covariates: [floor], group_covariates: [log_uranium]}
  fit:
    mode: train_only
    seed: 3
    tune: 200
    draws: 100
    chains: 2
    engine: pymc
    keep: {log_likelihood: true, posterior_predictive: {thin: 5}}
evaluate: {metrics: [elpd_loo]}
outputs: {plots: [core.convergence, core.posterior_forest]}
"""


def radon_dataset(
    *, counties: int = 8, homes: int = 64, seed: int = 0, home_start: int = 0
) -> xr.Dataset:
    """Long-layout radon: ``log_radon(home)``, ``floor(home)``, ``county_idx(home)`` and
    ``log_uranium(county)``; county sizes are unequal (a few counties have 1-2 homes)."""
    rng = np.random.default_rng(seed)
    t = RADON_TRUTH
    uranium = rng.normal(0.0, 0.5, counties)
    a = t["intercept"] + t["gamma_uranium"] * uranium + t["county_sd"] * rng.normal(size=counties)
    weights = rng.dirichlet(np.full(counties, 0.8))
    idx = np.sort(rng.choice(counties, size=homes, p=weights))
    idx[:counties] = np.arange(counties)  # every county has at least one home
    idx = np.sort(idx)
    floor = (rng.uniform(size=homes) < 0.3).astype(float)
    y = a[idx] + t["floor"] * floor + t["noise_sd"] * rng.standard_normal(homes)
    return xr.Dataset(
        {
            "log_radon": (("home",), y, {"units": "log_pCi_L"}),
            "floor": (("home",), floor),
            "county_idx": (("home",), idx.astype(np.int64)),
            "log_uranium": (("county",), uranium),
        },
        coords={
            "home": np.arange(home_start, home_start + homes, dtype=np.int64),
            "county": [f"county_{i:02d}" for i in range(counties)],
        },
    )


STORE_DEMAND_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "store_id": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
            },
            "sku": {"kind": "categorical", "policy": "fixed"},
            "week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
        },
        "vars": {
            "units_sold": {
                "dims": ["store_id", "sku", "week"],
                "dtype": "float64",
                "role": "target",
                "units": "units",
            },
            "price": {
                "dims": ["store_id", "sku", "week"],
                "dtype": "float64",
                "role": "covariate",
                "units": "EUR",
                "missing": "allow",
                "future_required": True,
            },
            "promo_flag": {
                "dims": ["store_id", "sku", "week"],
                "dtype": "float64",
                "role": "covariate",
                "future_required": True,
            },
        },
    }
)

STORE_DEMAND_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: store-demand-xgb, project: retail}
data:
  dataset: dataset://retail/store_sku_weekly@latest
  bind:
    dims: {store: store_id, sku: sku, time: week}
    vars: {y: units_sold, price: price, promo: promo_flag}
  split: {kind: time_holdout, dim: time, last: 8}
pipeline:
  - id: imp
    use: core.impute@^1
    with: {vars: [price], method: ffill, along: time, within: [store, sku], unseen_levels: pooled}
  - id: plags
    use: core.lag@^1
    with: {vars: [promo], along: time, within: [store, sku], lags: [1, 2]}
  - {id: cal,   use: core.calendar@^1, with: {along: time, features: [week_of_year, month]}}
  - {id: logy,  use: core.log1p@^1,   with: {vars: [y]}}
model:
  use: tabular.regressor@^1
  settings:
    estimator:
      kind: xgboost.XGBRegressor
      params: {n_estimators: 80, max_depth: 3, learning_rate: 0.15}
    rows: [store, sku, time]
    columns: {roles: [covariate]}
    coord_features: {store: category, sku: category}
    forecast_strategy: direct
    retransform: median
  fit: {seed: 7, mode: evaluate_then_refit}
evaluate: {metrics: [rmse, mape, coverage_80]}
outputs: {plots: [core.fit_vs_actual, core.residuals_over_time, ml.feature_importance]}
"""


def store_demand_dataset(
    *, stores: int = 5, skus: int = 3, weeks: int = 60, seed: int = 0, start: str = "2024-01-07"
) -> xr.Dataset:
    """Weekly ``units_sold``/``price``/``promo_flag`` over ``(store_id, sku, week)``.

    Price elasticity -1.2, a promotion lift of +65%, yearly seasonality and Poisson noise;
    a few prices are missing (a mid-series gap and a trailing gap, forward filled by the
    pipeline).
    """
    rng = np.random.default_rng(seed)
    week = pd.date_range(start, periods=weeks, freq="W-SUN").as_unit("ns")
    store_fx = rng.normal(0.0, 0.4, stores)
    sku_fx = np.linspace(-0.3, 0.3, skus)
    base = rng.uniform(2.0, 6.0, (stores, skus, 1))
    price = base * np.exp(rng.normal(0, 0.1, (stores, skus, weeks)))
    promo = (rng.uniform(size=(stores, skus, weeks)) < 0.2).astype(float)
    season = 0.3 * np.sin(2 * np.pi * np.arange(weeks) / 52.18)
    log_mu = (
        2.5
        + store_fx[:, None, None]
        + sku_fx[None, :, None]
        - 1.2 * np.log(price / base)
        + 0.5 * promo
        + season[None, None, :]
    )
    units = rng.poisson(np.exp(log_mu)).astype(float)
    price_obs = price.copy()
    if weeks > 14:
        price_obs[0, 0, 10:13] = np.nan
    price_obs[min(1, stores - 1), skus - 1, -2:] = np.nan
    attrs: dict[str, Any] = {"units": "EUR"}
    return xr.Dataset(
        {
            "units_sold": (("store_id", "sku", "week"), units, {"units": "units"}),
            "price": (("store_id", "sku", "week"), price_obs, attrs),
            "promo_flag": (("store_id", "sku", "week"), promo),
        },
        coords={
            "store_id": [f"s{i}" for i in range(stores)],
            "sku": [f"k{j}" for j in range(skus)],
            "week": week,
        },
    )


# ================================================================================ choice

CHOICE_TRUTH: dict[str, Any] = {
    "asc": {"cola": 0.0, "fizz": -0.3, "store": -0.8},
    "price": -1.6,
    "promo": 0.9,
    "household_sd": 0.7,
}
"""Generating parameters of :func:`choice_panel_dataset`."""

CHOICE_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "household": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "hyperprior",
            },
            "trip": {"kind": "index", "policy": "extend"},
            "product": {"kind": "categorical"},
        },
        "vars": {
            "brand": {"dims": ["household", "trip"], "dtype": "str", "role": "target",
                      "missing": "allow"},
            "price": {"dims": ["household", "trip", "product"], "dtype": "float64",
                      "role": "covariate", "missing": "allow", "units": "EUR"},
            "promo": {"dims": ["household", "trip", "product"], "dtype": "float64",
                      "role": "covariate", "missing": "allow"},
        },
    }
)  # fmt: skip

CHOICE_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: soda-choice, project: retail, labels: {family: choice}}
data:
  dataset: dataset://retail/soda_trips@latest
  bind:
    dims: {hh: household, trip: trip, product: product}
    vars: {y: brand}
  split: {kind: last_per_group, dim: trip, within: [hh], last: 1, min_len: 3}
model:
  use: choice.multinomial_logit@^1
  settings: {random_effects: [hh]}
  fit: {mode: evaluate_then_refit, draws: 20, seed: 5}
execution: {resource_class: S, timeout: 10m}
"""


def choice_panel_dataset(
    *,
    households: int = 40,
    max_trips: int = 8,
    min_trips: int = 4,
    products: tuple[str, ...] = ("cola", "fizz", "store"),
    seed: int = 0,
) -> xr.Dataset:
    """A ragged household x trip soda panel: ``brand`` chosen (text; '' where a household
    made fewer trips), ``price`` and ``promo`` per product, from :data:`CHOICE_TRUTH`
    (unknown products get intercept 0). The last product is off the shelf on some trips
    (``NaN`` price): it is not available then."""
    rng = np.random.default_rng(seed)
    nj = len(products)
    asc = np.array([CHOICE_TRUTH["asc"].get(p, 0.0) for p in products])
    u = rng.normal(0.0, CHOICE_TRUTH["household_sd"], (households, nj))
    u[:, 0] = 0.0
    base = np.linspace(1.0, 2.0, nj)
    price = base * np.exp(rng.normal(0.0, 0.15, (households, max_trips, nj)))
    promo = (rng.uniform(size=(households, max_trips, nj)) < 0.2).astype(float)
    util = (
        asc
        + CHOICE_TRUTH["price"] * price
        + CHOICE_TRUTH["promo"] * promo
        + u[:, None, :]
        + rng.gumbel(size=(households, max_trips, nj))
    )
    off = rng.uniform(size=(households, max_trips)) < 0.1
    util[..., nj - 1] = np.where(off, -np.inf, util[..., nj - 1])
    choice = np.asarray(products, dtype=object)[np.argmax(util, axis=-1)]
    trips = rng.integers(min_trips, max_trips + 1, households)
    absent = np.arange(max_trips)[None, :] >= trips[:, None]
    brand = np.where(absent, "", choice).astype(str)
    price_obs = np.where(absent[..., None], np.nan, price)
    price_obs[..., nj - 1] = np.where(off | absent, np.nan, price_obs[..., nj - 1])
    promo_obs = np.where(np.isnan(price_obs), np.nan, promo)
    return xr.Dataset(
        {
            "brand": (("household", "trip"), brand),
            "price": (("household", "trip", "product"), price_obs, {"units": "EUR"}),
            "promo": (("household", "trip", "product"), promo_obs),
        },
        coords={
            "household": [f"h{i:03d}" for i in range(households)],
            "trip": np.arange(1, max_trips + 1, dtype=np.int64),
            "product": list(products),
        },
    )
