"""``mmm.nested``: mmm-framework's ``NestedMMM`` (media effects through mediators) behind
the platform contract.

The engine (``mmm_framework.mmm_extensions.models.nested.NestedMMM``) is national (one
series, no geo or product dims) and takes no control variables. On the standardised KPI::

    m_latent[m, t] = alpha_m + sum_c beta[c, m] * sat_c[t]          # one per mediator
    mu[t] = alpha_y + sum_m gamma_m * m_latent[m, t]
            + sum_c delta_c * sat_c[t] + trend[t] + seasonality[t]
    y = mu * y_std + y_mean

``sat_c`` is the extension models' fixed media transform (:mod:`mapi_plugin_mmm.extended`).
A measured mediator is observed with noise where its series has values (a monthly survey
may leave most weeks empty); a latent one is not observed (``latent: true``), so only the
products ``gamma_m * beta[c, m]`` are identified - which is what the KPI-unit paths report.
Mediator series are standardised per label before the fit (mean and sd in ``learned``).

So a channel's total effect is ``(sum_m gamma_m * beta[c, m] + delta_c) * sat_c * y_std``
(the engine's ``channel_contributions``): its paths through the mediators plus its direct
path. :class:`NestedReplay` evaluates every term in NumPy from the posterior draws at any
period position, inside the fit window or after it (a linear trend is extrapolated, the
other trend kinds are held at their last fitted level), and implements
:class:`~mapi_plugin_mmm.forward.ReplayLike`, so the shared derived quantities, budget
optimisation and run tracking work unchanged.

Outputs: ``y_hat(chain, draw, time)``, ``contributions(chain, draw, component, time)``
(every channel's total effect, ``baseline`` - the intercept and ``sum_m gamma_m *
alpha_m`` - ``trend`` and ``seasonality``; they sum to ``y_hat``) and
``mediator_hat(chain, draw, mediator, time)`` (mediator units; latent mediators in
standard units). Derived: ``contributions``, ``response_curves``, ``marginal_cost``,
``adstock`` and ``mediation`` (direct and indirect KPI totals per channel and mediator).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final, Literal, cast

import numpy as np
import xarray as xr
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_sdk.models import (
    PREDICTIONS,
    BudgetPlan,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ScoreContext,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.optimize import OptimizeRequest
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, field_validator, model_validator

from mapi_plugin_mmm.base import FrameworkMMM, diagnostics_from, draw_samples, positions_of
from mapi_plugin_mmm.calibration import lift_measurements, lift_test_group, unit_scales
from mapi_plugin_mmm.derived import (
    SAMPLE,
    FitWindow,
    adstock_dataset,
    contributions_dataset,
    marginal_cost_dataset,
    response_curves_dataset,
)
from mapi_plugin_mmm.extended import (
    ExtSettings,
    SeasonTerm,
    ext_configs,
    ext_fit_groups,
    ext_kernels,
    fit_extended,
    fourier_at,
    national_panel,
    posterior_of,
    saturated_media,
    season_terms,
    trend_values,
)
from mapi_plugin_mmm.forward import TREND_BY_PERIOD, CellRef, Components, Posterior
from mapi_plugin_mmm.framework import (
    APPROX_ENGINES,
    BASELINE,
    CHANNEL,
    COMPONENT,
    MCMC_ENGINES,
    MEDIATOR,
    SEASONALITY,
    SPEND,
    TARGET,
    TIME,
    TREND,
    Controls,
    PanelData,
    engine_plan,
    framework_version,
    infer_frequency,
    period_index,
    quiet,
)
from mapi_plugin_mmm.planning import optimize_budget_plan, thin_index
from mapi_plugin_mmm.settings import _Sub, mcmc_fit_schema
from mapi_plugin_mmm.tracking import log_mmm_summary, summary_window

__all__ = [
    "MEDIATORS",
    "MMMNested",
    "MediatorSettings",
    "NestedReplay",
    "NestedSettings",
]

MEDIATORS: Final = "mediators"
"""The mediator series ``mediators(mediator, time)`` (a variable may not share its dim's
name, so the variable is plural)."""
_FIT_DATA: Final = "fit_data"


# ======================================================================== settings


class MediatorSettings(_Sub):
    """One mediator: a measured (or latent) result that channels move and that moves the
    KPI (mmm-framework ``MediatorConfig``)."""

    name: str = Field(..., min_length=1, max_length=128, description="A mediator label")
    channels: list[str] | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="Channels that move this mediator (default: every channel)",
    )
    direct_effect: bool = Field(
        True, description="The channels of this mediator also act on the KPI directly"
    )
    latent: bool = Field(
        False,
        description="The mediator is not measured: its series is ignored (it can be empty)",
    )
    media_sign: Literal["positive", "none"] = Field(
        "positive", description="Sign of the channel effects on the mediator"
    )
    outcome_sign: Literal["none", "positive", "negative"] = Field(
        "none", description="Sign of the mediator's effect on the KPI"
    )
    observation_sigma: float = Field(
        0.1,
        gt=0,
        le=5,
        description="Prior scale of the measurement noise, in mediator standard deviations",
    )

    @field_validator("channels")
    @classmethod
    def _unique(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(set(v)) != len(v):
            raise ValueError("channels lists each channel once")
        return v


class NestedSettings(ExtSettings):
    """Settings of ``mmm.nested`` (mmm-framework ``NestedMMM``).

    Canonical names: dims ``time``, ``channel`` and ``mediator``; target ``y(time)``;
    spend ``spend(channel, time)``; mediator series ``mediators(mediator, time)`` (missing
    values allowed). One national series, no control variables.
    """

    mediators: list[MediatorSettings] = Field(
        default_factory=list,
        max_length=8,
        description="The mediators (default: every label of the mediator dim, measured, "
        "moved by every channel, with direct effects)",
    )

    @model_validator(mode="after")
    def _check(self) -> NestedSettings:
        names = [m.name for m in self.mediators]
        if len(set(names)) != len(names):
            raise ValueError("mediators lists each mediator once")
        return self


# ======================================================================== replay


class NestedReplay:
    """The forward pass of a fitted ``NestedMMM`` from the bundle's ``learned`` record and
    posterior draws (see the module doc); implements
    :class:`~mapi_plugin_mmm.forward.ReplayLike` on one national cell."""

    def __init__(self, learned: Mapping[str, Any], post: Posterior) -> None:
        self.post = post
        self.n = post.n
        self.channels = [str(c) for c in learned["channels"]]
        self.frequency = str(learned.get("frequency", ""))
        scaling = learned["scaling"]
        self.y_mean = float(scaling["y_mean"])
        self.y_std = float(scaling["y_std"])
        self.scale = np.array([float(scaling["media_scale"][c]) for c in self.channels])
        meds = list(learned["mediators"])
        self.mediators = [str(m["name"]) for m in meds]
        self.med_mean = np.array(
            [float(scaling["mediator_mean"].get(m, 0.0)) for m in self.mediators]
        )
        self.med_sd = np.array([float(scaling["mediator_sd"].get(m, 1.0)) for m in self.mediators])
        n_c, n_m = len(self.channels), len(self.mediators)
        self.kernels: list[np.ndarray] = [
            ext_kernels(post.need(f"alpha_{ch}")) for ch in self.channels
        ]
        self.lam: list[np.ndarray] = [post.need(f"lambda_{ch}") for ch in self.channels]
        # media -> mediator (S, C, M), mediator -> KPI (S, M), direct (S, C)
        self.beta = np.zeros((self.n, n_c, n_m))
        self.gamma = np.zeros((self.n, n_m))
        self.alpha_m = np.zeros((self.n, n_m))
        direct = np.zeros(n_c, dtype=bool)
        for k, m in enumerate(meds):
            name = self.mediators[k]
            affecting = [str(c) for c in m["channels"]]
            for ch in affecting:
                c = self.channels.index(ch)
                rv = f"beta_media_to_{name}" if len(affecting) == 1 else f"beta_{ch}_to_{name}"
                self.beta[:, c, k] = post.need(rv)
                direct[c] |= bool(m["direct_effect"])
            gamma = post.need(f"gamma_{name}")
            # a negative sign is -HalfNormal: the posterior holds the HalfNormal
            self.gamma[:, k] = -gamma if m["outcome_sign"] == "negative" else gamma
            self.alpha_m[:, k] = post.need(f"alpha_{name}")
        self.delta = np.zeros((self.n, n_c))
        for c, ch in enumerate(self.channels):
            if direct[c]:
                self.delta[:, c] = post.need(f"delta_direct_{ch}")
        self.alpha_y = post.need("alpha_y")
        self.sigma = post.need("sigma_y")
        self.nu = post.get("nu_y")
        trend = learned["trend"]
        self.trend_kind = str(trend["kind"])
        self.n_periods = int(trend["n_periods"])
        self.terms = [
            SeasonTerm(str(t["name"]), float(t["period"]), int(t["order"]))
            for t in learned["season_terms"]
        ]
        self.season_coefs = post.get("seasonality_coefs") if self.terms else None

    # ------------------------------------------------------------------ terms
    @property
    def coef(self) -> np.ndarray:
        """``(S, C)`` total effect per unit of saturated media (standardised units)."""
        return np.einsum("sm,scm->sc", self.gamma, self.beta) + self.delta

    def saturated(self, spend: np.ndarray, positions: np.ndarray) -> np.ndarray:
        """``(S, C, n)`` saturated media of raw ``(T_full, C)`` spend at ``positions``."""
        return saturated_media(spend, positions, self.kernels, self.lam, self.scale)

    def channel_at(
        self,
        spend: np.ndarray,
        positions: np.ndarray,
        cells: Sequence[CellRef],
        *,
        strategy: Mapping[str, str] | None = None,
        seed: int = 0,
    ) -> np.ndarray:
        """``(S, C, n_cell, n)`` total channel effects (standardised units) of raw
        ``(T_full, n_cell, C)`` spend."""
        del strategy, seed  # one fitted national cell: no new levels
        pos = np.asarray(positions, dtype=int)
        out = np.empty((self.n, len(self.channels), spend.shape[1], pos.size))
        coef = self.coef[:, :, None]
        for j in range(spend.shape[1]):
            out[:, :, j] = coef * self.saturated(spend[:, j], pos)
        return out

    def mediated_at(
        self, spend: np.ndarray, positions: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """``(direct (S, C, n), via (S, C, M, n))`` in KPI units of raw ``(T_full, C)``
        spend: each channel's direct path and its path through each mediator."""
        sat = self.saturated(spend, positions) * self.y_std  # (S, C, n)
        direct = self.delta[:, :, None] * sat
        via = (self.beta * self.gamma[:, None, :])[:, :, :, None] * sat[:, :, None, :]
        return direct, via

    def mediator_at(self, spend: np.ndarray, positions: np.ndarray) -> np.ndarray:
        """``(S, M, n)`` expected mediators of raw ``(T_full, C)`` spend, in mediator units
        (latent mediators in standard units)."""
        sat = self.saturated(spend, positions)
        z = self.alpha_m[:, :, None] + np.einsum("scm,scn->smn", self.beta, sat)
        return np.asarray(z * self.med_sd[None, :, None] + self.med_mean[None, :, None])

    def trend_at(self, positions: np.ndarray) -> np.ndarray:
        """``(S, n)`` trend (standardised units)."""
        return trend_values(
            self.post,
            kind=self.trend_kind,
            slope_name="trend_slope",
            by_period=self.post.get(TREND_BY_PERIOD),
            n_periods=self.n_periods,
            positions=positions,
        )

    def seasonality_at(self, positions: np.ndarray) -> np.ndarray:
        """``(S, n)`` Fourier seasonality (standardised units)."""
        pos = np.asarray(positions)
        if self.season_coefs is None:
            return np.zeros((self.n, pos.size))
        design = fourier_at(pos, self.terms)  # (n, F)
        return np.asarray(self.season_coefs.reshape(self.n, -1) @ design.T)

    def evaluate(
        self,
        spend: np.ndarray,
        ctrl: np.ndarray,
        positions: np.ndarray,
        cells: Sequence[CellRef],
        *,
        strategy: Mapping[str, str] | None = None,
        seed: int = 0,
    ) -> Components:
        """Every term at ``positions`` in KPI units (``spend`` raw ``(T_full, n_cell, C)``;
        the model takes no controls, so ``ctrl`` is ignored)."""
        del ctrl
        pos = np.asarray(positions, dtype=int)
        n_cell = spend.shape[1]
        notes: list[str] = []
        if (
            pos.size
            and int(pos.max()) >= self.n_periods
            and self.trend_kind
            not in (
                "none",
                "linear",
            )
        ):
            notes.append(
                f"the {self.trend_kind} trend is held at its last fitted level beyond the fit "
                "window, so the interval does not widen with the horizon"
            )
        level = self.alpha_y + (self.gamma * self.alpha_m).sum(axis=1)  # (S,)
        baseline = np.broadcast_to(
            (level * self.y_std + self.y_mean)[:, None, None], (self.n, n_cell, pos.size)
        ).copy()
        nu = None if self.nu is None else self.nu.reshape(-1, 1, 1)
        return Components(
            channel=self.channel_at(spend, pos, cells, strategy=strategy, seed=seed) * self.y_std,
            baseline=baseline,
            trend=self.trend_at(pos) * self.y_std,
            seasonality=self.seasonality_at(pos) * self.y_std,
            controls=np.zeros((self.n, 0, n_cell, pos.size)),
            sigma=self.sigma,
            # Student-t degrees of freedom per draw, broadcast over (cell, time)
            nu=cast("float | None", nu),
            y_std=self.y_std,
            notes=notes,
        )

    def channel_totals(
        self,
        spend: np.ndarray,
        ctrl: np.ndarray,
        positions: np.ndarray,
        cells: Sequence[CellRef],
        multiplier: float,
    ) -> np.ndarray:
        """``(S, C, n_cell)`` KPI total over ``positions`` of each channel at ``multiplier``
        times its spend (the channels add up, so scaling one at a time is scaling all)."""
        del ctrl
        ch = self.channel_at(spend * multiplier, positions, cells)
        return np.asarray(ch.sum(axis=3) * self.y_std)

    def steady_state(
        self, spend: np.ndarray, cells: Sequence[CellRef], *, position: int | None = None
    ) -> np.ndarray:
        """``(S, n_cell, C)`` KPI per period of a constant raw spend ``(n_cell, C)`` held
        long enough for the carry-over to settle (the kernels sum to one)."""
        del cells, position  # constant coefficients
        sp = np.asarray(spend, dtype=float)
        out = np.empty((self.n, sp.shape[0], len(self.channels)))
        coef = self.coef
        for c in range(len(self.channels)):
            level = sp[None, :, c] / self.scale[c] * self.kernels[c].sum(axis=1)[:, None]
            sat = 1.0 - np.exp(-self.lam[c][:, None] * level)
            out[:, :, c] = coef[:, c, None] * sat * self.y_std
        return out

    def fit_cells(self) -> list[CellRef]:
        return [CellRef(0, 0, "0/0")]


