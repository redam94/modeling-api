"""Declarative GLM data: ``sales(row)``, ``price(row)``, ``promo(row)``, ``temp(row)`` and the
string label ``store(row)`` (one flat table, as an analyst uploads it).

:data:`RECIPE` is the model recipe whose compiled output ``spec.yaml`` holds (the fixture's
``check_fit`` compiles it again and compares)."""

from __future__ import annotations

from typing import Any

import numpy as np
import xarray as xr
from mapi_spec.schema import DataSchema

SIZES = {"tiny": (4, 160), "full": (12, 1200)}
TRUTH = {"intercept": 2.0, "price": -0.5, "promo": 0.6, "temp": 0.2, "store_sd": 0.3}
"""Generating values (``price`` per standard deviation of price, ``promo`` the mean over
stores); ``noise_sd`` is 0.3."""

RECIPE: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ModelRecipe",
    "metadata": {"name": "store-sales", "title": "Store sales with promotion lift"},
    "base": "declarative",
    "declarative": {
        "outcome": {"var": "sales"},
        "terms": [
            {
                "var": "price",
                "transform": [{"use": "standardize"}],
                "prior": {"sigma": 0.5},
                "sign": "negative",
            },
            {"var": "promo", "prior": {"sigma": 1.0}, "sign": "positive", "varies_by": "store"},
            {"var": "temp", "prior": {"dist": "student_t", "sigma": 0.5}},
        ],
        "groups": [{"dim": "store"}],
    },
    "plots": ["core.convergence", "core.posterior_forest"],
}


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    stores, rows = SIZES[size]
    rng = np.random.default_rng(seed)
    store = np.sort(rng.integers(0, stores, rows))
    store[:stores] = np.arange(stores)  # every store has rows
    base = TRUTH["store_sd"] * rng.standard_normal(stores)
    lift = TRUTH["promo"] + 0.15 * rng.standard_normal(stores)
    price = rng.normal(3.0, 0.5, rows)
    promo = (rng.uniform(size=rows) < 0.35).astype(float)
    temp = rng.normal(0.0, 1.0, rows)
    eta = (
        TRUTH["intercept"]
        + base[store]
        + TRUTH["price"] * (price - price.mean()) / price.std()
        + lift[store] * promo
        + TRUTH["temp"] * temp
    )
    sales = eta + 0.3 * rng.standard_normal(rows)
    ds = xr.Dataset(
        {
            "sales": (("row",), sales),
            "price": (("row",), price),
            "promo": (("row",), promo),
            "temp": (("row",), temp),
            "store": (("row",), np.array([f"store_{i:02d}" for i in store])),
        },
        coords={"row": np.arange(rows, dtype=np.int64)},
    )
    return ds, {}


def schema() -> DataSchema:
    return DataSchema.model_validate(
        {
            "layout": "long",
            "dims": {"row": {"kind": "integer", "policy": "extend"}},
            "vars": {
                "sales": {"dims": ["row"], "dtype": "float64", "role": "target"},
                "price": {"dims": ["row"], "dtype": "float64", "role": "covariate"},
                "promo": {"dims": ["row"], "dtype": "float64", "role": "covariate"},
                "temp": {"dims": ["row"], "dtype": "float64", "role": "covariate"},
                "store": {"dims": ["row"], "dtype": "str", "role": "covariate"},
            },
        }
    )


def payload_new_store(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Rows after the fit: two in a fitted store, four in a store the fit did not see."""
    start = int(data["row"].values.max()) + 1
    first = str(data["store"].values[0])
    return xr.Dataset(
        {
            "price": (("row",), np.full(6, 3.0)),
            "promo": (("row",), np.array([0.0, 1.0, 0.0, 1.0, 0.0, 0.0])),
            "temp": (("row",), np.zeros(6)),
            "store": (
                ("row",),
                np.array([first, first, "store_new", "store_new", "store_new", "store_new"]),
            ),
        },
        coords={"row": np.arange(start, start + 6, dtype=np.int64)},
    )


def payload_shuffled(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The fit data with its rows in reverse order (labels travel with their rows)."""
    return data.isel(row=np.arange(data.sizes["row"])[::-1])


def payload_new_store_without_temp(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """A new-store payload missing a term variable."""
    return payload_new_store(data, seed, size).drop_vars("temp")
