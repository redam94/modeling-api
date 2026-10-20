"""``mmm.multivariate``: mmm-framework's ``MultivariateMMM`` behind the platform contract.

One national series of spend per channel drives two or more outcomes (the sales of related
products, say). mmm-framework fits, per outcome ``k`` on the standardised scale::

    z_k = alpha[k] + sum_c beta_media[k, c] * sat_c + trend_k + season_k
    Y_k = mean_k + std_k * z_k + sum_s psi[s, k] * Y_s          (Y: the observed outcomes)

with the media transform shared by every outcome (normalised geometric adstock over 8
periods, ``sat = 1 - exp(-lambda * x)``), residuals correlated across outcomes (MvNormal
with an LKJ prior) and cross-effects ``psi`` (``unconstrained``, ``cannibalization`` < 0 or
``halo`` > 0) from a source outcome to a target outcome.

**Cross-effect policy.** The engine conditions on the *observed* source outcome. Scores,
derived quantities and plans instead use the reduced form ``Y = (I - P)^-1 c`` per draw and
period (``P[k, s] = psi[s, k]``, ``c_k = mean_k + std_k * z_k``), so a channel that moves
outcome A also moves outcome B through ``psi``. Every additive term is propagated through
``(I - P)^-1``, so the contributions still sum to ``y_hat`` per outcome. Draws whose
cross-effects form a loop without a solution (``|det(I - P)| < 1e-6``) are refused.

**Value units.** :class:`MultivariateReplay` implements the
:class:`~mapi_plugin_mmm.forward.ReplayLike` protocol on the weighted total
``sum_k weight_k * Y_k`` (``outcomes[*].weight``), so the shared window, response-curve,
marginal-cost, planning and tracking code works unchanged; the by-outcome quantities come
from :meth:`MultivariateReplay.evaluate_outcomes` and
:meth:`MultivariateReplay.channel_totals_by_outcome`.

Bundle layout: ``posterior`` (mmm-framework's parameters, compacted, plus
``trend_by_period(chain, draw, outcome, trend_period)`` read from the ``mapi_trend_<k>``
deterministics the plugin's engine subclass registers), ``fit_data`` (``y(time, outcome)``,
``spend(time, channel)``), ``observed_data``, ``sample_stats``, a thinned ``prior`` and
``log_likelihood`` when kept.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Literal

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

from mapi_plugin_mmm import extended, tracking
from mapi_plugin_mmm.base import FrameworkMMM, diagnostics_from, draw_samples, positions_of
from mapi_plugin_mmm.derived import (
    SAMPLE,
    FitWindow,
    ToOriginal,
    adstock_dataset,
    marginal_cost_dataset,
    response_curves_dataset,
)
from mapi_plugin_mmm.forward import TREND_BY_PERIOD, CellRef, Components, Posterior
from mapi_plugin_mmm.framework import (
    APPROX_ENGINES,
    BASELINE,
    CHANNEL,
    COMPONENT,
    FIT_DATA,
    MCMC_ENGINES,
    OUTCOME,
    SEASONALITY,
    SPEND,
    TARGET,
    TIME,
    TREND,
    PanelData,
    engine_plan,
    framework_version,
    infer_frequency,
    period_index,
    quiet,
)
from mapi_plugin_mmm.planning import optimize_budget_plan, thin_index
from mapi_plugin_mmm.settings import ADVANCED, BASIC, _Sub, mcmc_fit_schema

__all__ = [
    "CrossEffectSettings",
    "MMMMultivariate",
    "MultivariateFitWindow",
    "MultivariateReplay",
    "MultivariateSettings",
    "OutcomeComponents",
    "OutcomeSettings",
]

LOOP_DET_MIN: Final = 1e-6
"""Smallest ``|det(I - P)|`` of a draw's cross-effects the reduced form accepts."""
_PAIR_SEP: Final = "→"
_LABEL_MAX: Final = 128


# ======================================================================== settings


