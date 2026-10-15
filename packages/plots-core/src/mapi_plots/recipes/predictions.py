"""Prediction recipes: fit/forecast fan, fit vs actual, residuals, decomposition,
counterfactuals and decision distributions.

They read either a fitted bundle tree (``posterior_predictive`` + ``observed_data``) or a
score output tree (``summary`` with a ``stat`` dim, ``draws``/``predictions`` with draws,
``observed``, ``baseline/…`` and ``delta/…``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.capabilities import Representation
from pydantic import Field, field_validator

from mapi_plots.recipes import _stats
from mapi_plots.recipes._base import (
    Figure,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    VlType,
    named,
    recipe,
)
from mapi_plots.recipes._util import (
    SAMPLE,
    check_levels,
    coord_values,
    facet_dim,
    fmt,
    has_samples,
    interval_columns,
    normalise_time,
    observed_name,
    pct,
    pick_var,
    stack_samples,
    summary_bands,
    time_dim,
)

__all__ = [
    "CounterfactualSettings",
    "DecisionSettings",
    "DecompositionSettings",
    "FanSettings",
    "FitVsActualSettings",
    "counterfactual",
    "decision_distribution",
    "decomposition",
    "fit_vs_actual",
    "ppc_fan",
    "residuals_over_time",
]

DRAW_GROUPS = ("posterior_predictive", "predictions", "draws")
OBS_GROUPS = ("observed_data", "observed")


# ------------------------------------------------------------------------ shared bands


@dataclass(slots=True)
class Bands:
    frame: pd.DataFrame  # x, group, [cell], point, lo_*/hi_*, [observed]
    var: str
    x_dim: str
    facet: str | None
    summed: list[str]
    source: Literal["draws", "summary"]
    temporal: bool
    cells: list[str] = field(default_factory=list)  # dims kept per cell (summaries only)


def _x_values(da: xr.DataArray, dim: str) -> np.ndarray:
    return coord_values(da, dim)


def _reduce_draws(
    da: xr.DataArray, keep: list[str], how: Literal["sum", "mean"] = "sum"
) -> tuple[xr.DataArray, list[str]]:
    other = [str(d) for d in da.dims if d not in keep and d != SAMPLE]
    if not other:
        return da, []
    return (da.sum(other) if how == "sum" else da.mean(other)), other


def band_frame(
    inp: RecipeInput,
    *,
    var: str | None,
    along: str | None,
    facet: str | None,
    max_facets: int,
    levels: list[float],
    point: Literal["median", "mean"],
    draw_groups: tuple[str, ...] = DRAW_GROUPS,
    summary_groups: tuple[str, ...] = ("summary",),
    obs_groups: tuple[str, ...] = OBS_GROUPS,
    need_time: bool = True,
    observed_var: str | None = None,
    per_cell: bool = False,
) -> Bands:
    """Point + interval bands over a time-like dim (optionally per facet) with observations.

    Draws are summed over the other dims (joint samples make totals valid). Summaries cannot
    be added up, so they need a single cell per ``(x, facet)``; with ``per_cell`` the other
    dims are kept instead and labelled in a ``cell`` column (for cell-wise comparisons such as
    predicted vs actual, never for totals).
    """
    ds = inp.find(*draw_groups)
    source: Literal["draws", "summary"] = "draws"
    if ds is not None:
        v = pick_var(ds, var)
        if not has_samples(ds[v]):
            ds = None
    if ds is None:
        ds = inp.find(*summary_groups)
        source = "summary"
        if ds is None:
            raise RecipeDataError(
                "no predictions found: expected one of the groups "
                + ", ".join([*draw_groups, *summary_groups])
            )
    v = pick_var(ds, var)
    da = ds[v]
    t = time_dim(da, along)
    if t is None:
        if need_time:
            raise RecipeDataError(f"{v!r} has no time-like dim; pass along=<dim>")
        t = next(str(d) for d in da.dims if d not in ("chain", "draw", SAMPLE, "stat"))
    f = facet_dim(da, [t], prefer=facet, max_levels=max_facets)
    keep = [t] + ([f] if f else [])
    cells: list[str] = []
    cols: dict[str, xr.DataArray] = {}
    if source == "draws":
        st = stack_samples(da)
        st, summed = _reduce_draws(st, keep)
        cols["point"] = st.median(SAMPLE) if point == "median" else st.mean(SAMPLE)
        for lv, lo_c, hi_c in interval_columns(levels):
            a = (1 - lv) / 2
            cols[lo_c] = st.quantile(a, SAMPLE).drop_vars("quantile")
            cols[hi_c] = st.quantile(1 - a, SAMPLE).drop_vars("quantile")
    else:
        extra = [str(d) for d in da.dims if d not in (*keep, "stat")]
        big = [d for d in extra if da.sizes[d] > 1]
        if big and not per_cell:
            raise RecipeDataError(
                f"summaries cannot be added up over {big}; request them aggregated "
                "(ScoreRequest output.aggregate) or pass facet=<dim>"
            )
        cells = big
        single = [d for d in extra if d not in big]
        da = da.squeeze(single, drop=True) if single else da
        cols = summary_bands(da, levels)
        summed = []
    obs_ds = inp.find(*obs_groups)
    obs_name = observed_name(obs_ds, v, observed_var)
    if obs_ds is not None and obs_name is not None:
        o = obs_ds[obs_name]
        o_other = [d for d in o.dims if d not in (*keep, *cells)]
        if o_other:
            o = o.sum(o_other, min_count=1)
        ref = cols["point"]
        o = o.reindex({d: ref[d] for d in (*keep, *cells) if d in ref.coords and d in o.dims})
        cols["observed"] = o
    frame = xr.Dataset(
        {k: c.drop_vars([x for x in c.coords if x not in c.dims]) for k, c in cols.items()}
    ).to_dataframe()
    frame = frame.reset_index()
    frame = frame.rename(columns={t: "x"})
    frame["group"] = frame[f].astype(str) if f else "all"
    if cells:
        labels = frame[cells].astype(str)
        frame["cell"] = labels.agg(", ".join, axis=1) if len(cells) > 1 else labels[cells[0]]
    order = ["x", "group", *(["cell"] if cells else []), *cols]
    frame = frame[order]
    temporal = t in da.coords and np.issubdtype(da[t].dtype, np.datetime64)
    return Bands(frame, v, t, f, summed, source, temporal, cells)


def _split_point(inp: RecipeInput, bands: Bands, requested: Any) -> Any:
    if requested is not None:
        return normalise_time(requested) if bands.temporal else requested
    attr = inp.attrs.get("split_at")
    if attr is not None:
        return normalise_time(attr) if bands.temporal else attr
    if "observed" in bands.frame:
        seen = bands.frame.loc[bands.frame["observed"].notna(), "x"]
        if len(seen) and seen.max() < bands.frame["x"].max():
            later = bands.frame.loc[bands.frame["x"] > seen.max(), "x"]
            return later.min()
    return None


def _xenc(bands: Bands, **kw: Any) -> alt.X:
    return alt.X("x", type="temporal" if bands.temporal else "quantitative", **kw)


def _xtip(bands: Bands) -> alt.Tooltip:
    return alt.Tooltip("x", type="temporal" if bands.temporal else "quantitative")


class FanSettings(RecipeSettings):
    var: str | None = None
    observed_var: str | None = Field(
        None, description="Observed variable (default: same name, or the only one)"
    )
    along: str | None = None
    facet: str | None = None
    max_facets: int = Field(12, ge=1, le=48)
    levels: list[float] = Field(default_factory=lambda: [0.5, 0.9])
    point: Literal["median", "mean"] = "median"
    split_at: str | float | int | None = Field(
        None, description="Where training data ends (default: tree attr split_at, or auto)"
    )
    columns: int = Field(3, ge=1, le=8)
    max_rows: int = Field(50_000, ge=10, le=500_000)

    @field_validator("levels")
    @classmethod
    def _levels(cls, v: list[float]) -> list[float]:
        return check_levels(v)


@recipe(
    "core.ppc_fan",
    title="Fit and forecast",
    settings=FanSettings,
    persona="both",
    summary="Model estimate with uncertainty bands over time, actual values and the split line.",
)
def ppc_fan(inp: RecipeInput, s: FanSettings) -> Figure:
    """Fan chart of predictions over time with observations and the train/forecast split."""
    b = band_frame(
        inp,
        var=s.var,
        along=s.along,
        facet=s.facet,
        max_facets=s.max_facets,
        levels=s.levels,
        point=s.point,
        observed_var=s.observed_var,
    )
    frame = b.frame
    if len(frame) > s.max_rows:
        raise RecipeDataError(f"{len(frame)} rows to plot; select fewer coordinates")
    split = _split_point(inp, b, s.split_at)
    if split is not None:
        frame["split"] = pd.to_datetime(split) if b.temporal else split
    base = alt.Chart()
    layers: list[Any] = []
    pairs = interval_columns(s.levels)
    for i, (_, lo_c, hi_c) in enumerate(pairs):
        layers.append(
            base.mark_area(opacity=0.15 + 0.15 * i, color="#3366cc").encode(
                x=_xenc(b, title=b.x_dim),
                y=alt.Y(f"{lo_c}:Q", title=b.var, scale=alt.Scale(zero=False)),
                y2=f"{hi_c}:Q",
            )
        )
    layers.append(base.mark_line(color="#1f4e99").encode(x=_xenc(b), y="point:Q"))
    if "observed" in frame:
        layers.append(
            base.mark_circle(size=16, color="#111111").encode(
                x=_xenc(b), y="observed:Q", tooltip=[_xtip(b), "observed:Q", "point:Q"]
            )
        )
    if split is not None:
        agg = "min(split)"
        layers.append(
            base.mark_rule(strokeDash=[5, 4], color="#c62828").encode(
                x=alt.X(agg, type="temporal" if b.temporal else "quantitative")
            )
        )
    layer = alt.layer(*layers, data=named("fan")).properties(
        width=s.width if b.facet is None else max(160, s.width // min(s.columns, 3)),
        height=s.height,
    )
    if b.facet is not None:
        chart = layer.facet(
            facet=alt.Facet("group:N", title=b.facet), columns=s.columns
        ).resolve_scale(y="independent")
    else:
        chart = layer
    lv_txt = " and ".join(pct(lv) for lv, _, _ in reversed(pairs))
    caption = (
        f"The line is the model's {s.point} estimate of {b.var}; the shaded bands are the "
        f"{lv_txt} intervals"
        + ("; dots are the actual values" if "observed" in frame else "")
        + "."
    )
    if b.summed:
        caption += f" Values are summed over {', '.join(b.summed)}."
    if split is not None:
        caption += " The dashed line marks where the training data ends."
        if "observed" in frame:
            after = frame[(frame["x"] >= split) & frame["observed"].notna()]
            _, lo_c, hi_c = pairs[0]
            if len(after):
                inside = float(
                    ((after["observed"] >= after[lo_c]) & (after["observed"] <= after[hi_c])).mean()
                )
                caption += (
                    f" After it, {pct(inside)} of actual values fall inside the "
                    f"{pct(pairs[0][0])} band."
                )
    return Figure(chart=chart, datasets={"fan": frame}, caption=caption)


# ----------------------------------------------------------------- fit vs actual / residuals


class FitVsActualSettings(RecipeSettings):
    var: str | None = None
    observed_var: str | None = None
    along: str | None = None
    facet: str | None = None
    max_facets: int = Field(12, ge=1, le=48)
    point: Literal["median", "mean"] = "median"


def _cell_col(b: Bands) -> list[str]:
    return ["cell"] if b.cells else []


def _cell_tip(b: Bands) -> list[Any]:
    return [alt.Tooltip("cell:N", title=" × ".join(b.cells))] if b.cells else []


def _point_and_obs(inp: RecipeInput, s: FitVsActualSettings) -> Bands:
    b = band_frame(
        inp,
        var=s.var,
        along=s.along,
        facet=s.facet,
        max_facets=s.max_facets,
        levels=[0.9],
        point=s.point,
        need_time=False,
        observed_var=s.observed_var,
        per_cell=True,
    )
    if "observed" not in b.frame:
        raise RecipeDataError(f"no observed values of {b.var!r} (observed_data/observed group)")
    b.frame = b.frame[b.frame["observed"].notna() & b.frame["point"].notna()].reset_index(drop=True)
    if b.frame.empty:
        raise RecipeDataError("no cells have both a prediction and an observation")
    return b


@recipe(
    "core.fit_vs_actual",
    title="Predicted vs actual",
    settings=FitVsActualSettings,
    persona="both",
)
def fit_vs_actual(inp: RecipeInput, s: FitVsActualSettings) -> Figure:
    """Scatter of predicted against actual values with the identity line."""
    b = _point_and_obs(inp, s)
    f = b.frame
    lo = float(min(f["point"].min(), f["observed"].min()))
    hi = float(max(f["point"].max(), f["observed"].max()))
    diag = pd.DataFrame({"v": [lo, hi]})
    pts = (
        alt.Chart(named("points"))
        .mark_circle(size=28, opacity=0.7)
        .encode(
            x=alt.X("observed:Q", title=f"actual {b.var}"),
            y=alt.Y("point:Q", title=f"predicted {b.var}"),
            color=alt.Color("group:N", title=b.facet or ""),
            tooltip=[_xtip(b), "group:N", *_cell_tip(b), "observed:Q", "point:Q"],
        )
    )
    line = (
        alt.Chart(named("identity"))
        .mark_line(color="#999", strokeDash=[4, 4])
        .encode(x="v:Q", y="v:Q")
    )
    chart = alt.layer(line, pts).properties(width=s.width, height=s.width * 3 // 4)
    resid = f["observed"] - f["point"]
    ss_tot = float(((f["observed"] - f["observed"].mean()) ** 2).sum())
    r2 = 1 - float((resid**2).sum()) / ss_tot if ss_tot > 0 else float("nan")
    nz = f["observed"] != 0
    mape = (
        float((resid[nz].abs() / f.loc[nz, "observed"].abs()).mean()) if nz.any() else float("nan")
    )
    caption = (
        f"Each dot compares an actual value of {b.var} with the model's prediction; dots on the "
        f"dashed line are perfect predictions. The model explains {pct(r2)} of the variation "
        f"(R² = {r2:.2f})"
        + (f" with a typical error of {pct(mape)} (MAPE)." if np.isfinite(mape) else ".")
    )
    data = {"points": f[["x", "group", *_cell_col(b), "point", "observed"]], "identity": diag}
    return Figure(chart=chart, datasets=data, caption=caption)


@recipe(
    "core.residuals_over_time",
    title="Residuals over time",
    settings=FitVsActualSettings,
    persona="expert",
)
def residuals_over_time(inp: RecipeInput, s: FitVsActualSettings) -> Figure:
    """Actual minus predicted over time; patterns mean the model misses structure."""
    b = _point_and_obs(inp, s)
    f = b.frame.copy()
    f["residual"] = f["observed"] - f["point"]
    base = alt.Chart()
    chart = alt.layer(
        base.mark_rule(color="#999").encode(y=alt.datum(0)),
        base.mark_line(point=True, strokeWidth=1, opacity=0.6 if b.cells else 1.0).encode(
            x=_xenc(b, title=b.x_dim),
            y=alt.Y("residual:Q", title=f"actual − predicted {b.var}"),
            color=alt.Color("group:N", title=b.facet or ""),
            tooltip=[_xtip(b), "group:N", *_cell_tip(b), "residual:Q"],
            **({"detail": alt.Detail("cell:N")} if b.cells else {}),
        ),
        data=named("residuals"),
    ).properties(width=s.width, height=s.height)
    bias = float(f["residual"].mean())
    scale = float(f["observed"].abs().mean()) or 1.0
    caption = f"Residuals are actual minus predicted {b.var}; they should hover around zero."
    if abs(bias) > 0.05 * scale:
        direction = "under" if bias > 0 else "over"
        caption += f" On average the model {direction}-predicts by {fmt(abs(bias))}."
    else:
        caption += " There is no notable average bias."
    if b.cells:
        caption += f" Each line is one {' × '.join(b.cells)} cell."
    data = {"residuals": f[["x", "group", *_cell_col(b), "residual"]]}
    return Figure(chart=chart, datasets=data, caption=caption)


# ------------------------------------------------------------------------- decomposition


class DecompositionSettings(RecipeSettings):
    vars: list[str] | None = Field(None, description="Variables to use (default: all)")
    component: str | None = Field(None, description="Component dim (default: auto)")
    along: str | None = None
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    max_components: int = Field(20, ge=1, le=100)


_COMPONENT_DIMS = ("component", "channel", "contribution", "term", "effect")
DECOMP_GROUPS = ("contributions", "decomposition", "components")


def _components(
    ds: xr.Dataset, s: DecompositionSettings, default_component: str | None
) -> tuple[list[tuple[str, xr.DataArray]], str | None]:
    names = s.vars or [str(v) for v in ds.data_vars if np.issubdtype(ds[v].dtype, np.number)]
    if not names:
        raise RecipeDataError("the contributions group has no numeric variables")
    unknown = [n for n in names if n not in ds.data_vars]
    if unknown:
        raise RecipeDataError(f"unknown variables: {', '.join(unknown)}")
    comp = s.component
    if comp is None:
        for cand in ((default_component,) if default_component else ()) + _COMPONENT_DIMS:
            if cand and any(cand in ds[n].dims for n in names):
                comp = cand
                break
    out: list[tuple[str, xr.DataArray]] = []
    for n in names:
        da = ds[n]
        if comp is not None and comp in da.dims:
            for label in coord_values(da, comp):
                out.append((str(label), da.sel({comp: label}, drop=True)))
        else:
            out.append((n, da))
    return out[: s.max_components], comp


def _decomposition(
    inp: RecipeInput, s: DecompositionSettings, default_component: str | None
) -> Figure:
    ds = inp.require(*DECOMP_GROUPS)
    parts, comp = _components(ds, s, default_component)
    t = time_dim(parts[0][1], s.along)
    if t is None:
        raise RecipeDataError("contributions need a time-like dim; pass along=<dim>")
    series_parts: list[pd.DataFrame] = []
    totals: list[dict[str, Any]] = []
    summed: set[str] = set()
    for label, da in parts:
        if t not in da.dims:
            raise RecipeDataError(f"component {label!r} has no {t!r} dim")
        if has_samples(da):
            st = stack_samples(da)
            st, other = _reduce_draws(st, [t])
            summed |= set(other)
            per_t = st.mean(SAMPLE)
            tot = np.asarray(st.sum(t).values, dtype=float)
            lo, hi = _stats.hdi(tot, s.hdi_prob) if tot.size > 1 else (tot[0], tot[0])
            mean = float(tot.mean())
        else:
            other = [str(d) for d in da.dims if d != t]
            summed |= set(other)
            per_t = da.sum(other) if other else da
            mean = float(per_t.sum())
            lo = hi = mean
        series_parts.append(
            pd.DataFrame({"x": coord_values(per_t, t), "component": label, "value": per_t.values})
        )
        totals.append({"component": label, "mean": mean, "lo": float(lo), "hi": float(hi)})
    series = pd.concat(series_parts, ignore_index=True)
    tot = pd.DataFrame(totals)
    grand = float(tot["mean"].sum())
    tot["share"] = tot["mean"] / grand if grand else np.nan
    temporal = t in ds.coords and np.issubdtype(ds[t].dtype, np.datetime64)
    xtype: VlType = "temporal" if temporal else "quantitative"
    area = (
        alt.Chart(named("decomposition"))
        .mark_area(opacity=0.85)
        .encode(
            x=alt.X("x", type=xtype, title=t),
            y=alt.Y("value:Q", stack="zero", title="contribution"),
            color=alt.Color("component:N", title=comp or "component"),
            tooltip=[
                alt.Tooltip("x", type=xtype),
                "component:N",
                alt.Tooltip("value:Q", format=".3s"),
            ],
        )
        .properties(width=s.width, height=s.height, title="Contribution over time")
    )
    bbase = alt.Chart()
    bars = alt.layer(
        bbase.mark_bar().encode(
            x=alt.X("mean:Q", title="total contribution"),
            y=alt.Y("component:N", sort="-x", title=None),
            color=alt.Color("component:N", legend=None),
            tooltip=[
                "component:N",
                alt.Tooltip("mean:Q", format=".3s"),
                alt.Tooltip("share:Q", format=".1%"),
            ],
        ),
        bbase.mark_rule(color="#333").encode(
            x="lo:Q", x2="hi:Q", y=alt.Y("component:N", sort="-x")
        ),
        data=named("totals"),
    ).properties(width=s.width, height=20 * len(tot), title="Total contribution")
    chart = alt.vconcat(area, bars)
    ranked = tot.sort_values("mean", ascending=False)
    shares = ", ".join(f"{r.component} {pct(r.share)}" for r in ranked.head(4).itertuples())
    caption = f"Over the whole period the outcome splits into: {shares}."
    unsure = [r.component for r in tot.itertuples() if r.lo < 0 < r.hi]
    if unsure:
        caption += (
            f" The contribution of {', '.join(map(str, unsure[:3]))} is uncertain "
            f"(its {pct(s.hdi_prob)} interval includes zero)."
        )
    if summed:
        caption += f" Summed over {', '.join(sorted(summed))}."
    return Figure(chart=chart, datasets={"decomposition": series, "totals": tot}, caption=caption)


@recipe(
    "core.decomposition",
    title="Decomposition",
    settings=DecompositionSettings,
    persona="both",
    requires_groups=["contributions"],
)
def decomposition(inp: RecipeInput, s: DecompositionSettings) -> Figure:
    """Stacked contributions over time plus each component's total and share."""
    return _decomposition(inp, s, None)


