"""Synthetic data and an example spec for ``ts.bsts`` (tests, conformance E13 and docs).

:func:`bsts_dataset` simulates monthly sales from a local linear trend, a yearly cycle and
a price effect, with a few missing months; :data:`BSTS_TRUTH` holds the generating price
effect.
"""

from __future__ import annotations

from typing import Any, Final

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = ["BSTS_SCHEMA", "BSTS_SPEC_YAML", "BSTS_TRUTH", "bsts_dataset"]

BSTS_TRUTH: Final[dict[str, Any]] = {
    "price": -40.0,
    "level0": 1000.0,
    "slope0": 2.0,
    "season_amplitude": 60.0,
    "noise_sd": 15.0,
}
"""Generating values of :func:`bsts_dataset` (``price`` is units of sales per unit of price)."""

BSTS_SCHEMA = DataSchema.model_validate(
    {
        "dims": {"date": {"kind": "time", "policy": "extend", "freq": "MS"}},
        "vars": {
            "sales": {
                "dims": ["date"],
                "dtype": "float64",
                "role": "target",
                "units": "units",
                "missing": "allow",
            },
            "price": {
                "dims": ["date"],
                "dtype": "float64",
                "role": "covariate",
                "units": "USD",
            },
        },
    }
)

BSTS_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: monthly-sales-bsts, project: retail}
data:
  dataset: dataset://retail/monthly_sales@latest
  bind:
    dims: {t: date}
    vars: {y: sales}
  split: {kind: time_holdout, dim: t, last: 12}
model:
  use: ts.bsts@^1
  settings: {regressors: [price], seasonality: [{period: 12}]}
  fit: {mode: evaluate_then_refit, seed: 3, tune: 100, draws: 100, chains: 2, engine: nutpie}
evaluate: {metrics: [rmse, mape, coverage_90]}
outputs: {plots: [core.ppc_fan, core.decomposition, core.convergence]}
"""


def bsts_dataset(
    periods: int = 96, freq: str = "MS", seed: int = 0, missing: int = 3
) -> xr.Dataset:
    """``sales(date)`` and ``price(date)``: local linear trend + yearly cycle + price effect.

    ``missing`` periods (never the first or last three) have no sales value (NaN).
    """
    rng = np.random.default_rng(seed)
    date = pd.date_range("2015-01-01", periods=periods, freq=freq).as_unit("ns")
    span_days = max((date[-1] - date[0]).days, 1)
    per_year = (periods - 1) * 365.25 / span_days  # 12 for monthly data
    t = np.arange(periods, dtype=float)
    slope = BSTS_TRUTH["slope0"] + np.cumsum(rng.normal(0.0, 0.3, periods))
    level = BSTS_TRUTH["level0"] + np.cumsum(slope) + np.cumsum(rng.normal(0.0, 5.0, periods))
    season = BSTS_TRUTH["season_amplitude"] * np.sin(2 * np.pi * t / per_year)
    price = 10.0 + 0.5 * np.sin(2 * np.pi * t / 7.3) + rng.normal(0.0, 0.8, periods)
    sales = (
        level
        + season
        + BSTS_TRUTH["price"] * (price - 10.0)
        + rng.normal(0.0, BSTS_TRUTH["noise_sd"], periods)
    )
    if missing and periods > 6 + missing:
        holes = rng.choice(np.arange(3, periods - 3), size=missing, replace=False)
        sales[holes] = np.nan
    return xr.Dataset(
        {
            "sales": (("date",), sales, {"units": "units"}),
            "price": (("date",), price, {"units": "USD"}),
        },
        coords={"date": date},
        attrs={"mapi_schema": BSTS_SCHEMA.model_dump_json()},
    )
