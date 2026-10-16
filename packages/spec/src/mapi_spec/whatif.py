"""What-if scenarios, forecasts and counterfactuals for Guided mode (plan §11 tasks 6-7; M3).

**Sliders.** A :class:`WhatIfScenario` scales each channel's spend over the fit window.
The browser answers it instantly from the fit's :class:`~mapi_spec.views.ResponseGridView`
with the interpolation contract below; :func:`evaluate_scenario` is the reference
implementation (the web app's TypeScript port is tested against the same golden cases):

1. a channel without a multiplier keeps ``1``; multipliers outside the grid
   (``[multipliers[0], multipliers[-1]]``) are refused (``SCENARIO_OUT_OF_RANGE``) - the
   grid is never extrapolated;
2. per draw ``d`` and channel ``c``, the contribution at multiplier ``x`` is the *linear*
   interpolation of ``contribution[d][c]`` between the two grid points around ``x``; spend
   is interpolated the same way on ``spend[c]``;
3. per draw, ``kpi = baseline + sum_c contribution`` and ``delta = kpi - kpi(all at 1)``;
   summaries (mean and quantiles) are taken over draws *after* summing, so uncertainty
   stays joint across channels.

**Run exact** turns the scenario into a ``counterfactual`` :class:`~mapi_spec.spec.ScoreRequest`
(:func:`scenario_score_request`). Forecasts (:func:`forecast_request`) and intervention
presets ("channel off", "budget x k"; :func:`counterfactual_request`) build score requests
the same way, so the server, the CLI and the docs share one implementation
(``POST /v1/guided/requests:compile``).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from typing import Annotated, Final, Literal

from pydantic import ConfigDict, Field, RootModel, model_validator

from mapi_spec._dto import RequestModel, ResponseModel, dto_examples
from mapi_spec._types import Name
from mapi_spec.capabilities import OutputSelector
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.refs import DatasetRef, ModelTarget
from mapi_spec.spec import API_VERSION, FutureCovariates, Horizon, Intervention, ScoreRequest
from mapi_spec.views import ResponseGridView

__all__ = [
    "DEFAULT_GUIDED_OUTPUT",
    "DEFAULT_QUANTILES",
    "BudgetScale",
    "ChannelOff",
    "ChannelOutcome",
    "CounterfactualCompile",
    "ForecastCompile",
    "GuidedCompile",
    "GuidedCompileRequest",
    "InterventionPreset",
    "SummaryStats",
    "WhatIfCompile",
    "WhatIfResult",
    "WhatIfScenario",
    "compile_guided_request",
    "counterfactual_request",
    "evaluate_scenario",
    "forecast_request",
    "preset_interventions",
    "scenario_score_request",
]

DEFAULT_QUANTILES: Final = (0.05, 0.5, 0.95)
DEFAULT_GUIDED_OUTPUT: Final = OutputSelector(kind="expected", summary=["mean", "hdi_90"])
"""Q2: expected values, mean plus 90% HDI, no draws."""


class WhatIfScenario(RequestModel):
    """Spend multipliers per channel over the fit window (``1`` = what was actually spent).

    ``geo`` restricts the scenario to one geo's grid (needs a per-geo grid).
    """

    multipliers: dict[str, float] = Field(default_factory=dict, max_length=64)
    geo: str | None = Field(None, max_length=128)
    label: str | None = Field(None, max_length=200)

    model_config = dto_examples(
        {"multipliers": {"tv": 0.8, "search": 1.3}, "label": "Shift 20% of TV to search"}
    )

    @model_validator(mode="after")
    def _finite(self) -> WhatIfScenario:
        for ch, m in self.multipliers.items():
            if not math.isfinite(m) or m < 0:
                raise ValueError(f"multiplier of {ch!r} must be a finite number >= 0")
        return self


class SummaryStats(ResponseModel):
    """Mean and quantiles over posterior draws (``quantiles[i]`` at ``levels[i]``)."""

    mean: float
    levels: list[float]
    quantiles: list[float]

    model_config = dto_examples(
        {"mean": 10.0, "levels": [0.05, 0.5, 0.95], "quantiles": [8, 10, 12]}
    )


class ChannelOutcome(ResponseModel):
    multiplier: float
    spend: float
    contribution: SummaryStats


class WhatIfResult(ResponseModel):
    """The scenario's outcome over the fit window, in the grid's units."""

    channels: dict[str, ChannelOutcome]
    total_spend: float
    spend_delta: float
    kpi: SummaryStats | None = Field(None, description="Needs a baseline in the grid")
    kpi_delta: SummaryStats
    roi_delta: SummaryStats | None = Field(
        None, description="kpi_delta / spend_delta per draw (None when spend is unchanged)"
    )
    draws: int

    model_config = dto_examples(
        {
            "channels": {
                "tv": {
                    "multiplier": 0.8,
                    "spend": 800.0,
                    "contribution": {"mean": 90.0, "levels": [0.05, 0.95], "quantiles": [70, 110]},
                }
            },
            "total_spend": 800.0,
            "spend_delta": -200.0,
            "kpi_delta": {"mean": -20.0, "levels": [0.05, 0.95], "quantiles": [-30.0, -11.0]},
            "roi_delta": {"mean": 0.1, "levels": [0.05, 0.95], "quantiles": [0.055, 0.15]},
            "draws": 300,
        }
    )


def _interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    i = bisect.bisect_left(xs, x)
    if i < len(xs) and xs[i] == x:
        return ys[i]
    lo, hi = i - 1, i
    t = (x - xs[lo]) / (xs[hi] - xs[lo])
    return ys[lo] + t * (ys[hi] - ys[lo])


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (numpy's default, ``method="linear"``)."""
    n = len(sorted_values)
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, n - 1)
    return sorted_values[lo] + (pos - lo) * (sorted_values[hi] - sorted_values[lo])