# ------------------------------------------------------------------------ counterfactual


class CounterfactualSettings(RecipeSettings):
    var: str | None = None
    along: str | None = None
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    bins: int = Field(30, ge=5, le=200)


def _series(ds: xr.Dataset, var: str, t: str, prob: float, label: str) -> pd.DataFrame:
    da = ds[var]
    if has_samples(da):
        st, _ = _reduce_draws(stack_samples(da), [t])
        a = (1 - prob) / 2
        point = st.median(SAMPLE)
        lo = st.quantile(a, SAMPLE).drop_vars("quantile")
        hi = st.quantile(1 - a, SAMPLE).drop_vars("quantile")
    else:
        extra = [d for d in da.dims if d not in (t, "stat")]
        if any(da.sizes[d] > 1 for d in extra):
            if "mean" not in [str(x) for x in da["stat"].values]:
                raise RecipeDataError(
                    f"summary {var!r} has extra dims {extra} and no mean; request an "
                    "aggregated output"
                )
            # means add up (intervals do not): show the summed mean without a band
            point = da.sel(stat="mean", drop=True).sum(extra)
            lo = hi = point
        else:
            da = da.squeeze(extra, drop=True) if extra else da
            b = summary_bands(da, [prob])
            _, lo_c, hi_c = interval_columns([prob])[0]
            point = b["point"]
            lo, hi = b.get(lo_c, point), b.get(hi_c, point)
    return pd.DataFrame(
        {
            "x": coord_values(point, t),
            "series": label,
            "point": point.values,
            "lo": lo.values,
            "hi": hi.values,
        }
    )


