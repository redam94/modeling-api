"""Settings and fit schemas of the mmm-framework plugins.

Every field carries ``x-ui-level`` (``basic`` or ``advanced``): Guided mode shows the basic
fields only, Expert mode shows everything (plan §11). Settings are frozen and reject
unknown keys; the JSON Schema exported in the manifest is what the control plane validates
specs against, so a wrong value is refused before any sandbox starts.

The settings speak the platform's vocabulary (``adstock``, ``saturation``, ``trend``,
``seasonality``, ``hierarchy``); :mod:`mapi_plugin_mmm.framework` maps them onto
mmm-framework's ``MFFConfig``/``ModelConfig``/``TrendConfig`` one to one:

==============================  ================================================================
setting                         mmm-framework
==============================  ================================================================
``adstock.kind/l_max/normalize`` ``MediaChannelConfig.adstock`` (``AdstockConfig``)
``saturation.kind``             ``MediaChannelConfig.saturation`` (``SaturationConfig``)
``channels.<c>.*``              the same, per channel
``trend.kind`` ...              ``TrendConfig`` (none, linear, piecewise, spline, gp)
``seasonality.*_order``         ``SeasonalityConfig.yearly/monthly/weekly``
``hierarchy.pool_over``         the panel dims (``Geography``/``Product``) and
                                ``HierarchicalConfig.pool_across_geo/product``
``hierarchy.media_by_geo``      ``HierarchicalConfig.vary_media_by_geo``
``media_prior``                 ``ModelConfig.media_prior_mode``
``priors.roi.<c>``              ``MediaChannelConfig.roi_prior_mu/roi_prior_sigma``
``priors.intercept``            ``ModelConfig.intercept_prior_mu/intercept_prior_sigma``
``likelihood``                  ``ModelConfig.likelihood.family``
==============================  ================================================================
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from mapi_sdk.models import ModelSettings
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AdstockKind",
    "AdstockSettings",
    "BayesianSettings",
    "ChannelSettings",
    "FrequentistSettings",
    "GridSettings",
    "HierarchySettings",
    "NormalPrior",
    "PanelDim",
    "PriorSettings",
    "RoiPrior",
    "SaturationKind",
    "SaturationSettings",
    "SeasonalitySettings",
    "TrendKind",
    "TrendSettings",
    "mcmc_fit_schema",
]

BASIC: dict[str, Any] = {"x-ui-level": "basic"}
ADVANCED: dict[str, Any] = {"x-ui-level": "advanced"}

_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
_LABEL_MAX = 128

AdstockKind = Literal["geometric", "delayed", "weibull", "none"]
SaturationKind = Literal["logistic", "hill", "michaelis_menten", "tanh", "root", "none"]
TrendKind = Literal["none", "linear", "piecewise", "spline", "gp"]
PanelDim = Literal["geo", "product"]
Frequency = Literal["W", "D", "M"]


class _Sub(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdstockSettings(_Sub):
    """Carry-over of spend into later periods (mmm-framework ``AdstockConfig``)."""

    kind: AdstockKind = Field(
        "geometric",
        description="geometric: decays from the spend period; delayed: peaks a few periods "
        "later; weibull: a flexible shape; none: no carry-over",
        json_schema_extra=BASIC,
    )
    l_max: int = Field(
        8,
        ge=1,
        le=52,
        description="How many periods an effect can last (the kernel length)",
        json_schema_extra=BASIC,
    )
    normalize: bool = Field(
        True,
        description="Normalise the lag weights to sum to one (the effect size stays in the "
        "channel coefficient)",
        json_schema_extra=ADVANCED,
    )


class SaturationSettings(_Sub):
    """Diminishing returns of (carried-over) spend (mmm-framework ``SaturationConfig``)."""

    kind: SaturationKind = Field(
        "logistic",
        description="logistic (default, most stable), hill (S-shaped), michaelis_menten, "
        "tanh, root (power curve) or none (a straight line)",
        json_schema_extra=BASIC,
    )


class ChannelSettings(_Sub):
    """Per-channel overrides of the shared adstock and saturation settings."""

    adstock: AdstockSettings | None = Field(None, json_schema_extra=ADVANCED)
    saturation: SaturationSettings | None = Field(None, json_schema_extra=ADVANCED)


class TrendSettings(_Sub):
    """The slow-moving baseline (mmm-framework ``TrendConfig``)."""

    kind: TrendKind = Field(
        "linear",
        description="none, linear (default), piecewise (Prophet-style changepoints), spline "
        "or gp (a smooth Gaussian-process curve). Forecasts hold flexible trends at their "
        "last fitted level; a linear trend is extrapolated.",
        json_schema_extra=BASIC,
    )
    n_changepoints: int = Field(10, ge=1, le=50, json_schema_extra=ADVANCED)
    changepoint_range: float = Field(0.8, gt=0, le=1, json_schema_extra=ADVANCED)
    n_knots: int = Field(10, ge=3, le=50, json_schema_extra=ADVANCED)
    growth_prior_sigma: float = Field(
        0.5,
        gt=0,
        le=10,
        description="Prior sd of the linear slope, in KPI standard deviations over the "
        "whole history",
        json_schema_extra=ADVANCED,
    )


class SeasonalitySettings(_Sub):
    """Fourier seasonality (mmm-framework ``SeasonalityConfig``)."""

    yearly_order: int | None = Field(
        2,
        ge=0,
        le=26,
        description="Fourier pairs of the yearly cycle (0 or null: none)",
        json_schema_extra=BASIC,
    )
    monthly_order: int | None = Field(None, ge=0, le=10, json_schema_extra=ADVANCED)
    weekly_order: int | None = Field(
        None, ge=0, le=3, description="Daily data only", json_schema_extra=ADVANCED
    )
    prior_sigma: float = Field(
        0.3,
        gt=0,
        le=5,
        description="Prior sd of each Fourier coefficient, in KPI standard deviations "
        "(raise it for strongly seasonal categories)",
        json_schema_extra=ADVANCED,
    )


class HierarchySettings(_Sub):
    """Panel structure: the dims the KPI varies over besides time.

    ``pool_over`` lists the panel dims of the data (``geo`` and/or ``product``). Every
    level gets its own baseline, partially pooled towards the overall level
    (``HierarchicalConfig.pool_across_geo/product``); with ``media_by_geo`` each geo also
    gets its own channel effects, partially pooled across geos.
    """

    pool_over: list[PanelDim] = Field(
        default_factory=list,
        max_length=2,
        description="Panel dims of the data (geo, product); empty for one national series",
        json_schema_extra=BASIC,
    )
    media_by_geo: bool = Field(
        False,
        description="Let channel effects differ by geo (partially pooled)",
        json_schema_extra=ADVANCED,
    )
    media_geo_sigma: float = Field(
        0.3,
        gt=0,
        le=5,
        description="Prior sd of the geo deviations of log channel effects",
        json_schema_extra=ADVANCED,
    )

    @field_validator("pool_over")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("pool_over lists each dim once")
        # canonical order: geo before product (mmm-framework's panel order)
        return sorted(v, key=("geo", "product").index)


class RoiPrior(_Sub):
    """A log-normal prior on a channel's return on spend (KPI per unit of spend over the
    fit window): ``roi ~ LogNormal(mu, sigma)``, so ``exp(mu)`` is the median ROI."""

    mu: float = Field(..., ge=-20, le=20, description="Log of the median ROI")
    sigma: float = Field(..., gt=0, le=10, description="Spread on the log scale")

    @property
    def median(self) -> float:
        return math.exp(self.mu)


class NormalPrior(_Sub):
    """``Normal(mu, sigma)`` on the standardised KPI scale (KPI standard deviations)."""

    mu: float = Field(..., ge=-50, le=50)
    sigma: float = Field(..., gt=0, le=50)


class PriorSettings(_Sub):
    """Prior overrides; business-prior translation (``translate_priors``) writes them.

    * ``roi`` - per channel, a log-normal prior on the return on spend (mmm-framework's
      ROI-parameterised channel effect, ``roi_prior_mu``/``roi_prior_sigma``);
    * ``roi_default`` - the log-normal ROI prior of channels not listed (default
      ``LogNormal(0, 1)``: median break-even);
    * ``intercept`` - the baseline level in KPI standard deviations from the mean.
    """

    roi: dict[str, RoiPrior] = Field(
        default_factory=dict,
        max_length=64,
        description="Per-channel log-normal ROI priors (channel label -> {mu, sigma})",
        json_schema_extra=ADVANCED,
    )
    roi_default: RoiPrior | None = Field(None, json_schema_extra=ADVANCED)
    intercept: NormalPrior | None = Field(None, json_schema_extra=ADVANCED)

    @field_validator("roi")
    @classmethod
    def _labels(cls, v: dict[str, RoiPrior]) -> dict[str, RoiPrior]:
        bad = [k for k in v if not k or len(k) > _LABEL_MAX]
        if bad:
            raise ValueError(f"invalid channel labels {bad}")
        return v


class GridSettings(_Sub):
    """Response-curve grids cached per thinned draw (Guided-mode sliders, optimisation)."""

    max_multiplier: float = Field(
        2.5,
        ge=1,
        le=20,
        description="Largest spend multiplier on the grid (1 = spend over the fit window, "
        "which the grid always holds)",
        json_schema_extra=ADVANCED,
    )
    points: int = Field(
        26,
        ge=3,
        le=201,
        description="Grid points from 0 to max_multiplier (plus 1, today's spend, when the "
        "spacing skips it)",
        json_schema_extra=ADVANCED,
    )
    marginal_step: float = Field(
        0.01,
        gt=0,
        le=0.25,
        description="Relative spend change used for marginal cost (central difference)",
        json_schema_extra=ADVANCED,
    )

    def multipliers(self) -> list[float]:
        """``points`` evenly spaced multipliers from 0 to ``max_multiplier``, with ``1.0``
        (today's spend, which the what-if grid and the scorecard read) inserted when the
        spacing skips it."""
        step = self.max_multiplier / (self.points - 1)
        grid = [round(i * step, 12) for i in range(self.points)]
        return grid if 1.0 in grid else sorted([*grid, 1.0])


def _check_controls(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    bad = [n for n in v if not re.match(_NAME, n)]
    if bad:
        raise ValueError(f"invalid control names {bad}")
    if len(set(v)) != len(v):
        raise ValueError("controls must be unique")
    if {"y", "spend"} & set(v):
        raise ValueError("the target and spend cannot be controls")
    return v


class _Common(ModelSettings):
    """Settings shared by the Bayesian and frequentist models."""

    adstock: AdstockSettings = Field(AdstockSettings(), json_schema_extra=BASIC)
    saturation: SaturationSettings = Field(SaturationSettings(), json_schema_extra=BASIC)
    channels: dict[str, ChannelSettings] = Field(
        default_factory=dict,
        max_length=64,
        description="Per-channel adstock/saturation overrides (channel label -> settings)",
        json_schema_extra=ADVANCED,
    )
    trend: TrendSettings = Field(TrendSettings(), json_schema_extra=BASIC)
    seasonality: SeasonalitySettings = Field(SeasonalitySettings(), json_schema_extra=BASIC)
    hierarchy: HierarchySettings = Field(HierarchySettings(), json_schema_extra=BASIC)
    controls: list[str] | None = Field(
        None,
        max_length=64,
        description="Control variables (default: every other numeric variable over time)",
        json_schema_extra=ADVANCED,
    )
    frequency: Frequency | None = Field(
        None,
        description="Data frequency: W (weekly), D (daily) or M (monthly); default: read "
        "from the time labels",
        json_schema_extra=ADVANCED,
    )
    new_levels: Literal["pooled", "hyperprior", "zero", "error"] = Field(
        "pooled",
        description="How a geo or product unseen at fit is scored: pooled (the overall "
        "level and average channel effects), hyperprior (a level drawn from the fitted "
        "spread, so its range is wider), zero (no channel effects) or error (refuse)",
        json_schema_extra=ADVANCED,
    )
    grid: GridSettings = Field(GridSettings(), json_schema_extra=ADVANCED)

    @field_validator("controls")
    @classmethod
    def _controls(cls, v: list[str] | None) -> list[str] | None:
        return _check_controls(v)

    @property
    def panel_dims(self) -> list[str]:
        """The panel dims of the data besides time (``geo``, ``product``)."""
        return list(self.hierarchy.pool_over)

    def adstock_for(self, channel: str) -> AdstockSettings:
        over = self.channels.get(channel)
        return over.adstock if over is not None and over.adstock is not None else self.adstock

    def saturation_for(self, channel: str) -> SaturationSettings:
        over = self.channels.get(channel)
        if over is not None and over.saturation is not None:
            return over.saturation
        return self.saturation


class BayesianSettings(_Common):
    """Settings of ``mmm.bayesian`` (mmm-framework ``BayesianMMM``).

    Canonical names (bind dataset names to these in ``data.bind``): dims ``time`` and
    ``channel`` plus the panel dims listed in ``hierarchy.pool_over`` (``geo``,
    ``product``); target ``y(*panel, time)``; spend ``spend(*panel, channel, time)``;
    controls ``<name>(*panel?, time)``. The model owns scaling and carry-over, so the
    pipeline should only convert currencies and resample (no scaling, no lag tensors).
    """

    media_prior: Literal["roi", "coefficient"] = Field(
        "roi",
        description="Parameterise channel effects by their return on spend (priors are "
        "stated in ROI, comparable across channels) or by a standardised coefficient",
        json_schema_extra=ADVANCED,
    )
    priors: PriorSettings = Field(
        PriorSettings(),
        description="Prior overrides (written by business-prior translation)",
        json_schema_extra=ADVANCED,
    )
    likelihood: Literal["normal", "student_t"] = Field(
        "normal",
        description="Observation noise: normal, or student_t for heavy-tailed outliers",
        json_schema_extra=ADVANCED,
    )

    @model_validator(mode="after")
    def _check(self) -> BayesianSettings:
        if self.hierarchy.media_by_geo and "geo" not in self.hierarchy.pool_over:
            raise ValueError("hierarchy.media_by_geo needs 'geo' in hierarchy.pool_over")
        return self


class FrequentistSettings(_Common):
    """Settings of ``mmm.frequentist`` (mmm-framework's ridge estimator with bootstrap
    intervals). Same data contract as ``mmm.bayesian``; carry-over and saturation shapes
    are chosen by an out-of-sample search over ``search_budget`` candidates."""

    estimator: Literal["ridge", "constrained"] = Field(
        "ridge",
        description="ridge (closed form, non-negative channel effects) or constrained "
        "(a convex program; needs the optional cvxpy dependency)",
        json_schema_extra=ADVANCED,
    )
    bootstrap: int = Field(
        200,
        ge=20,
        le=5000,
        description="Bootstrap replicates (the 'draws' of every output)",
        json_schema_extra=BASIC,
    )
    search_budget: int = Field(
        200,
        ge=10,
        le=5000,
        description="Adstock/saturation candidates evaluated by the transform search",
        json_schema_extra=ADVANCED,
    )
    ridge_alpha: float = Field(
        1.0,
        gt=0,
        le=1e6,
        description="Fallback L2 penalty on the standardised design",
        json_schema_extra=ADVANCED,
    )

    @model_validator(mode="after")
    def _check(self) -> FrequentistSettings:
        if self.trend.kind == "gp":
            raise ValueError("the frequentist estimator cannot fit a gp trend (not linear)")
        if self.hierarchy.media_by_geo:
            raise ValueError("the frequentist estimator cannot fit per-geo channel effects")
        return self


def mcmc_fit_schema(*, engines: list[str], default_engine: str) -> dict[str, Any]:
    """JSON Schema for ``model.fit`` of an MCMC plugin (limits + ``x-ui-level``)."""
    return {
        "type": "object",
        "properties": {
            "mode": {
                "enum": ["evaluate_then_refit", "train_only"],
                "default": "evaluate_then_refit",
                "x-ui-level": "advanced",
            },
            "engine": {"enum": engines, "default": default_engine, "x-ui-level": "advanced"},
            "tune": {"type": "integer", "minimum": 0, "maximum": 20000, "x-ui-level": "basic"},
            "draws": {"type": "integer", "minimum": 1, "maximum": 20000, "x-ui-level": "basic"},
            "chains": {"type": "integer", "minimum": 1, "maximum": 16, "x-ui-level": "basic"},
            "target_accept": {
                "type": "number",
                "exclusiveMinimum": 0.5,
                "exclusiveMaximum": 1,
                "x-ui-level": "advanced",
            },
            "seed": {"type": "integer", "minimum": 0, "x-ui-level": "advanced"},
            "prior_predictive": {"type": "object", "x-ui-level": "advanced"},
            "keep": {"type": "object", "x-ui-level": "advanced"},
            "checkpoint": {"type": "object", "x-ui-level": "advanced"},
        },
    }
