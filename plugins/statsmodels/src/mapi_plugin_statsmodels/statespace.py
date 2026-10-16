"""State-space plugins: ``ts.sarimax``, ``ts.arima`` and ``ts.unobserved_components``.

All three are statsmodels ``MLEModel`` subclasses estimated by maximum likelihood with the
Kalman filter. A fitted cell is rebuilt with ``model.filter(params)`` on the stored data, so
the bundle holds parameters and data only. In-sample predictions are the filter's one-step
predictions (``get_prediction``), forecasts come from ``get_forecast`` with the future
drivers; quantiles are Gaussian from the predicted mean and forecast-error variance.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import numpy as np
import xarray as xr
from mapi_sdk.models import FitContext
from mapi_spec.capabilities import Diagnostic
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import Field, model_validator

from mapi_plugin_statsmodels._common import (
    TIME,
    Cell,
    CellFit,
    ExogMixin,
    StatsmodelsSettings,
    normal_quantiles,
    param_rows,
    quiet,
    schema_template,
    settings_error,
)
from mapi_plugin_statsmodels.base import StatsmodelsModel, quantile_capabilities

__all__ = [
    "ArimaModel",
    "ArimaSettings",
    "SarimaxModel",
    "SarimaxSettings",
    "StateSpaceModel",
    "UnobservedComponentsModel",
    "UnobservedComponentsSettings",
    "statespace_fit",
    "statespace_predict",
]

Trend = Literal["auto", "n", "c", "t", "ct"]


class _MLEMixin(StatsmodelsSettings):
    maxiter: int = Field(
        200,
        ge=10,
        le=5000,
        description="Optimiser iterations",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    method: Literal["lbfgs", "bfgs", "powell", "nm"] = Field(
        "lbfgs", description="Likelihood optimiser", json_schema_extra={"x-ui-level": "advanced"}
    )


# ================================================================================= generic


def statespace_fit(
    model: Any, cell: Cell, *, maxiter: int, method: str, learned: dict[str, Any] | None = None
) -> tuple[Any, CellFit]:
    """Fit an ``MLEModel`` and describe it as a :class:`CellFit`."""
    with quiet():
        res = model.fit(disp=False, maxiter=maxiter, method=method)
    return res, describe_statespace(model, res, cell, learned=learned)


def describe_statespace(
    model: Any, res: Any, cell: Cell, *, learned: dict[str, Any] | None = None
) -> CellFit:
    names = [str(n) for n in model.param_names]
    params = np.asarray(res.params, dtype=float)
    with quiet():
        bse = _safe(lambda: res.bse)
        z = _safe(lambda: res.zvalues)
        p = _safe(lambda: res.pvalues)
        ci = _safe(lambda: res.conf_int(alpha=0.05))
    table = param_rows(names, params, bse, z, p, ci)
    burn = int(getattr(res, "loglikelihood_burn", 0) or 0)
    resid = np.asarray(res.resid, dtype=float).copy()
    if resid.ndim == 2 and resid.shape[0] != cell.n:
        resid = resid.T
    resid[:burn] = np.nan
    sigma2 = dict(zip(names, params.tolist(), strict=True)).get("sigma2")
    if sigma2 is None:
        finite = resid[np.isfinite(resid)]
        sigma2 = float(np.var(finite)) if finite.size else float("nan")
    retvals = getattr(res, "mle_retvals", None) or {}
    converged = bool(retvals.get("converged", True))
    stats = {
        "aic": float(res.aic),
        "bic": float(res.bic),
        "hqic": float(res.hqic),
        "llf": float(res.llf),
        "nobs": float(res.nobs),
        "sigma2": float(sigma2),
    }
    return CellFit(
        params=dict(zip(names, params.tolist(), strict=True)),
        table=table,
        stats=stats,
        resid=resid,
        learned={"burn": burn, **(learned or {})},
        converged=converged,
    )


def _safe(fn: Any) -> Any:
    try:
        return fn()
    except (ValueError, np.linalg.LinAlgError, ZeroDivisionError, AttributeError):
        return None


def rebuild_statespace(model: Any, params: Mapping[str, float]) -> Any:
    """``model.filter`` with stored parameters (checked against the model's names)."""
    names = [str(n) for n in model.param_names]
    if set(names) != set(params):
        raise MapiError(
            "the stored parameters do not match the model's parameters; refit the model",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    with quiet():
        return model.filter(np.array([params[n] for n in names], dtype=float))


def statespace_predict(
    res: Any,
    n: int,
    insample: np.ndarray,
    x_future: np.ndarray | None,
    horizon: int,
    quantiles: list[float],
) -> tuple[np.ndarray, np.ndarray]:
    """One-step in-sample and multi-step forecast quantiles of a filtered ``MLEResults``."""
    with quiet():
        pred = res.get_prediction(start=0, end=n - 1)
        mean, var = _mean_var(pred)
        q_in = normal_quantiles(mean[insample], var[insample], quantiles)
        if horizon:
            kw = {"exog": x_future} if x_future is not None else {}
            fc = res.get_forecast(steps=horizon, **kw)
            fmean, fvar = _mean_var(fc)
            q_out = normal_quantiles(fmean, fvar, quantiles)
        else:
            q_out = np.empty((len(quantiles), 0, *mean.shape[1:]))
    return q_in, q_out


def _mean_var(pred: Any) -> tuple[np.ndarray, np.ndarray]:
    mean = np.asarray(pred.predicted_mean, dtype=float)
    var = np.asarray(pred.var_pred_mean, dtype=float)
    if var.ndim == 3:  # (steps, k, k) for multivariate models
        var = np.diagonal(var, axis1=1, axis2=2)
    return mean, var.reshape(mean.shape)


class StateSpaceModel[S: StatsmodelsSettings](StatsmodelsModel[S]):
    """A statsmodels ``MLEModel`` plugin: subclasses build the model in :meth:`build`."""

    def build(self, cell: Cell, s: S) -> Any:
        raise NotImplementedError

    def extra_groups(self, res: Any, cell: Cell, s: S) -> dict[str, xr.Dataset]:
        return {}

    def fit_cell(self, cell: Cell, s: S, ctx: FitContext) -> CellFit:
        model = self.build(cell, s)
        res, fit = statespace_fit(
            model,
            cell,
            maxiter=int(getattr(s, "maxiter", 200)),
            method=str(getattr(s, "method", "lbfgs")),
        )
        fit.groups.update(self.extra_groups(res, cell, s))
        fit.diagnostics.extend(self.cell_diagnostics(res, cell, s))
        return fit

    def cell_diagnostics(self, res: Any, cell: Cell, s: S) -> list[Diagnostic]:
        return []

    def rebuild(
        self, cell: Cell, s: S, params: Mapping[str, float], learned: Mapping[str, Any]
    ) -> Any:
        return rebuild_statespace(self.build(cell, s), params)

    def predict_cell(
        self,
        res: Any,
        cell: Cell,
        s: S,
        learned: Mapping[str, Any],
        insample: np.ndarray,
        x_future: np.ndarray | None,
        horizon: int,
        seed: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        return statespace_predict(res, cell.n, insample, x_future, horizon, s.quantiles)


# ============================================================================== SARIMAX


class _OrderMixin(_MLEMixin):
    order: tuple[int, int, int] = Field(
        (1, 0, 0),
        description="(p, d, q): past values used (AR), times differenced (d), past errors "
        "used (MA)",
        json_schema_extra={"x-ui-level": "basic"},
    )
    seasonal_order: tuple[int, int, int, int] = Field(
        (0, 0, 0, 0),
        description="(P, D, Q, s): the same for the seasonal pattern repeating every s periods",
        json_schema_extra={"x-ui-level": "basic"},
    )
    trend: Trend = Field(
        "auto",
        description="Deterministic trend: n none, c constant, t linear, ct both; auto uses a "
        "constant when the series is not differenced",
    )

    @model_validator(mode="after")
    def _orders(self) -> _OrderMixin:
        p, d, q = self.order
        if not (0 <= p <= 24 and 0 <= d <= 2 and 0 <= q <= 24):
            raise ValueError("order must be (p, d, q) with p, q in 0..24 and d in 0..2")
        sp, sd, sq, period = self.seasonal_order
        if not (0 <= sp <= 4 and 0 <= sd <= 1 and 0 <= sq <= 4 and 0 <= period <= 366):
            raise ValueError("seasonal_order must be (P, D, Q, s) with P, Q in 0..4, D in 0..1")
        if (sp or sd or sq) and period < 2:
            raise ValueError("a seasonal term needs a season length s of at least 2")
        return self

    def effective_seasonal(self) -> tuple[int, int, int, int]:
        """The seasonal order, or all zeros when no seasonal term is used."""
        sp, sd, sq, period = self.seasonal_order
        return (sp, sd, sq, period) if (sp or sd or sq) else (0, 0, 0, 0)

    def effective_trend(self) -> str:
        """``trend`` with ``auto`` resolved (a constant unless the series is differenced)."""
        if self.trend != "auto":
            return self.trend
        return "c" if self.order[1] + self.effective_seasonal()[1] == 0 else "n"


class SarimaxSettings(_OrderMixin, ExogMixin):
    """Settings of ``ts.sarimax`` (ARIMA with seasonal terms and drivers)."""

    enforce_stationarity: bool = Field(True, json_schema_extra={"x-ui-level": "advanced"})
    enforce_invertibility: bool = Field(True, json_schema_extra={"x-ui-level": "advanced"})


class SarimaxModel(StateSpaceModel[SarimaxSettings]):
    """SARIMAX: seasonal ARIMA with drivers (regression with ARIMA errors)."""

    id = "ts.sarimax"
    Settings = SarimaxSettings
    family = "forecast"
    uses_exog = True
    min_obs = 10
    description = (
        "statsmodels SARIMAX: ARIMA(p,d,q)(P,D,Q)s with drivers, estimated by exact maximum "
        "likelihood with the Kalman filter; one model per series of a panel; prediction "
        "intervals as quantiles; future drivers are required for forecasts."
    )
    capabilities = quantile_capabilities("kalman")
    schema_template = schema_template(multivariate=False, exog=True, missing="allow")

    def build(self, cell: Cell, s: SarimaxSettings) -> Any:
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        _check_length(cell, s.order, s.effective_seasonal())
        with quiet():
            return SARIMAX(
                cell.endog(),
                exog=cell.exog(),
                order=s.order,
                seasonal_order=s.effective_seasonal(),
                trend=s.effective_trend(),
                enforce_stationarity=s.enforce_stationarity,
                enforce_invertibility=s.enforce_invertibility,
            )


def _check_length(
    cell: Cell, order: tuple[int, int, int], seasonal: tuple[int, int, int, int]
) -> None:
    p, d, q = order
    sp, sd, sq, period = seasonal
    need = d + sd * period + max(p + sp * period, q + sq * period) + 4
    if cell.n < need:
        raise settings_error(
            f"{cell.label}: {cell.n} periods are too few for order {order} and seasonal order "
            f"{seasonal} (about {need} are needed); use smaller orders or more history",
            "order",
        )


# ================================================================================ ARIMA


class ArimaSettings(_OrderMixin):
    """Settings of ``ts.arima`` (no drivers)."""


class ArimaModel(StateSpaceModel[ArimaSettings]):
    """ARIMA without drivers (statsmodels ``tsa.arima.model.ARIMA``)."""

    id = "ts.arima"
    Settings = ArimaSettings
    family = "forecast"
    min_obs = 10
    description = (
        "statsmodels ARIMA(p,d,q)(P,D,Q)s without drivers (state-space form, exact "
        "likelihood); one model per series of a panel; prediction intervals as quantiles."
    )
    capabilities = quantile_capabilities("kalman")
    schema_template = schema_template(multivariate=False, exog=False, missing="allow")

    def build(self, cell: Cell, s: ArimaSettings) -> Any:
        from statsmodels.tsa.arima.model import ARIMA

        _check_length(cell, s.order, s.effective_seasonal())
        trend = s.effective_trend()
        if s.order[1] + s.effective_seasonal()[1] > 0 and trend in ("c", "ct"):
            raise settings_error(
                "a constant cannot be combined with differencing in ts.arima; use trend 't' "
                "for a drift or 'n'",
                "trend",
            )
        with quiet():
            return ARIMA(
                cell.endog(), order=s.order, seasonal_order=s.effective_seasonal(), trend=trend
            )

    def fit_cell(self, cell: Cell, s: ArimaSettings, ctx: FitContext) -> CellFit:
        model = self.build(cell, s)
        with quiet():
            res = model.fit(
                method="statespace",
                method_kwargs={"maxiter": s.maxiter, "disp": False, "method": s.method},
            )
        return describe_statespace(model, res, cell)


# ================================================================ UnobservedComponents

Level = Literal[
    "irregular",
    "fixed intercept",
    "deterministic constant",
    "local level",
    "random walk",
    "fixed slope",
    "deterministic trend",
    "local linear deterministic trend",
    "random walk with drift",
    "local linear trend",
    "smooth trend",
    "random trend",
]


class UnobservedComponentsSettings(_MLEMixin, ExogMixin):
    """Settings of ``ts.unobserved_components`` (structural time-series model)."""

    level: Level = Field(
        "local linear trend",
        description="How the level and trend move: 'local level' (level wanders), 'local "
        "linear trend' (level and slope wander), 'smooth trend', 'deterministic trend', ...",
        json_schema_extra={"x-ui-level": "basic"},
    )
    seasonal: int | None = Field(
        None, ge=2, le=366, description="Season length in periods (e.g. 52 for weekly data)"
    )
    stochastic_seasonal: bool = Field(True, description="Let the seasonal pattern change slowly")
    cycle: bool = Field(False, description="Add a cycle (business-cycle like swings)")
    stochastic_cycle: bool = Field(False, json_schema_extra={"x-ui-level": "advanced"})
    damped_cycle: bool = Field(False, json_schema_extra={"x-ui-level": "advanced"})
    autoregressive: int | None = Field(
        None, ge=1, le=4, description="Order of an AR term for the remaining noise"
    )


class UnobservedComponentsModel(StateSpaceModel[UnobservedComponentsSettings]):
    """Trend + season + noise (statsmodels ``UnobservedComponents``)."""

    id = "ts.unobserved_components"
    Settings = UnobservedComponentsSettings
    family = "forecast"
    uses_exog = True
    min_obs = 12
    description = (
        "statsmodels UnobservedComponents: a structural model with a level/trend, optional "
        "stochastic seasonal, cycle, AR noise and drivers, estimated by maximum likelihood; "
        "smoothed components are stored with the fit."
    )
    capabilities = quantile_capabilities("kalman")
    schema_template = schema_template(multivariate=False, exog=True, missing="allow")

    def build(self, cell: Cell, s: UnobservedComponentsSettings) -> Any:
        from statsmodels.tsa.statespace.structural import UnobservedComponents

        if s.seasonal is not None and cell.n < 2 * s.seasonal + 2:
            raise settings_error(
                f"{cell.label}: a season of {s.seasonal} periods needs at least two full "
                f"seasons of history ({2 * s.seasonal + 2} periods)",
                "seasonal",
            )
        with quiet():
            return UnobservedComponents(
                cell.endog(),
                level=s.level,
                exog=cell.exog(),
                seasonal=s.seasonal,
                stochastic_seasonal=s.stochastic_seasonal,
                cycle=s.cycle,
                stochastic_cycle=s.stochastic_cycle,
                damped_cycle=s.damped_cycle,
                autoregressive=s.autoregressive,
            )

    def extra_groups(
        self, res: Any, cell: Cell, s: UnobservedComponentsSettings
    ) -> dict[str, xr.Dataset]:
        comps: dict[str, Any] = {}
        for name in ("level", "trend", "seasonal", "cycle", "autoregressive"):
            part = getattr(res, name, None)
            if part is None:
                continue
            smoothed = (
                part["smoothed"] if isinstance(part, dict) else getattr(part, "smoothed", None)
            )
            if smoothed is None:
                continue
            arr = np.asarray(smoothed, dtype=float).reshape(-1)
            if arr.size == cell.n:
                comps[name] = ((TIME,), arr)
        if not comps:
            return {}
        return {"components": xr.Dataset(comps, coords={TIME: cell.t})}