def _summary(values: Sequence[float], levels: Sequence[float]) -> SummaryStats:
    s = sorted(values)
    return SummaryStats(
        mean=math.fsum(values) / len(values),
        levels=list(levels),
        quantiles=[_quantile(s, q) for q in levels],
    )


def evaluate_scenario(
    grid: ResponseGridView,
    scenario: WhatIfScenario,
    *,
    levels: Sequence[float] = DEFAULT_QUANTILES,
) -> WhatIfResult:
    """Answer ``scenario`` from ``grid`` (the reference interpolation; see module doc)."""
    if unknown := sorted(set(scenario.multipliers) - set(grid.channels)):
        raise MapiError(
            f"unknown channels {unknown}; the grid has {grid.channels}",
            code=ErrorCode.SCENARIO_OUT_OF_RANGE,
            pointer="/multipliers",
        )
    xs = grid.multipliers
    lo, hi = xs[0], xs[-1]
    for ch, m in scenario.multipliers.items():
        if not lo <= m <= hi:
            raise MapiError(
                f"the multiplier of {ch!r} ({m}) is outside the precomputed range "
                f"[{lo}, {hi}]; use 'Run exact' for it",
                code=ErrorCode.SCENARIO_OUT_OF_RANGE,
                pointer=f"/multipliers/{ch}",
            )
    if scenario.geo is not None:
        if not grid.geos or scenario.geo not in grid.geos or grid.contribution_by_geo is None:
            raise MapiError(
                f"the grid has no per-geo curves for {scenario.geo!r}",
                code=ErrorCode.SCENARIO_OUT_OF_RANGE,
                pointer="/geo",
            )
        g = grid.geos.index(scenario.geo)
        contrib = [
            [[cell[g] for cell in row] for row in per_c] for per_c in grid.contribution_by_geo
        ]
        spend = grid.spend_by_geo[g] if grid.spend_by_geo is not None else grid.spend
        baseline = None  # the baseline is not split by geo
    else:
        contrib, spend, baseline = grid.contribution, grid.spend, grid.baseline
    one = next(i for i, x in enumerate(xs) if abs(x - 1.0) < 1e-9)
    mult = [scenario.multipliers.get(ch, 1.0) for ch in grid.channels]
    new_spend = [_interp(xs, spend[c], m) for c, m in enumerate(mult)]
    cur_spend = [spend[c][one] for c in range(len(grid.channels))]
    per_channel: list[list[float]] = [[] for _ in grid.channels]
    kpi_delta: list[float] = []
    kpi: list[float] = []
    for d in range(grid.draws):
        total_new = total_cur = 0.0
        for c, m in enumerate(mult):
            v = _interp(xs, contrib[d][c], m)
            per_channel[c].append(v)
            total_new += v
            total_cur += contrib[d][c][one]
        kpi_delta.append(total_new - total_cur)
        if baseline is not None:
            kpi.append(baseline[d] + total_new)
    spend_delta = math.fsum(new_spend) - math.fsum(cur_spend)
    roi = (
        _summary([k / spend_delta for k in kpi_delta], levels) if abs(spend_delta) > 1e-12 else None
    )
    return WhatIfResult(
        channels={
            ch: ChannelOutcome(
                multiplier=mult[c],
                spend=new_spend[c],
                contribution=_summary(per_channel[c], levels),
            )
            for c, ch in enumerate(grid.channels)
        },
        total_spend=math.fsum(new_spend),
        spend_delta=spend_delta,
        kpi=_summary(kpi, levels) if kpi else None,
        kpi_delta=_summary(kpi_delta, levels),
        roi_delta=roi,
        draws=grid.draws,
    )


