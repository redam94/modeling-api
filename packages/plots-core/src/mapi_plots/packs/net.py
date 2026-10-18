"""Network domain pack: graphs of edge probabilities, weights or partial correlations.

Plotly figures, rendered through :func:`~mapi_plots.recipes._base.plotly_plot`. Plugins opt
in by listing the ids in their manifest's ``plots``:

* ``net.graph``: a node-link drawing of the edges above a threshold (line width shows the
  value, colour shows the sign, arrowheads for directed graphs) next to a heatmap of the full
  adjacency matrix, with an optional true or observed adjacency drawn over both.

Input: an edge variable with two node dims ``(…, node, node_)`` (the second is the first with
a trailing ``_``), as draws (the posterior mean is drawn), a score summary (its point
statistic) or plain values. Node positions come from a ``(…, node, latent_dim)`` variable
(posterior mean) when there is one, else from a seeded spectral layout of the mean weights
refined with a few Fruchterman–Reingold steps.
"""

from __future__ import annotations

import warnings
from typing import Any, Literal

import numpy as np
import pandas as pd
import xarray as xr
from pydantic import Field

from mapi_plots.packs.sci import _scale, _select
from mapi_plots.packs.sci_points import (
    MAX_CATEGORIES,
    SEARCH_GROUPS,
    _groups,
    category_colours,
    find_var,
    group_codes,
    point_values,
)
from mapi_plots.palette import ALERT, ESTIMATE, MUTED, NEGATIVE, OBSERVED, POSITIVE, SURFACE
from mapi_plots.recipes._base import (
    Figure,
    Recipe,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    recipe,
)
from mapi_plots.recipes._util import SAMPLE, fmt, pct, sample_dims

__all__ = ["GraphSettings", "graph", "net_recipes", "spectral_layout"]

_WIDTH_CLASSES = (1.0, 2.0, 3.5, 5.5)
_TRUTH_GROUPS = ("observed_data", "constant_data")
_POSITION_DIMS = ("latent_dim", "dim", "coord", "xy", "axis", "component", "latent")


# ----------------------------------------------------------------------------- layout


def spectral_layout(w: np.ndarray, *, seed: int = 0, iterations: int = 80) -> np.ndarray:
    """``(n, 2)`` node positions in [-1, 1] from a weight matrix (absolute values are used).

    Spectral start (the two smallest non-trivial eigenvectors of the normalised Laplacian of a
    slightly smoothed graph, signs fixed), a small seeded jitter, then Fruchterman–Reingold
    steps with a cooling step size. Deterministic for a given ``seed``.
    """
    n = w.shape[0]
    if n == 0:
        return np.zeros((0, 2))
    if n == 1:
        return np.zeros((1, 2))
    a = np.abs(np.nan_to_num(np.asarray(w, dtype=float)))
    a = (a + a.T) / 2
    np.fill_diagonal(a, 0.0)
    top = a.max()
    a = a / top if top > 0 else a
    smooth = a + 1e-3 * (1 - np.eye(n))  # connects every part, so the spectrum is defined
    deg = smooth.sum(1)
    dinv = 1 / np.sqrt(deg)
    lap = np.eye(n) - dinv[:, None] * smooth * dinv[None, :]
    _vals, vecs = np.linalg.eigh(lap)
    pos = vecs[:, 1:3] * dinv[:, None] if n > 2 else np.c_[vecs[:, 1], np.zeros(n)]
    for j in range(pos.shape[1]):
        k = int(np.argmax(np.abs(pos[:, j])))
        if pos[k, j] < 0:
            pos[:, j] = -pos[:, j]
    pos = _normalise(pos)
    rng = np.random.default_rng(seed)
    pos = pos + rng.normal(0, 0.02, pos.shape)
    k_opt = np.sqrt(4.0 / n)
    step = 0.1
    for _ in range(iterations):
        delta = pos[:, None, :] - pos[None, :, :]
        dist = np.sqrt((delta**2).sum(-1)) + 1e-9
        force = k_opt**2 / dist - a * dist**2 / k_opt
        np.fill_diagonal(force, 0.0)
        disp = (delta / dist[..., None] * force[..., None]).sum(1)
        length = np.sqrt((disp**2).sum(-1)) + 1e-9
        pos = pos + disp / length[:, None] * np.minimum(length, step)[:, None]
        pos = pos - pos.mean(0)
        step *= 0.95
    return _normalise(pos)


