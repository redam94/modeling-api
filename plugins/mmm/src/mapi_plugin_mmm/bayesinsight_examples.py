# ruff: noqa: E501
"""A synthetic BayesInsight project with known parameters (tests, docs, corpus).

:func:`bayesinsight_dataset` simulates a weekly geography x product panel exactly as
``mmm.bayesinsight`` models it - the KPI in its own units (sales of ~200 per week and
cell), two media variables through a Hill curve on their non-zero-median index and a
delayed carry-over, a standardised price control with a negative sign, an annual Fourier
season and Normal noise - and returns it shaped like the BayesInsight MFF ingest (dims
``date``, ``geo``, ``product``; variables named as in the MFF). :data:`BI_MODEL_DEF` and
:data:`BI_METADATA` are the matching ``model_def.json`` and ``metadata.json``;
:func:`bayesinsight_mff` turns the dataset back into the MFF long table, and
:data:`BI_SPEC_YAML` is the spec the importer writes for ``mmm.bayesinsight``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_mmm.bayesinsight_math import adstock_weights, fourier_features, nonzero_index

__all__ = [
    "BI_L_MAX",
    "BI_METADATA",
    "BI_MODEL_DEF",
    "BI_SPEC_YAML",
    "BITruth",
    "bayesinsight_dataset",
    "bayesinsight_mff",
    "bayesinsight_raw_schema",
    "bi_spec_text",
]

BI_L_MAX: Final = 6
GEOS: Final = ("US", "DE")
PRODUCTS: Final = ("Phones", "Watches")
MEDIA: Final = ("TV", "Search")

BI_METADATA: Final[dict[str, Any]] = {
    "allowed_geos": list(GEOS),
    "allowed_products": list(PRODUCTS),
    "allowed_outlets": ["Total"],
    "allowed_campaigns": ["Total"],
    "allowed_creatives": ["Total"],
    "periodicity": "Weekly",
    "row_ids": ["Geography", "Product", "Period"],
}
"""``metadata.json`` of the synthetic project."""

_SUFFIX = "_Total_Total_Total"

BI_MODEL_DEF: Final[dict[str, Any]] = {
    "variable_details": [
        {"variable_name": "Sales" + _SUFFIX, "variable_type": "exog",
         "intercept_prior": {"coeff_dist": "Normal", "coeff_params": {"mu": 200.0, "sigma": 50.0}},
         "random_coeff_dims": ["Geography"], "partial_pooling_sigma": 20.0,
         "likelihood": {"type": "Normal", "likelihood_params": {"sigma": 10.0}}},
        {"variable_name": "TV" + _SUFFIX, "variable_type": "media", "adstock": "delayed",
         "media_transform": "hill",
         "coeff_prior": {"coeff_dist": "LogNormal", "coeff_params": {"mu": math.log(30.0), "sigma": 0.5}},
         "media_transform_prior": {"type": "Hill", "K_ave": 0.85, "K_std": 0.3, "n_ave": 2.0},
         "adstock_prior": {"type": "Delayed", "retention_rate_mean": 0.5, "retention_rate_std": 10.0,
                           "lag_max": 1.0}},
        {"variable_name": "Search" + _SUFFIX, "variable_type": "media", "adstock": "geometric",
         "media_transform": "hill",
         "coeff_prior": {"coeff_dist": "LogNormal", "coeff_params": {"mu": math.log(20.0), "sigma": 0.5}},
         "media_transform_prior": {"type": "Hill", "K_ave": 0.85, "K_std": 0.3, "n_ave": 2.0},
         "adstock_prior": {"type": "Delayed", "retention_rate_mean": 0.3, "retention_rate_std": 10.0}},
        {"variable_name": "Price" + _SUFFIX, "variable_type": "control",
         "normalization": "Global Standardize", "sign": "negative",
         "coeff_prior": {"coeff_dist": "Normal", "coeff_params": {"mu": 0.0, "sigma": 20.0}}},
        {"variable_name": "annual_seasonality", "variable_type": "season", "n_fourier": 2,
         "period": 52.18, "partial_pooling_sigma": 0,
         "coeff_prior": {"coeff_dist": "Normal", "coeff_params": {"mu": 0.0, "sigma": 10.0}}},
    ],
    "artifact": "synthetic",
    "fitted": False,
}  # fmt: skip
"""``model_def.json`` of the synthetic project (priors in the KPI's units)."""

BI_SPEC_YAML: Final = f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: bi-sales, project: retail, labels: {{family: mmm, imported: bayesinsight}}}}
data:
  dataset: dataset://retail/bi_mff@latest
  bind:
    dims: {{time: date, geo: geo, product: product}}
    vars: {{y: Sales, tv: TV, search: Search, price: Price}}
  split: {{kind: time_holdout, dim: time, last: 6}}
