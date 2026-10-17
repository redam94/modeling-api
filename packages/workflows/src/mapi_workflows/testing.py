"""Models and data for workflow tests, examples and the docs.

The gallery templates name statsmodels plugin ids (``ts.sarimax``, ``ts.var``, ``ts.vecm``,
``ts.ets``, ``ts.theta``). :func:`test_registry` registers the real statsmodels plugin
(:mod:`mapi_plugin_statsmodels`) with the SDK's toy models and the built-in steps, so
workflows run end to end on a laptop exactly as they run in the runner; a worker loads it
with ``--registry mapi_workflows.testing:test_registry``. The data helpers simulate small
weekly series with known structure.

Requires the ``runtime`` and ``testing`` extras.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.registry import Registry
from mapi_spec.schema import DataSchema

__all__ = ["multi_series", "test_registry", "weekly_series"]

_TIME = "time"


# ================================================================================ registry


def test_registry() -> Registry:
    """The toy registry plus the statsmodels plugin's models and the built-in steps."""
    from mapi_plugin_statsmodels import PLUGINS
    from mapi_sdk.testing.toy import toy_registry
    from mapi_steps import builtin_steps

    reg = toy_registry()
    reg.register(list(PLUGINS))
    reg.register(builtin_steps())
    return reg


test_registry.__test__ = False  # type: ignore[attr-defined]


# ==================================================================================== data


def _schema(vars_: dict[str, str]) -> str:
    schema = DataSchema.model_validate(
        {
            "dims": {_TIME: {"kind": "time", "policy": "extend", "freq": "W-SUN"}},
            "vars": {k: {"dims": [_TIME], "dtype": "float64", "role": r} for k, r in vars_.items()},
        }
    )
    return schema.model_dump_json(exclude_none=True)


def weekly_series(
    n: int = 156,
    *,
    seed: int = 7,
    phi: float = 0.6,
    drift: float = 0.0,
    beta: float = 2.0,
    random_walk: bool = False,
    season: float = 0.0,
) -> xr.Dataset:
    """Weekly ``sales`` (the target) with an AR(1) or random-walk error and a ``price``
    driver."""
    rng = np.random.default_rng(seed)
    t = pd.date_range("2022-01-02", periods=n, freq="W-SUN")
    price = 10 + np.cumsum(rng.normal(0, 0.1, n))
    e = rng.normal(0, 1, n)
    u = np.zeros(n)
    for i in range(1, n):
        u[i] = (u[i - 1] if random_walk else phi * u[i - 1]) + e[i]
    seas = season * np.sin(2 * np.pi * np.arange(n) / 52)
    sales = 100 + drift * np.arange(n) + seas - beta * (price - 10) + u
    ds = xr.Dataset({"sales": (_TIME, sales), "price": (_TIME, price)}, coords={_TIME: t})
    ds.attrs["mapi_schema"] = _schema({"sales": "target", "price": "covariate"})
    return ds


def multi_series(
    n: int = 200, *, seed: int = 3, tied: bool = True, stationary: bool = False
) -> xr.Dataset:
    """Three weekly series ``value(series, time)`` (series ``a``, ``b``, ``c``: the layout of
    the multivariate models); with ``tied`` the first two share a stochastic trend; with
    ``stationary`` all three are independent stationary AR(1) series (growth rates)."""
    rng = np.random.default_rng(seed)
    t = pd.date_range("2021-01-03", periods=n, freq="W-SUN")
    common = np.cumsum(rng.normal(size=n))
    a = 10 + common + rng.normal(scale=0.5, size=n)
    b = (
        5 + 0.8 * common + rng.normal(scale=0.5, size=n)
        if tied
        else (5 + np.cumsum(rng.normal(size=n)))
    )
    c = np.cumsum(rng.normal(size=n))
    if stationary:
        e = rng.normal(size=(3, n))
        ar = np.zeros((3, n))
        for i in range(1, n):
            ar[:, i] = 0.5 * ar[:, i - 1] + e[:, i]
        a, b, c = 1.0 + ar[0], 2.0 + ar[1], ar[2]
    ds = xr.Dataset(
        {"value": (("series", _TIME), np.vstack([a, b, c]))},
        coords={"series": ["a", "b", "c"], _TIME: t},
    )
    schema = DataSchema.model_validate(
        {
            "dims": {
                _TIME: {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                "series": {"kind": "categorical", "policy": "fixed"},
            },
            "vars": {"value": {"dims": ["series", _TIME], "dtype": "float64", "role": "target"}},
        }
    )
    ds.attrs["mapi_schema"] = schema.model_dump_json(exclude_none=True)
    return ds
