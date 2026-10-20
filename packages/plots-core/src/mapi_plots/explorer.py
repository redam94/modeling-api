"""Vega-Lite recipes for the Data Explorer and Model Explorer views (M3.5).

:func:`explorer_plots` turns one explorer view (the JSON of a :mod:`mapi_spec.views`
document, dispatched on its ``format``) into :class:`~mapi_spec.plots.PlotSpec` documents.
The data travels inline as Vega-Lite top-level ``datasets`` referenced by name (never by
URL), capped at :data:`MAX_ROWS` rows per dataset, so every plot passes the platform
sanitizer and renders in the isolated plot sandbox. This module is JSON-only (no numpy,
pandas or altair): the runner writes the plots next to the views, and the control plane or
the web app may build them from a view too.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

__all__ = ["EXPLORER_RECIPES", "MAX_ROWS", "RECIPE_VERSION", "explorer_plots"]

MAX_ROWS: Final = 20_000
"""Rows per inline dataset (longer data is thinned evenly)."""
RECIPE_VERSION: Final = "1.0.0"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_PALETTE_LIMIT: Final = 12

Row = dict[str, Any]
Plot = dict[str, Any]


def _thin(rows: list[Row], limit: int = MAX_ROWS) -> list[Row]:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[int(i * step)] for i in range(limit)]


def _xtype(values: Iterable[Any]) -> str:
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            return "nominal"
        if isinstance(v, int | float):
            return "quantitative"
        if isinstance(v, str) and _DATE.match(v):
            return "temporal"
        return "ordinal"
    return "ordinal"


def _facet_label(facet: Mapping[str, Any] | None) -> str:
    if not facet:
        return "all"
    return ", ".join(f"{k}={v}" for k, v in facet.items())[:200]


def _plot(
    section: str,
    name: str,
    title: str,
    spec: dict[str, Any],
    rows: Mapping[str, list[Row]],
    caption: str | None,
) -> Plot:
    body = dict(spec)
    body["datasets"] = {k: _thin(v) for k, v in rows.items()}
    return {
        "id": f"explore.{section}.{name}",
        "title": title[:300],
        "kind": "vega-lite",
        "spec": body,
        "datasets": {},
        "schema_version": "v6",
        "persona": "both",
        "caption_plain": (caption or None) and str(caption)[:4000],
        "provenance": {
            "recipe": f"explore.{section}.{name}@{RECIPE_VERSION}",
            "generated_at": datetime.now(UTC).isoformat(),
        },
    }


def _q(field: str, title: str | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"field": field, "type": "quantitative"}
    if title is not None:
        out["title"] = title
    out.update(extra)
    return out


def _n(field: str, title: str | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"field": field, "type": "nominal"}
    if title is not None:
        out["title"] = title
    out.update(extra)
    return out


def _facet_spec(
    layer: dict[str, Any], field: str, *, columns: int = 3, independent: bool = True
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "data": layer.pop("data"),
        "facet": {"field": field, "type": "nominal", "title": None},
        "columns": columns,
        "spec": layer,
    }
    if independent:
        out["resolve"] = {"scale": {"y": "independent", "x": "independent"}}
    return out


# ---------------------------------------------------------------------- Data Explorer


def _overview(v: Mapping[str, Any]) -> list[Plot]:
    rows = [
        {"var": x["name"], "missing": x.get("missing_share", 0.0), "role": x.get("role") or "-"}
        for x in v.get("vars", [])
    ]
    if not rows:
        return []
    spec = {
        "data": {"name": "vars"},
        "mark": {"type": "bar"},
        "encoding": {
            "y": _n("var", "Variable", sort="-x"),
            "x": _q("missing", "Share missing", axis={"format": "%"}),
            "color": _n("role", "Role"),
        },
    }
    return [
        _plot(
            "overview",
            "missing",
            "Missing values by variable",
            spec,
            {"vars": rows},
            "How much of each variable is missing.",
        )
    ]


def _distributions(v: Mapping[str, Any]) -> list[Plot]:
    rows: list[Row] = []
    for item in v.get("items", []):
        h = item.get("histogram")
        if not h:
            continue
        edges, counts = h["edges"], h["counts"]
        rows += [
            {"var": item["var"], "lo": edges[i], "hi": edges[i + 1], "count": c}
            for i, c in enumerate(counts)
        ]
    if not rows:
        return []
    layer = {
        "data": {"name": "hist"},
        "mark": {"type": "bar"},
        "encoding": {
            "x": _q("lo", "Value", bin={"binned": True}),
            "x2": {"field": "hi"},
            "y": _q("count", "Count"),
        },
    }
    return [
        _plot(
            "distributions",
            "histograms",
            "Distributions",
            _facet_spec(layer, "var"),
            {"hist": rows},
            v.get("caption_plain"),
        )
    ]


def _timeseries(v: Mapping[str, Any]) -> list[Plot]:
    rows: list[Row] = []
    for s in v.get("series", []):
        label = _facet_label(s.get("facet"))
        rows += [
            {"var": s["var"], "panel": label, "x": c, "y": y}
            for c, y in zip(s["coords"], s["values"], strict=False)
            if y is not None
        ]
    if not rows:
        return []
    xt = _xtype(r["x"] for r in rows)
    layer = {
        "data": {"name": "series"},
        "mark": {"type": "line"},
        "encoding": {
            "x": {"field": "x", "type": xt, "title": v.get("dim")},
            "y": _q("y", "Value"),
            "color": _n("panel", "Series"),
        },
    }
    return [
        _plot(
            "timeseries",
            "lines",
            "Over time",
            _facet_spec(layer, "var"),
            {"series": rows},
            v.get("caption_plain"),
        )
    ]


def _missingness(v: Mapping[str, Any]) -> list[Plot]:
    g = v.get("grid")
    if not g:
        return []
    rows = [
        {"var": lab, "x": c, "share": f}
        for lab, fr in zip(g["labels"], g["fraction"], strict=False)
        for c, f in zip(g["coords"], fr, strict=False)
    ]
    xt = "ordinal" if _xtype(r["x"] for r in rows) != "temporal" else "temporal"
    spec = {
        "data": {"name": "grid"},
        "mark": {"type": "rect"},
        "encoding": {
            "x": {"field": "x", "type": xt, "title": g.get("along")},
            "y": _n("var", "Variable"),
            "color": _q("share", "Missing", scale={"domain": [0, 1]}),
        },
    }
    return [
        _plot(
            "missingness", "heatmap", "Missing values", spec, {"grid": rows}, v.get("caption_plain")
        )
    ]


def _outliers(v: Mapping[str, Any]) -> list[Plot]:
    rows = [{"var": x["var"], "count": x["count"], "share": x["share"]} for x in v.get("items", [])]
    if not rows:
        return []
    spec = {
        "data": {"name": "out"},
        "mark": {"type": "bar"},
        "encoding": {
            "y": _n("var", "Variable", sort="-x"),
            "x": _q("count", "Unusual values"),
            "tooltip": [_q("share", "Share", format=".1%")],
        },
    }
    return [
        _plot("outliers", "counts", "Unusual values", spec, {"out": rows}, v.get("caption_plain"))
    ]


def _correlation(v: Mapping[str, Any]) -> list[Plot]:
    names, m = v.get("vars", []), v.get("matrix", [])
    rows = [
        {"a": a, "b": b, "r": m[i][j]}
        for i, a in enumerate(names)
        for j, b in enumerate(names)
        if m[i][j] is not None
    ]
    if not rows:
        return []
    base = {"x": _n("a", None), "y": _n("b", None)}
    spec = {
        "data": {"name": "corr"},
        "layer": [
            {
                "mark": {"type": "rect"},
                "encoding": {
                    **base,
                    "color": _q("r", "Correlation", scale={"domain": [-1, 1], "scheme": "redblue"}),
                },
            },
            {
                "mark": {"type": "text", "fontSize": 9},
                "encoding": {**base, "text": _q("r", None, format=".2f")},
            },
        ],
    }
    return [
        _plot("correlation", "matrix", "Correlation", spec, {"corr": rows}, v.get("caption_plain"))
    ]


def _lag_rows(items: Sequence[Mapping[str, Any]], kind: str = "") -> list[Row]:
    rows: list[Row] = []
    for it in items:
        label = it["var"] + ("" if not it.get("facet") else f" ({_facet_label(it['facet'])})")
        bound = it.get("bound")
        rows += [
            {"series": label, "kind": kind, "lag": lag, "value": val, "bound": bound}
            for lag, val in zip(it["lags"], it["values"], strict=False)
            if val is not None
        ]
    return rows


def _lag_layer(name: str) -> dict[str, Any]:
    """Bars by lag with the dashed ``±bound`` significance band."""
    return {
        "data": {"name": name},
        "layer": [
            {
                "mark": {"type": "bar"},
                "encoding": {"x": _q("lag", "Lag"), "y": _q("value", "Correlation")},
            },
            {"mark": {"type": "rule", "strokeDash": [4, 4]}, "encoding": {"y": _q("bound")}},
            {
                "transform": [{"calculate": "-datum.bound", "as": "neg"}],
                "mark": {"type": "rule", "strokeDash": [4, 4]},
                "encoding": {"y": _q("neg")},
            },
        ],
    }


def _ccf(v: Mapping[str, Any]) -> list[Plot]:
    rows = _lag_rows(v.get("items", []))
    if not rows:
        return []
    return [
        _plot(
            "ccf",
            "bars",
            f"Correlation with {v.get('kpi')} by lag",
            _facet_spec(_lag_layer("ccf"), "series"),
            {"ccf": rows},
            v.get("caption_plain"),
        )
    ]


def _acf_pacf(v: Mapping[str, Any]) -> list[Plot]:
    out: list[Plot] = []
    for kind in ("acf", "pacf"):
        rows = _lag_rows(v.get(kind, []), kind)
        if rows:
            title = "Autocorrelation" if kind == "acf" else "Partial autocorrelation"
            out.append(
                _plot(
                    "acf_pacf",
                    kind,
                    title,
                    _facet_spec(_lag_layer(kind), "series"),
                    {kind: rows},
                    v.get("caption_plain"),
                )
            )
    return out


def _stationarity(v: Mapping[str, Any]) -> list[Plot]:
    rows: list[Row] = []
    for r in v.get("rows", []):
        label = r["var"] + ("" if not r.get("facet") else f" ({_facet_label(r['facet'])})")
        for test in ("adf", "kpss"):
            t = r.get(test)
            if t and t.get("p_value") is not None:
                rows.append(
                    {
                        "series": label,
                        "test": test.upper(),
                        "p": t["p_value"],
                        "verdict": r.get("verdict"),
                    }
                )
    if not rows:
        return []
    alpha = v.get("alpha", 0.05)
    spec = {
        "data": {"name": "st"},
        "layer": [
            {
                "mark": {"type": "point", "filled": True, "size": 60},
                "encoding": {
                    "y": _n("series", "Series"),
                    "x": _q("p", "p-value", scale={"domain": [0, 1]}),
                    "color": _n("test", "Test"),
                    "tooltip": [_n("verdict", "Verdict")],
                },
            },
            {
                "data": {"values": [{"alpha": alpha}]},
                "mark": {"type": "rule", "strokeDash": [4, 4]},
                "encoding": {"x": _q("alpha")},
            },
        ],
    }
    return [
        _plot(
            "stationarity",
            "pvalues",
            "Unit-root and stationarity tests",
            spec,
            {"st": rows},
            "If ADF is below the dashed line, the series is stable. If KPSS is below it, the "
            "series wanders.",
        )
    ]


def _stl(v: Mapping[str, Any]) -> list[Plot]:
    rows: list[Row] = []
    for it in v.get("items", [])[:12]:
        label = it["var"] + ("" if not it.get("facet") else f" ({_facet_label(it['facet'])})")
        for comp in ("observed", "trend", "seasonal", "resid"):
            rows += [
                {"series": label, "component": comp, "x": c, "y": y}
                for c, y in zip(it["coords"], it[comp], strict=False)
                if y is not None
            ]
    if not rows:
        return []
    xt = _xtype(r["x"] for r in rows)
    layer = {
        "data": {"name": "stl"},
        "mark": {"type": "line"},
        "encoding": {
            "x": {"field": "x", "type": xt, "title": None},
            "y": _q("y", None),
            "color": _n("series", "Series"),
            "row": _n("component", None, sort=["observed", "trend", "seasonal", "resid"]),
        },
        "resolve": {"scale": {"y": "independent"}},
    }
    return [
        _plot(
            "stl",
            "components",
            "Trend, season and remainder",
            layer,
            {"stl": rows},
            v.get("caption_plain"),
        )
    ]


def _vif(v: Mapping[str, Any]) -> list[Plot]:
    rows = [{"driver": k, "vif": x} for k, x in (v.get("vif") or {}).items() if x is not None]
    if not rows:
        return []
    spec = {
        "data": {"name": "vif"},
        "layer": [
            {
                "mark": {"type": "bar"},
                "encoding": {
                    "y": _n("driver", "Driver", sort="-x"),
                    "x": _q("vif", "Variance inflation"),
                },
            },
            {
                "data": {"values": [{"limit": v.get("limit", 10.0)}]},
                "mark": {"type": "rule", "color": "firebrick"},
                "encoding": {"x": _q("limit")},
            },
        ],
    }
    return [
        _plot("vif", "bars", "Overlap between drivers", spec, {"vif": rows}, v.get("caption_plain"))
    ]


def _groups(v: Mapping[str, Any]) -> list[Plot]:
    rows: list[Row] = []
    for it in v.get("items", []):
        for g in it.get("groups", [])[:200]:
            if g.get("mean") is None:
                continue
            sd = g.get("sd") or 0.0
            rows.append(
                {
                    "var": it["var"],
                    "group": str(g["group"]),
                    "mean": g["mean"],
                    "lo": g["mean"] - sd,
                    "hi": g["mean"] + sd,
                }
            )
    if not rows:
        return []
    layer = {
        "data": {"name": "groups"},
        "layer": [
            {
                "mark": {"type": "bar"},
                "encoding": {"x": _n("group", v.get("by")), "y": _q("mean", "Mean")},
            },
            {
                "mark": {"type": "rule"},
                "encoding": {"x": _n("group"), "y": _q("lo"), "y2": {"field": "hi"}},
            },
        ],
    }
    return [
        _plot(
            "groups",
            "means",
            f"By {v.get('by')}",
            _facet_spec(layer, "var"),
            {"groups": rows},
            v.get("caption_plain"),
        )
    ]


def _quality(v: Mapping[str, Any]) -> list[Plot]:
    rows = [
        {"code": i["code"], "severity": i.get("severity", "warning")} for i in v.get("items", [])
    ]
    if not rows:
        return []
    spec = {
        "data": {"name": "q"},
        "mark": {"type": "bar"},
        "encoding": {
            "y": _n("code", "Problem"),
            "x": {"aggregate": "count", "type": "quantitative", "title": "Count"},
            "color": _n(
                "severity",
                "Severity",
                scale={
                    "domain": ["error", "warning", "info"],
                    "range": ["#c0392b", "#e67e22", "#7f8c8d"],
                },
            ),
        },
    }
    return [_plot("quality", "counts", "Data problems", spec, {"q": rows}, v.get("caption_plain"))]


# ------------------------------------------------------------- Data Explorer: scientific data


def _structure(v: Mapping[str, Any]) -> list[Plot]:
    roles = {d["name"]: d.get("role_in_data", "other") for d in v.get("dims", [])}
    sizes = {d["name"]: d.get("size") for d in v.get("dims", [])}
    rows = [
        {"var": x["name"], "dim": d, "role": roles.get(d, "other"), "size": sizes.get(d)}
        for x in v.get("vars", [])
        for d in x.get("dims", [])
    ]
    if not rows:
        return []
    base = {"x": _n("dim", "Dimension"), "y": _n("var", "Variable")}
    spec = {
        "data": {"name": "cells"},
        "layer": [
            {"mark": {"type": "rect"}, "encoding": {**base, "color": _n("role", "Part")}},
            {
                "mark": {"type": "text", "fontSize": 10},
                "encoding": {**base, "text": {"field": "size", "type": "quantitative"}},
            },
        ],
    }
    return [
        _plot(
            "structure",
            "matrix",
            "Variables and dimensions",
            spec,
            {"cells": rows},
            "Each cell is a dimension of a variable, coloured by the part the dimension plays. "
            "The number is its size.",
        )
    ]


def _images(v: Mapping[str, Any]) -> list[Plot]:
    stats_rows: list[Row] = []
    hist_rows: list[Row] = []
    for f in v.get("fields", []):
        name = f.get("var") or f.get("output")
        label = name if not f.get("facet") else f"{name} ({_facet_label(f.get('facet'))})"
        fs = f.get("frame_stats")
        if fs:
            for i, c in enumerate(fs.get("coords", [])):
                for stat in ("mean", "min", "max"):
                    y = fs[stat][i]
                    if y is not None:
                        stats_rows.append({"image": label, "frame": c, "stat": stat, "value": y})
        h = f.get("histogram")
        if h:
            edges = h["edges"]
            hist_rows += [
                {"image": label, "lo": edges[i], "hi": edges[i + 1], "count": k}
                for i, k in enumerate(h["counts"])
            ]
    plots: list[Plot] = []
    if stats_rows:
        xt = _xtype(r["frame"] for r in stats_rows)
        layer = {
            "data": {"name": "frames"},
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "frame", "type": xt, "title": "Frame"},
                "y": _q("value", "Value"),
                "color": _n("stat", "Statistic"),
            },
        }
        plots.append(
            _plot(
                "images",
                "frames",
                "Each frame: mean, minimum and maximum",
                _facet_spec(layer, "image", columns=2),
                {"frames": stats_rows},
                "The mean, the smallest and the largest value of each frame.",
            )
        )
    if hist_rows:
        layer = {
            "data": {"name": "hist"},
            "mark": {"type": "bar"},
            "encoding": {
                "x": _q("lo", "Value", bin={"binned": True}),
                "x2": {"field": "hi"},
                "y": _q("count", "Cells"),
            },
        }
        plots.append(
            _plot(
                "images",
                "histograms",
                "Values of all the cells",
                _facet_spec(layer, "image", columns=2),
                {"hist": hist_rows},
                "How the values of all the cells (all frames) are spread out.",
            )
        )
    return plots


def _profiles(v: Mapping[str, Any]) -> list[Plot]:
    plots: list[Plot] = []
    for k, it in enumerate(v.get("items", [])[:8]):
        rows = [
            {"x": c, "y": y, "frame": str(fr.get("label", "all"))}
            for fr in it.get("frames", [])
            for c, y in zip(it.get("coords", []), fr.get("values", []), strict=False)
            if y is not None and c is not None
        ]
        if not rows:
            continue
        xscale = {"type": "symlog"} if it.get("log_x") else {}
        yscale = {"type": "log"} if it.get("log_y") else {}
        units = f" ({it['units']})" if it.get("units") else ""
        spec = {
            "data": {"name": "curves"},
            "mark": {"type": "line"},
            "encoding": {
                "x": _q("x", it.get("along"), scale=xscale),
                "y": _q("y", f"{it['var']}{units}", scale=yscale),
                "color": {
                    "field": "frame",
                    "type": "ordinal",
                    "title": it.get("frame_dim") or "Curve",
                    "scale": {"scheme": "viridis"},
                },
            },
        }
        plots.append(
            _plot(
                "profiles",
                f"curve{k}",
                f"{it['var']} along {it.get('along')}",
                spec,
                {"curves": rows},
                v.get("caption_plain"),
            )
        )
    return plots


def _map(v: Mapping[str, Any]) -> list[Plot]:
    areas = v.get("areas", [])
    n = len(areas)
    nbrs: list[list[int]] = [[] for _ in range(n)]
    for i, j in v.get("edges", []):
        if 0 <= i < n and 0 <= j < n:
            nbrs[i].append(j)
            nbrs[j].append(i)
    plots: list[Plot] = []
    bars: list[Row] = []
    scatter: list[Row] = []
    for layer in v.get("layers", [])[:6]:
        vals = layer.get("values", [])
        for i, a in enumerate(areas):
            if i < len(vals) and vals[i] is not None:
                bars.append({"layer": layer["var"], "area": str(a), "value": vals[i]})
        if not any(nbrs):
            continue
        for i in range(min(n, len(vals))):
            near = [vals[j] for j in nbrs[i] if j < len(vals) and vals[j] is not None]
            if vals[i] is not None and near:
                scatter.append(
                    {
                        "layer": layer["var"],
                        "area": str(areas[i]),
                        "value": vals[i],
                        "lag": sum(near) / len(near),
                    }
                )
    if scatter:
        layer_spec = {
            "data": {"name": "moran"},
            "layer": [
                {
                    "mark": {"type": "point", "filled": True, "opacity": 0.7},
                    "encoding": {
                        "x": _q("value", "Value of the area"),
                        "y": _q("lag", "Mean of its neighbours"),
                        "tooltip": [_n("area", "Area"), _q("value", "Value"), _q("lag", "Lag")],
                    },
                },
                {
                    "mark": {"type": "line", "color": "#888"},
                    "transform": [{"regression": "lag", "on": "value"}],
                    "encoding": {"x": _q("value"), "y": _q("lag")},
                },
            ],
        }
        plots.append(
            _plot(
                "map",
                "moran",
                "Each area against its neighbours",
                _facet_spec(layer_spec, "layer", columns=3),
                {"moran": scatter},
                "A rising line shows that neighbouring areas have similar values (Moran's I).",
            )
        )
    if bars:
        layer_spec = {
            "data": {"name": "areas"},
            "mark": {"type": "bar"},
            "encoding": {
                "y": _n("area", "Area", sort="-x"),
                "x": _q("value", "Value"),
            },
        }
        plots.append(
            _plot(
                "map",
                "areas",
                "Value of each area",
                _facet_spec(layer_spec, "layer", columns=3),
                {"areas": bars},
                v.get("caption_plain"),
            )
        )
    return plots


def _network(v: Mapping[str, Any]) -> list[Plot]:
    plots: list[Plot] = []
    for k, it in enumerate(v.get("items", [])[:2]):
        labels = [str(x) for x in it.get("labels", [])]
        title = it["var"] + (f" ({_facet_label(it.get('facet'))})" if it.get("facet") else "")
        m, order = it.get("matrix"), it.get("order", [])
        if m:
            names = [labels[i] for i in order]
            rows = [
                {"a": names[a], "b": names[b], "w": m[a][b], "ia": a, "ib": b}
                for a in range(len(names))
                for b in range(len(names))
                if m[a][b] is not None and m[a][b] != 0
            ]
            if rows:
                spec = {
                    "data": {"name": "matrix"},
                    "mark": {"type": "rect"},
                    "encoding": {
                        "x": _n("b", None, sort={"field": "ib"}, axis={"labels": len(names) <= 40}),
                        "y": _n("a", None, sort={"field": "ia"}, axis={"labels": len(names) <= 40}),
                        "color": _q("w", "Link"),
                    },
                }
                plots.append(
                    _plot(
                        "network",
                        f"matrix{k}",
                        f"{title}: matrix",
                        spec,
                        {"matrix": rows},
                        "Linked pairs, with similar nodes put together.",
                    )
                )
        pos = it.get("positions")
        if pos:
            xs, ys = pos["x"], pos["y"]
            deg = it.get("degree") or [0.0] * len(labels)
            nodes = [
                {"node": labels[i], "x": xs[i], "y": ys[i], "degree": deg[i]}
                for i in range(len(labels))
            ]
            links = [
                {"x": xs[e["i"]], "y": ys[e["i"]], "x2": xs[e["j"]], "y2": ys[e["j"]], "w": e["w"]}
                for e in it.get("edges", [])
            ]
            spec = {
                "layer": [
                    {
                        "data": {"name": "links"},
                        "mark": {"type": "rule", "opacity": 0.35},
                        "encoding": {
                            "x": _q("x", None, axis=None),
                            "y": _q("y", None, axis=None),
                            "x2": {"field": "x2"},
                            "y2": {"field": "y2"},
                        },
                    },
                    {
                        "data": {"name": "nodes"},
                        "mark": {"type": "circle", "opacity": 0.9},
                        "encoding": {
                            "x": _q("x", None, axis=None),
                            "y": _q("y", None, axis=None),
                            "size": _q("degree", "Links"),
                            "tooltip": [_n("node", "Node"), _q("degree", "Links")],
                        },
                    },
                ]
            }
            plots.append(
                _plot(
                    "network",
                    f"graph{k}",
                    f"{title}: drawing",
                    spec,
                    {"links": links, "nodes": nodes},
                    v.get("caption_plain"),
                )
            )
    return plots


def _tree(v: Mapping[str, Any]) -> list[Plot]:
    lay = v.get("layout") or {}
    xs, ys, parent = lay.get("x", []), lay.get("y", []), lay.get("parent", [])
    branches: list[Row] = []
    for i, p in enumerate(parent):
        if 0 <= p < len(xs):
            branches.append({"x": xs[p], "x2": xs[i], "y": ys[i], "y2": ys[i]})
            branches.append({"x": xs[p], "x2": xs[p], "y": ys[p], "y2": ys[i]})
    if not branches:
        return []
    tips = v.get("tips", [])
    labels = lay.get("label", [])
    tip_rows = [{"y": ys[t], "label": str(labels[t] or t)} for t in tips if t < len(ys)]
    tree_spec: dict[str, Any] = {
        "width": 360,
        "layer": [
            {
                "data": {"name": "branches"},
                "mark": {"type": "rule"},
                "encoding": {
                    "x": _q("x", "Distance from the root"),
                    "x2": {"field": "x2"},
                    "y": _q("y", None, axis=None, scale={"reverse": True}),
                    "y2": {"field": "y2"},
                },
            },
            {
                "data": {"name": "tips"},
                "mark": {"type": "tick", "orient": "horizontal", "opacity": 0},
                "encoding": {
                    "y": _q("y", None, axis=None, scale={"reverse": True}),
                    "tooltip": [_n("label", "Tip")],
                },
            },
        ],
    }
    trait_rows = [
        {"trait": t["var"], "y": ys[tips[i]], "value": val, "label": str(labels[tips[i]] or "")}
        for t in v.get("traits", [])[:6]
        for i, val in enumerate(t.get("values", []))
        if val is not None and i < len(tips) and tips[i] < len(ys)
    ]
    data: dict[str, list[Row]] = {"branches": branches, "tips": tip_rows}
    if not trait_rows:
        spec = tree_spec
    else:
        data["traits"] = trait_rows
        strips = {
            "data": {"name": "traits"},
            "facet": {"column": {"field": "trait", "type": "nominal", "title": None}},
            "spec": {
                "width": 120,
                "mark": {"type": "bar", "orient": "horizontal"},
                "encoding": {
                    "y": _q("y", None, axis=None, scale={"reverse": True}),
                    "x": _q("value", None),
                    "tooltip": [_n("label", "Tip"), _q("value", "Value")],
                },
            },
            "resolve": {"scale": {"x": "independent"}},
        }
        spec = {"hconcat": [tree_spec, strips], "resolve": {"scale": {"y": "shared"}}}
    return [_plot("tree", "dendrogram", "Tree and traits", spec, data, v.get("caption_plain"))]


def _circular(v: Mapping[str, Any]) -> list[Plot]:
    rows: list[Row] = []
    for it in v.get("items", []):
        bins = it.get("bins", [])
        for g in it.get("groups", []):
            total = sum(g.get("counts", [])) or 1
            for i, k in enumerate(g.get("counts", [])):
                rows.append(
                    {
                        "var": it["var"],
                        "group": str(g.get("label")),
                        "lo": bins[i],
                        "hi": bins[i + 1],
                        "share": k / total,
                    }
                )
    plots: list[Plot] = []
    if rows:
        layer = {
            "data": {"name": "bins"},
            "mark": {"type": "bar"},
            "encoding": {
                "x": _q("lo", "Angle or time", bin={"binned": True}),
                "x2": {"field": "hi"},
                "y": _q("share", "Share", axis={"format": "%"}),
                "color": _n("group", "Group"),
                "yOffset": _n("group"),
            },
        }
        plots.append(
            _plot(
                "circular",
                "bins",
                "Values around the circle",
                _facet_spec(layer, "var"),
                {"bins": rows},
                v.get("caption_plain"),
            )
        )
    for k, pair in enumerate(v.get("pairs", [])[:4]):
        ab, bb = pair["a_bins"], pair["b_bins"]
        cells = [
            {"a": ab[i], "a2": ab[i + 1], "b": bb[j], "b2": bb[j + 1], "n": n}
            for i, row in enumerate(pair["counts"])
            for j, n in enumerate(row)
        ]
        spec = {
            "data": {"name": "pair"},
            "mark": {"type": "rect"},
            "encoding": {
                "x": _q("a", pair["a"], bin={"binned": True}),
                "x2": {"field": "a2"},
                "y": _q("b", pair["b"], bin={"binned": True}),
                "y2": {"field": "b2"},
                "color": _q("n", "Count"),
            },
        }
        plots.append(
            _plot(
                "circular",
                f"pair{k}",
                f"{pair['a']} and {pair['b']}",
                spec,
                {"pair": cells},
                "Counts of each pair of values. A pattern shows that the two go together.",
            )
        )
    return plots


def _points(v: Mapping[str, Any]) -> list[Plot]:
    s = v.get("sample") or {}
    xs, ys = s.get("x", []), s.get("y", [])
    srgb = v.get("srgb") or []
    hexok = re.compile(r"^#[0-9a-f]{6}$")
    rows: list[Row] = []
    for i in range(min(len(xs), len(ys))):
        if xs[i] is None or ys[i] is None:
            continue
        row: Row = {"x": xs[i], "y": ys[i]}
        if i < len(srgb) and isinstance(srgb[i], str) and hexok.match(srgb[i]):
            row["hex"] = srgb[i]
        rows.append(row)
    if not rows:
        return []
    lattice = bool(v.get("lattice"))
    enc: dict[str, Any] = {
        "x": {"field": "x", "type": "ordinal" if lattice else "quantitative", "title": v.get("x")},
        "y": {"field": "y", "type": "ordinal" if lattice else "quantitative", "title": v.get("y")},
    }
    if all("hex" in r for r in rows):
        enc["color"] = {"field": "hex", "type": "nominal", "scale": None, "legend": None}
    mark = {"type": "rect"} if lattice else {"type": "circle", "opacity": 0.6, "size": 12}
    spec = {"data": {"name": "points"}, "mark": mark, "encoding": enc}
    return [_plot("points", "map", "Points", spec, {"points": rows}, v.get("caption_plain"))]


# --------------------------------------------------------------------- Model Explorer


def _params(v: Mapping[str, Any]) -> list[Plot]:
    rows: list[Row] = []
    for r in v.get("rows", [])[:300]:
        if r.get("mean") is None:
            continue
        coords = r.get("coords") or {}
        label = r["name"] + (f"[{','.join(str(x) for x in coords.values())}]" if coords else "")
        rows.append(
            {
                "param": label,
                "mean": r["mean"],
                "lo": r.get("lower", r["mean"]),
                "hi": r.get("upper", r["mean"]),
            }
        )
    if not rows:
        return []
    spec = {
        "data": {"name": "params"},
        "layer": [
            {
                "mark": {"type": "rule"},
                "encoding": {
                    "y": _n("param", None, sort=None),
                    "x": _q("lo", "Estimate"),
                    "x2": {"field": "hi"},
                },
            },
            {
                "mark": {"type": "point", "filled": True},
                "encoding": {"y": _n("param", None, sort=None), "x": _q("mean")},
            },
            {
                "data": {"values": [{"zero": 0}]},
                "mark": {"type": "rule", "strokeDash": [2, 2]},
                "encoding": {"x": _q("zero")},
            },
        ],
    }
    return [
        _plot(
            "params",
            "forest",
            f"Estimates ({v.get('interval')})",
            spec,
            {"params": rows},
            v.get("caption_plain"),
        )
    ]


def _fit(v: Mapping[str, Any]) -> list[Plot]:
    obs: list[Row] = []
    band: list[Row] = []
    for s, fan in zip(v.get("series", []), v.get("fitted", []), strict=False):
        label = _facet_label(s.get("facet"))
        obs += [
            {"panel": label, "x": c, "y": y, "what": "actual"}
            for c, y in zip(s["coords"], s["values"], strict=False)
            if y is not None
        ]
        q = fan["quantiles"]
        mid = fan["values"][q.index(0.5)] if 0.5 in q else fan["values"][0]
        lo = fan["values"][0] if len(q) > 1 else mid
        hi = fan["values"][-1] if len(q) > 1 else mid
        for c, m, a, b in zip(fan["coords"], mid, lo, hi, strict=False):
            if m is not None:
                band.append({"panel": label, "x": c, "y": m, "lo": a, "hi": b, "what": "fitted"})
    if not obs:
        return []
    xt = _xtype(r["x"] for r in obs)
    x = {"field": "x", "type": xt, "title": v.get("dim")}
    layers: list[dict[str, Any]] = [
        {
            "data": {"name": "fitted"},
            "mark": {"type": "area", "opacity": 0.25},
            "encoding": {"x": x, "y": _q("lo", None), "y2": {"field": "hi"}},
        },
        {
            "data": {"name": "fitted"},
            "mark": {"type": "line"},
            "encoding": {"x": x, "y": _q("y", "Value"), "color": _n("what", None)},
        },
        {
            "data": {"name": "actual"},
            "mark": {"type": "line", "strokeDash": [3, 2]},
            "encoding": {"x": x, "y": _q("y"), "color": _n("what", None)},
        },
    ]
    if v.get("split_at") is not None:
        layers.append(
            {
                "data": {"values": [{"split": v["split_at"]}]},
                "mark": {"type": "rule"},
                "encoding": {"x": {"field": "split", "type": xt}},
            }
        )
    # one plot per panel (Vega-Lite cannot facet a layer drawn from two datasets)
    out: list[Plot] = []
    for k, panel in enumerate(dict.fromkeys(r["panel"] for r in obs)):
        if k >= 6:
            break
        title = "Actual and fitted" + ("" if panel == "all" else f" ({panel})")
        out.append(
            _plot(
                "fit",
                f"actual_vs_fitted{k}",
                title,
                {"layer": layers},
                {
                    "actual": [r for r in obs if r["panel"] == panel],
                    "fitted": [r for r in band if r["panel"] == panel],
                },
                v.get("caption_plain"),
            )
        )
    return out


def _residuals(v: Mapping[str, Any]) -> list[Plot]:
    out: list[Plot] = []
    rows: list[Row] = []
    for s in v.get("series", []):
        label = _facet_label(s.get("facet"))
        rows += [
            {"panel": label, "x": c, "r": y}
            for c, y in zip(s["coords"], s["values"], strict=False)
            if y is not None
        ]
    if rows:
        xt = _xtype(r["x"] for r in rows)
        spec = {
            "data": {"name": "resid"},
            "layer": [
                {
                    "mark": {"type": "line"},
                    "encoding": {
                        "x": {"field": "x", "type": xt, "title": None},
                        "y": _q("r", "Residual"),
                        "color": _n("panel", None),
                    },
                },
                {
                    "data": {"values": [{"zero": 0}]},
                    "mark": {"type": "rule"},
                    "encoding": {"y": _q("zero")},
                },
            ],
        }
        out.append(
            _plot(
                "residuals",
                "over_time",
                "Residuals over time",
                spec,
                {"resid": rows},
                v.get("caption_plain"),
            )
        )
    qq = v.get("qq")
    if qq and qq.get("theoretical"):
        pts = [{"t": a, "s": b} for a, b in zip(qq["theoretical"], qq["sample"], strict=False)]
        spec = {
            "data": {"name": "qq"},
            "layer": [
                {
                    "mark": {"type": "point"},
                    "encoding": {
                        "x": _q("t", "Normal quantile"),
                        "y": _q("s", "Residual quantile"),
                    },
                },
                {
                    "mark": {"type": "line", "color": "gray"},
                    "encoding": {"x": _q("t"), "y": _q("t")},
                },
            ],
        }
        out.append(
            _plot(
                "residuals",
                "qq",
                "Normal QQ plot of residuals",
                spec,
                {"qq": pts},
                "If the points are on the line, the residuals follow a bell curve.",
            )
        )
    acf = _lag_rows(v.get("acf", []))
    if acf:
        out.append(
            _plot(
                "residuals",
                "acf",
                "Autocorrelation of residuals",
                _facet_spec(_lag_layer("racf"), "series"),
                {"racf": acf},
                "If bars go out of the dashed band, the residuals still have a pattern.",
            )
        )
    return out


def _decomposition(v: Mapping[str, Any]) -> list[Plot]:
    rows = [
        {"component": k, "x": c, "y": y}
        for k, vals in (v.get("components") or {}).items()
        for c, y in zip(v.get("coords", []), vals, strict=False)
        if y is not None
    ]
    if not rows:
        return []
    xt = _xtype(r["x"] for r in rows)
    spec = {
        "data": {"name": "dec"},
        "mark": {"type": "area"},
        "encoding": {
            "x": {"field": "x", "type": xt, "title": v.get("dim")},
            "y": _q("y", "Contribution", stack="zero"),
            "color": _n("component", "Part"),
        },
    }
    return [
        _plot(
            "decomposition",
            "stacked",
            "What makes up the prediction",
            spec,
            {"dec": rows},
            v.get("caption_plain"),
        )
    ]


def _response_curves(v: Mapping[str, Any]) -> list[Plot]:
    rows: list[Row] = []
    lower, upper = v.get("lower"), v.get("upper")
    for i, ch in enumerate(v.get("channels", [])):
        for j, (s, m) in enumerate(zip(v["spend"][i], v["mean"][i], strict=False)):
            rows.append(
                {
                    "channel": ch,
                    "spend": s,
                    "mean": m,
                    "lo": lower[i][j] if lower else m,
                    "hi": upper[i][j] if upper else m,
                }
            )
    if not rows:
        return []
    layer = {
        "data": {"name": "rc"},
        "layer": [
            {
                "mark": {"type": "area", "opacity": 0.25},
                "encoding": {"x": _q("spend", "Spend"), "y": _q("lo", None), "y2": {"field": "hi"}},
            },
            {
                "mark": {"type": "line"},
                "encoding": {"x": _q("spend"), "y": _q("mean", "Contribution")},
            },
        ],
    }
    return [
        _plot(
            "response_curves",
            "curves",
            "Response curves",
            _facet_spec(layer, "channel"),
            {"rc": rows},
            v.get("caption_plain"),
        )
    ]


def _irf(v: Mapping[str, Any]) -> list[Plot]:
    names = v.get("variables", [])
    rows: list[Row] = []
    for i, resp in enumerate(names):
        for j, shock in enumerate(names):
            for h, val in enumerate(v["values"][i][j]):
                rows.append(
                    {
                        "pair": f"{shock} -> {resp}",
                        "h": h,
                        "value": val,
                        "lo": v["lower"][i][j][h] if v.get("lower") else val,
                        "hi": v["upper"][i][j][h] if v.get("upper") else val,
                    }
                )
    if not rows:
        return []
    layer = {
        "data": {"name": "irf"},
        "layer": [
            {
                "mark": {"type": "area", "opacity": 0.25},
                "encoding": {
                    "x": _q("h", "Periods after the shock"),
                    "y": _q("lo", None),
                    "y2": {"field": "hi"},
                },
            },
            {"mark": {"type": "line"}, "encoding": {"x": _q("h"), "y": _q("value", "Response")}},
        ],
    }
    return [
        _plot(
            "irf",
            "responses",
            "Impulse responses",
            _facet_spec(layer, "pair", columns=max(1, min(len(names), 4))),
            {"irf": rows},
            v.get("caption_plain"),
        )
    ]


def _fevd(v: Mapping[str, Any]) -> list[Plot]:
    names = v.get("variables", [])
    rows = [
        {"variable": names[i], "h": h + 1, "shock": names[j], "share": s}
        for i, per_h in enumerate(v.get("shares", []))
        for h, row in enumerate(per_h)
        for j, s in enumerate(row)
    ]
    if not rows:
        return []
    layer = {
        "data": {"name": "fevd"},
        "mark": {"type": "area"},
        "encoding": {
            "x": _q("h", "Horizon"),
            "y": _q("share", "Share", stack="normalize"),
            "color": _n("shock", "Shock"),
        },
    }
    return [
        _plot(
            "fevd",
            "shares",
            "Forecast-error variance decomposition",
            _facet_spec(layer, "variable"),
            {"fevd": rows},
            v.get("caption_plain"),
        )
    ]


def _forecast(v: Mapping[str, Any]) -> list[Plot]:
    out: list[Plot] = []
    for k, f in enumerate(v.get("forecasts", [])[:6]):
        fan = f["fan"]
        q = fan["quantiles"]
        mid = fan["values"][q.index(0.5)] if 0.5 in q else fan["values"][0]
        lo, hi = (fan["values"][0], fan["values"][-1]) if len(q) > 1 else (mid, mid)
        rows = [
            {"x": c, "m": m, "lo": a, "hi": b}
            for c, m, a, b in zip(fan["coords"], mid, lo, hi, strict=False)
            if m is not None
        ]
        obs = [
            {"x": c, "y": y}
            for c, y in zip(fan["coords"], fan.get("observed") or [], strict=False)
            if y is not None
        ]
        if not rows:
            continue
        xt = _xtype(r["x"] for r in rows)
        x = {"field": "x", "type": xt, "title": f.get("dim")}
        layers = [
            {
                "data": {"name": "fc"},
                "mark": {"type": "area", "opacity": 0.25},
                "encoding": {"x": x, "y": _q("lo", None), "y2": {"field": "hi"}},
            },
            {
                "data": {"name": "fc"},
                "mark": {"type": "line"},
                "encoding": {"x": x, "y": _q("m", f.get("kpi"))},
            },
        ]
        if obs:
            layers.append(
                {
                    "data": {"name": "hist"},
                    "mark": {"type": "line", "color": "black"},
                    "encoding": {"x": x, "y": _q("y")},
                }
            )
        out.append(
            _plot(
                "forecast",
                f"fan{k}",
                f"Forecast of {f.get('kpi')}",
                {"layer": layers},
                {"fc": rows, "hist": obs},
                v.get("caption_plain"),
            )
        )
    return out


def _backtest(v: Mapping[str, Any]) -> list[Plot]:
    rows = [
        {"fold": f["fold"]["index"], "mae": f["metrics"].get("mae")}
        for f in v.get("folds", [])
        if f["metrics"].get("mae") is not None
    ]
    out: list[Plot] = []
    if rows:
        out.append(_backtest_folds(v, rows))
    horizons = _backtest_horizons(v)
    if horizons is not None:
        out.append(horizons)
    return out


def _backtest_folds(v: Mapping[str, Any], rows: list[Row]) -> Plot:
    layers: list[dict[str, Any]] = [
        {
            "data": {"name": "bt"},
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "fold", "type": "ordinal", "title": "Fold"},
                "y": _q("mae", "Error (MAE)"),
            },
        },
    ]
    bench = [
        {"name": k, "mae": b.get("mae")}
        for k, b in (v.get("benchmarks") or {}).items()
        if b.get("mae") is not None
    ]
    if bench:
        layers.append(
            {
                "data": {"name": "bench"},
                "mark": {"type": "rule", "strokeDash": [4, 4]},
                "encoding": {"y": _q("mae"), "color": _n("name", "Benchmark")},
            }
        )
    return _plot(
        "backtest",
        "folds",
        "Backtest error by fold",
        {"layer": layers},
        {"bt": rows, "bench": bench},
        v.get("caption_plain"),
    )


def _horizon(key: Any) -> int | None:
    try:
        h = int(key)
    except (TypeError, ValueError):
        return None
    return h if h >= 1 else None


def _backtest_horizons(v: Mapping[str, Any]) -> Plot | None:
    """Pooled MAE over the horizon (line + points) with one light line per fold; only when
    at least two horizons have a pooled MAE."""
    found: dict[int, Any] = {}
    for k, m in (v.get("by_horizon") or {}).items():
        h = _horizon(k)
        if h is not None and isinstance(m, Mapping) and m.get("mae") is not None:
            found[h] = m["mae"]
    pooled = [{"h": h, "mae": found[h]} for h in sorted(found)]
    if len(pooled) < 2:
        return None
    per_fold = [
        {"h": h, "fold": str(f["fold"]["index"]), "mae": m.get("mae")}
        for f in v.get("folds", [])
        for k, m in (f.get("by_horizon") or {}).items()
        if (h := _horizon(k)) is not None and isinstance(m, Mapping) and m.get("mae") is not None
    ]
    x = _q("h", "Horizon (periods ahead)", axis={"tickMinStep": 1})
    layers: list[dict[str, Any]] = []
    if per_fold:
        layers.append(
            {
                "data": {"name": "folds"},
                "mark": {"type": "line", "opacity": 0.3, "color": "#888"},
                "encoding": {"x": x, "y": _q("mae", "Error (MAE)"), "detail": _n("fold")},
            }
        )
    layers.append(
        {
            "data": {"name": "pooled"},
            "mark": {"type": "line", "point": True},
            "encoding": {"x": x, "y": _q("mae", "Error (MAE)")},
        }
    )
    return _plot(
        "backtest",
        "horizons",
        "Backtest error by horizon",
        {"layer": layers},
        {"pooled": pooled, "folds": per_fold},
        "Shows the error for each number of periods ahead. The error usually increases with "
        "the horizon.",
    )


def _comparison(v: Mapping[str, Any]) -> list[Plot]:
    rows = [
        {"model": m, "metric": k, "value": x}
        for m, per in (v.get("table") or {}).items()
        for k, x in per.items()
        if x is not None and k in ("rmse", "mae", "mape", "smape", "mase", "crps")
    ]
    if not rows:
        return []
    layer = {
        "data": {"name": "cmp"},
        "mark": {"type": "bar"},
        "encoding": {
            "y": _n("model", None),
            "x": _q("value", "Error (lower is better)"),
            "color": _n("model", None, legend=None),
        },
    }
    return [
        _plot(
            "comparison",
            "metrics",
            "Model comparison",
            _facet_spec(layer, "metric"),
            {"cmp": rows},
            v.get("caption_plain"),
        )
    ]


EXPLORER_RECIPES: Final[dict[str, Callable[[Mapping[str, Any]], list[Plot]]]] = {
    "mapi.data-explorer.overview/v1": _overview,
    "mapi.data-explorer.distributions/v1": _distributions,
    "mapi.data-explorer.timeseries/v1": _timeseries,
    "mapi.data-explorer.missingness/v1": _missingness,
    "mapi.data-explorer.outliers/v1": _outliers,
    "mapi.data-explorer.correlation/v1": _correlation,
    "mapi.data-explorer.ccf/v1": _ccf,
    "mapi.data-explorer.acf-pacf/v1": _acf_pacf,
    "mapi.data-explorer.stationarity/v1": _stationarity,
    "mapi.data-explorer.stl/v1": _stl,
    "mapi.data-explorer.vif/v1": _vif,
    "mapi.data-explorer.groups/v1": _groups,
    "mapi.data-explorer.quality/v1": _quality,
    "mapi.data-explorer.structure/v1": _structure,
    "mapi.data-explorer.images/v1": _images,
    "mapi.data-explorer.profiles/v1": _profiles,
    "mapi.data-explorer.map/v1": _map,
    "mapi.data-explorer.network/v1": _network,
    "mapi.data-explorer.tree/v1": _tree,
    "mapi.data-explorer.circular/v1": _circular,
    "mapi.data-explorer.points/v1": _points,
    "mapi.model-explorer.params/v1": _params,
    "mapi.model-explorer.diagnostics/v1": lambda _v: [],
    "mapi.model-explorer.fit/v1": _fit,
    "mapi.model-explorer.residuals/v1": _residuals,
    "mapi.model-explorer.decomposition/v1": _decomposition,
    "mapi.model-explorer.response-curves/v1": _response_curves,
    "mapi.model-explorer.irf/v1": _irf,
    "mapi.model-explorer.fevd/v1": _fevd,
    "mapi.model-explorer.fields/v1": lambda _v: [],
    "mapi.model-explorer.forecast/v1": _forecast,
    "mapi.model-explorer.backtest/v1": _backtest,
    "mapi.model-explorer.comparison/v1": _comparison,
}
"""View ``format`` -> recipe (the diagnostics view is a table and the fields view has its own
canvas viewer in the web app; they have no plot)."""


def explorer_plots(view: Mapping[str, Any]) -> list[Plot]:
    """The PlotSpec documents of one explorer view (``[]`` for an unknown format)."""
    recipe = EXPLORER_RECIPES.get(str(view.get("format", "")))
    return recipe(view) if recipe is not None else []