pipeline:
  - {{id: media_lags, use: core.lag_tensor@^1, with: {{vars: [tv, search], along: time, within: [geo, product], l_max: {BI_L_MAX}, pad: nan}}}}
model:
  use: mmm.bayesinsight@^1
  settings:
    variables: {{Sales{_SUFFIX}: y, TV{_SUFFIX}: tv, Search{_SUFFIX}: search, Price{_SUFFIX}: price}}
    media: [tv, search]
    controls: [price]
    row_dims: {{Geography: geo, Product: product}}
    panel: [geo, product]
    l_max: {BI_L_MAX}
    model_def: MODEL_DEF
  fit: {{mode: evaluate_then_refit, engine: pymc, tune: 300, draws: 200, chains: 2, seed: 20261015}}
outputs:
  plots: [core.ppc_fan, core.convergence]
evaluate: {{metrics: [mape, coverage_90]}}
execution: {{resource_class: M, timeout: 45m}}
"""
"""The spec for ``mmm.bayesinsight`` (``MODEL_DEF`` stands for :data:`BI_MODEL_DEF` as
JSON; use :func:`bi_spec_text`)."""


def bi_spec_text(model_def: dict[str, Any] | None = None, **replace: str) -> str:
    """:data:`BI_SPEC_YAML` with the model definition filled in (plus text replacements)."""
    import json

    md = dict(BI_MODEL_DEF if model_def is None else model_def)
    md.pop("artifact", None)
    md.pop("fitted", None)
    text = BI_SPEC_YAML.replace("MODEL_DEF", json.dumps(md))
    for old, new in replace.items():
        assert old in text, old
        text = text.replace(old, new)
    return text


@dataclass(frozen=True, slots=True)
class BITruth:
    """The parameters the data was simulated with."""

    coef: dict[str, float]
    K: dict[str, float]
    n: dict[str, float]
    retention: dict[str, float]
    lag: dict[str, float]
    intercept: dict[str, float]
    price: float
    sigma: float
    season: list[float] = field(default_factory=list)


def _spend(
    rng: np.random.Generator, shape: tuple[int, ...], level: float, flights: bool
) -> np.ndarray:
    base = rng.gamma(4.0, level / 4.0, size=shape)
    if flights:
        on = rng.uniform(size=shape) < 0.7
        base = np.where(on, base, 0.0)
    return np.round(base, 2)


def bayesinsight_dataset(
    *,
    geos: tuple[str, ...] = GEOS,
    products: tuple[str, ...] = PRODUCTS,
    weeks: int = 60,
    future_weeks: int = 0,
    seed: int = 0,
    start: str = "2023-01-02",
) -> tuple[xr.Dataset, BITruth]:
    """Weekly (``W-MON``) data shaped like the BayesInsight MFF ingest, with the truth.

    ``future_weeks`` more weeks are simulated with the same draws and included at the end
    (slice them off for a forecast payload).
    """
    rng = np.random.default_rng(seed)
    total = weeks + future_weeks
    dates = pd.date_range(start, periods=total, freq="W-MON")
    shape = (len(geos), len(products), total)
    truth = BITruth(
        coef={"TV": 30.0, "Search": 20.0},
        K={"TV": 0.8, "Search": 0.9},
        n={"TV": 2.0, "Search": 1.6},
        retention={"TV": 0.5, "Search": 0.3},
        lag={"TV": 1.0, "Search": 0.0},
        intercept={g: 200.0 + 15.0 * i for i, g in enumerate(geos)},
        price=-8.0,
        sigma=5.0,
        season=[6.0, 3.0, 0.0, 1.0],
    )
    spend = {"TV": _spend(rng, shape, 50.0, True), "Search": _spend(rng, shape, 20.0, False)}
    price = np.round(rng.normal(10.0, 1.0, size=shape), 3)
    eta = np.zeros(shape)
    for g, geo in enumerate(geos):
        eta[g] += truth.intercept[geo]
    for name, x in spend.items():
        idx = nonzero_index(x[..., :weeks], "median")[..., None]
        z = x / idx
        k, n = truth.K[name], truth.n[name]
        f = z**n / (k**n + z**n)
        kind: Literal["delayed", "geometric"] = "delayed" if name == "TV" else "geometric"
        w = np.asarray(
            adstock_weights(
                kind, BI_L_MAX, {"alpha": truth.retention[name], "theta": truth.lag[name]}
            )
        )
        carried = np.zeros(shape)
        for lag in range(BI_L_MAX):
            carried[..., lag:] += w[lag] * f[..., : total - lag]
        eta += truth.coef[name] * carried
    fit_price = price[..., :weeks]
    eta += truth.price * (price - fit_price.mean()) / fit_price.std(ddof=1)
    f = fourier_features(np.arange(total), weeks, 52.18, 2)
    eta += (f @ np.asarray(truth.season))[None, None, :]
    sales = eta + rng.normal(0.0, truth.sigma, size=shape)
    coords = {"date": dates, "geo": list(geos), "product": list(products)}
    dims = ("date", "geo", "product")

    def var(a: np.ndarray) -> tuple[tuple[str, ...], np.ndarray]:
        return dims, np.moveaxis(a, -1, 0)

    ds = xr.Dataset(
        {
            "Sales": var(np.round(sales, 3)),
            "TV": var(spend["TV"]),
            "Search": var(spend["Search"]),
            "Price": var(price),
        },
        coords=coords,
    )
    return ds, truth


def bayesinsight_raw_schema(ds: xr.Dataset) -> DataSchema:
    """The schema the MFF ingest gives the synthetic data (roles from the import)."""
    var = {"dims": ["date", "geo", "product"], "dtype": "float64", "missing": "allow"}
    return DataSchema.model_validate(
        {
            "dims": {
                "date": {"kind": "time", "policy": "extend", "freq": "W-MON"},
                "geo": {
                    "kind": "categorical",
                    "policy": "new_levels",
                    "new_level_strategy": "pooled",
                },
                "product": {
                    "kind": "categorical",
                    "policy": "new_levels",
                    "new_level_strategy": "pooled",
                },
            },
            "vars": {
                "Sales": {**var, "role": "target"},
                "TV": {**var, "role": "spend"},
                "Search": {**var, "role": "spend"},
                "Price": {**var, "role": "control"},
            },
        }
    )


def bayesinsight_mff(ds: xr.Dataset) -> pd.DataFrame:
    """The dataset as an MFF long table (one row per period, key and variable)."""
    frame = ds.to_dataframe().reset_index()
    long = frame.melt(
        id_vars=["date", "geo", "product"], var_name="VariableName", value_name="VariableValue"
    )
    long = long.rename(columns={"date": "Period", "geo": "Geography", "product": "Product"})
    long["Period"] = pd.to_datetime(long["Period"]).dt.strftime("%Y-%m-%d")
    for col in ("Outlet", "Campaign", "Creative"):
        long[col] = "Total"
    cols = ["Period", "Geography", "Product", "Outlet", "Campaign", "Creative", "VariableName",
            "VariableValue"]  # fmt: skip
    return long[cols].sort_values(["Period", "Geography", "Product", "VariableName"])
