"""Multivariate plugins: ``ts.var``, ``ts.vecm``, ``ts.varmax`` and ``ts.dynamic_factor``.

The series modelled jointly lie along the canonical ``variable`` dim of ``y``; panels loop
over ``settings.series_dims`` (one joint model per geo, for example).

* ``ts.var`` - VAR(p) by OLS, lag order by information criterion; rebuilt by
  re-estimation. Stores orthogonalised impulse responses with asymptotic 95% bands
  (``irf``), the forecast-error variance decomposition (``fevd``) and pairwise Granger
  causality F-tests (``granger``).
* ``ts.vecm`` - Johansen VECM; the cointegration rank comes from the Johansen trace (or
  max-eigenvalue) test unless set. Stores the rank test table, ``alpha``/``beta``
  (``cointegration``), impulse responses, FEVD and Granger tests; rebuilt by
  re-estimation.
* ``ts.varmax`` - VARMA(p, q) with drivers (VARX when q = 0), state space; stores impulse
  responses and FEVD.
* ``ts.dynamic_factor`` - dynamic factor model, state space; stores the smoothed factors.

Forecast intervals are Gaussian from the forecast-error covariance (VAR ``mse``, the VECM's
VAR representation, the Kalman filter for the state-space models).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import numpy as np
import xarray as xr
from mapi_sdk.models import FitContext
from pydantic import Field, field_validator, model_validator
from scipy import stats

from mapi_plugin_statsmodels._common import (
    TIME,
    VARIABLE,
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
from mapi_plugin_statsmodels.ardl import _verify
from mapi_plugin_statsmodels.base import StatsmodelsModel, quantile_capabilities
from mapi_plugin_statsmodels.statespace import StateSpaceModel, _MLEMixin

__all__ = [
    "DynamicFactorModel",
    "DynamicFactorSettings",
    "VarModel",
    "VarSettings",
    "VarmaxModel",
    "VarmaxSettings",
    "VecmModel",
    "VecmSettings",
    "fevd_from_orth",
    "irf_dataset",
]

HORIZON = "horizon"
RESPONSE = "response"
SHOCK = "shock"


class _IrfMixin(StatsmodelsSettings):
    irf_horizon: int = Field(
        12,
        ge=1,
        le=60,
        description="Periods of the impulse responses and the variance decomposition",
    )


# ================================================================================ helpers


def fevd_from_orth(orth: np.ndarray, periods: int) -> np.ndarray:
    """``[variable][horizon][shock]`` forecast-error variance shares from orthogonalised
    impulse responses ``orth[h, response, shock]`` (h = 0..), horizons 1..periods."""
    sq = np.asarray(orth, dtype=float)[:periods] ** 2  # (h, i, j)
    cum = np.cumsum(sq, axis=0)  # (h, i, j)
    total = cum.sum(axis=2, keepdims=True)
    with np.errstate(all="ignore"):
        shares = np.where(total > 0, cum / total, np.nan)
    return np.transpose(shares, (1, 0, 2))


def irf_dataset(
    orth: np.ndarray,
    variables: list[str],
    *,
    stderr: np.ndarray | None = None,
    level: float = 0.95,
) -> dict[str, xr.Dataset]:
    """``irf`` (response, shock, horizon 0..H, with bands when ``stderr`` is given) and
    ``fevd`` (variable, horizon 1..H, shock) groups."""
    orth = np.asarray(orth, dtype=float)
    h = orth.shape[0] - 1
    irf = np.transpose(orth, (1, 2, 0))  # (response, shock, horizon)
    data: dict[str, Any] = {"irf": ((RESPONSE, SHOCK, HORIZON), irf)}
    if stderr is not None:
        z = float(stats.norm.ppf(0.5 + level / 2))
        se = np.transpose(np.asarray(stderr, dtype=float), (1, 2, 0))
        data["irf_lower"] = ((RESPONSE, SHOCK, HORIZON), irf - z * se)
        data["irf_upper"] = ((RESPONSE, SHOCK, HORIZON), irf + z * se)
    irf_ds = xr.Dataset(
        data,
        coords={RESPONSE: variables, SHOCK: variables, HORIZON: np.arange(h + 1)},
        attrs={"orthogonalized": 1},
    )
    fevd = fevd_from_orth(orth, h)
    fevd_ds = xr.Dataset(
        {"share": ((VARIABLE, HORIZON, SHOCK), fevd)},
        coords={VARIABLE: variables, HORIZON: np.arange(1, h + 1), SHOCK: variables},
    )
    return {"irf": irf_ds, "fevd": fevd_ds}


def granger_dataset(variables: list[str], test: Any) -> xr.Dataset:
    """Pairwise Granger tests: ``test(caused_index, causing_index) -> (statistic, p_value)``."""
    k = len(variables)
    stat = np.full((k, k), np.nan)
    pval = np.full((k, k), np.nan)
    for i in range(k):
        for j in range(k):
            if i == j:
                continue
            try:
                with quiet():
                    st, p = test(i, j)
            except (ValueError, np.linalg.LinAlgError):
                continue
            stat[i, j], pval[i, j] = float(st), float(p)
    return xr.Dataset(
        {"statistic": (("caused", "causing"), stat), "p_value": (("caused", "causing"), pval)},
        coords={"caused": variables, "causing": variables},
    )


def _ma_mse(var_coefs: np.ndarray, sigma_u: np.ndarray, steps: int) -> np.ndarray:
    """``(steps, k, k)`` forecast-error covariance of a VAR with coefficients
    ``var_coefs[lag-1]`` and innovation covariance ``sigma_u``."""
    p, k, _ = var_coefs.shape
    phi = [np.eye(k)]
    for i in range(1, steps):
        acc = np.zeros((k, k))
        for j in range(1, min(i, p) + 1):
            acc += var_coefs[j - 1] @ phi[i - j]
        phi.append(acc)
    mse = np.zeros((steps, k, k))
    run = np.zeros((k, k))
    for i in range(steps):
        run = run + phi[i] @ sigma_u @ phi[i].T
        mse[i] = run
    return mse


def _insample(
    fitted: np.ndarray, n: int, sigma_u: np.ndarray, insample: np.ndarray, q: list[float]
) -> np.ndarray:
    full = np.full((n, fitted.shape[1]), np.nan)
    full[n - fitted.shape[0] :] = fitted
    var = np.broadcast_to(np.diag(sigma_u), full.shape)
    return normal_quantiles(full[insample], var[insample], q)


def _check_vars(cell: Cell, s: Any) -> None:
    if len(cell.variables) > 20:
        raise settings_error("multivariate models take at most 20 series along 'variable'")


# ==================================================================================== VAR


class VarSettings(_IrfMixin, ExogMixin):
    """Settings of ``ts.var``."""

    lags: int | None = Field(
        None, ge=1, le=24, description="Lag order; leave empty to choose it by the criterion"
    )
    max_lags: int = Field(8, ge=1, le=24, description="Largest lag order tried when choosing")
    ic: Literal["aic", "bic", "hqic", "fpe"] = Field(
        "aic", description="Criterion for the lag order"
    )
    trend: Literal["n", "c", "ct", "ctt"] = Field("c", description="n none, c constant, ct + trend")


class VarModel(StatsmodelsModel[VarSettings]):
    """Vector autoregression (statsmodels ``VAR``)."""

    id = "ts.var"
    Settings = VarSettings
    family = "ts_multivariate"
    multivariate = True
    uses_exog = True
    min_obs = 12
    description = (
        "statsmodels VAR(p) by least squares with the lag order chosen by AIC/BIC/HQIC/FPE; "
        "joint forecasts with intervals, orthogonalised impulse responses with 95% bands, "
        "forecast-error variance decomposition and pairwise Granger causality tests."
    )
    capabilities = quantile_capabilities("point")
    schema_template = schema_template(multivariate=True, exog=True)

    def _fit(self, cell: Cell, s: VarSettings, lags: int | None) -> Any:
        from statsmodels.tsa.api import VAR

        _check_vars(cell, s)
        k = len(cell.variables)
        model = VAR(cell.endog(), exog=cell.exog())
        with quiet():
            if lags is not None:
                if cell.n - lags < lags * k + 5:
                    raise settings_error(f"{cell.label}: too few periods for {lags} lags", "lags")
                return model.fit(lags, trend=s.trend)
            maxlags = max(1, min(s.max_lags, (cell.n - 5) // (k + 1) - 1))
            res = model.fit(maxlags=maxlags, ic=s.ic, trend=s.trend)
            if res.k_ar == 0:
                res = model.fit(1, trend=s.trend)
            return res

    def fit_cell(self, cell: Cell, s: VarSettings, ctx: FitContext) -> CellFit:
        res = self._fit(cell, s, s.lags)
        names, est = _var_params(res)
        with quiet():
            table = param_rows(
                names,
                est,
                _flat(res.stderr),
                _flat(res.tvalues),
                _flat(res.pvalues),
            )
            irf = res.irf(s.irf_horizon)
            groups = irf_dataset(irf.orth_irfs, cell.variables, stderr=irf.stderr(orth=True))
        groups["granger"] = granger_dataset(
            cell.variables,
            lambda i, j: _causality(
                res.test_causality(cell.variables[i], [cell.variables[j]], kind="f")
            ),
        )
        resid = np.full((cell.n, len(cell.variables)), np.nan)
        r = np.asarray(res.resid, dtype=float)
        resid[cell.n - r.shape[0] :] = r
        stats_ = {
            "aic": float(res.aic),
            "bic": float(res.bic),
            "hqic": float(res.hqic),
            "llf": float(res.llf),
            "nobs": float(res.nobs),
            "sigma2": float(np.mean(np.diag(np.asarray(res.sigma_u)))),
        }
        return CellFit(
            params=dict(zip(names, est.tolist(), strict=True)),
            table=table,
            stats=stats_,
            resid=resid,
            learned={"lags": int(res.k_ar), "lags_chosen_by": None if s.lags else s.ic},
            groups=groups,
            diagnostics=_stability(res, cell),
        )

    def rebuild(
        self, cell: Cell, s: VarSettings, params: Mapping[str, float], learned: Mapping[str, Any]
    ) -> Any:
        res = self._fit(cell, s, int(learned["lags"]))
        names, est = _var_params(res)
        _verify(dict(zip(names, est, strict=True)), params)
        return res

    def predict_cell(
        self,
        res: Any,
        cell: Cell,
        s: VarSettings,
        learned: Mapping[str, Any],
        insample: np.ndarray,
        x_future: np.ndarray | None,
        horizon: int,
        seed: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        q = s.quantiles
        sigma_u = np.asarray(res.sigma_u, dtype=float)
        q_in = _insample(np.asarray(res.fittedvalues, dtype=float), cell.n, sigma_u, insample, q)
        k = len(cell.variables)
        if not horizon:
            return q_in, np.empty((len(q), 0, k))
        with quiet():
            lags = int(res.k_ar)
            mean = res.forecast(cell.y[-lags:], steps=horizon, exog_future=x_future)
            mse = res.mse(horizon)
        var = np.diagonal(np.asarray(mse), axis1=1, axis2=2)
        return q_in, normal_quantiles(np.asarray(mean, dtype=float), var, q)


def _flat(frame: Any) -> np.ndarray:
    return np.asarray(frame, dtype=float).T.reshape(-1)


def _var_params(res: Any) -> tuple[list[str], np.ndarray]:
    """``<term>.<equation>`` names and values of a VAR's coefficient matrix."""
    frame = res.params
    names = [f"{term}.{eq}" for eq in frame.columns for term in frame.index]
    return names, _flat(frame)