def _normalise(pos: np.ndarray) -> np.ndarray:
    pos = pos - pos.mean(0)
    m = np.abs(pos).max()
    return pos / m if m > 0 else pos


def _fiedler_order(w: np.ndarray) -> np.ndarray:
    """Node order along the Fiedler vector (blocks of the matrix come out together)."""
    n = w.shape[0]
    if n < 3:
        return np.arange(n)
    a = np.abs(np.nan_to_num(w))
    a = (a + a.T) / 2
    np.fill_diagonal(a, 0.0)
    a = a + 1e-6 * (1 - np.eye(n))
    lap = np.diag(a.sum(1)) - a
    _vals, vecs = np.linalg.eigh(lap)
    f = vecs[:, 1]
    if f[int(np.argmax(np.abs(f)))] < 0:
        f = -f
    return np.argsort(f, kind="stable")


# ---------------------------------------------------------------------------- settings


class GraphSettings(RecipeSettings):
    var: str | None = Field(
        None, description="Edge variable with dims (…, node, node_) (default: the first one)"
    )
    node_dim: str | None = Field(
        None, description="The node dim (its pair is the same name + '_'; default: found)"
    )
    positions: str | None = Field(
        "auto", description="Variable with node positions (…, node, latent_dim); none: layout"
    )
    threshold: float | None = Field(
        None,
        description="Draw edges with |value| above this (default: 0.5 for values in [0, 1], "
        "else 10% of the largest |value|)",
    )
    signed: bool | None = Field(None, description="Colour by sign (default: if a value is < 0)")
    directed: bool | None = Field(
        None, description="Arrowheads from node to node_ (default: if the matrix is asymmetric)"
    )
    group: str | None = Field(
        "auto", description="Node variable for colours (auto: 'node_group' or 'group'; none: off)"
    )
    truth: str | None = Field(
        "auto",
        description="True or observed adjacency to outline (auto: a (node, node_) variable in "
        "observed_data or constant_data; none: off)",
    )
    panels: Literal["graph", "matrix", "both"] = "both"
    select: dict[str, str | int | float] | None = Field(
        None, description="Pick one level of other dims, for example {layer: 0}"
    )
    labels: bool | None = Field(None, description="Node names on the graph (default: ≤ 40 nodes)")
    max_nodes: int = Field(300, ge=2, le=600)
    max_edges: int = Field(2_000, ge=1, le=20_000)
    seed: int = 0
    width: int = Field(900, ge=80, le=2000)
    height: int = Field(440, ge=60, le=2000)


# ------------------------------------------------------------------------------ inputs


def _node_pair(da: xr.DataArray, node_dim: str | None) -> tuple[str, str] | None:
    dims = [str(d) for d in da.dims]
    if node_dim is not None:
        return (node_dim, node_dim + "_") if {node_dim, node_dim + "_"} <= set(dims) else None
    for d in dims:
        if d + "_" in dims and da.sizes[d] == da.sizes[d + "_"]:
            return d, d + "_"
    return None


def _edge_var(inp: RecipeInput, s: GraphSettings) -> tuple[xr.DataArray, str, tuple[str, str]]:
    if s.var is not None:
        hit = find_var(inp, s.var)
        if hit is None:
            raise RecipeDataError(f"no edge variable {s.var!r} found")
        pair = _node_pair(hit[0], s.node_dim)
        if pair is None:
            dims = ", ".join(map(str, hit[0].dims))
            raise RecipeDataError(
                f"{s.var!r} has dims {dims}: an edge variable needs two node dims such as "
                "(node, node_)"
            )
        return hit[0], hit[1], pair
    seen: list[str] = []
    for g, ds in _groups(inp, SEARCH_GROUPS):
        for v in ds.data_vars:
            seen.append(str(v))
            pair = _node_pair(ds[v], s.node_dim)
            if pair is not None:
                return ds[v], g, pair
    raise RecipeDataError(
        "no edge variable with two node dims such as (node, node_) found"
        + (f" (variables: {', '.join(dict.fromkeys(seen))})" if seen else "")
    )


