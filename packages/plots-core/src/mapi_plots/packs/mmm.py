"""Marketing-mix domain pack: response curves, marginal cost, budget plans, decomposition.

Plugins opt in by listing the ids in their manifest's ``plots`` (``mmm.response_curves`` ...).
Inputs follow the derived-quantity names the MMM plugins write:

* ``response_curves``: a response variable with sample dims, a ``channel`` dim and a numeric
  spend-grid dim (its coordinate is the spend level, or a ``spend`` variable over
  ``(channel, grid)`` gives per-channel levels); optional ``current_spend(channel)``;
* ``plans``: ``spend(plan, channel)`` and ``outcome(<sample dims>, plan)``;
* ``contributions``: as for ``core.decomposition``, split by ``channel`` (one level of an
  ``outcome`` dim: the ``outcome`` setting, else ``attrs["primary_outcome"]``, else the first);
* ``adstock``: ``weight(sample, channel, lag)`` and optional ``mean_lag(sample, channel)``;
* ``effectiveness``: ``effectiveness(sample, channel, time)`` and ``time_varying(channel)``;
* ``lift_tests``: ``observed(test)``, ``observed_sd(test)``, ``model(chain, draw, test)`` with
  ``channel``/``estimand``/``start``/``end``/``geo`` coords on ``test``;
* ``mediation``: ``direct(sample, channel)``, ``indirect(sample, channel, mediator)``,
  ``total(sample, channel)``, ``share_mediated(sample, channel)``;
* ``cross_effects``: ``psi(sample, pair)`` with ``source``/``target``/``kind`` coords on ``pair``.

Sample dims are ``sample`` or ``chain``/``draw``; a variable without them is one draw.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.capabilities import Representation
from pydantic import Field

from mapi_plots.palette import (
    ALERT,
    BAND,
    BASELINE,
    ESTIMATE,
    NEGATIVE,
    OBSERVED,
    POSITIVE,
    REFERENCE,
    STRONG,
)
from mapi_plots.recipes import _stats
from mapi_plots.recipes._base import (
    Figure,
    Recipe,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    VlType,
    named,
    recipe,
    wrap_facet,
)
from mapi_plots.recipes._util import (
    SAMPLE,
    coord_values,
    fmt,
    has_samples,
    pct,
    pick_var,
    stack_samples,
    time_dim,
)
from mapi_plots.recipes.predictions import DecompositionSettings, _components, _decomposition

__all__ = [
    "AdstockSettings",
    "BudgetPlanSettings",
    "CrossEffectsSettings",
    "EffectivenessSettings",
    "LiftTestSettings",
    "MMMDecompositionSettings",
    "MarginalCostSettings",
    "MediationSettings",
    "PriorBeliefSettings",
    "ROISettings",
    "ResponseCurveSettings",
    "SpendEffectSettings",
    "WaterfallSettings",
    "adstock",
    "budget_plans",
    "cross_effects",
    "effectiveness",
    "lift_tests",
    "marginal_cost",
    "mediation",
    "mmm_decomposition",
    "mmm_recipes",
    "outcome_dim",
    "prior_beliefs",
    "response_curves",
    "roi",
    "select_outcome",
    "spend_effect",
    "waterfall",
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
            # (nor a per-channel peak spend); a multi-outcome grid's weighted ``contribution``
            # comes before its ``contribution_by_outcome``
            names = [
                n for n in names if s.channel in ds[n].dims and len(_value_dims(ds[n])) > 1
            ] or names
            names = sorted(names, key=lambda n: n != "contribution")
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


def _value_dims(da: xr.DataArray) -> list[str]:
    return [str(d) for d in da.dims if d not in ("chain", "draw", SAMPLE)]


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


def _curve_chart(
    name: str,
    y_title: str,
    s: ResponseCurveSettings,
    current: bool,
    channels: list[str],
    channel_title: str = "channel",
) -> Any:
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
            base.mark_point(filled=True, size=80, shape="diamond", color=OBSERVED).encode(
                x="mean(current_spend):Q",
                y="mean(current_value):Q",
                detail="channel:N",
                tooltip=["channel:N", alt.Tooltip("mean(current_spend):Q", format=".3s")],
            )
        )
    layer = alt.layer(*layers)
    if s.facet:
        return wrap_facet(
            layer.properties(width=s.width // 2, height=s.height),
            "channel",
            channels,
            columns=3,
            title=channel_title,
            data=named(name),
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
    summary="The expected outcome for each level of spend in each channel, with uncertainty.",
    tags=["mmm"],
)
def response_curves(inp: RecipeInput, s: ResponseCurveSettings) -> Figure:
    """Response per channel over a spend grid (median + HDI band), current spend marked."""
    c = _Curves(inp, s)
    curves = _band_rows(c.channels, c.spend, c.values(), s.hdi_prob)
    current = _current_rows(c, curves, "point")
    datasets = {"curves": _with_current(curves, current)}
    chart = _curve_chart(
        "curves",
        c.var,
        s,
        current is not None,
        list(map(str, c.channels)),
        inp.dim_label("channel"),
    )
    caption = (
        f"Each curve shows the expected {c.var} for each level of spend in a channel. The "
        f"shaded area is the {pct(s.hdi_prob)} interval. A steeper curve returns more for each "
        "extra unit of spend. A curve that becomes flat shows that the channel is near "
        "saturation."
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
                f" At current spend (diamonds), {ch} is closest to saturation. An extra unit "
                f"there returns {pct(ratio)} of the return at its steepest point."
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
    summary="The cost (or the return) of the next unit of outcome at each spend level in each "
    "channel.",
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
    chart = _curve_chart(
        "marginal",
        label,
        s,
        current is not None,
        list(map(str, c.channels)),
        inp.dim_label("channel"),
    )
    caption = (
        f"The {label} of {c.var} at each spend level in each channel. The shaded area is the "
        f"{pct(s.hdi_prob)} interval."
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
    summary="The spend allocation and the expected outcome of different budget plans.",
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
        obase.mark_rule(color=REFERENCE).encode(x=alt.datum(0)),
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
            f"({pct(s.hdi_prob)} interval {fmt(best.delta_lo)} to {fmt(best.delta_hi)}). "
            f"The probability that it is better: {pct(best.p_better)}."
        )
    else:
        caption = f"The expected {s.outcome_var} of plan {ref}."
    datasets = {"allocation": alloc, "outcomes": summary}
    return Figure(chart=chart, datasets=datasets, caption=caption)


CHANNEL = "channel"
MEDIATOR = "mediator"
OUTCOME = "outcome"
_OUTCOME_DOC = (
    "Level of the outcome dim to show (default: attrs['primary_outcome'], else the first)"
)


class MMMDecompositionSettings(DecompositionSettings):
    component: str | None = Field("channel", description="Component dim")
    outcome: str | None = Field(None, max_length=200, description=_OUTCOME_DOC)


def outcome_dim(ds: xr.Dataset, named: str | None = None) -> str | None:
    """The dim of ``ds`` that holds the outcomes of a multi-outcome model, else ``None``.

    A score with ``output.names='dataset'`` renames ``outcome`` to the dataset's name for it
    (``data.bind`` maps ``outcome`` to e.g. ``product``): ``named`` is that name when the tree
    carries its ``/binding``; without it, the one dim whose labels hold the ``primary_outcome``
    attr is the outcome dim. Summing over it would add up different KPIs."""
    dims = [str(d) for d in ds.dims]
    for dim in (named, OUTCOME):
        if dim and dim in dims:
            return dim
    attrs = [ds.attrs, *(ds[v].attrs for v in ds.data_vars)]
    wanted = {str(a["primary_outcome"]) for a in attrs if a.get("primary_outcome")}
    if not wanted:
        return None
    hits = [d for d in dims if d in ds.coords and wanted & {str(v) for v in coord_values(ds, d)}]
    return hits[0] if len(hits) == 1 else None


def select_outcome(
    ds: xr.Dataset, outcome: str | None, *, dim: str | None = None
) -> tuple[xr.Dataset, str | None]:
    """``ds`` at one level of its outcome dim (see :func:`outcome_dim`; ``dim`` is the
    dataset's name for ``outcome``): ``outcome`` if given, else the ``primary_outcome`` attr
    (of the group or of a variable) when it is a level, else the first level. Without an
    outcome dim, ``ds`` itself (an ``outcome`` is then an error)."""
    found = outcome_dim(ds, dim)
    if found is None:
        if outcome is not None:
            raise RecipeDataError(f"the data has no {OUTCOME!r} dim to select {outcome!r} from")
        return ds, None
    labels = [str(v) for v in coord_values(ds, found)]
    if outcome is not None and outcome not in labels:
        raise RecipeDataError(f"no outcome {outcome!r} (outcomes: {', '.join(labels)})")
    pick = outcome
    if pick is None:
        attrs = [ds.attrs, *(ds[v].attrs for v in ds.data_vars)]
        named_ = (str(a.get("primary_outcome")) for a in attrs if a.get("primary_outcome"))
        pick = next((p for p in named_ if p in labels), labels[0])
    idx = labels.index(pick)
    out = ds.isel({found: idx}).drop_vars(found, errors="ignore")
    return out, pick


def _select_outcome(
    inp: RecipeInput, ds: xr.Dataset, outcome: str | None
) -> tuple[xr.Dataset, str | None, str]:
    """:func:`select_outcome` with the tree's name for the ``outcome`` dim, plus the caption
    note that names the outcome shown (empty for a single-outcome model)."""
    dim = inp.dim_label(OUTCOME)
    out, pick = select_outcome(ds, outcome, dim=dim)
    if pick is None:
        return out, None, ""
    n = ds.sizes[outcome_dim(ds, dim) or OUTCOME]
    return out, pick, f" The model has {n} outcomes. The chart shows the outcome {pick}."


@recipe(
    "mmm.decomposition",
    title="Contribution by component",
    settings=MMMDecompositionSettings,
    persona="both",
    requires_groups=["contributions"],
    summary="The baseline and channel contributions over time and in total.",
    tags=["mmm"],
)
def mmm_decomposition(inp: RecipeInput, s: MMMDecompositionSettings) -> Figure:
    """Marketing-mix decomposition: channels (plus baseline variables) over time."""
    full = inp.require("contributions", "decomposition", "components")
    ds, pick, note = _select_outcome(inp, full, s.outcome)
    comp = (
        s.component
        if s.component and any(s.component in ds[v].dims for v in ds.data_vars)
        else None
    )
    one = RecipeInput(xr.DataTree(dataset=ds)) if pick is not None else inp
    fig = _decomposition(one, s.model_copy(update={"component": comp}), "channel")
    fig.caption += note
    return fig


# ------------------------------------------------------------------ prior beliefs (M3)

BELIEF_GROUPS = ("prior_beliefs",)
_QUANTITY_TITLES = {
    "roi": "ROI (KPI per unit of spend)",
    "contribution_share": "Share of the KPI",
    "baseline_share": "Share of the KPI",
}


class PriorBeliefSettings(RecipeSettings):
    channel: str = "channel"
    height: int = Field(26, ge=12, le=200, description="Height of one channel row")


def _belief_rows(ds: Any, s: PriorBeliefSettings) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    stated = ds.get("stated_quantity")
    channels: list[str] = []
    for var in ("roi", "contribution_share"):
        if var in ds and s.channel in ds[var].dims:
            channels = [str(c) for c in coord_values(ds[var], s.channel)]
            break
    for i, ch in enumerate(channels):
        what = str(stated.values[i]) if stated is not None else ""
        quantity = what or ("roi" if "roi" in ds else "contribution_share")
        if quantity not in ds:
            continue
        da = ds[quantity]
        st = stack_samples(da) if has_samples(da) else da.expand_dims({SAMPLE: [0]})
        x = np.asarray(st.isel({s.channel: i}).values, dtype=float)
        x = x[np.isfinite(x)]
        if not x.size:
            continue
        q = np.quantile(x, [0.05, 0.25, 0.5, 0.75, 0.95])
        lo = float(ds["stated_low"].values[i]) if what and "stated_low" in ds else np.nan
        hi = float(ds["stated_high"].values[i]) if what and "stated_high" in ds else np.nan
        cred = (
            float(ds["stated_credibility"].values[i])
            if what and "stated_credibility" in ds
            else np.nan
        )
        inside = float(((x >= lo) & (x <= hi)).mean()) if np.isfinite(lo) else np.nan
        rows.append(
            {
                "quantity": _QUANTITY_TITLES[quantity],
                "label": ch,
                "q05": q[0], "q25": q[1], "q50": q[2], "q75": q[3], "q95": q[4],
                "stated_low": lo, "stated_high": hi, "credibility": cred, "coverage": inside,
            }
        )  # fmt: skip
    if "baseline_share" in ds:
        da = ds["baseline_share"]
        x = np.asarray(da.values, dtype=float).ravel()
        x = x[np.isfinite(x)]
        lo = float(da.attrs.get("range_low", np.nan))
        hi = float(da.attrs.get("range_high", np.nan))
        if x.size and np.isfinite(lo):
            q = np.quantile(x, [0.05, 0.25, 0.5, 0.75, 0.95])
            rows.append(
                {
                    "quantity": _QUANTITY_TITLES["baseline_share"],
                    "label": "baseline",
                    "q05": q[0], "q25": q[1], "q50": q[2], "q75": q[3], "q95": q[4],
                    "stated_low": lo, "stated_high": hi,
                    "credibility": float(da.attrs.get("credibility", np.nan)),
                    "coverage": float(((x >= lo) & (x <= hi)).mean()),
                }
            )  # fmt: skip
    if not rows:
        raise RecipeDataError("no ROI or share draws to compare with stated beliefs")
    return pd.DataFrame(rows)


@recipe(
    "mmm.prior_beliefs",
    title="Your beliefs and the priors",
    settings=PriorBeliefSettings,
    persona="guided",
    requires_groups=list(BELIEF_GROUPS),
    summary="The ROI or share of each channel in the translated priors before the model sees "
    "the KPI, compared with the range that you gave.",
    tags=["mmm"],
)
def prior_beliefs(inp: RecipeInput, s: PriorBeliefSettings) -> Figure:
    """Prior ROI/share intervals per channel (50% bar, 90% line, median tick) over the
    stated range (shaded), one panel per quantity."""
    ds = inp.require(*BELIEF_GROUPS)
    frame = _belief_rows(ds, s)
    base = alt.Chart()
    y = alt.Y("label:N", title=None, sort=list(dict.fromkeys(frame["label"])))
    layers: list[Any] = [
        base.mark_bar(height=16, opacity=0.25, color=REFERENCE).encode(
            y=y, x=alt.X("stated_low:Q", title=None), x2="stated_high:Q"
        ),
        base.mark_rule(color=OBSERVED).encode(y=y, x="q05:Q", x2="q95:Q"),
        base.mark_bar(height=8, color=REFERENCE).encode(y=y, x="q25:Q", x2="q75:Q"),
        base.mark_tick(thickness=2, size=14, color=OBSERVED).encode(
            y=y,
            x="q50:Q",
            tooltip=[
                alt.Tooltip("label:N", title="channel"),
                alt.Tooltip("q50:Q", title="median", format=".3g"),
                alt.Tooltip("q05:Q", title="5%", format=".3g"),
                alt.Tooltip("q95:Q", title="95%", format=".3g"),
                alt.Tooltip("stated_low:Q", title="you said from", format=".3g"),
                alt.Tooltip("stated_high:Q", title="to", format=".3g"),
                alt.Tooltip("coverage:Q", title="prior inside your range", format=".0%"),
            ],
        ),
    ]
    quantities = list(dict.fromkeys(frame["quantity"]))
    size = {"width": s.width, "height": alt.Step(s.height)}  # one row per channel
    chart: Any
    if len(quantities) > 1:
        chart = (
            alt.layer(*layers)
            .properties(**size)
            .facet(row=alt.Row("quantity:N", title=None, sort=quantities), data=named("beliefs"))
            .resolve_scale(x="independent", y="independent")
            .resolve_axis(x="independent")
        )
    else:
        chart = alt.layer(*layers, data=named("beliefs")).properties(title=quantities[0], **size)
    parts = []
    for row in frame.itertuples():
        if np.isfinite(row.coverage):
            parts.append(
                f"{row.label}: {pct(row.coverage)} of the prior is in your range "
                f"(you said {pct(row.credibility)})"
            )
    caption = (
        "Before the model sees the KPI, it puts the value of each channel on the black line "
        "(90% of the prior) and mostly in the dark bar (50%). The tick is at the median. The "
        "light band is the range that you gave."
    )
    if parts:
        caption += " " + "; ".join(parts[:8]) + "."
    return Figure(chart=chart, datasets={"beliefs": frame}, caption=caption)


# ------------------------------------------------------------------ v0.12 helpers


def _draw_matrix(da: xr.DataArray, keep: Sequence[str]) -> xr.DataArray:
    """``(sample, *keep)`` draws: ``sample`` or ``chain``/``draw`` flattened (an array without
    them is one draw), every other dim summed."""
    missing = [d for d in keep if d not in da.dims]
    if missing:
        raise RecipeDataError(f"{da.name!r} has no {', '.join(map(repr, missing))} dim")
    st = stack_samples(da) if has_samples(da) else da.expand_dims({SAMPLE: [0]})
    extra = [str(d) for d in st.dims if d != SAMPLE and d not in keep]
    if extra:
        st = st.sum(extra, skipna=False)
    return st.transpose(SAMPLE, *keep)


def _summary(x: np.ndarray, prob: float) -> tuple[float, float, float]:
    """``(median, lo, hi)`` of the finite draws (HDI at ``prob``); NaN without any."""
    x = np.asarray(x, dtype=float).ravel()
    x = x[np.isfinite(x)]
    if not x.size:
        return np.nan, np.nan, np.nan
    if x.size < 2 or np.ptp(x) == 0:
        return float(x[0]), float(x[0]), float(x[0])
    lo, hi = _stats.hdi(x, prob)
    return float(np.median(x)), float(lo), float(hi)


def _labels(ds: xr.Dataset | xr.DataArray, dim: str) -> list[str]:
    return [str(v) for v in coord_values(ds, dim)]


def _var(ds: xr.Dataset, name: str, group: str) -> xr.DataArray:
    if name not in ds.data_vars:
        raise RecipeDataError(f"the {group} group needs a {name!r} variable")
    return ds[name]


def _listing(items: Sequence[str], limit: int = 3) -> str:
    """``a``, ``a and b``, ``a, b and c`` (at most ``limit`` names)."""
    shown = [str(i) for i in items[:limit]]
    if len(items) > limit:
        shown[-1] = f"{shown[-1]} and {len(items) - limit + 1} more"
        return ", ".join(shown)
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def _intervals_of(items: Sequence[str]) -> str:
    """``The interval of a includes`` / ``The intervals of a and b include``."""
    if len(items) == 1:
        return f"The interval of {items[0]} includes"
    return f"The intervals of {_listing(items)} include"


def _interp_rows(x: np.ndarray, v: np.ndarray, at: float) -> np.ndarray:
    """Linear interpolation of every row of ``v`` (over increasing ``x``) at ``at``."""
    return np.array([np.interp(at, x, row) for row in v])


def _forest_layer(
    name: str,
    measure: str | None,
    x_title: str,
    width: int,
    order: list[str],
    *,
    reference: float | None,
    label: str = "channel",
    color: Any = None,
) -> Any:
    """Median dot and interval rule per row of ``name`` (rows of one ``measure``), with a
    dashed reference line."""
    base = alt.Chart()
    if measure is not None:
        base = base.transform_filter(alt.FieldEqualPredicate(field="measure", equal=measure))
    y = alt.Y(f"{label}:N", sort=order, title=None)
    dot = (
        {"color": color}
        if color is not None
        else {"color": alt.value(ESTIMATE)}  # one colour when nothing is encoded
    )
    layers: list[Any] = []
    if reference is not None:
        layers.append(base.mark_rule(color=ALERT, strokeDash=[4, 4]).encode(x=alt.datum(reference)))
    layers += [
        base.mark_rule(color=STRONG).encode(
            x=alt.X("lo:Q", title=x_title, scale=alt.Scale(zero=False)), x2="hi:Q", y=y
        ),
        base.mark_point(filled=True, size=70).encode(
            x="point:Q",
            y=y,
            tooltip=[
                f"{label}:N",
                alt.Tooltip("point:Q", title="median", format=".3g"),
                alt.Tooltip("lo:Q", title="from", format=".3g"),
                alt.Tooltip("hi:Q", title="to", format=".3g"),
            ],
            **dot,
        ),
    ]
    return alt.layer(*layers, data=named(name)).properties(width=width, height=alt.Step(22))


# ------------------------------------------------------------------ ROI and spend vs effect


class ROISettings(RecipeSettings):
    channel: str = "channel"
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    breakeven: float | None = Field(
        1.0, description="ROI at which a channel pays back its spend (None: no line)"
    )


class SpendEffectSettings(RecipeSettings):
    channel: str = "channel"
    hdi_prob: float = Field(0.9, gt=0, lt=1)


class _AtCurrent:
    """Per-draw channel effect and marginal return at current spend, from the response grid
    (``contribution`` @ multiplier 1, or the curve read at ``current_spend``)."""

    def __init__(self, inp: RecipeInput, channel: str) -> None:
        c = _Curves(inp, ResponseCurveSettings(channel=channel))
        ds = inp.require(*CURVE_GROUPS)
        self.kpi = str(ds.attrs.get("kpi") or "KPI")
        vals = c.values()  # (S, C, G)
        current = c.current
        if current is None:
            levels = np.asarray(coord_values(c.draws, c.grid), dtype=float)
            ones = np.flatnonzero(np.isclose(levels, 1.0))
            if c.grid != "multiplier" or not ones.size:
                raise RecipeDataError(
                    "the response curves need the current spend of each channel (current_spend)"
                )
            current = c.spend[:, int(ones[0])]
        self.channels = c.channels
        self.current = np.asarray(current, dtype=float)
        n, k = vals.shape[:2]
        self.effect = np.full((n, k), np.nan)
        self.slope = np.full((n, k), np.nan)
        for i in range(k):
            x = c.spend[i]
            order = np.argsort(x, kind="stable")
            x, v = x[order], vals[:, i, order]
            cur = self.current[i]
            if not (np.isfinite(cur) and cur > 0) or np.ptp(x) <= 0:
                continue  # no spend: no return to measure
            zero = _interp_rows(x, v, 0.0) if x[0] <= 0 else 0.0
            self.effect[:, i] = _interp_rows(x, v, cur) - zero
            keep = np.r_[True, np.diff(x) > 0]  # repeated spend levels break the gradient
            grad = np.gradient(v[:, keep], x[keep], axis=-1)
            self.slope[:, i] = _interp_rows(x[keep], grad, cur)

    @property
    def roi(self) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            out: np.ndarray = self.effect / self.current[None, :]
        return out


def _marginal_from_group(inp: RecipeInput, channels: list[str], channel: str) -> np.ndarray | None:
    """``marginal_roi_total(sample, channel)`` of the marginal_cost group, in ``channels``
    order (``None`` when the group or a channel is missing)."""
    ds = inp.find("marginal_cost")
    if ds is None or "marginal_roi_total" not in ds.data_vars:
        return None
    da = ds["marginal_roi_total"]
    if channel not in da.dims:
        return None
    have = _labels(da, channel)
    if not set(channels) <= set(have):
        return None
    m = _draw_matrix(da, [channel])
    return np.asarray(m.values, dtype=float)[:, [have.index(c) for c in channels]]


@recipe(
    "mmm.roi",
    title="ROI and marginal ROI",
    settings=ROISettings,
    persona="both",
    requires_groups=["response_curves"],
    summary="The return of each channel for each unit of its current spend, and the return "
    "of the next unit of spend, with uncertainty.",
    tags=["mmm"],
)
def roi(inp: RecipeInput, s: ROISettings) -> Figure:
    """ROI forest (effect at current spend / current spend) beside the marginal ROI forest
    (``marginal_cost`` group, else the slope of each draw's curve at current spend)."""
    at = _AtCurrent(inp, s.channel)
    marginal = _marginal_from_group(inp, at.channels, s.channel)
    source = "the marginal_cost group" if marginal is not None else "the response curves"
    if marginal is None:
        marginal = at.slope
    rows: list[dict[str, Any]] = []
    for measure, mat in (("ROI", at.roi), ("marginal ROI", marginal)):
        for i, ch in enumerate(at.channels):
            point, lo, hi = _summary(mat[:, i], s.hdi_prob)
            x = mat[:, i][np.isfinite(mat[:, i])]
            below = (
                float(np.mean(x < s.breakeven)) if s.breakeven is not None and x.size else np.nan
            )
            rows.append(
                {
                    "measure": measure,
                    "channel": ch,
                    "point": point,
                    "lo": lo,
                    "hi": hi,
                    "p_below": below,
                    "current_spend": float(at.current[i]),
                }
            )
    frame = pd.DataFrame(rows)
    if not np.isfinite(frame["point"]).any():
        raise RecipeDataError("no channel has spend, so there is no ROI to show")
    rois = frame[frame.measure == "ROI"].set_index("channel")
    margs = frame[frame.measure == "marginal ROI"].set_index("channel")
    order = list(rois["point"].sort_values(ascending=False, na_position="last").index)
    width = max(120, s.width // 2 - 10)
    unit = f"{at.kpi} per unit of spend"
    chart = alt.hconcat(
        _forest_layer(
            "roi", "ROI", f"ROI ({unit})", width, order, reference=s.breakeven
        ).properties(title="ROI at current spend"),
        _forest_layer(
            "roi", "marginal ROI", f"marginal ROI ({unit})", width, order, reference=s.breakeven
        ).properties(title="Return of the next unit"),
    )
    caption = (
        f"ROI is the {at.kpi} that a channel brings for each unit of its current spend. "
        f"Marginal ROI is the {at.kpi} that the next unit of spend brings. The lines are "
        f"{pct(s.hdi_prob)} intervals."
    )
    valid = rois[np.isfinite(rois["point"])]
    best = valid["point"].idxmax()
    caption += f" {best} has the highest ROI ({fmt(float(valid.loc[best, 'point']))})."
    if s.breakeven is not None:
        be = fmt(s.breakeven)
        sure = [str(c) for c in valid.index if valid.loc[c, "hi"] < s.breakeven]
        maybe = [
            str(c) for c in valid.index if valid.loc[c, "point"] < s.breakeven and c not in sure
        ]
        if sure:
            caption += (
                f" The ROI of {_listing(sure)} is below the break-even point ({be}) for all "
                "of the interval."
            )
        if maybe:
            caption += (
                f" The median ROI of {_listing(maybe)} is below the break-even point, but the "
                "interval includes it."
            )
        if not sure and not maybe:
            caption += f" The median ROI of every channel is above the break-even point ({be})."
        losing = [
            str(c)
            for c in valid.index
            if valid.loc[c, "point"] >= s.breakeven
            and c in margs.index
            and margs.loc[c, "point"] < s.breakeven
        ]
        if losing:
            caption += f" In {_listing(losing)}, the next unit of spend returns less than it costs."
    mvalid = margs[np.isfinite(margs["point"])]
    if len(mvalid):
        top = mvalid["point"].idxmax()
        caption += (
            f" The next unit of spend returns most in {top} "
            f"(marginal ROI {fmt(float(mvalid.loc[top, 'point']))}, from {source})."
        )
    return Figure(chart=chart, datasets={"roi": frame}, caption=caption)


@recipe(
    "mmm.spend_effect",
    title="Share of spend and share of effect",
    settings=SpendEffectSettings,
    persona="both",
    requires_groups=["response_curves"],
    summary="The share of the media budget that each channel gets, beside its share of the "
    "effect of all channels.",
    tags=["mmm"],
)
def spend_effect(inp: RecipeInput, s: SpendEffectSettings) -> Figure:
    """Spend share and effect share (per draw, HDI) of every channel at current spend."""
    at = _AtCurrent(inp, s.channel)
    spend = np.where(np.isfinite(at.current) & (at.current > 0), at.current, 0.0)
    if spend.sum() <= 0:
        raise RecipeDataError("no channel has spend, so there are no shares to show")
    eff = np.nan_to_num(at.effect, nan=0.0)
    tot = eff.sum(axis=1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        eshare = np.where(tot != 0, eff / tot, np.nan)
    sshare = spend / spend.sum()
    rows: list[dict[str, Any]] = []
    for i, ch in enumerate(at.channels):
        point, lo, hi = _summary(eshare[:, i], s.hdi_prob)
        rows.append(
            {
                "channel": ch,
                "measure": "share of spend",
                "point": float(sshare[i]),
                "lo": float(sshare[i]),
                "hi": float(sshare[i]),
            }
        )
        rows.append(
            {"channel": ch, "measure": "share of effect", "point": point, "lo": lo, "hi": hi}
        )
    frame = pd.DataFrame(rows)
    order = list(at.channels)
    measures = ["share of spend", "share of effect"]
    colour = alt.Color(
        "measure:N",
        title=None,
        sort=measures,
        scale=alt.Scale(domain=measures, range=[BASELINE, BAND]),
    )
    base = alt.Chart()
    x = alt.X("channel:N", sort=order, title=None, axis=alt.Axis(labelAngle=0))
    chart = alt.layer(
        base.mark_bar().encode(
            x=x,
            xOffset=alt.XOffset("measure:N", sort=measures),
            y=alt.Y("point:Q", title="share", axis=alt.Axis(format="%")),
            color=colour,
            tooltip=[
                "channel:N",
                "measure:N",
                alt.Tooltip("point:Q", format=".1%"),
                alt.Tooltip("lo:Q", format=".1%"),
                alt.Tooltip("hi:Q", format=".1%"),
            ],
        ),
        base.transform_filter(alt.FieldEqualPredicate(field="measure", equal="share of effect"))
        .mark_rule(color=STRONG)
        .encode(x=x, xOffset=alt.XOffset("measure:N", sort=measures), y="lo:Q", y2="hi:Q"),
        data=named("shares"),
    ).properties(width=s.width, height=s.height)
    caption = (
        "For each channel, the grey bar is its share of the media spend and the blue bar is "
        f"its share of the effect of all channels, with a {pct(s.hdi_prob)} interval. A "
        "channel with a larger share of effect than of spend returns more than the average "
        "channel."
    )
    eff_rows = frame[frame.measure == "share of effect"].set_index("channel")
    ratios = {
        ch: eff_rows.loc[ch, "point"] / sshare[i]
        for i, ch in enumerate(at.channels)
        if sshare[i] > 0 and np.isfinite(eff_rows.loc[ch, "point"])
    }
    if ratios:
        hi_ch = max(ratios, key=lambda k: ratios[k])
        i = at.channels.index(hi_ch)
        caption += (
            f" {hi_ch} gets {pct(sshare[i])} of the spend and brings "
            f"{pct(float(eff_rows.loc[hi_ch, 'point']))} of the effect."
        )
        lo_ch = min(ratios, key=lambda k: ratios[k])
        if lo_ch != hi_ch:
            j = at.channels.index(lo_ch)
            caption += (
                f" {lo_ch} gets {pct(sshare[j])} of the spend but brings only "
                f"{pct(float(eff_rows.loc[lo_ch, 'point']))} of the effect."
            )
    return Figure(chart=chart, datasets={"shares": frame}, caption=caption)


# ------------------------------------------------------------------ adstock


_FREQ_UNITS = {"D": "day", "W": "week", "M": "month", "Q": "quarter", "Y": "year", "A": "year"}


class AdstockSettings(RecipeSettings):
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    facet: bool = Field(False, description="One panel per channel instead of colour")


@recipe(
    "mmm.adstock",
    title="Carry-over of the effect",
    settings=AdstockSettings,
    persona="both",
    requires_groups=["adstock"],
    summary="How the effect of one unit of spend spreads over the periods after the spend, "
    "for each channel.",
    tags=["mmm"],
)
def adstock(inp: RecipeInput, s: AdstockSettings) -> Figure:
    """Share of a unit of spend's effect at each lag (median + HDI band) and the average
    delay per channel."""
    ds = inp.require("adstock")
    w = _draw_matrix(_var(ds, "weight", "adstock"), [CHANNEL, "lag"])
    channels = _labels(w, CHANNEL)
    lags = np.asarray(coord_values(w, "lag"), dtype=float)
    vals = np.asarray(w.values, dtype=float)  # (S, C, L)
    if "mean_lag" in ds.data_vars:
        ml = _draw_matrix(ds["mean_lag"], [CHANNEL])
        mean_lag = np.asarray(ml.values, dtype=float)
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            mean_lag = (vals * lags).sum(-1) / vals.sum(-1)
    freq = str(ds.attrs.get("frequency") or "")
    unit = _FREQ_UNITS.get(freq[:1].upper(), "period") if freq else "period"
    curves = _band_rows(channels, np.tile(lags, (len(channels), 1)), vals, s.hdi_prob)
    curves = curves.rename(columns={"spend": "lag"})
    delays = []
    for i, ch in enumerate(channels):
        point, lo, hi = _summary(mean_lag[:, i], s.hdi_prob)
        first = float(np.nanmedian(vals[:, i, 0]))
        delays.append({"channel": ch, "point": point, "lo": lo, "hi": hi, "first": first})
    delay = pd.DataFrame(delays)
    base = alt.Chart()
    layers = [
        base.mark_area(opacity=0.2).encode(
            x=alt.X("lag:Q", title=f"{unit}s after the spend"),
            y=alt.Y("lo:Q", title="share of the effect", axis=alt.Axis(format="%")),
            y2="hi:Q",
            color=alt.Color("channel:N"),
        ),
        base.mark_line(point=True).encode(
            x="lag:Q",
            y="point:Q",
            color=alt.Color("channel:N"),
            tooltip=["channel:N", "lag:Q", alt.Tooltip("point:Q", format=".1%")],
        ),
    ]
    curve_chart: Any
    if s.facet:
        curve_chart = wrap_facet(
            alt.layer(*layers).properties(width=s.width // 2, height=s.height),
            "channel",
            channels,
            columns=3,
            title=inp.dim_label(CHANNEL),
            data=named("weights"),
        )
    else:
        curve_chart = alt.layer(*layers, data=named("weights")).properties(
            width=s.width, height=s.height, title="Share of the effect in each period"
        )
    order = list(delay.sort_values("point", ascending=False)["channel"])
    delay_chart = _forest_layer(
        "delay", None, f"average delay ({unit}s)", s.width, order, reference=None
    ).properties(title="Average delay of the effect")
    chart = alt.vconcat(curve_chart, delay_chart)
    caption = (
        f"Each line shows the share of the effect of one unit of spend that comes 0, 1, 2 or "
        f"more {unit}s after the spend. The shaded area is the {pct(s.hdi_prob)} interval."
    )
    valid = delay[np.isfinite(delay["point"])]
    if len(valid):
        longest = valid.loc[valid["point"].idxmax()]
        caption += (
            f" {longest.channel} has the longest carry-over. On average, its effect comes "
            f"{fmt(float(longest.point))} {unit}s after the spend ({pct(s.hdi_prob)} interval "
            f"{fmt(float(longest.lo))} to {fmt(float(longest.hi))})."
        )
        if len(valid) > 1:
            shortest = valid.loc[valid["point"].idxmin()]
            caption += (
                f" {shortest.channel} has the shortest carry-over: {pct(float(shortest.first))} "
                f"of its effect comes in the {unit} of the spend."
            )
    return Figure(chart=chart, datasets={"weights": curves, "delay": delay}, caption=caption)


# ------------------------------------------------------------------ waterfall


class WaterfallSettings(RecipeSettings):
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    outcome: str | None = Field(None, max_length=200, description=_OUTCOME_DOC)


_BASE_PARTS = {"baseline", "trend", "seasonality", "intercept", "base"}


@recipe(
    "mmm.waterfall",
    title="Contribution waterfall",
    settings=WaterfallSettings,
    persona="both",
    requires_groups=["contributions"],
    summary="How the baseline and each channel add up to the total KPI of the period, step "
    "by step.",
    tags=["mmm"],
)
def waterfall(inp: RecipeInput, s: WaterfallSettings) -> Figure:
    """Totals over the period: baseline parts first, then channels by size, then the total;
    each step with its HDI."""
    full = inp.require("contributions", "decomposition", "components")
    ds, _, note = _select_outcome(inp, full, s.outcome)
    parts, comp, _ = _components(
        ds, DecompositionSettings(component=None, max_components=100), CHANNEL
    )
    channel_labels: set[str] = set()
    for v in ds.data_vars:
        if comp is not None and comp in ds[v].dims:
            channel_labels |= set(_labels(ds[v], comp))
    rows: list[dict[str, Any]] = []
    totals: list[np.ndarray] = []
    for label, da in parts:
        tot = np.asarray(_draw_matrix(da, []).values, dtype=float)
        is_base = label not in channel_labels or label.lower() in _BASE_PARTS
        point = float(np.nanmean(tot))
        _, lo, hi = _summary(tot, s.hdi_prob)
        rows.append(
            {
                "component": label,
                "kind": "base" if is_base else "channel",
                "mean": point,
                "lo": lo,
                "hi": hi,
            }
        )
        totals.append(tot)
    frame = pd.DataFrame(rows)
    n = min(len(t) for t in totals)
    grand_draws = np.sum([t[:n] for t in totals], axis=0)
    base_part = frame[frame.kind == "base"]
    chan_part = frame[frame.kind == "channel"].sort_values("mean", ascending=False)
    frame = pd.concat([base_part, chan_part], ignore_index=True)
    frame["end"] = frame["mean"].cumsum()
    frame["start"] = frame["end"] - frame["mean"]
    frame["kind"] = np.where(
        frame["kind"] == "base", "baseline", np.where(frame["mean"] >= 0, "adds", "removes")
    )
    frame["lo_end"] = frame["end"] - (frame["mean"] - frame["lo"])
    frame["hi_end"] = frame["end"] + (frame["hi"] - frame["mean"])
    grand = float(np.nanmean(grand_draws))
    _, glo, ghi = _summary(grand_draws, s.hdi_prob)
    total_row = {
        "component": "total",
        "kind": "total",
        "mean": grand,
        "lo": glo,
        "hi": ghi,
        "start": 0.0,
        "end": grand,
        "lo_end": glo,
        "hi_end": ghi,
    }
    frame = pd.concat([frame, pd.DataFrame([total_row])], ignore_index=True)
    frame["share"] = frame["mean"] / grand if grand else np.nan
    frame["order"] = np.arange(len(frame))
    kinds = ["baseline", "adds", "removes", "total"]
    base = alt.Chart()
    x = alt.X("component:N", sort=alt.SortField("order"), title=None, axis=alt.Axis(labelAngle=-30))
    chart = alt.layer(
        base.mark_bar().encode(
            x=x,
            y=alt.Y("start:Q", title="contribution over the period"),
            y2="end:Q",
            color=alt.Color(
                "kind:N",
                title=None,
                sort=kinds,
                scale=alt.Scale(domain=kinds, range=[BASELINE, POSITIVE, NEGATIVE, OBSERVED]),
            ),
            tooltip=[
                "component:N",
                alt.Tooltip("mean:Q", format=".3s"),
                alt.Tooltip("share:Q", format=".1%"),
                alt.Tooltip("lo:Q", format=".3s"),
                alt.Tooltip("hi:Q", format=".3s"),
            ],
        ),
        base.mark_rule(color=STRONG).encode(x=x, y="lo_end:Q", y2="hi_end:Q"),
        data=named("waterfall"),
    ).properties(width=s.width, height=s.height)
    caption = (
        f"The bars add up, from left to right, to the total of {fmt(grand)} over the period. "
        f"Each line is the {pct(s.hdi_prob)} interval of one step."
    )
    base_share = float(frame.loc[frame.kind == "baseline", "mean"].sum()) / grand if grand else 0
    if (frame.kind == "baseline").any() and grand:
        caption += f" The baseline parts give {pct(base_share)}."
    media = frame[frame.kind.isin(["adds", "removes"])]
    if len(media) and grand:
        top = media.loc[media["mean"].idxmax()]
        caption += f" Of the channels, {top.component} gives the most ({pct(float(top.share))})."
        unsure = [str(r.component) for r in media.itertuples() if r.lo < 0 < r.hi]
        if unsure:
            caption += (
                f" {_intervals_of(unsure)} zero, so {'its' if len(unsure) == 1 else 'their'} "
                "contribution is not certain."
            )
    caption += note
    return Figure(chart=chart, datasets={"waterfall": frame}, caption=caption)


# ------------------------------------------------------------------ lift tests


class LiftTestSettings(RecipeSettings):
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    z: float = Field(1.96, gt=0, le=10, description="Standard errors on each side of a test")


def _test_coord(ds: xr.Dataset, name: str, n: int) -> list[str]:
    if name in ds.coords or name in ds.data_vars:
        vals = np.asarray(ds[name].values).ravel()
        if vals.size == n:
            return ["" if v is None else str(v) for v in vals]
    return [""] * n


@recipe(
    "mmm.lift_tests",
    title="Lift tests and the model",
    settings=LiftTestSettings,
    persona="both",
    requires_groups=["lift_tests"],
    summary="The result of each lift test beside the value that the calibrated model gives "
    "for the same channel and period.",
    tags=["mmm"],
)
def lift_tests(inp: RecipeInput, s: LiftTestSettings) -> Figure:
    """Each test's measured value (± z standard errors) beside the model's estimate (HDI)."""
    ds = inp.require("lift_tests")
    model = _draw_matrix(_var(ds, "model", "lift_tests"), ["test"])
    observed = np.asarray(_var(ds, "observed", "lift_tests").values, dtype=float).ravel()
    tests = _labels(model, "test")
    n = len(tests)
    if observed.size != n:
        raise RecipeDataError("observed and model need the same tests")
    sd = (
        np.asarray(ds["observed_sd"].values, dtype=float).ravel()
        if "observed_sd" in ds.data_vars
        else np.zeros(n)
    )
    channel = _test_coord(ds, "channel", n)
    estimand = [e or "contribution" for e in _test_coord(ds, "estimand", n)]
    start, end, geo = (_test_coord(ds, k, n) for k in ("start", "end", "geo"))
    mat = np.asarray(model.values, dtype=float)
    rows: list[dict[str, Any]] = []
    disagree: list[str] = []
    for i, t in enumerate(tests):
        label = f"{t}: {channel[i]}" if channel[i] else t
        if geo[i]:
            label += f" ({geo[i]})"
        point, lo, hi = _summary(mat[:, i], s.hdi_prob)
        olo, ohi = observed[i] - s.z * sd[i], observed[i] + s.z * sd[i]
        agree = bool(np.isfinite(point) and lo <= ohi and olo <= hi)
        if not agree:
            disagree.append(t)
        period = f"{start[i]} to {end[i]}" if start[i] and end[i] else ""
        common = {
            "test": label,
            "estimand": estimand[i],
            "period": period,
            "agrees": "yes" if agree else "no",
        }
        rows.append(
            {
                **common,
                "source": "test",
                "point": float(observed[i]),
                "lo": float(olo),
                "hi": float(ohi),
            }
        )
        rows.append({**common, "source": "model", "point": point, "lo": lo, "hi": hi})
    frame = pd.DataFrame(rows)
    order = list(dict.fromkeys(frame["test"]))
    sources = ["test", "model"]
    colour = alt.Color(
        "source:N",
        title=None,
        sort=sources,
        scale=alt.Scale(domain=sources, range=[OBSERVED, ESTIMATE]),
    )
    base = alt.Chart()
    y = alt.Y("test:N", sort=order, title=None)
    yoff = alt.YOffset("source:N", sort=sources)
    layer = alt.layer(
        base.mark_rule(strokeWidth=2).encode(
            x=alt.X("lo:Q", title="effect", scale=alt.Scale(zero=False)),
            x2="hi:Q",
            y=y,
            yOffset=yoff,
            color=colour,
        ),
        base.mark_point(filled=True, size=60).encode(
            x="point:Q",
            y=y,
            yOffset=yoff,
            color=colour,
            shape=alt.Shape("source:N", sort=sources, legend=None),
            tooltip=[
                "test:N",
                "source:N",
                "period:N",
                "agrees:N",
                alt.Tooltip("point:Q", format=".3g"),
                alt.Tooltip("lo:Q", format=".3g"),
                alt.Tooltip("hi:Q", format=".3g"),
            ],
        ),
    )
    estimands = list(dict.fromkeys(estimand))
    chart: Any
    if len(estimands) > 1:
        chart = (
            layer.properties(width=s.width, height=alt.Step(14))
            .facet(row=alt.Row("estimand:N", title=None, sort=estimands), data=named("tests"))
            .resolve_scale(x="independent", y="independent")
            .resolve_axis(x="independent")
        )
    else:
        chart = alt.layer(*layer.layer, data=named("tests")).properties(
            width=s.width, height=alt.Step(14), title=estimands[0]
        )
    caption = (
        f"Each test has two rows. The black row is the result of the test (± {s.z:g} "
        f"standard errors). The blue row is the {pct(s.hdi_prob)} interval of the model for "
        "the same channel, period and market."
    )
    if disagree:
        first = tests.index(disagree[0])
        m_point = float(frame.loc[(frame.source == "model"), "point"].iloc[first])
        caption += (
            f" The model does not agree with {len(disagree)} of {n} tests: "
            f"{_listing(disagree)}. For {disagree[0]}, the model gives {fmt(m_point)} and the "
            f"test gave {fmt(float(observed[first]))}. Look at the period and the market of "
            "the test, then look at the prior of the channel."
        )
    else:
        caption += f" The model agrees with all {n} tests: each pair of ranges overlaps."
    return Figure(chart=chart, datasets={"tests": frame}, caption=caption)


# ------------------------------------------------------------------ effectiveness


class EffectivenessSettings(RecipeSettings):
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    only_varying: bool = Field(True, description="Show only the time-varying channels")


def _when(x: Any) -> str:
    if isinstance(x, np.datetime64 | pd.Timestamp):
        return str(pd.Timestamp(x).date())
    return str(x)


@recipe(
    "mmm.effectiveness",
    title="Effect of spend over time",
    settings=EffectivenessSettings,
    persona="both",
    requires_groups=["effectiveness"],
    summary="The effect of one unit of spend in each period, for channels whose effect "
    "changes over time.",
    tags=["mmm"],
)
def effectiveness(inp: RecipeInput, s: EffectivenessSettings) -> Figure:
    """KPI per unit of spend per period (median + HDI band); time-varying channels only."""
    ds = inp.require("effectiveness")
    da = _var(ds, "effectiveness", "effectiveness")
    t = time_dim(da.isel({d: 0 for d in da.dims if d in ("chain", "draw", SAMPLE)})) or "time"
    mat = _draw_matrix(da, [CHANNEL, t])
    channels = _labels(mat, CHANNEL)
    vals = np.asarray(mat.values, dtype=float)  # (S, C, T)
    med = np.nanmedian(vals, axis=0)
    if "time_varying" in ds.data_vars:
        flags = np.asarray(ds["time_varying"].transpose(CHANNEL).values).astype(bool)
    else:  # no flags: a channel varies when its median moves
        scale = np.nanmax(np.abs(med), axis=1)
        flags = np.nanmax(med, axis=1) - np.nanmin(med, axis=1) > 1e-9 * np.maximum(scale, 1)
    varying = [ch for ch, f in zip(channels, flags, strict=True) if f]
    shown = varying if s.only_varying and varying else channels
    idx = [channels.index(c) for c in shown]
    xs = coord_values(mat, t)
    rows = _band_rows(
        shown, np.tile(np.arange(len(xs)), (len(shown), 1)), vals[:, idx, :], s.hdi_prob
    )
    rows["x"] = np.tile(np.asarray(xs), len(shown))
    rows = rows.drop(columns="spend")
    temporal = np.issubdtype(np.asarray(xs).dtype, np.datetime64)
    xtype: VlType = "temporal" if temporal else "quantitative"
    base = alt.Chart()
    chart = alt.layer(
        base.mark_area(opacity=0.2).encode(
            x=alt.X("x", type=xtype, title=t),
            y=alt.Y("lo:Q", title="KPI per unit of spend"),
            y2="hi:Q",
            color=alt.Color("channel:N"),
        ),
        base.mark_line().encode(
            x=alt.X("x", type=xtype),
            y="point:Q",
            color=alt.Color("channel:N"),
            tooltip=[
                alt.Tooltip("x", type=xtype, title=t),
                "channel:N",
                alt.Tooltip("point:Q", format=".3g"),
            ],
        ),
        data=named("effectiveness"),
    ).properties(width=s.width, height=s.height)
    caption = (
        "Each line is the KPI that one unit of spend brings in each period, at the average "
        f"spend level. The shaded area is the {pct(s.hdi_prob)} interval."
    )
    if not varying:
        caption += " No channel has an effect that changes over time, so the lines are flat."
    for ch in varying[:4]:
        i = channels.index(ch)
        first, last = float(med[i, 0]), float(med[i, -1])
        peak = int(np.nanargmax(med[i]))
        change = (last - first) / abs(first) if first else np.nan
        caption += (
            f" The effect of {ch} went from {fmt(first)} to {fmt(last)}"
            + (f" ({'+' if change >= 0 else '−'}{pct(abs(change))})" if np.isfinite(change) else "")
            + f" and was highest at {_when(xs[peak])}."
        )
    if s.only_varying and varying and len(varying) < len(channels):
        fixed = [c for c in channels if c not in varying]
        caption += f" The effect of {_listing(fixed)} does not change over time (not shown)."
    return Figure(chart=chart, datasets={"effectiveness": rows}, caption=caption)


# ------------------------------------------------------------------ mediation


class MediationSettings(RecipeSettings):
    hdi_prob: float = Field(0.9, gt=0, lt=1)


@recipe(
    "mmm.mediation",
    title="Direct and mediated effects",
    settings=MediationSettings,
    persona="both",
    requires_groups=["mediation"],
    summary="How much of the effect of each channel comes directly, and how much comes "
    "through each mediator, such as visits or awareness.",
    tags=["mmm"],
)
def mediation(inp: RecipeInput, s: MediationSettings) -> Figure:
    """Stacked mean paths per channel (direct + each mediator), total HDI, and the share
    mediated (HDI)."""
    ds = inp.require("mediation")
    direct = _draw_matrix(_var(ds, "direct", "mediation"), [CHANNEL])
    indirect = _draw_matrix(_var(ds, "indirect", "mediation"), [CHANNEL, MEDIATOR])
    channels = _labels(direct, CHANNEL)
    mediators = _labels(indirect, MEDIATOR)
    d = np.asarray(direct.values, dtype=float)
    ind = np.asarray(indirect.sel({CHANNEL: direct[CHANNEL]}).values, dtype=float)
    n = min(d.shape[0], ind.shape[0])
    d, ind = d[:n], ind[:n]
    if "total" in ds.data_vars:
        total = np.asarray(_draw_matrix(ds["total"], [CHANNEL]).values, dtype=float)[:n]
    else:
        total = d + ind.sum(axis=2)
    if "share_mediated" in ds.data_vars:
        share = np.asarray(_draw_matrix(ds["share_mediated"], [CHANNEL]).values, dtype=float)[:n]
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            share = np.where(total != 0, ind.sum(axis=2) / total, np.nan)
    paths: list[dict[str, Any]] = []
    sums: list[dict[str, Any]] = []
    unsure: list[str] = []
    for i, ch in enumerate(channels):
        parts = [("direct", d[:, i])] + [
            (f"through {m}", ind[:, i, j]) for j, m in enumerate(mediators)
        ]
        for k, (path, x) in enumerate(parts):
            _, lo, hi = _summary(x, s.hdi_prob)
            paths.append(
                {
                    "channel": ch,
                    "path": path,
                    "mean": float(np.nanmean(x)),
                    "lo": lo,
                    "hi": hi,
                    "order": k,
                }
            )
            if path != "direct" and lo < 0 < hi:
                unsure.append(f"{ch} {path}")
        _, tlo, thi = _summary(total[:, i], s.hdi_prob)
        sp, slo, shi = _summary(share[:, i], s.hdi_prob)
        sums.append(
            {
                "channel": ch,
                "total": float(np.nanmean(total[:, i])),
                "total_lo": tlo,
                "total_hi": thi,
                "point": sp,
                "lo": slo,
                "hi": shi,
            }
        )
    pf, sf = pd.DataFrame(paths), pd.DataFrame(sums)
    order = list(sf.sort_values("total", ascending=False)["channel"])
    path_names = list(dict.fromkeys(pf["path"]))
    bars = alt.layer(
        alt.Chart(named("paths"))
        .mark_bar()
        .encode(
            y=alt.Y("channel:N", sort=order, title=None),
            x=alt.X("mean:Q", stack="zero", title="effect over the period"),
            color=alt.Color("path:N", title=None, sort=path_names),
            order=alt.Order("order:Q"),
            tooltip=[
                "channel:N",
                "path:N",
                alt.Tooltip("mean:Q", format=".3s"),
                alt.Tooltip("lo:Q", format=".3s"),
                alt.Tooltip("hi:Q", format=".3s"),
            ],
        ),
        alt.Chart(named("totals"))
        .mark_rule(color=STRONG)
        .encode(y=alt.Y("channel:N", sort=order), x="total_lo:Q", x2="total_hi:Q"),
    ).properties(width=s.width, height=alt.Step(22), title="Effect by path")
    share_chart = _forest_layer(
        "totals", None, "share of the effect through mediators", s.width, order, reference=None
    ).properties(title="Share mediated")
    chart = alt.vconcat(bars, share_chart)
    caption = (
        "Each bar divides the effect of a channel into its direct part and the parts that "
        f"go through each mediator. The lines are {pct(s.hdi_prob)} intervals."
    )
    valid = sf[np.isfinite(sf["point"])]
    if len(valid):
        top = valid.loc[valid["point"].idxmax()]
        caption += (
            f" {top.channel} has the largest share through the mediators: {pct(float(top.point))}"
            f" ({pct(float(top.lo))} to {pct(float(top.hi))})."
        )
    via = pf[pf.path != "direct"]
    if len(via):
        big = via.loc[via["mean"].abs().idxmax()]
        caption += f" The largest mediated path is {big.channel} {big.path} ({fmt(big['mean'])})."
    if unsure:
        caption += f" {_intervals_of(unsure)} zero."
    return Figure(chart=chart, datasets={"paths": pf, "totals": sf}, caption=caption)


# ------------------------------------------------------------------ cross-effects


class CrossEffectsSettings(RecipeSettings):
    hdi_prob: float = Field(0.9, gt=0, lt=1)


@recipe(
    "mmm.cross_effects",
    title="Effects between outcomes",
    settings=CrossEffectsSettings,
    persona="both",
    requires_groups=["cross_effects"],
    summary="How a change in one outcome goes with a change in another outcome, for each "
    "pair that the model links.",
    tags=["mmm"],
)
def cross_effects(inp: RecipeInput, s: CrossEffectsSettings) -> Figure:
    """Forest of each cross-effect ψ (median + HDI) with a zero line, coloured by kind."""
    ds = inp.require("cross_effects")
    psi = _draw_matrix(_var(ds, "psi", "cross_effects"), ["pair"])
    pairs = _labels(psi, "pair")
    n = len(pairs)
    source, target = _test_coord(ds, "source", n), _test_coord(ds, "target", n)
    kind = [k or "unconstrained" for k in _test_coord(ds, "kind", n)]
    mat = np.asarray(psi.values, dtype=float)
    rows = []
    for i, p in enumerate(pairs):
        point, lo, hi = _summary(mat[:, i], s.hdi_prob)
        rows.append(
            {
                "pair": p,
                "source": source[i] or p,
                "target": target[i],
                "kind": kind[i],
                "point": point,
                "lo": lo,
                "hi": hi,
            }
        )
    frame = pd.DataFrame(rows)
    units = str(ds["psi"].attrs.get("units") or ds.attrs.get("units") or "")
    chart = _forest_layer(
        "effects",
        None,
        "effect" + (f" ({units})" if units else ""),
        s.width,
        pairs,
        reference=0.0,
        label="pair",
        color=alt.Color("kind:N", title="kind"),
    ).properties(title="Effect of one outcome on another")
    caption = (
        "Each row is the change in the target outcome that goes with a rise of one unit in "
        f"the source outcome. The lines are {pct(s.hdi_prob)} intervals. The model measures "
        "an association, not a proof of cause."
    )
    clear = frame[(frame["lo"] > 0) | (frame["hi"] < 0)]
    if len(clear):
        parts = [
            f"{r.source} to {r.target} ({'+' if r.point >= 0 else '−'}{fmt(abs(r.point))})"
            for r in clear.itertuples()
        ]
        caption += (
            f" For {len(clear)} of {n} pairs, the interval does not include zero: "
            f"{_listing(parts)}."
        )
    else:
        caption += f" The intervals of all {n} pairs include zero."
    return Figure(chart=chart, datasets={"effects": frame}, caption=caption)


def mmm_recipes() -> list[Recipe[Any]]:
    """The MMM domain pack (entry point ``mapi.plots: mmm``)."""
    return [
        response_curves,
        marginal_cost,
        budget_plans,
        mmm_decomposition,
        prior_beliefs,
        roi,
        adstock,
        waterfall,
        spend_effect,
        lift_tests,
        effectiveness,
        mediation,
        cross_effects,
    ]