def _causality(result: Any) -> tuple[float, float]:
    return float(result.test_statistic), float(result.pvalue)


def _stability(res: Any, cell: Cell) -> list[Any]:
    from mapi_spec.capabilities import Diagnostic

    try:
        stable = bool(res.is_stable())
    except (ValueError, np.linalg.LinAlgError, AttributeError):
        return []
    if stable:
        return []
    return [
        Diagnostic(
            code="VAR_NOT_STABLE",
            severity="warning",
            message_expert=f"the VAR on {cell.label} has roots outside the unit circle",
            message_plain="The series keep drifting apart in this model, so long forecasts "
            "explode.",
            suggested_action="Difference the series (or use ts.vecm when they trend together).",
            coords=dict(cell.key),
        )
    ]


# =================================================================================== VECM

Deterministic = Literal["n", "co", "ci", "lo", "li", "cili", "colo"]
_DET_ORDER = {"n": -1, "co": 0, "ci": 0, "lo": 1, "li": 1, "cili": 1, "colo": 1}


class VecmSettings(_IrfMixin, ExogMixin):
    """Settings of ``ts.vecm``."""

    @field_validator("rank_signif")
    @classmethod
    def _signif(cls, v: float) -> float:
        if v not in (0.01, 0.05, 0.1):
            raise ValueError("rank_signif must be 0.01, 0.05 or 0.1 (tabulated critical values)")
        return v

    k_ar_diff: int = Field(1, ge=0, le=12, description="Lagged differences in each equation")
    coint_rank: int | None = Field(
        None,
        ge=1,
        le=19,
        description="Number of long-run relationships; leave empty to take it from the "
        "Johansen test",
    )
    rank_test: Literal["trace", "maxeig"] = Field("trace", description="Johansen test statistic")
    rank_signif: float = Field(0.05, description="Significance of the rank test: 0.01, 0.05 or 0.1")
    deterministic: Deterministic = Field(
        "ci",
        description="Constant/trend: n none, co outside / ci inside the long-run relation, "
        "lo/li linear trend outside/inside, combinations",
    )


