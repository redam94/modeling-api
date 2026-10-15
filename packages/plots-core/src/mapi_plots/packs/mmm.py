"""Marketing-mix domain pack: response curves, marginal cost, budget plans, decomposition.

Plugins opt in by listing the ids in their manifest's ``plots`` (``mmm.response_curves`` ...).
Inputs follow the derived-quantity names the MMM plugins write:

* ``response_curves``: a response variable with sample dims, a ``channel`` dim and a numeric
  spend-grid dim (its coordinate is the spend level, or a ``spend`` variable over
  ``(channel, grid)`` gives per-channel levels); optional ``current_spend(channel)``;
* ``plans``: ``spend(plan, channel)`` and ``outcome(<sample dims>, plan)``;
* ``contributions``: as for ``core.decomposition``, split by ``channel``.
"""

from __future__ import annotations

from typing import Any, Literal

import altair as alt
import numpy as np
import pandas as pd
from mapi_spec.capabilities import Representation
from pydantic import Field

from mapi_plots.recipes import _stats
from mapi_plots.recipes._base import (
    Figure,
    Recipe,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    named,
    recipe,
)
from mapi_plots.recipes._util import (
    SAMPLE,
    coord_values,
    fmt,
    has_samples,
    pct,
    pick_var,
    stack_samples,
)
from mapi_plots.recipes.predictions import DecompositionSettings, _decomposition

__all__ = [
    "BudgetPlanSettings",
    "MarginalCostSettings",
    "ResponseCurveSettings",
    "budget_plans",
    "marginal_cost",
    "mmm_decomposition",
    "mmm_recipes",
    "response_curves",
]

CURVE_GROUPS = ("response_curves", "response_curve", "curves")


class ResponseCurveSettings(RecipeSettings):
    var: str | None = Field(None, description="Response variable (default: first)")
    channel: str = "channel"
    grid: str | None = Field(None, description="Spend-grid dim (default: the numeric dim)")
    spend_var: str = "spend"
    current_var: str = "current_spend"
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    facet: bool = Field(False, description="One panel per channel instead of colour")


class _Curves:
    """Response draws ``(sample, channel, grid)`` with per-channel spend levels."""

    def __init__(self, inp: RecipeInput, s: ResponseCurveSettings) -> None:
        ds = inp.require(*CURVE_GROUPS)
        names = [n for n in map(str, ds.data_vars) if n not in (s.spend_var, s.current_var)]
        if s.var is None:
            # the response is the variable over channel x grid, not a per-geo scale beside it
            names = [n for n in names if s.channel in ds[n].dims] or names
        self.var = pick_var(ds[names] if names else ds, s.var)
        da = ds[self.var]
        if s.channel not in da.dims:
            raise RecipeDataError(f"{self.var!r} has no {s.channel!r} dim")
        st = stack_samples(da) if has_samples(da) else da.expand_dims({SAMPLE: [0]})
        grid = s.grid or next((str(d) for d in st.dims if d not in (SAMPLE, s.channel)), None)
        if grid is None or grid not in st.dims:
            raise RecipeDataError("response curves need a spend-grid dim")
        extra = [d for d in st.dims if d not in (SAMPLE, s.channel, grid)]
        if extra:
            st = st.sum(extra)
        self.draws = st.transpose(SAMPLE, s.channel, grid)
        self.channels = [str(c) for c in coord_values(st, s.channel)]
        if s.spend_var in ds.data_vars:
            sp = ds[s.spend_var]
            if grid in sp.dims and s.channel not in sp.dims:
                sp = sp.expand_dims({s.channel: st[s.channel]})
            self.spend = np.asarray(sp.transpose(s.channel, grid).values, dtype=float)
        else:
            levels = np.asarray(coord_values(st, grid), dtype=float)
            self.spend = np.tile(levels, (len(self.channels), 1))
        self.current: np.ndarray | None = None
        if s.current_var in ds.data_vars:
            self.current = np.asarray(ds[s.current_var].transpose(s.channel).values, dtype=float)
        self.grid = grid
        self.channel_dim = s.channel

    def values(self) -> np.ndarray:
        return np.asarray(self.draws.values, dtype=float)  # (sample, channel, grid)


