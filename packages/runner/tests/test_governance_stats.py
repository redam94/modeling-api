"""Statistical properties of the governance checks (M3.5 review): residual whiteness on
diffuse-start models, the size and power of the permutation test, placebo channels of
sign-constrained and calibrated models, prior sensitivity of the default MMM and the
multiplicity of the noise-regressor p-values."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from gov_fixtures import fit
from mapi_runner.worker.governance import GovernanceEnv, measure
from mapi_runner.worker.refit import Refitter
from mapi_sdk.registry import default_registry
from mapi_spec.governance import CheckStatus, PolicyCheck, RefitBudget
from mapi_spec.schema import DataSchema

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

TS_SCHEMA = DataSchema.model_validate(
    {
        "dims": {"time": {"kind": "time", "policy": "extend", "freq": "W-SUN"}},
        "vars": {"y": {"dims": ["time"], "dtype": "float64", "role": "target"}},
    }
)


def ts_data(y: np.ndarray) -> xr.Dataset:
    t = pd.date_range("2020-01-05", periods=len(y), freq="W-SUN")
    return xr.Dataset({"y": ("time", np.asarray(y, float))}, coords={"time": t})


def ts_spec(use: str, settings: dict[str, Any], *, holdout: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "dataset": "dataset://p/sales@latest",
        "bind": {"dims": {"t": "time"}},
    }
    if holdout:
        data["split"] = {"kind": "time_holdout", "dim": "t", "last": holdout}
    return {
        "apiVersion": "mapi/v1",
        "kind": "ModelSpec",
        "metadata": {"name": "ts", "project": "p"},
        "data": data,
        "model": {"use": use, "settings": settings},
        "evaluate": {"metrics": ["rmse", "mae"]},
    }


def ts_env(y: np.ndarray, use: str, settings: dict[str, Any], **kw: Any) -> GovernanceEnv:
    reg = default_registry()
    data = ts_data(y)
    b = fit(ts_spec(use, settings, **kw), data, reg, schema=TS_SCHEMA)
    return GovernanceEnv(b, data, reg, Refitter(b, reg, RefitBudget(max_refits=200)))


def ar1(rng: np.random.Generator, n: int, phi: float, scale: float = 1.0) -> np.ndarray:
    e = rng.normal(scale=scale, size=n)
    out = np.zeros(n)
    for t in range(1, n):
        out[t] = phi * out[t - 1] + e[t]
    return out


# ================================================================ residuals.whiteness


def test_whiteness_drops_the_diffuse_start_of_unobserved_components() -> None:
    """Regression: the first one-step prediction of a diffuse-initialised model is 0, so the
    first residual was the level (about 500); that one outlier swamped every
    autocorrelation and strongly autocorrelated errors passed with p = 1.00."""
    rng = np.random.default_rng(0)
    n = 156
    y = 500 + 0.5 * np.arange(n) + ar1(rng, n, 0.8)
    env = ts_env(y, "ts.unobserved_components@^1", {"level": "deterministic trend"})
    r = measure(env, PolicyCheck(check="residuals.whiteness"))
    assert r.status is CheckStatus.FAIL, (r.status, r.value, r.message_plain)
    assert r.value is not None and r.value < 0.01
    # white errors around the same trend still pass
    white = 500 + 0.5 * np.arange(n) + rng.normal(size=n)
    ok = measure(
        ts_env(white, "ts.unobserved_components@^1", {"level": "deterministic trend"}),
        PolicyCheck(check="residuals.whiteness"),
    )
    assert ok.status is CheckStatus.PASS, (ok.value, ok.message_plain)
