"""``ts.bsts``: a Bayesian structural time series on ``pymc_extras.statespace``.

Canonical inputs (bind dataset names onto them in the spec)::

    dims  t: time, policy extend (evenly spaced periods)
    vars  y(t): target, float64, missing allowed (no update at a missing period)
          <regressors...>(t): drivers named in settings.regressors (future values required)

The model, on ``y`` and the drivers standardised over the fitted periods::

    y_t = Z x_t + x_t' beta + eps_t,     eps_t ~ Normal(0, sigma_obs)
    x_{t+1} = T x_t + R eta_t,           eta_t ~ Normal(0, Q)

with ``x`` the structural states: a trend (``level``: random-walk level; ``local_linear``:
level and slope both random walks; ``smooth``: a fixed level step with a random-walk
slope) and up to three seasonal patterns (one effect per season, or ``harmonics``
sine/cosine pairs; fixed or slowly changing). pymc-extras marginalises the states with its
Kalman filter, so NUTS samples only the variances, the initial states and ``beta``.

``beta`` is a static coefficient: pymc-extras keeps the regression coefficients as states
with an identity transition, and their block of the initial covariance ``P0`` is zero, so
the filter never re-estimates them (a diffuse block would leave ``beta`` at its prior).

**After sampling** (:func:`postprocess_draws`) the posterior is thinned to at most
:data:`SCORE_DRAWS` draws on a rectangular (chain, draw) grid. The system matrices of each
kept draw come from the same graph as the likelihood (``ss.unpack_statespace()``,
registered as deterministics after sampling and evaluated with
``pm.compute_deterministics``); the regression states are dropped and enter as
``d_t = x_t' beta``. A numpy Kalman filter and RTS smoother (:mod:`mapi_plugin_ts.ss_math`)
give the one-step-ahead predictions, the smoothed components and the filtered state at the
last fitted period, stored as the ``terminal_state`` group.

**Score modes** (outputs ``y_hat(chain, draw, t)`` and ``level(chain, draw, t)`` on the
thinned grid): ``predict``/``evaluate`` give the one-step-ahead prediction inside the fitted
range and a forecast from the terminal state after it (never conditioning on payload
targets); ``forecast`` the same over a horizon; ``filter`` runs the filter over new
observations that continue the fit and returns an **updated** ``terminal_state`` group.
Predictive draws use common random numbers seeded by the score seed.

Pointwise LOO is not offered: the Kalman likelihood is a sum over the whole series.
"""

from __future__ import annotations

import json
import logging
import math
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Final, Literal

import numpy as np
import pandas as pd
import pymc as pm
import xarray as xr
from mapi_pymc.checkpoint import CheckpointSink, fit_key
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_pymc.engines import EngineHooks, EnginePlan, EngineResult, run_engine
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, JsonValue, field_validator, model_validator

from mapi_plugin_ts.ss_math import (
    FilterResult,
    StructuralSystem,
    crn_normal,
    kalman_filter,
    one_step_draws,
    rts_smoother,
    simulate_ahead,
)

__all__ = [
    "COMPONENTS",
    "MAX_STATES",
    "ONE_STEP",
    "SCORE_DRAWS",
    "TARGET",
    "TERMINAL_STATE",
    "TIME_DIM",
    "BSTSFit",
    "BSTSModel",
    "BSTSSettings",
    "Scaling",
    "SeasonSettings",
    "StructureSettings",
    "Terminal",
    "Trend",
    "build_model",
    "build_statespace",
    "check_regular",
    "components_group",
    "fit_bsts",
    "forecast",
    "postprocess_draws",
    "read_terminal",
    "scale_regressors",
    "standardise",
    "system_from_posterior",
    "terminal_group",
    "track_bsts",
]

TIME_DIM: Final = "t"
TARGET: Final = "y"
SCORE_DRAWS: Final = 250
"""Draws kept for scoring (stride-thinned per chain: a rectangular (chain, draw) grid)."""
MAX_STATES: Final = 40
"""Cap on the structural states (trend plus seasonal states)."""
MAX_DUMMY_PERIOD: Final = 24
"""Longest period of a seasonality with one effect per season (no harmonics)."""

TERMINAL_STATE: Final = "terminal_state"
ONE_STEP: Final = "one_step"
COMPONENTS: Final = "components"
STATE: Final = "state"
STATE_AUX: Final = "state_aux"
REGRESSOR: Final = "regressor"
SAMPLE: Final = "sample"

_TREND: Final = "trend"
_REG: Final = "reg"
_OBS: Final = "obs"
_REG_PREFIX: Final = "x_"
_SS_VARS: Final = ("ss_x0", "ss_P0", "ss_T", "ss_Z0", "ss_R", "ss_H", "ss_Q")
_RESERVED: Final = frozenset({TARGET, TIME_DIM, "chain", "draw", SAMPLE, STATE, STATE_AUX})

Trend = Literal["level", "local_linear", "smooth"]
"""``level``: LevelTrend order 1; ``local_linear``: order 2, shocks on level and slope;
``smooth``: order 2, a shock on the slope only."""

_TREND_SHAPE: Final[dict[str, tuple[int, list[int]]]] = {
    "level": (1, [1]),
    "local_linear": (2, [1, 1]),
    "smooth": (2, [0, 1]),
}


# ===================================================================================== settings


class SeasonSettings(ModelSettings):
    """One seasonal pattern."""

    period: float = Field(
        ...,
        gt=1,
        le=1000,
        description="Length of the cycle in periods of the time dim (12 for monthly data "
        "with a yearly cycle, 52.18 for weekly data, 7 for daily data)",
    )
    harmonics: int | None = Field(
        None,
        ge=1,
        le=50,
        description="Number of sine/cosine pairs. Empty: one effect per season (needs a "
        f"whole-number period of at most {MAX_DUMMY_PERIOD})",
    )
    stochastic: bool = Field(False, description="Let the seasonal pattern change over time")
    sigma: float = Field(
        0.05, gt=0, le=5, description="Half-normal scale of the seasonal changes (standardised)"
    )

    @model_validator(mode="after")
    def _shape(self) -> SeasonSettings:
        if self.harmonics is None:
            if not float(self.period).is_integer() or self.period > MAX_DUMMY_PERIOD:
                raise ValueError(
                    f"a seasonality without harmonics needs a whole-number period of at most "
                    f"{MAX_DUMMY_PERIOD}; set harmonics for period {self.period:g}"
                )
        elif self.harmonics > self.period / 2:
            raise ValueError(
                f"harmonics must be at most period/2 ({self.period / 2:g}), got {self.harmonics}"
            )
        return self

    @property
    def n_states(self) -> int:
        if self.harmonics is None:
            return int(self.period) - 1
        return 2 * self.harmonics

    @property
    def label(self) -> str:
        """``"12"`` or ``"52.18x3"`` (period and harmonics, for tracking)."""
        p = f"{self.period:g}"
        return p if self.harmonics is None else f"{p}x{self.harmonics}"


