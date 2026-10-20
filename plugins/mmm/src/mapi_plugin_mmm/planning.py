"""Budget optimisation with mmm-framework's ``planning`` module (``POST …:optimize``).

The fitted model's per-draw response curves - the total contribution of each channel (or
each geo x channel arm with ``by_geo``) when its spend over the fit window is scaled by a
multiplier - are computed with the model's own forward pass
(:class:`~mapi_plugin_mmm.forward.Replay`) and handed to mmm-framework as precomputed
``planning.budget.ResponseCurves``. Its optimiser (``optimize_budget``: greedy marginal
allocation, or the constrained multi-start solver for risk objectives, portfolio groups,
money bounds and ``mode="free"``) re-optimises per posterior draw, which turns parameter
uncertainty into decision uncertainty (stability, expected regret, the chance the plan
beats the current one); ``budget_frontier`` sweeps budgets for the efficient frontier.

The request maps one to one (see :mod:`mapi_spec.optimize`): ``ChannelBounds`` multiples
are ``bounds``, money bounds ``abs_bounds``, ``locked`` pins the multiplier at 1, ``groups``
are portfolio constraints. ``horizon`` rescales the fit-window curves to that many
periods (spend and response per period held at the fit window's average - a steady-state
plan). The expected KPI adds the baseline (the KPI no channel explains) to the media
contribution, per draw.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_sdk.models import BudgetPlan
from mapi_spec.calibration import DistributionSummary
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.optimize import (
    ChannelAllocation,
    ChannelBounds,
    FrontierPoint,
    OptimizeRequest,
    OptimizeResult,
    check_feasible,
)

from mapi_plugin_mmm.derived import FitWindow
from mapi_plugin_mmm.framework import quiet

__all__ = ["DEFAULT_GRID_MAX", "optimize_budget_plan"]

DEFAULT_GRID_MAX: Final = 2.5
_GRID_POINTS: Final = 41
_QUANTILES: Final = (0.05, 0.25, 0.5, 0.75, 0.95)
_SEP = " │ "


def _summary(values: np.ndarray) -> DistributionSummary:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if not v.size:
        return DistributionSummary(mean=0.0, sd=0.0, quantiles={})
    qs = np.quantile(v, _QUANTILES)
    return DistributionSummary(
        mean=float(v.mean()),
        sd=float(v.std()),
        quantiles={f"{q:g}": float(x) for q, x in zip(_QUANTILES, qs, strict=True)},
    )


def _bounds_for(req: OptimizeRequest, channel: str) -> ChannelBounds:
    return req.channels.get(channel, req.default_bounds)


def _grid(req: OptimizeRequest, base: dict[str, float]) -> np.ndarray:
    """Multipliers wide enough for every bound of the request (and 1, current spend)."""
    top = DEFAULT_GRID_MAX
    for ch, b in [(c, _bounds_for(req, c)) for c in base]:
        if b.max_multiplier is not None:
            top = max(top, b.max_multiplier)
        if b.max_spend is not None and base[ch] > 0:
            top = max(top, b.max_spend / base[ch])
        if b.min_spend is not None and base[ch] > 0:
            top = max(top, b.min_spend / base[ch])
    if req.mode == "fixed":
        total = sum(base.values())
        if req.total_budget is not None and total > 0:
            top = max(top, 1.05 * req.total_budget / total)
        if req.budget_change_pct is not None:
            top = max(top, 1.05 * (1 + req.budget_change_pct / 100))
    if req.frontier is not None and req.frontier.budget_max is not None:
        total = sum(base.values())
        if total > 0:
            top = max(top, 1.05 * req.frontier.budget_max / total)
    top = min(float(top), 100.0)
    grid = np.linspace(0.0, top, _GRID_POINTS)
    return np.unique(np.sort(np.append(grid, 1.0)))


def _interp(curves: np.ndarray, grid: np.ndarray, spend: np.ndarray) -> np.ndarray:
    """``(D, A)`` contribution of every arm at ``spend (A,)`` on piecewise-linear curves
    ``curves (D, A, G)`` over spend grids ``grid (A, G)``."""
    out = np.empty(curves.shape[:2])
    for a in range(curves.shape[1]):
        g = grid[a]
        if g[-1] <= 0:
            out[:, a] = curves[:, a, 0]
            continue
        x = float(spend[a])
        if x <= g[-1]:
            out[:, a] = [np.interp(x, g, curves[d, a]) for d in range(curves.shape[0])]
        else:  # past the grid: the last segment's slope (mmm-framework's convention)
            slope = (curves[:, a, -1] - curves[:, a, -2]) / max(g[-1] - g[-2], 1e-12)
            out[:, a] = curves[:, a, -1] + slope * (x - g[-1])
    return out


def optimize_budget_plan(win: FitWindow, req: OptimizeRequest, *, seed: int) -> BudgetPlan:
    """The plan for ``req`` from the fitted model's curves (see the module doc), with the
    current and optimal plans per draw for the ``mmm.budget_plans`` plot."""
    from mmm_framework.planning.budget import ResponseCurves, optimize_budget
    from mmm_framework.planning.frontier import budget_frontier

    fit = win.fit
    spend, _ = win.arrays()  # (T, n_cell, C)
    channels = list(fit.channels)
    by_geo = bool(req.by_geo)
    if by_geo and not fit.geos:
        raise MapiError(
            "by_geo needs a model fitted per geo (hierarchy.pool_over: [geo])",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            pointer="/by_geo",
        )
    periods = fit.n_periods
    factor = (req.horizon / periods) if req.horizon is not None else 1.0
    base_ch = {c: float(spend[:, :, k].sum()) * factor for k, c in enumerate(channels)}
    check_feasible(req, base_ch)
    grid = _grid(req, base_ch)
    # per-draw curves on the multiplier grid: (D, C, n_cell, G)
    totals = np.stack([win.channel_totals(float(m)) for m in grid], axis=-1) * factor
    n_draws = totals.shape[0]
    cells_per_geo = fit.n_products
    if by_geo:
        assert fit.geos is not None
        geo_tot = totals.reshape(n_draws, len(channels), fit.n_geos, cells_per_geo, -1).sum(axis=3)
        arms = [(g, c) for g in range(fit.n_geos) for c in range(len(channels))]
        curves = np.stack([geo_tot[:, c, g] for g, c in arms], axis=1)  # (D, A, G)
        sp_geo = spend.reshape(periods, fit.n_geos, cells_per_geo, len(channels)).sum(axis=2)
        base = np.array([sp_geo[:, g, c].sum() for g, c in arms]) * factor
        peak = np.array([sp_geo[:, g, c].max() for g, c in arms])
        n_obs: Any = np.full(len(arms), periods * factor)
        names = [f"{g}{_SEP}{channels[c]}" for g, c in arms]
    else:
        arms = [(None, c) for c in range(len(channels))]  # type: ignore[misc]
        curves = totals.sum(axis=2)  # (D, C, G)
        base = np.array([base_ch[c] for c in channels])
        peak = spend.max(axis=(0, 1))
        n_obs = periods * fit.n_cells * factor
        names = list(channels)
    rc = ResponseCurves(
        channel_names=names,
        multipliers=grid,
        base_spend=base,
        contributions=curves,
        obs_max_spend=peak,
        n_obs=n_obs,
    )
    bounds: dict[str, tuple[float, float]] = {}
    abs_bounds: dict[str, tuple[float, float]] = {}
    for a, (_g, c) in enumerate(arms):
        ch = channels[c]
        b = _bounds_for(req, ch)
        if b.locked:
            bounds[names[a]] = (1.0, 1.0)
            continue
        lo_m = b.min_multiplier if b.min_multiplier is not None else 0.0
        hi_m = b.max_multiplier if b.max_multiplier is not None else 2.0
        bounds[names[a]] = (float(lo_m), float(hi_m))
        if b.min_spend is not None or b.max_spend is not None:
            if by_geo:
                raise MapiError(
                    "money bounds are per channel; with by_geo give multipliers",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/channels/{ch}",
                )
            # money bounds are stated for the plan's horizon, like the budget itself
            lo_s = b.min_spend if b.min_spend is not None else lo_m * base[a]
            hi_s = b.max_spend if b.max_spend is not None else hi_m * base[a]
            abs_bounds[names[a]] = (float(lo_s), float(hi_s))
    groups: list[dict[str, Any]] = []
    for grp in req.groups:
        members = [n for a, n in enumerate(names) if channels[arms[a][1]] in grp.channels]
        groups.append(
            {
                "name": grp.name,
                "channels": members,
                "min_share": grp.min_share,
                "max_share": grp.max_share,
                "min_spend": grp.min_spend,
                "max_spend": grp.max_spend,
            }
        )
    total_budget = req.total_budget
    try:
        with quiet():
            res = optimize_budget(
                curves=rc,
                total_budget=total_budget,
                budget_change_pct=req.budget_change_pct,
                bounds=bounds,
                abs_bounds=abs_bounds or None,
                groups=groups or None,
                min_channel_spend=req.min_channel_spend,
                objective=req.objective,
                mode=req.mode,
                value_per_kpi=req.value_per_kpi,
                n_steps=req.n_steps,
                max_draws=req.max_draws,
                random_seed=req.seed if req.seed is not None else seed,
            )
            frontier = None
            if req.frontier is not None:
                frontier = budget_frontier(
                    curves=rc,
                    budget_min=req.frontier.budget_min,
                    budget_max=req.frontier.budget_max,
                    n_points=req.frontier.n_points,
                    objective=req.objective,
                    bounds=bounds,
                    abs_bounds=abs_bounds or None,
                    groups=groups or None,
                    min_channel_spend=req.min_channel_spend,
                    value_per_kpi=req.value_per_kpi,
                    random_seed=req.seed if req.seed is not None else seed,
                )
    except ValueError as exc:
        raise MapiError(
            "the budget cannot be allocated within the constraints",
            code=ErrorCode.OPTIMIZE_INFEASIBLE,
            untrusted_text=str(exc)[:2000],
        ) from exc
    optimal = np.asarray(res.optimal_alloc, dtype=float)
    spend_grid = rc.spend_grid
    plan_contrib = _interp(curves, spend_grid, optimal)  # (D, A)
    now_contrib = _interp(curves, spend_grid, base)
    baseline = _baseline(win) * factor  # (D,)
    expected = baseline + plan_contrib.sum(axis=1)
    current = baseline + now_contrib.sum(axis=1)
    uplift = expected - current
    per_draw = res.per_draw_alloc if res.per_draw_alloc is not None else optimal[None, :]
    total = float(res.total_budget)
    current_total = float(base.sum())
    table = res.table.reset_index(drop=True)
    allocation: list[ChannelAllocation] = []
    for a, (g, c) in enumerate(arms):
        row = table.iloc[a]
        move = np.sign(optimal[a] - base[a])
        stab = float(np.mean(np.sign(per_draw[:, a] - base[a]) == move)) if move != 0 else None
        allocation.append(
            ChannelAllocation(
                channel=channels[c],
                geo=(fit.geos[g] if by_geo and g is not None and fit.geos else None),
                current_spend=float(base[a]),
                optimal_spend=float(optimal[a]),
                current_share=float(base[a] / current_total) if current_total > 0 else 0.0,
                optimal_share=min(1.0, float(optimal[a] / total)) if total > 0 else 0.0,
                change_pct=(float((optimal[a] - base[a]) / base[a] * 100) if base[a] > 0 else None),
                expected_contribution=_summary(plan_contrib[:, a]),
                marginal_roi=(
                    float(res.marginal_roas[names[a]])
                    if res.marginal_roas and np.isfinite(res.marginal_roas[names[a]])
                    else None
                ),
                stability=stab,
                extrapolated=not bool(row.get("within_observed_range", True)),
            )
        )
    points: list[FrontierPoint] = []
    if frontier is not None:
        base_mean = float(baseline.mean())
        for p in frontier.points:
            alloc = {
                (
                    f"{fit.geos[arms[a][0]]}/{channels[arms[a][1]]}" if by_geo and fit.geos else n
                ): float(v)
                for a, (n, v) in enumerate(p.allocation.items())
            }
            points.append(
                FrontierPoint(
                    total_budget=float(p.total_budget),
                    expected_kpi=base_mean + float(p.expected_return),
                    kpi_p5=base_mean + float(p.return_p5),
                    kpi_p95=base_mean + float(p.return_p95),
                    marginal_roi=float(p.marginal_roi) if np.isfinite(p.marginal_roi) else None,
                    allocation=alloc,
                )
            )
    notes = [str(n)[:500] for n in [*res.notes, *(frontier.notes if frontier else [])]][:50]
    result = OptimizeResult(
        objective=req.objective,
        objective_label=str(res.objective_label)[:200],
        mode=req.mode,
        total_budget=total,
        current_total=current_total,
        horizon=req.horizon,
        kpi="y",
        allocation=allocation,
        expected_kpi=_summary(expected),
        current_kpi=_summary(current),
        uplift=_summary(uplift),
        prob_positive_uplift=float(np.mean(uplift > 0)),
        expected_regret=max(0.0, float(res.expected_regret)),
        shadow_price=(
            float(res.shadow_price)
            if res.shadow_price is not None and np.isfinite(res.shadow_price)
            else None
        ),
        frontier=points,
        n_draws=int(n_draws),
        notes_plain=notes,
    )
    spend = np.zeros((2, len(channels)))
    for a, (_g, c) in enumerate(arms):
        spend[0, c] += base[a]
        spend[1, c] += optimal[a]
    plans = xr.Dataset(
        {
            "spend": (("plan", "channel"), spend),
            "outcome": (("sample", "plan"), np.stack([current, expected], axis=1)),
        },
        coords={
            "plan": ["current", "optimal"],
            "channel": channels,
            "sample": np.arange(expected.size),
        },
    )
    plans["outcome"].attrs["description"] = "expected KPI of the plan, per posterior draw"
    return BudgetPlan(result=result, plans=plans)


def _baseline(win: FitWindow) -> np.ndarray:
    """``(D,)`` KPI over the fit window that no channel explains."""
    return np.asarray(win.baseline_cells().sum(axis=1))


def thin_index(n: int, k: int) -> np.ndarray:
    """``k`` evenly spaced indices into ``n`` draws (all of them when ``k >= n``)."""
    if k >= n:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, k).round().astype(int))


def labels(values: Sequence[Any]) -> list[str]:
    return [str(v) for v in values]
