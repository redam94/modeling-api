"""``ts.markov_switching``: Hamilton regime-switching regression and autoregression.

``order = 0`` uses statsmodels ``MarkovRegression`` (regime-specific level, drivers and
variance); ``order > 0`` uses ``MarkovAutoregression``. A fitted cell is rebuilt with
``model.smooth(params)``.

Predictions are mixtures over regimes:

* in-sample, one step ahead: the regime probabilities predicted from the previous period
  weight the regime-conditional means (``predict_conditional``), each with its regime's
  variance; quantiles are those of the Gaussian mixture (solved by bisection);
* forecasts of a regression: the last filtered regime probabilities are propagated with the
  transition matrix and mixed analytically;
* forecasts of an autoregression: seeded simulation of regime paths and values
  (``simulations`` paths) - statsmodels has no forecasting for these models.

The bundle's ``regimes`` group holds the smoothed and filtered regime probabilities, the
transition matrix, expected durations and each regime's mean level.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

import numpy as np
import xarray as xr
from mapi_sdk.models import FitContext
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import Field, model_validator
from scipy import stats

from mapi_plugin_statsmodels._common import (
    TIME,
    Cell,
    CellFit,
    ExogMixin,
    StatsmodelsSettings,
    param_rows,
    quiet,
    sample_quantiles,
    schema_template,
    settings_error,
)
from mapi_plugin_statsmodels.base import StatsmodelsModel, quantile_capabilities

__all__ = ["MarkovSwitchingModel", "MarkovSwitchingSettings", "mixture_quantiles"]

REGIME = "regime"


class MarkovSwitchingSettings(StatsmodelsSettings, ExogMixin):
    """Settings of ``ts.markov_switching``."""

    k_regimes: int = Field(2, ge=2, le=4, description="Number of regimes")
    order: int = Field(0, ge=0, le=4, description="Autoregressive order (0 = no AR terms)")
    trend: Literal["n", "c", "t", "ct"] = Field("c", description="n, c (level), t or ct")
    switching_trend: bool = Field(True, description="Each regime has its own level/trend")
    switching_exog: bool = Field(True, description="Each regime has its own driver effects")
    switching_variance: bool = Field(True, description="Each regime has its own noise level")
    switching_ar: bool = Field(True, description="Each regime has its own AR coefficients")
    search_reps: int = Field(
        0,
        ge=0,
        le=50,
        description="Random restarts to avoid poor local optima (seeded)",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    simulations: int = Field(
        2000,
        ge=200,
        le=20_000,
        description="Simulated paths for forecasts of autoregressive models",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    maxiter: int = Field(100, ge=10, le=2000, json_schema_extra={"x-ui-level": "advanced"})

    @model_validator(mode="after")
    def _check(self) -> MarkovSwitchingSettings:
        if self.trend == "n" and not self.exog and self.order == 0:
            raise ValueError("a regime-switching regression needs a trend or drivers")
        return self


@contextmanager
def _seeded(seed: int) -> Iterator[None]:
    """statsmodels' random restarts draw from NumPy's global generator: seed it, then
    restore the previous state."""
    state = np.random.get_state()
    np.random.seed(seed % (2**32 - 1))
    try:
        yield
    finally:
        np.random.set_state(state)


class MarkovSwitchingModel(StatsmodelsModel[MarkovSwitchingSettings]):
    """Markov regime switching (statsmodels ``MarkovRegression``/``MarkovAutoregression``)."""

    id = "ts.markov_switching"
    Settings = MarkovSwitchingSettings
    family = "regime"
    uses_exog = True
    min_obs = 20
    description = (
        "statsmodels Markov switching: a regression or autoregression whose level, driver "
        "effects and noise switch between hidden regimes (Hamilton filter, maximum "
        "likelihood); stores regime probabilities and durations; forecasts are regime "
        "mixtures (simulated for autoregressions)."
    )
    capabilities = quantile_capabilities("custom")
    schema_template = schema_template(multivariate=False, exog=True)

    def build(self, cell: Cell, s: MarkovSwitchingSettings) -> Any:
        from statsmodels.tsa.regime_switching.markov_autoregression import MarkovAutoregression
        from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

        if cell.n < 10 * s.k_regimes + s.order:
            raise settings_error(
                f"{cell.label}: {cell.n} periods are too few for {s.k_regimes} regimes",
                "k_regimes",
            )
        with quiet():
            if s.order == 0:
                return MarkovRegression(
                    cell.endog(),
                    k_regimes=s.k_regimes,
                    trend=s.trend,
                    exog=cell.exog(),
                    switching_trend=s.switching_trend,
                    switching_exog=s.switching_exog,
                    switching_variance=s.switching_variance,
                )
            return MarkovAutoregression(
                cell.endog(),
                k_regimes=s.k_regimes,
                order=s.order,
                trend=s.trend,
                exog=cell.exog(),
                switching_ar=s.switching_ar,
                switching_trend=s.switching_trend,
                switching_exog=s.switching_exog,
                switching_variance=s.switching_variance,
            )

    def fit_cell(self, cell: Cell, s: MarkovSwitchingSettings, ctx: FitContext) -> CellFit:
        model = self.build(cell, s)
        seed = int(ctx.seed) * 7919 + zlib.crc32(cell.label.encode())
        try:
            with quiet(), _seeded(seed):
                start = self._start_params(cell, s)
                res = model.fit(
                    start_params=start, disp=False, search_reps=s.search_reps, maxiter=s.maxiter
                )
        except (RuntimeError, np.linalg.LinAlgError, ValueError) as exc:
            raise MapiError(
                f"{cell.label}: the regimes could not be estimated; try fewer regimes, "
                "switching_variance false or a longer history",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/k_regimes",
                untrusted_text=f"{type(exc).__name__}: {exc}"[:2000],
            ) from exc
        names = [str(n) for n in model.param_names]
        params = np.asarray(res.params, dtype=float)
        with quiet():
            table = param_rows(
                names, params, _safe(res, "bse"), _safe(res, "tvalues"), _safe(res, "pvalues")
            )
        mean, _ = _insample_mixture(model, res, cell, s)
        resid = cell.y - mean
        stats_ = {
            "aic": float(res.aic),
            "bic": float(res.bic),
            "hqic": float(res.hqic),
            "llf": float(res.llf),
            "nobs": float(res.nobs),
            "sigma2": float(np.nanmean(resid**2)),
        }
        retvals = getattr(res, "mle_retvals", None) or {}
        return CellFit(
            params=dict(zip(names, params.tolist(), strict=True)),
            table=table,
            stats=stats_,
            resid=resid,
            groups={"regimes": self._regimes(model, res, cell, s)},
            converged=bool(retvals.get("converged", True)),
        )

    def _start_params(self, cell: Cell, s: MarkovSwitchingSettings) -> np.ndarray:
        """Start values from a fit on the standardised series, mapped back to the data's
        scale (statsmodels' own start values put a regime level at zero, which fails on
        series far from zero)."""
        has_const = s.trend in ("c", "ct")
        center = float(np.mean(cell.y)) if has_const else 0.0
        scale = float(np.std(cell.y)) or 1.0
        zcell = Cell(
            key=cell.key,
            y=(cell.y - center) / scale,
            x=cell.x,
            exog_names=cell.exog_names,
            t=cell.t,
        )
        zmodel = self.build(zcell, s)
        zres = zmodel.fit(disp=False, search_reps=s.search_reps, maxiter=s.maxiter)
        params = np.asarray(zres.params, dtype=float).copy()
        exog = np.asarray(zmodel.exog, dtype=float) if zmodel.exog is not None else None
        done: set[int] = set()
        for i in range(s.k_regimes):
            idx = [int(j) for j in np.asarray(zmodel.parameters[i, "exog"]).reshape(-1)]
            for col, j in enumerate(idx):
                if j in done:
                    continue
                done.add(j)
                const_col = exog is not None and bool(np.all(exog[:, col] == 1.0))
                params[j] = center + scale * params[j] if const_col else scale * params[j]
            for j in np.asarray(zmodel.parameters[i, "variance"]).reshape(-1):
                if int(j) not in done:
                    done.add(int(j))
                    params[int(j)] *= scale**2
        return params

    def _regimes(self, model: Any, res: Any, cell: Cell, s: MarkovSwitchingSettings) -> xr.Dataset:
        k = s.k_regimes

        def pad(a: Any) -> np.ndarray:
            arr = np.asarray(a, dtype=float).reshape(-1, k)
            out = np.full((cell.n, k), np.nan)
            out[cell.n - arr.shape[0] :] = arr
            return out

        params = np.asarray(res.params, dtype=float)
        level = np.full(k, np.nan)
        exog = np.asarray(model.exog, dtype=float) if model.exog is not None else None
        if exog is not None and exog.size:
            for i in range(k):
                beta = params[np.asarray(model.parameters[i, "exog"])]
                level[i] = float(np.mean(exog @ beta))
        return xr.Dataset(
            {
                "smoothed_prob": ((TIME, REGIME), pad(res.smoothed_marginal_probabilities)),
                "filtered_prob": ((TIME, REGIME), pad(res.filtered_marginal_probabilities)),
                "transition": (
                    ("regime_from", "regime_to"),
                    np.asarray(res.regime_transition, dtype=float)[:, :, 0].T,
                ),
                "expected_duration": (
                    (REGIME,),
                    np.asarray(res.expected_durations, float).reshape(-1)[:k],
                ),
                "mean_level": ((REGIME,), level),
            },
            coords={
                TIME: cell.t,
                REGIME: np.arange(k),
                "regime_from": np.arange(k),
                "regime_to": np.arange(k),
            },
        )

    def rebuild(
        self,
        cell: Cell,
        s: MarkovSwitchingSettings,
        params: Mapping[str, float],
        learned: Mapping[str, Any],
    ) -> Any:
        model = self.build(cell, s)
        names = [str(n) for n in model.param_names]
        if set(names) != set(params):
            raise MapiError(
                "the stored regime-switching parameters do not match the model; refit it",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        with quiet():
            return model.smooth(np.array([params[n] for n in names], dtype=float))

    def predict_cell(
        self,
        res: Any,
        cell: Cell,
        s: MarkovSwitchingSettings,
        learned: Mapping[str, Any],
        insample: np.ndarray,
        x_future: np.ndarray | None,
        horizon: int,
        seed: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        model = res.model
        q = np.asarray(s.quantiles, dtype=float)
        _, (mu, w, sd) = _insample_mixture(model, res, cell, s)
        q_in = mixture_quantiles(mu[:, insample], w[:, insample], sd[:, insample], q)
        if not horizon:
            return q_in, np.empty((q.size, 0))
        if s.order == 0:
            mu_f, w_f, sd_f = _forecast_mixture(model, res, cell, s, x_future, horizon)
            return q_in, mixture_quantiles(mu_f, w_f, sd_f, q)
        paths = _simulate_ar(model, res, cell, s, x_future, horizon, seed)
        return q_in, sample_quantiles(paths, q)


def _safe(res: Any, attr: str) -> Any:
    try:
        return getattr(res, attr)
    except (ValueError, np.linalg.LinAlgError, ZeroDivisionError):
        return None


def _sigma(model: Any, params: np.ndarray, k: int) -> np.ndarray:
    return np.array(
        [float(np.sqrt(params[model.parameters[i, "variance"]].item())) for i in range(k)]
    )


def _insample_mixture(
    model: Any, res: Any, cell: Cell, s: MarkovSwitchingSettings
) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Mean ``(n,)`` and the mixture ``(mu, weights, sd)`` each ``(J, n)`` of the
    one-step-ahead predictions over the fitted periods (NaN before ``order``)."""
    k = s.k_regimes
    params = np.asarray(res.params, dtype=float)
    with quiet():
        cond = np.asarray(model.predict_conditional(params), dtype=float)
    probs = np.asarray(res.predicted_joint_probabilities, dtype=float)
    cond = np.broadcast_to(cond, probs.shape)
    nobs = probs.shape[-1]
    sig = _sigma(model, params, k)
    sd = np.broadcast_to(sig.reshape((k,) + (1,) * (probs.ndim - 1)), probs.shape)
    j = int(np.prod(probs.shape[:-1]))
    mu_j = np.full((j, cell.n), np.nan)
    w_j = np.full((j, cell.n), np.nan)
    sd_j = np.full((j, cell.n), np.nan)
    mu_j[:, cell.n - nobs :] = cond.reshape(j, nobs)
    w_j[:, cell.n - nobs :] = probs.reshape(j, nobs)
    sd_j[:, cell.n - nobs :] = sd.reshape(j, nobs)
    mean = np.nansum(mu_j * w_j, axis=0)
    mean[: cell.n - nobs] = np.nan
    return mean, (mu_j, w_j, sd_j)