class StructureSettings(ModelSettings):
    """The structural part shared by ``ts.bsts`` and ``causal.impact`` (standardised scale)."""

    trend: Trend = Field(
        "local_linear",
        description="level: a level that wanders; local_linear: a level and a slope that "
        "both wander; smooth: a slope that wanders slowly (a smooth trend)",
        json_schema_extra={"x-ui-level": "basic"},
    )
    seasonality: list[SeasonSettings] = Field(
        default_factory=list,
        max_length=3,
        description="Seasonal patterns (at most three)",
        json_schema_extra={"x-ui-level": "basic"},
    )
    level_sigma: float = Field(0.1, gt=0, le=5, description="Half-normal scale of level changes")
    slope_sigma: float = Field(0.02, gt=0, le=5, description="Half-normal scale of slope changes")
    obs_sigma: float = Field(0.5, gt=0, le=10, description="Half-normal scale of the noise")
    initial_sd: float = Field(
        1.0, gt=0, le=10, description="Prior sd of the initial states and seasonal effects"
    )
    regression_prior: Literal["normal", "horseshoe"] = Field(
        "normal",
        description="normal: every driver has an effect; horseshoe: most drivers have none",
    )
    regression_sigma: float = Field(
        1.0, gt=0, le=10, description="Prior scale of the driver effects (standardised)"
    )

    @model_validator(mode="after")
    def _states(self) -> StructureSettings:
        n = structural_states(self)
        if n > MAX_STATES:
            raise ValueError(
                f"the model has {n} structural states, more than {MAX_STATES}; use fewer "
                "seasonal effects (harmonics instead of one effect per season)"
            )
        return self


class BSTSSettings(StructureSettings):
    """Settings of ``ts.bsts``."""

    regressors: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="Driver variables (prices, promotions) whose future values are supplied "
        "for forecasts",
        json_schema_extra={"x-ui-level": "basic"},
    )

    @field_validator("regressors")
    @classmethod
    def _regressors(cls, v: list[str]) -> list[str]:
        return check_names(v, "regressors")


def check_names(v: list[str], what: str) -> list[str]:
    """Driver names: unique identifiers that are not canonical names (like statsmodels' exog)."""
    if len(set(v)) != len(v):
        raise ValueError(f"{what} must not repeat a variable")
    bad = sorted({x for x in v if x in _RESERVED or not x.isidentifier()})
    if bad:
        raise ValueError(f"{what} cannot use the names {bad}")
    return v


def structural_states(s: StructureSettings) -> int:
    """Number of structural states (trend plus seasonal states, no regression)."""
    return _TREND_SHAPE[s.trend][0] + sum(season.n_states for season in s.seasonality)


# ====================================================================================== scaling


@dataclass(slots=True)
class Scaling:
    """Location/scale of ``y`` and of every regressor over the fitted periods."""

    y_loc: float
    y_scale: float
    x_loc: list[float]
    x_scale: list[float]

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, doc: str | Mapping[str, Any]) -> Scaling:
        raw = json.loads(doc) if isinstance(doc, str) else dict(doc)
        return cls(
            y_loc=float(raw["y_loc"]),
            y_scale=float(raw["y_scale"]),
            x_loc=[float(v) for v in raw["x_loc"]],
            x_scale=[float(v) for v in raw["x_scale"]],
        )


def standardise(
    y: np.ndarray, X: np.ndarray | None, names: Sequence[str]
) -> tuple[np.ndarray, np.ndarray | None, Scaling]:
    """``y`` and ``X`` (``(n, p)``) standardised over the observed ``y`` / every period."""
    y = np.asarray(y, dtype=float).reshape(-1)
    obs = y[np.isfinite(y)]
    if obs.size < 3:
        raise MapiError(
            f"the target has {obs.size} observed periods; the model needs at least 3",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/data",
        )
    loc, scale = float(obs.mean()), float(obs.std())
    if not scale > 0:
        raise MapiError(
            "the target is constant over the fitted periods", code=ErrorCode.SCHEMA_INVALID
        )
    if X is None or X.shape[1] == 0:
        return (y - loc) / scale, None, Scaling(loc, scale, [], [])
    X = np.asarray(X, dtype=float)
    x_loc = X.mean(axis=0)
    x_scale = X.std(axis=0)
    flat = [n for n, sd in zip(names, x_scale, strict=True) if not sd > 0]
    if flat:
        raise MapiError(
            f"the drivers {flat} are constant over the fitted periods; remove them",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/model/settings",
        )
    scaling = Scaling(loc, scale, x_loc.tolist(), x_scale.tolist())
    return (y - loc) / scale, scale_regressors(X, scaling), scaling


def scale_regressors(X: np.ndarray, scaling: Scaling) -> np.ndarray:
    """``X`` (``(n, p)``) on the standardised scale of the fit."""
    return (np.asarray(X, dtype=float) - np.asarray(scaling.x_loc)) / np.asarray(scaling.x_scale)


# ======================================================================================= model


def build_statespace(s: StructureSettings, regressors: Sequence[str]) -> Any:
    """The pymc-extras structural model (``PyMCStateSpace``) for ``s`` and ``regressors``."""
    from pymc_extras.statespace import structural as st

    order, innovations = _TREND_SHAPE[s.trend]
    comp = st.LevelTrend(order=order, innovations_order=innovations, name=_TREND)
    for i, season in enumerate(s.seasonality):
        name = f"season_{i}"
        if season.harmonics is None:
            comp = comp + st.TimeSeasonality(
                season_length=int(season.period), innovations=season.stochastic, name=name
            )
        else:
            comp = comp + st.FrequencySeasonality(
                season_length=season.period,
                n=season.harmonics,
                innovations=season.stochastic,
                name=name,
            )
    if regressors:
        comp = comp + st.Regression(
            name=_REG, state_names=[f"{_REG_PREFIX}{r}" for r in regressors]
        )
    comp = comp + st.MeasurementError(name=_OBS)
    with _quiet():
        return comp.build(verbose=False)


