"""More core recipes (Vega-Lite): a corner plot, ridgelines, rankings, calibration, states.

============================  ==========================================================
``core.pair``                 corner plot of a few scalar parameters, divergences in red
``core.ridge``                one density per level of a dim, ordered by mean, HDI ticks
``core.rank_matrix``          P(rank = k) per item and the pairwise P(i above j) matrix
``core.calibration``          PIT-ECDF difference with a simultaneous envelope, or a
                              reliability diagram for 0/1 targets (draws or point
                              probabilities)
``core.state_sequence``       observed series over the stacked state probabilities (HMM
                              ``hidden_states``, Markov ``regimes``, draws, summaries)
============================  ==========================================================
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import NormalDist
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
    EMPTY,
    ESTIMATE,
    MUTED,
    NEGATIVE,
    OBSERVED,
    OK,
    POSITIVE,
    REFERENCE,
    SCENARIO,
    STRONG,
    SURFACE,
)
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
    coord_values,
    fmt,
    iter_elements,
    pct,
    sample_dims,
    stack_samples,
    time_dim,
)
from mapi_plots.recipes.classification import binary_scores
from mapi_plots.recipes.diagnostics import _matched, _posterior

__all__ = [
    "CalibrationSettings",
    "PairSettings",
    "RankMatrixSettings",
    "RidgeSettings",
    "StateSequenceSettings",
    "calibration",
    "pair",
    "rank_matrix",
    "ridge",
    "state_sequence",
]

GroupName = Literal["auto", "posterior", "predictions", "posterior_predictive"]
_DRAW_GROUPS: tuple[str, ...] = ("posterior", "predictions", "posterior_predictive")
# distinct swatches for categories (states); they cycle after ten
_CATEGORY = [BAND, SCENARIO, OK, ALERT, STRONG, POSITIVE, BASELINE, ESTIMATE, MUTED, REFERENCE]


def _thin(n: int, k: int) -> np.ndarray:
    """At most ``k`` evenly spaced indices of ``range(n)``."""
    return np.unique(np.linspace(0, n - 1, min(n, k)).round().astype(int))


def _find(
    inp: RecipeInput,
    var: str | None,
    group: str,
    order: Sequence[str],
    *,
    need: str | None = None,
    draws: bool = True,
) -> tuple[xr.DataArray, str]:
    """``(var, group name)``: the first group in ``order`` (or ``group``) that has ``var``.

    Without ``var``: the first variable with draws (``draws``) that has the dim ``need``.
    """
    groups = tuple(order) if group == "auto" else (group,)
    seen: list[str] = []
    for g in groups:
        ds = inp.find(g)
        if ds is None:
            continue
        names = [str(v) for v in ds.data_vars if np.issubdtype(ds[v].dtype, np.number)]
        if var is not None:
            if var in ds.data_vars:
                return ds[var], g
            seen += names
            continue
        seen += names
        for n in names:
            if draws and not sample_dims(ds[n]):
                continue
            if need is not None and need not in ds[n].dims:
                continue
            return ds[n], g
    if var is not None:
        raise RecipeDataError(
            f"no variable {var!r} in the groups {', '.join(groups)} "
            f"(variables: {', '.join(dict.fromkeys(seen)) or 'none'})"
        )
    what = f"draws with the dim {need!r}" if need else "draws"
    raise RecipeDataError(
        f"no variable with {what} in the groups {', '.join(groups)} "
        "(expected chain/draw or sample dims)"
    )


def _select(da: xr.DataArray, select: Mapping[str, Any] | None) -> xr.DataArray:
    """Pick one level of each dim in ``select`` (by label, else by position)."""
    for dim, value in (select or {}).items():
        if dim not in da.dims:
            raise RecipeDataError(f"select: no dim {dim!r} (dims: {', '.join(map(str, da.dims))})")
        try:
            da = da.sel({dim: value})
        except (KeyError, TypeError, ValueError):
            if isinstance(value, int) and -da.sizes[dim] <= value < da.sizes[dim]:
                da = da.isel({dim: value})
            else:
                raise RecipeDataError(f"select: {dim!r} has no level {value!r}") from None
    return da


def _squeeze_except(da: xr.DataArray, keep: Sequence[str]) -> xr.DataArray:
    """Drop length-1 dims outside ``keep``; a longer one is an error (use ``select``)."""
    extra = [str(d) for d in da.dims if d not in keep]
    big = [d for d in extra if da.sizes[d] > 1]
    if big:
        raise RecipeDataError(
            f"{da.name!r} has the extra dims {', '.join(big)}: pick one level of each with "
            "`select`, for example select: {" + f"{big[0]}: 0" + "}"
        )
    return da.squeeze([d for d in extra], drop=True) if extra else da


def _labels(da: xr.DataArray, dim: str) -> list[str]:
    return [str(v) for v in coord_values(da, dim)]


# ----------------------------------------------------------------------------------- pair


class PairSettings(RecipeSettings):
    var_names: list[str] | None = Field(None, max_length=200)
    max_params: int = Field(5, ge=2, le=8)
    max_draws: int = Field(500, ge=20, le=4000)
    bins: int = Field(20, ge=5, le=80)
    width: int = Field(520, ge=80, le=2000)


@recipe(
    "core.pair",
    title="Pair plot",
    settings=PairSettings,
    persona="expert",
    requires_groups=["posterior"],
    requires_representation=[Representation.DRAWS],
    summary="A grid of the draws of each pair of parameters, with a histogram of each "
    "parameter. Red points are divergent transitions.",
    tags=["diagnostic"],
)
def pair(inp: RecipeInput, s: PairSettings) -> Figure:
    """Corner plot: 2-D scatter of thinned draws per pair, 1-D histograms on the diagonal."""
    post, names = _posterior(inp, s.var_names)
    labels: list[str] = []
    columns: list[np.ndarray] = []
    for name in names:
        da = post[name].transpose("chain", "draw", ...)
        for label, sub in iter_elements(da, ("chain", "draw")):
            if len(labels) >= s.max_params:
                break
            x = np.asarray(sub.transpose("chain", "draw").values, dtype=float).ravel()
            if np.isfinite(x).sum() < 2 or np.nanstd(x) == 0:
                continue
            labels.append(label)
            columns.append(x)
    if len(labels) < 2:
        raise RecipeDataError("a pair plot needs at least two parameters that vary")
    k = len(labels)
    values = np.stack(columns, axis=1)  # (chain * draw, k)
    n = values.shape[0]
    divergent = np.zeros(n, dtype=bool)
    stats = inp.find("sample_stats")
    if stats is not None and "diverging" in stats.data_vars:
        dv = stats["diverging"]
        if {"chain", "draw"} <= set(dv.dims) and dv.size == n:
            divergent = np.asarray(dv.transpose("chain", "draw").values).astype(bool).ravel()
    keep = np.union1d(_thin(n, s.max_draws), np.flatnonzero(divergent)[: s.max_draws])
    draws = pd.DataFrame({f"p{i}": values[keep, i] for i in range(k)})
    draws["transition"] = np.where(divergent[keep], "divergent", "regular")

    hist_parts, domains = [], []
    for i in range(k):
        x = values[:, i][np.isfinite(values[:, i])]
        lo, hi = float(x.min()), float(x.max())
        domains.append([lo, hi])
        counts, edges = np.histogram(x, bins=s.bins, range=(lo, hi))
        hist_parts.append(
            pd.DataFrame(
                {"param": f"p{i}", "bin_start": edges[:-1], "bin_end": edges[1:], "count": counts}
            )
        )
    hist = pd.concat(hist_parts, ignore_index=True)

    cell = max(60, s.width // k)
    rows = []
    for r in range(k):
        row = []
        for c in range(r + 1):
            x_title = labels[c] if r == k - 1 else None
            if c == r:
                ch = (
                    alt.Chart(named("hist"))
                    .transform_filter(alt.FieldEqualPredicate(field="param", equal=f"p{c}"))
                    .mark_rect(color=BAND, opacity=0.7, stroke=SURFACE, strokeWidth=0.5)
                    .encode(
                        x=alt.X(
                            "bin_start:Q",
                            title=x_title,
                            scale=alt.Scale(domain=domains[c], nice=False, zero=False),
                        ),
                        x2="bin_end:Q",
                        y=alt.Y("count:Q", axis=None),
                        y2=alt.datum(0),
                    )
                )
            else:
                y_title = labels[r] if c == 0 else None
                base = alt.Chart(named("draws")).encode(
                    x=alt.X(
                        f"p{c}:Q",
                        title=x_title,
                        scale=alt.Scale(domain=domains[c], nice=False, zero=False),
                    ),
                    y=alt.Y(
                        f"p{r}:Q",
                        title=y_title,
                        scale=alt.Scale(domain=domains[r], nice=False, zero=False),
                    ),
                )
                ch = alt.layer(
                    base.transform_filter(
                        alt.FieldEqualPredicate(field="transition", equal="regular")
                    ).mark_circle(size=6, opacity=0.35, color=BAND),
                    base.transform_filter(
                        alt.FieldEqualPredicate(field="transition", equal="divergent")
                    ).mark_circle(size=16, opacity=0.9, color=ALERT),
                )
            row.append(ch.properties(width=cell, height=cell))
        rows.append(alt.hconcat(*row, spacing=4))
    chart = alt.vconcat(*rows, spacing=4)

    finite = np.all(np.isfinite(values), axis=1)
    corr = np.corrcoef(values[finite].T) if finite.sum() > 2 else np.eye(k)
    np.fill_diagonal(corr, 0.0)
    flat = int(np.nanargmax(np.abs(corr)))
    first, second = sorted((flat // k, flat % k))
    r_max = float(corr[first, second])
    caption = (
        f"Each small plot shows the draws of two parameters; the diagonal shows the histogram "
        f"of each parameter ({k} parameters, {len(keep)} of {n} draws). "
        f"The strongest correlation is between {labels[first]} and {labels[second]} "
        f"(r = {r_max:.2f})."
    )
    if abs(r_max) > 0.8:
        caption += (
            " Strongly correlated parameters are hard to sample. A reparameterisation can help."
        )
    n_div = int(divergent.sum())
    if n_div:
        caption += (
            f" The red points are the {n_div} divergent transitions. If they gather in one "
            "corner, the sampler cannot reach that region (for example a funnel)."
        )
    elif stats is not None and "diverging" in stats.data_vars:
        caption += " There were no divergent transitions."
    return Figure(chart=chart, datasets={"draws": draws, "hist": hist}, caption=caption)


# ---------------------------------------------------------------------------------- ridge


class RidgeSettings(RecipeSettings):
    var: str | None = None
    group: GroupName = "auto"
    dim: str | None = None
    select: dict[str, Any] | None = None
    hdi_prob: float = Field(0.94, gt=0, lt=1)
    max_levels: int = Field(30, ge=2, le=100)
    max_draws: int = Field(4000, ge=50, le=20_000)
    points: int = Field(100, ge=20, le=400)
    overlap: float = Field(1.6, gt=0.2, le=5.0)
    descending: bool = True


def _level_dim(da: xr.DataArray, dim: str | None) -> str:
    dims = [str(d) for d in da.dims if d != SAMPLE]
    if dim is not None:
        if dim not in dims:
            raise RecipeDataError(f"no dim {dim!r} (dims: {', '.join(dims) or 'none'})")
        return dim
    big = [d for d in dims if da.sizes[d] > 1]
    if len(big) != 1:
        raise RecipeDataError(
            f"{da.name!r} needs exactly one dim to compare levels along; it has "
            f"{', '.join(big) or 'none'}. Set `dim` (and `select` for the others)"
        )
    return big[0]


@recipe(
    "core.ridge",
    title="Ridgeline",
    settings=RidgeSettings,
    persona="both",
    requires_representation=[Representation.DRAWS],
    summary="One curve of possible values for each level of a dimension, such as each group, "
    "sorted by the average.",
)
def ridge(inp: RecipeInput, s: RidgeSettings) -> Figure:
    """Ridgeline: the density of a variable for each level of one dim, ordered by mean."""
    raw, group = _find(inp, s.var, s.group, _DRAW_GROUPS, need=s.dim)
    if not sample_dims(raw):
        raise RecipeDataError(f"{raw.name!r} has no draws (chain/draw or sample dims)")
    da = stack_samples(_select(raw, s.select))
    dim = _level_dim(da, s.dim)
    da = _squeeze_except(da, (SAMPLE, dim)).transpose(SAMPLE, dim)
    x = np.asarray(da.values, dtype=float)
    x = x[_thin(x.shape[0], s.max_draws)]
    names = _labels(da, dim)
    finite = np.isfinite(x)
    ok = finite.sum(axis=0) >= 2
    means = np.where(finite, x, 0.0).sum(axis=0) / np.maximum(finite.sum(axis=0), 1)
    if not ok.any():
        raise RecipeDataError(f"no level of {dim!r} has enough finite draws")
    idx = np.flatnonzero(ok)
    idx = idx[np.argsort(means[idx], kind="stable")]  # lowest mean first (bottom row)
    if not s.descending:
        idx = idx[::-1]
    dropped = 0
    if idx.size > s.max_levels:
        dropped = idx.size - s.max_levels
        idx = idx[_thin(idx.size, s.max_levels)]
    pool = x[:, idx][np.isfinite(x[:, idx])]
    lo, hi = np.percentile(pool, [0.5, 99.5])
    if hi <= lo:
        hi = lo + 1.0
    pad = 0.05 * (hi - lo)
    grid = np.linspace(lo - pad, hi + pad, s.points)
    parts, rows = [], []
    for row, j in enumerate(idx):
        v = x[:, j][np.isfinite(x[:, j])]
        _, dens = _stats.kde(v, grid)
        top = row + s.overlap * dens / max(float(dens.max()), 1e-300)
        parts.append(pd.DataFrame({"level": names[j], "x": grid, "base": float(row), "top": top}))
        h_lo, h_hi = _stats.hdi(v, s.hdi_prob)
        rows.append(
            {
                "level": names[j],
                "base": float(row),
                "label_y": row + 0.25,
                "mean": float(v.mean()),
                "lo": float(h_lo),
                "hi": float(h_hi),
            }
        )
    dens_frame = pd.concat(parts, ignore_index=True)
    summary = pd.DataFrame(rows)
    n = len(idx)
    y_scale = alt.Scale(domain=[-0.4, n - 1 + s.overlap + 0.2], nice=False, zero=False)
    height = max(s.height, 24 * n)
    xs = alt.Scale(domain=[float(grid[0]), float(grid[-1])], nice=False, zero=False)
    title = f"{raw.name} by {dim}"
    rbase = alt.Chart(named("ridge"))
    sbase = alt.Chart(named("summary"))
    layers: list[Any] = []
    if grid[0] < 0 < grid[-1]:
        layers.append(
            alt.Chart(named("summary"))
            .mark_rule(color=REFERENCE, strokeDash=[3, 3])
            .encode(x=alt.datum(0.0))
        )
    layers += [
        rbase.mark_area(color=BAND, opacity=0.45).encode(
            x=alt.X("x:Q", title=str(raw.name), scale=xs),
            y=alt.Y("top:Q", axis=None, scale=y_scale),
            y2="base:Q",
            detail="level:N",
        ),
        rbase.mark_line(color=ESTIMATE, strokeWidth=1).encode(x="x:Q", y="top:Q", detail="level:N"),
        sbase.mark_rule(color=STRONG, strokeWidth=2.5).encode(
            x="lo:Q",
            x2="hi:Q",
            y="base:Q",
            tooltip=[
                "level:N",
                alt.Tooltip("mean:Q", format=".3g"),
                alt.Tooltip("lo:Q", format=".3g", title=f"HDI {pct(s.hdi_prob)} low"),
                alt.Tooltip("hi:Q", format=".3g", title=f"HDI {pct(s.hdi_prob)} high"),
            ],
        ),
        sbase.mark_tick(color=STRONG, thickness=2, size=10).encode(x="lo:Q", y="base:Q"),
        sbase.mark_tick(color=STRONG, thickness=2, size=10).encode(x="hi:Q", y="base:Q"),
        sbase.mark_point(filled=True, color=SURFACE, stroke=OBSERVED, size=30).encode(
            x="mean:Q", y="base:Q"
        ),
    ]
    ridges = alt.layer(*layers).properties(width=s.width, height=height, title=title)
    text = (
        alt.Chart(named("summary"))
        .mark_text(align="right", baseline="middle", fontSize=11, color=MUTED, limit=110)
        .encode(x=alt.value(112), y=alt.Y("label_y:Q", axis=None, scale=y_scale), text="level:N")
        .properties(width=116, height=height, view=alt.ViewConfig(strokeWidth=0))
    )
    chart = alt.hconcat(text, ridges, spacing=2)
    top_row, bottom_row = summary.iloc[-1], summary.iloc[0]
    caption = (
        f"Each curve shows the possible values of {raw.name} for one {dim} ({n} levels, from "
        f"{group}). The dot is the mean and the bar is the {pct(s.hdi_prob)} highest-density "
        f"interval. The top row is {top_row['level']} (mean {fmt(top_row['mean'])}) and the "
        f"bottom row is {bottom_row['level']} (mean {fmt(bottom_row['mean'])})."
    )
    lo_v, hi_v = summary["lo"].to_numpy(), summary["hi"].to_numpy()
    overlaps = int(np.sum((lo_v[1:] <= hi_v[:-1]) & (lo_v[:-1] <= hi_v[1:])))
    if overlaps:
        caption += (
            f" The intervals of {overlaps} of the {n - 1} pairs of neighbouring rows overlap, "
            "so the order of those rows is not certain."
        )
    else:
        caption += " No intervals of neighbouring rows overlap, so the order is clear."
    if dropped:
        caption += f" {dropped} levels are not shown (`max_levels` is {s.max_levels})."
    return Figure(chart=chart, datasets={"ridge": dens_frame, "summary": summary}, caption=caption)


# ---------------------------------------------------------------------------- rank matrix


class RankMatrixSettings(RecipeSettings):
    var: str | None = None
    group: GroupName = "auto"
    dim: str | None = None
    select: dict[str, Any] | None = None
    descending: bool = Field(True, description="Rank 1 is the largest value")
    max_items: int = Field(30, ge=2, le=60)
    max_draws: int = Field(4000, ge=50, le=20_000)
    labels: Literal["auto", "always", "never"] = "auto"


@recipe(
    "core.rank_matrix",
    title="Rank matrix",
    settings=RankMatrixSettings,
    persona="both",
    requires_representation=[Representation.DRAWS],
    summary="The probability of each rank for each item, and the probability that one item is "
    "above another.",
)
def rank_matrix(inp: RecipeInput, s: RankMatrixSettings) -> Figure:
    """P(rank = k) heatmap sorted by expected rank, plus the pairwise P(i above j) matrix."""
    raw, group = _find(inp, s.var, s.group, _DRAW_GROUPS, need=s.dim)
    if not sample_dims(raw):
        raise RecipeDataError(f"{raw.name!r} has no draws (chain/draw or sample dims)")
    da = stack_samples(_select(raw, s.select))
    dim = _level_dim(da, s.dim)
    da = _squeeze_except(da, (SAMPLE, dim)).transpose(SAMPLE, dim)
    n_items = da.sizes[dim]
    if n_items > s.max_items:
        raise RecipeDataError(
            f"{dim!r} has {n_items} items; the rank matrix shows at most {s.max_items} "
            "(raise `max_items` or use `select`)"
        )
    x = np.asarray(da.values, dtype=float)
    x = x[_thin(x.shape[0], s.max_draws)]
    x = x[np.all(np.isfinite(x), axis=1)]
    if x.shape[0] < 2:
        raise RecipeDataError(f"{raw.name!r} has fewer than two complete draws")
    names = _labels(da, dim)
    key = -x if s.descending else x
    ranks = key.argsort(axis=1, kind="stable").argsort(axis=1) + 1  # (S, n), 1 = best
    p_rank = np.stack([(ranks == k).mean(axis=0) for k in range(1, n_items + 1)], axis=1)
    expected = ranks.mean(axis=0)
    order = np.argsort(expected, kind="stable")
    sorted_names = [names[i] for i in order]
    rank_rows = [
        {"item": names[i], "rank": k + 1, "prob": float(p_rank[i, k])}
        for i in order
        for k in range(n_items)
    ]
    above = (key[:, :, None] < key[:, None, :]).mean(axis=0)  # P(i ranked above j)
    pair_rows = [
        {"item": names[i], "other": names[j], "prob": float(above[i, j]) if i != j else None}
        for i in order
        for j in order
    ]
    ranks_df = pd.DataFrame(rank_rows)
    pairs_df = pd.DataFrame(pair_rows)
    show_text = s.labels == "always" or (s.labels == "auto" and n_items <= 12)
    step = max(14, min(28, s.height // max(1, n_items) + 8))
    y = alt.Y("item:N", sort=sorted_names, title=None)
    rbase = alt.Chart(named("ranks")).encode(
        x=alt.X("rank:O", title="rank (1 = top)"),
        y=y,
    )
    rank_layers: list[Any] = [
        rbase.mark_rect().encode(
            color=alt.Color(
                "prob:Q",
                title="P(rank)",
                scale=alt.Scale(domain=[0, 1], range=[EMPTY, ESTIMATE]),
            ),
            tooltip=["item:N", "rank:O", alt.Tooltip("prob:Q", format=".0%")],
        )
    ]
    pbase = alt.Chart(named("pairs")).encode(
        x=alt.X("other:N", sort=sorted_names, title=None),
        y=y,
    )
    pair_layers: list[Any] = [
        pbase.mark_rect().encode(
            color=alt.Color(
                "prob:Q",
                title="P(row above column)",
                scale=alt.Scale(domain=[0, 0.5, 1], range=[NEGATIVE, EMPTY, POSITIVE]),
            ),
            tooltip=["item:N", "other:N", alt.Tooltip("prob:Q", format=".0%")],
        )
    ]
    if show_text:
        text_color = alt.condition(alt.datum.prob > 0.6, alt.value(SURFACE), alt.value(OBSERVED))
        rank_layers.append(
            rbase.transform_filter(alt.datum.prob >= 0.005)
            .mark_text(fontSize=9)
            .encode(text=alt.Text("prob:Q", format=".0%"), color=text_color)
        )
        pair_layers.append(
            pbase.transform_filter(alt.datum.prob >= 0)
            .mark_text(fontSize=9)
            .encode(
                text=alt.Text("prob:Q", format=".0%"),
                color=alt.condition(
                    (alt.datum.prob > 0.85) | (alt.datum.prob < 0.15),
                    alt.value(SURFACE),
                    alt.value(OBSERVED),
                ),
            )
        )
    size = alt.Step(step)
    chart = alt.hconcat(
        alt.layer(*rank_layers).properties(
            width=size, height=size, title=f"Rank of each {dim} (sorted by expected rank)"
        ),
        alt.layer(*pair_layers).properties(
            width=size, height=size, title="Probability that the row is above the column"
        ),
    ).resolve_scale(color="independent")
    best = order[0]
    caption = (
        f"Ranks of {raw.name} over the {n_items} levels of {dim} ({x.shape[0]} draws from "
        f"{group}; rank 1 is the {'largest' if s.descending else 'smallest'} value). "
        f"{names[best]} has the best expected rank ({expected[best]:.1f}) and is first in "
        f"{pct(float(p_rank[best, 0]))} of the draws."
    )
    if n_items > 1:
        second = order[1]
        p12 = float(above[best, second])
        caption += f" It is above {names[second]} with probability {pct(p12)}."
        if p12 < 0.8:
            caption += " The top of the ranking is not certain."
    return Figure(chart=chart, datasets={"ranks": ranks_df, "pairs": pairs_df}, caption=caption)


# ---------------------------------------------------------------------------- calibration


class CalibrationSettings(RecipeSettings):
    var: str | None = None
    observed_var: str | None = None
    kind: Literal["auto", "continuous", "binary"] = "auto"
    prob: float = Field(0.95, gt=0.5, lt=1, description="Envelope / band probability")
    bins: int = Field(10, ge=3, le=50, description="Probability bins (binary targets)")
    n_sim: int = Field(1000, ge=100, le=10_000, description="Simulations for the envelope")
    seed: int = 0


@recipe(
    "core.calibration",
    title="Calibration",
    settings=CalibrationSettings,
    persona="expert",
    requires_groups=["observed_data"],
    requires_representation=[Representation.DRAWS, Representation.POINT],
    summary="Checks if the predicted ranges and probabilities of the model agree with how "
    "often things happen in the data.",
    tags=["diagnostic"],
)
def calibration(inp: RecipeInput, s: CalibrationSettings) -> Figure:
    """PIT-ECDF difference with a simultaneous envelope, or a reliability diagram (0/1).

    Without draws (a point classifier's probabilities, or a score summary) only the
    reliability diagram is possible: the PIT needs the predictive distribution.
    """
    pred = inp.find("posterior_predictive", "predictions")
    obs = inp.find("observed_data", "observed")
    var = s.var
    if pred is not None and var is None:
        names = [str(v) for v in pred.data_vars if sample_dims(pred[v])]
        var = names[0] if names else None
    if pred is None or var is None or var not in pred.data_vars or not sample_dims(pred[var]):
        if s.kind == "continuous":
            raise RecipeDataError("kind=continuous (the PIT check) needs predictive draws")
        sc = binary_scores(
            inp,
            s.var,
            s.observed_var,
            None,
            not_binary="a point prediction needs a 0/1 target for the reliability diagram; "
            "the PIT check needs draws",
        )
        return _reliability(sc.var, sc.p[None, :], sc.y, s)
    draws, y = _matched(pred, obs, var, s.observed_var)
    binary = bool(np.all((y == 0) | (y == 1)) and np.nanmin(draws) >= 0 and np.nanmax(draws) <= 1)
    kind = s.kind if s.kind != "auto" else ("binary" if binary else "continuous")
    if kind == "binary":
        if not binary:
            raise RecipeDataError(
                f"kind=binary needs 0/1 observations and predictions in [0, 1] for {var!r}"
            )
        return _reliability(var, draws, y, s)
    return _pit_ecdf(var, draws, y, s)


def _pit_ecdf(var: str, draws: np.ndarray, y: np.ndarray, s: CalibrationSettings) -> Figure:
    pit = np.sort(_stats.pit_values(draws, y))
    n = pit.size
    k = int(min(max(n, 20), 200))
    z = np.arange(1, k + 1) / (k + 1)
    ecdf = np.searchsorted(pit, z, side="right") / n
    diff = ecdf - z
    lo, hi = _stats.ecdf_envelope(n, z, s.prob, s.n_sim, s.seed)
    outside = (diff < lo) | (diff > hi)
    frame = pd.DataFrame(
        {
            "z": z,
            "diff": diff,
            "lo": lo,
            "hi": hi,
            "status": np.where(outside, "outside", "inside"),
        }
    )
    base = alt.Chart(named("ecdf"))
    xs = alt.X("z:Q", title="PIT value", scale=alt.Scale(domain=[0, 1], nice=False))
    chart = alt.layer(
        base.mark_area(color=BAND, opacity=0.18).encode(
            x=xs, y=alt.Y("lo:Q", title="ECDF − uniform"), y2="hi:Q"
        ),
        base.mark_rule(color=REFERENCE, strokeDash=[4, 4]).encode(y=alt.datum(0.0)),
        base.mark_line(color=ESTIMATE, strokeWidth=1.8).encode(
            x="z:Q",
            y="diff:Q",
            tooltip=[
                alt.Tooltip("z:Q", format=".2f"),
                alt.Tooltip("diff:Q", format=".3f"),
                alt.Tooltip("hi:Q", format=".3f", title="envelope"),
            ],
        ),
        base.transform_filter(alt.FieldEqualPredicate(field="status", equal="outside"))
        .mark_circle(color=ALERT, size=24)
        .encode(x="z:Q", y="diff:Q"),
    ).properties(
        width=s.width,
        height=max(s.height, 200),
        title=f"PIT-ECDF difference of {var} ({pct(s.prob)} simultaneous envelope)",
    )
    caption = (
        f"The line is the share of the {n} observations of {var} below each PIT value minus "
        f"the share that a calibrated model gives. The band is where the line stays with "
        f"probability {pct(s.prob)} when the model is calibrated."
    )
    if outside.any():
        shift = float(pit.mean()) - 0.5
        tails = float(np.mean((pit < 0.1) | (pit > 0.9)))  # 0.2 when calibrated
        if abs(shift) < 0.08 and tails > 0.25:
            what = "Too many observations are in the tails. The intervals are too narrow."
        elif abs(shift) < 0.08 and tails < 0.15:
            what = "Too few observations are in the tails. The intervals are too wide."
        elif shift < 0:
            what = "Most observations are below the predictions. The model over-predicts."
        else:
            what = "Most observations are above the predictions. The model under-predicts."
        caption += f" The line goes outside the band, so the model is not calibrated. {what}"
    else:
        caption += " The line stays inside the band: no sign of bad calibration."
    if n < 30:
        caption += f" With only {n} observations the check is weak."
    return Figure(chart=chart, datasets={"ecdf": frame}, caption=caption)


def _reliability(var: str, draws: np.ndarray, y: np.ndarray, s: CalibrationSettings) -> Figure:
    p = draws.mean(axis=0)
    n = y.size
    edges = np.linspace(0.0, 1.0, s.bins + 1)
    which = np.clip(np.digitize(p, edges[1:-1]), 0, s.bins - 1)
    zq = NormalDist().inv_cdf((1 + s.prob) / 2)
    rows = []
    for b in range(s.bins):
        m = which == b
        cnt = int(m.sum())
        if not cnt:
            continue
        pm = float(p[m].mean())
        half = zq * np.sqrt(max(pm * (1 - pm), 1e-12) / cnt)
        rows.append(
            {
                "bin_start": float(edges[b]),
                "bin_end": float(edges[b + 1]),
                "predicted": pm,
                "observed": float(y[m].mean()),
                "count": cnt,
                "lo": max(0.0, pm - half),
                "hi": min(1.0, pm + half),
            }
        )
    rel = pd.DataFrame(rows)
    brier = float(np.mean((p - y) ** 2))
    ece = float(np.sum(rel["count"] * np.abs(rel["observed"] - rel["predicted"])) / n)
    rel["status"] = np.where(
        (rel["observed"] < rel["lo"]) | (rel["observed"] > rel["hi"]), "outside", "inside"
    )
    unit = alt.Scale(domain=[0, 1], nice=False)
    base = alt.Chart(named("reliability"))
    status = alt.Scale(domain=["inside", "outside"], range=[ESTIMATE, ALERT])
    diag = pd.DataFrame({"x": [0.0, 1.0]})
    rel_chart = alt.layer(
        alt.Chart(named("diagonal"))
        .mark_line(color=REFERENCE, strokeDash=[4, 4])
        .encode(
            x=alt.X("x:Q", title="predicted probability", scale=unit),
            y=alt.Y("x:Q", title="observed frequency", scale=unit),
        ),
        base.mark_rule(color=BAND, strokeWidth=6, opacity=0.3).encode(
            x="predicted:Q", y="lo:Q", y2="hi:Q"
        ),
        base.mark_line(color=ESTIMATE, strokeWidth=1).encode(x="predicted:Q", y="observed:Q"),
        base.mark_point(filled=True).encode(
            x="predicted:Q",
            y="observed:Q",
            size=alt.Size("count:Q", title="observations", scale=alt.Scale(range=[20, 200])),
            color=alt.Color("status:N", scale=status, legend=None),
            tooltip=[
                alt.Tooltip("predicted:Q", format=".2f"),
                alt.Tooltip("observed:Q", format=".2f"),
                "count:Q",
            ],
        ),
    ).properties(width=s.width // 2, height=max(s.height, 220), title="Reliability diagram")
    hist = (
        base.mark_rect(color=BAND, opacity=0.7, stroke=SURFACE, strokeWidth=0.5)
        .encode(
            x=alt.X("bin_start:Q", title="predicted probability", scale=unit),
            x2="bin_end:Q",
            y=alt.Y("count:Q", title="observations"),
            y2=alt.datum(0),
        )
        .properties(width=s.width // 2, height=max(s.height, 220), title="Predictions per bin")
    )
    chart = alt.hconcat(rel_chart, hist)
    caption = (
        f"Each dot is a group of observations of {var} with similar predicted probability: "
        "the observed share of 1s against the average prediction. A calibrated model puts the "
        f"dots on the dashed line. Brier score {brier:.3f}; average calibration error "
        f"{ece:.3f} ({n} observations)."
    )
    n_out = int((rel["status"] == "outside").sum())
    if n_out:
        caption += (
            f" {n_out} of {len(rel)} groups are outside their {pct(s.prob)} range (red), so "
            "the probabilities are not calibrated there."
        )
        hi_bins = rel[rel["predicted"] > 0.5]
        if len(hi_bins) and float((hi_bins["observed"] - hi_bins["predicted"]).mean()) < 0:
            caption += " High predicted probabilities happen less often than predicted."
    else:
        caption += " All groups are inside their ranges."
    return Figure(chart=chart, datasets={"reliability": rel, "diagonal": diag}, caption=caption)


# ------------------------------------------------------------------------- state sequence


class StateSequenceSettings(RecipeSettings):
    var: str | None = None
    group: Literal[
        "auto",
        "posterior",
        "predictions",
        "posterior_predictive",
        "summary",
        "hidden_states",
        "regimes",
        "root",
    ] = "auto"
    state_dim: str = Field("auto", description="The state dim (auto: state, regime, ...)")
    along: str | None = None
    observed_var: str | None = None
    select: dict[str, Any] | None = None
    max_states: int = Field(10, ge=2, le=30)
    max_times: int = Field(2000, ge=10, le=20_000)
    max_draws: int = Field(4000, ge=10, le=20_000)
    width: int = Field(640, ge=80, le=2000)
    height: int = Field(140, ge=60, le=2000)


_STATE_GROUPS = (
    "hidden_states",
    "regimes",
    "posterior",
    "predictions",
    "posterior_predictive",
    "summary",
    "root",
)
# probability variables the HMM and Markov-switching plugins write, best first
_STATE_PROB_NAMES = ("smoothed_prob", "state_prob", "filtered_prob")
_STATE_WORDS = ("state", "regime")
# a sequence dim that is not a calendar time (time_dim does not know it)
_SEQUENCE_NAMES = ("step",)
_NOT_DATA = ("chain", "draw", SAMPLE, "stat")


def _state_dim(da: xr.DataArray, s: StateSequenceSettings) -> str | None:
    """``state_dim`` if set, else ``state``/``regime``, else a dim whose name has one of them."""
    dims = [str(d) for d in da.dims if d not in _NOT_DATA]
    if s.state_dim != "auto":
        return s.state_dim if s.state_dim in dims else None
    for name in _STATE_WORDS:
        if name in dims:
            return name
    for d in dims:
        if any(w in d for w in _STATE_WORDS):
            return d
    return None


def _state_var(inp: RecipeInput, s: StateSequenceSettings) -> tuple[xr.DataArray, str]:
    groups = _STATE_GROUPS if s.group == "auto" else (s.group,)
    found: list[tuple[xr.DataArray, str]] = []
    for g in groups:
        ds = inp.root() if g == "root" else inp.find(g)
        if ds is None:
            continue
        for v in ds.data_vars:
            da = ds[v]
            if not np.issubdtype(da.dtype, np.number) and da.dtype != bool:
                continue
            if s.var is not None:
                if str(v) == s.var:
                    return da, g
                continue
            found.append((da, g))
    if s.var is not None:
        raise RecipeDataError(f"no variable {s.var!r} in the groups {', '.join(groups)}")
    for name in _STATE_PROB_NAMES:  # the known probability names first
        for da, g in found:
            if da.name == name and _state_dim(da, s) is not None:
                return da, g
    for da, g in found:  # then probabilities over a state dim and a sequence dim
        sd = _state_dim(da, s)
        if sd is None:
            continue
        # a transition matrix (regime, regime_to) or a duration per state is not a sequence
        others = [
            str(d)
            for d in da.dims
            if d not in (*_NOT_DATA, sd) and not any(w in str(d) for w in _STATE_WORDS)
        ]
        if others:
            return da, g
    for da, g in found:  # then discrete state draws
        if sample_dims(da) and np.issubdtype(da.dtype, np.integer):
            return da, g
    want = "a state" if s.state_dim == "auto" else repr(s.state_dim)
    raise RecipeDataError(
        f"no state variable: expected probabilities with {want} dim or integer "
        f"state draws in the groups {', '.join(groups)}"
    )


def _summary_point(da: xr.DataArray) -> xr.DataArray:
    """One statistic of a score summary (``stat`` dim): mean, else median, else point."""
    if "stat" not in da.dims:
        return da
    labels = [str(v) for v in coord_values(da, "stat")]
    for name in ("mean", "median", "point"):
        if name in labels:
            return da.isel(stat=labels.index(name), drop=True)
    raise RecipeDataError(f"the summary of {da.name!r} has no mean, median or point statistic")


def _sequence_dim(da: xr.DataArray, s: StateSequenceSettings, skip: Sequence[str]) -> str:
    """The time (sequence) dim: ``along``, a time-like dim, ``step``, the only other dim or,
    without ``select``, the longest one."""
    rest = da.drop_vars([d for d in skip if d in da.coords], errors="ignore")
    t = time_dim(rest, s.along)
    if t is not None and t not in skip:
        return t
    others = [str(d) for d in da.dims if d not in (*_NOT_DATA, *skip)]
    for name in _SEQUENCE_NAMES:
        if name in others:
            return name
    if len(others) == 1:
        return others[0]
    if others and s.select is None:
        return max(others, key=lambda d: da.sizes[d])
    raise RecipeDataError("set `along` to the time dim of the states")


def _auto_select(
    da: xr.DataArray, s: StateSequenceSettings, keep: Sequence[str]
) -> tuple[xr.DataArray, dict[str, Any]]:
    """Without ``select``: the first level of every other dim longer than 1.

    Returns the array and ``{dim: picked value}`` (the coord label, else position 0) so the
    observed series can take the same level.
    """
    picked: dict[str, Any] = {}
    if s.select is not None:
        return da, picked
    for d in [str(d) for d in da.dims if d not in (*_NOT_DATA, *keep)]:
        if da.sizes[d] > 1:
            picked[d] = coord_values(da, d)[0] if d in da.coords else 0
            da = da.isel({d: 0})
    return da, picked


def _state_probs(
    da: xr.DataArray, s: StateSequenceSettings
) -> tuple[np.ndarray, list[str], str, np.ndarray, str, dict[str, Any]]:
    """``(probs (state, time), state labels, time dim, time values, state dim, picked)``."""
    da = _summary_point(_select(da, s.select))
    sd = _state_dim(da, s)
    if sd is not None:
        if sample_dims(da):
            st = stack_samples(da)
            st = st.isel({SAMPLE: _thin(st.sizes[SAMPLE], s.max_draws)})
            mean = st.mean(SAMPLE)
        else:
            mean = da
        t = _sequence_dim(mean, s, (sd,))
        mean, picked = _auto_select(mean, s, (sd, t))
        mean = _squeeze_except(mean, (sd, t)).transpose(sd, t)
        probs = np.nan_to_num(np.asarray(mean.values, dtype=float), nan=0.0)
        probs = np.clip(probs, 0.0, None)
        total = probs.sum(axis=0, keepdims=True)
        probs = np.divide(probs, total, out=np.zeros_like(probs), where=total > 0)
        labels = _labels(mean, sd)
        times = coord_values(mean, t)
    else:
        if not sample_dims(da):
            want = "state" if s.state_dim == "auto" else s.state_dim
            raise RecipeDataError(
                f"{da.name!r} has no {want!r} dim and no draws: give state "
                "probabilities (…, state, time) or state draws (chain, draw, time)"
            )
        st = stack_samples(da)
        st = st.isel({SAMPLE: _thin(st.sizes[SAMPLE], s.max_draws)})
        t = _sequence_dim(st, s, ())
        st, picked = _auto_select(st, s, (t,))
        st = _squeeze_except(st, (SAMPLE, t)).transpose(SAMPLE, t)
        x = np.asarray(st.values, dtype=float)
        values = np.unique(x[np.isfinite(x)])
        if not np.allclose(values, np.round(values)):
            raise RecipeDataError(f"{da.name!r} is not a discrete state (integer draws)")
        if values.size > s.max_states:
            raise RecipeDataError(
                f"{da.name!r} has {values.size} distinct values; state draws need at most "
                f"{s.max_states} (raise `max_states`)"
            )
        probs = np.stack([(x == v).mean(axis=0) for v in values])
        labels = [str(int(v)) for v in values]
        times = coord_values(st, t)
        sd = "state"
    if len(labels) > s.max_states:
        raise RecipeDataError(
            f"{len(labels)} states; the plot shows at most {s.max_states} (raise `max_states`)"
        )
    return probs, labels, t, times, sd, picked


@recipe(
    "core.state_sequence",
    title="State sequence",
    settings=StateSequenceSettings,
    persona="both",
    summary="The data over time above the probability of each hidden state, such as a regime "
    "or a phase, at each time.",
)
def state_sequence(inp: RecipeInput, s: StateSequenceSettings) -> Figure:
    """Observed series on top, stacked posterior state probabilities over time below.

    Reads HMM ``hidden_states``, Markov-switching ``regimes``, draws and score summaries.
    Without ``select``, every extra dim (an HMM's ``animal``) shows its first level.
    """
    da, group = _state_var(inp, s)
    probs, labels, t, times, state_title, picked = _state_probs(da, s)
    n_t = probs.shape[1]
    keep = _thin(n_t, s.max_times)
    is_time = np.issubdtype(np.asarray(times).dtype, np.datetime64)
    enc = "T" if is_time else "Q"
    rows = []
    for k, lab in enumerate(labels):
        rows.append(
            pd.DataFrame(
                {"t": np.asarray(times)[keep], "state": lab, "order": k, "prob": probs[k, keep]}
            )
        )
    frame = pd.concat(rows, ignore_index=True)
    colors = alt.Scale(
        domain=labels, range=[_CATEGORY[i % len(_CATEGORY)] for i in range(len(labels))]
    )
    area = (
        alt.Chart(named("states"))
        .mark_area(interpolate="step-after" if n_t <= 200 else "linear", opacity=0.85)
        .encode(
            x=alt.X(f"t:{enc}", title=t),
            y=alt.Y(
                "prob:Q",
                stack="zero",
                title="state probability",
                scale=alt.Scale(domain=[0, 1], nice=False),
            ),
            color=alt.Color("state:N", scale=colors, title=state_title, sort=labels),
            order=alt.Order("order:Q"),
            tooltip=[f"t:{enc}", "state:N", alt.Tooltip("prob:Q", format=".0%")],
        )
        .properties(width=s.width, height=s.height, title="State probabilities")
    )
    best = probs.argmax(axis=0)
    best_p = probs.max(axis=0)
    datasets: dict[str, pd.DataFrame] = {"states": frame}
    charts: list[Any] = []
    obs_ds = inp.find("observed_data", "observed")
    obs_name = None
    if obs_ds is not None:
        if s.observed_var is not None:
            if s.observed_var not in obs_ds.data_vars:
                raise RecipeDataError(f"no observed variable {s.observed_var!r}")
            obs_name = s.observed_var
        else:
            cands = [str(v) for v in obs_ds.data_vars if t in obs_ds[v].dims]
            obs_name = cands[0] if cands else None
    if obs_name is not None and obs_ds is not None:
        o = obs_ds[obs_name]
        if t not in o.dims:
            raise RecipeDataError(f"observed {obs_name!r} has no dim {t!r}")
        chosen = s.select if s.select is not None else picked
        o = _select(o, {k: v for k, v in (chosen or {}).items() if k in o.dims})
        other = [str(d) for d in o.dims if d != t]
        o = o.stack(_series=other) if other else o.expand_dims(_series=[0])
        o = o.transpose(t, "_series")
        if o.sizes["_series"] > 8:
            raise RecipeDataError(
                f"observed {obs_name!r} has {o.sizes['_series']} series; use `select`"
            )
        o_times = coord_values(o, t)
        if len(o_times) != n_t:
            raise RecipeDataError(
                f"observed {obs_name!r} has {len(o_times)} times, the states have {n_t}"
            )
        parts = []
        for j in range(o.sizes["_series"]):
            label = (
                ", ".join(str(v) for v in np.atleast_1d(o["_series"].values[j]))
                if other
                else obs_name
            )
            parts.append(
                pd.DataFrame(
                    {
                        "t": np.asarray(times)[keep],
                        "value": np.asarray(o.values[:, j], dtype=float)[keep],
                        "series": label,
                        "state": [labels[b] for b in best[keep]],
                    }
                )
            )
        obs_frame = pd.concat(parts, ignore_index=True)
        # padded sequences (an HMM's shorter series) hold NaN after their end
        obs_frame = obs_frame[np.isfinite(obs_frame["value"])].reset_index(drop=True)
        datasets["observed"] = obs_frame
        obase = alt.Chart(named("observed"))
        charts.append(
            alt.layer(
                obase.mark_line(color=OBSERVED, strokeWidth=0.8, opacity=0.6).encode(
                    x=alt.X(f"t:{enc}", title=None),
                    y=alt.Y("value:Q", title=obs_name, scale=alt.Scale(zero=False)),
                    detail="series:N",
                ),
                obase.mark_circle(size=14, opacity=0.9).encode(
                    x=f"t:{enc}",
                    y="value:Q",
                    color=alt.Color("state:N", scale=colors, title=state_title, sort=labels),
                    tooltip=[f"t:{enc}", "series:N", alt.Tooltip("value:Q", format=".3g")],
                ),
            ).properties(
                width=s.width, height=s.height, title="Observed, coloured by most likely state"
            )
        )
    charts.append(area)
    chart = alt.vconcat(*charts).resolve_scale(color="shared")
    switches = int(np.sum(best[1:] != best[:-1]))
    unsure = float(np.mean(best_p < 0.8))
    share = ", ".join(f"{lab} {pct(float(np.mean(best == k)))}" for k, lab in enumerate(labels))
    caption = (
        f"The coloured areas show the probability of each of the {len(labels)} states of "
        f"{da.name} ({group}) at each {t}. The most likely state changes {switches} times; "
        f"share of time as the most likely state: {share}."
    )
    for dim, value in picked.items():
        caption += f" The plot shows {dim} = {value}. Set `select` to show another {dim}."
    if unsure > 0:
        caption += (
            f" At {pct(unsure)} of the times no state has a probability of 80% or more, so the "
            "state there is not certain."
        )
    if obs_name is None:
        caption += " There is no observed series to show above the states."
    if keep.size < n_t:
        caption += f" The plot shows {keep.size} of {n_t} times."
    return Figure(chart=chart, datasets=datasets, caption=caption)