def _matrix(
    da: xr.DataArray, pair: tuple[str, str], select: dict[str, Any] | None
) -> tuple[np.ndarray, np.ndarray | None]:
    """``(mean (n, n), draws (sample, n, n) or None)``."""
    sel = {d: v for d, v in (select or {}).items() if d in da.dims}
    da = _select(da, sel)
    skip = {*pair, *sample_dims(da), "stat"}
    extra = [str(d) for d in da.dims if d not in skip]
    big = [d for d in extra if da.sizes[d] > 1]
    if big:
        raise RecipeDataError(
            f"{da.name!r} has more dims than the plot uses: {', '.join(big)}. "
            "Pick one level of each with select={dim: level}."
        )
    if extra:
        da = da.squeeze(extra, drop=True)
    val, st = point_values(da)
    mean = np.asarray(val.transpose(*pair).values, dtype=float)
    draws = None if st is None else np.asarray(st.transpose(SAMPLE, *pair).values, dtype=float)
    return mean, draws


def _positions(
    inp: RecipeInput, s: GraphSettings, node: str, n: int, edge_name: str
) -> tuple[np.ndarray, str] | None:
    """``(n, 2)`` positions and the variable name, or None (use the layout)."""
    if s.positions is None or s.positions == "none":
        return None
    if s.positions != "auto":
        hit = find_var(inp, s.positions)
        if hit is None:
            raise RecipeDataError(f"no position variable {s.positions!r} found")
        cand = [hit[0]]
    else:
        cand = []
        for _g, ds in _groups(inp, SEARCH_GROUPS):
            for v in ds.data_vars:
                da = ds[v]
                if str(v) == edge_name or node not in da.dims:
                    continue
                other = [d for d in da.dims if d not in (node, *sample_dims(da), "stat")]
                if (
                    len(other) == 1
                    and (other[0] in _POSITION_DIMS or "pos" in str(v).lower())
                    and da.sizes[other[0]] >= 2
                ):
                    cand.append(da)
    for da in cand:
        sel = {d: v for d, v in (s.select or {}).items() if d in da.dims}
        val, _ = point_values(_select(da, sel))
        other = [d for d in val.dims if d != node]
        if node not in val.dims or len(other) != 1 or val.sizes[node] != n:
            if s.positions != "auto":
                raise RecipeDataError(
                    f"{s.positions!r} must have dims ({node}, <coordinate dim>) with {n} nodes"
                )
            continue
        xy = np.asarray(val.transpose(node, other[0]).values, dtype=float)
        if xy.shape[1] == 1:
            xy = np.c_[xy, np.zeros(n)]
        xy = xy[:, :2]
        if not np.isfinite(xy).all():
            continue
        return _normalise(xy), str(da.name)
    return None


def _node_groups(
    inp: RecipeInput, s: GraphSettings, node: str, n: int
) -> tuple[str, np.ndarray, list[str] | None] | None:
    """``(variable, codes or values, level names or None for a continuous value)``."""
    if s.group is None or s.group == "none":
        return None
    names = ["node_group", "group"] if s.group == "auto" else [s.group]
    for name in names:
        hit = find_var(inp, name)
        if hit is None:
            continue
        da = hit[0]
        sel = {d: v for d, v in (s.select or {}).items() if d in da.dims}
        da = _select(da, sel)
        got = group_codes(da, node)
        if got is not None and len(got[0]) == n:
            return name, got[0], got[1]
        if node in da.dims:
            val, _ = point_values(da)
            if val.dims == (node,):
                return name, np.asarray(val.values, dtype=float), None
        if s.group != "auto":
            raise RecipeDataError(f"{s.group!r} must give one value for each {node}")
    if s.group != "auto":
        raise RecipeDataError(f"no node variable {s.group!r} found")
    return None