# ======================================================================== the plugin

SCHEMA_TEMPLATE: Final[dict[str, Any]] = {
    "template": {
        "dims": {
            TIME: {"kind": "time", "policy": "extend"},
            CHANNEL: {"kind": "categorical", "policy": "fixed"},
            MEDIATOR: {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            TARGET: {"dims": [TIME], "dtype": "float64", "role": "target"},
            SPEND: {
                "dims": [CHANNEL, TIME],
                "dtype": "float64",
                "role": "spend",
                "future_required": True,
            },
            MEDIATORS: {
                "dims": [MEDIATOR, TIME],
                "dtype": "float64",
                "role": "covariate",
                "missing": "allow",
            },
        },
    }
}
"""Canonical names: ``y(time)``, ``spend(channel, time)``, ``mediators(mediator, time)``."""

OUTPUTS: Final[dict[str, Any]] = {
    "y_hat": {
        "dims": [TIME],
        "dims_of": TARGET,
        "units_of": TARGET,
        "description": "Expected (or predictive) KPI over time",
    },
    "contributions": {
        "dims": [COMPONENT, TIME],
        "dims_of": TARGET,
        "units_of": TARGET,
        "description": "Every channel's total effect (through the mediators and direct), the "
        "baseline, trend and seasonality; they sum to y_hat",
    },
    "mediator_hat": {
        "dims": [MEDIATOR, TIME],
        "units_of": MEDIATORS,
        "description": "Expected mediators (latent mediators in standard units)",
    },
}
DERIVED: Final[list[dict[str, Any]]] = [
    {
        "name": "contributions",
        "dims": [SAMPLE, CHANNEL, TIME],
        "units": "target",
        "cache": "fit",
        "description": "Channel, baseline, trend and seasonality contributions over the fit "
        "window per thinned draw",
    },
    {
        "name": "response_curves",
        "dims": [SAMPLE, CHANNEL, "multiplier"],
        "units": "target",
        "cache": "fit",
        "description": "Total contribution per spend multiplier and channel per thinned draw "
        "(Guided-mode slider grids, budget optimisation)",
    },
    {
        "name": "marginal_cost",
        "dims": [SAMPLE, CHANNEL],
        "units": "spend per target",
        "cache": "fit",
        "description": "Extra spend per extra unit of the KPI at current spend",
    },
    {
        "name": "adstock",
        "dims": [SAMPLE, CHANNEL, "lag"],
        "cache": "fit",
        "description": "Share of a unit of spend's effect that lands each period later",
    },
    {
        "name": "mediation",
        "dims": [SAMPLE, CHANNEL, MEDIATOR],
        "units": "target",
        "cache": "fit",
        "description": "Direct and indirect (through each mediator) effect of each channel "
        "over the fit window",
    },
]


class MMMNested(CustomModel[NestedSettings]):
    """mmm-framework's nested (mediated) marketing-mix model."""

    id = "mmm.nested"
    version = "1.0.0"
    Settings = NestedSettings
    family = "mmm"
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "counterfactual", "evaluate"}),
        decision=True,
        contribution_scale="additive",
        identified_quantities=["y_hat", "contributions"],
        owns=frozenset({"scaling", "carryover", "inverse_units"}),
        deterministic_given_seed=False,
        checkpointable=False,
        calibration=frozenset({"lift_tests"}),
    )
    schema_template = SCHEMA_TEMPLATE
    outputs: ClassVar[dict[str, Any]] = OUTPUTS
    derived: ClassVar[list[dict[str, Any]]] = DERIVED
    plots: ClassVar[list[str]] = [
        "core.ppc_fan",
        "core.convergence",
        "core.energy",
        "core.prior_posterior",
        "mmm.decomposition",
        "mmm.waterfall",
        "mmm.response_curves",
        "mmm.roi",
        "mmm.spend_effect",
        "mmm.adstock",
        "mmm.mediation",
        "mmm.lift_tests",
    ]
    resources = ResourceHint(
        min_class=ResourceClass.S, default_class=ResourceClass.M, est_fit_seconds=900
    )
    fit_schema: ClassVar[dict[str, Any]] = mcmc_fit_schema(
        engines=[*MCMC_ENGINES, *APPROX_ENGINES], default_engine="nutpie"
    )
    description = (
        "mmm-framework's nested marketing-mix model: channels move mediators (visits, "
        "awareness, measured in some periods or latent) that move the KPI, plus direct "
        "channel effects; one national series, trend and seasonality, lift tests as "
        "calibration, budget optimisation."
    )
    pad_extend = False
    structural_missing: ClassVar[Sequence[str]] = (MEDIATORS,)
    default_engine: ClassVar[str] = "nutpie"
    _cache: tuple[Any, Any, NestedReplay, PanelData] | None = None

    # ------------------------------------------------------------------ helpers
    def _settings(self, fitted: Fitted) -> NestedSettings:
        return self.parse_settings(fitted.settings)

    @staticmethod
    def _frequency(fitted: Fitted) -> str:
        freq = fitted.learned.get("frequency")
        if freq not in ("W", "D", "M"):
            raise MapiError(
                "the bundle does not record the data frequency", code=ErrorCode.INTEGRITY_FAILURE
            )
        return str(freq)

    @staticmethod
    def _fit_panel(fitted: Fitted) -> PanelData:
        fd = fitted.group(_FIT_DATA)
        return national_panel(
            np.asarray(fd[TIME].values),
            [str(c) for c in fd[CHANNEL].values],
            np.asarray(fd[SPEND].transpose(TIME, CHANNEL).values, dtype=float),
            np.asarray(fd[TARGET].values, dtype=float) if TARGET in fd else None,
        )

    def replay(
        self, fitted: Fitted, posterior: xr.Dataset | None = None
    ) -> tuple[NestedReplay, PanelData]:
        """The fitted model's forward pass over ``posterior`` (default: all draws)."""
        post = fitted.posterior if posterior is None else posterior
        cached = self._cache
        if cached is not None and cached[0] is fitted and cached[1] is post:
            return cached[2], cached[3]
        rp = NestedReplay(fitted.learned, Posterior.from_dataset(post))
        fit = self._fit_panel(fitted)
        self._cache = (fitted, post, rp, fit)
        return rp, fit

    # ------------------------------------------------------------------ fit
    def fit(self, ds: xr.Dataset, s: NestedSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        data = PanelData.from_dataset(ds, (), [], with_target=True)
        freq = infer_frequency(data.times, s.frequency)
        spend = data.spend[:, 0, 0, :]  # (T, C)
        y = np.asarray(data.y, dtype=float)[:, 0, 0]
        meds, series = _mediators(ds, s, data.channels)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        plan = engine_plan(
            cfg.engine,
            draws=cfg.draws,
            tune=cfg.tune,
            chains=cfg.chains,
            target_accept=cfg.target_accept,
            seed=seed,
            default_engine=self.default_engine,
        )
        # standardise each measured mediator (the engine's priors assume unit scale)
        mean: dict[str, float] = {}
        sd: dict[str, float] = {}
        z: dict[str, np.ndarray] = {}
        for m in meds:
            if m.latent:
                continue
            x = series[m.name]
            mu = float(np.nanmean(x))
            sigma = float(np.nanstd(x))
            sigma = sigma if np.isfinite(sigma) and sigma > 0 else 1.0
            mean[m.name], sd[m.name] = mu, sigma
            z[m.name] = (x - mu) / sigma
        ctx.progress(0.05, "building the mmm-framework model")
        model = _make_model(spend, y, data, meds, z, s, freq)
        lift = self._lift_tests(ds, data, ctx, model)
        ctx.check_cancelled()
        ctx.progress(0.1, f"sampling ({plan.engine})")
        result = fit_extended(model, plan, ctx)
        ctx.progress(0.9, "posterior ready")
        post = posterior_of(model)
        extra: dict[str, xr.DataArray] = {}
        if s.trend.kind not in ("none", "linear") and "trend_component" in post:
            tc = post["trend_component"].transpose("chain", "draw", ...) / float(model.y_std)
            extra[TREND_BY_PERIOD] = xr.DataArray(
                np.asarray(tc.values, dtype=float),
                dims=["chain", "draw", "trend_period"],
                coords={"chain": post["chain"].values, "draw": post["draw"].values},
            )
        names = [m.name for m in meds]
        raw = np.stack([series[n] for n in names], axis=1) if names else np.zeros((y.size, 0))
        fit_data = xr.Dataset(
            {
                TARGET: ([TIME], y),
                SPEND: ([TIME, CHANNEL], spend),
                MEDIATORS: ([TIME, MEDIATOR], raw),
            },
            coords={
                TIME: data.times,
                CHANNEL: np.asarray(data.channels, dtype=str),
                MEDIATOR: np.asarray(names, dtype=str),
            },
        )
        observed = xr.Dataset({TARGET: ([TIME], y)}, coords={TIME: data.times})
        groups = ext_fit_groups(
            model, post, fit_data=fit_data, observed=observed, extra=extra, cfg=cfg, ll_var="y_obs"
        )
        calibration: dict[str, Any] = {}
        if lift is not None:
            calibration["lift_tests"] = lift.record()
            lt = lift_test_group(lift, post)
            if lt is not None:
                groups["lift_tests"] = lt
        diagnostics: list[Diagnostic] = list(diagnostics_from(result.diagnostics))
        if plan.mcmc:
            free = [rv.name for rv in model.model.free_RVs if rv.name in groups["posterior"]]
            diagnostics += convergence_diagnostics(
                groups["posterior"], groups.get("sample_stats"), var_names=free
            )
        learned: dict[str, Any] = {
            "mmm_framework": framework_version(),
            "frequency": freq,
            "panel": [],
            "channels": list(data.channels),
            "mediators": [
                {
                    "name": m.name,
                    "channels": list(m.channels or data.channels),
                    "direct_effect": m.direct_effect,
                    "latent": m.latent,
                    "media_sign": m.media_sign,
                    "outcome_sign": m.outcome_sign,
                }
                for m in meds
            ],
            "scaling": {
                "y_mean": float(model.y_mean),
                "y_std": float(model.y_std),
                "media_scale": {
                    c: float(v) for c, v in zip(data.channels, model._media_scale, strict=True)
                },
                "mediator_mean": mean,
                "mediator_sd": sd,
            },
            "season_terms": [
                {"name": t.name, "period": t.period, "order": t.order}
                for t in season_terms(freq, s.seasonality)
            ],
            "trend": {"kind": s.trend.kind, "n_periods": int(y.size)},
            "engine": plan.engine,
            "method": plan.method,
            "draws": plan.draws,
            "tune": plan.tune,
            "chains": plan.chains,
            "fit_diagnostics": FrameworkMMM._diagnostics_json(result.diagnostics),
        }
        if calibration:
            learned["calibration"] = calibration
        self._track(ctx, learned, groups["posterior"], data)
        ctx.progress(1.0, "fit complete")
        return FitOutput(groups=groups, learned=learned, diagnostics=diagnostics)

    def _lift_tests(self, ds: xr.Dataset, data: PanelData, ctx: FitContext, model: Any) -> Any:
        """Lift tests as experiment likelihoods (the plan, or ``None`` without tests)."""
        cal = ctx.calibration
        if cal is None or cal.empty:
            return None
        if cal.business_priors is not None:
            raise MapiError(
                "mmm.nested does not take business priors; use lift tests",
                code=ErrorCode.CALIBRATION_UNSUPPORTED,
                pointer="/calibration/business_priors",
            )
        if not cal.lift_tests:
            return None
        plan = lift_measurements(cal.lift_tests, data, unit_scales(ds, ctx.to_original))
        for tid, why in plan.skipped.items():
            ctx.info(f"lift test {tid} skipped: {why}")
        if plan.measurements:
            model.add_experiment_calibration(plan.measurements)
        return plan

    def _track(
        self, ctx: FitContext, learned: Mapping[str, Any], posterior: xr.Dataset, data: PanelData
    ) -> None:
        """Model-level tracking metrics and params (no-op without a tracker)."""
        if not ctx.tracking:
            return

        def factory(post: xr.Dataset) -> NestedReplay:
            return NestedReplay(learned, Posterior.from_dataset(post))

        # ``data`` is already the one-cell panel of the fit window
        win = summary_window(factory, posterior, data)
        log_mmm_summary(
            ctx,
            win,
            params={
                "mmm.framework_version": str(learned["mmm_framework"]),
                "mmm.n_channels": len(data.channels),
                "mmm.n_periods": int(data.n_periods),
                "mmm.n_cells": 1,
                "mmm.link": "identity",
                "mmm.mediators": ",".join(str(m["name"]) for m in learned["mediators"]),
            },
        )

    # ------------------------------------------------------------------ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        rp, fit = self.replay(fitted)
        freq = self._frequency(fitted)
        order = np.asarray(ds[TIME].values) if TIME in ds.coords else None
        work = ds.sortby(TIME) if order is not None else ds
        data = PanelData.from_dataset(
            work, (), [], with_target=False, fill_missing=True, control_features=_NO_CONTROLS
        )
        if data.channels != fit.channels:
            data = _reorder_channels(data, fit.channels)
        pos = positions_of(fit.times, data.times, freq)
        cells = rp.fit_cells()
        spend_full, ctrl_full = FrameworkMMM._history(fit, data, cells, pos)
        ctx.check_cancelled()
        comps = rp.evaluate(spend_full, ctrl_full, pos, cells, seed=ctx.seed)
        for note in comps.notes:
            ctx.info(note)
        kind, noise = "expected", "none"
        mu = comps.mu
        if ctx.kind == "predictive":
            mu = comps.predictive(ctx.seed)
            kind, noise = "predictive", "crn_seeded"
        n_t = pos.size
        parts = [comps.channel[:, :, 0]]  # (S, C, n)
        for arr in (comps.baseline[:, 0], comps.trend, comps.seasonality):
            parts.append(np.asarray(arr).reshape(rp.n, 1, n_t))
        contrib = np.concatenate(parts, axis=1)
        labels = [*fit.channels, BASELINE, TREND, SEASONALITY]
        post = fitted.posterior
        y_hat = _labelled(mu[:, 0], post, data.times, [], {})
        y_hat.attrs = {"kind": kind, "noise": noise}
        contributions = _labelled(
            contrib, post, data.times, [COMPONENT], {COMPONENT: np.asarray(labels)}
        )
        contributions.attrs = {"kind": "expected", "noise": "none"}
        mediator_hat = _labelled(
            rp.mediator_at(spend_full[:, 0], pos),
            post,
            data.times,
            [MEDIATOR],
            {MEDIATOR: np.asarray(rp.mediators, dtype=str)},
        )
        mediator_hat.attrs = {"kind": "expected", "noise": "none"}
        pred = xr.Dataset(
            {"y_hat": y_hat, "contributions": contributions, "mediator_hat": mediator_hat}
        )
        if order is not None:
            pred = pred.sel({TIME: order})
        pred.attrs = {"units": "original", "owner": "mmm-framework"}
        return xr.DataTree.from_dict({PREDICTIONS: pred})

    # ------------------------------------------------------------------ derived
    def _window(self, fitted: Fitted, draw_index: np.ndarray | None) -> FitWindow:
        post = draw_samples(fitted.posterior, draw_index)
        rp, fit = self.replay(fitted, post)
        return FitWindow(rp, fit)

    def derive(self, fitted: Fitted, name: str, ds: xr.Dataset, ctx: ScoreContext) -> xr.Dataset:
        """Derived quantities over the fit window (see the module doc)."""
        s = self._settings(fitted)
        to_original = getattr(ctx, "to_original", None)
        win = self._window(fitted, ctx.draw_index)
        if name == "contributions":
            return contributions_dataset(win, to_original)
        if name == "response_curves":
            return response_curves_dataset(win, s.grid, to_original)
        if name == "marginal_cost":
            return marginal_cost_dataset(win, s.grid, to_original)
        if name == "adstock":
            return adstock_dataset(win)
        if name == "mediation":
            return mediation_dataset(win, to_original)
        raise MapiError(
            f"model {self.id!r} has no derived quantity {name!r}",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            pointer="/outputs/derived",
        )

    # ------------------------------------------------------------------ decisions
    def optimize(self, fitted: Fitted, request: OptimizeRequest, ctx: ScoreContext) -> BudgetPlan:
        """The budget plan for ``request`` over at most ``request.max_draws`` evenly thinned
        posterior draws (:mod:`mapi_plugin_mmm.planning`)."""
        post = fitted.posterior
        n = int(post.sizes["chain"]) * int(post.sizes["draw"])
        idx = thin_index(n, request.max_draws)
        stacked = post.stack({SAMPLE: ("chain", "draw")}, create_index=False)
        stacked = stacked.drop_vars(["chain", "draw"], errors="ignore").isel({SAMPLE: idx})
        rp, fit = self.replay(fitted, stacked.assign_coords({SAMPLE: np.arange(idx.size)}))
        ctx.check_cancelled()
        return optimize_budget_plan(FitWindow(rp, fit), request, seed=ctx.seed)

    def objective(self, fitted: Fitted, decision: xr.Dataset, ctx: ScoreContext) -> xr.DataArray:
        """Expected total KPI of a per-period budget ``decision["spend"](channel)`` at
        steady state, per posterior draw (``attrs["periods"]``, default 1: how many periods
        the budget runs; channels left out get zero)."""
        if "spend" not in decision:
            raise MapiError(
                "the decision needs a 'spend' variable over channel",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/spend",
            )
        periods = float(decision.attrs.get("periods", 1))
        if not periods > 0:
            raise MapiError(
                "decision periods must be > 0",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/periods",
            )
        rp, fit = self.replay(fitted)
        da = decision["spend"]
        if list(map(str, da.dims)) != [CHANNEL]:
            raise MapiError(
                f"decision spend must be over channel only (the model is national), got "
                f"{list(da.dims)}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/spend",
            )
        unknown = sorted(set(map(str, da[CHANNEL].values)) - set(fit.channels))
        if unknown:
            raise MapiError(
                f"unknown channel(s) {unknown}",
                code=ErrorCode.UNSEEN_LEVELS,
                pointer="/decision/spend",
            )
        values = np.asarray(da.reindex({CHANNEL: fit.channels}, fill_value=0.0).values, float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise MapiError(
                "decision spend must be finite and non-negative",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/spend",
            )
        per_period = rp.steady_state(values[None, :], rp.fit_cells())  # (S, 1, C)
        total = per_period.sum(axis=(1, 2)) * periods
        post = fitted.posterior
        out = xr.DataArray(
            total.reshape(post.sizes["chain"], post.sizes["draw"]),
            dims=["chain", "draw"],
            coords={"chain": post["chain"].values, "draw": post["draw"].values},
            name="objective",
        )
        out.attrs = {
            "units": "target (model-stage units)",
            "periods": periods,
            "description": "expected total KPI of the budget at steady state",
        }
        return out


# ======================================================================== helpers

_NO_CONTROLS: Final = Controls((), (), ())


def _mediators(
    ds: xr.Dataset, s: NestedSettings, channels: list[str]
) -> tuple[list[MediatorSettings], dict[str, np.ndarray]]:
    """The mediators to fit (the settings', or every label with defaults) and their raw
    ``(T,)`` series (NaN where not measured), with the data checks."""
    if MEDIATORS not in ds:
        raise MapiError(
            f"the model needs the mediator series {MEDIATORS!r} over ({MEDIATOR}, {TIME}) "
            "(bind your mediator columns to it)",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{MEDIATORS}",
        )
    da = ds[MEDIATORS]
    if set(map(str, da.dims)) != {MEDIATOR, TIME}:
        raise MapiError(
            f"{MEDIATORS!r} must have dims [{MEDIATOR}, {TIME}], got {list(da.dims)}",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{MEDIATORS}",
        )
    labels = [str(v) for v in da[MEDIATOR].values] if MEDIATOR in da.coords else []
    meds = list(s.mediators) or [MediatorSettings(name=lab) for lab in labels]
    if not meds:
        raise MapiError(
            "the data has no mediator labels", code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{MEDIATORS}",
        )  # fmt: skip
    arr = np.asarray(da.transpose(TIME, MEDIATOR).values, dtype=float)
    series: dict[str, np.ndarray] = {}
    for i, m in enumerate(meds):
        here = f"/model/settings/mediators/{i}"
        if m.name not in labels:
            raise MapiError(
                f"mediator {m.name!r} is not a label of the {MEDIATOR!r} dim (labels: "
                f"{labels[:10]})",
                code=ErrorCode.SETTINGS_INVALID,
                pointer=here + "/name",
            )
        x = arr[:, labels.index(m.name)]
        if not m.latent and not np.isfinite(x).any():
            raise MapiError(
                f"mediator {m.name!r} has no values; set latent: true if it is not measured",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{MEDIATORS}",
            )
        if np.isinf(x).any():
            raise MapiError(
                f"mediator {m.name!r} has infinite values",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{MEDIATORS}",
            )
        unknown = sorted(set(m.channels or ()) - set(channels))
        if unknown:
            raise MapiError(
                f"mediator {m.name!r} names unknown channels {unknown}",
                code=ErrorCode.SETTINGS_INVALID,
                pointer=here + "/channels",
            )
        series[m.name] = x
    reached = {c for m in meds for c in (m.channels or channels)}
    lost = [c for c in channels if c not in reached]
    if lost:
        raise MapiError(
            f"channel(s) {lost} move no mediator, so the model gives them no effect; add them "
            "to a mediator's channels",
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/settings/mediators",
        )
    return meds, series


def _make_model(
    spend: np.ndarray,
    y: np.ndarray,
    data: PanelData,
    meds: Sequence[MediatorSettings],
    z: Mapping[str, np.ndarray],
    s: NestedSettings,
    freq: str,
) -> Any:
    """A (not yet fitted) mmm-framework ``NestedMMM``."""
    from mmm_framework.mmm_extensions.config import (
        EffectConstraint,
        EffectPriorConfig,
        MediatorConfig,
        MediatorType,
        NestedModelConfig,
    )
    from mmm_framework.mmm_extensions.models import NestedMMM

    sign = {
        "none": EffectConstraint.NONE,
        "positive": EffectConstraint.POSITIVE,
        "negative": EffectConstraint.NEGATIVE,
    }
    config = NestedModelConfig(
        mediators=tuple(
            MediatorConfig(
                name=m.name,
                mediator_type=(
                    MediatorType.FULLY_LATENT if m.latent else MediatorType.PARTIALLY_OBSERVED
                ),
                media_effect=EffectPriorConfig(constraint=sign[m.media_sign], sigma=1.0),
                outcome_effect=EffectPriorConfig(constraint=sign[m.outcome_sign], sigma=1.0),
                observation_noise_sigma=float(m.observation_sigma),
                allow_direct_effect=bool(m.direct_effect),
            )
            for m in meds
        ),
        media_to_mediator_map={m.name: tuple(m.channels) for m in meds if m.channels},
    )
    model_cfg, trend_cfg = ext_configs(s, frequency=freq)
    with quiet():
        try:
            return NestedMMM(
                np.asarray(spend, dtype=float),
                np.asarray(y, dtype=float),
                list(data.channels),
                config,
                mediator_data={k: np.asarray(v, dtype=float) for k, v in z.items()},
                index=period_index(data.times, freq),
                model_config=model_cfg,
                trend_config=trend_cfg,
            )
        except (ValueError, NotImplementedError) as exc:
            raise MapiError(
                "mmm-framework refused the model configuration",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings",
                untrusted_text=f"{type(exc).__name__}: {exc}"[:4000],
            ) from exc


def _reorder_channels(data: PanelData, channels: list[str]) -> PanelData:
    if sorted(data.channels) != sorted(channels):
        raise MapiError(
            f"the channels {data.channels} differ from the fitted channels {channels}",
            code=ErrorCode.UNSEEN_LEVELS,
            pointer="/data/spend",
        )
    idx = [data.channels.index(c) for c in channels]
    data.spend = data.spend[..., idx]
    data.channels = list(channels)
    return data


def _labelled(
    values: np.ndarray,
    post: xr.Dataset,
    times: np.ndarray,
    lead: list[str],
    lead_coords: dict[str, Any],
) -> xr.DataArray:
    """``(S, *lead, n)`` to ``(chain, draw, *lead, time)``."""
    chains, draws = int(post.sizes["chain"]), int(post.sizes["draw"])
    arr = np.asarray(values).reshape(chains, draws, *values.shape[1:])
    return xr.DataArray(
        arr,
        dims=["chain", "draw", *lead, TIME],
        coords={
            "chain": post["chain"].values,
            "draw": post["draw"].values,
            **lead_coords,
            TIME: times,
        },
    )


def mediation_dataset(win: FitWindow, to_original: Any) -> xr.Dataset:
    """``direct(sample, channel)``, ``indirect(sample, channel, mediator)``,
    ``total(sample, channel)`` (= direct + the indirect sum) and ``share_mediated(sample,
    channel)``: KPI totals over the fit window at the actual spend."""
    rp = win.replay
    assert isinstance(rp, NestedReplay)
    spend, _ = win.arrays()  # (T, 1, C)
    direct, via = rp.mediated_at(spend[:, 0], np.arange(win.fit.n_periods))
    d = direct.sum(axis=2)  # (S, C)
    ind = via.sum(axis=3)  # (S, C, M)
    total = d + ind.sum(axis=2)
    with np.errstate(divide="ignore", invalid="ignore"):
        share = np.where(total != 0, ind.sum(axis=2) / np.where(total != 0, total, 1.0), np.nan)
    sample = np.arange(d.shape[0])
    coords = {SAMPLE: sample, CHANNEL: list(win.fit.channels)}
    factor, attrs = (1.0, {"output_space": "model"})
    if to_original is not None:
        factor, attrs = win.unit_map(to_original, "y")

    def kpi(arr: np.ndarray, dims: list[str], extra: dict[str, Any], text: str) -> xr.DataArray:
        out = xr.DataArray(arr * factor, dims=dims, coords={**coords, **extra})
        out.attrs = {**attrs, "units_of": "y", "description": text}
        return out

    ds = xr.Dataset(
        {
            "direct": kpi(d, [SAMPLE, CHANNEL], {}, "direct effect over the fit window"),
            "indirect": kpi(
                ind,
                [SAMPLE, CHANNEL, MEDIATOR],
                {MEDIATOR: list(rp.mediators)},
                "effect through each mediator over the fit window",
            ),
            "total": kpi(total, [SAMPLE, CHANNEL], {}, "total effect over the fit window"),
            "share_mediated": xr.DataArray(share, dims=[SAMPLE, CHANNEL], coords=coords),
        }
    )
    ds["share_mediated"].attrs = {"description": "share of the total effect through mediators"}
    ds.attrs = {
        "description": "Direct and mediated effects of each channel on the KPI over the fit "
        "window, per thinned posterior draw"
    }
    return ds
