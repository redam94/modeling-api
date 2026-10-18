"""``sci.fisher_kpp``: a scratch assay as a Fisher-KPP reaction-diffusion PDE (E50).

Canonical inputs (bind dataset names onto them in the spec)::

    dims  t: integer, policy extend, max_extension = settings.max_extension   (hours)
          x: float, policy fixed                                              (um, column centres)
    vars  count(t, x): target, int64 - cells counted in each column at each time

The model, for the cell density ``u(x, t)`` relative to the carrying capacity ``K``::

    du/dt = a(t) * (D * d2u/dx2 + lam * u * (1 - u)),    a(t) = tanh(beta * t) or 1
    count[t, x] ~ Poisson(K * u(x, t))

solved inside the model by the **method of lines**: each column is a finite volume, the
Laplacian is the second difference with **zero-flux** boundaries (no cells cross the edges of
the image) and time advances with explicit Euler steps of ``1 / steps_per_hour`` hours in a
``pytensor.scan``. ``wave_speed = 2 sqrt(D lam)`` is the speed of the travelling front.

**Initial condition.** The PDE starts from the counts at the *first* fitted time ``t0``:
``u(x, t0) = max(count[t0, x], initial_floor) / K``. That time is conditioned on, not
predicted: it is left out of the likelihood (the fit likelihood runs over dim ``t__obs``,
the later times), and its expected density is the observed profile itself. The profile is
kept as ``constant_data/initial_count`` in the bundle; at score time it is put back into the
score data (the platform never hands a plugin the targets to score), so every score - a
forecast past the last fitted hour included - runs the same PDE from the same start. The
delay clock ``t`` in ``tanh(beta t)`` counts hours since ``t0``.

**Times.** ``t`` is an integer dim of hours, not a ``time`` dim with a frequency: assay
times are elapsed hours, and an integer extend dim gives contiguous forecasts at the fitted
spacing (48 -> 60, 72 h). The observation times need not be regular, but every
``t - t0`` must fall on the Euler grid (``(t - t0) * steps_per_hour`` an integer).

**Stability.** Explicit Euler is stable only when ``D dt / dx^2 <= 1/2``. ``build`` checks
this at the ``cfl_quantile`` quantile of the prior of ``D`` and raises ``SETTINGS_INVALID``
when it fails (raise ``steps_per_hour`` or tighten the prior of ``D``). The fit also reports
the largest posterior Courant number in ``learned["cfl_number_max"]``.
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any, ClassVar

import numpy as np
import pymc as pm
import pytensor
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import FitContext, FitOutput, Fitted, ModelSettings, ScoreContext
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field

__all__ = [
    "INITIAL",
    "OBS_DIM",
    "SPACE_DIM",
    "TARGET",
    "TIME_DIM",
    "FisherKPPModel",
    "FisherKPPSettings",
    "cfl_number",
    "simulate_kpp",
]

TIME_DIM = "t"
SPACE_DIM = "x"
TARGET = "count"
OBS_DIM = "t__obs"
"""Dim of the fit likelihood: the fitted times after the first (the initial condition)."""
INITIAL = "initial_count"
"""Constant data: the counts at the first fitted time, where the PDE starts."""

_GRID_TOL = 1e-6


class FisherKPPSettings(ModelSettings):
    """Settings of ``sci.fisher_kpp`` (D in um^2/h, rates in 1/h, K in cells per column)."""

    steps_per_hour: int = Field(
        10,
        ge=1,
        le=1000,
        description="Explicit Euler steps per hour; more steps are slower but stable for a "
        "larger diffusivity (x-ui-level: basic)",
    )
    delay: bool = Field(
        True,
        description="Multiply movement and growth by tanh(beta t): the cells start slowly after "
        "the scratch (x-ui-level: basic)",
    )
    max_extension: int = Field(
        4,
        ge=0,
        le=1000,
        description="Observation periods a forecast may reach past the last fitted time",
    )
    diffusivity_median: float = Field(1000.0, gt=0, description="Prior median of D (um^2/h)")
    diffusivity_log_sd: float = Field(1.0, gt=0, description="Log-scale sd of the prior of D")
    growth_median: float = Field(0.05, gt=0, description="Prior median of lam (1/h)")
    growth_log_sd: float = Field(1.0, gt=0, description="Log-scale sd of the prior of lam")
    capacity_mu: float = Field(122.0, gt=0, description="Prior mean of K (cells per column)")
    capacity_sigma: float = Field(15.0, gt=0, description="Prior sd of K")
    delay_rate_median: float = Field(0.1, gt=0, description="Prior median of beta (1/h)")
    delay_rate_log_sd: float = Field(1.0, gt=0, description="Log-scale sd of the prior of beta")
    initial_floor: float = Field(
        0.1, gt=0, description="Smallest initial count, so an empty column can still fill"
    )
    cfl_quantile: float = Field(
        0.99,
        gt=0.5,
        lt=1.0,
        description="Quantile of the prior of D that must satisfy D dt / dx^2 <= 1/2",
    )


_TEMPLATE: dict[str, Any] = {
    "params": {"max_extension": {"type": "integer", "default": 4}},
    "template": {
        "dims": {
            TIME_DIM: {
                "kind": "integer",
                "policy": "extend",
                "max_extension": "{max_extension}",
                "description": "Hours since the start of the assay",
            },
            SPACE_DIM: {
                "kind": "float",
                "policy": "fixed",
                "description": "Centre of each column across the wound (um, equal spacing)",
            },
        },
        "vars": {
            TARGET: {
                "dims": [TIME_DIM, SPACE_DIM],
                "dtype": "int64",
                "role": "target",
                "units": "cells",
                "bounds": [0, None],
            }
        },
    },
}


def cfl_number(diffusivity: float | np.ndarray, dx: float, steps_per_hour: int) -> Any:
    """The Courant number ``D dt / dx^2`` of the explicit scheme (stable when <= 1/2)."""
    return np.asarray(diffusivity) / steps_per_hour / dx**2


def _laplacian_noflux(u: Any, dx: float) -> Any:
    """Finite-volume second difference with zero-flux boundaries, divided by ``dx^2``
    (works on the last axis of a PyTensor or NumPy array)."""
    cat = pt.concatenate if isinstance(u, pt.TensorVariable) else np.concatenate
    return (
        cat(
            [
                u[..., 1:2] - u[..., 0:1],
                u[..., 2:] - 2 * u[..., 1:-1] + u[..., :-2],
                u[..., -2:-1] - u[..., -1:],
            ],
            axis=-1,
        )
        / dx**2
    )


def simulate_kpp(
    initial: np.ndarray,
    hours: np.ndarray,
    *,
    diffusivity: float,
    growth: float,
    capacity: float,
    delay_rate: float | None,
    dx: float,
    steps_per_hour: int,
) -> np.ndarray:
    """NumPy twin of the scan in :meth:`FisherKPPModel.build`: expected counts
    ``(len(hours), len(initial))`` at ``hours`` since the start, from ``initial`` counts."""
    dt = 1.0 / steps_per_hour
    steps = np.rint(np.asarray(hours, dtype=float) * steps_per_hour).astype(int)
    u = np.asarray(initial, dtype=float) / capacity
    out = np.empty((steps.size, u.size))
    for j in np.flatnonzero(steps == 0):
        out[j] = u
    for i in range(int(steps.max(initial=0))):
        a = math.tanh(delay_rate * i * dt) if delay_rate is not None else 1.0
        u = u + dt * a * (diffusivity * _laplacian_noflux(u, dx) + growth * u * (1 - u))
        for j in np.flatnonzero(steps == i + 1):
            out[j] = u
    return out * capacity


class FisherKPPModel(PyMCModel[FisherKPPSettings]):
    """Fisher-KPP cell invasion of a scratch assay, the PDE solved in PyMC (E50)."""

    id = "sci.fisher_kpp"
    version = "1.0.0"
    Settings = FisherKPPSettings
    description = (
        "Cell counts across a closing wound (a scratch assay) as a Fisher-KPP "
        "reaction-diffusion equation: cells move with diffusivity D, divide logistically at "
        "rate lam up to a capacity K, optionally after a start-up delay. The PDE runs inside "
        "the model from the counts at the first time; outputs are the expected density field "
        "(a kymograph over time and position) and the front speed 2 sqrt(D lam)."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["wave_speed", "D", "lam"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "density": {
            "dims": [TIME_DIM, SPACE_DIM],
            "units_of": TARGET,
            "description": "Cells per column: the expected count K u(x, t), or a Poisson draw",
            "grid": {"y": TIME_DIM, "x": SPACE_DIM, "origin": "lower", "colormap": "viridis"},
        },
        "wave_speed": {
            "dims": [],
            "units": "um/h",
            "description": "Speed of the travelling invasion front, 2 sqrt(D lam)",
        },
    }
    expected: ClassVar[dict[str, str]] = {"density": "mu", "wave_speed": "wave_speed"}
    predictive: ClassVar[dict[str, str]] = {"density": TARGET}
    plots = ("core.convergence", "core.posterior_forest", "sci.profile_bands", "sci.field_map")
    default_engine = "pymc"
    prior_overlap_vars = ("D", "lam", "K", "beta")

    # ------------------------------------------------------------------ geometry
    @staticmethod
    def spacing(ctx: BuildContext) -> float:
        """The column width ``dx`` (um): ``x`` must be increasing with equal steps."""
        x = np.asarray(ctx.labels(SPACE_DIM), dtype=float)
        if x.size < 3:
            raise MapiError(
                f"{SPACE_DIM!r} needs at least 3 positions for the diffusion term",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{SPACE_DIM}",
            )
        diffs = np.diff(x)
        dx = float(diffs.mean())
        if dx <= 0 or not np.allclose(diffs, dx, rtol=1e-6, atol=0.0):
            raise MapiError(
                f"the positions of {SPACE_DIM!r} must increase in equal steps (a regular grid "
                "of columns)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{SPACE_DIM}",
            )
        return dx

    @staticmethod
    def euler_steps(ctx: BuildContext, s: FisherKPPSettings) -> tuple[np.ndarray, np.ndarray]:
        """``(hours since t0, Euler step index)`` of each current label of ``t``."""
        t0 = float(np.min(ctx.fit_levels(TIME_DIM)))
        hours = np.asarray(ctx.labels(TIME_DIM), dtype=float) - t0
        raw = hours * s.steps_per_hour
        steps = np.rint(raw).astype(np.int64)
        if (hours < 0).any() or not np.allclose(raw, steps, atol=_GRID_TOL, rtol=0.0):
            raise MapiError(
                f"every time in {TIME_DIM!r} minus the first fitted time must be a whole "
                f"number of Euler steps (steps_per_hour={s.steps_per_hour})",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TIME_DIM}",
            )
        return hours, steps

    @staticmethod
    def check_stability(s: FisherKPPSettings, dx: float) -> float:
        """Raise ``SETTINGS_INVALID`` unless ``D dt / dx^2 <= 1/2`` at the prior's
        ``cfl_quantile``; return that Courant number."""
        z = NormalDist().inv_cdf(s.cfl_quantile)
        d_hi = s.diffusivity_median * math.exp(z * s.diffusivity_log_sd)
        nu = float(cfl_number(d_hi, dx, s.steps_per_hour))
        if nu > 0.5:
            need = math.ceil(2.0 * d_hi / dx**2)
            raise MapiError(
                f"the explicit Euler scheme is unstable: D dt / dx^2 = {nu:.3g} > 0.5 at the "
                f"{s.cfl_quantile:g} prior quantile of D ({d_hi:.4g} um^2/h, dx = {dx:g} um). "
                f"Set steps_per_hour to {need} or more, or lower the prior of D",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/steps_per_hour",
            )
        return nu

    # ------------------------------------------------------------------ model
    def build(self, ds: xr.Dataset, s: FisherKPPSettings, ctx: BuildContext) -> pm.Model:
        dx = self.spacing(ctx)
        self.check_stability(s, dx)
        _, steps = self.euler_steps(ctx, s)
        dt = 1.0 / s.steps_per_hour
        n_steps = int(steps.max())
        initial = self._initial_counts(ds, ctx)
        with pm.Model(coords=ctx.coords()) as model:
            c0 = ctx.data(INITIAL, np.maximum(initial, s.initial_floor), dims=[SPACE_DIM])
            D = pm.LogNormal("D", math.log(s.diffusivity_median), s.diffusivity_log_sd)
            lam = pm.LogNormal("lam", math.log(s.growth_median), s.growth_log_sd)
            K = pm.Normal("K", s.capacity_mu, s.capacity_sigma)
            beta = (
                pm.LogNormal("beta", math.log(s.delay_rate_median), s.delay_rate_log_sd)
                if s.delay
                else None
            )
            pm.Deterministic("wave_speed", 2.0 * pt.sqrt(D * lam))
            u0 = c0 / K
            if n_steps:
                t_grid = pt.as_tensor_variable(np.arange(n_steps, dtype=float) * dt)

                def step(t: Any, u: Any, D: Any, lam: Any, *rest: Any) -> Any:
                    a = pt.tanh(rest[0] * t) if rest else 1.0
                    return u + dt * a * (D * _laplacian_noflux(u, dx) + lam * u * (1 - u))

                path = pytensor.scan(
                    step,
                    sequences=[t_grid],
                    outputs_info=[u0],
                    non_sequences=[D, lam] + ([beta] if beta is not None else []),
                    return_updates=False,
                )
                u_all = pt.concatenate([u0[None, :], path], axis=0)
            else:
                u_all = u0[None, :]
            mu = pm.Deterministic("mu", K * u_all[steps], dims=(TIME_DIM, SPACE_DIM))
            rate = pt.clip(mu, 1e-6, 1e6)
            y = ctx.observed(TARGET, dims=[TIME_DIM, SPACE_DIM])
            if ctx.is_score:
                pm.Poisson(TARGET, rate, observed=y, dims=(TIME_DIM, SPACE_DIM))
            else:
                later = np.flatnonzero(steps > 0)
                if later.size == 0:
                    raise MapiError(
                        f"the fit needs at least two times in {TIME_DIM!r}: the first is the "
                        "initial condition",
                        code=ErrorCode.SCHEMA_INVALID,
                        pointer=f"/data/{TIME_DIM}",
                    )
                model.add_coord(OBS_DIM, ctx.labels(TIME_DIM)[later])
                pm.Poisson(TARGET, rate[later], observed=y[later], dims=(OBS_DIM, SPACE_DIM))
                ctx.observed_vars[TARGET] = (OBS_DIM, SPACE_DIM)
                ctx.observed_values[TARGET] = y[later]
        return model

    @staticmethod
    def _initial_counts(ds: xr.Dataset, ctx: BuildContext) -> np.ndarray:
        """The counts at the first fitted time (fit: from the target; score: the stored
        profile that :meth:`score` puts back into the data)."""
        if ctx.is_fit:
            t = np.asarray(ctx.labels(TIME_DIM))
            first = ds[TARGET].isel({TIME_DIM: int(np.argmin(t))})
            values = np.asarray(first.transpose(SPACE_DIM).values, dtype=float)
            if not np.isfinite(values).all():
                raise MapiError(
                    f"the counts at the first time of {TIME_DIM!r} (the initial condition) "
                    "must not be missing",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{TARGET}",
                )
            return values
        if INITIAL not in ds:
            raise MapiError(
                f"the score data has no {INITIAL!r}: score through the platform, which adds "
                "the initial profile kept in the bundle",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return np.asarray(ds[INITIAL].transpose(SPACE_DIM).values, dtype=float)

    # ------------------------------------------------------------------ fit / score
    def fit(
        self, ds: xr.Dataset, s: FisherKPPSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        out = super().fit(ds, s, cfg, ctx)
        t = np.sort(np.asarray(ds[TIME_DIM].values))
        for name, grp in list(out.groups.items()):
            if OBS_DIM in grp.dims and OBS_DIM not in grp.coords:
                out.groups[name] = grp.assign_coords({OBS_DIM: t[1:]})
        dx = float(np.diff(np.asarray(ds[SPACE_DIM].values, dtype=float)).mean())
        d_max = float(out.groups["posterior"]["D"].max())
        out.learned["dx"] = dx
        out.learned["steps_per_hour"] = s.steps_per_hour
        out.learned["t0"] = float(t[0])
        out.learned["cfl_number_max"] = float(cfl_number(d_max, dx, s.steps_per_hour))
        out.learned["max_extension"] = s.max_extension
        return out

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        const = fitted.groups.get("constant_data")
        if const is None or INITIAL not in const:
            raise MapiError(
                f"the bundle of {self.id!r} has no constant_data/{INITIAL}",
                code=ErrorCode.INTERNAL_ERROR,
            )
        init = const[INITIAL]
        if SPACE_DIM in ds.coords and SPACE_DIM in init.coords:
            init = init.sel({SPACE_DIM: ds[SPACE_DIM].values})
        ds = ds.assign({INITIAL: ((SPACE_DIM,), np.asarray(init.values, dtype=float))})
        return super().score(fitted, ds, req, ctx)