def _truth(
    inp: RecipeInput, s: GraphSettings, pair: tuple[str, str], edge: tuple[str, str], n: int
) -> tuple[np.ndarray, str] | None:
    if s.truth is None or s.truth == "none":
        return None
    if s.truth != "auto":
        hit = find_var(inp, s.truth)
        if hit is None:
            raise RecipeDataError(f"no adjacency variable {s.truth!r} found")
        cands = [hit[0]]
    else:
        cands = []
        for g in _TRUTH_GROUPS if inp.tree.children else ():
            ds = inp.find(g)
            if ds is None:
                continue
            names = [edge[0], *(str(v) for v in ds.data_vars if str(v) != edge[0])]
            for v in names:
                own = g == edge[1] and v == edge[0]
                if v in ds.data_vars and not own and set(pair) <= set(ds[v].dims):
                    cands.append(ds[v])
    for da in cands:
        try:
            m, _ = _matrix(da, pair, s.select)
        except RecipeDataError:
            if s.truth != "auto":
                raise
            continue
        if m.shape == (n, n):
            return m, str(da.name)
    if s.truth != "auto":
        raise RecipeDataError(f"{s.truth!r} must have the dims {pair[0]} and {pair[1]}")
    return None


# ------------------------------------------------------------------------------ recipe


def _edge_list(
    m: np.ndarray, draws: np.ndarray | None, directed: bool, signed: bool, threshold: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None]:
    """``(i, j, value, P(|value| > threshold) or None)`` for the edges above the threshold."""
    n = m.shape[0]
    if directed:
        ii, jj = np.where(~np.eye(n, dtype=bool))
        vals = m[ii, jj]
        d = None if draws is None else draws[:, ii, jj]
    else:
        ii, jj = np.triu_indices(n, 1)
        sym = np.where(np.isnan(m), m.T, m)
        both = np.where(np.isnan(m.T), sym, (sym + sym.T) / 2)
        vals = both[ii, jj]
        d = None if draws is None else (draws[:, ii, jj] + draws[:, jj, ii]) / 2
    size = np.abs(vals) if signed else vals
    keep = np.isfinite(size) & (size > threshold)
    prob = None
    if d is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            hit = (np.abs(d) if signed else d) > threshold
            prob = np.where(np.isfinite(d), hit, np.nan)
            prob = np.nanmean(prob, 0)[keep]
    return ii[keep], jj[keep], vals[keep], prob