def _band_rows(
    channels: list[str], spend: np.ndarray, vals: np.ndarray, prob: float
) -> pd.DataFrame:
    parts = []
    for i, ch in enumerate(channels):
        x = vals[:, i, :]
        if x.shape[0] > 1:
            lo, hi = _stats.hdi(x, prob, axis=0)
        else:
            lo = hi = x[0]
        parts.append(
            pd.DataFrame(
                {
                    "channel": ch,
                    "spend": spend[i],
                    "point": np.nanmedian(x, axis=0),
                    "lo": lo,
                    "hi": hi,
                }
            )
        )
    return pd.concat(parts, ignore_index=True)


def _curve_chart(name: str, y_title: str, s: ResponseCurveSettings, current: bool) -> Any:
    base = alt.Chart()
    layers: list[Any] = [
        base.mark_area(opacity=0.2).encode(
            x=alt.X("spend:Q", title="spend"),
            y=alt.Y("lo:Q", title=y_title),
            y2="hi:Q",
            color=alt.Color("channel:N"),
        ),
        base.mark_line().encode(
            x="spend:Q",
            y="point:Q",
            color=alt.Color("channel:N"),
            tooltip=[
                "channel:N",
                alt.Tooltip("spend:Q", format=".3s"),
                alt.Tooltip("point:Q", format=".3s"),
            ],
        ),
    ]
    if current:
        # current spend is stored on every row of its channel, so one dataset serves all
        # layers and the chart can still be faceted
        layers.append(
            base.mark_point(filled=True, size=80, shape="diamond", color="#111").encode(
                x="mean(current_spend):Q",
                y="mean(current_value):Q",
                detail="channel:N",
                tooltip=["channel:N", alt.Tooltip("mean(current_spend):Q", format=".3s")],
            )
        )
    layer = alt.layer(*layers)
    if s.facet:
        return layer.properties(width=s.width // 2, height=s.height).facet(
            facet=alt.Facet("channel:N"), columns=3, data=named(name)
        )
    return alt.layer(*layers, data=named(name)).properties(width=s.width, height=s.height)


def _with_current(rows: pd.DataFrame, current: pd.DataFrame | None) -> pd.DataFrame:
    if current is None:
        return rows
    cur = current.rename(columns={"spend": "current_spend", "value": "current_value"})
    return rows.merge(cur, on="channel", how="left")


def _current_rows(c: _Curves, point_curves: pd.DataFrame, col: str) -> pd.DataFrame | None:
    if c.current is None:
        return None
    rows = []
    for i, ch in enumerate(c.channels):
        sub = point_curves[point_curves.channel == ch]
        rows.append(
            {
                "channel": ch,
                "spend": float(c.current[i]),
                "value": float(np.interp(c.current[i], sub["spend"], sub[col])),
            }
        )
    return pd.DataFrame(rows)


@recipe(
    "mmm.response_curves",
    title="Response curves",
    settings=ResponseCurveSettings,
    persona="both",
    requires_groups=["response_curves"],
    summary="Expected outcome for each level of spend per channel, with uncertainty.",
    tags=["mmm"],
)
def response_curves(inp: RecipeInput, s: ResponseCurveSettings) -> Figure:
    """Response per channel over a spend grid (median + HDI band), current spend marked."""
    c = _Curves(inp, s)
    curves = _band_rows(c.channels, c.spend, c.values(), s.hdi_prob)
    current = _current_rows(c, curves, "point")
    datasets = {"curves": _with_current(curves, current)}
    chart = _curve_chart("curves", c.var, s, current is not None)
    caption = (
        f"Each curve shows the expected {c.var} for a level of spend per channel, with the "
        f"{pct(s.hdi_prob)} interval shaded. Steeper curves return more per extra unit of "
        "spend; flattening curves mean the channel is saturating."
    )
    if current is not None and c.current is not None:
        sat = []
        for i, ch in enumerate(c.channels):
            sub = curves[curves.channel == ch]
            slope = np.gradient(sub["point"].to_numpy(), sub["spend"].to_numpy())
            here = float(np.interp(c.current[i], sub["spend"], slope))
            peak = float(np.nanmax(slope)) if np.isfinite(slope).any() else np.nan
            sat.append((here / peak if peak > 0 else np.nan, ch))
        valid = [x for x in sat if np.isfinite(x[0])]
        if valid:
            ratio, ch = min(valid)
            caption += (
                f" At current spend (diamonds), {ch} is closest to saturation: an extra unit "
                f"there returns {pct(ratio)} of what it returned at its steepest point."
            )
    return Figure(chart=chart, datasets=datasets, caption=caption)


class MarginalCostSettings(ResponseCurveSettings):
    metric: Literal["cost", "roi"] = Field(
        "cost", description="cost: spend per extra unit of outcome; roi: outcome per extra spend"
    )
    clip_quantile: float = Field(0.98, gt=0.5, le=1.0)


