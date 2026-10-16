"""Budget optimisation through mmm-framework planning (the ``optimize`` hook)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from mapi_plugin_mmm.derived import FitWindow
from mapi_plugin_mmm.planning import _grid, _interp
from mapi_sdk.models import ScoreContext
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.optimize import OptimizeRequest, OptimizeResult


def _plan(pf: Any, **kw: Any) -> OptimizeResult:
    req = OptimizeRequest.model_validate({"max_draws": 20, "n_steps": 200, **kw})
    out = pf.plugin.optimize(pf.fitted, req, ScoreContext(seed=3))
    return OptimizeResult.model_validate(out.model_dump(mode="json"))


def _current(res: OptimizeResult) -> float:
    return sum(a.current_spend for a in res.allocation)


def test_a_reallocation_spends_the_current_budget(geo_prior_fit: Any) -> None:
    res = _plan(geo_prior_fit)
    total = sum(a.optimal_spend for a in res.allocation)
    assert total == pytest.approx(res.current_total, rel=0.005)
    assert res.total_budget == pytest.approx(res.current_total, rel=1e-9)
    assert res.n_draws == 20 and res.mode == "fixed"
    assert {a.channel for a in res.allocation} == {"tv", "search"}
    assert res.expected_kpi.mean > 0 and res.uplift is not None
    assert 0.0 <= (res.prob_positive_uplift or 0.0) <= 1.0
    for a in res.allocation:
        assert 0 <= a.optimal_spend <= 2.0 * a.current_spend * (1 + 1e-6)
        assert a.expected_contribution is not None


def test_the_optimum_beats_every_other_split(national_prior_fit: Any) -> None:
    """Reference check: on the mean curves, no split of the budget between the two
    channels does better than the optimiser's (brute force over 401 splits)."""
    pf = national_prior_fit
    res = _plan(pf, objective="mean")
    post = pf.fitted.posterior
    n = int(post.sizes["chain"]) * int(post.sizes["draw"])
    idx = np.unique(np.linspace(0, n - 1, 20).round().astype(int))
    stacked = post.stack(sample=("chain", "draw"), create_index=False).drop_vars(
        ["chain", "draw"], errors="ignore"
    )
    rp, fit = pf.plugin.replay(
        pf.fitted, stacked.isel(sample=idx).assign_coords(sample=np.arange(idx.size))
    )
    win = FitWindow(rp, fit)
    base = np.array([a.current_spend for a in res.allocation])
    req = OptimizeRequest()
    grid = _grid(req, dict(zip(fit.channels, base, strict=True)))
    curves = np.stack([win.channel_totals(float(m)).sum(axis=2) for m in grid], axis=-1)
    spend_grid = base[:, None] * grid[None, :]
    mean_curve = curves.mean(axis=0)[None]
    budget = base.sum()

    def value(alloc: np.ndarray) -> float:
        return float(_interp(mean_curve, spend_grid, alloc).sum())

    best = max(
        value(np.array([x, budget - x]))
        for x in np.linspace(0, budget, 401)
        if x <= 2 * base[0] and budget - x <= 2 * base[1]
    )
    got = value(np.array([a.optimal_spend for a in res.allocation]))
    assert got >= best * (1 - 2e-3)


def test_locked_channels_and_budget_changes(geo_prior_fit: Any) -> None:
    res = _plan(geo_prior_fit, channels={"tv": {"locked": True}}, budget_change_pct=10)
    tv = next(a for a in res.allocation if a.channel == "tv")
    assert tv.optimal_spend == pytest.approx(tv.current_spend, rel=1e-6)
    assert res.total_budget == pytest.approx(1.1 * _current(res), rel=1e-6)
    assert sum(a.optimal_spend for a in res.allocation) == pytest.approx(
        res.total_budget, rel=0.005
    )


def test_groups_money_bounds_and_risk_objectives(geo_prior_fit: Any) -> None:
    res = _plan(
        geo_prior_fit,
        objective="p10",
        groups=[{"name": "digital", "channels": ["search"], "min_share": 0.6}],
    )
    search = next(a for a in res.allocation if a.channel == "search")
    assert search.optimal_spend >= 0.6 * res.total_budget * (1 - 0.01)
    assert "P10" in res.objective_label
    base = _plan(geo_prior_fit)
    cap = 0.5 * next(a for a in base.allocation if a.channel == "tv").current_spend
    capped = _plan(geo_prior_fit, channels={"tv": {"max_spend": cap}}, objective="cvar5")
    tv = next(a for a in capped.allocation if a.channel == "tv")
    assert tv.optimal_spend <= cap * (1 + 1e-6)


def test_the_frontier_rises_with_the_budget(geo_prior_fit: Any) -> None:
    res = _plan(geo_prior_fit, frontier={"n_points": 5})
    assert len(res.frontier) == 5
    kpis = [p.expected_kpi for p in res.frontier]
    assert kpis == sorted(kpis)
    budgets = [p.total_budget for p in res.frontier]
    assert budgets[0] == pytest.approx(0.4 * res.current_total, rel=1e-6)
    assert budgets[-1] == pytest.approx(2.0 * res.current_total, rel=1e-6)


def test_free_mode_chooses_the_total(geo_prior_fit: Any) -> None:
    res = _plan(geo_prior_fit, mode="free", value_per_kpi=1.0)
    assert res.mode == "free"
    assert res.total_budget == pytest.approx(sum(a.optimal_spend for a in res.allocation), rel=1e-6)


def test_by_geo_allocates_every_geo_channel_arm(geo_prior_fit: Any) -> None:
    res = _plan(geo_prior_fit, by_geo=True)
    assert len(res.allocation) == 3 * 2
    assert {a.geo for a in res.allocation} == {"north", "south", "west"}
    assert sum(a.optimal_spend for a in res.allocation) == pytest.approx(
        res.total_budget, rel=0.005
    )
    with pytest.raises(MapiError) as err:
        _plan(geo_prior_fit, by_geo=True, channels={"tv": {"max_spend": 10.0}})
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_by_geo_needs_geos(national_prior_fit: Any) -> None:
    with pytest.raises(MapiError) as err:
        _plan(national_prior_fit, by_geo=True)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_horizon_rescales_the_plan(geo_prior_fit: Any) -> None:
    full = _plan(geo_prior_fit)
    half = _plan(geo_prior_fit, horizon=26)
    assert half.current_total == pytest.approx(full.current_total / 2, rel=1e-9)
    assert half.horizon == 26


def test_infeasible_requests(geo_prior_fit: Any) -> None:
    current = _plan(geo_prior_fit).current_total
    with pytest.raises(MapiError) as err:
        _plan(geo_prior_fit, total_budget=10 * current)
    assert err.value.code is ErrorCode.OPTIMIZE_INFEASIBLE
    with pytest.raises(MapiError) as err:
        _plan(geo_prior_fit, channels={"radio": {"locked": True}})
    assert err.value.code is ErrorCode.OPTIMIZE_INFEASIBLE
