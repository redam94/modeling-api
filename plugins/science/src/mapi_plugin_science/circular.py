"""``sci.circular_regression``: directions and times of day on the circle (E81).

A wind blows *from* a direction, an animal walks past a camera at a time of day: the data
are angles, and 359 degrees is next to 1 degree. This model fits densities on the circle
without wrapping problems, the way pymc-challenges E81 does.

Canonical inputs (bind dataset names onto them in the spec)::

    dims  obs:   integer, policy extend (one row per report or record)
    vars  direction(obs): target, float64, in ``units`` (degrees, radians or hours)
    regression kinds (von_mises, projected_normal, regime_mixture):
          hour(obs): covariate, hours since midnight in [0, 24)
          day(obs):  covariate, days since 1 January in [0, 366)
    kind activity:
          dims  {group_dim}: categorical, fixed (the species)
          vars  {group_dim}_idx(obs): index of the record's group

**Regression kinds.** Hour and day of year are circular *predictors*; they enter as
harmonics ``cos(k 2 pi h / 24), sin(k 2 pi h / 24)`` (``daily_harmonics``), the same for
the day of year (``annual_harmonics``) and the products of the first
``interaction_harmonics`` daily harmonics with the first annual one (the daily cycle changes
with the season). The E81 defaults (3, 2, 2) give 19 features ``F`` with the intercept.

* ``von_mises`` - the canonical (natural-parameter) link: ``eta = F B``,
  ``log p(theta) = eta_1 cos theta + eta_2 sin theta - log(2 pi I0(|eta|))``. The mean
  direction is ``atan2(eta_2, eta_1)`` and the concentration ``|eta|``. The log posterior is
  concave in ``B``: one mode, no wrapped prior, no seam at +-pi.
* ``projected_normal`` - the angle of a latent vector ``z ~ N(F B, I)``, with the closed
  form density of E81.
* ``regime_mixture`` - two von Mises regimes (E81: easterly night drainage and the westerly
  sea breeze). Each regime has natural parameters on a smaller feature set (intercept,
  ``regime_daily_harmonics`` and ``regime_annual_harmonics``); its intercept prior is
  centred on ``regime_strength (cos mu_r, sin mu_r)`` with ``mu_r`` from
  ``regime_directions_deg``, which anchors the labels. The probability of the second regime
  follows all features through a logistic gate.

Priors (as in E81): normal(0, ``intercept_sd``) on intercepts and normal(0, ``coef_sd``) on
the other coefficients. ``regime_mixture`` does not use ``intercept_sd``: regime ``r``'s
intercept is normal(``regime_strength (cos mu_r, sin mu_r)``, 1), the gate intercept
normal(0, ``gate_intercept_sd``) and every other coefficient normal(0, ``coef_sd``).

**Kind activity** (camera-trap times): a label-free log-Fourier density per group,
``log f(theta) = sum_k a_k cos k theta + b_k sin k theta - log Z``, with ``harmonics`` terms,
the smoothness prior ``a_k, b_k ~ N(0, (tau / k)^2)`` and ``tau ~ HalfNormal(tau_sd)`` per
group. The likelihood uses only the trigonometric moments of each group; ``Z`` is a sum over
``grid_points`` equally spaced angles. Outputs add the activity level ``1 / (2 pi max f)``
and the overlap ``Delta = int min(f_i, f_j)`` of each pair of groups.

**Fit.** The likelihood is a ``pm.Potential`` (NUTS, ``mapi_pymc.engines``). With
``fit.keep.log_likelihood`` the bundle stores the pointwise log density of every row
(per radian, as in the notebook), so ``elpd_loo`` compares the kinds on the same rows. With
``fit.keep.posterior_predictive`` it stores replicated angles ``theta`` (radians, from 0 to
2 pi) next to the observed ``theta`` in ``observed_data``: ``sci.rose`` then draws the
observed rose with the model's band in its default settings, in the maths orientation (0 at
the east, anticlockwise); ``clockwise: true`` draws it as a compass or a clock. The fit also
stores the gridded outputs ``density`` (and, for kind activity, ``overlap``) in the
posterior, so ``sci.field_map`` and ``sci.profile_bands`` draw them from the bundle.

**Outputs** (draws; ``score_draws`` evenly spaced posterior draws):

* ``direction_hat(obs)`` - the mean direction of the row's predictive distribution
  (``expected``) or one simulated angle (``predictive`` and ``simulate``), in ``units`` from
  0 to one turn;
* ``resultant(obs)`` - the mean resultant length of the row's predictive distribution
  (1: one direction, 0: no preferred direction);
* ``density(frame, angle)`` - the predictive density per unit of ``units`` on ``angle_points``
  angles. A frame is a block of ``24 / frames`` hours (regression kinds: the average of the
  densities of up to ``rows_per_frame`` scored rows in the block, the model's rose diagram)
  or a group (activity);
* ``mean_direction(frame)`` and ``resultant_length(frame)`` - the mean resultant vector of
  each frame (its direction in ``units``);
* activity only: ``activity_level(frame)`` and ``overlap(frame, frame_)``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.checkpoint import CheckpointSink, fit_key
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_pymc.engines import EngineHooks, EnginePlan, run_engine
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
)
from mapi_spec.capabilities import Capabilities, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, PosteriorPredictiveKeep, ScoreRequest
from pydantic import Field, model_validator
from scipy import special

from mapi_plugin_science._common import (
    clean_attrs as _clean,
)
from mapi_plugin_science._common import (
    draw_index as _draw_index,
)
from mapi_plugin_science._common import (
    quiet as _quiet,
)
from mapi_plugin_science._common import (
    rng as _rng,
)

__all__ = [
    "ANGLE_DIM",
    "DAY",
    "FRAME_DIM",
    "HOUR",
    "KINDS",
    "OBS_DIM",
    "PERIODS",
    "TARGET",
    "THETA",
    "CircularRegressionModel",
    "CircularRegressionSettings",
    "a1",
    "harmonic_features",
    "log_fourier_density",
    "overlap",
    "pn_logpdf",
    "pn_resultant",
    "vm_logpdf",
]

TARGET = "direction"
OBS_DIM = "obs"
HOUR = "hour"
DAY = "day"
FRAME_DIM = "frame"
FRAME2_DIM = "frame_"
ANGLE_DIM = "angle"
FEATURE_DIM = "feature"
REGIME_FEATURE_DIM = "regime_feature"
COMPONENT_DIM = "component"
REGIME_DIM = "regime"
HARMONIC_DIM = "harmonic"
THETA = "theta"
"""Angles in radians (0 to 2 pi) in ``observed_data`` and ``posterior_predictive``."""

TWO_PI = 2.0 * math.pi
YEAR_DAYS = 365.25
PERIODS: dict[str, float] = {"degrees": 360.0, "radians": TWO_PI, "hours": 24.0}
"""One turn of the circle in each unit of ``units``."""

Kind = Literal["von_mises", "projected_normal", "regime_mixture", "activity"]
KINDS: tuple[str, ...] = ("von_mises", "projected_normal", "regime_mixture", "activity")
REGRESSION_KINDS = frozenset({"von_mises", "projected_normal", "regime_mixture"})
_EPS = 1e-12
_CHUNK = 64  # posterior draws per block in the NumPy post-processing


class CircularRegressionSettings(ModelSettings):
    """Settings of ``sci.circular_regression`` (see the module docstring)."""

    kind: Kind = Field(
        "von_mises",
        description="von_mises (canonical link), projected_normal, regime_mixture (two von "
        "Mises regimes with a gate), or activity (a log-Fourier density per group) "
        "(x-ui-level: basic)",
    )
    units: Literal["degrees", "radians", "hours"] = Field(
        "degrees",
        description="Units of the direction: degrees, radians or hours (a time of day) "
        "(x-ui-level: basic)",
    )
    group_dim: str = Field(
        "group",
        pattern=r"^[a-z][a-z0-9_]{0,40}$",
        description="kind activity: the dim of the groups (species); the index variable is "
        "<group_dim>_idx",
    )
    daily_harmonics: int = Field(3, ge=0, le=8, description="Harmonics of the hour of day")
    annual_harmonics: int = Field(2, ge=0, le=6, description="Harmonics of the day of year")
    interaction_harmonics: int = Field(
        2,
        ge=0,
        le=8,
        description="Daily harmonics multiplied by the first annual harmonic (the daily cycle "
        "changes with the season)",
    )
    intercept_sd: float = Field(
        2.0, gt=0, description="Prior sd of the intercepts (not used by regime_mixture)"
    )
    coef_sd: float = Field(1.0, gt=0, description="Prior sd of the other coefficients")
    regime_directions_deg: list[float] = Field(
        default_factory=lambda: [90.0, 250.0],
        min_length=2,
        max_length=2,
        description="regime_mixture: prior mean direction of each regime (degrees, in the "
        "units' own orientation)",
    )
    regime_names: list[str] = Field(
        default_factory=lambda: ["easterly", "westerly"],
        min_length=2,
        max_length=2,
        description="regime_mixture: names of the two regimes",
    )
    regime_strength: float = Field(
        2.0, gt=0, description="regime_mixture: prior mean concentration of each regime"
    )
    regime_daily_harmonics: int = Field(2, ge=0, le=8)
    regime_annual_harmonics: int = Field(1, ge=0, le=6)
    gate_intercept_sd: float = Field(1.5, gt=0, description="Prior sd of the gate intercept")
    harmonics: int = Field(16, ge=1, le=64, description="activity: Fourier harmonics")
    tau_sd: float = Field(2.0, gt=0, description="activity: half-normal scale of tau")
    grid_points: int = Field(
        256, ge=32, le=4096, description="activity: angles of the normalising sum"
    )
    frames: int = Field(
        8,
        ge=1,
        le=48,
        description="Regression kinds: blocks of the day for density and mean_direction "
        "(8: three hours each)",
    )
    angle_points: int = Field(72, ge=8, le=720, description="Angles of the density output")
    rows_per_frame: int = Field(
        200, ge=1, le=20_000, description="Scored rows averaged per frame for the density"
    )
    score_draws: int = Field(
        400, ge=1, le=20_000, description="Posterior draws kept per score (evenly spaced)"
    )

    @model_validator(mode="after")
    def _check(self) -> CircularRegressionSettings:
        if self.interaction_harmonics > self.daily_harmonics:
            raise ValueError("interaction_harmonics must not be larger than daily_harmonics")
        if self.interaction_harmonics and not self.annual_harmonics:
            raise ValueError("interaction_harmonics needs annual_harmonics >= 1")
        if self.regime_names[0] == self.regime_names[1]:
            raise ValueError("regime_names must differ")
        return self


_REGRESSION_VARS: dict[str, Any] = {
    "vars": {
        HOUR: {
            "dims": [OBS_DIM],
            "dtype": "float64",
            "role": "covariate",
            "units": "h",
            "bounds": [0, 24],
        },
        DAY: {
            "dims": [OBS_DIM],
            "dtype": "float64",
            "role": "covariate",
            "units": "d",
            "bounds": [0, 367],
        },
    }
}

_TEMPLATE: dict[str, Any] = {
    "params": {
        "kind": {"type": "string", "default": "von_mises", "enum": list(KINDS)},
        "group_dim": {"type": "string", "default": "group"},
    },
    "template": {
        "layout": "long",
        "dims": {
            OBS_DIM: {
                "kind": "integer",
                "policy": "extend",
                "description": "One row per report or record",
            }
        },
        "vars": {
            TARGET: {"dims": [OBS_DIM], "dtype": "float64", "role": "target"},
        },
    },
    "cases": [
        *({"when": {"kind": k}, "merge": _REGRESSION_VARS} for k in sorted(REGRESSION_KINDS)),
        {
            "when": {"kind": "activity"},
            "merge": {
                "dims": {"{group_dim}": {"kind": "categorical", "policy": "fixed"}},
                "vars": {
                    "{group_dim}_idx": {
                        "dims": [OBS_DIM],
                        "dtype": "int64",
                        "role": "index",
                        "index_of": "{group_dim}",
                    }
                },
            },
        },
    ],
}


# ============================================================================ NumPy twins


def a1(kappa: Any) -> Any:
    """Mean resultant length of a von Mises with concentration ``kappa``: I1 / I0."""
    k = np.asarray(kappa, dtype=float)
    return special.i1e(k) / special.i0e(k)


def vm_logpdf(theta: Any, e1: Any, e2: Any) -> Any:
    """Von Mises log density (per radian) in natural parameters ``eta = (e1, e2)``."""
    k = np.hypot(e1, e2)
    return e1 * np.cos(theta) + e2 * np.sin(theta) - math.log(TWO_PI) - (np.log(special.i0e(k)) + k)


def pn_logpdf(theta: Any, m1: Any, m2: Any) -> Any:
    """Projected normal (identity covariance) log density of the angle of ``z ~ N(m, I)``."""
    u = m1 * np.cos(theta) + m2 * np.sin(theta)
    return (
        -math.log(TWO_PI)
        - 0.5 * (m1**2 + m2**2)
        + np.log1p(u * math.sqrt(math.pi / 2) * special.erfcx(-u / math.sqrt(2)))
    )


def pn_resultant(r: Any) -> Any:
    """Mean resultant length of the projected normal with ``|m| = r`` (identity covariance):
    ``sqrt(pi / 2) (r / 2) exp(-r^2 / 4) (I0(r^2 / 4) + I1(r^2 / 4))``."""
    r = np.asarray(r, dtype=float)
    x = r * r / 4.0
    return math.sqrt(math.pi / 2) * (r / 2) * (special.i0e(x) + special.i1e(x))


def harmonic_features(
    hour: np.ndarray, day: np.ndarray, *, daily: int, annual: int, interactions: int
) -> tuple[np.ndarray, list[str]]:
    """The E81 design matrix ``(rows, features)`` and its feature names.

    Intercept, ``cos/sin(k 2 pi h / 24)`` for ``k <= daily``, ``cos/sin(k 2 pi d / 365.25)``
    for ``k <= annual``, then ``cos/sin(k h) * cos/sin(d)`` for ``k <= interactions``.
    """
    h = TWO_PI * np.asarray(hour, dtype=float) / 24.0
    d = TWO_PI * np.asarray(day, dtype=float) / YEAR_DAYS
    cols: list[np.ndarray] = [np.ones_like(h)]
    names = ["1"]
    for k in range(1, daily + 1):
        cols += [np.cos(k * h), np.sin(k * h)]
        names += [f"cos{k}h", f"sin{k}h"]
    for k in range(1, annual + 1):
        cols += [np.cos(k * d), np.sin(k * d)]
        names += [f"cos{k}d", f"sin{k}d"]
    for k in range(1, interactions + 1):
        for a, an in ((np.cos(k * h), f"cos{k}h"), (np.sin(k * h), f"sin{k}h")):
            for b, bn in ((np.cos(d), "cos1d"), (np.sin(d), "sin1d")):
                cols.append(a * b)
                names.append(f"{an}*{bn}")
    return np.column_stack(cols), names


def log_fourier_density(
    a: np.ndarray, b: np.ndarray, angles: np.ndarray, grid_points: int
) -> np.ndarray:
    """Density per radian of ``exp(sum_k a_k cos k t + b_k sin k t) / Z`` at ``angles``.

    ``a``, ``b`` are ``(..., K)``; ``Z`` is the sum over ``grid_points`` equally spaced
    angles (exact to many digits for a smooth periodic integrand). Returns ``(..., A)``.
    """
    kk = np.arange(1, a.shape[-1] + 1)
    grid = np.arange(grid_points) * TWO_PI / grid_points
    lg = a @ np.cos(np.outer(kk, grid)) + b @ np.sin(np.outer(kk, grid))
    log_z = special.logsumexp(lg, axis=-1) + math.log(TWO_PI / grid_points)
    at = np.asarray(angles, dtype=float)
    la = a @ np.cos(np.outer(kk, at)) + b @ np.sin(np.outer(kk, at))
    return np.asarray(np.exp(la - log_z[..., None]))


def overlap(f: np.ndarray, d_theta: float) -> np.ndarray:
    """Coefficient of overlap ``int min(f_i, f_j)`` of every pair: ``(..., G, A)`` densities
    on an equally spaced grid of step ``d_theta`` -> ``(..., G, G)``."""
    return np.asarray(np.minimum(f[..., :, None, :], f[..., None, :, :]).sum(-1) * d_theta)


# ============================================================================ data


@dataclass(slots=True)
class _Data:
    """The rows as the model needs them: angles in radians and the design."""

    theta: np.ndarray  # (n,) radians in (-pi, pi]
    hour: np.ndarray | None  # (n,) hours
    features: np.ndarray | None  # (n, P)
    feature_names: list[str]
    small: np.ndarray | None  # (n, Q) regime features
    small_names: list[str]
    group_idx: np.ndarray | None  # (n,) position in the fit groups
    groups: list[str]


def _period(s: CircularRegressionSettings) -> float:
    return PERIODS[s.units]


def to_radians(values: np.ndarray, units: str) -> np.ndarray:
    """Angles in ``units`` to radians in (-pi, pi]."""
    t = np.asarray(values, dtype=float) / PERIODS[units] * TWO_PI
    return np.angle(np.exp(1j * t))


def _vector(ds: xr.Dataset, name: str, *, score: bool) -> np.ndarray:
    if name not in ds:
        raise MapiError(
            f"the {'score' if score else 'fit'} data has no variable {name!r}",
            code=ErrorCode.MISSING_FUTURE_COVARIATE if score else ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{name}",
        )
    da = ds[name]
    if da.dims != (OBS_DIM,):
        raise MapiError(
            f"{name!r} must be one-dimensional along {OBS_DIM!r}, not {list(da.dims)}",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{name}",
        )
    return np.asarray(da.values, dtype=float)


def _read(
    ds: xr.Dataset,
    s: CircularRegressionSettings,
    *,
    score: bool,
    groups: list[str] | None = None,
) -> _Data:
    """Check and convert the fit or score data (the score data have no target)."""
    if OBS_DIM not in ds.dims:
        raise MapiError(
            f"the data has no dim {OBS_DIM!r}", code=ErrorCode.SCHEMA_INVALID, pointer="/data"
        )
    n = int(ds.sizes[OBS_DIM])
    if n == 0 and not score:
        raise MapiError("the fit data has no rows", code=ErrorCode.SCHEMA_INVALID)
    theta = np.zeros(n)
    if not score:
        raw = _vector(ds, TARGET, score=False)
        if not np.isfinite(raw).all():
            raise MapiError(
                f"{TARGET!r} has missing or infinite values; drop those rows (a calm wind "
                "has no direction)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        theta = to_radians(raw, s.units)
    if s.kind == "activity":
        g = s.group_dim
        idx_name = f"{g}_idx"
        if g not in ds.dims and groups is None:
            raise MapiError(
                f"kind activity needs the dim {g!r} (the groups)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{g}",
            )
        idx = _vector(ds, idx_name, score=score)
        labels = [str(v) for v in np.asarray(ds[g].values).tolist()] if g in ds.coords else None
        fit_groups = groups if groups is not None else (labels or [])
        if not fit_groups:
            raise MapiError(
                f"dim {g!r} has no levels", code=ErrorCode.SCHEMA_INVALID, pointer=f"/data/{g}"
            )
        pos = np.asarray(idx, dtype=float)
        n_lab = len(labels) if labels is not None else len(fit_groups)
        if n and (not np.isfinite(pos).all() or pos.min() < 0 or pos.max() >= n_lab):
            raise MapiError(
                f"{idx_name!r} must hold positions 0..{n_lab - 1} of dim {g!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{idx_name}",
            )
        ints = pos.astype(np.int64)
        if labels is not None and groups is not None:
            lookup = {lab: i for i, lab in enumerate(groups)}
            missing = sorted({labels[i] for i in ints} - set(lookup))
            if missing:
                raise MapiError(
                    f"groups {missing[:5]} were not in the fit data",
                    code=ErrorCode.UNSEEN_LEVELS,
                    pointer=f"/data/{g}",
                )
            ints = np.asarray([lookup[labels[i]] for i in ints], dtype=np.int64)
        return _Data(theta, None, None, [], None, [], ints, list(fit_groups))
    hour = _vector(ds, HOUR, score=score)
    day = _vector(ds, DAY, score=score)
    for name, v, hi in ((HOUR, hour, 24.0), (DAY, day, 367.0)):
        if n and (not np.isfinite(v).all() or v.min() < 0 or v.max() >= hi):
            raise MapiError(
                f"{name!r} must be finite and in [0, {hi:g}) "
                f"({'hours since midnight' if name == HOUR else 'days since 1 January'})",
                code=ErrorCode.MISSING_FUTURE_COVARIATE if score else ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{name}",
            )
    feats, names = harmonic_features(
        hour,
        day,
        daily=s.daily_harmonics,
        annual=s.annual_harmonics,
        interactions=s.interaction_harmonics,
    )
    small, small_names = harmonic_features(
        hour,
        day,
        daily=s.regime_daily_harmonics,
        annual=s.regime_annual_harmonics,
        interactions=0,
    )
    return _Data(theta, hour, feats, names, small, small_names, None, [])


# ============================================================================ PyTensor


def _log_i0(k: Any) -> Any:
    """``log I0(k)`` as E81 writes it (``pt.i0`` is fast in every backend; ``pt.ive`` is
    slow under numba), with the large-``k`` expansion where ``I0`` would overflow."""
    big = k - 0.5 * pt.log(TWO_PI * k) + 1.0 / (8.0 * k) + 1.0 / (16.0 * k**2)
    return pt.switch(k < 300.0, pt.log(pt.i0(pt.minimum(k, 300.0))), big)


def _vm_lp(theta: np.ndarray, e1: Any, e2: Any) -> Any:
    k = pt.sqrt(e1**2 + e2**2 + _EPS)
    return e1 * np.cos(theta) + e2 * np.sin(theta) - math.log(TWO_PI) - _log_i0(k)


def _pn_lp(theta: np.ndarray, m1: Any, m2: Any) -> Any:
    u = m1 * np.cos(theta) + m2 * np.sin(theta)
    return (
        -math.log(TWO_PI)
        - 0.5 * (m1**2 + m2**2)
        + pt.log1p(u * math.sqrt(math.pi / 2) * pt.erfcx(-u / math.sqrt(2)))
    )


def _prior_sd(names: list[str], s: CircularRegressionSettings) -> np.ndarray:
    return np.asarray([s.intercept_sd if n == "1" else s.coef_sd for n in names])


def _regime_means(s: CircularRegressionSettings) -> np.ndarray:
    mu = np.deg2rad(np.asarray(s.regime_directions_deg, dtype=float))
    return s.regime_strength * np.column_stack([np.cos(mu), np.sin(mu)])


def build_model(data: _Data, s: CircularRegressionSettings) -> pm.Model:
    """The PyMC model of ``s.kind`` (a ``pm.Potential`` over every row)."""
    th = data.theta
    if s.kind == "activity":
        assert data.group_idx is not None
        kk = np.arange(1, s.harmonics + 1)
        grid = np.arange(s.grid_points) * TWO_PI / s.grid_points
        n_g = len(data.groups)
        csum = np.zeros((n_g, s.harmonics))
        ssum = np.zeros((n_g, s.harmonics))
        np.add.at(csum, data.group_idx, np.cos(np.outer(th, kk)))
        np.add.at(ssum, data.group_idx, np.sin(np.outer(th, kk)))
        counts = np.bincount(data.group_idx, minlength=n_g).astype(float)
        g_coords = {s.group_dim: data.groups, HARMONIC_DIM: kk.tolist()}
        with pm.Model(coords=g_coords) as model:
            tau = pm.HalfNormal("tau", s.tau_sd, dims=s.group_dim)
            za = pm.Normal("za", 0.0, 1.0, dims=(s.group_dim, HARMONIC_DIM))
            zb = pm.Normal("zb", 0.0, 1.0, dims=(s.group_dim, HARMONIC_DIM))
            a = pm.Deterministic("a", za * tau[:, None] / kk, dims=(s.group_dim, HARMONIC_DIM))
            b = pm.Deterministic("b", zb * tau[:, None] / kk, dims=(s.group_dim, HARMONIC_DIM))
            lg = pt.dot(a, np.cos(np.outer(kk, grid))) + pt.dot(b, np.sin(np.outer(kk, grid)))
            log_z = pt.logsumexp(lg, axis=1) + math.log(TWO_PI / s.grid_points)
            pm.Potential("ll", pt.sum(a * csum + b * ssum) - pt.sum(counts * log_z))
        return model
    assert data.features is not None and data.small is not None
    coords: dict[str, Any] = {
        FEATURE_DIM: data.feature_names,
        COMPONENT_DIM: ["eta1", "eta2"],
    }
    if s.kind == "regime_mixture":
        coords[REGIME_DIM] = list(s.regime_names)
        coords[REGIME_FEATURE_DIM] = data.small_names[1:]
    with pm.Model(coords=coords) as model:
        if s.kind in ("von_mises", "projected_normal"):
            sd = _prior_sd(data.feature_names, s)[:, None]
            B = pm.Normal("B", 0.0, sd, dims=(FEATURE_DIM, COMPONENT_DIM))
            eta = pt.dot(data.features, B)
            lp = _vm_lp if s.kind == "von_mises" else _pn_lp
            pm.Potential("ll", pt.sum(lp(th, eta[:, 0], eta[:, 1])))
        else:
            B0 = pm.Normal("B0", _regime_means(s), 1.0, dims=(REGIME_DIM, COMPONENT_DIM))
            Bh = pm.Normal(
                "Bh", 0.0, s.coef_sd, dims=(REGIME_DIM, REGIME_FEATURE_DIM, COMPONENT_DIM)
            )
            Bc = pt.concatenate([B0[:, None, :], Bh], axis=1)
            e0 = pt.dot(data.small, Bc[0])
            e1 = pt.dot(data.small, Bc[1])
            gsd = np.r_[s.gate_intercept_sd, np.full(len(data.feature_names) - 1, s.coef_sd)]
            g = pm.Normal("g", 0.0, gsd, dims=FEATURE_DIM)
            z = pt.dot(data.features, g)
            ll = pt.logaddexp(
                -pm.math.log1pexp(z) + _vm_lp(th, e0[:, 0], e0[:, 1]),
                -pm.math.log1pexp(-z) + _vm_lp(th, e1[:, 0], e1[:, 1]),
            )
            pm.Potential("ll", pt.sum(ll))
    return model


FREE_VARS: dict[str, tuple[str, ...]] = {
    "von_mises": ("B",),
    "projected_normal": ("B",),
    "regime_mixture": ("B0", "Bh", "g"),
    "activity": ("tau", "za", "zb"),
}
GRID_OUTPUTS = ("density", "overlap")
"""Outputs with a ``grid``; the fit also stores them in the posterior."""
_KEEP_VARS: dict[str, tuple[str, ...]] = {**FREE_VARS, "activity": ("tau", "za", "zb", "a", "b")}


# ============================================================================ predictive


@dataclass(slots=True)
class _Draws:
    """Posterior arrays stacked over ``(chain, draw)`` -> ``S`` first."""

    values: dict[str, np.ndarray]
    size: int

    @classmethod
    def of(cls, post: xr.Dataset, names: tuple[str, ...]) -> _Draws:
        vals: dict[str, np.ndarray] = {}
        size = int(post.sizes["chain"] * post.sizes["draw"])
        for n in names:
            da = post[n].transpose("chain", "draw", ...)
            vals[n] = np.asarray(da.values, dtype=float).reshape(size, *da.shape[2:])
        return cls(vals, size)

    def take(self, idx: slice) -> dict[str, np.ndarray]:
        return {k: v[idx] for k, v in self.values.items()}


def _components(kind: str, d: dict[str, np.ndarray], data: _Data) -> dict[str, np.ndarray]:
    """Per-row predictive parameters of a block of draws: ``(S, n, 2)`` natural (or mean)
    vectors, plus the gate probability of the second regime ``(S, n)``."""
    if kind in ("von_mises", "projected_normal"):
        assert data.features is not None
        return {"eta": np.einsum("nf,sfc->snc", data.features, d["B"])}
    assert data.features is not None and data.small is not None
    bc = np.concatenate([d["B0"][:, :, None, :], d["Bh"]], axis=2)  # S, R, Q, 2
    ee = np.einsum("nq,srqc->srnc", data.small, bc)
    p = special.expit(np.einsum("nf,sf->sn", data.features, d["g"]))
    return {"e0": ee[:, 0], "e1": ee[:, 1], "p": p}


def _row_logpdf(kind: str, comp: dict[str, np.ndarray], theta: np.ndarray) -> np.ndarray:
    """Log density per radian of each row at its own angle ``theta`` ``(n,)``: ``(S, n)``."""
    if kind == "von_mises":
        e = comp["eta"]
        return np.asarray(vm_logpdf(theta, e[..., 0], e[..., 1]))
    if kind == "projected_normal":
        e = comp["eta"]
        return np.asarray(pn_logpdf(theta, e[..., 0], e[..., 1]))
    e0, e1, p = comp["e0"], comp["e1"], comp["p"]
    with np.errstate(divide="ignore"):
        return np.asarray(
            np.logaddexp(
                np.log1p(-p) + vm_logpdf(theta, e0[..., 0], e0[..., 1]),
                np.log(p) + vm_logpdf(theta, e1[..., 0], e1[..., 1]),
            )
        )


def _row_resultant(kind: str, comp: dict[str, np.ndarray]) -> np.ndarray:
    """Complex mean resultant vector ``E[exp(i theta)]`` of each row: ``(S, n)``."""

    def vm(e: np.ndarray) -> np.ndarray:
        k = np.hypot(e[..., 0], e[..., 1])
        return np.asarray(a1(k) * np.exp(1j * np.arctan2(e[..., 1], e[..., 0])))

    if kind == "von_mises":
        return vm(comp["eta"])
    if kind == "projected_normal":
        e = comp["eta"]
        r = np.hypot(e[..., 0], e[..., 1])
        return np.asarray(pn_resultant(r) * np.exp(1j * np.arctan2(e[..., 1], e[..., 0])))
    return np.asarray((1 - comp["p"]) * vm(comp["e0"]) + comp["p"] * vm(comp["e1"]))


def _row_sample(kind: str, comp: dict[str, np.ndarray], rng: np.random.Generator) -> np.ndarray:
    """One simulated angle (radians) per draw and row: ``(S, n)``."""
    if kind == "projected_normal":
        e = comp["eta"]
        z = e + rng.standard_normal(e.shape)
        return np.asarray(np.arctan2(z[..., 1], z[..., 0]))

    def vm(e: np.ndarray) -> np.ndarray:
        mu = np.arctan2(e[..., 1], e[..., 0])
        k = np.hypot(e[..., 0], e[..., 1])
        return np.asarray(rng.vonmises(mu, k))

    if kind == "von_mises":
        return vm(comp["eta"])
    second = rng.random(comp["p"].shape) < comp["p"]
    e = np.where(second[..., None], comp["e1"], comp["e0"])
    return vm(e)


def _wrap_units(theta: np.ndarray, period: float) -> np.ndarray:
    """Radians -> ``units`` in [0, period)."""
    return np.asarray(np.mod(np.asarray(theta) / TWO_PI * period, period))


# ============================================================================ the model


class CircularRegressionModel(CustomModel[CircularRegressionSettings]):
    """Von Mises regression on harmonics of the hour and the season, and log-Fourier
    activity densities on the 24-hour clock (E81)."""

    id = "sci.circular_regression"
    version = "1.0.0"
    Settings = CircularRegressionSettings
    description = (
        "Angles (wind directions, times of day) on the circle. Regression kinds put the "
        "hour of day and the day of year into harmonic features and use a von Mises with a "
        "canonical link (or a projected normal, or a mixture of two regimes with a gate); "
        "kind activity fits a log-Fourier density per group (camera-trap times) with "
        "activity levels and overlaps. Outputs are mean directions, resultant lengths and "
        "predictive densities by block of the day or by group."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "simulate", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["mean_direction", "resultant_length", "density"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "direction_hat": {
            "dims": [OBS_DIM],
            "units_of": TARGET,
            "circular": True,
            "description": "Mean direction of the row's predictive distribution (expected), or "
            "a simulated angle (predictive), from 0 to one turn",
        },
        "resultant": {
            "dims": [OBS_DIM],
            "units": "1",
            "description": "Mean resultant length of the row's predictive distribution "
            "(1: one direction, 0: no preferred direction)",
        },
        "density": {
            "dims": [FRAME_DIM, ANGLE_DIM],
            "units": "1/unit",
            "description": "Predictive density per unit of the direction on a grid of angles, "
            "for each block of the day (regression) or group (activity)",
            "grid": {"y": FRAME_DIM, "x": ANGLE_DIM, "origin": "lower", "colormap": "viridis"},
        },
        "mean_direction": {
            "dims": [FRAME_DIM],
            "units_of": TARGET,
            "description": "Direction of the mean resultant vector of each frame",
        },
        "resultant_length": {
            "dims": [FRAME_DIM],
            "units": "1",
            "description": "Length of the mean resultant vector of each frame",
        },
        "activity_level": {
            "dims": [FRAME_DIM],
            "units": "1",
            "description": "kind activity: fraction of the day the group is active, "
            "1 / (2 pi max f)",
        },
        "overlap": {
            "dims": [FRAME_DIM, FRAME2_DIM],
            "units": "1",
            "description": "kind activity: coefficient of overlap of the activity densities "
            "of two groups",
            "grid": {"y": FRAME_DIM, "x": FRAME2_DIM, "origin": "upper", "colormap": "magma"},
        },
    }
    plots = (
        "core.convergence",
        "core.posterior_forest",
        "sci.rose",
        "sci.profile_bands",
        "sci.field_map",
    )
    resources = ResourceHint(est_fit_seconds=60)
    default_engine: ClassVar[str] = "pymc"
    pad_extend = False

    # ================================================================== fit
    def fit(
        self, ds: xr.Dataset, s: CircularRegressionSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        data = _read(ds, s, score=False)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        model = build_model(data, s)
        plan = EnginePlan.from_config(cfg, default_engine=self.default_engine, seed=seed)
        sink = ctx.checkpoint_sink if isinstance(ctx.checkpoint_sink, CheckpointSink) else None
        key = None
        if sink is not None:
            key = fit_key(
                model_id=self.id,
                version=self.version,
                settings=s.model_dump(mode="json"),
                fit_config=cfg.model_dump(mode="json"),
                data=ds,
            )
        report = None
        if ctx.on_progress is not None:

            def report(fraction: float, message: str) -> None:
                ctx.progress(0.85 * fraction, message)

        with _quiet():
            result = run_engine(
                model,
                plan,
                EngineHooks(progress=report, cancel=ctx.cancel, log=ctx.log, sink=sink, key=key),
            )
        ctx.check_cancelled()
        posterior = result.group("posterior")
        if posterior is None:
            raise MapiError(
                f"engine {result.engine!r} returned no posterior", code=ErrorCode.INTERNAL_ERROR
            )
        posterior = posterior[[n for n in _KEEP_VARS[s.kind] if n in posterior]]
        engine = result.engine
        groups: dict[str, xr.Dataset] = {"posterior": _clean(posterior, engine)}
        stats = result.group("sample_stats")
        if stats is not None:
            keep = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[keep], engine)
        obs_coord = {OBS_DIM: np.asarray(ds[OBS_DIM].values)}
        target = np.asarray(ds[TARGET].values, dtype=float)
        theta_01 = np.mod(data.theta, TWO_PI)
        groups["observed_data"] = _clean(
            xr.Dataset(
                {TARGET: ((OBS_DIM,), target), THETA: ((OBS_DIM,), theta_01)}, coords=obs_coord
            ),
            engine,
        )
        const: dict[str, Any] = {}
        if data.hour is not None:
            const[HOUR] = ((OBS_DIM,), np.asarray(ds[HOUR].values, dtype=float))
            const[DAY] = ((OBS_DIM,), np.asarray(ds[DAY].values, dtype=float))
        else:
            assert data.group_idx is not None
            const[f"{s.group_dim}_idx"] = ((OBS_DIM,), data.group_idx)
        groups["constant_data"] = _clean(xr.Dataset(const, coords=obs_coord), engine)
        ctx.progress(0.9, "posterior ready")
        draws = _Draws.of(posterior, _KEEP_VARS[s.kind])
        groups["posterior"] = _clean(
            xr.merge([posterior, self._posterior_fields(s, data, draws, posterior)]), engine
        )
        if cfg.keep.log_likelihood:
            ll = self._pointwise(s, data, draws)
            shape = (posterior.sizes["chain"], posterior.sizes["draw"], ll.shape[-1])
            groups["log_likelihood"] = _clean(
                xr.Dataset(
                    {TARGET: (("chain", "draw", OBS_DIM), ll.reshape(shape))},
                    coords={
                        "chain": posterior["chain"].values,
                        "draw": posterior["draw"].values,
                        **obs_coord,
                    },
                ),
                engine,
            )
        if cfg.keep.posterior_predictive:
            thin = (
                cfg.keep.posterior_predictive.thin
                if isinstance(cfg.keep.posterior_predictive, PosteriorPredictiveKeep)
                else 1
            )
            groups["posterior_predictive"] = _clean(
                self._replicates(s, data, posterior, thin, seed, obs_coord), engine
            )
        with _quiet():
            prior = pm.sample_prior_predictive(
                draws=200, model=model, var_names=list(FREE_VARS[s.kind]), random_seed=seed
            )
        groups["prior"] = _clean(prior["prior"].to_dataset(), engine)
        diagnostics = convergence_diagnostics(
            groups["posterior"], groups.get("sample_stats"), var_names=list(FREE_VARS[s.kind])
        )
        ctx.progress(1.0, "fit complete")
        learned: dict[str, Any] = {
            "engine": engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "kind": s.kind,
            "units": s.units,
            "rows": int(data.theta.size),
            "features": data.feature_names,
            "groups": data.groups,
        }
        return FitOutput(groups=groups, learned=learned, diagnostics=diagnostics)

    def _posterior_fields(
        self, s: CircularRegressionSettings, data: _Data, draws: _Draws, posterior: xr.Dataset
    ) -> xr.Dataset:
        """The gridded outputs as posterior variables: ``density`` and, for kind activity,
        ``overlap``, the same values a score returns, so that ``sci.field_map`` and
        ``sci.profile_bands`` draw them from the bundle of a fit run. The other frame
        outputs stay in the score: an angle such as ``mean_direction`` in the posterior
        would be the default variable of ``sci.rose``, which reads angles as radians."""
        none = replace(
            data,
            theta=data.theta[:0],
            hour=None if data.hour is None else data.hour[:0],
            features=None if data.features is None else data.features[:0],
            small=None if data.small is None else data.small[:0],
            group_idx=None if data.group_idx is None else data.group_idx[:0],
        )
        out = self._summaries(s, none, None if s.kind == "activity" else data, draws)
        fields, coords = _field_arrays(s, out)
        n_c, n_d = posterior.sizes["chain"], posterior.sizes["draw"]
        return xr.Dataset(
            {
                name: (("chain", "draw", *dims), arr.reshape(n_c, n_d, *arr.shape[1:]))
                for name, (dims, arr) in fields.items()
                if name in GRID_OUTPUTS
            },
            coords={
                "chain": posterior["chain"].values,
                "draw": posterior["draw"].values,
                **coords,
            },
        )

    @staticmethod
    def _pointwise(s: CircularRegressionSettings, data: _Data, draws: _Draws) -> np.ndarray:
        """Log density per radian of every row for every draw: ``(S, n)``."""
        n = data.theta.size
        out = np.empty((draws.size, n))
        if s.kind == "activity":
            assert data.group_idx is not None
            kk = np.arange(1, s.harmonics + 1)
            cos_n = np.cos(np.outer(data.theta, kk))
            sin_n = np.sin(np.outer(data.theta, kk))
            grid = np.arange(s.grid_points) * TWO_PI / s.grid_points
            cg, sg = np.cos(np.outer(kk, grid)), np.sin(np.outer(kk, grid))
            for lo in range(0, draws.size, _CHUNK):
                d = draws.take(slice(lo, lo + _CHUNK))
                a, b = d["a"], d["b"]  # S, G, K
                log_z = special.logsumexp(a @ cg + b @ sg, axis=-1) + math.log(
                    TWO_PI / s.grid_points
                )
                for gi in range(a.shape[1]):
                    rows = np.flatnonzero(data.group_idx == gi)
                    if rows.size:
                        out[lo : lo + a.shape[0], rows] = (
                            a[:, gi] @ cos_n[rows].T + b[:, gi] @ sin_n[rows].T - log_z[:, gi, None]
                        )
            return out
        for lo in range(0, draws.size, _CHUNK):
            d = draws.take(slice(lo, lo + _CHUNK))
            comp = _components(s.kind, d, data)
            out[lo : lo + len(next(iter(d.values())))] = _row_logpdf(s.kind, comp, data.theta)
        return out

    def _replicates(
        self,
        s: CircularRegressionSettings,
        data: _Data,
        posterior: xr.Dataset,
        thin: int,
        seed: int,
        obs_coord: dict[str, np.ndarray],
    ) -> xr.Dataset:
        """Replicated angles ``theta`` (radians, 0 to 2 pi) for every ``thin``-th draw."""
        sub = posterior.isel(draw=slice(None, None, thin))
        names = _KEEP_VARS[s.kind]
        draws = _Draws.of(sub, names)
        reps = self._simulate(s, data, draws, _rng(seed, "posterior_predictive"))
        shape = (sub.sizes["chain"], sub.sizes["draw"], data.theta.size)
        return xr.Dataset(
            {THETA: (("chain", "draw", OBS_DIM), np.mod(reps, TWO_PI).reshape(shape))},
            coords={"chain": sub["chain"].values, "draw": sub["draw"].values, **obs_coord},
        )

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        groups = [str(g) for g in fitted.learned.get("groups") or []]  # type: ignore[union-attr]
        data = _read(ds, s, score=True, groups=groups or None)
        post = fitted.posterior
        chains, idx = _draw_index(post, s.score_draws)
        sub = post.isel(chain=chains, draw=idx)
        draws = _Draws.of(sub, _KEEP_VARS[s.kind])
        ctx.check_cancelled()
        period = _period(s)
        noisy = ctx.mode == "simulate" or ctx.kind == "predictive"
        n_c, n_d = sub.sizes["chain"], sub.sizes["draw"]
        coords: dict[str, Any] = {
            "chain": np.asarray(sub["chain"].values),
            "draw": np.asarray(sub["draw"].values),
            OBS_DIM: np.asarray(ds[OBS_DIM].values),
        }
        out = self._summaries(s, data, self._fit_rows(fitted, s), draws)
        if noisy:
            sim = self._simulate(s, data, draws, _rng(ctx.seed, "score"))
            direction = _wrap_units(sim, period)
            how = ctx.kind if ctx.mode != "simulate" else "predictive"
            noise = "seeded_stream"
        else:
            direction = _wrap_units(np.angle(out["row_vector"]), period)
            how, noise = "expected", "none"

        def dr(arr: np.ndarray, dims: tuple[str, ...]) -> tuple[tuple[str, ...], np.ndarray]:
            return ("chain", "draw", *dims), arr.reshape(n_c, n_d, *arr.shape[1:])

        pred: dict[str, xr.DataArray] = {
            "direction_hat": _da(
                dr(direction, (OBS_DIM,)), {"kind": how, "noise": noise, "period": period}
            ),
            "resultant": _da(dr(np.abs(out["row_vector"]), (OBS_DIM,)), {"kind": "expected"}),
        }
        fields, field_coords = _field_arrays(s, out)
        for name, (dims, arr) in fields.items():
            pred[name] = _da(dr(arr, dims), {"kind": "posterior"})
        coords.update(field_coords)
        result = xr.Dataset(pred, coords=coords)
        return xr.DataTree.from_dict({PREDICTIONS: result})

    @staticmethod
    def _fit_rows(fitted: Fitted, s: CircularRegressionSettings) -> _Data | None:
        """The hour and day of every fit row (regression kinds), from ``constant_data``."""
        if s.kind == "activity":
            return None
        const = fitted.groups.get("constant_data")
        if const is None or HOUR not in const or DAY not in const:
            raise MapiError(
                f"the bundle of 'sci.circular_regression' has no constant_data/{HOUR}; refit "
                "the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        return _read(xr.Dataset({HOUR: const[HOUR], DAY: const[DAY]}), s, score=True)

    @staticmethod
    def _simulate(
        s: CircularRegressionSettings, data: _Data, draws: _Draws, rng: np.random.Generator
    ) -> np.ndarray:
        """Simulated angles (radians) ``(S, n)`` for every draw and row."""
        n = data.theta.size
        out = np.empty((draws.size, n))
        if s.kind == "activity":
            assert data.group_idx is not None
            g = s.grid_points
            grid = np.arange(g) * TWO_PI / g
            for lo in range(0, draws.size, _CHUNK):
                d = draws.take(slice(lo, lo + _CHUNK))
                f = log_fourier_density(d["a"], d["b"], grid, g)  # S, G, grid
                cdf = np.cumsum(f, axis=-1)
                cdf /= cdf[..., -1:]
                m = f.shape[0]
                u = rng.random((m, n))
                jitter = rng.random((m, n))
                for gi in range(f.shape[1]):
                    rows = np.flatnonzero(data.group_idx == gi)
                    if not rows.size:
                        continue
                    for j in range(m):
                        cell = np.searchsorted(cdf[j, gi], u[j, rows], side="right")
                        cell = np.minimum(cell, g - 1)
                        out[lo + j, rows] = (cell + jitter[j, rows] - 0.5) * TWO_PI / g
            return out
        for lo in range(0, draws.size, _CHUNK):
            d = draws.take(slice(lo, lo + _CHUNK))
            comp = _components(s.kind, d, data)
            block = _row_sample(s.kind, comp, rng)
            out[lo : lo + block.shape[0]] = block
        return out

    def _summaries(
        self,
        s: CircularRegressionSettings,
        data: _Data,
        frame_data: _Data | None,
        draws: _Draws,
    ) -> dict[str, Any]:
        """Row summaries of the scored rows ``data`` and frame summaries for every draw
        (densities per radian). Regression frames average over the fit rows
        ``frame_data`` (kept in ``constant_data``), so they do not depend on which rows a
        request scores."""
        n = data.theta.size
        angles = np.arange(s.angle_points) * TWO_PI / s.angle_points
        if s.kind == "activity":
            g = s.grid_points
            grid = np.arange(g) * TWO_PI / g
            n_g = len(data.groups)
            dens = np.empty((draws.size, n_g, s.angle_points))
            vec = np.empty((draws.size, n_g), dtype=complex)
            act = np.empty((draws.size, n_g))
            ov = np.empty((draws.size, n_g, n_g))
            for lo in range(0, draws.size, _CHUNK):
                d = draws.take(slice(lo, lo + _CHUNK))
                m = d["a"].shape[0]
                f = log_fourier_density(d["a"], d["b"], grid, g)
                dens[lo : lo + m] = log_fourier_density(d["a"], d["b"], angles, g)
                vec[lo : lo + m] = (f * np.exp(1j * grid)).sum(-1) * (TWO_PI / g)
                act[lo : lo + m] = 1.0 / (TWO_PI * f.max(-1))
                ov[lo : lo + m] = overlap(f, TWO_PI / g)
            assert data.group_idx is not None
            row = vec[:, data.group_idx] if n else np.zeros((draws.size, 0), dtype=complex)
            return {
                "row_vector": row,
                "frame_vector": vec,
                "density": dens,
                "activity": act,
                "overlap": ov,
                "frame_labels": np.asarray(data.groups),
            }
        assert frame_data is not None and frame_data.hour is not None
        width = 24.0 / s.frames
        block = np.minimum((frame_data.hour // width).astype(np.int64), s.frames - 1)
        rows_vec = np.empty((draws.size, n), dtype=complex)
        dens = np.full((draws.size, s.frames, s.angle_points), np.nan)
        frame_vec = np.full((draws.size, s.frames), np.nan, dtype=complex)
        members = [np.flatnonzero(block == f_i) for f_i in range(s.frames)]
        picks = []
        for rows in members:
            if rows.size > s.rows_per_frame:
                keep = np.linspace(0, rows.size - 1, s.rows_per_frame).round().astype(int)
                rows = rows[np.unique(keep)]
            picks.append(rows)
        for lo in range(0, draws.size, _CHUNK):
            d = draws.take(slice(lo, lo + _CHUNK))
            m = next(iter(d.values())).shape[0]
            if n:
                rows_vec[lo : lo + m] = _row_resultant(s.kind, _components(s.kind, d, data))
            comp = _components(s.kind, d, frame_data)
            fit_vec = _row_resultant(s.kind, comp)
            for f_i, rows in enumerate(members):
                if rows.size:
                    frame_vec[lo : lo + m, f_i] = fit_vec[:, rows].mean(axis=1)
            for f_i, rows in enumerate(picks):
                if not rows.size:
                    continue
                sub = {k: v[:, rows] for k, v in comp.items()}
                if s.kind == "regime_mixture":
                    lp = _mixture_grid(sub, angles)
                else:
                    e = sub["eta"][:, :, None, :]
                    fn = vm_logpdf if s.kind == "von_mises" else pn_logpdf
                    lp = fn(angles, e[..., 0], e[..., 1])
                dens[lo : lo + m, f_i] = np.exp(lp).mean(axis=1)
        return {
            "row_vector": rows_vec,
            "frame_vector": frame_vec,
            "density": dens,
            "frame_labels": np.arange(s.frames) * width,
        }

    # ================================================================== extras
    def holdout_log_score(self, fitted: Fitted, ds: xr.Dataset) -> np.ndarray:
        """Log predictive density (per radian) of each row of ``ds``, which holds the
        target: ``log mean_s p(theta_i | draw s)`` over every posterior draw (E81's held-out
        year)."""
        s = self.parse_settings(fitted.settings)
        groups = [str(g) for g in fitted.learned.get("groups") or []]  # type: ignore[union-attr]
        data = _read(ds, s, score=False, groups=groups or None)
        draws = _Draws.of(fitted.posterior, _KEEP_VARS[s.kind])
        ll = self._pointwise(s, data, draws)
        return np.asarray(special.logsumexp(ll, axis=0) - math.log(draws.size))


def _field_arrays(
    s: CircularRegressionSettings, out: dict[str, Any]
) -> tuple[dict[str, tuple[tuple[str, ...], np.ndarray]], dict[str, np.ndarray]]:
    """The frame outputs of :meth:`CircularRegressionModel._summaries` in ``units``:
    ``({name: (dims, (S, ...) array)}, coords)``."""
    period = _period(s)
    frame_vec = out["frame_vector"]
    fields: dict[str, tuple[tuple[str, ...], np.ndarray]] = {
        "density": ((FRAME_DIM, ANGLE_DIM), out["density"] * TWO_PI / period),
        "mean_direction": ((FRAME_DIM,), _wrap_units(np.angle(frame_vec), period)),
        "resultant_length": ((FRAME_DIM,), np.abs(frame_vec)),
    }
    coords: dict[str, np.ndarray] = {
        FRAME_DIM: np.asarray(out["frame_labels"]),
        ANGLE_DIM: np.arange(s.angle_points) * period / s.angle_points,
    }
    if s.kind == "activity":
        fields["activity_level"] = ((FRAME_DIM,), out["activity"])
        fields["overlap"] = ((FRAME_DIM, FRAME2_DIM), out["overlap"])
        coords[FRAME2_DIM] = np.asarray(out["frame_labels"])
    return fields, coords


def _mixture_grid(comp: dict[str, np.ndarray], angles: np.ndarray) -> np.ndarray:
    """Mixture log density ``(S, rows, A)`` on a grid of angles."""
    e0, e1 = comp["e0"][:, :, None, :], comp["e1"][:, :, None, :]
    p = comp["p"][:, :, None]
    with np.errstate(divide="ignore"):
        return np.asarray(
            np.logaddexp(
                np.log1p(-p) + vm_logpdf(angles, e0[..., 0], e0[..., 1]),
                np.log(p) + vm_logpdf(angles, e1[..., 0], e1[..., 1]),
            )
        )


def _da(spec: tuple[tuple[str, ...], np.ndarray], attrs: dict[str, Any]) -> xr.DataArray:
    dims, arr = spec
    return xr.DataArray(arr, dims=dims, attrs=attrs)