def build_model(
    s: StructureSettings, y_std: np.ndarray, X_std: np.ndarray | None, regressors: Sequence[str]
) -> tuple[pm.Model, Any]:
    """The PyMC model (Kalman likelihood of ``y_std``) and its state-space object."""
    import pytensor.tensor as pt

    ss = build_statespace(s, regressors)
    y_std = np.asarray(y_std, dtype=float).reshape(-1)
    coords = {k: list(v) for k, v in ss.coords.items()}
    coords["time"] = list(range(y_std.size))
    seasons = {f"season_{i}": season for i, season in enumerate(s.seasonality)}
    with pm.Model(coords=coords) as model:
        for name in ss.param_names:
            if name == "P0":
                continue
            info = ss.param_info[name]
            dims = ss.param_dims.get(name)
            shape = info["shape"]
            if name == f"sigma_{_TREND}" and dims:  # one scale per shock (level, trend)
                scale = [
                    s.level_sigma if lab == "level" else s.slope_sigma for lab in ss.coords[dims[0]]
                ]
                pm.HalfNormal(name, sigma=np.array(scale), dims=dims)
            elif name == f"beta_{_REG}":
                _regression_prior(name, s, dims)
            elif name == f"sigma_{_OBS}":
                pm.HalfNormal(name, sigma=s.obs_sigma, dims=dims)
            elif name.startswith("sigma_") and name[len("sigma_") :] in seasons:
                pm.HalfNormal(name, sigma=seasons[name[len("sigma_") :]].sigma, dims=dims)
            elif info.get("constraints") == "Positive":
                pm.HalfNormal(name, sigma=s.initial_sd, dims=dims, shape=None if dims else shape)
            else:  # initial_trend, params_season_<i> and anything new: the initial states
                pm.Normal(name, 0.0, s.initial_sd, dims=dims, shape=None if dims else shape)
        reg = set(_regression_states(ss))
        diag = np.array([0.0 if n in reg else s.initial_sd**2 for n in ss.state_names])
        pm.Deterministic("P0", pt.diag(pt.as_tensor_variable(diag)), dims=(STATE, STATE_AUX))
        if regressors:
            if X_std is None:
                raise ValueError("regressors without data")
            pm.Data(f"data_{_REG}", np.asarray(X_std, dtype=float), dims=("time", f"state_{_REG}"))
        with _quiet():
            ss.build_statespace_graph(y_std[:, None])
    return model, ss


def _regression_prior(name: str, s: StructureSettings, dims: Any) -> None:
    if s.regression_prior == "normal":
        pm.Normal(name, 0.0, s.regression_sigma, dims=dims)
        return
    # non-centred regularised horseshoe: a small global scale (most drivers have no
    # effect), heavy-tailed local scales, and a slab of scale ~regression_sigma
    z = pm.Normal(f"{name}_z", 0.0, 1.0, dims=dims)
    lam = pm.HalfCauchy(f"{name}_lambda", 1.0, dims=dims)
    tau = pm.HalfCauchy(f"{name}_tau", 0.1)
    c2 = pm.InverseGamma(f"{name}_c2", 2.0, 2.0 * s.regression_sigma**2)
    lam_t = pm.math.sqrt(c2 * lam**2 / (c2 + tau**2 * lam**2))
    pm.Deterministic(name, z * tau * lam_t, dims=dims)


def _regression_states(ss: Any) -> list[str]:
    return [n for n in ss.state_names if n.startswith(_REG_PREFIX)]


# ======================================================================================== fit


@dataclass(slots=True)
class BSTSFit:
    """Everything a fit leaves for scoring and for ``causal.impact``.

    ``filt`` runs over the fitted series; it carries the per-step state means
    (``pred_mean``/``filt_mean``) but not the per-step covariances (memory: ``S·n·k²``).
    """

    result: EngineResult | None
    posterior: xr.Dataset
    sample_stats: xr.Dataset | None
    chain: np.ndarray  # the thinned score grid (coords of the original posterior)
    draw: np.ndarray
    system: StructuralSystem  # on that grid, S = chain.size * draw.size
    beta: np.ndarray  # (S, p) standardised coefficients (p = 0: shape (S, 0))
    scaling: Scaling
    X_std: np.ndarray | None
    filt: FilterResult
    smoothed: np.ndarray  # (S, n, k)
    state_names: list[str]
    components: dict[str, np.ndarray]  # name -> (k,) loadings: "level", "season_<i>"
    learned: dict[str, JsonValue]

    @property
    def d(self) -> np.ndarray | None:
        """``(S, n)`` regression term ``X_std beta`` (None without regressors)."""
        if self.X_std is None or self.beta.shape[1] == 0:
            return None
        return np.asarray(self.beta @ self.X_std.T)


@dataclass(slots=True)
class Terminal:
    """The ``terminal_state`` group, read back (standardised scale)."""

    system: StructuralSystem
    beta: np.ndarray
    mean: np.ndarray
    cov: np.ndarray
    chain: np.ndarray
    draw: np.ndarray
    scaling: Scaling
    last_label: str
    n_fit: int


def fit_bsts(
    y: np.ndarray,
    X: np.ndarray | None,
    regressors: Sequence[str],
    s: StructureSettings,
    cfg: FitConfig,
    ctx: FitContext,
    *,
    model_id: str,
    version: str,
    data: xr.Dataset,
    progress: tuple[float, float] = (0.0, 0.85),
) -> BSTSFit:
    """Standardise, sample the Kalman likelihood and post-process (see the module doc).

    ``progress`` is the window of the fit's progress bar the sampling reports into.
    """
    y_std, X_std, scaling = standardise(y, X, regressors)
    model, ss = build_model(s, y_std, X_std, regressors)
    seed = cfg.seed if cfg.seed is not None else ctx.seed
    plan = EnginePlan.from_config(cfg, default_engine=BSTSModel.default_engine, seed=seed)
    sink = ctx.checkpoint_sink if isinstance(ctx.checkpoint_sink, CheckpointSink) else None
    key = None
    if sink is not None:
        key = fit_key(
            model_id=model_id,
            version=version,
            settings=s.model_dump(mode="json"),
            fit_config=cfg.model_dump(mode="json"),
            data=data,
        )
    lo, hi = progress
    report = None
    if ctx.on_progress is not None:

        def report(fraction: float, message: str) -> None:
            ctx.progress(lo + (hi - lo) * fraction, message)

    with _quiet():
        result = run_engine(
            model, plan, EngineHooks.for_context(ctx, progress=report, sink=sink, key=key)
        )
    ctx.check_cancelled()
    posterior = result.group("posterior")
    if posterior is None:
        raise MapiError(
            f"engine {result.engine!r} returned no posterior", code=ErrorCode.INTERNAL_ERROR
        )
    ctx.progress(hi, "filtering the states")
    learned: dict[str, JsonValue] = {
        "engine": result.engine,
        "tune": result.plan.tune,
        "draws": result.plan.draws,
        "chains": result.plan.chains,
        "resumed_from": result.resumed_from,
        "checkpoints": result.checkpoints,
    }
    if result.notes:
        learned["notes"] = list(result.notes)
    return postprocess_draws(
        model,
        ss,
        posterior,
        regressors,
        y_std=y_std,
        X_std=X_std,
        scaling=scaling,
        result=result,
        sample_stats=result.group("sample_stats"),
        learned=learned,
    )