class OutcomeSettings(_Sub):
    """One outcome (a label of the data's ``outcome`` dim) and its priors."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=_LABEL_MAX,
        description="A label of the data's outcome dim",
        json_schema_extra=BASIC,
    )
    weight: float = Field(
        1.0,
        ge=0,
        le=1e6,
        description="Value of one unit of this outcome in the budget objective (the plans "
        "and response curves use the weighted total of the outcomes)",
        json_schema_extra=BASIC,
    )
    include_trend: bool = Field(True, json_schema_extra=ADVANCED)
    include_seasonality: bool = Field(True, json_schema_extra=ADVANCED)
    intercept_sigma: float = Field(
        2.0,
        gt=0,
        le=20,
        description="Prior sd of the baseline level, in outcome standard deviations",
        json_schema_extra=ADVANCED,
    )
    media_sigma: float = Field(
        0.5,
        gt=0,
        le=10,
        description="Prior sd of each channel effect, in outcome standard deviations",
        json_schema_extra=ADVANCED,
    )


class CrossEffectSettings(_Sub):
    """An effect of one outcome on another (mmm-framework ``CrossEffectConfig``)."""

    source: str = Field(..., min_length=1, max_length=_LABEL_MAX, json_schema_extra=BASIC)
    target: str = Field(..., min_length=1, max_length=_LABEL_MAX, json_schema_extra=BASIC)
    kind: Literal["unconstrained", "cannibalization", "halo"] = Field(
        "unconstrained",
        description="unconstrained (the data picks the sign), cannibalization (the source "
        "takes sales from the target) or halo (the source adds sales to the target)",
        json_schema_extra=BASIC,
    )
    prior_sigma: float = Field(
        0.3,
        gt=0,
        le=5,
        description="Prior sd of the effect (target units per unit of source)",
        json_schema_extra=ADVANCED,
    )


class MultivariateSettings(extended.ExtSettings):
    """Settings of ``mmm.multivariate`` (mmm-framework ``MultivariateMMM``).

    Canonical names (bind dataset names to these in ``data.bind``): dims ``time``,
    ``channel`` and ``outcome``; target ``y(outcome, time)``; spend ``spend(channel,
    time)``. National data only, no control variables. ``likelihood`` is not used: the
    outcomes share a correlated normal likelihood.
    """

    outcomes: list[OutcomeSettings] = Field(
        default_factory=list,
        max_length=8,
        description="Settings per outcome; empty: every outcome of the data, with defaults",
        json_schema_extra=BASIC,
    )
    primary_outcome: str | None = Field(
        None,
        max_length=_LABEL_MAX,
        description="The outcome reports show first (default: the first outcome)",
        json_schema_extra=BASIC,
    )
    cross_effects: list[CrossEffectSettings] = Field(
        default_factory=list,
        max_length=28,
        description="Effects of one outcome on another",
        json_schema_extra=BASIC,
    )
    lkj_eta: float = Field(
        2.0,
        gt=0,
        le=50,
        description="LKJ concentration of the residual correlation (larger: closer to none)",
        json_schema_extra=ADVANCED,
    )
    share_adstock: bool = Field(
        True,
        description="One carry-over rate for every channel",
        json_schema_extra=ADVANCED,
    )
    share_trend: bool = Field(
        False, description="One trend for every outcome", json_schema_extra=ADVANCED
    )
    share_seasonality: bool = Field(
        True, description="One seasonal pattern for every outcome", json_schema_extra=ADVANCED
    )

    @field_validator("outcomes")
    @classmethod
    def _unique_outcomes(cls, v: list[OutcomeSettings]) -> list[OutcomeSettings]:
        names = [o.name for o in v]
        if len(set(names)) != len(names):
            raise ValueError("outcomes lists each outcome once")
        if v and not any(o.weight > 0 for o in v):
            raise ValueError("at least one outcome needs a weight above zero")
        return v

    @model_validator(mode="after")
    def _check(self) -> MultivariateSettings:
        known = {o.name for o in self.outcomes}
        if self.primary_outcome is not None and known and self.primary_outcome not in known:
            raise ValueError(f"primary_outcome {self.primary_outcome!r} is not in outcomes")
        pairs: set[tuple[str, str]] = set()
        for ce in self.cross_effects:
            if ce.source == ce.target:
                raise ValueError(f"cross effect {ce.source!r} -> {ce.target!r} is a self-loop")
            if known and not {ce.source, ce.target} <= known:
                raise ValueError(
                    f"cross effect {ce.source!r} -> {ce.target!r} names an outcome that is "
                    "not in outcomes"
                )
            if (ce.source, ce.target) in pairs:
                raise ValueError(f"cross effect {ce.source!r} -> {ce.target!r} is listed twice")
            pairs.add((ce.source, ce.target))
        return self

    def resolve(self, labels: Sequence[str]) -> list[OutcomeSettings]:
        """Settings per data label, in data order (``MapiError`` when the settings and the
        data's outcome labels disagree)."""
        labels = [str(x) for x in labels]
        if not self.outcomes:
            unknown = sorted({self.primary_outcome or labels[0]} - set(labels)) + sorted(
                {x for ce in self.cross_effects for x in (ce.source, ce.target)} - set(labels)
            )
            if unknown:
                raise MapiError(
                    f"outcome(s) {unknown} are not in the data (its outcomes: {labels})",
                    code=ErrorCode.SETTINGS_INVALID,
                    pointer="/model/settings",
                )
            return [OutcomeSettings(name=x) for x in labels]
        by_name = {o.name: o for o in self.outcomes}
        missing = [n for n in by_name if n not in labels]
        if missing:
            raise MapiError(
                f"outcome(s) {missing} are not in the data (its outcomes: {labels})",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/outcomes",
            )
        extra = [x for x in labels if x not in by_name]
        if extra:
            raise MapiError(
                f"the data has outcome(s) {extra} that are not in outcomes: add them to the "
                "settings or remove them from the data",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/outcomes",
            )
        return [by_name[x] for x in labels]


# ======================================================================== engine


@functools.cache
def engine_class() -> type[Any]:
    """mmm-framework's ``MultivariateMMM`` with two platform additions: every outcome's
    trend term is registered as ``mapi_trend_<k>`` (standardised, over ``obs``) so any trend
    kind can be replayed, and the primary outcome is configurable."""
    import pymc as pm
    from mmm_framework.mmm_extensions.models.multivariate import MultivariateMMM

    class PlatformMultivariateMMM(MultivariateMMM):
        mapi_primary: int = 0

        def _baseline_dynamics_terms(self) -> tuple[dict[Any, Any], dict[Any, Any]]:
            trend_terms, season_terms = super()._baseline_dynamics_terms()
            for k, term in sorted(trend_terms.items()):
                if term is not None:
                    pm.Deterministic(f"mapi_trend_{k}", term, dims="obs")
            return trend_terms, season_terms

        def _primary_outcome_index(self) -> int:
            return int(self.mapi_primary)

    return PlatformMultivariateMMM


def build_engine(
    spend: np.ndarray,
    y: np.ndarray,
    times: np.ndarray,
    channels: list[str],
    outcomes: list[OutcomeSettings],
    s: MultivariateSettings,
    *,
    frequency: str,
    primary: int,
) -> Any:
    """The engine model for ``spend (T, C)`` and ``y (T, K)`` (raw units)."""
    from mmm_framework.mmm_extensions.config import (
        CrossEffectConfig,
        CrossEffectType,
        EffectPriorConfig,
        MultivariateModelConfig,
        OutcomeConfig,
    )

    model_config, trend_config = extended.ext_configs(s, frequency=frequency)
    config = MultivariateModelConfig(
        outcomes=tuple(
            OutcomeConfig(
                name=o.name,
                column=o.name,
                intercept_prior_sigma=float(o.intercept_sigma),
                media_effect=EffectPriorConfig(sigma=float(o.media_sigma)),
                include_trend=o.include_trend,
                include_seasonality=o.include_seasonality,
            )
            for o in outcomes
        ),
        cross_effects=tuple(
            CrossEffectConfig(
                source_outcome=ce.source,
                target_outcome=ce.target,
                effect_type=CrossEffectType(ce.kind),
                prior_sigma=float(ce.prior_sigma),
                promotion_modulated=False,
            )
            for ce in s.cross_effects
        ),
        lkj_eta=float(s.lkj_eta),
        share_media_adstock=s.share_adstock,
        share_trend=s.share_trend,
        share_seasonality=s.share_seasonality,
    )
    cls = engine_class()
    model = cls(
        np.asarray(spend, dtype=float),
        {o.name: np.asarray(y[:, k], dtype=float) for k, o in enumerate(outcomes)},
        list(channels),
        config,
        index=period_index(times, frequency),
        model_config=model_config,
        trend_config=trend_config,
    )
    model.mapi_primary = int(primary)
    return model


def trend_extra(post: xr.Dataset, outcomes: Sequence[str]) -> xr.Dataset | None:
    """``trend_by_period(chain, draw, outcome, trend_period)`` from the ``mapi_trend_<k>``
    deterministics (zeros for an outcome without a trend); ``None`` without any trend."""
    names = [f"mapi_trend_{k}" for k in range(len(outcomes))]
    present = [n for n in names if n in post]
    if not present:
        return None
    ref = post[present[0]]
    shape = (int(ref.sizes["chain"]), int(ref.sizes["draw"]), -1)
    first = np.asarray(ref.transpose("chain", "draw", ...).values).reshape(shape)
    stacked = np.stack(
        [
            np.asarray(post[n].transpose("chain", "draw", ...).values).reshape(shape)
            if n in post
            else np.zeros_like(first)
            for n in names
        ],
        axis=2,
    )
    da = xr.DataArray(
        stacked,
        dims=["chain", "draw", OUTCOME, "trend_period"],
        coords={
            "chain": post["chain"].values,
            "draw": post["draw"].values,
            OUTCOME: np.asarray(list(outcomes), dtype=str),
        },
    )
    return xr.Dataset({TREND_BY_PERIOD: da})


# ======================================================================== replay


@dataclass(slots=True)
class OutcomeComponents:
    """Every term per outcome in KPI units: ``channel (S, C, K, n)``, ``baseline``,
    ``trend``, ``seasonality`` and ``cross`` ``(S, K, n)``.

    In the reduced form (scoring) every term is already propagated through ``(I - P)^-1``
    and ``cross`` is zero; in the observed form (``observed=`` - parity with the engine's
    ``mu``) ``cross`` is ``sum_s psi[s, k] * Y_s`` on the observed outcomes."""

    channel: np.ndarray
    baseline: np.ndarray
    trend: np.ndarray
    seasonality: np.ndarray
    cross: np.ndarray
    chol: np.ndarray  # (S, K, K) residual Cholesky factor, standardised scale
    stds: np.ndarray  # (K,) outcome standard deviations
    propagator: np.ndarray | None  # (S, K, K) (I - P)^-1, None in the observed form
    notes: list[str] = field(default_factory=list)

    @property
    def mu(self) -> np.ndarray:
        """The expected outcomes ``(S, K, n)``."""
        return self.baseline + self.trend + self.seasonality + self.cross + self.channel.sum(1)

    def noise(self, seed: int, name: str = "y") -> np.ndarray:
        """Correlated observation noise ``(S, K, n)`` in KPI units from a generator seeded by
        ``(seed, name)`` (common random numbers), propagated like the terms."""
        digest = hashlib.sha256(name.encode()).digest()
        rng = np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])
        z = rng.standard_normal(self.baseline.shape)
        eps = np.einsum("skj,sjn->skn", self.chol, z) * self.stds[None, :, None]
        if self.propagator is not None:
            eps = np.einsum("skj,sjn->skn", self.propagator, eps)
        return np.asarray(eps)


