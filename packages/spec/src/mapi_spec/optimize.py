"""Budget optimisation: ``POST /v1/models/{model_id}/versions/{version}:optimize`` (plan
§8.2; M3.5), backed by mmm-framework's ``planning`` module in the sandbox.

An :class:`OptimizeRequest` asks how to split a budget across channels (and optionally
geos): a total budget (or a change in percent of current spend, or ``mode="free"`` to fund
every channel up to break-even), bounds per channel as multiples of current spend or in
money, portfolio groups ("digital at least 40%"), the objective (the expected KPI, a
cautious percentile such as ``p10``, or the mean of the worst draws ``cvar5``), a horizon
and an optional efficient frontier. The ``optimize`` run answers with an
:class:`OptimizeResult`: the allocation per channel [and geo] with the expected KPI and its
interval, the uplift over the current plan with the chance it is positive, the expected
regret of committing to one plan, the marginal return of every channel at the plan, and
frontier points. Everything is computed from posterior draws in the sandbox; the control
plane validates only the JSON.

Mapping onto mmm-framework (``planning.budget.optimize_budget``, ``planning.frontier
.budget_frontier``, ``optimize_budget_by_geo``): ``total_budget`` / ``budget_change_pct`` /
``mode`` / ``objective`` / ``value_per_kpi`` map one to one; ``ChannelBounds.min_multiplier``
/ ``max_multiplier`` are its ``bounds`` and ``min_spend`` / ``max_spend`` its
``abs_bounds``; ``groups`` its portfolio constraints; ``max_draws``, ``n_steps`` and
``seed`` its sampling controls.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from mapi_spec._dto import RequestModel, ResponseModel, dto_examples
from mapi_spec._types import Name, StrictModel
from mapi_spec.calibration import DistributionSummary
from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "OPTIMIZE_OUTPUT",
    "ChannelAllocation",
    "ChannelBounds",
    "FrontierPoint",
    "FrontierRequest",
    "OptimizeJob",
    "OptimizeRequest",
    "OptimizeResult",
    "SpendGroup",
    "check_feasible",
]

OPTIMIZE_OUTPUT: Final = "optimize.json"
"""What an ``optimize`` run writes: the :class:`OptimizeResult`."""

_OBJECTIVE_RE: Final = re.compile(r"^(?:mean|p(?:[1-9]|[1-4][0-9])|cvar(?:[1-9]|[1-4][0-9]))$")


class ChannelBounds(StrictModel):
    """Bounds on one channel: as multiples of its current spend and/or in money (money
    bounds override multiples, as in mmm-framework)."""

    min_multiplier: float | None = Field(None, ge=0, le=100)
    max_multiplier: float | None = Field(None, ge=0, le=100)
    min_spend: float | None = Field(None, ge=0)
    max_spend: float | None = Field(None, ge=0)
    locked: bool = Field(False, description="Keep the channel at its current spend")

    @model_validator(mode="after")
    def _order(self) -> ChannelBounds:
        if (
            self.min_multiplier is not None
            and self.max_multiplier is not None
            and self.min_multiplier > self.max_multiplier
        ):
            raise ValueError("min_multiplier must not exceed max_multiplier")
        if (
            self.min_spend is not None
            and self.max_spend is not None
            and self.min_spend > self.max_spend
        ):
            raise ValueError("min_spend must not exceed max_spend")
        return self


class SpendGroup(StrictModel):
    """A portfolio constraint over several channels (shares of the total and/or money)."""

    name: str = Field(..., min_length=1, max_length=64)
    channels: list[Name] = Field(..., min_length=1, max_length=64)
    min_share: float | None = Field(None, ge=0, le=1)
    max_share: float | None = Field(None, ge=0, le=1)
    min_spend: float | None = Field(None, ge=0)
    max_spend: float | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _some(self) -> SpendGroup:
        if all(x is None for x in (self.min_share, self.max_share, self.min_spend,
                                   self.max_spend)):  # fmt: skip
            raise ValueError("a group needs at least one bound")
        if (self.min_share or 0) > (self.max_share if self.max_share is not None else 1):
            raise ValueError("min_share must not exceed max_share")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("channels must be unique")
        return self


class FrontierRequest(StrictModel):
    """Also sweep budgets from ``budget_min`` to ``budget_max`` (default 40%-200% of the
    current total) in ``n_points`` steps."""

    budget_min: float | None = Field(None, ge=0)
    budget_max: float | None = Field(None, gt=0)
    n_points: int = Field(12, ge=3, le=50)

    @model_validator(mode="after")
    def _range(self) -> FrontierRequest:
        if (
            self.budget_min is not None
            and self.budget_max is not None
            and self.budget_min >= self.budget_max
        ):
            raise ValueError("budget_min must be below budget_max")
        return self


class OptimizeRequest(RequestModel):
    """``POST …:optimize``: how should the budget be split?

    ``objective``: ``mean`` (expected KPI), ``p<q>`` (the q-th percentile across posterior
    draws, a cautious plan) or ``cvar<a>`` (mean of the worst a% of draws). ``mode``:
    ``fixed`` spends the budget; ``free`` funds each channel to break-even (marginal value
    = 1 per unit of money, with ``value_per_kpi`` converting KPI to money) and makes the
    total an output. ``horizon`` is the number of future periods the plan covers (default:
    the fit window's length, as the response grid).
    """

    total_budget: float | None = Field(None, gt=0)
    budget_change_pct: float | None = Field(None, gt=-100, le=1000)
    mode: Literal["fixed", "free"] = "fixed"
    channels: dict[Name, ChannelBounds] = Field(default_factory=dict, max_length=64)
    default_bounds: ChannelBounds = Field(
        default_factory=lambda: ChannelBounds(min_multiplier=0.0, max_multiplier=2.0),
        description="Bounds of channels not listed in channels",
    )
    groups: list[SpendGroup] = Field(default_factory=list, max_length=16)
    min_channel_spend: float | None = Field(
        None, ge=0, description="Keep every channel funded at least this much"
    )
    objective: str = Field("mean", examples=["mean", "p10", "cvar5"])
    value_per_kpi: float | None = Field(None, gt=0)
    horizon: int | None = Field(None, ge=1, le=520)
    by_geo: bool = Field(False, description="Allocate per (geo, channel) jointly")
    frontier: FrontierRequest | None = None
    max_draws: int = Field(200, ge=20, le=2000)
    n_steps: int = Field(400, ge=50, le=5000)
    seed: int | None = Field(None, ge=0, lt=2**32)

    model_config = dto_examples(
        {
            "total_budget": 1_000_000,
            "channels": {"tv": {"min_multiplier": 0.5, "max_multiplier": 1.5},
                         "search": {"min_spend": 50_000}},
            "groups": [{"name": "digital", "channels": ["search", "social"],
                        "min_share": 0.4}],
            "objective": "p10",
            "frontier": {"n_points": 10},
        },
        {"budget_change_pct": -10, "objective": "mean"},
        {"mode": "free", "value_per_kpi": 1.0},
    )  # fmt: skip

    @field_validator("objective")
    @classmethod
    def _objective(cls, v: str) -> str:
        if not _OBJECTIVE_RE.match(v):
            raise ValueError("objective is 'mean', 'p<1-49>' or 'cvar<1-49>'")
        return v

    @model_validator(mode="after")
    def _budget(self) -> OptimizeRequest:
        if self.total_budget is not None and self.budget_change_pct is not None:
            raise ValueError("give total_budget or budget_change_pct, not both")
        if self.mode == "free" and (
            self.total_budget is not None or self.budget_change_pct is not None
        ):
            raise ValueError("mode 'free' chooses the total itself: drop the budget")
        if len({g.name for g in self.groups}) != len(self.groups):
            raise ValueError("group names must be unique")
        return self


def check_feasible(
    req: OptimizeRequest, current: dict[str, float], *, total: float | None = None
) -> None:
    """Static feasibility from the current spend per channel (``OPTIMIZE_INFEASIBLE``):
    unknown channels, bounds whose minimums exceed the budget or whose maximums cannot
    reach it, and group shares that cannot be met together."""
    unknown = sorted(set(req.channels) - set(current))
    for g in req.groups:
        unknown += sorted(set(g.channels) - set(current))
    if unknown:
        raise MapiError(
            f"unknown channels: {sorted(set(unknown))}", code=ErrorCode.OPTIMIZE_INFEASIBLE,
            pointer="/channels",
        )  # fmt: skip
    if req.mode == "free":
        return
    base = sum(current.values())
    budget = total if total is not None else (
        req.total_budget if req.total_budget is not None
        else base * (1 + (req.budget_change_pct or 0) / 100)
    )  # fmt: skip
    lo = hi = 0.0
    for ch, spend in current.items():
        b = req.channels.get(ch, req.default_bounds)
        if b.locked:
            lo += spend
            hi += spend
            continue
        ch_lo = b.min_spend if b.min_spend is not None else (b.min_multiplier or 0.0) * spend
        if req.min_channel_spend is not None:
            ch_lo = max(ch_lo, req.min_channel_spend)
        mult = b.max_multiplier if b.max_multiplier is not None else 2.0
        ch_hi = b.max_spend if b.max_spend is not None else mult * spend
        lo += ch_lo
        hi += max(ch_hi, ch_lo)
    if lo > budget * (1 + 1e-9):
        raise MapiError(
            f"the channel minimums add up to {lo:,.0f}, more than the budget {budget:,.0f}",
            code=ErrorCode.OPTIMIZE_INFEASIBLE,
        )
    if hi < budget * (1 - 1e-9):
        raise MapiError(
            f"the channel maximums add up to {hi:,.0f}, less than the budget {budget:,.0f}",
            code=ErrorCode.OPTIMIZE_INFEASIBLE,
        )
    if sum(g.min_share or 0 for g in req.groups if len(g.channels) == len(set(g.channels))) > 1:
        overlap = any(
            set(a.channels) & set(b.channels)
            for i, a in enumerate(req.groups)
            for b in req.groups[i + 1 :]
        )
        if not overlap:
            raise MapiError(
                "the groups' minimum shares add up to more than 100%",
                code=ErrorCode.OPTIMIZE_INFEASIBLE,
                pointer="/groups",
            )


class ChannelAllocation(ResponseModel):
    """The plan for one channel (and geo): spend now and at the optimum, shares, the
    expected contribution, the marginal return at the plan and how stable the
    recommendation is across draws."""

    channel: str
    geo: str | None = None
    current_spend: float = Field(..., ge=0)
    optimal_spend: float = Field(..., ge=0)
    current_share: float = Field(..., ge=0, le=1)
    optimal_share: float = Field(..., ge=0, le=1)
    change_pct: float | None = None
    expected_contribution: DistributionSummary | None = None
    marginal_roi: float | None = Field(None, description="Next unit of money's KPI return")
    stability: float | None = Field(
        None, ge=0, le=1, description="Share of draws whose own optimum moves the same way"
    )
    extrapolated: bool = Field(False, description="Beyond the spend range seen in the data")


class FrontierPoint(ResponseModel):
    """The best plan at one total budget."""

    total_budget: float = Field(..., ge=0)
    expected_kpi: float
    kpi_p5: float | None = None
    kpi_p95: float | None = None
    marginal_roi: float | None = None
    allocation: dict[str, float] = Field(default_factory=dict)


class OptimizeResult(ResponseModel):
    """``optimize.json``: the recommended plan and its uncertainty."""

    format: Literal["mapi.optimize/v1"] = "mapi.optimize/v1"
    objective: str
    objective_label: str = Field(..., max_length=200)
    mode: Literal["fixed", "free"] = "fixed"
    total_budget: float = Field(..., ge=0)
    current_total: float = Field(..., ge=0)
    horizon: int | None = Field(None, ge=1)
    kpi: str = Field(..., max_length=128)
    kpi_units: str | None = Field(None, max_length=64)
    spend_units: str | None = Field(None, max_length=64)
    allocation: list[ChannelAllocation] = Field(..., min_length=1, max_length=64 * 256)
    expected_kpi: DistributionSummary
    current_kpi: DistributionSummary | None = None
    uplift: DistributionSummary | None = Field(None, description="Plan minus current plan")
    prob_positive_uplift: float | None = Field(None, ge=0, le=1)
    expected_regret: float | None = Field(None, ge=0)
    shadow_price: float | None = Field(None, description="KPI value of the next unit of money")
    frontier: list[FrontierPoint] = Field(default_factory=list, max_length=50)
    n_draws: int = Field(..., ge=1)
    notes_plain: list[str] = Field(default_factory=list, max_length=50)
    computed_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "objective": "p10",
            "objective_label": "cautious plan (10th percentile of the KPI)",
            "total_budget": 1000000,
            "current_total": 1000000,
            "kpi": "revenue",
            "kpi_units": "EUR",
            "spend_units": "EUR",
            "allocation": [
                {"channel": "tv", "current_spend": 600000, "optimal_spend": 450000,
                 "current_share": 0.6, "optimal_share": 0.45, "change_pct": -25,
                 "marginal_roi": 1.4, "stability": 0.82},
                {"channel": "search", "current_spend": 400000, "optimal_spend": 550000,
                 "current_share": 0.4, "optimal_share": 0.55, "change_pct": 37.5,
                 "marginal_roi": 1.4, "stability": 0.9},
            ],
            "expected_kpi": {"mean": 3.4e6, "sd": 2.1e5,
                             "quantiles": {"0.05": 3.06e6, "0.95": 3.75e6}},
            "uplift": {"mean": 1.2e5, "sd": 6.0e4, "quantiles": {"0.05": 2.5e4, "0.95": 2.2e5}},
            "prob_positive_uplift": 0.97,
            "expected_regret": 8.0e3,
            "n_draws": 200,
        }
    )  # fmt: skip

    @model_validator(mode="after")
    def _shares(self) -> OptimizeResult:
        total = sum(a.optimal_spend for a in self.allocation)
        if (
            self.mode == "fixed"
            and self.total_budget > 0
            and abs(total - self.total_budget) / self.total_budget > 0.005
        ):
            raise ValueError("the allocation must add up to the total budget (within 0.5%)")
        return self


class OptimizeJob(StrictModel):
    """``RunSpec.spec`` of an ``optimize`` run (inputs: ``bundle``, ``bundle.manifest``)."""

    request: OptimizeRequest
    model: str = Field(..., max_length=512, description="model://project/name@vN")