@recipe(
    "mmm.marginal_cost",
    title="Marginal cost",
    settings=MarginalCostSettings,
    persona="both",
    requires_groups=["response_curves"],
    summary="What the next unit of outcome costs (or returns) at each spend level per channel.",
    tags=["mmm"],
)
def marginal_cost(inp: RecipeInput, s: MarginalCostSettings) -> Figure:
    """Marginal cost (or marginal ROI) from the slope of each response-curve draw."""
    c = _Curves(inp, s)
    vals = c.values()
    slopes = np.empty_like(vals)
    for i in range(len(c.channels)):
        slopes[:, i, :] = np.gradient(vals[:, i, :], c.spend[i], axis=-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        metric = np.where(slopes > 0, 1.0 / slopes, np.nan) if s.metric == "cost" else slopes
    finite = metric[np.isfinite(metric)]
    if finite.size:
        cap = float(np.quantile(finite, s.clip_quantile))
        metric = np.where(np.isfinite(metric) & (metric > cap), cap, metric)
    rows = _band_rows(c.channels, c.spend, metric, s.hdi_prob)
    label = "cost per extra unit" if s.metric == "cost" else "return per extra unit spent"
    current = _current_rows(c, rows, "point")
    datasets = {"marginal": _with_current(rows, current)}
    chart = _curve_chart("marginal", label, s, current is not None)
    caption = (
        f"The {label} of {c.var} at each spend level per channel ({pct(s.hdi_prob)} interval "
        "shaded)."
    )
    if current is not None:
        best = (current.sort_values("value", ascending=s.metric == "cost")).iloc[0]
        caption += (
            f" At current spend, the {'cheapest' if s.metric == 'cost' else 'best-returning'} "
            f"next unit comes from {best.channel} ({fmt(float(best.value))})."
        )
    return Figure(chart=chart, datasets=datasets, caption=caption)


class BudgetPlanSettings(RecipeSettings):
    spend_var: str = "spend"
    outcome_var: str = "outcome"
    plan: str = "plan"
    channel: str = "channel"
    reference: str | None = Field(None, description="Plan to compare against (default: first)")
    hdi_prob: float = Field(0.9, gt=0, lt=1)


@recipe(
    "mmm.budget_plans",
    title="Budget plan comparison",
    settings=BudgetPlanSettings,
    persona="both",
    requires_groups=["plans"],
    requires_representation=[Representation.DRAWS],
    summary="Spend allocation and expected outcome of alternative budget plans.",
    tags=["mmm"],
)
def budget_plans(inp: RecipeInput, s: BudgetPlanSettings) -> Figure:
    """Allocation per plan, outcome intervals and the change against a reference plan."""
    ds = inp.require("plans", "budget_plans", "plan_comparison")
    for name in (s.spend_var, s.outcome_var):
        if name not in ds.data_vars:
            raise RecipeDataError(f"the plans group needs a {name!r} variable")
    spend = ds[s.spend_var]
    if s.plan not in spend.dims:
        raise RecipeDataError(f"{s.spend_var!r} needs a {s.plan!r} dim")
    alloc = spend.to_dataframe(name="spend").reset_index()
    alloc = alloc.rename(columns={s.plan: "plan", s.channel: "channel"})
    if "channel" not in alloc:
        alloc["channel"] = "total"
    alloc["plan"] = alloc["plan"].astype(str)
    alloc["channel"] = alloc["channel"].astype(str)
    alloc = alloc[["plan", "channel", "spend"]]
    out = stack_samples(ds[s.outcome_var])
    extra = [d for d in out.dims if d not in (SAMPLE, s.plan)]
    if extra:
        out = out.sum(extra)
    plans = [str(p) for p in coord_values(out, s.plan)]
    ref = s.reference or plans[0]
    if ref not in plans:
        raise RecipeDataError(f"unknown reference plan {ref!r}")
    mat = np.asarray(out.transpose(SAMPLE, s.plan).values, dtype=float)
    r = mat[:, plans.index(ref)]
    rows = []
    for j, p in enumerate(plans):
        x = mat[:, j]
        d = x - r
        lo, hi = _stats.hdi(x, s.hdi_prob)
        dlo, dhi = _stats.hdi(d, s.hdi_prob) if np.ptp(d) > 0 else (0.0, 0.0)
        rows.append(
            {
                "plan": p,
                "mean": float(x.mean()),
                "lo": float(lo),
                "hi": float(hi),
                "delta": float(d.mean()),
                "delta_lo": float(dlo),
                "delta_hi": float(dhi),
                "p_better": float(np.mean(d > 0)) if p != ref else np.nan,
                "total_spend": float(alloc.loc[alloc.plan == p, "spend"].sum()),
            }
        )
    summary = pd.DataFrame(rows)
    bars = (
        alt.Chart(named("allocation"))
        .mark_bar()
        .encode(
            x=alt.X("channel:N", title=None, axis=alt.Axis(labelAngle=0)),
            xOffset="plan:N",
            y=alt.Y("spend:Q", title="spend"),
            color=alt.Color("plan:N"),
            tooltip=["plan:N", "channel:N", alt.Tooltip("spend:Q", format=".3s")],
        )
        .properties(width=s.width, height=s.height, title="Allocation by channel")
    )
    obase = alt.Chart()
    outcome = alt.layer(
        obase.mark_rule().encode(
            x=alt.X(
                "lo:Q",
                title=f"{s.outcome_var} ({pct(s.hdi_prob)} interval)",
                scale=alt.Scale(zero=False),
            ),
            x2="hi:Q",
            y=alt.Y("plan:N", title=None),
            color=alt.Color("plan:N", legend=None),
        ),
        obase.mark_point(filled=True, size=70).encode(
            x="mean:Q",
            y="plan:N",
            color=alt.Color("plan:N", legend=None),
            tooltip=["plan:N", alt.Tooltip("mean:Q", format=".3s")],
        ),
        data=named("outcomes"),
    ).properties(width=s.width // 2, height=26 * len(summary), title="Expected outcome")
    delta = alt.layer(
        obase.mark_rule(color="#999").encode(x=alt.datum(0)),
        obase.mark_rule().encode(
            x=alt.X("delta_lo:Q", title=f"change vs {ref}"),
            x2="delta_hi:Q",
            y=alt.Y("plan:N", title=None),
        ),
        obase.mark_point(filled=True, size=70).encode(
            x="delta:Q",
            y="plan:N",
            tooltip=[
                "plan:N",
                alt.Tooltip("delta:Q", format=".3s"),
                alt.Tooltip("p_better:Q", format=".0%"),
            ],
        ),
        data=named("outcomes"),
    ).properties(width=s.width // 2, height=26 * len(summary), title=f"Change vs {ref}")
    chart = alt.vconcat(bars, alt.hconcat(outcome, delta))
    others = summary[summary.plan != ref]
    if len(others):
        best = others.sort_values("delta", ascending=False).iloc[0]
        ref_mean = float(summary.loc[summary.plan == ref, "mean"].iloc[0])
        rel = best.delta / ref_mean if ref_mean else np.nan
        caption = (
            f"{best.plan} is expected to deliver {fmt(best.delta)} "
            + (f"({pct(rel)}) " if np.isfinite(rel) else "")
            + f"{'more' if best.delta >= 0 else 'less'} {s.outcome_var} than {ref} "
            f"({pct(s.hdi_prob)} interval {fmt(best.delta_lo)} to {fmt(best.delta_hi)}); "
            f"probability it is better: {pct(best.p_better)}."
        )
    else:
        caption = f"Expected {s.outcome_var} of plan {ref}."
    datasets = {"allocation": alloc, "outcomes": summary}
    return Figure(chart=chart, datasets=datasets, caption=caption)


class MMMDecompositionSettings(DecompositionSettings):
    component: str | None = Field("channel", description="Component dim")


@recipe(
    "mmm.decomposition",
    title="Contribution by channel",
    settings=MMMDecompositionSettings,
    persona="both",
    requires_groups=["contributions"],
    summary="Baseline and channel contributions over time and in total.",
    tags=["mmm"],
)
def mmm_decomposition(inp: RecipeInput, s: MMMDecompositionSettings) -> Figure:
    """Marketing-mix decomposition: channels (plus baseline variables) over time."""
    ds = inp.require("contributions", "decomposition", "components")
    comp = (
        s.component
        if s.component and any(s.component in ds[v].dims for v in ds.data_vars)
        else None
    )
    return _decomposition(inp, s.model_copy(update={"component": comp}), "channel")


def mmm_recipes() -> list[Recipe[Any]]:
    """The MMM domain pack (entry point ``mapi.plots: mmm``)."""
    return [response_curves, marginal_cost, budget_plans, mmm_decomposition]