def postprocess_draws(
    model: pm.Model,
    ss: Any,
    posterior: xr.Dataset,
    regressors: Sequence[str],
    *,
    y_std: np.ndarray,
    X_std: np.ndarray | None,
    scaling: Scaling,
    result: EngineResult | None = None,
    sample_stats: xr.Dataset | None = None,
    learned: dict[str, JsonValue] | None = None,
) -> BSTSFit:
    """Everything after sampling: thin, system matrices, filter, smoother, components.

    ``posterior`` holds (chain, draw) draws of the model's free variables (tests pass prior
    draws). The returned ``posterior`` keeps every draw of the free variables and
    deterministics, without ``P0`` and the ``ss_*`` matrices.
    """
    chain, draw = _score_grid(posterior)
    thinned = posterior.sel(chain=chain, draw=draw)
    system, beta = system_from_posterior(model, ss, thinned, regressors)
    y_std = np.asarray(y_std, dtype=float).reshape(-1)
    d = None if X_std is None or beta.shape[1] == 0 else beta @ np.asarray(X_std).T
    filt, smoothed = _filter_and_smooth(system, y_std, d)
    reg = set(_regression_states(ss))
    names = [n for n in ss.state_names if n not in reg]
    comps = _component_loadings(ss, system, names)
    keep = [str(v) for v in posterior.data_vars if v != "P0" and v not in _SS_VARS]
    clean = posterior[keep]
    lacking = [
        d.name
        for d in model.deterministics
        if d.name not in clean.data_vars and d.name != "P0" and d.name not in _SS_VARS
    ]
    if lacking:  # e.g. the horseshoe's beta_reg when the draws hold free variables only
        with _quiet():
            det = pm.compute_deterministics(
                clean, var_names=lacking, model=model, progressbar=False
            )
        clean = clean.assign({name: det[name] for name in lacking})
    reg_dim = f"state_{_REG}"
    if reg_dim in clean.dims:  # coords x_<name> -> <name>
        clean = clean.assign_coords({reg_dim: list(regressors)})
    n_missing = int((~np.isfinite(y_std)).sum())
    out_learned: dict[str, JsonValue] = dict(learned or {})
    out_learned.update(
        {
            "scaling": asdict(scaling),
            "state_names": list(names),
            "n_states": len(names),
            "missing_periods": n_missing,
            "regressors": list(regressors),
            "score_draws": int(chain.size * draw.size),
        }
    )
    return BSTSFit(
        result=result,
        posterior=clean,
        sample_stats=sample_stats,
        chain=chain,
        draw=draw,
        system=system,
        beta=beta,
        scaling=scaling,
        X_std=None if X_std is None else np.asarray(X_std, dtype=float),
        filt=filt,
        smoothed=smoothed,
        state_names=names,
        components=comps,
        learned=out_learned,
    )