class MultivariateReplay:
    """The forward pass of a fitted ``mmm.multivariate`` model (see the module doc).

    Built from the bundle's ``learned`` record and the posterior draws (no engine graph).
    The :class:`~mapi_plugin_mmm.forward.ReplayLike` methods report the weighted total of
    the outcomes; spend arrays are ``(T_full, C)`` or ``(T_full, 1, C)`` (one national
    cell).
    """

    def __init__(self, learned: Mapping[str, Any], post: Posterior) -> None:
        self.post = post
        self.n = post.n
        self.channels: list[str] = [str(c) for c in learned["channels"]]
        self.outcomes: list[str] = [str(o) for o in learned["outcomes"]]
        self.primary: str = str(learned.get("primary_outcome") or self.outcomes[0])
        k = len(self.outcomes)
        self.weights = np.array([float(learned["weights"][o]) for o in self.outcomes], dtype=float)
        scaling = learned["scaling"]
        self.means = np.array([float(scaling["outcome_mean"][o]) for o in self.outcomes])
        self.stds = np.array([float(scaling["outcome_std"][o]) for o in self.outcomes])
        self.scale = np.array([float(scaling["media_scale"][c]) for c in self.channels])
        self.n_periods = int(learned["trend"]["n_periods"])
        # media: shared or per-channel carry-over, per-channel saturation rate
        shared = post.get("alpha_shared")
        self.kernels: list[np.ndarray] = []
        self.lam: list[np.ndarray] = []
        for ch in self.channels:
            alpha = shared if shared is not None else post.need(f"alpha_{ch}")
            self.kernels.append(extended.ext_kernels(alpha))
            self.lam.append(post.need(f"lambda_{ch}"))
        self.alpha = post.need("alpha").reshape(self.n, k)
        self.beta = post.need("beta_media").reshape(self.n, k, len(self.channels))
        # trend and seasonality per outcome
        trend = learned["trend"]
        self.trend_kind = str(trend["kind"])
        self.trend_on = [bool(x) for x in trend["by_outcome"]]
        self.trend_shared = bool(trend.get("shared", False))
        self.season_terms = [extended.SeasonTerm(**t) for t in learned.get("season_terms", [])]
        season = learned.get("seasonality", {})
        self.season_on = [bool(x) for x in season.get("by_outcome", [False] * k)]
        self.season_shared = bool(season.get("shared", True))
        # cross-effects and the residual covariance
        psi = post.get("psi_matrix")
        self.psi = psi.reshape(self.n, k, k) if psi is not None else np.zeros((self.n, k, k))
        sd = post.get("Y_obs_chol_cov_stds")
        corr = post.get("Y_obs_correlation")
        if sd is None or corr is None:
            raise MapiError(
                "the posterior has no residual covariance (Y_obs_chol_cov_stds, Y_obs_correlation)",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        cov = sd.reshape(self.n, k, 1) * corr.reshape(self.n, k, k) * sd.reshape(self.n, 1, k)
        self.chol = np.linalg.cholesky(cov + 1e-12 * np.eye(k)[None])
        self._propagator: np.ndarray | None = None

    # ------------------------------------------------------------------ terms
    @property
    def n_outcomes(self) -> int:
        return len(self.outcomes)

    def propagator(self) -> np.ndarray:
        """``(S, K, K)`` ``(I - P)^-1`` with ``P[k, s] = psi[s, k]`` (refused when a draw's
        cross-effects form a loop without a solution)."""
        if self._propagator is None:
            k = self.n_outcomes
            a = np.eye(k)[None] - self.psi.transpose(0, 2, 1)
            det = np.linalg.det(a)
            if (np.abs(det) < LOOP_DET_MIN).any():
                raise MapiError(
                    "the cross-effects form a loop without a solution",
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                    pointer="/model/settings/cross_effects",
                )
            self._propagator = np.linalg.inv(a)
        return self._propagator

    @staticmethod
    def _flat_spend(spend: np.ndarray) -> np.ndarray:
        arr = np.asarray(spend, dtype=float)
        return arr[:, 0, :] if arr.ndim == 3 else arr

    def media_at(self, spend: np.ndarray, positions: np.ndarray) -> np.ndarray:
        """``(S, C, n)`` saturated media at ``positions`` (full spend history in front)."""
        return np.asarray(
            extended.saturated_media(
                self._flat_spend(spend),
                np.asarray(positions, dtype=int),
                self.kernels,
                self.lam,
                self.scale,
            )
        )

    def trend_at(self, positions: np.ndarray) -> np.ndarray:
        """``(S, K, n)`` trend per outcome (standardised units)."""
        pos = np.asarray(positions, dtype=int)
        out = np.zeros((self.n, self.n_outcomes, pos.size))
        if self.trend_kind == "none":
            return out
        by_period = self.post.get(TREND_BY_PERIOD)
        for k, name in enumerate(self.outcomes):
            if not self.trend_on[k]:
                continue
            slope = "trend_slope" if self.trend_shared else f"{name}_trend_slope"
            out[:, k] = extended.trend_values(
                self.post,
                kind=self.trend_kind,
                slope_name=slope,
                by_period=(
                    by_period.reshape(self.n, self.n_outcomes, -1)[:, k]
                    if by_period is not None
                    else None
                ),
                n_periods=self.n_periods,
                positions=pos,
            )
        return out

    def seasonality_at(self, positions: np.ndarray) -> np.ndarray:
        """``(S, K, n)`` Fourier seasonality per outcome (standardised units)."""
        pos = np.asarray(positions, dtype=int)
        out = np.zeros((self.n, self.n_outcomes, pos.size))
        if not self.season_terms:
            return out
        feats = extended.fourier_at(pos, self.season_terms)  # (n, F)
        for k, name in enumerate(self.outcomes):
            if not self.season_on[k]:
                continue
            coef = self.post.get(
                "seasonality_coefs" if self.season_shared else f"{name}_seasonality_coefs"
            )
            if coef is not None:
                out[:, k] = coef.reshape(self.n, -1) @ feats.T
        return out

    def _notes(self, pos: np.ndarray) -> list[str]:
        if (
            pos.size
            and int(pos.max()) >= self.n_periods
            and self.trend_kind not in ("none", "linear")
            and any(self.trend_on)
        ):
            return [
                f"the {self.trend_kind} trend is held at its last fitted level beyond the fit "
                "window, so the interval does not widen with the horizon"
            ]
        return []

    def evaluate_outcomes(
        self,
        spend: np.ndarray,
        positions: np.ndarray,
        *,
        observed: np.ndarray | None = None,
        seed: int = 0,
    ) -> OutcomeComponents:
        """Every term per outcome at ``positions`` (KPI units).

        Without ``observed`` the reduced form (every term through ``(I - P)^-1``); with
        ``observed (n, K)`` (raw outcomes at ``positions``) the engine's own observed form.
        ``seed`` is unused (the replay has no random terms besides noise)."""
        del seed
        pos = np.asarray(positions, dtype=int)
        sd = self.stds[None, :, None]
        sat = self.media_at(spend, pos)  # (S, C, n)
        channel = np.einsum("skc,scn->sckn", self.beta, sat) * self.stds[None, None, :, None]
        baseline = np.broadcast_to(
            (self.means[None, :] + self.stds[None, :] * self.alpha)[:, :, None],
            (self.n, self.n_outcomes, pos.size),
        )
        trend = self.trend_at(pos) * sd
        season = self.seasonality_at(pos) * sd
        if observed is not None:
            obs = np.asarray(observed, dtype=float).reshape(pos.size, self.n_outcomes)
            cross = np.einsum("sjk,nj->skn", self.psi, obs)
            return OutcomeComponents(
                channel=channel,
                baseline=np.array(baseline),
                trend=trend,
                seasonality=season,
                cross=cross,
                chol=self.chol,
                stds=self.stds,
                propagator=None,
                notes=self._notes(pos),
            )
        m = self.propagator()

        def prop(term: np.ndarray) -> np.ndarray:
            return np.asarray(np.einsum("skj,sjn->skn", m, term))

        return OutcomeComponents(
            channel=np.einsum("skj,scjn->sckn", m, channel),
            baseline=prop(np.asarray(baseline)),
            trend=prop(trend),
            seasonality=prop(season),
            cross=np.zeros((self.n, self.n_outcomes, pos.size)),
            chol=self.chol,
            stds=self.stds,
            propagator=m,
            notes=self._notes(pos),
        )

    def channel_totals_by_outcome(
        self, spend: np.ndarray, positions: np.ndarray, multiplier: float
    ) -> np.ndarray:
        """``(S, C, K)`` total over ``positions`` of each channel's (propagated) effect on
        each outcome when its spend is scaled by ``multiplier``."""
        comps = self.evaluate_outcomes(self._flat_spend(spend) * multiplier, positions)
        return np.asarray(comps.channel.sum(axis=3))

    # ------------------------------------------------------------------ ReplayLike
    def _weigh(self, arr: np.ndarray, axis: int) -> np.ndarray:
        return np.asarray(np.tensordot(arr, self.weights, axes=([axis], [0])))

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
        """The weighted total of the outcomes as :class:`~mapi_plugin_mmm.forward.Components`
        (one national cell, no controls; ``sigma`` is the sd of the weighted noise)."""
        del ctrl, cells, strategy
        pos = np.asarray(positions, dtype=int)
        oc = self.evaluate_outcomes(spend, pos, seed=seed)
        m = oc.propagator if oc.propagator is not None else np.eye(self.n_outcomes)[None]
        # sd of sum_k w_k * noise_k: w' M D L L' D M' w per draw
        v = np.einsum("k,skj->sj", self.weights, m) * self.stds[None, :]
        lv = np.einsum("sj,sji->si", v, oc.chol)
        sigma = np.sqrt((lv**2).sum(axis=1))
        return Components(
            channel=self._weigh(oc.channel, 2)[:, :, None, :],
            baseline=self._weigh(oc.baseline, 1)[:, None, :],
            trend=self._weigh(oc.trend, 1),
            seasonality=self._weigh(oc.seasonality, 1),
            controls=np.zeros((self.n, 0, 1, pos.size)),
            sigma=sigma,
            nu=None,
            y_std=1.0,
            notes=oc.notes,
        )

    def channel_totals(
        self,
        spend: np.ndarray,
        ctrl: np.ndarray,
        positions: np.ndarray,
        cells: Sequence[CellRef],
        multiplier: float,
    ) -> np.ndarray:
        """``(S, C, 1)`` weighted total over ``positions`` of each channel's effect at
        ``multiplier`` times its spend (channels add up, so scaling them all at once gives
        each channel's own curve)."""
        del ctrl, cells
        tot = self.channel_totals_by_outcome(spend, positions, multiplier)
        return self._weigh(tot, 2)[:, :, None]

    def steady_state(
        self, spend: np.ndarray, cells: Sequence[CellRef], *, position: int | None = None
    ) -> np.ndarray:
        """``(S, 1, C)`` weighted value per period of a constant raw spend ``(1, C)`` held
        until the carry-over settles."""
        del cells, position
        x = np.asarray(spend, dtype=float).reshape(-1)[: len(self.channels)]
        sat = np.stack(
            [
                1.0 - np.exp(-self.lam[c] * (x[c] / self.scale[c]) * self.kernels[c].sum(axis=1))
                for c in range(len(self.channels))
            ],
            axis=1,
        )  # (S, C)
        term = self.beta * sat[:, None, :] * self.stds[None, :, None]  # (S, K, C)
        prop = np.einsum("skj,sjc->skc", self.propagator(), term)
        return self._weigh(prop, 1)[:, None, :]

    def fit_cells(self) -> list[CellRef]:
        return [CellRef(0, 0, "0/0")]


# ======================================================================== window


@dataclass(slots=True)
class MultivariateFitWindow(FitWindow):
    """The weighted replay over the fit window; ``y`` probes carry the outcome dim."""

    outcomes: tuple[str, ...] = ()

    def unit_map(self, to_original: ToOriginal, src: str) -> tuple[float, dict[str, Any]]:
        if src == "spend" or not self.outcomes:
            return FitWindow.unit_map(self, to_original, src)
        if src in self._units:
            return self._units[src]
        probe = xr.DataArray(
            np.ones((len(self.outcomes), self.fit.n_periods)),
            dims=[OUTCOME, TIME],
            coords={OUTCOME: np.asarray(self.outcomes), TIME: self.fit.times},
        )
        out = to_original(probe, src)
        vals = np.asarray(out.values, dtype=float)
        finite = vals[np.isfinite(vals)]
        factor = float(finite.flat[0]) if finite.size else 1.0
        if finite.size and not np.allclose(finite, factor, rtol=1e-9, atol=1e-12):
            raise MapiError(
                f"the pipeline rescales {src!r} after its last reshape step; the mmm-framework "
                "model owns scaling, so drop that step",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/pipeline",
            )
        attrs = {k: v for k, v in out.attrs.items() if k in ("units", "output_space")}
        self._units[src] = (factor, attrs)
        return factor, attrs

    @property
    def mv(self) -> MultivariateReplay:
        rp = self.replay
        assert isinstance(rp, MultivariateReplay)
        return rp


def _convert(
    to_original: ToOriginal | None, da: xr.DataArray, src: str, win: FitWindow
) -> xr.DataArray:
    """``da`` in the output space with its units labelled (as the shared derived code)."""
    if to_original is None:
        out = da.copy()
        out.attrs = {**da.attrs, "output_space": "model", "units_of": src}
        return out
    factor, attrs = win.unit_map(to_original, src)
    out = da * factor if factor != 1.0 else da.copy()
    out.attrs = {**da.attrs, **attrs, "units_of": src}
    return out


# ======================================================================== plugin

SCHEMA_TEMPLATE: Final[dict[str, Any]] = {
    "template": {
        "dims": {
            TIME: {"kind": "time", "policy": "extend"},
            CHANNEL: {"kind": "categorical", "policy": "fixed"},
            OUTCOME: {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            TARGET: {"dims": [OUTCOME, TIME], "dtype": "float64", "role": "target"},
            SPEND: {
                "dims": [CHANNEL, TIME],
                "dtype": "float64",
                "role": "spend",
                "future_required": True,
            },
        },
    }
}
"""Canonical names: ``y(outcome, time)``, ``spend(channel, time)`` (national data)."""

OUTPUTS: Final[dict[str, Any]] = {
    "y_hat": {
        "dims": [TIME],
        "dims_of": TARGET,
        "units_of": TARGET,
        "description": "Expected (or predictive) value of every outcome over time",
    },
    "contributions": {
        "dims": [COMPONENT, TIME],
        "dims_of": TARGET,
        "units_of": TARGET,
        "description": "Every channel, the baseline, trend and seasonality per outcome, "
        "with cross-effects passed through; they sum to y_hat",
    },
}
DERIVED: Final[list[dict[str, Any]]] = [
    {
        "name": "contributions",
        "dims": [SAMPLE, CHANNEL, OUTCOME, TIME],
        "units": "target",
        "cache": "fit",
        "description": "Channel, baseline, trend and seasonality contributions per outcome "
        "over the fit window per thinned draw",
    },
    {
        "name": "response_curves",
        "dims": [SAMPLE, CHANNEL, "multiplier"],
        "units": "target",
        "cache": "fit",
        "description": "Weighted total of the outcomes per spend multiplier and channel per "
        "thinned draw, plus the curves per outcome",
    },
    {
        "name": "marginal_cost",
        "dims": [SAMPLE, CHANNEL],
        "units": "spend per target",
        "cache": "fit",
        "description": "Extra spend per extra unit of the weighted outcome total at current spend",
    },
    {
        "name": "adstock",
        "dims": [SAMPLE, CHANNEL, "lag"],
        "cache": "fit",
        "description": "Share of a unit of spend's effect that lands each period later",
    },
    {
        "name": "cross_effects",
        "dims": [SAMPLE, "pair"],
        "units": "target per target",
        "cache": "fit",
        "description": "Effect of one outcome on another (target units per unit of source)",
    },
]


class MMMMultivariate(CustomModel[MultivariateSettings]):
    """mmm-framework's multivariate (several outcomes) marketing-mix model."""

    id = "mmm.multivariate"
    version = "1.0.0"
    Settings = MultivariateSettings
    family = "mmm"
    schema_template = SCHEMA_TEMPLATE
    outputs: ClassVar[dict[str, Any]] = OUTPUTS
    derived: ClassVar[list[dict[str, Any]]] = DERIVED
    pad_extend = False
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
        calibration=frozenset(),
    )
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
        "mmm.cross_effects",
    ]
    resources = ResourceHint(
        min_class=ResourceClass.S, default_class=ResourceClass.M, est_fit_seconds=900
    )
    fit_schema: ClassVar[dict[str, Any]] = mcmc_fit_schema(
        engines=[*MCMC_ENGINES, *APPROX_ENGINES], default_engine="nutpie"
    )
    description = (
        "mmm-framework's multivariate marketing-mix model: one national spend series per "
        "channel drives two or more outcomes, with correlated residuals and cross-effects "
        "between outcomes (cannibalization, halo); budgets are optimised on the weighted "
        "total of the outcomes."
    )
    default_engine: ClassVar[str] = "nutpie"
    _cache: tuple[Any, Any, MultivariateReplay, PanelData] | None = None

    # ------------------------------------------------------------------ data
    @staticmethod
    def _arrays(
        ds: xr.Dataset, *, with_target: bool, channels: Sequence[str] | None = None
    ) -> tuple[np.ndarray, list[str], np.ndarray, np.ndarray | None, list[str]]:
        """``(times, channels, spend (T, C), y (T, K) | None, outcome labels)``."""
        if SPEND not in ds:
            raise MapiError(
                "the data has no spend variable",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/spend",
            )
        sp = ds[SPEND]
        if set(map(str, sp.dims)) != {CHANNEL, TIME}:
            raise MapiError(
                f"spend must be over (channel, time) - the model takes national data only, "
                f"got {list(map(str, sp.dims))}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/spend",
            )
        if channels is not None:
            unknown = sorted(set(map(str, sp[CHANNEL].values)) - set(channels))
            if unknown:
                raise MapiError(
                    f"channel(s) {unknown} were not seen at fit",
                    code=ErrorCode.UNSEEN_LEVELS,
                    pointer="/data/spend",
                )
            sp = sp.reindex({CHANNEL: list(channels)})
        times = np.asarray(ds[TIME].values)
        spend = np.asarray(sp.transpose(TIME, CHANNEL).values, dtype=float)
        labels: list[str] = []
        y = None
        if with_target:
            if TARGET not in ds:
                raise MapiError(
                    "the data has no target variable",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/data/y",
                )
            da = ds[TARGET]
            if set(map(str, da.dims)) != {OUTCOME, TIME}:
                raise MapiError(
                    f"the target must be over (outcome, time), got {list(map(str, da.dims))}",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/data/y",
                )
            labels = [str(x) for x in da[OUTCOME].values]
            y = np.asarray(da.transpose(TIME, OUTCOME).values, dtype=float)
        return times, [str(c) for c in sp[CHANNEL].values], spend, y, labels

    @staticmethod
    def _check_fit_data(spend: np.ndarray, y: np.ndarray, labels: list[str]) -> None:
        if len(labels) < 2:
            raise MapiError(
                "a multivariate model needs two or more outcomes",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/y",
            )
        if not np.isfinite(y).all():
            raise MapiError(
                "the outcomes have missing or infinite values",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/y",
            )
        if not np.isfinite(spend).all() or (spend < 0).any():
            raise MapiError(
                "spend must be finite and non-negative",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/spend",
            )
        if spend.shape[0] < 8:
            raise MapiError(
                "a multivariate model needs at least 8 periods",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )

    # ------------------------------------------------------------------ fit
    def build(
        self, ds: xr.Dataset, s: MultivariateSettings
    ) -> tuple[Any, dict[str, Any], xr.Dataset, xr.Dataset]:
        """The engine model, the ``learned`` record, ``fit_data`` and ``observed_data`` for
        the model-stage data ``ds`` (used by :meth:`fit` and the prior-draw tests)."""
        times, channels, spend, y, labels = self._arrays(ds, with_target=True)
        assert y is not None
        self._check_fit_data(spend, y, labels)
        outcomes = s.resolve(labels)
        names = [o.name for o in outcomes]
        primary = s.primary_outcome or names[0]
        if primary not in names:
            raise MapiError(
                f"primary_outcome {primary!r} is not in the data (its outcomes: {names})",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/primary_outcome",
            )
        freq = infer_frequency(times, s.frequency)
        with quiet():
            model = build_engine(
                spend, y, times, channels, outcomes, s, frequency=freq,
                primary=names.index(primary),
            )  # fmt: skip
        trend_on = model.trend_config is not None and str(
            getattr(model.trend_config.type, "value", model.trend_config.type)
        ) not in ("none", "")
        season_terms = extended.season_terms(freq, s.seasonality)
        learned: dict[str, Any] = {
            "mmm_framework": framework_version(),
            "frequency": freq,
            "channels": channels,
            "outcomes": names,
            "primary_outcome": primary,
            "weights": {o.name: float(o.weight) for o in outcomes},
            "scaling": {
                "outcome_mean": {n: float(model.outcome_means[n]) for n in names},
                "outcome_std": {n: float(model.outcome_stds[n]) for n in names},
                "media_scale": {
                    c: float(v) for c, v in zip(channels, model._media_scale, strict=True)
                },
            },
            "trend": {
                "kind": s.trend.kind if trend_on else "none",
                "n_periods": int(times.size),
                "shared": bool(s.share_trend),
                "by_outcome": [bool(trend_on and o.include_trend) for o in outcomes],
            },
            "season_terms": [
                {"name": t.name, "period": float(t.period), "order": int(t.order)}
                for t in season_terms
            ],
            "seasonality": {
                "shared": bool(s.share_seasonality),
                "by_outcome": [bool(season_terms and o.include_seasonality) for o in outcomes],
            },
            "cross_effects": [
                {"source": ce.source, "target": ce.target, "kind": ce.kind}
                for ce in s.cross_effects
            ],
        }
        fit_data = xr.Dataset(
            {
                TARGET: ((TIME, OUTCOME), y),
                SPEND: ((TIME, CHANNEL), spend),
            },
            coords={
                TIME: times,
                OUTCOME: np.asarray(names, dtype=str),
                CHANNEL: np.asarray(channels, dtype=str),
            },
        )
        observed = xr.Dataset(
            {TARGET: ((OUTCOME, TIME), y.T)},
            coords={TIME: times, OUTCOME: np.asarray(names, dtype=str)},
        )
        return model, learned, fit_data, observed

    def bundle_groups(
        self,
        model: Any,
        post: xr.Dataset,
        learned: Mapping[str, Any],
        fit_data: xr.Dataset,
        observed: xr.Dataset,
        cfg: FitConfig,
    ) -> dict[str, xr.Dataset]:
        """The bundle groups of a fitted engine model whose posterior is ``post``."""
        extra = trend_extra(post, learned["outcomes"])
        return extended.ext_fit_groups(
            model,
            post,
            fit_data=fit_data,
            observed=observed,
            extra={"posterior": extra} if extra is not None else {},
            cfg=cfg,
            ll_var="Y_obs",
        )

    def fit(
        self, ds: xr.Dataset, s: MultivariateSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        unused = sorted(str(v) for v in ds.data_vars if v not in (TARGET, SPEND))
        if unused:
            ctx.info(f"the model takes no control variables, so it does not use {unused}")
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
        ctx.progress(0.05, "building the mmm-framework model")
        model, learned, fit_data, observed = self.build(ds, s)
        ctx.check_cancelled()
        ctx.progress(0.1, f"sampling ({plan.engine})")
        result = extended.fit_extended(model, plan, ctx)
        ctx.check_cancelled()
        ctx.progress(0.9, "posterior ready")
        post = model._trace["posterior"]
        post = post.to_dataset(inherit=False) if isinstance(post, xr.DataTree) else post
        groups = self.bundle_groups(model, post, learned, fit_data, observed, cfg)
        raw_diag = getattr(result, "diagnostics", None) or {}
        diagnostics: list[Diagnostic] = list(diagnostics_from(raw_diag))
        if plan.mcmc:
            free = [rv.name for rv in model.model.free_RVs if rv.name in groups["posterior"]]
            diagnostics += convergence_diagnostics(
                groups["posterior"], groups.get("sample_stats"), var_names=free
            )
        learned = {
            **learned,
            "engine": plan.engine,
            "method": plan.method,
            "draws": plan.draws,
            "tune": plan.tune,
            "chains": plan.chains,
            "fit_diagnostics": FrameworkMMM._diagnostics_json(raw_diag),
        }
        self.log_summary(ctx, groups["posterior"], learned, fit_data)
        ctx.progress(1.0, "fit complete")
        return FitOutput(groups=groups, learned=learned, diagnostics=diagnostics)

    def log_summary(
        self,
        ctx: FitContext,
        posterior: xr.Dataset,
        learned: Mapping[str, Any],
        fit_data: xr.Dataset,
    ) -> None:
        """Run tracking: the shared MMM summary on the weighted total plus each channel's
        return on spend per outcome (labels ``channel``, ``outcome``)."""
        if not ctx.tracking:
            return
        fit = self._panel(learned, fit_data)
        win = tracking.summary_window(
            lambda p: MultivariateReplay(learned, Posterior.from_dataset(p)), posterior, fit
        )
        outcomes = [str(o) for o in learned["outcomes"]]
        tracking.log_mmm_summary(
            ctx,
            win,
            params={
                "mmm.framework_version": str(learned.get("mmm_framework", "")),
                "mmm.n_channels": len(fit.channels),
                "mmm.n_periods": int(fit.n_periods),
                "mmm.n_cells": 1,
                "mmm.link": "identity",
                "mmm.outcomes": ",".join(outcomes),
                "mmm.cross_effects": len(learned.get("cross_effects", [])),
            },
        )
        rp = win.replay
        assert isinstance(rp, MultivariateReplay)
        spend, _ = win.arrays()
        totals = rp.channel_totals_by_outcome(spend, np.arange(fit.n_periods), 1.0)  # (S,C,K)
        paid = spend.sum(axis=(0, 1))  # (C,)
        ch_lab = tracking.channel_labels(fit.channels)
        out_lab = tracking.channel_labels(outcomes)
        for c, ch in enumerate(fit.channels):
            if paid[c] <= 0:
                continue
            for k, name in enumerate(outcomes):
                ctx.log_metrics(
                    {"roi": float(totals[:, c, k].mean() / paid[c])},
                    labels={"channel": ch_lab[ch], "outcome": out_lab[name]},
                )

    # ------------------------------------------------------------------ replay
    @staticmethod
    def _panel(learned: Mapping[str, Any], fit_data: xr.Dataset | None) -> PanelData:
        if fit_data is None:
            raise MapiError("the bundle has no fit data", code=ErrorCode.INTEGRITY_FAILURE)
        return extended.national_panel(
            np.asarray(fit_data[TIME].values),
            [str(c) for c in learned["channels"]],
            np.asarray(fit_data[SPEND].transpose(TIME, CHANNEL).values, dtype=float),
            None,
        )

    def replay(
        self, fitted: Fitted, posterior: xr.Dataset | None = None
    ) -> tuple[MultivariateReplay, PanelData]:
        """The fitted model's forward pass over ``posterior`` (default: all draws)."""
        post = fitted.posterior if posterior is None else posterior
        cached = self._cache
        if cached is not None and cached[0] is fitted and cached[1] is post:
            return cached[2], cached[3]
        rp = MultivariateReplay(fitted.learned, Posterior.from_dataset(post))
        fit = self._panel(fitted.learned, fitted.group(FIT_DATA))
        self._cache = (fitted, post, rp, fit)
        return rp, fit

    def _window(self, fitted: Fitted, draw_index: np.ndarray | None) -> MultivariateFitWindow:
        post = draw_samples(fitted.posterior, draw_index)
        rp, fit = self.replay(fitted, post)
        return MultivariateFitWindow(rp, fit, outcomes=tuple(rp.outcomes))

    @staticmethod
    def _frequency(fitted: Fitted) -> str:
        freq = fitted.learned.get("frequency")
        if freq not in ("W", "D", "M"):
            raise MapiError(
                "the bundle does not record the data frequency", code=ErrorCode.INTEGRITY_FAILURE
            )
        return str(freq)

    # ------------------------------------------------------------------ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        rp, fit = self.replay(fitted)
        order = np.asarray(ds[TIME].values) if TIME in ds.coords else None
        work = ds.sortby(TIME) if order is not None else ds
        times, _, spend, _, _ = self._arrays(work, with_target=False, channels=fit.channels)
        spend = np.where(np.isfinite(spend), spend, 0.0)
        data = extended.national_panel(times, list(fit.channels), spend, None)
        pos = positions_of(fit.times, data.times, self._frequency(fitted))
        cells = rp.fit_cells()
        spend_full, _ = FrameworkMMM._history(fit, data, cells, pos)
        ctx.check_cancelled()
        oc = rp.evaluate_outcomes(spend_full, pos, seed=ctx.seed)
        for note in oc.notes:
            ctx.info(note)
        mu = oc.mu
        kind, noise = "expected", "none"
        if ctx.kind == "predictive":
            mu = mu + oc.noise(ctx.seed)
            kind, noise = "predictive", "crn_seeded"
        parts = [oc.channel, oc.baseline[:, None], oc.trend[:, None], oc.seasonality[:, None]]
        labels = [*fit.channels, BASELINE, TREND, SEASONALITY]
        contrib = np.concatenate(parts, axis=1)  # (S, n_comp, K, n)
        attrs = {"primary_outcome": rp.primary}
        y_hat = self._labelled(mu, fitted, rp, times, lead=[], lead_coords={})
        y_hat.attrs = {"kind": kind, "noise": noise, **attrs}
        contributions = self._labelled(
            contrib, fitted, rp, times, lead=[COMPONENT],
            lead_coords={COMPONENT: np.asarray(labels)},
        )  # fmt: skip
        contributions.attrs = {"kind": "expected", "noise": "none", **attrs}
        pred = xr.Dataset({"y_hat": y_hat, "contributions": contributions})
        if order is not None:
            pred = pred.sel({TIME: order})
        pred.attrs = {"units": "original", "owner": "mmm-framework", **attrs}
        return xr.DataTree.from_dict({PREDICTIONS: pred})

    @staticmethod
    def _labelled(
        values: np.ndarray,
        fitted: Fitted,
        rp: MultivariateReplay,
        times: np.ndarray,
        *,
        lead: list[str],
        lead_coords: dict[str, Any],
    ) -> xr.DataArray:
        """``(S, *lead, K, n)`` to ``(chain, draw, *lead, outcome, time)``."""
        post = fitted.posterior
        chains, draws = int(post.sizes["chain"]), int(post.sizes["draw"])
        arr = values.reshape(chains, draws, *values.shape[1:])
        return xr.DataArray(
            arr,
            dims=["chain", "draw", *lead, OUTCOME, TIME],
            coords={
                "chain": post["chain"].values,
                "draw": post["draw"].values,
                **lead_coords,
                OUTCOME: np.asarray(rp.outcomes),
                TIME: times,
            },
        )

    # ------------------------------------------------------------------ derived
    def derive(self, fitted: Fitted, name: str, ds: xr.Dataset, ctx: ScoreContext) -> xr.Dataset:
        """Derived quantities over the fit window (see :data:`DERIVED`)."""
        s = self.parse_settings(fitted.settings)
        to_original = getattr(ctx, "to_original", None)
        win = self._window(fitted, ctx.draw_index)
        if name == "contributions":
            return contributions_by_outcome(win, to_original)
        if name == "response_curves":
            return response_curves_by_outcome(win, s, to_original)
        if name == "marginal_cost":
            return marginal_cost_dataset(win, s.grid, to_original)
        if name == "adstock":
            return adstock_dataset(win)
        if name == "cross_effects":
            return cross_effects_dataset(win.mv, fitted.learned)
        raise MapiError(
            f"model {self.id!r} has no derived quantity {name!r}",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            pointer="/outputs/derived",
        )

    # ------------------------------------------------------------------ decisions
    def optimize(self, fitted: Fitted, request: OptimizeRequest, ctx: ScoreContext) -> BudgetPlan:
        """The budget plan for ``request`` on the weighted total of the outcomes, over at
        most ``request.max_draws`` evenly thinned posterior draws."""
        post = fitted.posterior
        n = int(post.sizes["chain"]) * int(post.sizes["draw"])
        idx = thin_index(n, request.max_draws)
        stacked = post.stack({"sample": ("chain", "draw")}, create_index=False)
        stacked = stacked.drop_vars(["chain", "draw"], errors="ignore").isel(sample=idx)
        rp, fit = self.replay(fitted, stacked.assign_coords(sample=np.arange(idx.size)))
        ctx.check_cancelled()
        win = MultivariateFitWindow(rp, fit, outcomes=tuple(rp.outcomes))
        plan = optimize_budget_plan(win, request, seed=ctx.seed)
        weights = dict(zip(rp.outcomes, map(float, rp.weights), strict=True))
        nonzero = [o for o, w in weights.items() if w > 0]
        # the plan's KPI is Σ_k weight_k × outcome_k; the runner keeps a label that is not "y"
        if len(nonzero) == 1:
            (only,) = nonzero
            if len(weights) == 1 and weights[only] == 1:
                return plan  # the target itself
            # one outcome: named like the response curves name it, with its weight
            w = weights[only]
            kpi = only
            note = f"The KPI is the outcome {only}" + ("" if w == 1 else f" × {w:g}") + "."
            if len(weights) > 1:
                note += " The other outcomes have weight 0."
        else:
            kpi = "weighted total of the outcomes"
            shown = ", ".join(f"{o} × {w:g}" for o, w in weights.items())
            note = f"The KPI is the weighted total of the outcomes: {shown}."
        plan.result = plan.result.model_copy(
            update={"kpi": kpi, "notes_plain": [note, *plan.result.notes_plain][:50]}
        )
        return plan

    def objective(self, fitted: Fitted, decision: xr.Dataset, ctx: ScoreContext) -> xr.DataArray:
        """Expected weighted total of the outcomes of a per-period budget at steady state,
        per posterior draw. ``decision["spend"]`` is spend per period over ``channel``
        (channels left out get zero); ``decision.attrs["periods"]`` (default 1) is how many
        periods the budget runs."""
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
                f"decision spend must be over channel only (national model), got "
                f"{list(map(str, da.dims))}",
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
            "units": "weighted total of the outcomes (model-stage units)",
            "periods": periods,
            "primary_outcome": rp.primary,
            "description": "expected weighted outcome total of the budget at steady state",
        }
        return out


