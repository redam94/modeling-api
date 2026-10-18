"""``sci.lgcp_grid``: a log-Gaussian Cox process on a space-time count grid (E07).

Canonical inputs (bind dataset names onto them in the spec)::

    dims  period: time, policy extend, max_extension = settings.max_extension
          row, col: integer cell indices, policy fixed (rows from the south)
    vars  count(period, row, col): target, int64 event counts per cell and period

The model is rung 4 of the E07 ladder, in its "how many / where" parametrisation::

    count[t, r, c] ~ NegBinomial(rate[t, r, c], 1 / inv_phi)        (or Poisson)
    log rate[t, s] = alpha + g_t + log softmax_s(f_e(t)(s))
    g = cumsum(step) - mean over the fit periods,  step_t ~ Normal(0, sigma_g)
    f_e(s) = phi(s) @ (sqrt_psd(ell) * beta_e)   2-D HSGP, Matérn-5/2, product basis
    beta = random walk over eras anchored at one era (scale eta0; increments eta_d)

``exp(alpha + g_t)`` is the expected total of the grid in period ``t`` whatever the field
does, and the field only says where the events fall. An era groups ``era_periods``
consecutive periods (4 quarters = the notebook's years). The anchor era (the busiest fit
era by default) gets the marginal prior ``GP(0, eta0² k)``; the others are sums of
increments that walk backwards and forwards from it.

**The future is inside the model** (as in the notebook): the random-walk steps and the era
coefficients cover the fit periods plus ``max_extension`` anticipated periods. Steps after
the fit have no data, so their posterior is their prior given each draw's hyperparameters,
and every draw carries its own simulated future. A forecast evaluates the same parameters
at the requested positions, so the parameter shapes never change between fit and score.
Horizons past ``max_extension`` raise ``HORIZON_EXCEEDS_FIT`` (CoordPlan).

The spatial basis is fixed by the cell centres (``row``/``col`` labels times ``cell_km``),
which are ``fixed`` dims, so the basis is identical at fit and score time.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import scipy.optimize
import scipy.stats
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import FitContext, FitOutput, Fitted, ModelSettings, ScoreContext
from mapi_spec.capabilities import Capabilities, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, model_validator

__all__ = [
    "COL_DIM",
    "PERIOD_DIM",
    "ROW_DIM",
    "TARGET",
    "LGCPGridModel",
    "LGCPGridSettings",
    "grid_basis",
    "inverse_gamma_from_interval",
    "matern52_psd_2d",
    "walk_matrix",
]

PERIOD_DIM = "period"
ROW_DIM = "row"
COL_DIM = "col"
TARGET = "count"
STEP_DIM = "step_period"
"""Positions of the random-walk steps: the fit periods plus the anticipated ones."""
ERA_DIM = "era"
BASIS_DIM = "basis"

_ANCHOR: ContextVar[int | None] = ContextVar("lgcp_anchor_era", default=None)
"""The fit's anchor era while a score rebuilds the model (it depends on the fit data)."""


class LGCPGridSettings(ModelSettings):
    """Settings of ``sci.lgcp_grid`` (lengthscales are in km)."""

    max_extension: int = Field(
        8,
        ge=1,
        le=400,
        description="Periods a forecast may reach past the fitted range without a refit "
        "(x-ui-level: basic)",
    )
    era_periods: int = Field(
        4,
        ge=1,
        le=1000,
        description="Periods in one era: the spatial field changes from era to era "
        "(4 quarters = one map per year)",
    )
    field: Literal["era_walk", "static"] = Field(
        "era_walk",
        description="era_walk: a new map each era (a random walk of maps); static: one map "
        "for all periods (the separable model)",
    )
    anchor: Literal["busiest", "first", "last"] = Field(
        "busiest",
        description="The era whose map gets the marginal prior; the other eras walk away "
        "from it (busiest = the fit era with the most events)",
    )
    likelihood: Literal["negbin", "poisson"] = "negbin"
    cell_km: float = Field(25.0, gt=0, description="Width of one grid cell in km")
    m_row: int = Field(10, ge=2, le=60, description="HSGP basis functions along rows")
    m_col: int = Field(14, ge=2, le=60, description="HSGP basis functions along columns")
    c: float = Field(1.5, gt=1.0, le=5.0, description="HSGP boundary factor")
    ell_lower_km: float = Field(15.0, gt=0, description="5% quantile of the lengthscale prior")
    ell_upper_km: float = Field(100.0, gt=0, description="95% quantile of the lengthscale prior")
    alpha_sigma: float = Field(
        1.5, gt=0, description="Sd of the log total per period (centred on the fit mean)"
    )
    sigma_g_scale: float = Field(0.5, gt=0, description="Half-normal scale of the RW step")
    eta0_scale: float = Field(2.0, gt=0, description="Half-normal scale of the anchor map")
    eta_d_scale: float = Field(1.0, gt=0, description="Half-normal scale of the era increments")
    inv_phi_rate: float = Field(1.0, gt=0, description="Exponential rate of 1/phi (0 = Poisson)")

    @model_validator(mode="after")
    def _check(self) -> LGCPGridSettings:
        if self.ell_upper_km <= self.ell_lower_km:
            raise ValueError("ell_upper_km must be greater than ell_lower_km")
        return self