def _score_grid(posterior: xr.Dataset) -> tuple[np.ndarray, np.ndarray]:
    """Chain and draw coords of at most :data:`SCORE_DRAWS` draws (a per-chain stride)."""
    chain = np.asarray(posterior["chain"].values)
    draw = np.asarray(posterior["draw"].values)
    per_chain = max(1, SCORE_DRAWS // max(chain.size, 1))
    if chain.size > SCORE_DRAWS:
        chain = chain[:SCORE_DRAWS]
    stride = max(1, draw.size // per_chain)
    return chain, draw[::stride][:per_chain]


def _filter_and_smooth(
    system: StructuralSystem, y_std: np.ndarray, d: np.ndarray | None
) -> tuple[FilterResult, np.ndarray]:
    """The filter over every draw and the smoothed means, in draw chunks (bounded memory)."""
    s_len, k, n = system.S, system.k, y_std.size
    full = kalman_filter(system, y_std, d)
    pred_mean = np.empty((s_len, n, k))
    filt_mean = np.empty((s_len, n, k))
    smoothed = np.empty((s_len, n, k))
    chunk = max(1, int(64e6 // max(16 * n * k * k, 1)))
    for lo in range(0, s_len, chunk):
        idx = np.arange(lo, min(lo + chunk, s_len))
        sub = kalman_filter(system.take(idx), y_std, None if d is None else d[idx], keep=True)
        assert sub.pred_mean is not None and sub.filt_mean is not None
        pred_mean[idx] = sub.pred_mean
        filt_mean[idx] = sub.filt_mean
        smoothed[idx] = rts_smoother(system.take(idx), sub)
    filt = FilterResult(
        mean=full.mean,
        var=full.var,
        last_mean=full.last_mean,
        last_cov=full.last_cov,
        loglik=full.loglik,
        pred_mean=pred_mean,
        filt_mean=filt_mean,
    )
    return filt, smoothed


def system_from_posterior(
    model: pm.Model, ss: Any, posterior: xr.Dataset, regressors: Sequence[str]
) -> tuple[StructuralSystem, np.ndarray]:
    """The structural system and ``beta`` ``(S, p)`` of every draw in ``posterior``.

    The matrices come from ``ss.unpack_statespace()`` (the likelihood's own graph),
    registered as deterministics in ``model`` and evaluated on the draws; the regression
    states are dropped (their initial values are ``beta``).
    """
    x0, P0, _c, _d, T, Z, R, H, Q = ss.unpack_statespace()
    exprs = dict(zip(_SS_VARS, (x0, P0, T, Z[0] if Z.ndim == 3 else Z, R, H, Q), strict=True))
    with model:
        for name, expr in exprs.items():
            if name not in model.named_vars:
                pm.Deterministic(name, expr)
    free = [v.name for v in model.free_RVs]
    missing = [v for v in free if v not in posterior.data_vars]
    if missing:
        raise MapiError(
            f"the posterior lacks the variables {missing}", code=ErrorCode.INTERNAL_ERROR
        )
    with _quiet():
        det = pm.compute_deterministics(
            posterior[free], var_names=list(_SS_VARS), model=model, progressbar=False
        )

    def flat(name: str) -> np.ndarray:
        arr = np.asarray(det[name].transpose("chain", "draw", ...).values, dtype=float)
        return arr.reshape(-1, *arr.shape[2:])

    names = list(ss.state_names)
    reg = set(_regression_states(ss))
    keep = np.array([i for i, n in enumerate(names) if n not in reg], dtype=np.int64)
    regi = np.array([names.index(f"{_REG_PREFIX}{r}") for r in regressors], dtype=np.int64)
    x0_v, P0_v, T_v = flat("ss_x0"), flat("ss_P0"), flat("ss_T")
    Z_v, R_v, H_v, Q_v = flat("ss_Z0"), flat("ss_R"), flat("ss_H"), flat("ss_Q")
    RQR = R_v @ Q_v @ np.swapaxes(R_v, -1, -2)
    grid = np.ix_(keep, keep)
    system = StructuralSystem(
        T=np.ascontiguousarray(T_v[:, grid[0], grid[1]]),
        Z=np.ascontiguousarray(Z_v[:, 0, keep]),
        RQR=np.ascontiguousarray(RQR[:, grid[0], grid[1]]),
        H=np.ascontiguousarray(H_v[:, 0, 0]),
        x0=np.ascontiguousarray(x0_v[:, keep]),
        P0=np.ascontiguousarray(P0_v[:, grid[0], grid[1]]),
    )
    beta = np.ascontiguousarray(x0_v[:, regi]) if regi.size else np.zeros((system.S, 0))
    return system, beta


def _component_loadings(
    ss: Any, system: StructuralSystem, names: list[str]
) -> dict[str, np.ndarray]:
    """Design loadings ``(k,)`` of the trend (``level``) and of each seasonality."""
    z = system.Z[0] if system.S else np.zeros(len(names))
    groups = {"level": set(ss.coords[f"state_{_TREND}"])}
    i = 0
    while f"state_season_{i}" in ss.coords:  # the component's states (pymc-extras coords)
        groups[f"season_{i}"] = set(ss.coords[f"state_season_{i}"])
        i += 1
    return {
        comp: z * np.array([1.0 if n in members else 0.0 for n in names])
        for comp, members in groups.items()
    }


# ================================================================================== groups


def components_group(
    fit: BSTSFit, times: np.ndarray, dim: str, *, sample_index: np.ndarray | None = None
) -> xr.Dataset:
    """Smoothed components per draw on the model-stage scale of ``y`` (I-4 ``components``).

    ``level`` carries ``y_loc``; ``season_<i>`` and ``regression`` are zero-centred parts,
    so the variables sum to the smoothed expected value of ``y``.
    """
    idx = np.arange(fit.system.S) if sample_index is None else np.asarray(sample_index)
    sm = fit.smoothed[idx]  # (S', n, k)
    sc = fit.scaling
    parts: dict[str, Any] = {}
    for name, load in fit.components.items():
        val = np.einsum("snk,k->sn", sm, load) * sc.y_scale
        if name == "level":
            val = val + sc.y_loc
        parts[name] = ((SAMPLE, dim), val)
    d = fit.d
    if d is not None:
        parts["regression"] = ((SAMPLE, dim), d[idx] * sc.y_scale)
    return xr.Dataset(parts, coords={SAMPLE: np.arange(idx.size), dim: np.asarray(times)})


def terminal_group(
    fit: BSTSFit, last_label: Any, *, extra: Mapping[str, Any] | None = None
) -> xr.Dataset:
    """The filtered state at the last fitted period with the system (I-4 ``terminal_state``)."""
    regs = fit.learned.get("regressors")
    return _terminal(
        fit.system,
        fit.beta,
        fit.filt.last_mean,
        fit.filt.last_cov,
        chain=fit.chain,
        draw=fit.draw,
        state_names=fit.state_names,
        regressors=[str(r) for r in regs] if isinstance(regs, list) else [],
        scaling=fit.scaling,
        last_label=label_text(last_label),
        n_fit=int(fit.filt.mean.shape[1]),
        components=fit.components,
        extra=extra,
    )


def _terminal(
    system: StructuralSystem,
    beta: np.ndarray,
    mean: np.ndarray,
    cov: np.ndarray,
    *,
    chain: np.ndarray,
    draw: np.ndarray,
    state_names: Sequence[str],
    regressors: Sequence[str],
    scaling: Scaling,
    last_label: str,
    n_fit: int,
    components: Mapping[str, np.ndarray] | None,
    extra: Mapping[str, Any] | None = None,
) -> xr.Dataset:
    shape = (chain.size, draw.size)
    k = system.k
    cd = ("chain", "draw")
    data: dict[str, Any] = {
        "T": ((*cd, STATE, STATE_AUX), system.T.reshape(*shape, k, k)),
        "RQR": ((*cd, STATE, STATE_AUX), system.RQR.reshape(*shape, k, k)),
        "Z": ((*cd, STATE), system.Z.reshape(*shape, k)),
        "H": (cd, system.H.reshape(shape)),
        "mean": ((*cd, STATE), np.asarray(mean).reshape(*shape, k)),
        "cov": ((*cd, STATE, STATE_AUX), np.asarray(cov).reshape(*shape, k, k)),
    }
    coords: dict[str, Any] = {
        "chain": chain,
        "draw": draw,
        STATE: list(state_names),
        STATE_AUX: list(state_names),
    }
    if beta.shape[1]:
        data["beta"] = ((*cd, REGRESSOR), beta.reshape(*shape, -1))
        coords[REGRESSOR] = list(regressors)
    attrs: dict[str, Any] = {
        "last_label": last_label,
        "n_fit": int(n_fit),
        "scaling": scaling.to_json(),
    }
    if components is not None:
        attrs["components"] = json.dumps({c: np.asarray(v).tolist() for c, v in components.items()})
    attrs.update(extra or {})
    return xr.Dataset(data, coords=coords, attrs=attrs)


def read_terminal(ds: xr.Dataset) -> Terminal:
    """A :class:`Terminal` from a ``terminal_state`` group (flattened over (chain, draw))."""
    try:
        chain = np.asarray(ds["chain"].values)
        draw = np.asarray(ds["draw"].values)

        def flat(name: str, *dims: str) -> np.ndarray:
            arr = np.asarray(ds[name].transpose("chain", "draw", *dims).values, dtype=float)
            return arr.reshape(-1, *arr.shape[2:])

        T = flat("T", STATE, STATE_AUX)
        mean = flat("mean", STATE)
        cov = flat("cov", STATE, STATE_AUX)
        system = StructuralSystem(
            T=T,
            Z=flat("Z", STATE),
            RQR=flat("RQR", STATE, STATE_AUX),
            H=flat("H"),
            x0=mean,
            P0=cov,
        )
        beta = flat("beta", REGRESSOR) if "beta" in ds else np.zeros((T.shape[0], 0))
        return Terminal(
            system=system,
            beta=beta,
            mean=mean,
            cov=cov,
            chain=chain,
            draw=draw,
            scaling=Scaling.from_json(str(ds.attrs["scaling"])),
            last_label=str(ds.attrs["last_label"]),
            n_fit=int(ds.attrs["n_fit"]),
        )
    except (KeyError, ValueError) as exc:
        raise MapiError(
            f"the terminal state is incomplete ({exc}); refit the model",
            code=ErrorCode.INTEGRITY_FAILURE,
        ) from None


def forecast(
    term: Terminal, X_future: np.ndarray | None, h: int, *, seed: int, name: str
) -> tuple[np.ndarray, np.ndarray]:
    """Expected values and predictive draws ``(S, h)`` of ``h`` periods after the terminal
    state, in the model-stage units of ``y`` (``X_future`` ``(h, p)`` unscaled)."""
    d = None
    if term.beta.shape[1]:
        if X_future is None or np.asarray(X_future).shape != (h, term.beta.shape[1]):
            raise MapiError(
                "the forecast needs future values of every driver",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer="/data",
            )
        d = term.beta @ scale_regressors(X_future, term.scaling).T
    expected, predictive = simulate_ahead(
        term.system, term.mean, term.cov, h, d, seed=seed, name=name
    )
    sc = term.scaling
    return expected * sc.y_scale + sc.y_loc, predictive * sc.y_scale + sc.y_loc


def label_text(label: Any) -> str:
    """A time label as text: ISO date (or date-time) for datetimes, the integer otherwise."""
    if isinstance(label, np.datetime64 | pd.Timestamp):
        ts = pd.Timestamp(label)
        return ts.date().isoformat() if ts == ts.normalize() else ts.isoformat()
    if isinstance(label, np.integer | int):
        return str(int(label))
    return str(label)


# ==================================================================================== plugin

_TEMPLATE: dict[str, Any] = {
    "params": {"regressors": {"type": "string_list", "default": []}},
    "template": {
        "dims": {TIME_DIM: {"kind": "time", "policy": "extend"}},
        "vars": {
            TARGET: {
                "dims": [TIME_DIM],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
            },
            "{*regressors as x}": {
                "dims": [TIME_DIM],
                "dtype": "float64",
                "role": "covariate",
                "future_required": True,
            },
        },
    },
}


class BSTSModel(CustomModel[BSTSSettings]):
    """Bayesian structural time series with a terminal state, forecasting and filtering."""

    id = "ts.bsts"
    version = "1.0.0"
    Settings = BSTSSettings
    description = (
        "A Bayesian structural time series: a trend, seasonal patterns and drivers, fitted "
        "with the Kalman filter of pymc-extras. Forecasts and filters start from the stored "
        "terminal state."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "filter", "evaluate"}),
        loo_valid=False,
        loo_invalid_reason="the Kalman likelihood is a sum over the whole series; steps "
        "depend on each other, so pointwise leave-one-out is not defined",
        owns=frozenset({"scaling"}),
        identified_quantities=["sigma_obs"],
        deterministic_given_seed=False,
        checkpointable=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [TIME_DIM],
            "units_of": TARGET,
            "description": "Expected value or predictive draw: one-step-ahead inside the "
            "fitted range, a forecast after it",
        },
        "level": {
            "dims": [TIME_DIM],
            "units_of": TARGET,
            "description": "The trend and level component (expected)",
        },
    }
    plots = ("core.ppc_fan", "core.decomposition", "core.convergence", "core.posterior_forest")
    pad_extend = False
    default_engine: ClassVar[str] = "nutpie"

    # ================================================================== fit
    def fit(self, ds: xr.Dataset, s: BSTSSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        y, X, times = _fit_arrays(ds, s.regressors)
        freq = check_regular(times)
        fit = fit_bsts(
            y, X, s.regressors, s, cfg, ctx, model_id=self.id, version=self.version, data=ds
        )
        ctx.check_cancelled()
        engine = fit.result.engine if fit.result is not None else "prior"
        groups: dict[str, xr.Dataset] = {"posterior": _clean(fit.posterior, engine)}
        if fit.sample_stats is not None:
            stats = fit.sample_stats
            keep = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[keep], engine)
        groups["observed_data"] = _clean(
            xr.Dataset({TARGET: ((TIME_DIM,), y)}, coords={TIME_DIM: times}), engine
        )
        if X is not None:
            groups["constant_data"] = _clean(
                xr.Dataset(
                    {r: ((TIME_DIM,), X[:, j]) for j, r in enumerate(s.regressors)},
                    coords={TIME_DIM: times},
                ),
                engine,
            )
        groups[ONE_STEP] = one_step_group(fit, times, TIME_DIM)
        groups[COMPONENTS] = _to_original(components_group(fit, times, TIME_DIM), ctx)
        terminal = terminal_group(fit, times[-1], extra={"freq": freq or ""})
        learned: dict[str, JsonValue] = {
            **fit.learned,
            "seasonality": [season.label for season in s.seasonality],
            "first_label": label_text(times[0]),
            "last_label": label_text(times[-1]),
            "freq": freq,
            "n_fit": int(times.size),
        }
        if cfg.keep.log_likelihood:
            notes = list(learned.get("notes", []) or [])  # type: ignore[arg-type]
            notes.append("log_likelihood not stored: pointwise LOO is not valid for BSTS")
            learned["notes"] = notes
        track_bsts(ctx, fit, s, y, times)
        ctx.progress(1.0, "posterior ready")
        return FitOutput(groups=groups, learned=learned, terminal_state=terminal)

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        names = [str(v) for v in post.data_vars]
        return convergence_diagnostics(post, fitted.groups.get("sample_stats"), var_names=names)

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        if fitted.terminal_state is None or ONE_STEP not in fitted.groups:
            raise MapiError(
                "the fitted model has no terminal state; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        s = self.parse_settings(fitted.settings)
        term = read_terminal(fitted.terminal_state)
        labels = np.asarray(ds[TIME_DIM].values)
        ctx.check_cancelled()
        extra: dict[str, xr.Dataset] = {}
        if ctx.mode == "filter":
            y_hat, level, new_term = self._filter_new(fitted, ds, s, term, labels, ctx)
            extra[TERMINAL_STATE] = new_term
            noise = "crn_seeded" if ctx.kind == "predictive" else "none"
        else:
            y_hat, level, noise = self._predict(fitted, ds, s, term, labels, ctx)
        shape = (term.chain.size, term.draw.size)
        dims = ("chain", "draw", TIME_DIM)
        predictions = xr.Dataset(
            {
                "y_hat": xr.DataArray(
                    y_hat.reshape(*shape, -1), dims=dims, attrs={"kind": ctx.kind, "noise": noise}
                ),
                "level": xr.DataArray(
                    level.reshape(*shape, -1), dims=dims, attrs={"kind": "expected"}
                ),
            },
            coords={"chain": term.chain, "draw": term.draw, TIME_DIM: labels},
        )
        return xr.DataTree.from_dict({PREDICTIONS: predictions, **extra})

    def _predict(
        self,
        fitted: Fitted,
        ds: xr.Dataset,
        s: BSTSSettings,
        term: Terminal,
        labels: np.ndarray,
        ctx: ScoreContext,
    ) -> tuple[np.ndarray, np.ndarray, str]:
        one = fitted.groups[ONE_STEP]
        fit_t = np.asarray(one[TIME_DIM].values)
        sc = term.scaling
        s_len = term.system.S
        y_hat = np.empty((s_len, labels.size))
        level = np.empty((s_len, labels.size))
        inside = np.isin(labels, fit_t)
        after = _steps_after(labels, term.last_label, fit_t, fitted.learned.get("freq"))
        bad = ~inside & (after < 1)
        if bad.any():
            raise MapiError(
                "score periods inside the fitted range must be fitted periods",
                code=ErrorCode.COORD_NOT_ALLOWED,
                pointer="/data",
            )
        predictive = ctx.kind == "predictive"
        if inside.any():
            pos = np.searchsorted(fit_t, labels[inside])

            def flat(name: str) -> np.ndarray:
                arr = np.asarray(one[name].transpose("chain", "draw", TIME_DIM).values)
                return arr.reshape(s_len, -1)

            mean, var = flat("mean"), flat("var")
            if predictive:
                z = crn_normal(ctx.seed, "y_hat", mean.shape)
                vals = mean + np.sqrt(var) * z
            else:
                vals = mean
            y_hat[:, inside] = vals[:, pos] * sc.y_scale + sc.y_loc
            level[:, inside] = flat("level")[:, pos] * sc.y_scale + sc.y_loc
        if (~inside).any():
            steps = after[~inside]
            h = int(steps.max())
            X_future = _future_regressors(ds, s.regressors, labels[~inside], steps, h)
            expected, draws = forecast(term, X_future, h, seed=ctx.seed, name="y_hat")
            y_hat[:, ~inside] = (draws if predictive else expected)[:, steps - 1]
            level[:, ~inside] = _level_ahead(fitted.terminal_state, term, h)[:, steps - 1]
        return y_hat, level, "crn_seeded" if predictive else "none"

    def _filter_new(
        self,
        fitted: Fitted,
        ds: xr.Dataset,
        s: BSTSSettings,
        term: Terminal,
        labels: np.ndarray,
        ctx: ScoreContext,
    ) -> tuple[np.ndarray, np.ndarray, xr.Dataset]:
        fit_t = np.asarray(fitted.groups[ONE_STEP][TIME_DIM].values)
        after = _steps_after(labels, term.last_label, fit_t, fitted.learned.get("freq"))
        if labels.size == 0 or (after < 1).any():
            raise MapiError(
                f"filter mode continues the fitted series: every period must come after the "
                f"last fitted period {term.last_label}",
                code=ErrorCode.COORD_NOT_ALLOWED,
                pointer="/data",
            )
        order = np.argsort(after, kind="stable")
        if not np.array_equal(after[order], np.arange(1, labels.size + 1)):
            raise MapiError(
                "filter mode needs the periods right after the fit, without gaps",
                code=ErrorCode.NON_CONTIGUOUS_EXTENSION,
                pointer="/data",
            )
        if TARGET not in ds:
            raise MapiError(
                f"filter mode needs observations of {TARGET!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        sc = term.scaling
        y = (np.asarray(ds[TARGET].values, dtype=float)[order] - sc.y_loc) / sc.y_scale
        X = _future_regressors(ds, s.regressors, labels[order], after[order], labels.size)
        d = None if X is None else term.beta @ scale_regressors(X, sc).T
        fr = kalman_filter(term.system, y, d, start=(term.mean, term.cov), keep=True)
        assert fr.pred_mean is not None
        if ctx.kind == "predictive":
            vals = one_step_draws(fr, d, seed=ctx.seed, name="filter:y_hat")
        else:
            vals = fr.mean if d is None else fr.mean + d
        y_hat = np.empty_like(vals)
        y_hat[:, order] = vals * sc.y_scale + sc.y_loc
        comps = _terminal_components(fitted.terminal_state)
        lvl = np.einsum("snk,k->sn", fr.pred_mean, comps.get("level", term.system.Z[0]))
        level = np.empty_like(lvl)
        level[:, order] = lvl * sc.y_scale + sc.y_loc
        assert fitted.terminal_state is not None
        old = fitted.terminal_state.attrs
        regs = (
            [str(v) for v in fitted.terminal_state[REGRESSOR].values]
            if REGRESSOR in fitted.terminal_state.coords
            else []
        )
        updated = _terminal(
            term.system,
            term.beta,
            fr.last_mean,
            fr.last_cov,
            chain=term.chain,
            draw=term.draw,
            state_names=[str(v) for v in fitted.terminal_state[STATE].values],
            regressors=regs,
            scaling=sc,
            last_label=label_text(labels[order][-1]),
            n_fit=term.n_fit + labels.size,
            components=comps or None,
            extra={
                "freq": old.get("freq", ""),
                "previous_label": term.last_label,
                "loglik": float(fr.loglik.mean()),
            },
        )
        return y_hat, level, updated


# ============================================================================== helpers


def one_step_group(fit: BSTSFit, times: np.ndarray, dim: str) -> xr.Dataset:
    """I-4 ``one_step``: in-sample one-step-ahead ``mean`` (with ``d_t``) and ``var``,
    plus the one-step ``level`` (standardised scale, on the score grid)."""
    shape = (fit.chain.size, fit.draw.size)
    d = fit.d
    mean = fit.filt.mean if d is None else fit.filt.mean + d
    level_load = fit.components.get("level", fit.system.Z[0] if fit.system.S else None)
    assert fit.filt.pred_mean is not None and level_load is not None
    level = np.einsum("snk,k->sn", fit.filt.pred_mean, level_load)
    dims = ("chain", "draw", dim)
    return xr.Dataset(
        {
            "mean": (dims, mean.reshape(*shape, -1)),
            "var": (dims, fit.filt.var.reshape(*shape, -1)),
            "level": (dims, level.reshape(*shape, -1)),
        },
        coords={"chain": fit.chain, "draw": fit.draw, dim: np.asarray(times)},
        attrs={"scale": "standardised"},
    )


def _terminal_components(ds: xr.Dataset | None) -> dict[str, np.ndarray]:
    if ds is None or "components" not in ds.attrs:
        return {}
    raw = json.loads(str(ds.attrs["components"]))
    return {str(k): np.asarray(v, dtype=float) for k, v in raw.items()}


def _level_ahead(terminal: xr.Dataset | None, term: Terminal, h: int) -> np.ndarray:
    """Expected level ``Z_level T^j a_{n|n}`` for ``j = 1..h`` (model-stage units)."""
    load = _terminal_components(terminal).get("level", term.system.Z[0])
    m = term.mean
    out = np.empty((term.system.S, h))
    for j in range(h):
        m = np.einsum("sij,sj->si", term.system.T, m)
        out[:, j] = m @ load
    return out * term.scaling.y_scale + term.scaling.y_loc


def _fit_arrays(
    ds: xr.Dataset, regressors: Sequence[str]
) -> tuple[np.ndarray, np.ndarray | None, np.ndarray]:
    if TIME_DIM not in ds.dims:
        raise MapiError(f"the data has no dim {TIME_DIM!r}", code=ErrorCode.SCHEMA_INVALID)
    if TARGET not in ds:
        raise MapiError(f"the data has no variable {TARGET!r}", code=ErrorCode.SCHEMA_INVALID)
    ds = ds.sortby(TIME_DIM)
    times = np.asarray(ds[TIME_DIM].values)
    y = np.asarray(ds[TARGET].transpose(TIME_DIM).values, dtype=float)
    if not regressors:
        return y, None, times
    cols = []
    for r in regressors:
        if r not in ds:
            raise MapiError(
                f"the data has no driver {r!r}", code=ErrorCode.SCHEMA_INVALID, pointer="/data"
            )
        col = np.asarray(ds[r].transpose(TIME_DIM).values, dtype=float)
        if not np.isfinite(col).all():
            raise MapiError(
                f"the driver {r!r} has missing values in the fitted periods; fill them",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        cols.append(col)
    return y, np.column_stack(cols), times


def check_regular(t: np.ndarray) -> str | None:
    """The Kalman filter indexes periods by position: they must be evenly spaced.

    Returns the pandas frequency of datetime periods (None for integer periods).
    """
    if t.size < 3 or t.dtype.kind not in "iuM":
        return None
    if t.dtype.kind == "M":
        freq = pd.infer_freq(pd.DatetimeIndex(t))
        if freq is not None:
            return str(freq)
    elif np.unique(np.diff(t)).size == 1:
        return None
    raise MapiError(
        "the time index is not evenly spaced (periods are missing or repeated); resample it "
        "with core.resample or add the missing periods with empty targets (the model "
        "accepts missing values)",
        code=ErrorCode.SCHEMA_INVALID,
        pointer="/data",
    )


def _steps_after(labels: np.ndarray, last_label: str, fit_t: np.ndarray, freq: Any) -> np.ndarray:
    """Periods after the terminal state's label (``0`` at it, negative before; labels
    off the period grid get ``0``)."""
    out = np.zeros(labels.size, dtype=np.int64)
    if labels.size == 0:
        return out
    if labels.dtype.kind == "M":
        last = pd.Timestamp(last_label)
        lab = pd.DatetimeIndex(labels)
        later = lab > last
        if later.any():
            if not freq:
                raise MapiError(
                    "the fitted model has no period frequency; refit the model",
                    code=ErrorCode.INTEGRITY_FAILURE,
                )
            grid = pd.date_range(last, end=lab[later].max(), freq=str(freq))
            pos = grid.get_indexer(lab[later])
            out[np.flatnonzero(later)] = np.where(pos > 0, pos, 0)
        out[~later] = -1
        return out
    step = int(np.diff(fit_t).min()) if fit_t.size > 1 else 1
    rel = labels.astype(np.int64) - int(last_label)
    return np.where(rel % step == 0, rel // step, 0).astype(np.int64)


def _future_regressors(
    ds: xr.Dataset,
    names: Sequence[str],
    labels: np.ndarray,
    steps: np.ndarray,
    h: int,
) -> np.ndarray | None:
    """``(h, p)`` drivers of the periods ``1..h`` after the terminal state (unscaled)."""
    if not names:
        return None
    X = np.full((h, len(names)), np.nan)
    for j, n in enumerate(names):
        if n not in ds:
            raise MapiError(
                f"the forecast needs future values of the driver {n!r}",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer="/data",
            )
        col = np.asarray(ds[n].sel({TIME_DIM: labels}).values, dtype=float).reshape(-1)
        X[steps - 1, j] = col
    if not np.isfinite(X).all():
        raise MapiError(
            "the drivers need values for every period of the forecast horizon",
            code=ErrorCode.MISSING_FUTURE_COVARIATE,
            pointer="/data",
        )
    return X


def _to_original(ds: xr.Dataset, ctx: FitContext) -> xr.Dataset:
    """Components in target units: cumulative sums mapped back, then differenced (so the
    parts still add up when the pipeline's inverse is not linear)."""
    if ctx.to_original is None:
        return ds
    names = list(ds.data_vars)
    out: dict[str, xr.Variable] = {}
    running: xr.DataArray | None = None
    prev: xr.DataArray | None = None
    for name in names:
        running = ds[name] if running is None else running + ds[name]
        mapped = ctx.to_original(running, TARGET)
        part = mapped if prev is None else mapped - prev
        out[str(name)] = part.variable.copy()
        out[str(name)].attrs = {}
        prev = mapped
    return xr.Dataset(out, coords=ds.coords)


def track_bsts(
    ctx: FitContext, fit: BSTSFit, s: StructureSettings, y: np.ndarray, times: np.ndarray
) -> None:
    """Log the I-10 ``bsts.*`` params and the fit metrics (no-op without a tracker)."""
    if not ctx.tracking:
        return
    regressors = fit.learned.get("regressors") or []
    ctx.log_params(
        {
            "bsts.trend": s.trend,
            "bsts.seasonality": ",".join(season.label for season in s.seasonality),
            "bsts.regressors": ",".join(str(r) for r in regressors),  # type: ignore[union-attr]
            "bsts.n_states": len(fit.state_names),
            "bsts.n_periods": int(times.size),
            "bsts.missing_periods": int((~np.isfinite(y)).sum()),
        }
    )
    ctx.log_metrics(fit_metrics(fit, y))


def fit_metrics(fit: BSTSFit, y: np.ndarray) -> dict[str, float]:
    """``sigma_obs``, ``sigma_level`` (posterior means, target units), ``one_step_rmse`` and
    ``one_step_coverage_90`` (in-sample one-step-ahead predictions vs the observed ``y``)."""
    sc = fit.scaling
    out: dict[str, float] = {}
    post = fit.posterior
    if f"sigma_{_OBS}" in post:
        out["sigma_obs"] = float(post[f"sigma_{_OBS}"].mean()) * sc.y_scale
    sig = post.get(f"sigma_{_TREND}")
    shock = f"shock_{_TREND}"
    if sig is not None and shock in sig.dims and "level" in sig[shock].values.astype(str):
        out["sigma_level"] = float(sig.sel({shock: "level"}).mean()) * sc.y_scale
    y = np.asarray(y, dtype=float).reshape(-1)
    obs = np.isfinite(y)
    if obs.any():
        d = fit.d
        mean = (fit.filt.mean if d is None else fit.filt.mean + d) * sc.y_scale + sc.y_loc
        sd = np.sqrt(fit.filt.var) * sc.y_scale
        point = mean.mean(axis=0)
        out["one_step_rmse"] = float(np.sqrt(np.mean((point[obs] - y[obs]) ** 2)))
        draws = mean + sd * crn_normal(0, "one_step_coverage", mean.shape)
        lo, hi = np.quantile(draws, [0.05, 0.95], axis=0)
        out["one_step_coverage_90"] = float(np.mean((y[obs] >= lo[obs]) & (y[obs] <= hi[obs])))
    return {k: v for k, v in out.items() if math.isfinite(v)}


def _clean(ds: xr.Dataset, engine: str) -> xr.Dataset:
    out = ds.copy()
    for v in out.variables:
        out[v].attrs = {}
    out.attrs = {
        "inference_library": "pymc",
        "inference_library_version": pm.__version__,
        "engine": engine,
    }
    return out


@contextmanager
def _quiet() -> Iterator[None]:
    logger = logging.getLogger("pymc")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logger.setLevel(level)