# ======================================================================== derived


def contributions_by_outcome(
    win: MultivariateFitWindow, to_original: ToOriginal | None
) -> xr.Dataset:
    """``channel_contribution(sample, channel, outcome, time)`` and ``baseline``, ``trend``,
    ``seasonality`` over ``(sample, outcome, time)`` on the fit window; they sum to the
    expected outcomes."""
    rp = win.mv
    spend, _ = win.arrays()
    oc = rp.evaluate_outcomes(spend, np.arange(win.fit.n_periods))
    sample = np.arange(rp.n)
    coords = {SAMPLE: sample, OUTCOME: np.asarray(rp.outcomes), TIME: win.fit.times}
    out: dict[str, xr.DataArray] = {
        "channel_contribution": _convert(
            to_original,
            xr.DataArray(
                oc.channel,
                dims=[SAMPLE, CHANNEL, OUTCOME, TIME],
                coords={**coords, CHANNEL: np.asarray(rp.channels)},
            ),
            "y",
            win,
        )
    }
    for name, arr in ((BASELINE, oc.baseline), (TREND, oc.trend), (SEASONALITY, oc.seasonality)):
        out[name] = _convert(
            to_original, xr.DataArray(arr, dims=[SAMPLE, OUTCOME, TIME], coords=coords), "y", win
        )
    ds = xr.Dataset(out)
    ds.attrs = {
        "primary_outcome": rp.primary,
        "description": "Additive contributions per outcome over the fit window, with "
        "cross-effects passed through",
    }
    return ds