_TEMPLATE: dict[str, Any] = {
    "params": {"max_extension": {"type": "integer", "default": 8}},
    "template": {
        "dims": {
            PERIOD_DIM: {"kind": "time", "policy": "extend", "max_extension": "{max_extension}"},
            ROW_DIM: {"kind": "integer", "policy": "fixed"},
            COL_DIM: {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            TARGET: {
                "dims": [PERIOD_DIM, ROW_DIM, COL_DIM],
                "dtype": "int64",
                "role": "target",
            }
        },
    },
}

_GRID: dict[str, Any] = {
    "y": ROW_DIM,
    "x": COL_DIM,
    "frame": PERIOD_DIM,
    "origin": "lower",
    "colormap": "magma",
    "log": True,
}


# ============================================================================ maths
@functools.lru_cache(maxsize=64)
def inverse_gamma_from_interval(lower: float, upper: float) -> tuple[float, float]:
    """``(alpha, beta)`` of the inverse gamma with 5% / 95% quantiles ``lower`` / ``upper``."""

    def gap(log_ab: np.ndarray) -> np.ndarray:
        a, b = np.exp(log_ab)
        dist = scipy.stats.invgamma(a, scale=b)
        return np.array(
            [math.log(dist.ppf(0.05) / lower), math.log(dist.ppf(0.95) / upper)], dtype=float
        )

    mid = math.sqrt(lower * upper)
    guess = np.log([4.0, 3.0 * mid])
    sol = scipy.optimize.fsolve(gap, guess, full_output=True)
    a, b = (float(v) for v in np.exp(sol[0]))
    if np.abs(gap(np.log([a, b]))).max() > 1e-6:  # pragma: no cover - fsolve is robust here
        raise ValueError(f"no inverse gamma with 90% of its mass in [{lower}, {upper}]")
    return a, b


def _axis_basis(x: np.ndarray, m: int, L: float) -> tuple[np.ndarray, np.ndarray]:
    """1-D Laplacian eigenfunctions on ``[-L, L]`` and their ``sqrt(eigenvalue)``."""
    j = np.arange(1, m + 1)
    phi = np.sqrt(1.0 / L) * np.sin(np.pi * j[None, :] * (x[:, None] + L) / (2.0 * L))
    return phi, np.pi * j / (2.0 * L)


def grid_basis(
    rows: np.ndarray, cols: np.ndarray, *, cell_km: float, m_row: int, m_col: int, c: float
) -> tuple[np.ndarray, np.ndarray]:
    """The 2-D product HSGP basis at the cell centres (row-major cells).

    Returns ``phi`` of shape ``(n_row * n_col, m_row * m_col)`` and the frequency norm
    ``omega`` of each basis function. Coordinates are centred on the grid and each axis
    gets ``L = c * half-range`` (half a cell when the axis has one cell).
    """
    phis, omegas = [], []
    for labels, m in ((np.asarray(rows, float), m_row), (np.asarray(cols, float), m_col)):
        km = labels * cell_km
        lo, hi = float(km.min()), float(km.max())
        half = (hi - lo) / 2.0 or cell_km / 2.0
        phi, w = _axis_basis(km - (lo + hi) / 2.0, m, c * half)
        phis.append(phi)
        omegas.append(w)
    (pr, pc), (wr, wc) = phis, omegas
    # cell (r, c) -> r * n_col + c; basis (j, k) -> j * m_col + k
    phi2 = (pr[:, None, :, None] * pc[None, :, None, :]).reshape(
        pr.shape[0] * pc.shape[0], m_row * m_col
    )
    omega = np.sqrt(wr[:, None] ** 2 + wc[None, :] ** 2).ravel()
    return phi2, omega


def matern52_psd_2d(omega: Any, ell: Any) -> Any:
    """Spectral density of a unit-variance isotropic Matérn-5/2 kernel in two dimensions.

    ``S(w) = 2^D pi^(D/2) Γ(nu + D/2) (2 nu)^nu / (Γ(nu) l^(2 nu)) (2 nu / l² + w²)^-(nu + D/2)``
    with ``D = 2`` and ``nu = 5/2``. Works on numpy arrays and pytensor tensors.
    """
    nu, d = 2.5, 2
    const = 2.0**d * math.pi ** (d / 2) * math.gamma(nu + d / 2) * (2.0 * nu) ** nu / math.gamma(nu)
    return const * ell ** (-2.0 * nu) * (2.0 * nu / ell**2 + omega**2) ** (-(nu + d / 2))


def walk_matrix(n_era: int, anchor: int) -> np.ndarray:
    """``W[e, j] = 1`` if innovation ``j`` is part of era ``e``'s map (E07's ``WALK``).

    The anchor's innovation is in every era; eras after it add the increments
    ``anchor+1 .. e``, eras before it the (backward) increments ``e .. anchor-1``.
    """
    w = np.zeros((n_era, n_era))
    w[:, anchor] = 1.0
    for e in range(n_era):
        w[e, anchor + 1 : e + 1] = 1.0
        w[e, e:anchor] = 1.0
    return w


# ============================================================================ model
class LGCPGridModel(PyMCModel[LGCPGridSettings]):
    """Space-time LGCP for gridded counts with a 2-D HSGP map per era (E07)."""

    id = "sci.lgcp_grid"
    version = "1.0.0"
    Settings = LGCPGridSettings
    description = (
        "A log-Gaussian Cox process for event counts on a regular space-time grid: a "
        "random-walk total per period times a smooth 2-D map (Matérn-5/2 HSGP) that can "
        "change from era to era, with negative-binomial counts. Forecasts reach at most "
        "max_extension periods past the fit."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["alpha", "sigma_g", "ell", "eta0", "eta_d", "inv_phi"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "rate": {
            "dims": [PERIOD_DIM, ROW_DIM, COL_DIM],
            "units_of": TARGET,
            "description": "Events per cell and period: the expected intensity, or a "
            "predictive count draw",
            "grid": _GRID,
        },
        "total": {
            "dims": [PERIOD_DIM],
            "units_of": TARGET,
            "description": "Expected events per period in the whole grid",
        },
    }
    expected: ClassVar[dict[str, str]] = {"rate": "rate", "total": "total"}
    predictive: ClassVar[dict[str, str]] = {"rate": TARGET}
    plots = (
        "core.convergence",
        "core.posterior_forest",
        "core.ppc_fan",
        "sci.field_map",
        "sci.ensemble",
    )
    resources = ResourceHint(est_fit_seconds=120)
    default_engine = "pymc"
    prior_overlap_vars = ("sigma_g", "ell", "eta0", "eta_d", "inv_phi")

    # ------------------------------------------------------------------ geometry
    @staticmethod
    def n_anticipated(ctx: BuildContext, s: LGCPGridSettings) -> tuple[int, int]:
        """``(fit periods, fit + anticipated periods)`` along ``period``."""
        n_fit = int(ctx.fit_levels(PERIOD_DIM).size)
        return n_fit, n_fit + s.max_extension

    @staticmethod
    def choose_anchor(counts: np.ndarray, n_fit: int, s: LGCPGridSettings) -> int:
        """The anchor era from the fit counts ``(period, ...)``."""
        n_fit_era = -(-n_fit // s.era_periods)
        if s.anchor == "first":
            return 0
        if s.anchor == "last":
            return n_fit_era - 1
        per = np.nansum(np.asarray(counts, dtype=float).reshape(n_fit, -1), axis=1)
        era_tot = np.bincount(np.arange(n_fit) // s.era_periods, weights=per, minlength=n_fit_era)
        return int(np.argmax(era_tot))

    # ------------------------------------------------------------------ build
    def build(self, ds: xr.Dataset, s: LGCPGridSettings, ctx: BuildContext) -> pm.Model:
        n_fit, n_all = self.n_anticipated(ctx, s)
        pos = np.rint(ctx.position(PERIOD_DIM)).astype(int)
        if pos.size and (pos.min() < 0 or pos.max() >= n_all):
            raise MapiError(
                f"{PERIOD_DIM!r} reaches past the periods anticipated at fit (max_extension="
                f"{s.max_extension}); refit with a larger max_extension",
                code=ErrorCode.HORIZON_EXCEEDS_FIT,
                pointer="/horizon/periods",
            )
        rows, cols = ctx.labels(ROW_DIM), ctx.labels(COL_DIM)
        phi, omega = grid_basis(rows, cols, cell_km=s.cell_km, m_row=s.m_row, m_col=s.m_col, c=s.c)
        n_era = -(-n_all // s.era_periods)
        era_of = pos // s.era_periods

        y = ctx.observed(TARGET, dims=[PERIOD_DIM, ROW_DIM, COL_DIM])
        if ctx.is_fit:
            if not np.isfinite(y).all():
                raise MapiError(
                    f"the target {TARGET!r} has missing counts; the model needs every cell",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{TARGET}",
                )
            anchor = self.choose_anchor(y, n_fit, s)
            alpha_mu = math.log(max(float(y.reshape(n_fit, -1).sum(1).mean()), 0.5))
        else:
            fitted_anchor = _ANCHOR.get()
            anchor = fitted_anchor if fitted_anchor is not None else 0
            alpha_mu = 0.0  # alpha comes from the posterior at score time

        coords = ctx.coords()
        coords[STEP_DIM] = list(range(n_all))
        coords[BASIS_DIM] = list(range(phi.shape[1]))
        coords[ERA_DIM] = list(range(n_era))
        ell_a, ell_b = inverse_gamma_from_interval(s.ell_lower_km, s.ell_upper_km)
        n_row, n_col = int(rows.size), int(cols.size)

        with pm.Model(coords=coords) as model:
            # how many: log expected events per period in the whole grid
            alpha = pm.Normal("alpha", alpha_mu, s.alpha_sigma)
            sigma_g = pm.HalfNormal("sigma_g", s.sigma_g_scale)
            step = pm.Normal("step", 0.0, sigma_g, dims=STEP_DIM)
            walk = pt.cumsum(step)
            g_all = walk - walk[:n_fit].mean()
            g = pm.Deterministic("g", g_all[pos], dims=PERIOD_DIM)

            # where: log share of each cell, per era
            ell = pm.InverseGamma("ell", alpha=ell_a, beta=ell_b)
            eta0 = pm.HalfNormal("eta0", s.eta0_scale)
            sqrt_psd = pt.sqrt(matern52_psd_2d(omega, ell))
            if s.field == "static":
                z = pm.Normal("z", 0.0, 1.0, dims=BASIS_DIM)
                f = pt.dot(phi, sqrt_psd * eta0 * z)  # (cell,)
                field = pt.tile(f[None, :], (n_era, 1))  # (era, cell)
            else:
                eta_d = pm.HalfNormal("eta_d", s.eta_d_scale)
                z = pm.Normal("z", 0.0, 1.0, dims=(BASIS_DIM, ERA_DIM))
                scale = pt.switch(pt.eq(pt.arange(n_era), anchor), eta0, eta_d)
                beta = pt.dot(z * scale[None, :], walk_matrix(n_era, anchor).T)  # (basis, era)
                field = pt.dot(phi, sqrt_psd[:, None] * beta).T  # (era, cell)
            pm.Deterministic(
                "field", field.reshape((n_era, n_row, n_col)), dims=(ERA_DIM, ROW_DIM, COL_DIM)
            )
            log_share = field - pt.logsumexp(field, axis=1, keepdims=True)

            log_total = alpha + g
            pm.Deterministic("total", pt.exp(log_total), dims=PERIOD_DIM)
            log_rate = log_total[:, None] + log_share[era_of]  # (period, cell)
            rate = pm.Deterministic(
                "rate",
                pt.exp(log_rate).reshape((pos.size, n_row, n_col)),
                dims=(PERIOD_DIM, ROW_DIM, COL_DIM),
            )
            dims3 = (PERIOD_DIM, ROW_DIM, COL_DIM)
            if s.likelihood == "poisson":
                pm.Poisson(TARGET, rate, observed=y, dims=dims3)
            else:
                inv_phi = pm.Exponential("inv_phi", s.inv_phi_rate)
                pm.NegativeBinomial(TARGET, mu=rate, alpha=1.0 / inv_phi, observed=y, dims=dims3)
        return model

    # ------------------------------------------------------------------ fit / score
    def fit(
        self, ds: xr.Dataset, s: LGCPGridSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        out = super().fit(ds, s, cfg, ctx)
        n_fit = int(ds.sizes[PERIOD_DIM])
        y = np.asarray(ds[TARGET].transpose(PERIOD_DIM, ROW_DIM, COL_DIM).values, dtype=float)
        out.learned["anchor_era"] = self.choose_anchor(y, n_fit, s)
        out.learned["n_fit_periods"] = n_fit
        out.learned["n_eras"] = -(-(n_fit + s.max_extension) // s.era_periods)
        out.learned["max_extension"] = s.max_extension
        return out

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        anchor = fitted.learned.get("anchor_era")
        with _anchor(int(anchor) if isinstance(anchor, int | float) else None):
            return super().score(fitted, ds, req, ctx)


@contextmanager
def _anchor(value: int | None) -> Iterator[None]:
    token = _ANCHOR.set(value)
    try:
        yield
    finally:
        _ANCHOR.reset(token)
