"""Example data, raw schemas and specs for the statsmodels plugins (docs, tests, conformance).

* :func:`sales_dataset` - synthetic weekly sales with a price and a promotion driver and
  AR(1) noise, optionally per geo (an ARIMAX panel);
* :func:`macro_dataset` - US quarterly real GDP, consumption and investment from
  statsmodels' bundled ``macrodata`` (public domain, FRED), as ``value(series, quarter)``
  for VAR/VECM;
* :func:`regime_dataset` - a synthetic series that switches between a low and a high
  regime (Markov switching);
* :func:`ecm_dataset` - a synthetic result tied to a trending driver in the long run
  (error correction).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = [
    "ECM_SCHEMA",
    "ECM_SPEC_YAML",
    "MACRO_SCHEMA",
    "MACRO_SERIES",
    "REGIME_SCHEMA",
    "SALES_SCHEMA",
    "SALES_SPEC_YAML",
    "SALES_TRUTH",
    "VAR_SPEC_YAML",
    "VECM_SPEC_YAML",
    "ecm_dataset",
    "macro_dataset",
    "regime_dataset",
    "sales_dataset",
    "sales_schema",
]

SALES_TRUTH: dict[str, float] = {
    "intercept": 100.0,
    "price": -8.0,
    "promo": 12.0,
    "ar": 0.6,
    "sigma": 2.0,
}
"""Generating parameters of :func:`sales_dataset` (per geo the intercept shifts by +10)."""


def sales_schema(*, geo: bool = False) -> DataSchema:
    """Raw schema of :func:`sales_dataset` (with or without the ``geo`` dim)."""
    lead = ["geo"] if geo else []
    dims: dict[str, Any] = {"date": {"kind": "time", "policy": "extend", "freq": "W-SUN"}}
    if geo:
        dims["geo"] = {"kind": "categorical", "policy": "fixed"}
    return DataSchema.model_validate(
        {
            "dims": dims,
            "vars": {
                "sales": {
                    "dims": [*lead, "date"],
                    "dtype": "float64",
                    "role": "target",
                    "units": "units",
                },
                "price": {
                    "dims": [*lead, "date"],
                    "dtype": "float64",
                    "role": "covariate",
                    "units": "EUR",
                    "future_required": True,
                },
                "promo": {
                    "dims": [*lead, "date"],
                    "dtype": "float64",
                    "role": "covariate",
                    "future_required": True,
                },
            },
        }
    )


SALES_SCHEMA = sales_schema()


def sales_dataset(
    *, periods: int = 104, geos: list[str] | None = None, seed: int = 0, start: str = "2023-01-01"
) -> xr.Dataset:
    """Weekly ``sales(date)`` (or ``sales(geo, date)``) driven by ``price`` and ``promo``.

    ``sales = intercept + price_effect * price + promo_effect * promo + u``, with AR(1)
    noise ``u`` (see :data:`SALES_TRUTH`).
    """
    rng = np.random.default_rng(seed)
    date = pd.date_range(start, periods=periods, freq="W-SUN").as_unit("ns")
    cells = geos or [None]
    sales, price, promo = [], [], []
    for g, _ in enumerate(cells):
        p = 10.0 + np.cumsum(rng.normal(0, 0.15, periods)) * 0.5 + rng.normal(0, 0.3, periods)
        pr = (rng.uniform(size=periods) < 0.2).astype(float)
        u = np.zeros(periods)
        eps = rng.normal(0, SALES_TRUTH["sigma"], periods)
        for i in range(periods):
            u[i] = (SALES_TRUTH["ar"] * u[i - 1] if i else 0.0) + eps[i]
        y = (
            SALES_TRUTH["intercept"]
            + 10.0 * g
            + SALES_TRUTH["price"] * p
            + SALES_TRUTH["promo"] * pr
            + u
        )
        sales.append(y)
        price.append(p)
        promo.append(pr)
    if geos is None:
        return xr.Dataset(
            {
                "sales": (("date",), sales[0], {"units": "units"}),
                "price": (("date",), price[0], {"units": "EUR"}),
                "promo": (("date",), promo[0]),
            },
            coords={"date": date},
        )
    return xr.Dataset(
        {
            "sales": (("geo", "date"), np.array(sales), {"units": "units"}),
            "price": (("geo", "date"), np.array(price), {"units": "EUR"}),
            "promo": (("geo", "date"), np.array(promo)),
        },
        coords={"geo": list(geos), "date": date},
    )


SALES_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: weekly-sales-arimax, project: retail}
data:
  dataset: dataset://retail/weekly_sales@latest
  bind:
    dims: {t: date}
    vars: {y: sales}
  split: {kind: time_holdout, dim: t, last: 8}
model:
  use: ts.sarimax@^1
  settings: {order: [1, 0, 0], exog: [price, promo]}
  fit: {mode: evaluate_then_refit, seed: 3}
evaluate: {metrics: [rmse, mae, mape, coverage_90]}
outputs: {plots: [core.ppc_fan, core.fit_vs_actual]}
"""

# =================================================================================== macro

MACRO_SERIES = ("realgdp", "realcons", "realinv")
"""The ``macrodata`` columns used by :func:`macro_dataset`."""