def response_curves_by_outcome(
    win: MultivariateFitWindow, s: MultivariateSettings, to_original: ToOriginal | None
) -> xr.Dataset:
    """The shared response-curve layout on the weighted total plus
    ``contribution_by_outcome(sample, channel, multiplier, outcome)`` and
    ``baseline_by_outcome(sample, outcome)``."""
    rp = win.mv
    ds = response_curves_dataset(win, s.grid, to_original)
    mults = np.asarray(ds["multiplier"].values, dtype=float)
    spend, _ = win.arrays()
    pos = np.arange(win.fit.n_periods)
    by = np.stack([rp.channel_totals_by_outcome(spend, pos, float(m)) for m in mults], axis=2)
    contrib = _convert(
        to_original,
        xr.DataArray(
            by,
            dims=[SAMPLE, CHANNEL, "multiplier", OUTCOME],
            coords={
                SAMPLE: np.arange(rp.n),
                CHANNEL: np.asarray(rp.channels),
                "multiplier": mults,
                OUTCOME: np.asarray(rp.outcomes),
            },
        ),
        "y",
        win,
    )
    contrib.attrs["description"] = "total contribution to each outcome over the fit window"
    oc = rp.evaluate_outcomes(spend, pos)
    base = (oc.baseline + oc.trend + oc.seasonality).sum(axis=2)  # (S, K)
    base_da = _convert(
        to_original,
        xr.DataArray(
            base,
            dims=[SAMPLE, OUTCOME],
            coords={SAMPLE: np.arange(rp.n), OUTCOME: np.asarray(rp.outcomes)},
        ),
        "y",
        win,
    )
    base_da.attrs["description"] = "outcome over the fit window that no channel explains"
    ds = ds.assign(contribution_by_outcome=contrib, baseline_by_outcome=base_da)
    weights = dict(zip(rp.outcomes, map(float, rp.weights), strict=True))
    nonzero = [o for o, w in weights.items() if w > 0]
    ds.attrs = {
        **ds.attrs,
        "kpi": "weighted total of the outcomes" if len(nonzero) > 1 else nonzero[0],
        "outcome_weights": json.dumps(weights),
        "primary_outcome": rp.primary,
    }
    return ds


