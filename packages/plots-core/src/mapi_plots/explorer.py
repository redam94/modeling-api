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
    if not rows:
        return []
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
    return [
        _plot(
            "backtest",
            "folds",
            "Backtest error by fold",
            {"layer": layers},
            {"bt": rows, "bench": bench},
            v.get("caption_plain"),
        )
    ]


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