# ====================================================================== score request helpers


def _request(
    model: ModelTarget | str,
    *,
    mode: Literal["counterfactual", "forecast"],
    interventions: list[Intervention],
    horizon: Horizon | None = None,
    scenario: DatasetRef | str | None = None,
    future: FutureCovariates = FutureCovariates.SUPPLIED,
    output: OutputSelector | None = None,
    inputs_stage: str | None = None,
) -> ScoreRequest:
    body: dict[str, object] = {
        "apiVersion": API_VERSION,
        "kind": "ScoreRequest",
        "model": str(model),
        "mode": mode,
        "interventions": [i.model_dump(by_alias=True, exclude_none=True) for i in interventions],
        "output": (output or DEFAULT_GUIDED_OUTPUT).model_dump(exclude_defaults=True),
    }
    if future is not FutureCovariates.SUPPLIED:
        body["future_covariates"] = future.value
    if horizon is not None:
        body["horizon"] = horizon.model_dump()
    if scenario is not None:
        body["scenario"] = str(scenario)
    if inputs_stage is not None:
        body["inputs_stage"] = inputs_stage
    return ScoreRequest.model_validate(body)


def scenario_score_request(
    scenario: WhatIfScenario,
    model: ModelTarget | str,
    *,
    spend_var: str = "spend",
    channel_dim: str = "channel",
    geo_dim: str = "geo",
    output: OutputSelector | None = None,
) -> ScoreRequest:
    """ "Run exact": the scenario as a ``counterfactual`` score over the fit window (one
    ``multiply`` intervention per changed channel, on the dataset's spend variable)."""
    sel_geo: dict[str, str] = {geo_dim: scenario.geo} if scenario.geo is not None else {}
    interventions = [
        Intervention.model_validate(
            {"var": spend_var, "sel": {channel_dim: ch, **sel_geo}, "multiply": m}
        )
        for ch, m in sorted(scenario.multipliers.items())
        if m != 1.0
    ]
    if not interventions:
        raise MapiError(
            "the scenario changes nothing (every multiplier is 1)",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/multipliers",
        )
    return _request(model, mode="counterfactual", interventions=interventions, output=output)


class ChannelOff(RequestModel):
    """Intervention preset: what if this channel had spent nothing?"""

    kind: Literal["channel_off"] = "channel_off"
    channel: str = Field(..., min_length=1, max_length=128)


class BudgetScale(RequestModel):
    """Intervention preset: scale the budget by ``k`` (all channels, or the listed ones)."""

    kind: Literal["budget_scale"] = "budget_scale"
    k: float = Field(..., ge=0, le=100)
    channels: list[str] | None = Field(None, min_length=1, max_length=64)


InterventionPreset = Annotated[ChannelOff | BudgetScale, Field(discriminator="kind")]


def preset_interventions(
    preset: ChannelOff | BudgetScale, *, spend_var: str = "spend", channel_dim: str = "channel"
) -> list[Intervention]:
    """The interventions of a preset (on the dataset's spend variable)."""
    if isinstance(preset, ChannelOff):
        return [
            Intervention.model_validate(
                {"var": spend_var, "sel": {channel_dim: preset.channel}, "set": 0.0}
            )
        ]
    sel: dict[str, object] = {channel_dim: list(preset.channels)} if preset.channels else {}
    return [Intervention.model_validate({"var": spend_var, "sel": sel, "multiply": preset.k})]