@recipe(
    "net.graph",
    title="Network graph",
    settings=GraphSettings,
    persona="both",
    summary="A network drawn as nodes and links, where thick links are strong and colours "
    "show the sign, next to the full matrix of links.",
    tags=["graph"],
)
def graph(inp: RecipeInput, s: GraphSettings) -> Figure:
    """Node-link drawing of the edges above a threshold plus the adjacency heatmap."""
    da, group, pair = _edge_var(inp, s)
    name = str(da.name)
    node, node_ = pair
    m, draws = _matrix(da, pair, s.select)
    n = m.shape[0]
    if n > s.max_nodes:
        raise RecipeDataError(
            f"{name!r} has {n} nodes, more than max_nodes ({s.max_nodes}): pick fewer nodes or "
            "raise max_nodes"
        )
    labels = [str(v) for v in da[node].values] if node in da.coords else [str(i) for i in range(n)]
    finite = m[np.isfinite(m)]
    if not finite.size:
        raise RecipeDataError(f"{name!r} has no finite values")
    signed = s.signed if s.signed is not None else bool((finite < -1e-12).any())
    directed = (
        s.directed
        if s.directed is not None
        else not np.allclose(np.nan_to_num(m), np.nan_to_num(m.T), atol=1e-9, rtol=1e-6)
    )
    top = float(np.max(np.abs(finite)))
    unit = float(finite.min()) >= 0.0 and float(finite.max()) <= 1.0
    if s.threshold is not None:
        threshold = s.threshold
    elif unit and not signed:
        threshold = 0.5
    else:
        threshold = 0.1 * top
    off = m.copy()
    np.fill_diagonal(off, np.nan)

    ii, jj, vals, prob = _edge_list(off, draws, directed, signed, threshold)
    n_above = len(vals)
    if n_above > s.max_edges:
        order = np.argsort(-np.abs(vals), kind="stable")[: s.max_edges]
        order.sort()
        ii, jj, vals = ii[order], jj[order], vals[order]
        prob = None if prob is None else prob[order]

    placed = _positions(inp, s, node, n, name)
    pos = placed[0] if placed is not None else spectral_layout(off, seed=s.seed)
    grp = _node_groups(inp, s, node, n)
    truth = _truth(inp, s, pair, (name, group), n)

    show_graph = s.panels in ("graph", "both")
    show_matrix = s.panels in ("matrix", "both")
    gap = 0.08
    gdom = [0.0, 0.5 - gap / 2] if show_matrix and show_graph else [0.0, 1.0]
    mdom = [0.5 + gap / 2, 1.0] if show_matrix and show_graph else [0.0, 1.0]
    datasets: dict[str, pd.DataFrame] = {}
    traces: list[dict[str, Any]] = []
    layout: dict[str, Any] = {
        "height": s.height,
        "width": s.width,
        "margin": {"l": 56, "r": 56, "t": 36, "b": 56},
        "hovermode": "closest",
        "showlegend": False,
        "annotations": [],
    }
    mag = np.abs(vals)
    scale_top = float(mag.max()) if mag.size else 1.0

    if show_graph:
        # edges: one trace per (sign, width class), NaN between segments, one shared table
        cls = (
            np.minimum((mag / (scale_top or 1.0) * len(_WIDTH_CLASSES)).astype(int), 3)
            if mag.size
            else np.zeros(0, dtype=int)
        )
        signs = (
            [(1, POSITIVE, "positive"), (-1, NEGATIVE, "negative")]
            if signed
            else [(1, ESTIMATE, "edge")]
        )
        shrink = 0.05 if directed else 0.0
        segs: dict[str, np.ndarray] = {}
        arrows: list[tuple[str, np.ndarray, np.ndarray, np.ndarray, str]] = []
        for sg, colour, tag in signs:
            in_sign = (vals >= 0) if sg > 0 else (vals < 0)
            if not signed:
                in_sign = np.ones_like(vals, dtype=bool)
            for c, width_px in enumerate(_WIDTH_CLASSES):
                sel = in_sign & (cls == c)
                if not sel.any():
                    continue
                x0, y0 = pos[ii[sel], 0], pos[ii[sel], 1]
                x1, y1 = pos[jj[sel], 0], pos[jj[sel], 1]
                if directed:
                    dx, dy = x1 - x0, y1 - y0
                    ln = np.hypot(dx, dy) + 1e-12
                    x1, y1 = x1 - dx / ln * shrink, y1 - dy / ln * shrink
                key = f"{tag}{c}"
                gaps = np.full(x0.shape, np.nan)
                segs[f"x_{key}"] = np.c_[x0, x1, gaps].ravel()
                segs[f"y_{key}"] = np.c_[y0, y1, gaps].ravel()
                traces.append(
                    {
                        "type": "scatter",
                        "mode": "lines",
                        "x": {"$ref": f"edges.x_{key}"},
                        "y": {"$ref": f"edges.y_{key}"},
                        "line": {"color": colour, "width": width_px},
                        "opacity": 0.75,
                        "hoverinfo": "skip",
                        "showlegend": False,
                    }
                )
                if directed:
                    angle = np.degrees(np.arctan2(x1 - x0, y1 - y0))
                    arrows.append((tag, x1, y1, angle, colour))
        # truth edges under the graph: thin dashed lines
        if truth is not None:
            tm = truth[0].copy()
            np.fill_diagonal(tm, 0.0)
            t_on = np.nan_to_num(tm) != 0
            if not directed:
                t_on = np.triu(t_on | t_on.T, 1)
            ti, tj = np.where(t_on)
            if ti.size:
                gaps = np.full(ti.shape, np.nan)
                segs["x_truth"] = np.c_[pos[ti, 0], pos[tj, 0], gaps].ravel()
                segs["y_truth"] = np.c_[pos[ti, 1], pos[tj, 1], gaps].ravel()
                traces.insert(
                    0,
                    {
                        "type": "scatter",
                        "mode": "lines",
                        "x": {"$ref": "edges.x_truth"},
                        "y": {"$ref": "edges.y_truth"},
                        "line": {"color": OBSERVED, "width": 1, "dash": "dot"},
                        "opacity": 0.6,
                        "hoverinfo": "skip",
                        "showlegend": False,
                    },
                )
        if segs:
            rows = max(len(v) for v in segs.values())
            datasets["edges"] = pd.DataFrame(
                {
                    k: np.pad(v, (0, rows - len(v)), constant_values=np.nan).astype(np.float32)
                    for k, v in segs.items()
                }
            )
        if arrows:
            heads = max(len(a[1]) for a in arrows)
            cols: dict[str, np.ndarray] = {}
            for tag, ax, ay, ang, _colour in arrows:
                for col, arr in ((f"x_{tag}", ax), (f"y_{tag}", ay), (f"a_{tag}", ang)):
                    prev = cols.get(col, np.zeros(0))
                    cols[col] = np.concatenate([prev, arr])
            heads = max(len(v) for v in cols.values())
            datasets["arrows"] = pd.DataFrame(
                {
                    k: np.pad(v, (0, heads - len(v)), constant_values=np.nan).astype(np.float32)
                    for k, v in cols.items()
                }
            )
            for tag, colour in {a[0]: a[4] for a in arrows}.items():
                traces.append(
                    {
                        "type": "scatter",
                        "mode": "markers",
                        "x": {"$ref": f"arrows.x_{tag}"},
                        "y": {"$ref": f"arrows.y_{tag}"},
                        "marker": {
                            "symbol": "triangle-up",
                            "size": 9,
                            "color": colour,
                            "angle": {"$ref": f"arrows.a_{tag}"},
                        },
                        "hoverinfo": "skip",
                        "showlegend": False,
                    }
                )
        # hover points in the middle of each edge
        if vals.size:
            arrow = "→" if directed else "–"
            text = []
            for k in range(len(vals)):
                t = f"{labels[ii[k]]} {arrow} {labels[jj[k]]}: {fmt(float(vals[k]))}"
                if prob is not None:
                    t += f"<br>P(|value| > {fmt(threshold)}) = {fmt(float(prob[k]))}"
                text.append(t)
            datasets["mids"] = pd.DataFrame(
                {
                    "x": ((pos[ii, 0] + pos[jj, 0]) / 2).astype(np.float32),
                    "y": ((pos[ii, 1] + pos[jj, 1]) / 2).astype(np.float32),
                    "text": text,
                }
            )
            traces.append(
                {
                    "type": "scatter",
                    "mode": "markers",
                    "x": {"$ref": "mids.x"},
                    "y": {"$ref": "mids.y"},
                    "text": {"$ref": "mids.text"},
                    "marker": {"size": 8, "color": SURFACE, "opacity": 0},
                    "hovertemplate": "%{text}<extra></extra>",
                    "showlegend": False,
                }
            )
        # nodes
        show_labels = s.labels if s.labels is not None else n <= 40
        degree = np.nansum(np.abs(np.nan_to_num(off)), 1)
        node_cols: dict[str, Any] = {
            "x": pos[:, 0].astype(np.float32),
            "y": pos[:, 1].astype(np.float32),
            "label": labels,
            "degree": degree.astype(np.float32),
        }
        base = {
            "type": "scatter",
            "mode": "markers+text" if show_labels else "markers",
            "textposition": "top center",
            "textfont": {"size": 10, "color": MUTED},
            "hovertemplate": "%{text}<br>sum of |values| %{customdata:.3g}<extra></extra>",
        }
        marker = {"size": 11, "line": {"color": SURFACE, "width": 1.2}}
        if grp is not None and grp[2] is not None and len(grp[2]) <= MAX_CATEGORIES:
            gname, codes, levels_opt = grp
            levels = levels_opt or []
            colours = category_colours(len(levels))
            for k, lev in enumerate(levels):
                sel = codes == k
                if not sel.any():
                    continue
                key = f"nodes_{k}"
                datasets[key] = pd.DataFrame(
                    {
                        "x": pos[sel, 0].astype(np.float32),
                        "y": pos[sel, 1].astype(np.float32),
                        "label": [labels[i] for i in np.flatnonzero(sel)],
                        "degree": degree[sel].astype(np.float32),
                    }
                )
                traces.append(
                    {
                        **base,
                        "name": f"{gname} {lev}",
                        "x": {"$ref": f"{key}.x"},
                        "y": {"$ref": f"{key}.y"},
                        "text": {"$ref": f"{key}.label"},
                        "customdata": {"$ref": f"{key}.degree"},
                        "marker": {**marker, "color": colours[k]},
                        "showlegend": True,
                    }
                )
            layout["showlegend"] = True
            layout["legend"] = {"orientation": "h", "x": 0, "y": -0.08, "xanchor": "left"}
            layout["margin"]["b"] = 80
        else:
            if grp is not None:
                node_cols["code"] = np.asarray(grp[1], dtype=np.float32)
                marker = {
                    **marker,
                    "color": {"$ref": "nodes.code"},
                    "colorscale": _scale("viridis"),
                    "showscale": True,
                    "colorbar": {
                        "x": gdom[1] + 0.005,
                        "xanchor": "left",
                        "thickness": 8,
                        "outlinewidth": 0,
                        "len": 0.6,
                        "title": {"text": grp[0]},
                    },
                }
            else:
                marker = {**marker, "color": ESTIMATE}
            datasets["nodes"] = pd.DataFrame(node_cols)
            traces.append(
                {
                    **base,
                    "name": node,
                    "x": {"$ref": "nodes.x"},
                    "y": {"$ref": "nodes.y"},
                    "text": {"$ref": "nodes.label"},
                    "customdata": {"$ref": "nodes.degree"},
                    "marker": marker,
                    "showlegend": False,
                }
            )
        hidden = {"showgrid": False, "zeroline": False, "showticklabels": False}
        layout["xaxis"] = {"domain": gdom, "anchor": "y", **hidden, "range": [-1.15, 1.15]}
        layout["yaxis"] = {
            "domain": [0.0, 1.0],
            "anchor": "x",
            **hidden,
            "range": [-1.15, 1.15],
            "scaleanchor": "x",
            "scaleratio": 1,
        }
        layout["annotations"].append(_title("Graph", gdom))

    if show_matrix:
        aid = "2" if show_graph else ""
        order = _fiedler_order(off)
        if grp is not None and grp[2] is not None:
            codes = np.asarray(grp[1])
            rank = np.empty(n, dtype=int)
            rank[order] = np.arange(n)
            order = np.lexsort((rank, codes))
        inv = np.empty(n, dtype=int)
        inv[order] = np.arange(n)
        rr, cc = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        z = off[np.ix_(order, order)]
        datasets["cells"] = pd.DataFrame(
            {
                "row": rr.ravel().astype(np.float32),
                "col": cc.ravel().astype(np.float32),
                "z": z.ravel().astype(np.float32),
            }
        )
        ordered = [labels[i] for i in order]
        datasets["ticks"] = pd.DataFrame({"pos": np.arange(n, dtype=np.float32), "label": ordered})
        zmax = float(np.nanmax(np.abs(z))) if np.isfinite(z).any() else 1.0
        hm: dict[str, Any] = {
            "type": "heatmap",
            "x": {"$ref": "cells.col"},
            "y": {"$ref": "cells.row"},
            "z": {"$ref": "cells.z"},
            "xaxis": f"x{aid}",
            "yaxis": f"y{aid}",
            "colorscale": _scale("diverging" if signed else "viridis"),
            "zmin": -zmax if signed else min(0.0, float(np.nanmin(z))),
            "zmax": zmax or 1.0,
            "hoverongaps": False,
            "hovertemplate": f"{node} %{{y}} {'→' if directed else '–'} %{{x}}: %{{z:.3g}}"
            "<extra></extra>",
            "colorbar": {
                "x": 1.0,
                "xanchor": "left",
                "thickness": 10,
                "outlinewidth": 0,
                "title": {"text": name},
            },
        }
        traces.append(hm)
        if truth is not None:
            tm = truth[0][np.ix_(order, order)].copy()
            np.fill_diagonal(tm, 0.0)
            tr, tc = np.where(np.nan_to_num(tm) != 0)
            if tr.size:
                datasets["truth_cells"] = pd.DataFrame(
                    {"row": tr.astype(np.float32), "col": tc.astype(np.float32)}
                )
                traces.append(
                    {
                        "type": "scatter",
                        "mode": "markers",
                        "x": {"$ref": "truth_cells.col"},
                        "y": {"$ref": "truth_cells.row"},
                        "xaxis": f"x{aid}",
                        "yaxis": f"y{aid}",
                        "marker": {
                            "symbol": "square-open",
                            "color": OBSERVED if signed else ALERT,
                            "size": max(3, min(12, int(300 / max(n, 1)))),
                            "line": {"width": 1.2},
                        },
                        "hoverinfo": "skip",
                        "showlegend": False,
                    }
                )
        ticks = n <= 40
        tick_axis: dict[str, Any] = (
            {
                "tickmode": "array",
                "tickvals": {"$ref": "ticks.pos"},
                "ticktext": {"$ref": "ticks.label"},
                "tickfont": {"size": 9},
            }
            if ticks
            else {"showticklabels": False}
        )
        layout[f"xaxis{aid}"] = {
            "domain": mdom,
            "anchor": f"y{aid}",
            "showgrid": False,
            "zeroline": False,
            "title": {"text": node_},
            **tick_axis,
        }
        layout[f"yaxis{aid}"] = {
            "domain": [0.0, 1.0],
            "anchor": f"x{aid}",
            "showgrid": False,
            "zeroline": False,
            "autorange": "reversed",
            "scaleanchor": f"x{aid}",
            "scaleratio": 1,
            "title": {"text": node},
            **tick_axis,
        }
        layout["annotations"].append(_title("Matrix", mdom))

    # caption
    rep = f"the posterior mean of {draws.shape[0]} draws" if draws is not None else "the values"
    caption = (
        f"The network has {n} nodes ({node}). It shows {name} ({rep}) as "
        f"{'a directed' if directed else 'an undirected'} graph."
    )
    if show_graph:
        rule = f"|{name}| > {fmt(threshold)}" if signed else f"{name} > {fmt(threshold)}"
        caption += f" The graph draws the {n_above} links with {rule}"
        caption += f" ({len(vals)} strongest shown)." if n_above > len(vals) else "."
        caption += " Thicker lines are stronger links."
        if signed:
            caption += " Blue lines are positive and red lines are negative."
        if directed:
            caption += f" Arrows point from {node} to {node_}."
        if placed is not None:
            caption += f" The node positions are the posterior mean of {placed[1]}."
        else:
            caption += (
                " The positions come from a layout that puts strongly linked nodes near each "
                "other. Distances in the layout have no other meaning."
            )
        if prob is not None and len(prob):
            caption += (
                f" Point at the middle of a link to see the share of draws above the threshold"
                f" (median {pct(float(np.nanmedian(prob)))})."
            )
    if show_matrix:
        caption += (
            " The matrix shows every pair of nodes. Nodes that link to each other are next to "
            "each other in the order."
        )
    if grp is not None:
        caption += f" The colours of the nodes show {grp[0]}."
    if truth is not None:
        caption += f" Dotted lines and open squares show the links in {truth[1]}, for comparison."
    return Figure(
        chart={"data": traces, "layout": layout},
        datasets=datasets,
        caption=caption,
        title=f"Network of {name}",
    )


def _title(text: str, dom: list[float]) -> dict[str, Any]:
    return {
        "text": f"<b>{text}</b>",
        "xref": "paper",
        "yref": "paper",
        "x": (dom[0] + dom[1]) / 2,
        "y": 1.0,
        "xanchor": "center",
        "yanchor": "bottom",
        "showarrow": False,
        "font": {"size": 12},
    }


def net_recipes() -> list[Recipe[Any]]:
    """The network pack (entry point ``mapi.plots: net``)."""
    return [graph]