MACRO_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "quarter": {"kind": "time", "policy": "extend", "freq": "QS-JAN"},
            "series": {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            "value": {
                "dims": ["series", "quarter"],
                "dtype": "float64",
                "role": "target",
                "units": "bn USD (2005)",
            }
        },
    }
)


def macro_dataset(*, quarters: int | None = None, log: bool = True) -> xr.Dataset:
    """US quarterly ``value(series, quarter)`` for real GDP, consumption and investment,
    1959Q1-2009Q3 (statsmodels ``macrodata``; public domain, FRED). ``log`` takes natural
    logs (cointegration analysis is done on log levels); ``quarters`` keeps the first ones.
    """
    from statsmodels.datasets import macrodata

    raw = macrodata.load_pandas().data
    if quarters is not None:
        raw = raw.iloc[:quarters]
    dates = pd.PeriodIndex.from_fields(
        year=raw["year"].astype(int), quarter=raw["quarter"].astype(int), freq="Q"
    ).to_timestamp(how="start")
    values = raw[list(MACRO_SERIES)].to_numpy(dtype=float).T
    if log:
        values = np.log(values)
    return xr.Dataset(
        {"value": (("series", "quarter"), values, {"units": "log bn USD" if log else "bn USD"})},
        coords={"series": list(MACRO_SERIES), "quarter": pd.DatetimeIndex(dates).as_unit("ns")},
    )


VECM_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: us-macro-vecm, project: macro}
data:
  dataset: dataset://macro/us_quarterly@latest
  bind:
    dims: {t: quarter, variable: series}
    vars: {y: value}
  split: {kind: time_holdout, dim: t, last: 8}
model:
  use: ts.vecm@^1
  settings: {k_ar_diff: 2, deterministic: ci}
  fit: {mode: evaluate_then_refit, seed: 1}
evaluate: {metrics: [rmse, mae, coverage_90]}
"""

VAR_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: us-macro-var, project: macro}
data:
  dataset: dataset://macro/us_quarterly_growth@latest
  bind:
    dims: {t: quarter, variable: series}
    vars: {y: value}
  split: {kind: time_holdout, dim: t, last: 8}
model:
  use: ts.var@^1
  settings: {max_lags: 4, ic: aic}
  fit: {mode: evaluate_then_refit, seed: 1}
evaluate: {metrics: [rmse, mae, coverage_90]}
"""

# =================================================================================== regimes

REGIME_SCHEMA = DataSchema.model_validate(
    {
        "dims": {"week": {"kind": "integer", "policy": "extend"}},
        "vars": {"demand": {"dims": ["week"], "dtype": "float64", "role": "target"}},
    }
)


def regime_dataset(*, periods: int = 160, seed: int = 0) -> xr.Dataset:
    """``demand(week)`` switching between a normal (mean 10) and a high (mean 16) regime
    with persistent regimes (stay probabilities 0.92 and 0.85)."""
    rng = np.random.default_rng(seed)
    stay = (0.92, 0.85)
    state = np.zeros(periods, dtype=int)
    for i in range(1, periods):
        state[i] = state[i - 1] if rng.uniform() < stay[state[i - 1]] else 1 - state[i - 1]
    y = np.where(state == 0, 10.0, 16.0) + rng.normal(0, np.where(state == 0, 1.0, 1.5))
    return xr.Dataset(
        {"demand": (("week",), y), "regime": (("week",), state)},
        coords={"week": np.arange(periods, dtype=np.int64)},
    )


# =================================================================================== ECM

ECM_SCHEMA = DataSchema.model_validate(
    {
        "dims": {"month": {"kind": "time", "policy": "extend", "freq": "MS"}},
        "vars": {
            "sales": {"dims": ["month"], "dtype": "float64", "role": "target"},
            "income": {
                "dims": ["month"],
                "dtype": "float64",
                "role": "covariate",
                "future_required": True,
            },
        },
    }
)


def ecm_dataset(*, periods: int = 150, seed: int = 0) -> xr.Dataset:
    """``sales(month)`` tied to a random-walk ``income`` in the long run
    (``sales* = 2 + 0.8 income``) with 30% of a gap closed each month."""
    rng = np.random.default_rng(seed)
    income = 10.0 + np.cumsum(rng.normal(0.05, 0.5, periods))
    sales = np.zeros(periods)
    sales[0] = 2 + 0.8 * income[0]
    for i in range(1, periods):
        gap = sales[i - 1] - (2 + 0.8 * income[i - 1])
        sales[i] = sales[i - 1] - 0.3 * gap + 0.5 * (income[i] - income[i - 1]) + rng.normal(0, 0.3)
    month = pd.date_range("2012-01-01", periods=periods, freq="MS").as_unit("ns")
    return xr.Dataset(
        {"sales": (("month",), sales), "income": (("month",), income)}, coords={"month": month}
    )


ECM_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: sales-income-ecm, project: retail}
data:
  dataset: dataset://retail/monthly_sales@latest
  bind:
    dims: {t: month}
    vars: {y: sales}
  split: {kind: time_holdout, dim: t, last: 6}
model:
  use: ts.ecm@^1
  settings: {lags: 1, order: 1, exog: [income]}
  fit: {mode: evaluate_then_refit, seed: 1}
evaluate: {metrics: [rmse, mae, coverage_90]}
"""
