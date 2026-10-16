"""Exponential smoothing plugins: ``ts.ets`` (ETSModel) and ``ts.theta`` (ThetaModel).

* ``ts.ets`` is rebuilt with ``ETSModel.smooth(params)`` (the stored smoothing parameters
  and initial states). Intervals come from ``get_prediction``: closed form for additive
  models, seeded simulation (``simulate_repetitions`` paths) for multiplicative ones.
* ``ts.theta`` is closed form (OLS drift + simple exponential smoothing), so it is
  re-estimated on the stored data and checked against the stored ``b0``/``alpha``. Its
  in-sample predictions are the theta forecasts one step ahead with the final parameters;
  intervals follow statsmodels' ``prediction_intervals`` (the IMA(1,1) variance).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import numpy as np
from mapi_sdk.models import FitContext
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import Field, model_validator
from scipy import stats

from mapi_plugin_statsmodels._common import (
    Cell,
    CellFit,
    StatsmodelsSettings,
    data_error,
    normal_quantiles,
    param_rows,
    quiet,
    schema_template,
    settings_error,
)
from mapi_plugin_statsmodels.base import StatsmodelsModel, quantile_capabilities
from mapi_plugin_statsmodels.statespace import describe_statespace

__all__ = ["EtsModel", "EtsSettings", "ThetaModel", "ThetaSettings"]

Component = Literal["add", "mul"]


# ===================================================================================== ETS


class EtsSettings(StatsmodelsSettings):
    """Settings of ``ts.ets``."""

    error: Component = Field("add", description="Error: additive (add) or multiplicative (mul)")
    trend: Component | None = Field(None, description="Trend: none, additive or multiplicative")
    damped_trend: bool = Field(False, description="Let the trend flatten out over the horizon")
    seasonal: Component | None = Field(None, description="Season: none, additive or multiplicative")
    seasonal_periods: int | None = Field(
        None, ge=2, le=366, description="Season length in periods (e.g. 12 for monthly data)"
    )
    simulate_repetitions: int = Field(
        1000,
        ge=100,
        le=20_000,
        description="Simulated paths for intervals of multiplicative models",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    maxiter: int = Field(1000, ge=10, le=10_000, json_schema_extra={"x-ui-level": "advanced"})

    @model_validator(mode="after")
    def _check(self) -> EtsSettings:
        if self.damped_trend and self.trend is None:
            raise ValueError("damped_trend needs a trend")
        if self.seasonal is not None and self.seasonal_periods is None:
            raise ValueError("a seasonal component needs seasonal_periods")
        return self

    @property
    def multiplicative(self) -> bool:
        return "mul" in (self.error, self.trend, self.seasonal)


class EtsModel(StatsmodelsModel[EtsSettings]):
    """Error-trend-season exponential smoothing (statsmodels ``ETSModel``)."""

    id = "ts.ets"
    Settings = EtsSettings
    family = "forecast"
    min_obs = 10
    description = (
        "statsmodels ETSModel: exponential smoothing with additive or multiplicative error, "
        "trend (optionally damped) and season, estimated by maximum likelihood; one model per "
        "series of a panel; closed-form or simulated prediction intervals as quantiles."
    )
    capabilities = quantile_capabilities("custom")
    schema_template = schema_template(multivariate=False, exog=False)

    def build(self, cell: Cell, s: EtsSettings) -> Any:
        from statsmodels.tsa.exponential_smoothing.ets import ETSModel

        if s.multiplicative and not (cell.y > 0).all():
            raise data_error(
                f"{cell.label}: multiplicative components need strictly positive values; "
                "use additive components"
            )
        if s.seasonal_periods is not None and s.seasonal and cell.n < 2 * s.seasonal_periods:
            raise settings_error(
                f"{cell.label}: a season of {s.seasonal_periods} periods needs at least two "
                "full seasons of history",
                "seasonal_periods",
            )
        with quiet():
            return ETSModel(
                cell.endog(),
                error=s.error,
                trend=s.trend,
                damped_trend=s.damped_trend,
                seasonal=s.seasonal,
                seasonal_periods=s.seasonal_periods if s.seasonal else None,
            )

    def fit_cell(self, cell: Cell, s: EtsSettings, ctx: FitContext) -> CellFit:
        model = self.build(cell, s)
        with quiet():
            res = model.fit(disp=False, maxiter=s.maxiter)
        return describe_statespace(model, res, cell)

    def rebuild(
        self, cell: Cell, s: EtsSettings, params: Mapping[str, float], learned: Mapping[str, Any]
    ) -> Any:
        model = self.build(cell, s)
        names = [str(n) for n in model.param_names]
        if set(names) != set(params):
            raise MapiError(
                "the stored ETS parameters do not match the model; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        with quiet():
            return model.smooth(np.array([params[n] for n in names], dtype=float))

    def predict_cell(
        self,
        res: Any,
        cell: Cell,
        s: EtsSettings,
        learned: Mapping[str, Any],
        insample: np.ndarray,
        x_future: np.ndarray | None,
        horizon: int,
        seed: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        q = s.quantiles
        with quiet():
            fitted = np.asarray(res.fittedvalues, dtype=float)
            resid = np.asarray(res.resid, dtype=float)
            if s.error == "mul":
                # relative errors: the one-step spread scales with the level
                sd = np.sqrt(float(np.nanmean(resid**2))) * np.abs(fitted)
                var_in = sd**2
            else:
                var_in = np.full(cell.n, float(np.nanmean(resid**2)))
            q_in = normal_quantiles(fitted[insample], var_in[insample], q)
            if not horizon:
                return q_in, np.empty((len(q), 0))
            pred = res.get_prediction(
                start=cell.n,
                end=cell.n + horizon - 1,
                simulate_repetitions=s.simulate_repetitions,
                random_state=np.random.default_rng(seed).integers(2**31 - 1),
            )
            q_out = np.stack([_ets_quantile(pred, level) for level in q])
        return q_in, q_out


def _ets_quantile(pred: Any, level: float) -> np.ndarray:
    """Quantile ``level`` from an ETS ``PredictionResults`` (closed form or simulated)."""
    if np.isclose(level, 0.5):
        alpha, side = 1.0, 0
    elif level < 0.5:
        alpha, side = 2 * level, 0
    else:
        alpha, side = 2 * (1 - level), 1
    bounds = np.asarray(pred.pred_int(alpha=alpha), dtype=float)
    return bounds[:, side]


# =================================================================================== Theta


class ThetaSettings(StatsmodelsSettings):
    """Settings of ``ts.theta``."""

    period: int | None = Field(
        None,
        ge=2,
        le=366,
        description="Season length; leave empty for no seasonal adjustment",
    )
    deseasonalize: bool = Field(True, description="Remove the season before forecasting")
    use_test: bool = Field(
        True,
        description="Only remove the season when a test finds one",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    method: Literal["auto", "additive", "multiplicative"] = Field(
        "auto",
        description="How the season is removed",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    theta: float = Field(2.0, ge=1.0, le=10.0, description="Weight of the trend line (2 = classic)")
    use_mle: bool = Field(
        False,
        description="Estimate by maximum likelihood (ARIMA(0,1,1) with drift) instead of OLS + SES",
        json_schema_extra={"x-ui-level": "advanced"},
    )


class ThetaModel(StatsmodelsModel[ThetaSettings]):
    """The Theta method (statsmodels ``ThetaModel``)."""

    id = "ts.theta"
    Settings = ThetaSettings
    family = "forecast"
    min_obs = 8
    description = (
        "statsmodels ThetaModel (Assimakopoulos-Nikolopoulos theta method, equivalent to "
        "simple exponential smoothing with drift) with optional seasonal adjustment; a "
        "strong, fast baseline; one model per series of a panel."
    )
    capabilities = quantile_capabilities("custom")
    schema_template = schema_template(multivariate=False, exog=False)

    def _estimate(self, cell: Cell, s: ThetaSettings) -> Any:
        from statsmodels.tsa.forecasting.theta import ThetaModel as SmTheta

        period = s.period or 1
        deseason = s.deseasonalize and period > 1
        if deseason and cell.n < 2 * period:
            raise settings_error(
                f"{cell.label}: seasonal adjustment with period {period} needs two full "
                "seasons of history",
                "period",
            )
        if deseason and s.method == "multiplicative" and not (cell.y > 0).all():
            raise data_error(
                f"{cell.label}: multiplicative seasonal adjustment needs positive values"
            )
        with quiet():
            model = SmTheta(
                cell.endog(),
                period=period,
                deseasonalize=deseason,
                use_test=s.use_test,
                method=s.method,
            )
            return model.fit(use_mle=s.use_mle)

    def fit_cell(self, cell: Cell, s: ThetaSettings, ctx: FitContext) -> CellFit:
        res = self._estimate(cell, s)
        b0, alpha = float(res.params.iloc[0]), float(res.params.iloc[1])
        sigma2 = float(res.sigma2)
        mean, _ = _theta_insample(res, cell, s.theta)
        resid = cell.y - mean
        params = {"b0": b0, "alpha": alpha, "sigma2": sigma2}
        table = param_rows(list(params), list(params.values()))
        n = int(np.isfinite(resid).sum())
        llf = float(stats.norm.logpdf(resid[np.isfinite(resid)], scale=np.sqrt(sigma2)).sum())
        k = 3
        stats_ = {
            "aic": 2 * k - 2 * llf,
            "bic": k * np.log(n) - 2 * llf,
            "hqic": 2 * k * np.log(np.log(n)) - 2 * llf,
            "llf": llf,
            "nobs": float(cell.n),
            "sigma2": sigma2,
        }
        return CellFit(
            params=params,
            table=table,
            stats=stats_,
            resid=resid,
            learned={"deseasonalized": bool(res.model.deseasonalize)},
        )

    def rebuild(
        self, cell: Cell, s: ThetaSettings, params: Mapping[str, float], learned: Mapping[str, Any]
    ) -> Any:
        res = self._estimate(cell, s)
        got = np.array([float(res.params.iloc[0]), float(res.params.iloc[1])])
        want = np.array([params["b0"], params["alpha"]])
        if not np.allclose(got, want, rtol=1e-6, atol=1e-9):
            raise MapiError(
                "re-estimating the theta model gave different parameters than the stored fit",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        return res

    def predict_cell(
        self,
        res: Any,
        cell: Cell,
        s: ThetaSettings,
        learned: Mapping[str, Any],
        insample: np.ndarray,
        x_future: np.ndarray | None,
        horizon: int,
        seed: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        q = s.quantiles
        mean, var = _theta_insample(res, cell, s.theta)
        q_in = normal_quantiles(mean[insample], var[insample], q)
        if not horizon:
            return q_in, np.empty((len(q), 0))
        with quiet():
            fmean = np.asarray(res.forecast(horizon, theta=s.theta), dtype=float)
        alpha = float(res.params.iloc[1])
        fvar = (1 + np.arange(horizon) * (1 + (alpha - 1) ** 2)) * float(res.sigma2)
        return q_in, normal_quantiles(fmean, fvar, q)


def _theta_insample(res: Any, cell: Cell, theta: float) -> tuple[np.ndarray, np.ndarray]:
    """One-step-ahead theta forecasts over the fitted periods with the final parameters.

    The SES level is run over the (deseasonalized) series from the known initial level
    ``y[0]``; the forecast for period ``t`` uses ``t`` observations, exactly as
    ``ThetaModelResults.forecast(1)`` does for the whole sample.
    """
    b0, alpha = float(res.params.iloc[0]), float(res.params.iloc[1])
    model = res.model
    y = cell.y.astype(float)
    n = y.size
    seasonal = np.asarray(getattr(res, "_seasonal", np.array([])), dtype=float)
    period = int(model.period)
    mult = not str(model.method).startswith("add")
    if model.deseasonalize and seasonal.size:
        season = seasonal[np.arange(n) % period]
        yd = y / season if mult else y - season
    else:
        season = np.ones(n) if mult else np.zeros(n)
        yd = y
    level = np.empty(n)
    level[0] = yd[0]
    for t in range(1, n):
        level[t] = alpha * yd[t - 1] + (1 - alpha) * level[t - 1]
    t_obs = np.arange(n, dtype=float)
    with np.errstate(all="ignore"):
        drift = 1 / alpha - ((1 - alpha) ** t_obs) / alpha if alpha > 0 else np.zeros(n)
    w = (theta - 1) / theta
    fcast = level + w * b0 * drift
    mean = fcast * season if mult else fcast + season
    var = np.full(n, float(res.sigma2))
    return mean, var