def _with_samples(ds: xr.Dataset | None, var: str | None) -> bool:
    if ds is None:
        return False
    names = [var] if var else list(map(str, ds.data_vars))
    return bool(names) and all(n in ds.data_vars and has_samples(ds[n]) for n in names)


@recipe(
    "core.counterfactual",
    title="Scenario vs baseline",
    settings=CounterfactualSettings,
    persona="both",
    summary="Scenario and baseline over time, and the distribution of the total difference.",
)
def counterfactual(inp: RecipeInput, s: CounterfactualSettings) -> Figure:
    """Scenario vs baseline bands and the distribution of the total change."""
    scen = next(
        (g for g in (inp.find("scenario", "draws", "predictions"),) if _with_samples(g, s.var)),
        None,
    ) or inp.find("summary")
    delta_ds = inp.find("delta/draws", "delta/predictions", "delta")
    base = inp.find("baseline/draws", "baseline/predictions", "baseline")
    if base is None or not _with_samples(base, s.var):
        if scen is not None and _with_samples(scen, s.var) and _with_samples(delta_ds, s.var):
            # score_bundle keeps baseline summaries only; the draws share the thinning index,
            # so baseline draws are exactly scenario draws minus delta draws
            assert delta_ds is not None
            base = scen - delta_ds
        else:
            base = inp.find("baseline/summary") or base
    if scen is None or base is None:
        raise RecipeDataError("a counterfactual needs scenario and baseline groups")
    var = pick_var(scen, s.var)
    if var not in base.data_vars:
        raise RecipeDataError(f"the baseline has no {var!r}")
    t = time_dim(scen[var], s.along)
    if t is None:
        raise RecipeDataError(f"{var!r} has no time-like dim; pass along=<dim>")
    lines = pd.concat(
        [
            _series(scen, var, t, s.hdi_prob, "scenario"),
            _series(base, var, t, s.hdi_prob, "baseline"),
        ],
        ignore_index=True,
    )
    total: np.ndarray | None = None
    if delta_ds is not None and var in delta_ds.data_vars and has_samples(delta_ds[var]):
        st = stack_samples(delta_ds[var])
        total = np.asarray(st.sum([d for d in st.dims if d != SAMPLE]).values, dtype=float)
    elif has_samples(scen[var]) and has_samples(base[var]):
        d = stack_samples(scen[var]) - stack_samples(base[var])
        total = np.asarray(d.sum([x for x in d.dims if x != SAMPLE]).values, dtype=float)
    temporal = np.issubdtype(lines["x"].dtype, np.datetime64)
    xtype: VlType = "temporal" if temporal else "quantitative"
    colors = alt.Scale(domain=["baseline", "scenario"], range=["#9aa5b1", "#e8710a"])
    lbase = alt.Chart()
    band = alt.layer(
        lbase.mark_area(opacity=0.25).encode(
            x=alt.X("x", type=xtype, title=t),
            y=alt.Y("lo:Q", title=var, scale=alt.Scale(zero=False)),
            y2="hi:Q",
            color=alt.Color("series:N", scale=colors, title=None),
        ),
        lbase.mark_line().encode(
            x=alt.X("x", type=xtype),
            y="point:Q",
            color=alt.Color("series:N", scale=colors),
            tooltip=[
                alt.Tooltip("x", type=xtype),
                "series:N",
                alt.Tooltip("point:Q", format=".3s"),
            ],
        ),
        data=named("lines"),
    ).properties(width=s.width, height=s.height, title="Scenario vs baseline")
    datasets: dict[str, pd.DataFrame] = {"lines": lines}
    charts: list[Any] = [band]
    if total is not None and total.size > 1:
        lo, hi = _stats.hdi(total, s.hdi_prob)
        mean = float(total.mean())
        p_up = float(np.mean(total > 0))
        counts, edges = np.histogram(total, bins=s.bins)
        datasets["delta"] = pd.DataFrame(
            {"bin_start": edges[:-1], "bin_end": edges[1:], "count": counts}
        )
        datasets["delta_summary"] = pd.DataFrame(
            {"mean": [mean], "lo": [float(lo)], "hi": [float(hi)], "p_increase": [p_up]}
        )
        hbase = alt.Chart(named("delta"))
        sbase = alt.Chart(named("delta_summary"))
        charts.append(
            alt.layer(
                hbase.mark_rect(color="#e8710a", opacity=0.7, stroke="white").encode(
                    x=alt.X("bin_start:Q", title=f"total change in {var}"),
                    x2="bin_end:Q",
                    y=alt.Y("count:Q", title="draws"),
                    y2=alt.datum(0),
                ),
                sbase.mark_rule(color="#333", strokeWidth=2).encode(x="mean:Q"),
                sbase.mark_rule(color="#333", strokeDash=[4, 4]).encode(x="lo:Q"),
                sbase.mark_rule(color="#333", strokeDash=[4, 4]).encode(x="hi:Q"),
            ).properties(width=s.width, height=s.height * 2 // 3, title="Total change")
        )
        caption = (
            f"The scenario changes total {var} by {fmt(mean)} ({pct(s.hdi_prob)} interval "
            f"{fmt(float(lo))} to {fmt(float(hi))}) compared with the baseline. Probability "
            f"of an increase: {pct(p_up)}."
        )
    else:
        diff = float(
            lines.loc[lines.series == "scenario", "point"].sum()
            - lines.loc[lines.series == "baseline", "point"].sum()
        )
        caption = (
            f"The scenario changes total {var} by about {fmt(diff)} compared with the baseline "
            "(no draws are available, so the uncertainty of the change is not shown)."
        )
    return Figure(chart=alt.vconcat(*charts), datasets=datasets, caption=caption)


# -------------------------------------------------------------------------------- decision


class DecisionSettings(RecipeSettings):
    var: str | None = None
    candidate: str | None = Field(None, description="Dim enumerating decision options")
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    higher_is_better: bool = True
    max_candidates: int = Field(12, ge=1, le=64)


@recipe(
    "core.decision_distribution",
    title="Decision outcome",
    settings=DecisionSettings,
    persona="both",
    requires_representation=[Representation.DRAWS],
    summary="Distribution of a scalar decision objective, per option when there are several.",
)
def decision_distribution(inp: RecipeInput, s: DecisionSettings) -> Figure:
    """Densities of the objective per option, intervals and the probability each is best."""
    ds = inp.require("decision", "objective", "decisions", "recommendation")
    var = pick_var(ds, s.var)
    st = stack_samples(ds[var])
    others = [str(d) for d in st.dims if d != SAMPLE]
    cand = s.candidate or (others[0] if len(others) == 1 else None)
    if cand is not None and cand not in st.dims:
        raise RecipeDataError(f"no dim {cand!r}")
    rest = [d for d in others if d != cand]
    if rest:
        raise RecipeDataError(f"the objective must be scalar per option; extra dims {rest}")
    if cand is None:
        labels = [var]
        mat = np.asarray(st.values, dtype=float)[:, None]
    else:
        labels = [str(x) for x in coord_values(st, cand)][: s.max_candidates]
        mat = np.asarray(st.transpose(SAMPLE, cand).values, dtype=float)[:, : len(labels)]
    best_idx = np.argmax(mat if s.higher_is_better else -mat, axis=1)
    rows, dens_parts = [], []
    lo_all, hi_all = np.nanpercentile(mat, [0.5, 99.5])
    grid = np.linspace(lo_all, hi_all if hi_all > lo_all else lo_all + 1, 150)
    for j, label in enumerate(labels):
        x = mat[:, j]
        lo, hi = _stats.hdi(x, s.hdi_prob)
        rows.append(
            {
                "option": label,
                "mean": float(np.mean(x)),
                "lo": float(lo),
                "hi": float(hi),
                "p_best": float(np.mean(best_idx == j)),
                "p_positive": float(np.mean(x > 0)),
            }
        )
        _, d = _stats.kde(x, grid)
        dens_parts.append(pd.DataFrame({"option": label, "x": grid, "density": d}))
    summary = pd.DataFrame(rows)
    dens = (
        alt.Chart(named("density"))
        .mark_area(opacity=0.35, line=True)
        .encode(
            x=alt.X("x:Q", title=var),
            y=alt.Y("density:Q", stack=None, title="density"),
            color=alt.Color("option:N", title=cand or None),
        )
        .properties(width=s.width, height=s.height, title="Outcome distribution")
    )
    ibase = alt.Chart()
    intervals = alt.layer(
        ibase.mark_rule().encode(
            x=alt.X("lo:Q", title=f"{var} ({pct(s.hdi_prob)} interval)"),
            x2="hi:Q",
            y=alt.Y("option:N", title=None),
            color=alt.Color("option:N", legend=None),
        ),
        ibase.mark_point(filled=True, size=60).encode(
            x="mean:Q",
            y="option:N",
            color=alt.Color("option:N", legend=None),
            tooltip=[
                "option:N",
                alt.Tooltip("mean:Q", format=".3s"),
                alt.Tooltip("p_best:Q", format=".0%"),
            ],
        ),
        data=named("summary"),
    ).properties(width=s.width, height=24 * len(summary))
    charts: list[Any] = [dens, intervals]
    if len(labels) > 1:
        charts.append(
            alt.Chart(named("summary"))
            .mark_bar()
            .encode(
                x=alt.X(
                    "p_best:Q", title="probability of being best", scale=alt.Scale(domain=[0, 1])
                ),
                y=alt.Y("option:N", sort="-x", title=None),
                tooltip=["option:N", alt.Tooltip("p_best:Q", format=".0%")],
            )
            .properties(width=s.width, height=24 * len(summary))
        )
        top = summary.sort_values("p_best", ascending=False).iloc[0]
        caption = (
            f"{top.option} is the best option with probability {pct(top.p_best)}; its expected "
            f"{var} is {fmt(top['mean'])} ({pct(s.hdi_prob)} interval {fmt(top.lo)} to "
            f"{fmt(top.hi)})."
        )
    else:
        r = summary.iloc[0]
        caption = (
            f"The expected {var} is {fmt(r['mean'])} ({pct(s.hdi_prob)} interval {fmt(r.lo)} to "
            f"{fmt(r.hi)}); the chance it is below zero is {pct(1 - r.p_positive)}."
        )
    datasets = {"density": pd.concat(dens_parts, ignore_index=True), "summary": summary}
    return Figure(chart=alt.vconcat(*charts), datasets=datasets, caption=caption)