def _design(
    model: Any, s: MarkovSwitchingSettings, x: np.ndarray | None, t: np.ndarray
) -> np.ndarray:
    """Regression rows (trend columns then drivers) at 1-based periods ``t``."""
    cols: list[np.ndarray] = []
    if s.trend in ("c", "ct"):
        cols.append(np.ones(t.size))
    if s.trend in ("t", "ct"):
        cols.append(t.astype(float))
    if x is not None:
        cols.extend(np.asarray(x, dtype=float).T)
    if not cols:
        return np.zeros((t.size, 0))
    return np.column_stack(cols)


def _beta(model: Any, params: np.ndarray, i: int) -> np.ndarray:
    idx = np.asarray(model.parameters[i, "exog"])
    return params[idx] if idx.size else np.zeros(0)


def _forecast_mixture(
    model: Any,
    res: Any,
    cell: Cell,
    s: MarkovSwitchingSettings,
    x_future: np.ndarray | None,
    horizon: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    k = s.k_regimes
    params = np.asarray(res.params, dtype=float)
    trans = np.asarray(res.regime_transition, dtype=float)[:, :, 0]  # [to, from]
    p = np.asarray(res.filtered_marginal_probabilities, dtype=float).reshape(-1, k)[-1]
    t = np.arange(cell.n + 1, cell.n + horizon + 1)
    design = _design(model, s, x_future, t)
    mu = np.stack([design @ _beta(model, params, i) for i in range(k)])  # (k, h)
    w = np.empty((k, horizon))
    for h in range(horizon):
        p = trans @ p
        w[:, h] = p
    sd = np.broadcast_to(_sigma(model, params, k)[:, None], (k, horizon))
    return mu, w, np.array(sd)


def _ar_coefs(model: Any, params: np.ndarray, i: int, order: int) -> np.ndarray:
    idx = np.asarray(model.parameters[i, "autoregressive"])
    return params[idx] if idx.size else np.zeros(order)


def _simulate_ar(
    model: Any,
    res: Any,
    cell: Cell,
    s: MarkovSwitchingSettings,
    x_future: np.ndarray | None,
    horizon: int,
    seed: int,
) -> np.ndarray:
    """``(simulations, horizon)`` simulated future values of a Markov autoregression."""
    k, order, n_sim = s.k_regimes, s.order, s.simulations
    rng = np.random.default_rng(seed)
    params = np.asarray(res.params, dtype=float)
    trans = np.asarray(res.regime_transition, dtype=float)[:, :, 0]
    joint = np.asarray(res.filtered_joint_probabilities, dtype=float)[
        ..., -1
    ]  # (S_n, ..., S_n-order)
    flat = joint.reshape(-1)
    flat = flat / flat.sum()
    draws = rng.choice(flat.size, size=n_sim, p=flat)
    hist = np.stack(np.unravel_index(draws, joint.shape), axis=1)  # (sim, order+1): S_n, S_n-1..
    sig = _sigma(model, params, k)
    betas = [_beta(model, params, i) for i in range(k)]
    phis = [_ar_coefs(model, params, i, order) for i in range(k)]
    x_hist = cell.x
    t_hist = np.arange(1, cell.n + 1)
    design_hist = _design(model, s, x_hist, t_hist)
    t_f = np.arange(cell.n + 1, cell.n + horizon + 1)
    design_f = _design(model, s, x_future, t_f)
    # deviations from the regime mean over the last `order` periods, per path
    dev = np.empty((n_sim, order))
    for lag in range(1, order + 1):
        row = design_hist[cell.n - lag]
        regimes = hist[:, lag - 1]
        means = np.array([row @ betas[i] if betas[i].size else 0.0 for i in range(k)])
        dev[:, lag - 1] = cell.y[cell.n - lag] - means[regimes]
    state = hist[:, 0]
    out = np.empty((n_sim, horizon))
    cum = np.cumsum(trans, axis=0)  # column j: cdf of the next regime given j
    for h in range(horizon):
        u = rng.uniform(size=n_sim)
        state = np.minimum((u[:, None] > cum[:, state].T).sum(axis=1), k - 1)
        means = np.array([design_f[h] @ betas[i] if betas[i].size else 0.0 for i in range(k)])
        ar = np.array([phis[i] for i in range(k)])[state]  # (sim, order)
        e = np.einsum("so,so->s", ar, dev)
        new_dev = e + sig[state] * rng.standard_normal(n_sim)
        out[:, h] = means[state] + new_dev
        dev = np.concatenate([new_dev[:, None], dev[:, :-1]], axis=1)
    return out


def mixture_quantiles(
    mu: np.ndarray, weights: np.ndarray, sd: np.ndarray, quantiles: np.ndarray
) -> np.ndarray:
    """``(Q, n)`` quantiles of Gaussian mixtures ``sum_j w_j N(mu_j, sd_j)`` per column
    (bisection to 1e-10 of the spread; columns with no weight are NaN)."""
    mu = np.asarray(mu, dtype=float)
    w = np.nan_to_num(np.asarray(weights, dtype=float))
    sd = np.asarray(sd, dtype=float)
    n = mu.shape[1]
    out = np.full((quantiles.size, n), np.nan)
    total = w.sum(axis=0)
    ok = total > 0
    if not ok.any():
        return out
    mu, w, sd = mu[:, ok], w[:, ok] / total[ok], np.where(np.isfinite(sd[:, ok]), sd[:, ok], 1.0)
    mu = np.where(np.isfinite(mu), mu, 0.0)
    lo = (mu - 10 * sd).min(axis=0)
    hi = (mu + 10 * sd).max(axis=0)
    for qi, level in enumerate(quantiles):
        a, b = lo.copy(), hi.copy()
        for _ in range(80):
            mid = (a + b) / 2
            cdf = (w * stats.norm.cdf((mid[None, :] - mu) / sd)).sum(axis=0)
            below = cdf < level
            a = np.where(below, mid, a)
            b = np.where(below, b, mid)
        out[qi, ok] = (a + b) / 2
    return out
