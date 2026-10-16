"""Small statsmodels models and data for workflow tests and examples.

The gallery templates name statsmodels plugin ids (``ts.sarimax``, ``ts.var``, ``ts.vecm``,
``ts.ets``, ``ts.theta``) that the statsmodels plugin ships. These are compact but real
stand-ins with the same ids, so workflows can be run end to end where that plugin is not
installed (tests, the docs, a laptop). They keep their training data in their learned state
and are not meant for production use. :func:`test_registry` registers them with the toy
models and the built-in steps; a worker loads it with ``--registry
mapi_workflows.testing:test_registry``.

Requires the ``runtime`` extra.
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
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, JsonValue

__all__ = [
    "EtsModel",
    "SarimaxModel",
    "ThetaModel",
    "VarModel",
    "VecmModel",
    "multi_series",
    "test_registry",
    "weekly_series",
]

QUANTILES = [0.05, 0.5, 0.95]
_TIME = "time"


def _y(ds: xr.Dataset, name: str) -> np.ndarray:
    return np.asarray(ds[name].transpose(_TIME).values, dtype=float)


def _learned_series(fitted: Fitted, key: str) -> np.ndarray:
    raw = cast(list[Any], fitted.learned[key])
    return np.array([np.nan if v is None else v for v in raw], dtype=float)


def _floats(a: np.ndarray) -> list[JsonValue]:
    return [None if not np.isfinite(v) else float(v) for v in np.asarray(a, float).reshape(-1)]


def _tree(name: str, values: np.ndarray, time: np.ndarray, quantiles: bool) -> xr.DataTree:
    if quantiles:
        pred = xr.Dataset(
            {name: (("quantile", _TIME), values)}, coords={"quantile": QUANTILES, _TIME: time}
        )
    else:
        pred = xr.Dataset({name: ((_TIME,), values)}, coords={_TIME: time})
    return xr.DataTree.from_dict({PREDICTIONS: pred})


_UNIVARIATE: dict[str, Any] = {
    "template": {
        "dims": {_TIME: {"kind": "time", "policy": "extend"}},
        "vars": {"y": {"dims": [_TIME], "dtype": "float64", "role": "target", "missing": "allow"}},
    }
}


# ==================================================================================== SARIMAX


class SarimaxSettings(ModelSettings):
    order: list[int] = Field(default_factory=lambda: [1, 0, 0], min_length=3, max_length=3)
    seasonal_order: list[int] = Field(
        default_factory=lambda: [0, 0, 0, 0], min_length=4, max_length=4
    )
    exog: list[str] = Field(default_factory=list)
    trend: str | None = "c"


def _trend(s: SarimaxSettings) -> str | None:
    if s.trend in (None, "n"):
        return None
    d = s.order[1] + s.seasonal_order[1]
    if d > 0 and s.trend == "c":
        return None
    return s.trend


def _sarimax(y: np.ndarray, exog: np.ndarray | None, s: SarimaxSettings) -> Any:
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
        cols.append(_y(ds, name))
    return np.column_stack(cols)


class SarimaxModel(CustomModel[SarimaxSettings]):
    """SARIMAX with drivers; quantile predictions (5%, 50%, 95%)."""

    id = "ts.sarimax"
    version = "1.0.0"
    Settings = SarimaxSettings
    capabilities = Capabilities(
        inference="point",
        representation=Representation.QUANTILES,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
    )
    schema_template = _UNIVARIATE
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": [_TIME], "units_of": "y"}}
    pad_extend = False
    family = "ts"

    def fit(self, ds: xr.Dataset, s: SarimaxSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        y = _y(ds, "y")
        exog = _exog(ds, s)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = _sarimax(y, exog, s).fit(disp=False)
        learned: dict[str, JsonValue] = {
            "params": _floats(np.asarray(res.params)),
            "y": _floats(y),
            "exog": None if exog is None else [_floats(row) for row in exog],
            "aic": float(res.aic),
        }
        return FitOutput(learned=learned)

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        y = _learned_series(fitted, "y")
        ex_raw = fitted.learned.get("exog")
        exog = None if ex_raw is None else np.asarray(ex_raw, dtype=float)
        params = np.asarray(fitted.learned["params"], dtype=float)
        n = len(ds[_TIME])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = _sarimax(y, exog, s).filter(params)
            if req.mode == "forecast":
                fc = res.get_forecast(steps=n, exog=_exog(ds, s))
                mean, ci = np.asarray(fc.predicted_mean), np.asarray(fc.conf_int(alpha=0.1))
            else:
                pr = res.get_prediction()
                mean = np.asarray(pr.predicted_mean)[-n:]
                ci = np.asarray(pr.conf_int(alpha=0.1))[-n:]
        q = np.stack([ci[:, 0], mean, ci[:, 1]])
        return _tree("y_hat", q, ds[_TIME].values, quantiles=True)


# ======================================================================== ETS and Theta


class EtsSettings(ModelSettings):
    trend: str | None = None
    damped_trend: bool = False
    seasonal: str | None = None
    seasonal_periods: int | None = None


class EtsModel(CustomModel[EtsSettings]):
    """Holt-Winters exponential smoothing (point forecasts)."""

    id = "ts.ets"
    version = "1.0.0"
    Settings = EtsSettings
    capabilities = Capabilities(
        inference="point",
        representation=Representation.POINT,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
    )
    schema_template = _UNIVARIATE
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": [_TIME], "units_of": "y"}}
    pad_extend = False
    family = "ts"

    def _model(self, y: np.ndarray, s: EtsSettings) -> Any:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing

        return ExponentialSmoothing(
            y,
            trend=s.trend,
            damped_trend=s.damped_trend and s.trend is not None,
            seasonal=s.seasonal,
            seasonal_periods=s.seasonal_periods,
        )

    def fit(self, ds: xr.Dataset, s: EtsSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        y = _y(ds, "y")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = self._model(y, s).fit()
        return FitOutput(
            learned={"params": {k: _scalar(v) for k, v in res.params.items()}, "y": _floats(y)}
        )

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        y = _learned_series(fitted, "y")
        n = len(ds[_TIME])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = self._model(y, s).fit()
            values = np.asarray(res.forecast(n) if req.mode == "forecast" else res.fittedvalues)
        if req.mode != "forecast":
            values = values[-n:]
        return _tree("y_hat", values, ds[_TIME].values, quantiles=False)


def _scalar(v: Any) -> JsonValue:
    if isinstance(v, np.ndarray):
        return _floats(v)
    if isinstance(v, bool | str) or v is None:
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f if np.isfinite(f) else None


class ThetaSettings(ModelSettings):
    period: int | None = None


class ThetaModel(CustomModel[ThetaSettings]):
    """The Theta method (point forecasts)."""

    id = "ts.theta"
    version = "1.0.0"
    Settings = ThetaSettings
    capabilities = Capabilities(
        inference="point",
        representation=Representation.POINT,
        score_modes=frozenset({"forecast", "evaluate"}),
    )
    schema_template = _UNIVARIATE
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": [_TIME], "units_of": "y"}}
    pad_extend = False
    family = "ts"

    def fit(self, ds: xr.Dataset, s: ThetaSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        return FitOutput(learned={"y": _floats(_y(ds, "y"))})

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        from statsmodels.tsa.forecasting.theta import ThetaModel as Theta

        s = self.parse_settings(fitted.settings)
        y = _learned_series(fitted, "y")
        n = len(ds[_TIME])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = Theta(y, period=s.period or 1, deseasonalize=bool(s.period)).fit()
            values = np.asarray(res.forecast(n))
        return _tree("y_hat", values, ds[_TIME].values, quantiles=False)


# =========================================================================== VAR and VECM


class VarSettings(ModelSettings):
    lags: int = Field(1, ge=1, le=52)
    vars: list[str] | None = None


_MULTI: dict[str, Any] = {
    "template": {"dims": {_TIME: {"kind": "time", "policy": "extend"}}, "vars": {}}
}


def _columns(ds: xr.Dataset, names: list[str] | None) -> list[str]:
    if names:
        return names
    return [str(v) for v in ds.data_vars if ds[v].dims == (_TIME,) and ds[v].dtype.kind == "f"]


class VarModel(CustomModel[VarSettings]):
    """A vector autoregression of several series (point forecasts of each)."""

    id = "ts.var"
    version = "1.0.0"
    Settings = VarSettings
    capabilities = Capabilities(
        inference="point",
        representation=Representation.POINT,
        score_modes=frozenset({"forecast"}),
    )
    schema_template = _MULTI
    pad_extend = False
    family = "ts"

    def fit(self, ds: xr.Dataset, s: VarSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        from statsmodels.tsa.api import VAR

        cols = _columns(ds, s.vars)
        mat = np.column_stack([_y(ds, c) for c in cols])
        mat = mat[np.all(np.isfinite(mat), axis=1)]
        res = VAR(mat).fit(s.lags)
        return FitOutput(
            learned={
                "vars": cast(list[JsonValue], cols),
                "coefs": _floats(res.coefs),
                "intercept": _floats(res.intercept),
                "tail": [_floats(r) for r in mat[-s.lags :]],
            }
        )

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        cols = cast(list[str], fitted.learned["vars"])
        k = len(cols)
        coefs = np.asarray(fitted.learned["coefs"], float).reshape(s.lags, k, k)
        c = np.asarray(fitted.learned["intercept"], float)
        hist = [np.asarray(r, float) for r in cast(list[Any], fitted.learned["tail"])]
        n = len(ds[_TIME])
        out = []
        for _ in range(n):
            nxt = c + sum(coefs[i] @ hist[-1 - i] for i in range(s.lags))
            out.append(nxt)
            hist.append(nxt)
        arr = np.asarray(out)
        pred = xr.Dataset(
            {f"{name}_hat": ((_TIME,), arr[:, i]) for i, name in enumerate(cols)},
            coords={_TIME: ds[_TIME].values},
        )
        return xr.DataTree.from_dict({PREDICTIONS: pred})


class VecmSettings(ModelSettings):
    coint_rank: int = Field(1, ge=0, le=10)
    k_ar_diff: int = Field(1, ge=0, le=24)
    vars: list[str] | None = None


class VecmModel(CustomModel[VecmSettings]):
    """A vector error-correction model (point forecasts)."""

    id = "ts.vecm"
    version = "1.0.0"
    Settings = VecmSettings
    capabilities = Capabilities(
        inference="point",
        representation=Representation.POINT,
        score_modes=frozenset({"forecast"}),
    )
    schema_template = _MULTI
    pad_extend = False
    family = "ts"

    def fit(self, ds: xr.Dataset, s: VecmSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        from statsmodels.tsa.vector_ar.vecm import VECM

        cols = _columns(ds, s.vars)
        mat = np.column_stack([_y(ds, c) for c in cols])
        mat = mat[np.all(np.isfinite(mat), axis=1)]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = VECM(mat, k_ar_diff=s.k_ar_diff, coint_rank=s.coint_rank).fit()
        return FitOutput(
            learned={
                "vars": cast(list[JsonValue], cols),
                "alpha": _floats(res.alpha),
                "beta": _floats(res.beta),
                "data": [_floats(r) for r in mat],
            }
        )

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        from statsmodels.tsa.vector_ar.vecm import VECM

        s = self.parse_settings(fitted.settings)
        cols = cast(list[str], fitted.learned["vars"])
        mat = np.asarray(fitted.learned["data"], float)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = VECM(mat, k_ar_diff=s.k_ar_diff, coint_rank=s.coint_rank).fit()
            arr = np.asarray(res.predict(steps=len(ds[_TIME])))
        pred = xr.Dataset(
            {f"{name}_hat": ((_TIME,), arr[:, i]) for i, name in enumerate(cols)},
            coords={_TIME: ds[_TIME].values},
        )
        return xr.DataTree.from_dict({PREDICTIONS: pred})


# ================================================================================ registry


def test_registry() -> Registry:
    """The toy registry plus the stand-in statsmodels models and the built-in steps."""
    from mapi_sdk.testing.toy import toy_registry
    from mapi_steps import builtin_steps

    reg = toy_registry()
    reg.register([SarimaxModel, EtsModel, ThetaModel, VarModel, VecmModel])
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


def multi_series(n: int = 200, *, seed: int = 3, tied: bool = True) -> xr.Dataset:
    """Three weekly series; with ``tied`` the first two share a stochastic trend."""
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
    ds = xr.Dataset({"a": (_TIME, a), "b": (_TIME, b), "c": (_TIME, c)}, coords={_TIME: t})
    ds.attrs["mapi_schema"] = _schema({"a": "target", "b": "covariate", "c": "covariate"})
    return ds