class VecmModel(StatsmodelsModel[VecmSettings]):
    """Vector error correction (statsmodels ``VECM`` with Johansen rank selection)."""

    id = "ts.vecm"
    Settings = VecmSettings
    family = "ts_multivariate"
    multivariate = True
    uses_exog = True
    min_obs = 20
    description = (
        "statsmodels VECM (Johansen maximum likelihood) for trending series tied together in "
        "the long run; the cointegration rank comes from the Johansen trace or max-eigenvalue "
        "test unless set; stores alpha/beta, the rank test table, impulse responses, FEVD and "
        "Granger tests."
    )
    capabilities = quantile_capabilities("point")
    schema_template = schema_template(multivariate=True, exog=True)

    def _rank_test(self, cell: Cell, s: VecmSettings) -> Any:
        from statsmodels.tsa.vector_ar.vecm import select_coint_rank

        with quiet():
            return select_coint_rank(
                cell.endog(),
                det_order=_DET_ORDER[s.deterministic],
                k_ar_diff=s.k_ar_diff,
                method=s.rank_test,
                signif=s.rank_signif,
            )

    def _fit(self, cell: Cell, s: VecmSettings, rank: int) -> Any:
        from statsmodels.tsa.vector_ar.vecm import VECM

        k = len(cell.variables)
        if not 1 <= rank < k:
            raise settings_error(
                f"{cell.label}: the cointegration rank must be between 1 and {k - 1}; the "
                f"Johansen test found {rank} long-run relationships"
                + (
                    " - the series are not tied together; model their changes with ts.var"
                    if rank == 0
                    else " - the series look stationary; use ts.var on the levels"
                ),
                "coint_rank",
            )
        exog = cell.exog()
        with quiet():
            model = VECM(
                cell.endog(),
                exog=None if exog is None else exog.to_numpy(),
                k_ar_diff=s.k_ar_diff,
                coint_rank=rank,
                deterministic=s.deterministic,
            )
            return model.fit()

    def fit_cell(self, cell: Cell, s: VecmSettings, ctx: FitContext) -> CellFit:
        _check_vars(cell, s)
        test = self._rank_test(cell, s)
        rank = s.coint_rank if s.coint_rank is not None else int(test.rank)
        res = self._fit(cell, s, rank)
        names, est, se, tv, pv = _vecm_params(res, cell.variables, cell.exog_names)
        table = param_rows(names, est, se, tv, pv)
        with quiet():
            irf = res.irf(s.irf_horizon)
            groups = irf_dataset(irf.orth_irfs, cell.variables)
        groups["granger"] = granger_dataset(
            cell.variables, lambda i, j: _causality(res.test_granger_causality(caused=i, causing=j))
        )
        groups["cointegration"] = _coint_dataset(test, res, cell.variables, s)
        resid = np.full((cell.n, len(cell.variables)), np.nan)
        r = np.asarray(res.resid, dtype=float)
        resid[cell.n - r.shape[0] :] = r
        sigma_u = np.asarray(res.sigma_u, dtype=float)
        nobs = float(res.nobs)
        n_par = len(names)
        llf = float(res.llf)
        stats_ = {
            "aic": -2 * llf + 2 * n_par,
            "bic": -2 * llf + np.log(nobs) * n_par,
            "hqic": -2 * llf + 2 * np.log(np.log(nobs)) * n_par,
            "llf": llf,
            "nobs": nobs,
            "sigma2": float(np.mean(np.diag(sigma_u))),
        }
        return CellFit(
            params=dict(zip(names, est.tolist(), strict=True)),
            table=table,
            stats=stats_,
            resid=resid,
            learned={
                "coint_rank": int(rank),
                "rank_from_test": s.coint_rank is None,
                "johansen_rank": int(test.rank),
            },
            groups=groups,
        )

    def rebuild(
        self, cell: Cell, s: VecmSettings, params: Mapping[str, float], learned: Mapping[str, Any]
    ) -> Any:
        res = self._fit(cell, s, int(learned["coint_rank"]))
        names, est, *_ = _vecm_params(res, cell.variables, cell.exog_names)
        _verify(dict(zip(names, est, strict=True)), params)
        return res

    def predict_cell(
        self,
        res: Any,
        cell: Cell,
        s: VecmSettings,
        learned: Mapping[str, Any],
        insample: np.ndarray,
        x_future: np.ndarray | None,
        horizon: int,
        seed: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        q = s.quantiles
        sigma_u = np.asarray(res.sigma_u, dtype=float)
        q_in = _insample(np.asarray(res.fittedvalues, dtype=float), cell.n, sigma_u, insample, q)
        k = len(cell.variables)
        if not horizon:
            return q_in, np.empty((len(q), 0, k))
        with quiet():
            mean = res.predict(steps=horizon, exog_fc=x_future)
        mse = _ma_mse(np.asarray(res.var_rep, dtype=float), sigma_u, horizon)
        var = np.diagonal(mse, axis1=1, axis2=2)
        return q_in, normal_quantiles(np.asarray(mean, dtype=float), var, q)


def _vecm_params(
    res: Any, variables: list[str], exog: list[str]
) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Named alpha, beta, gamma, deterministic and driver coefficients of a VECM."""
    names: list[str] = []
    cols: list[list[np.ndarray]] = [[], [], [], []]

    def add(labels: list[str], *arrays: Any) -> None:
        names.extend(labels)
        size = len(labels)
        for slot, arr in zip(cols, arrays, strict=False):
            slot.append(
                np.full(size, np.nan) if arr is None else np.asarray(arr, float).reshape(-1)
            )
        for slot in cols[len(arrays) :]:
            slot.append(np.full(size, np.nan))

    k = len(variables)
    r = int(res.coint_rank)
    ec = [f"ec{j + 1}" for j in range(r)]
    # (k, r) arrays flattened row-major: variable then relation
    add([f"alpha.{e}.{v}" for v in variables for e in ec],
        res.alpha, res.stderr_alpha, res.tvalues_alpha, res.pvalues_alpha)  # fmt: skip
    add([f"beta.{e}.{v}" for v in variables for e in ec],
        res.beta, res.stderr_beta, res.tvalues_beta, res.pvalues_beta)  # fmt: skip
    k_ar_diff = int(res.k_ar) - 1
    if k_ar_diff > 0:
        gamma_names = [
            f"L{lag + 1}.D.{src}.{eq}"
            for eq in variables
            for lag in range(k_ar_diff)
            for src in variables
        ]
        add(gamma_names, res.gamma, res.stderr_gamma, res.tvalues_gamma, res.pvalues_gamma)
    det = np.asarray(res.det_coef, dtype=float)
    if det.size:
        n_det = det.reshape(k, -1).shape[1]
        add([f"det{d}.{eq}" for eq in variables for d in range(n_det)],
            det, res.stderr_det_coef, res.tvalues_det_coef, res.pvalues_det_coef)  # fmt: skip
    det_c = np.asarray(res.det_coef_coint, dtype=float)
    if det_c.size:
        n_dc = det_c.reshape(-1, r).shape[0]
        add([f"coint_det{d}.{e}" for d in range(n_dc) for e in ec],
            det_c, res.stderr_det_coef_coint, res.tvalues_det_coef_coint,
            res.pvalues_det_coef_coint)  # fmt: skip
    if exog:
        coefs = np.asarray(res.exog_coefs, dtype=float).reshape(k, -1)[:, : len(exog)]
        add([f"{x}.{eq}" for eq in variables for x in exog], coefs)
    est, se, tv, pv = (np.concatenate(c) if c else np.array([]) for c in cols)
    return names, est, se, tv, pv


def _coint_dataset(test: Any, res: Any, variables: list[str], s: VecmSettings) -> xr.Dataset:
    r = int(res.coint_rank)
    tested = np.arange(len(np.asarray(test.test_stats)))
    return xr.Dataset(
        {
            "statistic": (("rank_tested",), np.asarray(test.test_stats, dtype=float)),
            "critical_value": (("rank_tested",), np.asarray(test.crit_vals, dtype=float)),
            "alpha": ((VARIABLE, "relation"), np.asarray(res.alpha, dtype=float)),
            "beta": ((VARIABLE, "relation"), np.asarray(res.beta, dtype=float)),
            "rank": ((), int(r)),
            "johansen_rank": ((), int(test.rank)),
        },
        coords={
            "rank_tested": tested,
            VARIABLE: variables,
            "relation": [f"ec{j + 1}" for j in range(r)],
        },
        attrs={"method": s.rank_test, "signif": float(s.rank_signif)},
    )


# ================================================================================= VARMAX


class VarmaxSettings(_MLEMixin, _IrfMixin, ExogMixin):
    """Settings of ``ts.varmax`` (VARX when q = 0)."""

    order: tuple[int, int] = Field((1, 0), description="(p, q): VAR lags and MA lags")
    trend: Literal["n", "c", "t", "ct"] = Field("c", description="n, c, t or ct")
    measurement_error: bool = Field(False, json_schema_extra={"x-ui-level": "advanced"})
    enforce_stationarity: bool = Field(True, json_schema_extra={"x-ui-level": "advanced"})
    enforce_invertibility: bool = Field(True, json_schema_extra={"x-ui-level": "advanced"})

    @model_validator(mode="after")
    def _order(self) -> VarmaxSettings:
        p, q = self.order
        if not (0 <= p <= 8 and 0 <= q <= 4) or p + q == 0:
            raise ValueError("order (p, q) needs p in 0..8, q in 0..4 and p + q >= 1")
        return self


class _MultiStateSpace[S: StatsmodelsSettings](StateSpaceModel[S]):
    multivariate = True
    uses_exog = True


class VarmaxModel(_MultiStateSpace[VarmaxSettings]):
    """VARMA with drivers (statsmodels ``VARMAX``)."""

    id = "ts.varmax"
    Settings = VarmaxSettings
    family = "ts_multivariate"
    min_obs = 16
    description = (
        "statsmodels VARMAX(p, q) with drivers (VARX when q = 0) estimated by maximum "
        "likelihood in state-space form; joint forecasts need future drivers; stores "
        "orthogonalised impulse responses and the variance decomposition."
    )
    capabilities = quantile_capabilities("kalman")
    schema_template = schema_template(multivariate=True, exog=True)

    def build(self, cell: Cell, s: VarmaxSettings) -> Any:
        from statsmodels.tsa.statespace.varmax import VARMAX

        _check_vars(cell, s)
        with quiet():
            return VARMAX(
                cell.endog(),
                exog=cell.exog(),
                order=s.order,
                trend=s.trend,
                measurement_error=s.measurement_error,
                enforce_stationarity=s.enforce_stationarity,
                enforce_invertibility=s.enforce_invertibility,
            )

    def extra_groups(self, res: Any, cell: Cell, s: VarmaxSettings) -> dict[str, xr.Dataset]:
        k = len(cell.variables)
        with quiet():
            cols = [
                np.asarray(
                    res.impulse_responses(s.irf_horizon, impulse=j, orthogonalized=True), float
                ).reshape(s.irf_horizon + 1, k)
                for j in range(k)
            ]
        orth = np.stack(cols, axis=2)  # (h, response, shock)
        return irf_dataset(orth, cell.variables)


class DynamicFactorSettings(_MLEMixin, ExogMixin):
    """Settings of ``ts.dynamic_factor``."""

    k_factors: int = Field(1, ge=1, le=5, description="Number of hidden common factors")
    factor_order: int = Field(1, ge=0, le=4, description="Lags in the factors' own dynamics")
    error_order: int = Field(0, ge=0, le=2, description="Lags in each series' own noise")


class DynamicFactorModel(_MultiStateSpace[DynamicFactorSettings]):
    """Dynamic factor model (statsmodels ``DynamicFactor``)."""

    id = "ts.dynamic_factor"
    Settings = DynamicFactorSettings
    family = "ts_multivariate"
    min_obs = 16
    description = (
        "statsmodels DynamicFactor: a few latent factors following a VAR drive many series; "
        "estimated by maximum likelihood in state-space form; stores the smoothed factors. "
        "Standardise the series first (core.standardize)."
    )
    capabilities = quantile_capabilities("kalman")
    schema_template = schema_template(multivariate=True, exog=True)

    def build(self, cell: Cell, s: DynamicFactorSettings) -> Any:
        from statsmodels.tsa.statespace.dynamic_factor import DynamicFactor

        _check_vars(cell, s)
        if s.k_factors >= len(cell.variables):
            raise settings_error(
                "use fewer factors than series (k_factors < number of series)", "k_factors"
            )
        with quiet():
            return DynamicFactor(
                cell.endog(),
                k_factors=s.k_factors,
                factor_order=s.factor_order,
                error_order=s.error_order,
                exog=cell.exog(),
            )

    def extra_groups(self, res: Any, cell: Cell, s: DynamicFactorSettings) -> dict[str, xr.Dataset]:
        smoothed = np.asarray(res.factors.smoothed, dtype=float).reshape(s.k_factors, cell.n)
        loadings = np.full((len(cell.variables), s.k_factors), np.nan)
        params = dict(
            zip(map(str, res.model.param_names), np.asarray(res.params, float), strict=True)
        )
        for i, v in enumerate(cell.variables):
            for f in range(s.k_factors):
                loadings[i, f] = params.get(f"loading.f{f + 1}.{v}", np.nan)
        names = [f"f{f + 1}" for f in range(s.k_factors)]
        return {
            "factors": xr.Dataset(
                {
                    "factor": (("factor_name", TIME), smoothed),
                    "loading": ((VARIABLE, "factor_name"), loadings),
                },
                coords={"factor_name": names, TIME: cell.t, VARIABLE: cell.variables},
            )
        }