def counterfactual_request(
    preset: ChannelOff | BudgetScale,
    model: ModelTarget | str,
    *,
    spend_var: str = "spend",
    channel_dim: str = "channel",
    horizon: Horizon | None = None,
    scenario: DatasetRef | str | None = None,
    future: FutureCovariates = FutureCovariates.SUPPLIED,
    output: OutputSelector | None = None,
) -> ScoreRequest:
    """A counterfactual over the fit window - or, with a ``horizon``, a forecast with the
    preset applied to the future."""
    interventions = preset_interventions(preset, spend_var=spend_var, channel_dim=channel_dim)
    if horizon is None:
        return _request(model, mode="counterfactual", interventions=interventions, output=output)
    return _request(
        model,
        mode="forecast",
        interventions=interventions,
        horizon=horizon,
        scenario=scenario,
        future=future,
        output=output,
    )


def forecast_request(
    model: ModelTarget | str,
    *,
    periods: int,
    dim: str = "time",
    future: FutureCovariates = FutureCovariates.CARRY_LAST,
    scenario: DatasetRef | str | None = None,
    output: OutputSelector | None = None,
) -> ScoreRequest:
    """A guided forecast ``periods`` ahead along ``dim``. Guided forecasts default to
    carrying the last values of future covariates forward; ``supplied`` needs ``scenario``."""
    if future is FutureCovariates.SUPPLIED and scenario is None:
        raise MapiError(
            "future covariates 'supplied' needs a scenario dataset with their future values",
            code=ErrorCode.MISSING_FUTURE_COVARIATE,
            pointer="/scenario",
        )
    return _request(
        model,
        mode="forecast",
        interventions=[],
        horizon=Horizon(dim=dim, periods=periods),
        scenario=scenario,
        future=future,
        output=output,
    )


# ======================================================================= compile endpoint body


class _CompileBase(RequestModel):
    model: ModelTarget
    spend_var: Name = "spend"
    channel_dim: Name = "channel"
    output: OutputSelector | None = None


class WhatIfCompile(_CompileBase):
    kind: Literal["whatif"] = "whatif"
    scenario: WhatIfScenario


class ForecastCompile(_CompileBase):
    kind: Literal["forecast"] = "forecast"
    periods: int = Field(..., ge=1, le=10_000)
    dim: Name = "time"
    future_covariates: FutureCovariates = FutureCovariates.CARRY_LAST
    scenario: DatasetRef | None = None
    preset: InterventionPreset | None = None


class CounterfactualCompile(_CompileBase):
    kind: Literal["counterfactual"] = "counterfactual"
    preset: InterventionPreset


GuidedCompileRequest = Annotated[
    WhatIfCompile | ForecastCompile | CounterfactualCompile, Field(discriminator="kind")
]


class GuidedCompile(RootModel[GuidedCompileRequest]):
    """Body of ``POST /v1/guided/requests:compile``: one of ``whatif``, ``forecast`` or
    ``counterfactual`` (by ``kind``); the response is the equivalent ``ScoreRequest``."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "kind": "whatif",
                    "model": "model://growth/eu-geo-mmm@v3",
                    "scenario": {"multipliers": {"tv": 0.8, "search": 1.3}},
                },
                {
                    "kind": "forecast",
                    "model": "endpoint://growth/geo-mmm-prod",
                    "periods": 13,
                    "preset": {"kind": "budget_scale", "k": 1.1},
                },
                {
                    "kind": "counterfactual",
                    "model": "endpoint://growth/geo-mmm-prod",
                    "preset": {"kind": "channel_off", "channel": "tv"},
                },
            ]
        },
    )


def compile_guided_request(
    req: WhatIfCompile | ForecastCompile | CounterfactualCompile,
) -> ScoreRequest:
    """The :class:`~mapi_spec.spec.ScoreRequest` a guided request stands for."""
    if isinstance(req, WhatIfCompile):
        return scenario_score_request(
            req.scenario,
            req.model,
            spend_var=req.spend_var,
            channel_dim=req.channel_dim,
            output=req.output,
        )
    if isinstance(req, CounterfactualCompile):
        return counterfactual_request(
            req.preset,
            req.model,
            spend_var=req.spend_var,
            channel_dim=req.channel_dim,
            output=req.output,
        )
    if req.preset is not None:
        return counterfactual_request(
            req.preset,
            req.model,
            spend_var=req.spend_var,
            channel_dim=req.channel_dim,
            horizon=Horizon(dim=req.dim, periods=req.periods),
            scenario=req.scenario,
            future=req.future_covariates,
            output=req.output,
        )
    return forecast_request(
        req.model,
        periods=req.periods,
        dim=req.dim,
        future=req.future_covariates,
        scenario=req.scenario,
        output=req.output,
    )
