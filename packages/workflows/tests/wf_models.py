"""Test models and data for the workflow runtime.

``ts.sarimax`` here is a small but real statsmodels SARIMAX plugin registered under the
contract id the gallery templates use; the statsmodels plugin component ships the
production one. It keeps the training series in its learned state so forecasts filter from
the end of the fit, returns quantile predictions (5%, 50%, 95%) and supports ``predict``,
``forecast`` and ``evaluate``.
"""

from __future__ import annotations

import warnings
from typing import Any, ClassVar, cast

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
)
from mapi_sdk.registry import Registry
from mapi_sdk.testing.toy import toy_registry
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, JsonValue

QUANTILES = [0.05, 0.5, 0.95]


class SarimaxSettings(ModelSettings):
    order: list[int] = Field(default_factory=lambda: [1, 0, 0], min_length=3, max_length=3)
    seasonal_order: list[int] = Field(
        default_factory=lambda: [0, 0, 0, 0], min_length=4, max_length=4
    )
    exog: list[str] = Field(default_factory=list)
    trend: str | None = "c"


_TEMPLATE: dict[str, Any] = {
    "template": {
        "dims": {"time": {"kind": "time", "policy": "extend"}},
        "vars": {"y": {"dims": ["time"], "dtype": "float64", "role": "target", "missing": "allow"}},
    }
}


def _trend(s: SarimaxSettings) -> str | None:
    if s.trend in (None, "n"):
        return None
    # statsmodels refuses a constant together with integration; drop it then
    d = s.order[1] + s.seasonal_order[1]
    if d > 0 and s.trend == "c":
        return None
    return s.trend


def _model(y: np.ndarray, exog: np.ndarray | None, s: SarimaxSettings) -> Any:
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    so = tuple(s.seasonal_order) if s.seasonal_order[3] > 1 else (0, 0, 0, 0)
    return SARIMAX(
        y,
        exog=exog,
        order=tuple(s.order),
        seasonal_order=so,
        trend=_trend(s),
        enforce_stationarity=False,
        enforce_invertibility=False,
    )


def _exog(ds: xr.Dataset, s: SarimaxSettings) -> np.ndarray | None:
    if not s.exog:
        return None
    cols = []
    for name in s.exog:
        if name not in ds:
            raise MapiError(f"driver {name!r} is missing", code=ErrorCode.MISSING_FUTURE_COVARIATE)
        cols.append(np.asarray(ds[name].transpose("time").values, dtype=float))
    return np.column_stack(cols)


class SarimaxModel(CustomModel[SarimaxSettings]):
    id = "ts.sarimax"
    version = "1.0.0"
    Settings = SarimaxSettings
    capabilities = Capabilities(
        inference="point",
        representation=Representation.QUANTILES,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": ["time"], "units_of": "y"}}
    pad_extend = False
    family = "ts"

    def fit(self, ds: xr.Dataset, s: SarimaxSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        y = np.asarray(ds["y"].transpose("time").values, dtype=float)
        exog = _exog(ds, s)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = _model(y, exog, s).fit(disp=False)
        learned: dict[str, JsonValue] = {
            "params": [float(v) for v in res.params],
            "y": [None if not np.isfinite(v) else float(v) for v in y],
            "exog": None if exog is None else [[float(v) for v in row] for row in exog],
            "aic": float(res.aic),
            "bic": float(res.bic),
            "time": [str(v) for v in pd.DatetimeIndex(ds["time"].values)]
            if np.issubdtype(ds["time"].dtype, np.datetime64)
            else None,
        }
        return FitOutput(learned=learned)

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        y = np.array([np.nan if v is None else v for v in cast(list[Any], fitted.learned["y"])])
        ex_raw = fitted.learned.get("exog")
        exog = None if ex_raw is None else np.asarray(ex_raw, dtype=float)
        params = np.asarray(fitted.learned["params"], dtype=float)
        n = len(ds["time"])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = _model(y, exog, s).filter(params)
            if req.mode == "forecast":
                fut = _exog(ds, s)
                fc = res.get_forecast(steps=n, exog=fut)
                mean = np.asarray(fc.predicted_mean)
                ci = np.asarray(fc.conf_int(alpha=0.1))
            else:
                # in-sample one-step-ahead predictions over the fit range (ds aligns with it)
                pr = res.get_prediction()
                mean = np.asarray(pr.predicted_mean)[-n:]
                ci = np.asarray(pr.conf_int(alpha=0.1))[-n:]
        q = np.stack([ci[:, 0], mean, ci[:, 1]])
        pred = xr.Dataset(
            {"y_hat": (("quantile", "time"), q)},
            coords={"quantile": QUANTILES, "time": ds["time"].values},
        )
        return xr.DataTree.from_dict({PREDICTIONS: pred})


def registry() -> Registry:
    """The toy registry plus ``ts.sarimax`` and the built-in steps."""
    from mapi_steps import builtin_steps

    reg = toy_registry()
    reg.register(SarimaxModel)
    reg.register(builtin_steps())
    return reg


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
    """A weekly sales series with one price driver: ``sales`` (target) and ``price``."""
    rng = np.random.default_rng(seed)
    t = pd.date_range("2022-01-02", periods=n, freq="W-SUN")
    price = 10 + np.cumsum(rng.normal(0, 0.1, n))
    e = rng.normal(0, 1, n)
    u = np.zeros(n)
    for i in range(1, n):
        u[i] = (u[i - 1] if random_walk else phi * u[i - 1]) + e[i]
    seas = season * np.sin(2 * np.pi * np.arange(n) / 52)
    sales = 100 + drift * np.arange(n) + seas - beta * (price - 10) + u
    ds = xr.Dataset({"sales": ("time", sales), "price": ("time", price)}, coords={"time": t})
    schema = DataSchema.model_validate(
        {
            "dims": {"time": {"kind": "time", "policy": "extend", "freq": "W-SUN"}},
            "vars": {
                "sales": {"dims": ["time"], "dtype": "float64", "role": "target"},
                "price": {"dims": ["time"], "dtype": "float64", "role": "covariate"},
            },
        }
    )
    ds.attrs["mapi_schema"] = schema.model_dump_json(exclude_none=True)
    return ds