def cross_effects_dataset(rp: MultivariateReplay, learned: Mapping[str, Any]) -> xr.Dataset:
    """``psi(sample, pair)`` per configured cross-effect (``pair`` = "source→target")."""
    pairs = list(learned.get("cross_effects", []))
    idx = {o: k for k, o in enumerate(rp.outcomes)}
    vals = np.stack(
        [rp.psi[:, idx[p["source"]], idx[p["target"]]] for p in pairs], axis=1
    ) if pairs else np.zeros((rp.n, 0))  # fmt: skip
    names = [f"{p['source']}{_PAIR_SEP}{p['target']}" for p in pairs]
    psi = xr.DataArray(
        vals,
        dims=[SAMPLE, "pair"],
        coords={
            SAMPLE: np.arange(rp.n),
            "pair": np.asarray(names, dtype=str),
            "source": ("pair", np.asarray([p["source"] for p in pairs], dtype=str)),
            "target": ("pair", np.asarray([p["target"] for p in pairs], dtype=str)),
            "kind": ("pair", np.asarray([p["kind"] for p in pairs], dtype=str)),
        },
    )
    psi.attrs = {"units": "target units per unit of source"}
    ds = xr.Dataset({"psi": psi})
    ds.attrs = {
        "units": "target units per unit of source",
        "primary_outcome": rp.primary,
        "description": "Effect of one outcome on another: the change in the target per "
        "unit of the source. On observed outcomes it is an association, not proof of cause.",
    }
    return ds
