"""Reference PyMC plugins and synthetic data (used by the tests, the docs and the corpus).

* :class:`RadonModel` - E02-style varying intercepts in **long layout**: ``y(obs)`` with
  ``county_idx(obs)`` indexing ``county`` (``new_levels``, hyperprior). New counties are
  drawn from the fitted hyperprior through :meth:`BuildContext.hier_effect`.
* :class:`TrendModel` - a linear trend with optional yearly seasonality over an ``extend``
  time dim; forecasts use :meth:`BuildContext.position`, so the time axis is identical at
  fit and at forecast time.
* :class:`PanelModel` - hierarchical geo intercepts plus slopes on every numeric feature of
  a dense ``(geo, time)`` panel (the E33-shaped toy pipeline's output).
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pymc as pm
import xarray as xr
from mapi_sdk.models import ModelSettings
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.schema import DataSchema
from pydantic import Field

from mapi_pymc.context import BuildContext
from mapi_pymc.frozen import FrozenPredictorSpec
from mapi_pymc.model import PyMCModel

__all__ = [
    "RADON_SCHEMA",
    "RADON_SPEC_YAML",
    "TREND_SCHEMA",
    "TREND_SPEC_YAML",
    "PanelModel",
    "PanelSettings",
    "RadonModel",
    "RadonSettings",
    "TrendModel",
    "TrendSettings",
    "radon_dataset",
    "trend_dataset",
]

# Example specs (tiny fit settings: these double as test fixtures and documentation).
RADON_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: radon, project: demo}
data:
  dataset: dataset://demo/radon@latest
model:
  use: examples.radon@^1
  fit: {mode: train_only, seed: 3, tune: 150, draws: 150, chains: 2, engine: pymc,
        keep: {log_likelihood: true, posterior_predictive: {thin: 5}},
        prior_predictive: {draws: 40}}
evaluate: {metrics: [elpd_loo]}
"""

TREND_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: trend, project: demo}
data:
  dataset: dataset://demo/trend@latest
  split: {kind: time_holdout, dim: time, last: 6}
model:
  use: examples.trend@^1
  fit: {mode: evaluate_then_refit, seed: 5, tune: 150, draws: 150, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mape, coverage_90]}
