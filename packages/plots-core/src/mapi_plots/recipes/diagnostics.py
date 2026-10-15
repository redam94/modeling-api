"""Inference diagnostics: prior predictive, convergence, posterior forest and PPC."""

from __future__ import annotations

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
    named,
    recipe,
)
from mapi_plots.recipes._util import (
    SAMPLE,
    check_levels,
    coord_values,
    flatten_samples,
    interval_columns,
    iter_elements,
    observed_name,
    pct,
    pick_var,
    stack_samples,
    time_dim,
)

__all__ = [
    "ConvergenceSettings",
    "ForestSettings",
    "PPCSettings",
    "PriorPredictiveSettings",
    "convergence",
    "posterior_forest",
    "ppc",
    "prior_predictive",
]

_LEVELS_DOC = "Central interval levels to shade, e.g. [0.5, 0.9]"


def _matched(
    pred: xr.Dataset, obs: xr.Dataset | None, var: str, observed_var: str | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """``(draws (S, N), observed (N,))`` over the cells both share, NaN cells dropped."""
    name = observed_name(obs, var, observed_var)
    if obs is None or name is None:
        raise RecipeDataError(f"no observed values for {var!r} (observed_data group)")
    p = stack_samples(pred[var])
    o = obs[name]
    p, o = xr.align(p, o, join="inner", exclude=[SAMPLE])
    o = o.transpose(*[d for d in p.dims if d != SAMPLE and d in o.dims])
    extra = [d for d in p.dims if d != SAMPLE and d not in o.dims]
    if extra:
        raise RecipeDataError(f"observed {var!r} lacks the dims {extra}")
    draws = np.asarray(p.values, dtype=float).reshape(p.sizes[SAMPLE], -1)
    y = np.asarray(o.values, dtype=float).ravel()
    keep = np.isfinite(y) & np.all(np.isfinite(draws), axis=0)
    if not keep.any():
        raise RecipeDataError(f"no finite observations of {var!r} to compare")
    return draws[:, keep], y[keep]


def _density_frame(
    draws: np.ndarray, observed: np.ndarray | None, num_draws: int, label: str, points: int = 120
) -> pd.DataFrame:
    pool = draws[np.isfinite(draws)]
    lo, hi = np.percentile(pool, [0.5, 99.5])
    if observed is not None and observed.size:
        lo, hi = min(lo, float(np.min(observed))), max(hi, float(np.max(observed)))
    if hi <= lo:
        hi = lo + 1.0
    grid = np.linspace(lo, hi, points)
    idx = np.unique(np.linspace(0, draws.shape[0] - 1, min(num_draws, draws.shape[0])).astype(int))
    parts = []
    for i in idx:
        _, dens = _stats.kde(draws[i], grid)
        parts.append(pd.DataFrame({"draw": int(i), "x": grid, "density": dens, "series": label}))
    _, mean_dens = _stats.kde(pool[np.isfinite(pool)], grid)
    parts.append(
        pd.DataFrame({"draw": -1, "x": grid, "density": mean_dens, "series": f"{label} (all)"})
    )
    if observed is not None and observed.size:
        _, od = _stats.kde(observed, grid)
        parts.append(pd.DataFrame({"draw": -2, "x": grid, "density": od, "series": "observed"}))
    return pd.concat(parts, ignore_index=True)


def _density_chart(name: str, label: str, var: str, s: RecipeSettings) -> Any:
    colors = alt.Scale(
        domain=[label, f"{label} (all)", "observed"], range=["#9aa5b1", "#3366cc", "#111111"]
    )
    return (
        alt.Chart(named(name))
        .mark_line(strokeWidth=1)
        .encode(
            x=alt.X("x:Q", title=var),
            y=alt.Y("density:Q", title="density"),
            detail="draw:N",
            color=alt.Color("series:N", scale=colors, title=None),
            opacity=alt.condition(alt.datum.draw >= 0, alt.value(0.25), alt.value(1.0)),
            strokeWidth=alt.condition(alt.datum.draw >= 0, alt.value(0.8), alt.value(2.2)),
        )
        .properties(width=s.width, height=s.height)
    )


# --------------------------------------------------------------------- prior predictive


class PriorPredictiveSettings(RecipeSettings):
    var: str | None = None
    num_draws: int = Field(50, ge=1, le=500)
    along: str | None = None
    levels: list[float] = Field(default_factory=lambda: [0.5, 0.9], description=_LEVELS_DOC)

    @field_validator("levels")
    @classmethod
    def _levels(cls, v: list[float]) -> list[float]:
        return check_levels(v)


@recipe(
    "core.prior_predictive",
    title="Prior predictive check",
    settings=PriorPredictiveSettings,
    persona="expert",
    requires_groups=["prior_predictive"],
)
def prior_predictive(inp: RecipeInput, s: PriorPredictiveSettings) -> Figure:
    """Datasets the model can generate before seeing data, against the observed data."""
    prior = inp.require("prior_predictive")
    var = pick_var(prior, s.var)
    obs_ds = inp.find("observed_data", "observed")
    obs = obs_ds[var] if obs_ds is not None and var in obs_ds.data_vars else None
    draws = flatten_samples(prior[var])
    observed = None if obs is None else np.asarray(obs.values, dtype=float).ravel()
    if observed is not None:
        observed = observed[np.isfinite(observed)]
    datasets = {"density": _density_frame(draws, observed, s.num_draws, "prior draw")}
    charts: list[Any] = [_density_chart("density", "prior draw", var, s)]
    t = time_dim(prior[var], s.along)
    if t is not None:
        da = stack_samples(prior[var])
        other = [d for d in da.dims if d not in (SAMPLE, t)]
        mean = da.mean(other) if other else da
        q = {"point": mean.median(SAMPLE)}
        for lv, lo_c, hi_c in interval_columns(s.levels):
            a = (1 - lv) / 2
            q[lo_c] = mean.quantile(a, SAMPLE).drop_vars("quantile")
            q[hi_c] = mean.quantile(1 - a, SAMPLE).drop_vars("quantile")
        fan = pd.DataFrame({"x": coord_values(da, t), **{k: v.values for k, v in q.items()}})
        if obs is not None and t in obs.dims:
            o = obs.mean([d for d in obs.dims if d != t]) if obs.ndim > 1 else obs
            fan["observed"] = o.reindex({t: da[t]}).values if t in da.coords else o.values
        datasets["fan"] = fan
        charts.append(_fan_chart("fan", t, var, s.levels, "observed" in fan, s, mean_over=other))
    caption = (
        f"Grey curves are datasets of {var} the model could generate before seeing any data; "
        "the black curve is your data. Your data should look like a plausible member of the "
        "grey family."
    )
    if obs is not None:
        pr = stack_samples(prior[var])
        pr, o = xr.align(pr, obs, join="inner", exclude=[SAMPLE])
        lo, hi = _stats.eti(np.asarray(pr.values, dtype=float), 0.9, axis=0)
        y = np.asarray(o.transpose(*[d for d in pr.dims if d != SAMPLE]).values, dtype=float)
        ok = np.isfinite(y)
        inside = float(np.mean((y >= lo) & (y <= hi), where=ok)) if ok.any() else float("nan")
        width = float(np.nanmedian(hi - lo))
        data_range = float(np.nanmax(y) - np.nanmin(y)) if ok.any() else float("nan")
        caption += f" {pct(inside)} of observations fall inside the prior 90% range."
        if np.isfinite(inside) and inside < 0.5:
            caption += " The priors may be too narrow or on the wrong scale."
        elif data_range > 0 and width > 100 * data_range:
            caption += " The priors are very wide compared with the data; consider tightening them."
    return Figure(chart=alt.vconcat(*charts), datasets=datasets, caption=caption)


def _fan_chart(
    name: str,
    t: str,
    var: str,
    levels: list[float],
    with_obs: bool,
    s: RecipeSettings,
    *,
    mean_over: list[Any],
) -> Any:
    base = alt.Chart(named(name))
    layers: list[Any] = []
    for i, (_, lo_c, hi_c) in enumerate(interval_columns(levels)):
        layers.append(
            base.mark_area(opacity=0.18 + 0.12 * i, color="#3366cc").encode(
                x=alt.X("x:T", title=t), y=alt.Y(f"{lo_c}:Q", title=var), y2=f"{hi_c}:Q"
            )
        )
    layers.append(base.mark_line(color="#3366cc").encode(x="x:T", y="point:Q"))
    if with_obs:
        layers.append(base.mark_circle(size=14, color="#111111").encode(x="x:T", y="observed:Q"))
    title = f"Prior predictive {var} over {t}"
    if mean_over:
        title += f" (mean over {', '.join(map(str, mean_over))})"
    return alt.layer(*layers).properties(width=s.width, height=s.height, title=title)


# --------------------------------------------------------------------------- convergence


class ConvergenceSettings(RecipeSettings):
    var_names: list[str] | None = Field(None, max_length=200)
    max_parameters: int = Field(40, ge=1, le=500)
    max_traces: int = Field(6, ge=1, le=24)
    max_draws: int = Field(400, ge=10, le=5000)
    max_elements: int = Field(2000, ge=1, le=20000)
    rhat_threshold: float = Field(1.01, gt=1.0, le=2.0)
    ess_per_chain: float = Field(100.0, gt=0, le=10_000)
    bins: int = Field(20, ge=4, le=100)
    table_rows: int = Field(15, ge=1, le=100)


def _posterior(inp: RecipeInput, var_names: list[str] | None) -> tuple[xr.Dataset, list[str]]:
    post = inp.require("posterior")
    if not {"chain", "draw"} <= set(post.dims):
        raise RecipeDataError("the posterior needs (chain, draw) dims")
    names = var_names or [str(v) for v in post.data_vars if np.issubdtype(post[v].dtype, np.number)]
    unknown = [n for n in names if n not in post.data_vars]
    if unknown:
        raise RecipeDataError(f"unknown posterior variables: {', '.join(unknown)}")
    if not names:
        raise RecipeDataError("the posterior has no numeric variables")
    return post, names


@recipe(
    "core.convergence",
    title="Sampler convergence",
    settings=ConvergenceSettings,
    persona="expert",
    requires_groups=["posterior"],
    requires_representation=[Representation.DRAWS],
)
def convergence(inp: RecipeInput, s: ConvergenceSettings) -> Figure:
    """R-hat and ESS per parameter, a table of the worst ones, trace and rank plots."""
    post, names = _posterior(inp, s.var_names)
    chains = post.sizes["chain"]
    ess_thr = s.ess_per_chain * chains
    rows: list[dict[str, Any]] = []
    arrays: dict[str, np.ndarray] = {}
    truncated = False
    for name in names:
        da = post[name].transpose("chain", "draw", ...)
        for label, sub in iter_elements(da, ("chain", "draw")):
            if len(rows) >= s.max_elements:
                truncated = True
                break
            x = np.asarray(sub.transpose("chain", "draw").values, dtype=float)
            r, eb, et = _stats.rhat(x), _stats.ess_bulk(x), _stats.ess_tail(x)
            bad = (np.isfinite(r) and r > s.rhat_threshold) or (
                np.isfinite(eb) and min(eb, et if np.isfinite(et) else eb) < ess_thr
            )
            rows.append(
                {
                    "parameter": label,
                    "variable": name,
                    "rhat": r,
                    "ess_bulk": eb,
                    "ess_tail": et,
                    "status": "check" if bad else "ok",
                }
            )
            arrays[label] = x
    diag = pd.DataFrame(rows)
    diag["_sort"] = diag["rhat"].fillna(-np.inf)
    diag = diag.sort_values("_sort", ascending=False).drop(columns="_sort").reset_index(drop=True)
    shown = diag.head(s.max_parameters).copy()

    status_scale = alt.Scale(domain=["ok", "check"], range=["#2e7d32", "#c62828"])
    height = max(60, 14 * len(shown))
    base = alt.Chart(named("diagnostics"))
    rhat_chart = alt.layer(
        base.mark_point(filled=True).encode(
            x=alt.X("rhat:Q", scale=alt.Scale(zero=False), title="R-hat"),
            y=alt.Y("parameter:N", sort=None, title=None),
            color=alt.Color("status:N", scale=status_scale, title=None),
            tooltip=["parameter:N", alt.Tooltip("rhat:Q", format=".4f")],
        ),
        base.mark_rule(strokeDash=[4, 4], color="#c62828").encode(x=alt.datum(s.rhat_threshold)),
    ).properties(width=s.width // 2, height=height, title="R-hat (lower is better)")
    ess_chart = alt.layer(
        base.mark_point(filled=True).encode(
            x=alt.X("ess_bulk:Q", title="bulk ESS"),
            y=alt.Y("parameter:N", sort=None, title=None, axis=None),
            color=alt.Color("status:N", scale=status_scale, title=None),
            tooltip=[
                "parameter:N",
                alt.Tooltip("ess_bulk:Q", format=".0f"),
                alt.Tooltip("ess_tail:Q", format=".0f"),
            ],
        ),
        base.mark_rule(strokeDash=[4, 4], color="#c62828").encode(x=alt.datum(ess_thr)),
    ).properties(width=s.width // 2, height=height, title="Effective sample size")

    top = diag.head(s.table_rows)
    # Left edge of each column as a fraction of the table width: the parameter label needs
    # most of the room (band-scaled columns made long labels run into the numbers).
    columns = {"parameter": 0.0, "R-hat": 0.5, "ESS bulk": 0.63, "ESS tail": 0.76, "status": 0.89}
    cells = [
        {"row": -1, "column": col, "x": x, "text": col, "status": "header"}
        for col, x in columns.items()
    ]
    for i, r in enumerate(top.itertuples(index=False)):
        for col, text in (
            ("parameter", str(r.parameter)),
            ("R-hat", f"{r.rhat:.3f}" if np.isfinite(r.rhat) else "n/a"),
            ("ESS bulk", f"{r.ess_bulk:.0f}" if np.isfinite(r.ess_bulk) else "n/a"),
            ("ESS tail", f"{r.ess_tail:.0f}" if np.isfinite(r.ess_tail) else "n/a"),
            ("status", str(r.status)),
        ):
            cells.append(
                {"row": i, "column": col, "x": columns[col], "text": text, "status": r.status}
            )
    cell = alt.Chart(named("table")).encode(
        x=alt.X("x:Q", axis=None, scale=alt.Scale(domain=[0, 1], nice=False)),
        y=alt.Y("row:O", axis=None),
        text="text:N",
    )
    table = alt.layer(
        cell.transform_filter(alt.FieldEqualPredicate(field="status", equal="header")).mark_text(
            align="left", baseline="middle", fontSize=11, fontWeight="bold", color="#666"
        ),
        cell.transform_filter(alt.FieldOneOfPredicate(field="status", oneOf=["ok", "check"]))
        .mark_text(align="left", baseline="middle", fontSize=11, limit=int(s.width * 0.48))
        .encode(color=alt.Color("status:N", scale=status_scale, legend=None)),
    ).properties(width=s.width, height=16 * (len(top) + 1), title="Worst parameters")

    trace_params = list(diag["parameter"].head(s.max_traces))
    trace_parts, rank_parts = [], []
    for p in trace_params:
        x = arrays[p]
        n_draw = x.shape[1]
        keep = np.unique(np.linspace(0, n_draw - 1, min(s.max_draws, n_draw)).astype(int))
        for c in range(x.shape[0]):
            trace_parts.append(
                pd.DataFrame({"parameter": p, "chain": c, "draw": keep, "value": x[c, keep]})
            )
        ranks = _stats._rankdata(x)
        edges = np.linspace(0.5, x.size + 0.5, s.bins + 1)
        expected = x.shape[1] / s.bins
        for c in range(x.shape[0]):
            counts, _ = np.histogram(ranks[c], bins=edges)
            rank_parts.append(
                pd.DataFrame(
                    {
                        "parameter": p,
                        "chain": c,
                        "bin": np.arange(s.bins),
                        "count": counts,
                        "expected": expected,
                    }
                )
            )
    trace = (
        alt.Chart(named("trace"))
        .mark_line(strokeWidth=0.7, opacity=0.8)
        .encode(
            x=alt.X("draw:Q", title="draw"),
            y=alt.Y("value:Q", title=None, scale=alt.Scale(zero=False)),
            color=alt.Color("chain:N"),
        )
        .properties(width=s.width // 2, height=70)
        .facet(row=alt.Row("parameter:N", title=None, sort=trace_params))
        .resolve_scale(y="independent")
        .properties(title="Trace (worst R-hat first)")
    )
    rbase = alt.Chart()
    rank = (
        alt.layer(
            rbase.mark_line(interpolate="step", strokeWidth=1).encode(
                x=alt.X("bin:Q", title="rank bin"), y=alt.Y("count:Q", title=None), color="chain:N"
            ),
            rbase.mark_rule(strokeDash=[3, 3], color="#666").encode(y="mean(expected):Q"),
            data=named("rank"),
        )
        .properties(width=s.width // 2, height=70)
        .facet(row=alt.Row("parameter:N", title=None, sort=trace_params))
        .properties(title="Rank plot (flat = well mixed)")
    )
    chart = alt.vconcat(alt.hconcat(rhat_chart, ess_chart), table, alt.hconcat(trace, rank))

    n_bad = int((diag["status"] == "check").sum())
    n = len(diag)
    if n_bad == 0:
        caption = (
            f"All {n} parameters look converged: R-hat is at most {s.rhat_threshold} and the "
            f"effective sample size is at least {ess_thr:.0f} for each."
        )
    else:
        worst = ", ".join(diag.loc[diag["status"] == "check", "parameter"].head(3))
        caption = (
            f"{n_bad} of {n} parameters have not converged (R-hat above {s.rhat_threshold} or "
            f"fewer than {ess_thr:.0f} effective samples), for example {worst}. Run the sampler "
            "longer, increase tuning or reparameterise before trusting the results."
        )
    stats = inp.find("sample_stats")
    if stats is not None and "diverging" in stats.data_vars:
        div = int(np.asarray(stats["diverging"].values).astype(bool).sum())
        if div:
            caption += f" There were {div} divergent transitions; estimates may be biased."
        else:
            caption += " There were no divergent transitions."
    if truncated:
        caption += f" Only the first {s.max_elements} parameter elements were checked."
    datasets = {
        "diagnostics": shown,
        "table": pd.DataFrame(cells),
        "trace": pd.concat(trace_parts, ignore_index=True),
        "rank": pd.concat(rank_parts, ignore_index=True),
    }
    return Figure(chart=chart, datasets=datasets, caption=caption)


# ------------------------------------------------------------------------------- forest


class ForestSettings(RecipeSettings):
    var_names: list[str] | None = Field(None, max_length=200)
    hdi_prob: float = Field(0.94, gt=0, lt=1)
    inner_prob: float = Field(0.5, gt=0, lt=1)
    point: Literal["median", "mean"] = "median"
    max_rows: int = Field(60, ge=1, le=500)


@recipe(
    "core.posterior_forest",
    title="Posterior forest",
    settings=ForestSettings,
    persona="expert",
    requires_groups=["posterior"],
    requires_representation=[Representation.DRAWS],
)
def posterior_forest(inp: RecipeInput, s: ForestSettings) -> Figure:
    """Point estimate, inner interval and HDI for every parameter element."""
    post, names = _posterior(inp, s.var_names)
    rows: list[dict[str, Any]] = []
    for name in names:
        da = post[name]
        for label, sub in iter_elements(da, ("chain", "draw")):
            if len(rows) >= s.max_rows:
                break
            x = np.asarray(sub.values, dtype=float).ravel()
            x = x[np.isfinite(x)]
            if x.size < 2:
                continue
            lo, hi = _stats.hdi(x, s.hdi_prob)
            ilo, ihi = _stats.eti(x, s.inner_prob)
            rows.append(
                {
                    "parameter": label,
                    "variable": name,
                    "point": float(np.median(x) if s.point == "median" else np.mean(x)),
                    "lo": float(lo),
                    "hi": float(hi),
                    "lo_inner": float(ilo),
                    "hi_inner": float(ihi),
                    "order": len(rows),
                }
            )
    if not rows:
        raise RecipeDataError("no parameter has enough finite draws")
    forest = pd.DataFrame(rows)
    y = alt.Y("parameter:N", sort=alt.SortField("order"), title=None)
    base = alt.Chart()
    layers = [
        base.mark_rule().encode(
            x=alt.X("lo:Q", title="value", scale=alt.Scale(zero=False)),
            x2="hi:Q",
            y=y,
            color="variable:N",
        ),
        base.mark_rule(strokeWidth=4).encode(
            x="lo_inner:Q", x2="hi_inner:Q", y=y, color="variable:N"
        ),
        base.mark_point(filled=True, color="white", stroke="black", size=40).encode(
            x="point:Q",
            y=y,
            tooltip=[
                "parameter:N",
                alt.Tooltip("point:Q", format=".3g"),
                alt.Tooltip("lo:Q", format=".3g"),
                alt.Tooltip("hi:Q", format=".3g"),
            ],
        ),
    ]
    spans = forest.groupby("variable").agg(lo=("lo", "min"), hi=("hi", "max"))
    crossing = set(spans.index[(spans["lo"] < 0) & (spans["hi"] > 0)])
    forest["zero"] = np.where(forest["variable"].isin(crossing), 0.0, np.nan)
    if crossing:  # a zero reference only where the variable's intervals straddle zero
        layers.insert(0, base.mark_rule(color="#999", strokeDash=[3, 3]).encode(x="zero:Q"))
    layer = alt.layer(*layers).properties(width=s.width, height=alt.Step(16))
    if forest["variable"].nunique() > 1:
        # one row per variable, each on its own value scale (alpha ~ 100, beta ~ 0.5 ...)
        chart = layer.facet(
            row=alt.Row("variable:N", title=None), data=named("forest")
        ).resolve_scale(x="independent", y="independent")
    else:
        chart = alt.layer(*layers, data=named("forest")).properties(
            width=s.width, height=alt.Step(16)
        )
    excl = int(((forest["lo"] > 0) | (forest["hi"] < 0)).sum())
    caption = (
        f"Each row is a model parameter: the dot is the posterior {s.point}, the thick bar the "
        f"central {pct(s.inner_prob)} interval and the thin line the {pct(s.hdi_prob)} "
        f"highest-density interval. {excl} of {len(forest)} intervals exclude zero."
    )
    return Figure(chart=chart, datasets={"forest": forest}, caption=caption)


# ----------------------------------------------------------------------------------- PPC


class PPCSettings(RecipeSettings):
    var: str | None = None
    observed_var: str | None = None
    num_draws: int = Field(50, ge=1, le=500)
    bins: int = Field(20, ge=4, le=100)
    levels: list[float] = Field(
        default_factory=lambda: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
    )

    @field_validator("levels")
    @classmethod
    def _levels(cls, v: list[float]) -> list[float]:
        return check_levels(v)


@recipe(
    "core.ppc",
    title="Posterior predictive check",
    settings=PPCSettings,
    persona="expert",
    requires_groups=["posterior_predictive", "observed_data"],
    requires_representation=[Representation.DRAWS],
)
def ppc(inp: RecipeInput, s: PPCSettings) -> Figure:
    """Predictive densities against the data, the PIT histogram and interval coverage."""
    pred = inp.require("posterior_predictive", "predictions")
    obs = inp.find("observed_data", "observed")
    var = pick_var(pred, s.var)
    draws, y = _matched(pred, obs, var, s.observed_var)
    density = _density_frame(draws, y, s.num_draws, "predictive draw")
    pit = _stats.pit_values(draws, y)
    counts, edges = np.histogram(pit, bins=s.bins, range=(0.0, 1.0))
    n = y.size
    p = 1.0 / s.bins
    sd = np.sqrt(n * p * (1 - p))
    pit_frame = pd.DataFrame(
        {
            "bin_start": edges[:-1],
            "bin_end": edges[1:],
            "count": counts,
            "expected": n * p,
            "lo": max(0.0, n * p - 1.96 * sd),
            "hi": n * p + 1.96 * sd,
        }
    )
    cov = pd.DataFrame(_stats.coverage_curve(draws, y, s.levels), columns=["nominal", "empirical"])

    dens_chart = _density_chart("density", "predictive draw", var, s).properties(
        title="Predicted vs observed distribution"
    )
    pbase = alt.Chart()
    pit_chart = alt.layer(
        pbase.mark_rect(color="#e0e0e0").encode(
            x="bin_start:Q", x2="bin_end:Q", y="lo:Q", y2="hi:Q"
        ),
        pbase.mark_rect(opacity=0.8, stroke="white", strokeWidth=0.5).encode(
            x=alt.X("bin_start:Q", title="PIT", scale=alt.Scale(domain=[0, 1])),
            x2="bin_end:Q",
            y=alt.Y("count:Q", title="observations"),
            y2=alt.datum(0),
        ),
        pbase.mark_rule(strokeDash=[4, 4]).encode(y="mean(expected):Q"),
        data=named("pit"),
    ).properties(width=s.width // 2, height=s.height, title="PIT (flat = calibrated)")
    cbase = alt.Chart()
    cov_chart = alt.layer(
        cbase.mark_line(color="#999", strokeDash=[4, 4]).encode(
            x=alt.X("nominal:Q", title="nominal coverage", scale=alt.Scale(domain=[0, 1])),
            y=alt.Y("nominal:Q", title="observed coverage", scale=alt.Scale(domain=[0, 1])),
        ),
        cbase.mark_line(point=True).encode(
            x="nominal:Q",
            y="empirical:Q",
            tooltip=[
                alt.Tooltip("nominal:Q", format=".0%"),
                alt.Tooltip("empirical:Q", format=".0%"),
            ],
        ),
        data=named("coverage"),
    ).properties(width=s.width // 2, height=s.height, title="Interval coverage")
    chart = alt.vconcat(dens_chart, alt.hconcat(pit_chart, cov_chart))

    c90 = dict(_stats.coverage_curve(draws, y, [0.9]))[0.9]
    caption = (
        f"{pct(c90)} of observations of {var} fall inside the model's 90% predictive interval "
        "(ideal: about 90%)."
    )
    mean_pit = float(np.mean(pit))
    if mean_pit < 0.4:
        caption += " Observations tend to sit below the predictions: the model over-predicts."
    elif mean_pit > 0.6:
        caption += " Observations tend to sit above the predictions: the model under-predicts."
    tails = float(np.mean((pit < 0.05) | (pit > 0.95)))
    if tails > 0.2:
        caption += " Too many observations fall in the tails: the intervals are too narrow."
    elif tails < 0.02 and n >= 50:
        caption += " Very few observations fall in the tails: the intervals may be too wide."
    datasets = {"density": density, "pit": pit_frame, "coverage": cov}
    return Figure(chart=chart, datasets=datasets, caption=caption)
