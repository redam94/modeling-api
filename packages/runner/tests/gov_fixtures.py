"""Tiny datasets, fits and a worker-context stand-in for the explorer and governance tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pymc as pm
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.model import PyMCModel
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec
from mapi_sdk.models import ModelSettings
from mapi_sdk.registry import Registry, default_registry
from mapi_spec.loader import load_model_spec
from mapi_spec.schema import DataSchema
from pydantic import Field

WEEKS = 104
HOLDOUT = 12


def linear_data(*, signal: bool = True, seed: int = 0, n: int = WEEKS) -> xr.Dataset:
    """``y = 10 + 1.5 * spend - 0.8 * price + noise`` weekly (``signal=False``: pure noise)."""
    rng = np.random.default_rng(seed)
    t = pd.date_range("2022-01-02", periods=n, freq="W-SUN")
    spend = np.abs(rng.normal(size=n)) * 10
    price = rng.normal(size=n)
    noise = rng.normal(scale=0.5 if signal else 2.0, size=n)
    y = 10 + (1.5 * spend - 0.8 * price if signal else 0.0) + noise
    return xr.Dataset(
        {"y": ("time", y), "spend": ("time", spend), "price": ("time", price)},
        coords={"time": t},
    )


SCHEMA = DataSchema.model_validate(
    {
        "dims": {"time": {"kind": "time", "policy": "extend", "freq": "W-SUN"}},
        "vars": {
            "y": {"dims": ["time"], "dtype": "float64", "role": "target"},
            "spend": {"dims": ["time"], "dtype": "float64", "role": "spend"},
            "price": {"dims": ["time"], "dtype": "float64", "role": "covariate"},
        },
    }
)


def linear_spec(
    estimator: str = "sklearn.LinearRegression", *, split: bool = True
) -> dict[str, Any]:
    data: dict[str, Any] = {"dataset": "dataset://p/sales@latest"}
    if split:
        data["split"] = {"kind": "time_holdout", "dim": "time", "last": HOLDOUT}
    return {
        "apiVersion": "mapi/v1",
        "kind": "ModelSpec",
        "metadata": {"name": "lin", "project": "p"},
        "data": data,
        "model": {
            "use": "sklearn.tabular_regressor@^1",
            "settings": {"estimator": {"kind": estimator}, "rows": ["time"]},
            "fit": {"seed": 1},
        },
        "evaluate": {"metrics": ["rmse", "mae", "mape"]},
    }


def fit(
    spec: dict[str, Any],
    data: xr.Dataset,
    registry: Registry | None = None,
    schema: DataSchema = SCHEMA,
) -> Bundle:
    return fit_spec(
        load_model_spec(json.dumps(spec)),
        data,
        raw_schema=schema,
        registry=registry or default_registry(),
    )


# ----------------------------------------------------------------------- a Bayesian model


class BayesLinSettings(ModelSettings):
    priors: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {"beta": {"sigma": 2.0}, "noise": {"sigma": 2.0}}
    )


class BayesLin(PyMCModel[BayesLinSettings]):
    """``y ~ Normal(a + b * spend + c * price, sigma)`` with priors from the settings."""

    id = "test.bayes_lin"
    version = "1.0.0"
    Settings = BayesLinSettings
    schema_template: ClassVar[dict[str, Any]] = {
        "template": {
            "dims": {"time": {"kind": "time", "policy": "extend"}},
            "vars": {
                "y": {"dims": ["time"], "dtype": "float64", "role": "target"},
                "spend": {"dims": ["time"], "dtype": "float64", "role": "spend"},
                "price": {"dims": ["time"], "dtype": "float64"},
            },
        }
    }
    outputs: ClassVar[dict[str, Any]] = {"y": {"dims": ["time"], "units_of": "y"}}
    expected: ClassVar[dict[str, str]] = {"y": "mu"}
    pad_extend = False

    def build(self, ds: xr.Dataset, s: BayesLinSettings, ctx: BuildContext) -> pm.Model:
        with pm.Model(coords=ctx.coords()) as model:
            a = pm.Normal("a", 10.0, 10.0)
            b = pm.Normal("b_spend", 0.0, s.priors["beta"]["sigma"])
            c = pm.Normal("b_price", 0.0, s.priors["beta"]["sigma"])
            sigma = pm.HalfNormal("sigma", s.priors["noise"]["sigma"])
            mu = pm.Deterministic(
                "mu", a + b * ctx.data("spend") + c * ctx.data("price"), dims="time"
            )
            pm.Normal("y", mu, sigma, observed=ctx.observed("y", dims=["time"]), dims="time")
        return model


def bayes_registry() -> Registry:
    reg = default_registry().copy()  # never leak test models into the shared registry
    reg.register_model(BayesLin)
    return reg


def bayes_spec(*, draws: int = 60, tune: int = 60) -> dict[str, Any]:
    spec = linear_spec()
    spec["model"] = {
        "use": "test.bayes_lin@^1",
        "settings": {"priors": {"beta": {"sigma": 2.0}, "noise": {"sigma": 2.0}}},
        "fit": {"seed": 1, "draws": draws, "tune": tune, "chains": 2, "engine": "pymc"},
    }
    return spec


# ---------------------------------------------------------------------- worker context


class _Cancel:
    cancelled = False

    def check(self) -> None:
        return None


class FakeCtx:
    """What explorer and governance code uses of a :class:`WorkerContext`, writing outputs
    under ``root`` and recording warnings."""

    def __init__(self, root: Path, registry: Registry | None = None) -> None:
        self.root = root
        self.registry = registry or default_registry()
        self.cancel = _Cancel()
        self.warnings: list[tuple[str, str, str | None]] = []
        self.written: dict[str, Any] = {}

    def state(self, *_a: Any, **_k: Any) -> None:
        return None

    def progress(self, *_a: Any, **_k: Any) -> None:
        return None

    def log(self, _m: str) -> None:
        return None

    def warning(self, code: str, message: str, untrusted_text: str | None = None) -> None:
        self.warnings.append((code, message, untrusted_text))

    def write_json(self, name: str, obj: Any) -> Path:
        data = json.dumps(obj, sort_keys=True, allow_nan=False, default=str)
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        self.written[name] = json.loads(data)
        return path

    def has_input(self, _name: str) -> bool:
        return False