"""

_MODES = frozenset({"predict", "forecast", "counterfactual", "evaluate", "simulate"})

RADON_SCHEMA = DataSchema.model_validate(
    {
        "layout": "long",
        "dims": {
            "obs": {"kind": "integer", "policy": "extend"},
            "county": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "hyperprior",
            },
        },
        "vars": {
            "y": {"dims": ["obs"], "dtype": "float64", "role": "target", "units": "log_pCi_L"},
            "floor": {"dims": ["obs"], "dtype": "float64", "role": "covariate"},
            "county_idx": {
                "dims": ["obs"],
                "dtype": "int64",
                "role": "index",
                "index_of": "county",
            },
        },
    }
)


class RadonSettings(ModelSettings):
    """Prior scales for the radon model."""

    beta_sigma: float = Field(1.0, gt=0)
    noise_sigma: float = Field(1.0, gt=0)
    centered: bool = False


class RadonModel(PyMCModel[RadonSettings]):
    """``y ~ Normal(a[county] + b * floor, sigma)`` with partially pooled ``a``."""

    id = "examples.radon"
    version = "1.0.0"
    Settings = RadonSettings
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=_MODES,
        deterministic_given_seed=True,
        checkpointable=True,
    )
    schema_template = RADON_SCHEMA
    outputs: ClassVar[dict[str, Any]] = {"y": {"dims": ["obs"], "units_of": "y"}}
    expected: ClassVar[dict[str, str]] = {"y": "mu"}
    default_engine = "pymc"
    pad_extend = False
    fast_score_spec = FrozenPredictorSpec(response_exprs={"y": "mu"}, input_vars=["floor"])

    def build(self, ds: xr.Dataset, s: RadonSettings, ctx: BuildContext) -> pm.Model:
        with pm.Model(coords=ctx.coords()) as model:
            floor = ctx.data("floor")
            county = ctx.data("county_idx")
            a = ctx.hier_effect("a", "county", centered=s.centered, mu_sigma=2.0)
            b = pm.Normal("b", 0.0, s.beta_sigma)
            sigma = pm.HalfNormal("sigma", s.noise_sigma)
            mu = pm.Deterministic("mu", a[county] + b * floor, dims="obs")
            pm.Normal("y", mu, sigma, observed=ctx.observed("y"), dims="obs")
        return model


def radon_dataset(
    *, counties: int = 6, per_county: int = 8, seed: int = 0, obs_start: int = 0
) -> xr.Dataset:
    """Synthetic radon-like data: county intercepts ~ N(1.5, 0.5), floor slope -0.7."""
    rng = np.random.default_rng(seed)
    a = 1.5 + 0.5 * rng.standard_normal(counties)
    idx = np.repeat(np.arange(counties), per_county)
    floor = rng.integers(0, 2, size=idx.size).astype(float)
    y = a[idx] - 0.7 * floor + 0.3 * rng.standard_normal(idx.size)
    return xr.Dataset(
        {
            "y": (("obs",), y, {"units": "log_pCi_L"}),
            "floor": (("obs",), floor),
            "county_idx": (("obs",), idx.astype(np.int64)),
        },
        coords={
            "obs": np.arange(obs_start, obs_start + idx.size),
            "county": [f"county_{i}" for i in range(counties)],
        },
    )


TREND_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "time": {"kind": "time", "policy": "extend", "freq": "W-SUN", "max_extension": 52},
        },
        "vars": {"y": {"dims": ["time"], "dtype": "float64", "role": "target", "units": "units"}},
    }
)


class TrendSettings(ModelSettings):
    """Seasonality and prior scales for the trend model."""

    period: float = Field(52.18, gt=1)
    order: int = Field(0, ge=0, le=10)
    slope_sigma: float = Field(2.0, gt=0)


class TrendModel(PyMCModel[TrendSettings]):
    """``y ~ Normal(a + b * t + fourier(t), sigma)`` with ``t`` from ``ctx.position``."""

    id = "examples.trend"
    version = "1.0.0"
    Settings = TrendSettings
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
        deterministic_given_seed=True,
    )
    schema_template = TREND_SCHEMA
    outputs: ClassVar[dict[str, Any]] = {"y": {"dims": ["time"], "units_of": "y"}}
    expected: ClassVar[dict[str, str]] = {"y": "mu"}
    default_engine = "pymc"

    def build(self, ds: xr.Dataset, s: TrendSettings, ctx: BuildContext) -> pm.Model:
        t = ctx.position("time")
        with pm.Model(coords=ctx.coords()) as model:
            tt = ctx.data("t", t, dims=["time"])
            a = pm.Normal("a", 0.0, 5.0)
            b = pm.Normal("b", 0.0, s.slope_sigma)
            mu = a + b * tt / 52.0
            if s.order:
                ang = 2 * np.pi * np.outer(t, np.arange(1, s.order + 1)) / s.period
                feats = np.concatenate([np.sin(ang), np.cos(ang)], axis=1)
                model.add_coord("fourier_mode", np.arange(2 * s.order))
                f = ctx.data("fourier", feats, dims=["time", "fourier_mode"])
                gamma = pm.Normal("gamma", 0.0, 1.0, dims="fourier_mode")
                mu = mu + pm.math.dot(f, gamma)
            mu = pm.Deterministic("mu", mu, dims="time")
            sigma = pm.HalfNormal("sigma", 1.0)
            pm.Normal("y", mu, sigma, observed=ctx.observed("y", dims=["time"]), dims="time")
        return model


def trend_dataset(*, weeks: int = 60, seed: int = 0, start: str = "2023-01-01") -> xr.Dataset:
    """Weekly (W-SUN) series ``y = 2 + 1.5 * weeks/52 + noise``."""
    rng = np.random.default_rng(seed)
    time = pd.date_range(start, periods=weeks, freq="W-SUN").as_unit("ns")
    t = np.arange(weeks) / 52.0
    y = 2.0 + 1.5 * t + 0.1 * rng.standard_normal(weeks)
    return xr.Dataset({"y": (("time",), y, {"units": "units"})}, coords={"time": time})


class PanelSettings(ModelSettings):
    """Names in the model-stage panel."""

    target: str = "y"
    group_dim: str = "geo"
    time_dim: str = "time"
    beta_sigma: float = Field(1.0, gt=0)


_PANEL_TEMPLATE: dict[str, Any] = {
    "params": {
        "target": {"type": "string", "default": "y"},
        "group_dim": {"type": "string", "default": "geo"},
        "time_dim": {"type": "string", "default": "time"},
    },
    "template": {
        "dims": {
            "{group_dim}": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "hyperprior",
            },
            "{time_dim}": {"kind": "time", "policy": "extend"},
        },
        "vars": {
            "{target}": {
                "dims": ["{group_dim}", "{time_dim}"],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
            }
        },
    },
}


class PanelModel(PyMCModel[PanelSettings]):
    """Hierarchical geo intercepts plus a slope per numeric feature (flattened over extra
    dims such as ``channel``, ``lag`` or ``fourier_mode``)."""

    id = "examples.panel"
    version = "1.0.0"
    Settings = PanelSettings
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=_MODES,
        deterministic_given_seed=True,
        checkpointable=True,
    )
    schema_template = _PANEL_TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": ["geo", "time"], "units_of": "y"}}
    expected: ClassVar[dict[str, str]] = {"y_hat": "mu"}
    predictive: ClassVar[dict[str, str]] = {"y_hat": "y"}
    default_engine = "pymc"

    def build(self, ds: xr.Dataset, s: PanelSettings, ctx: BuildContext) -> pm.Model:
        g, t = s.group_dim, s.time_dim
        x, names = panel_design(ds, s.target, g, t)
        with pm.Model(coords={**ctx.coords(), "feature": names}) as model:
            xd = ctx.data("X", x, dims=[g, t, "feature"])
            a = ctx.hier_effect("a", g)
            beta = pm.Normal("beta", 0.0, s.beta_sigma, dims="feature")
            mu = pm.Deterministic("mu", a[:, None] + pm.math.dot(xd, beta), dims=[g, t])
            sigma = pm.HalfNormal("sigma", 1.0)
            pm.Normal(
                s.target, mu, sigma, observed=ctx.observed(s.target, dims=[g, t]), dims=[g, t]
            )
        return model


def panel_design(
    ds: xr.Dataset, target: str, group_dim: str, time_dim: str
) -> tuple[np.ndarray, list[str]]:
    """``(group, time, feature)`` design matrix of every numeric non-target variable.

    Extra dims (``channel``, ``lag``, ``fourier_mode``) are flattened into one feature per
    label, named ``var[dim=label,...]`` and sorted by name, so the column order never
    depends on the payload's label order.
    """
    frame = xr.DataArray(
        np.zeros((ds.sizes[group_dim], ds.sizes[time_dim])),
        dims=[group_dim, time_dim],
        coords={group_dim: ds[group_dim].values, time_dim: ds[time_dim].values},
    )
    cols: dict[str, np.ndarray] = {}
    for v in map(str, ds.data_vars):
        da = ds[v]
        if v == target or da.dtype.kind not in "fiub" or time_dim not in da.dims:
            continue
        extra = [str(d) for d in da.dims if d not in (group_dim, time_dim)]
        labels = [
            np.asarray(ds[d].values) if d in ds.coords else np.arange(ds.sizes[d]) for d in extra
        ]
        combos = list(np.ndindex(*[len(lab) for lab in labels])) if extra else [()]
        for combo in combos:
            sel = {d: int(i) for d, i in zip(extra, combo, strict=True)}
            name = v
            if extra:
                parts = [f"{d}={labels[k][i]}" for k, (d, i) in enumerate(sel.items())]
                name = f"{v}[{','.join(parts)}]"
            col = da.isel(sel) if sel else da
            col = col.drop_vars([c for c in col.coords if c not in (group_dim, time_dim)])
            cols[name] = col.broadcast_like(frame).transpose(group_dim, time_dim).values
    names = sorted(cols)
    if not names:
        return np.zeros((*frame.shape, 0)), []
    x = np.stack([cols[n].astype(float) for n in names], axis=-1)
    return np.nan_to_num(x, nan=0.0), names
