"""``ts.hsgp_trend``: a Mauna-Loa-style (C08) HSGP trend plus Fourier seasonality.

Canonical inputs (bind dataset names onto them in the spec)::

    dims  t: time, policy extend, max_extension = settings.max_extension
    vars  y(t): target, float64, missing allowed (sentinels -> NaN via core.replace_sentinel)

The model, on a standardised target (put ``core.standardize`` in the pipeline)::

    y_t ~ Normal(mu_t, sigma)
    mu_t = intercept + slope * z_t + f(z_t) + Σ_k gamma_k · fourier_k(t)
    f ~ HSGP(kernel, lengthscale, eta) on [-c, c]

``z`` is the time position scaled so that the **anticipated** coordinates (the fit range
plus ``max_extension`` periods, :attr:`~mapi_pymc.context.BuildContext.anticipated_coords`)
map onto ``[-1, 1]``. The HSGP domain is therefore fixed at fit time and every forecast up to
``max_extension`` periods evaluates exactly the fitted functions. Requests past it are
rejected with ``HORIZON_EXCEEDS_FIT`` (409, refit required) by the platform's CoordPlan,
because the template declares ``max_extension`` on ``t``.

Missing targets (NaN after sentinel replacement) are left out of the likelihood: the fit
likelihood runs over the observed months only (dim ``t__obs``), while ``mu`` covers every
month, so the posterior also interpolates the gaps.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import FitContext, FitOutput, ModelSettings
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig
from pydantic import Field, model_validator

from mapi_plugin_ts.hsgp import HSGPDomain, Kernel, hsgp_basis, spectral_density, sqrt_eigenvalues

__all__ = ["OBS_DIM", "TARGET", "TIME_DIM", "HSGPTrendModel", "HSGPTrendSettings"]

TIME_DIM = "t"
TARGET = "y"
OBS_DIM = "t__obs"
"""Dim of the fit likelihood when some months are missing (the observed subset of ``t``)."""


class HSGPTrendSettings(ModelSettings):
    """Settings of ``ts.hsgp_trend`` (priors are on the standardised target)."""

    max_extension: int = Field(
        24,
        ge=1,
        le=10_000,
        description="Periods a forecast may reach past the fitted range without a refit; "
        "sizes the HSGP domain (x-ui-level: basic)",
    )
    kernel: Kernel = Field("expquad", description="Covariance kernel of the smooth trend")
    m: int = Field(30, ge=4, le=400, description="Number of HSGP basis functions")
    c: float = Field(1.5, gt=1.0, le=5.0, description="Boundary factor (L = c on the scaled axis)")
    lengthscale_mu: float = Field(
        0.4, gt=0, description="Median of the lengthscale prior on the scaled axis [-1, 1]"
    )
    lengthscale_sigma: float = Field(0.5, gt=0, description="Log-scale sd of the lengthscale")
    eta_sigma: float = Field(1.0, gt=0, description="Half-normal scale of the GP amplitude")
    linear_trend: bool = True
    intercept_sigma: float = Field(1.0, gt=0)
    slope_sigma: float = Field(1.0, gt=0)
    season_period: float | None = Field(
        12.0, gt=1, description="Seasonal period in periods of t (12 for monthly data)"
    )
    season_order: int = Field(3, ge=0, le=20, description="Fourier order of the seasonality")
    season_sigma: float = Field(0.5, gt=0)
    noise_sigma: float = Field(0.5, gt=0)

    @model_validator(mode="after")
    def _check(self) -> HSGPTrendSettings:
        if self.season_order and self.season_period is None:
            raise ValueError("season_order > 0 needs a season_period")
        return self


_TEMPLATE: dict[str, Any] = {
    "params": {"max_extension": {"type": "integer", "default": 24}},
    "template": {
        "dims": {
            TIME_DIM: {"kind": "time", "policy": "extend", "max_extension": "{max_extension}"},
        },
        "vars": {
            TARGET: {"dims": [TIME_DIM], "dtype": "float64", "role": "target", "missing": "allow"}
        },
    },
}


class HSGPTrendModel(PyMCModel[HSGPTrendSettings]):
    """HSGP trend + Fourier seasonality for one regularly spaced series (plan §5, C08)."""

    id = "ts.hsgp_trend"
    version = "1.0.0"
    Settings = HSGPTrendSettings
    description = (
        "A smooth Hilbert-space GP trend with optional linear drift and Fourier seasonality "
        "for one regular time series (Mauna-Loa style). Forecasts reach at most "
        "max_extension periods past the fit; further horizons need a refit."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["lengthscale", "eta", "sigma"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [TIME_DIM],
            "units_of": TARGET,
            "description": "The series: expected value (trend + seasonality) or a predictive draw",
        },
        "trend": {
            "dims": [TIME_DIM],
            "units_of": TARGET,
            "description": "The long-term component (intercept + drift + GP), no seasonality",
        },
    }
    expected: ClassVar[dict[str, str]] = {"y_hat": "mu", "trend": "trend_mu"}
    predictive: ClassVar[dict[str, str]] = {"y_hat": TARGET}
    plots = ("core.ppc_fan", "core.convergence", "core.posterior_forest", "core.decomposition")
    default_engine = "pymc"
    pad_extend = False

    # ------------------------------------------------------------------ geometry
    @staticmethod
    def domain(ctx: BuildContext, s: HSGPTrendSettings) -> HSGPDomain:
        """The HSGP domain spanning the anticipated coordinates of ``t``."""
        fit = ctx.fit_levels(TIME_DIM)
        anticipated = ctx.anticipated_coords.get(TIME_DIM)
        if anticipated is None:
            anticipated = fit
        probe = BuildContext(
            phase="score",
            ds=xr.Dataset(coords={TIME_DIM: anticipated}),
            schema=ctx.schema,
            policies=ctx.policies,
            fit_coords={TIME_DIM: fit},
        )
        return HSGPDomain.from_positions(probe.position(TIME_DIM), s.c)

    # ------------------------------------------------------------------ model
    def build(self, ds: xr.Dataset, s: HSGPTrendSettings, ctx: BuildContext) -> pm.Model:
        x = ctx.position(TIME_DIM)
        dom = self.domain(ctx, s)
        if not dom.contains(x):
            raise MapiError(
                f"{TIME_DIM!r} reaches past the HSGP domain fixed at fit (max_extension="
                f"{s.max_extension} periods); refit with a larger max_extension",
                code=ErrorCode.HORIZON_EXCEEDS_FIT,
                pointer="/horizon/periods",
            )
        z = dom.scale(x)
        phi = hsgp_basis(z, s.m, dom.L)
        omega = sqrt_eigenvalues(s.m, dom.L)
        coords = ctx.coords()
        coords["basis"] = list(range(s.m))
        with pm.Model(coords=coords) as model:
            z_t = ctx.data("z", z, dims=[TIME_DIM])
            phi_t = ctx.data("phi", phi, dims=[TIME_DIM, "basis"])
            intercept = pm.Normal("intercept", 0.0, s.intercept_sigma)
            level: Any = intercept
            if s.linear_trend:
                slope = pm.Normal("slope", 0.0, s.slope_sigma)
                level = level + slope * z_t
            ls = pm.LogNormal(
                "lengthscale", mu=math.log(s.lengthscale_mu), sigma=s.lengthscale_sigma
            )
            eta = pm.HalfNormal("eta", s.eta_sigma)
            beta = pm.Normal("beta", 0.0, 1.0, dims="basis")
            sqrt_psd = pt.sqrt(spectral_density(s.kernel, omega, ls, eta))
            gp = pm.Deterministic("gp", pt.dot(phi_t, beta * sqrt_psd), dims=TIME_DIM)
            trend = pm.Deterministic("trend_mu", level + gp, dims=TIME_DIM)
            total: Any = trend
            if s.season_order and s.season_period:
                feats = fourier_features(x, s.season_period, s.season_order)
                model.add_coord("season_mode", list(range(feats.shape[1])))
                f_t = ctx.data("season", feats, dims=[TIME_DIM, "season_mode"])
                gamma = pm.Normal("gamma", 0.0, s.season_sigma, dims="season_mode")
                total = total + pm.Deterministic("seasonality", pt.dot(f_t, gamma), dims=TIME_DIM)
            mu = pm.Deterministic("mu", total, dims=TIME_DIM)
            sigma = pm.HalfNormal("sigma", s.noise_sigma)
            self._likelihood(model, mu, sigma, ctx)
        return model

    @staticmethod
    def _likelihood(model: pm.Model, mu: Any, sigma: Any, ctx: BuildContext) -> None:
        y = ctx.observed(TARGET, dims=[TIME_DIM])
        if ctx.is_score:
            pm.Normal(TARGET, mu, sigma, observed=y, dims=TIME_DIM)
            return
        ok = np.isfinite(y)
        if not ok.any():
            raise MapiError(
                f"every value of the target {TARGET!r} is missing", code=ErrorCode.SCHEMA_INVALID
            )
        if ok.all():
            pm.Normal(TARGET, mu, sigma, observed=y, dims=TIME_DIM)
            return
        idx = np.flatnonzero(ok)
        model.add_coord(OBS_DIM, ctx.labels(TIME_DIM)[idx])
        pm.Normal(TARGET, mu[idx], sigma, observed=y[idx], dims=OBS_DIM)
        ctx.observed_vars[TARGET] = (OBS_DIM,)
        ctx.observed_values[TARGET] = y[idx]

    # ------------------------------------------------------------------ fit
    def fit(
        self, ds: xr.Dataset, s: HSGPTrendSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        out = super().fit(ds, s, cfg, ctx)
        y = np.asarray(ds[TARGET].transpose(TIME_DIM).values, dtype=float)
        observed = np.asarray(ds[TIME_DIM].values)[np.isfinite(y)]
        for name, grp in list(out.groups.items()):
            if OBS_DIM in grp.dims and OBS_DIM not in grp.coords:
                out.groups[name] = grp.assign_coords({OBS_DIM: observed})
        components = self._components(out.groups["posterior"], ctx)
        if components is not None:
            out.groups["components"] = components
        n_missing = int((~np.isfinite(y)).sum())
        out.learned["missing_periods"] = n_missing
        out.learned["max_extension"] = s.max_extension
        return out

    @staticmethod
    def _components(posterior: xr.Dataset, ctx: FitContext) -> xr.Dataset | None:
        """``trend`` and ``seasonality`` (``mu - trend``) draws in the units of the target.

        ``mu`` and ``trend_mu`` live on the model stage (standardised when the pipeline has
        ``core.standardize``); the fitted pipeline maps each back to the target's units, and
        the seasonality is the difference of the converted series (an affine inverse would
        add the mean to it twice).
        """
        if "trend_mu" not in posterior or "mu" not in posterior:
            return None
        trend, mu = posterior["trend_mu"], posterior["mu"]
        if ctx.to_original is not None:
            trend, mu = ctx.to_original(trend, TARGET), ctx.to_original(mu, TARGET)
        parts = {"trend": trend.variable.copy()}
        if "seasonality" in posterior:
            parts["seasonality"] = (mu - trend).variable
        for part in parts.values():
            part.attrs = {}
        return xr.Dataset(parts, coords=posterior["trend_mu"].coords)


def fourier_features(x: np.ndarray, period: float, order: int) -> np.ndarray:
    """``[sin(2 pi k x / P), cos(2 pi k x / P)]`` for ``k = 1..order`` (shape ``(n, 2·order)``)."""
    ang = 2.0 * np.pi * np.outer(np.asarray(x, dtype=float), np.arange(1, order + 1)) / period
    return np.concatenate([np.sin(ang), np.cos(ang)], axis=1)
