"""Derive the v0.17 golden dashboard fixtures (``golden/<case>.json``) from the WP0 probe
captures in this directory. Not a test; run it by hand after a new probe:

    .venv/bin/python packages/server/tests/fixtures/dashboards_v017/_derive.py [--check]

Each bundle groups, for one preset, everything a v2 readout is built from (score summaries
and guided views per role, fit metrics, scorecard, input contract, manifest) plus derived
actuals and pre-computed facts. The captured files are copied, never edited. Actuals are
SYNTHETIC (deterministic offsets / seeded draws from the captured predictions); their
expected metrics come from ``mapi_sdk.metrics.compute_metrics``, the reference the plan
asks ``mapi_spec.dashboard_metrics`` to match within 1e-9. See README.md.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden"
FORMAT = "mapi.test.dashboard-material/v1"
MAX_BYTES = 200 * 1024
CENTRES = ("mean", "median", "q50", "p50", "point")
BANDS = (
    ("hdi_90_lower", "hdi_90_upper"),
    ("eti_90_lower", "eti_90_upper"),
    ("interval_lower", "interval_upper"),
)
VIEWS = ("forecast", "counterfactual", "fields")
ERROR_METRICS = ("rmse", "mae", "mape", "smape", "wape", "bias", "r2")
# k * half band width added to the centre: |k| > 1 lands outside the band.
TIME_OFFSETS = (-0.5, 0.2, 1.3, -1.6, 0.0, 0.7, -0.9, 0.4)
SEED = 17017

Json = Any

CASES: dict[str, dict[str, Any]] = {
    "forecast_bsts": {
        "preset": "forecast",
        "plugin": "ts.bsts",
        "primary": "y_hat",
        "scores": {
            "baseline": "forecast_12__readout",
            "predictive": "forecast_12__predictive",
            "aggregate": "forecast_sum",
        },
        "actuals": "time",
        "note": "draws: ForecastView.total is the band of the horizon total; `level` is "
        "latent (predictive does not widen it)",
    },
    "forecast_sarimax": {
        "preset": "forecast",
        "plugin": "ts.sarimax",
        "primary": "y_hat",
        "scores": {
            "baseline": "forecast_8__readout",
            "predictive": "forecast_8__predictive",
            "in_sample": "predict_tail__readout",
        },
        "refusals": ["forecast_sum_geo"],
        "actuals": "time",
        "note": "quantiles: no forecast.json; a sum over geo or time is refused "
        "(CAPABILITY_UNSUPPORTED); predictive is a no-op (already an outcome band)",
    },
    "regression_conformal": {
        "preset": "regression",
        "plugin": "sklearn.tabular_conformal",
        "primary": "y_hat",
        "scores": {
            "baseline": "forecast_4__readout",
            "predictive": "forecast_4__predictive",
            "rows": "forecast_inline",
        },
        "refusals": ["predict_new_sku_inline", "predict_new_sku__readout"],
        "actuals": "row_value",
        "note": "interval: centre `point`, band interval_lower/upper; new sku (fixed dim) "
        "refused UNSEEN_LEVELS",
    },
    "regression_point": {
        "preset": "regression",
        "plugin": "sklearn.tabular_regressor",
        "primary": "y_hat",
        "scores": {
            "baseline": "predict_tail__readout",
            "aggregate": "predict_sum_store",
            "rows_two": "inline_two_rows",
            "rows_new_store": "inline_new_store",
            "rows_next_week": "inline_next_week",
        },
        "refusals": ["inline_gap_week", "predict_tail__predictive", "inline_two_rows__predictive"],
        "actuals": "row_value",
        "note": "point: no band; the aggregate has column `point` and aggregation_flagged; "
        "kind=predictive is refused before any run",
    },
    "classifier_binary": {
        "preset": "classifier",
        "plugin": "sklearn.tabular_classifier",
        "primary": "y_hat",
        "scores": {
            "baseline": "predict_tail__readout",
            "aggregate": "predict_sum_store",
            "rows_two": "inline_two_rows",
            "rows_new_store": "inline_new_store",
            "rows_next_week": "inline_next_week",
        },
        "refusals": ["inline_gap_week", "predict_tail__predictive", "inline_two_rows__predictive"],
        "actuals": "row_binary",
        "note": "point: y_hat = P(positive), no class dim, no `meaning` in the manifest",
    },
    "classifier_multiclass": {
        "preset": "classifier",
        "plugin": "sklearn.multiclass_classifier",
        "primary": "y_hat",
        "scores": {
            "baseline": "predict_tail__readout",
            "aggregate": "predict_sum_store",
            "rows_two": "inline_two_rows",
            "rows_new_store": "inline_new_store",
            "rows_next_week": "inline_next_week",
        },
        "refusals": ["inline_gap_week", "predict_tail__predictive", "inline_two_rows__predictive"],
        "actuals": "row_multiclass",
        "note": "point: y_hat(class) with classes high, low, mid",
    },
    "causal_impact": {
        "preset": "causal_impact",
        "plugin": "causal.impact",
        "primary": "effect",
        "scores": {
            "baseline": "predict_over_data__readout",
            "predictive": "predict_over_data__predictive",
            "forecast": "forecast_4__readout",
            "forecast_predictive": "forecast_4__predictive",
        },
        "actuals": "effect",
        "note": "fit metrics hold effect_total{,_sd,_p05,_p95}; ModelVersion.metrics is {}",
    },
    "causal_did": {
        "preset": "causal_impact",
        "plugin": "causal.did",
        "primary": "y_hat",
        "scores": {
            "baseline": "predict_in_sample__readout",
            "predictive": "predict_in_sample__predictive",
            "counterfactual": "no_treatment",
        },
        "actuals": None,
        # 20 stores x 24 months is too big: keep 2 treated and 2 control stores in the
        # summaries (counterfactual.json and the fit metrics stay whole).
        "trim": {"dim": "store", "keep": ["store_00", "store_01", "store_04", "store_05"]},
        "note": "no new rows possible (unit and time fixed); att*, event_coef{event_time} and "
        "per-unit metrics; the counterfactual delta is in counterfactual.json",
    },
    "field_lgcp": {
        "preset": "field_map",
        "plugin": "sci.lgcp_grid",
        "primary": "rate",
        "scores": {
            "baseline": "predict_tail__readout",
            "predictive": "predict_tail__predictive",
        },
        "actuals": None,
        "note": "predict over the fitted tail; `total` ignores kind=predictive (predictive_ok "
        "false); fields.json `kind` says expected even for predictive runs - use the "
        "request's kind",
    },
    "field_lgcp_forecast": {
        "preset": "field_map",
        "plugin": "sci.lgcp_grid",
        "primary": "rate",
        "scores": {
            "forecast": "forecast_4__readout",
            "forecast_predictive": "forecast_4__predictive",
            "aggregate": "forecast_sum",
        },
        "actuals": None,
        "note": "forecast 4 periods (horizon-only scenario rows); forecast.json kpi is `total`; "
        "forecast_sum aggregates over quarter",
    },
    "field_areal": {
        "preset": "field_map",
        "plugin": "sci.areal_bym2",
        "primary": "cases",
        "scores": {
            "baseline": "predict_map__readout",
            "predictive": "predict_map__predictive",
            "simulate": "simulate",
        },
        "actuals": None,
        "note": "areal: no fields.json; cases widen under predictive, rr/effect/p_exceed do not",
    },
    "image_logistic": {
        "preset": "image",
        "plugin": "tabular.image_logistic",
        "primary": "y_hat",
        "scores": {
            "baseline": "predict_few__readout",
            "predictive": "predict_few__predictive",
        },
        "actuals": None,
        "note": "stand-in for dl.cnn_classifier (not probed); class probabilities ignore "
        "predictive",
    },
}


def _load(path: Path) -> Json:
    return json.loads(path.read_text())


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _r(v: float | None) -> float | None:
    return None if v is None or not math.isfinite(v) else float(v)


def _centre(columns: Sequence[str]) -> str | None:
    return next((c for c in CENTRES if c in columns), None)


def _band(columns: Sequence[str]) -> list[str] | None:
    return next(([lo, hi] for lo, hi in BANDS if lo in columns and hi in columns), None)


def _manifest(plugin: str) -> Json:
    from mapi_sdk import default_registry

    by = {e.id: e for e in default_registry().catalog().models}
    return by[plugin].manifest.model_dump(mode="json", exclude_defaults=True)


def _table_facts(table: Mapping[str, Any]) -> dict[str, Any]:
    cols: list[str] = table["columns"]
    rows: list[list[Any]] = table["rows"]
    centre, band = _centre(cols), _band(cols)
    out: dict[str, Any] = {
        "dims": table.get("dims"),
        "time_dim": table.get("time_dim"),
        "centre": centre,
        "band": band,
        "n_rows": len(rows),
    }
    if "class" in cols:
        ci = cols.index("class")
        out["classes"] = sorted({str(r[ci]) for r in rows})
    if centre is not None:
        out["sum_centre"] = math.fsum(_num(r[cols.index(centre)]) or 0.0 for r in rows)
    if band is not None:
        lo, hi = cols.index(band[0]), cols.index(band[1])
        out["sum_low"] = math.fsum(_num(r[lo]) or 0.0 for r in rows)
        out["sum_high"] = math.fsum(_num(r[hi]) or 0.0 for r in rows)
        widths = [(_num(r[hi]) or 0.0) - (_num(r[lo]) or 0.0) for r in rows]
        out["mean_band_width"] = math.fsum(widths) / len(widths) if widths else None
    return out


def _trim(summary: Json, trim: Mapping[str, Any] | None) -> Json:
    if trim is None:
        return summary
    keep = set(trim["keep"])
    for t in summary["tables"]:
        if trim["dim"] in t["columns"]:
            i = t["columns"].index(trim["dim"])
            t["rows"] = [r for r in t["rows"] if r[i] in keep]
    return summary


def _score(
    probe: Mapping[str, Any], plugin: str, name: str, trim: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    entry = probe["scores"][name]
    if entry.get("state") != "succeeded":
        raise SystemExit(f"{plugin}/{name} did not succeed in the probe")
    req = entry["request"]
    out: dict[str, Any] = {
        "probe_case": name,
        "mode": req["mode"],
        "horizon": req.get("horizon"),
        "output": req.get("output", {}),
        "inline": bool(entry.get("inline")),
        "trimmed": None if trim is None else {"dim": trim["dim"], "keep": sorted(trim["keep"])},
        "result": _load(HERE / plugin / f"{name}.result.json"),
        "summary": _trim(_load(HERE / plugin / f"{name}.summary.json"), trim),
    }
    for view in VIEWS:
        p = HERE / plugin / f"{name}.{view}.json"
        out[view] = _load(p) if p.exists() else None
    return out


def _refusal(probe: Mapping[str, Any], name: str) -> dict[str, Any]:
    e = probe["scores"][name]
    return {
        "probe_case": name,
        "output": e["request"].get("output", {}),
        "inline": bool(e.get("inline")),
        "code": e.get("code"),
        "refused": e.get("refused"),
        "issues": e.get("issues", []),
    }


def _canonical_names(probe: Mapping[str, Any], plugin: str, name: str) -> dict[str, str]:
    """Dataset dim name -> canonical (input table) name, from the __canonical capture."""
    base = name.split("__")[0]
    p = HERE / plugin / f"{base}__canonical.summary.json"
    if not p.exists():
        return {}
    a = _load(HERE / plugin / f"{name}.summary.json")["tables"]
    b = {t["variable"]: t for t in _load(p)["tables"]}
    out: dict[str, str] = {}
    for t in a:
        u = b.get(t["variable"])
        if u is None:
            continue
        for x, y in zip(t.get("dims") or [], u.get("dims") or [], strict=False):
            out[x] = y
    return out


def _primary(score: Mapping[str, Any], var: str) -> Mapping[str, Any]:
    return next(t for t in score["summary"]["tables"] if t["variable"] == var)


def _target(probe: Mapping[str, Any]) -> str:
    cols = probe["inputs"]["input_table"]["columns"]
    return next(c["name"] for c in cols if c["role"] == "target")


def _xr(
    table: Mapping[str, Any], names: Mapping[str, str], target: str, values: Sequence[Any]
) -> dict[str, Any]:
    """Long-format actuals as XrJsonDataset: one ``row`` dim, each coord a data var."""
    cols, rows = table["columns"], table["rows"]
    dims = [d for d in (table.get("dims") or []) if d != "class"]
    data_vars: dict[str, Any] = {
        names.get(d, d): {"dims": ["row"], "data": [r[cols.index(d)] for r in rows]} for d in dims
    }
    data_vars[target] = {"dims": ["row"], "data": list(values)}
    return {
        "coords": {"row": {"dims": ["row"], "data": list(range(len(rows)))}},
        "data_vars": data_vars,
    }


def _grid_xr(
    table: Mapping[str, Any], names: Mapping[str, str], target: str, values: Sequence[float]
) -> dict[str, Any]:
    """Time-matched actuals in the input table's shape (coords = the dims, canonical
    names), as a v1 ``ActualsSubmit.inputs`` upload holds them."""
    cols, rows = table["columns"], table["rows"]
    dims: list[str] = list(table.get("dims") or [])
    levels = {d: list(dict.fromkeys(r[cols.index(d)] for r in rows)) for d in dims}
    shape = [len(levels[d]) for d in dims]
    if math.prod(shape) != len(rows):
        raise SystemExit("time actuals need a dense table")
    flat: dict[tuple[Any, ...], float] = {
        tuple(r[cols.index(d)] for d in dims): v for r, v in zip(rows, values, strict=True)
    }

    def nest(prefix: tuple[Any, ...], i: int) -> Any:
        if i == len(dims):
            return flat[prefix]
        return [nest((*prefix, lv), i + 1) for lv in levels[dims[i]]]

    cn = [names.get(d, d) for d in dims]
    return {
        "coords": {c: {"dims": [c], "data": levels[d]} for c, d in zip(cn, dims, strict=True)},
        "data_vars": {target: {"dims": cn, "data": nest((), 0)}},
    }


def _sdk_error(pred: Sequence[float], obs: Sequence[float]) -> dict[str, float | None]:
    import xarray as xr
    from mapi_sdk.metrics import compute_metrics
    from mapi_spec.capabilities import Representation

    p = xr.DataArray(list(pred), dims=["row"])
    o = xr.DataArray(list(obs), dims=["row"])
    values, _ = compute_metrics(p, o, Representation.POINT, list(ERROR_METRICS))
    return {k: _r(v) for k, v in values.items()}


def _hits(obs: Sequence[float], bands: Sequence[tuple[float, float]]) -> float:
    return sum(lo <= a <= hi for a, (lo, hi) in zip(obs, bands, strict=True)) / len(obs)


def _time_actuals(case: Mapping[str, Any], bundle: Mapping[str, Any], probe: Json) -> Json:
    score = bundle["scores"]["predictive"]
    table = _primary(score, case["primary"])
    cols = table["columns"]
    c, (lo, hi) = cols.index(_centre(cols) or ""), [cols.index(x) for x in _band(cols) or []]
    obs, pred, bands = [], [], []
    for i, r in enumerate(table["rows"]):
        k = TIME_OFFSETS[i % len(TIME_OFFSETS)]
        a = round(r[c] + k * (r[hi] - r[lo]) / 2, 3)
        obs.append(a)
        pred.append(r[c])
        bands.append((r[lo], r[hi]))
    names = _canonical_names(probe, case["plugin"], score["probe_case"])
    target = _target(probe)
    facts = {"n": len(obs), **_sdk_error(pred, obs), "band_hit_rate": _hits(obs, bands)}
    return {
        "match": "time",
        "against": "predictive",
        "kpi_column": target,
        "names": names,
        "table": {
            "columns": [*table["dims"], "actual"],
            "rows": [
                [*r[: len(table["dims"])], a] for r, a in zip(table["rows"], obs, strict=True)
            ],
        },
        "inputs": _grid_xr(table, names, target, obs),
        "expected": facts,
    }


def _row_actuals(case: Mapping[str, Any], bundle: Mapping[str, Any], probe: Json) -> Json:
    import xarray as xr
    from mapi_sdk.metrics import BINARY_METRICS, CATEGORICAL_METRICS, compute_metrics
    from mapi_spec.capabilities import Representation

    kind = case["actuals"]
    score = bundle["scores"]["baseline"]
    table = _primary(score, case["primary"])
    cols, rows = table["columns"], table["rows"]
    names = _canonical_names(probe, case["plugin"], score["probe_case"])
    target = _target(probe)
    rng = random.Random(SEED)
    centre = _centre(cols) or ""
    c = cols.index(centre)
    keydims = [d for d in table["dims"] if d != "class"]
    if kind == "row_multiclass":
        ci = cols.index("class")
        classes = sorted({str(r[ci]) for r in rows})
        cases: dict[tuple[Any, ...], dict[str, float]] = {}
        for r in rows:
            cases.setdefault(tuple(r[cols.index(d)] for d in keydims), {})[str(r[ci])] = r[c]
        keys = list(cases)
        labels = []
        for k in keys:
            u, acc = rng.random(), 0.0
            pick = classes[-1]
            for cl in classes:
                acc += cases[k][cl]
                if u < acc:
                    pick = cl
                    break
            labels.append(pick)
        p = xr.DataArray(
            [[cases[k][cl] for cl in classes] for k in keys],
            dims=["row", "class"],
            coords={"class": classes},
        )
        o = xr.DataArray(labels, dims=["row"])
        values, _ = compute_metrics(p, o, Representation.POINT, list(CATEGORICAL_METRICS))
        flat = {
            "columns": [*keydims, "actual"],
            "rows": [[*k, y] for k, y in zip(keys, labels, strict=True)],
        }
        tab = {"dims": keydims, "columns": [*keydims, "x"], "rows": [[*k, None] for k in keys]}
        counts = {cl: labels.count(cl) for cl in classes}
        expected: dict[str, Any] = {
            "n": len(labels),
            **{k: _r(v) for k, v in values.items()},
            "label_counts": counts,
            "top_class_counts": {
                cl: sum(max(cases[k], key=lambda x: cases[k][x]) == cl for k in keys)
                for cl in classes
            },
        }
        return {
            "match": "row",
            "against": "baseline",
            "label_column": target,
            "names": names,
            "table": flat,
            "inputs": _xr(tab, names, target, labels),
            "expected": expected,
        }
    if kind == "row_binary":
        probs = [float(r[c]) for r in rows]
        ys = [1 if rng.random() < p else 0 for p in probs]
        values, _ = compute_metrics(
            xr.DataArray(probs, dims=["row"]),
            xr.DataArray(ys, dims=["row"]),
            Representation.POINT,
            list(BINARY_METRICS),
        )
        t = 0.5
        expected = {
            "n": len(ys),
            "threshold": t,
            **{k: _r(v) for k, v in values.items()},
            "rate_pred": sum(p >= t for p in probs) / len(probs),
            "rate_obs": sum(ys) / len(ys),
            "confusion": {  # rows = actual [0, 1], cols = predicted [0, 1] at threshold
                "labels": [0, 1],
                "counts": [
                    [
                        sum(y == a and (p >= t) == b for p, y in zip(probs, ys, strict=True))
                        for b in (False, True)
                    ]
                    for a in (0, 1)
                ],
            },
        }
        return {
            "match": "row",
            "against": "baseline",
            "label_column": target,
            "positive_label": "1",
            "names": names,
            "table": {
                "columns": [*keydims, "actual"],
                "rows": [[*r[: len(keydims)], y] for r, y in zip(rows, ys, strict=True)],
            },
            "inputs": _xr(table, names, target, ys),
            "expected": expected,
        }
    # row_value: regression
    band = _band(cols)
    obs, pred, bands = [], [], []
    for i, r in enumerate(rows):
        k = TIME_OFFSETS[i % len(TIME_OFFSETS)]
        if band is not None:
            lo, hi = r[cols.index(band[0])], r[cols.index(band[1])]
            half = (hi - lo) / 2
            bands.append((lo, hi))
        else:
            half = 0.1 * abs(r[c]) + 1.0
        obs.append(round(r[c] + k * half, 3))
        pred.append(r[c])
    expected = {"n": len(obs), **_sdk_error(pred, obs)}
    if bands:
        expected["band_hit_rate"] = _hits(obs, bands)
    return {
        "match": "row",
        "against": "baseline",
        "kpi_column": target,
        "names": names,
        "table": {
            "columns": [*keydims, "actual"],
            "rows": [[*r[: len(keydims)], a] for r, a in zip(rows, obs, strict=True)],
        },
        "inputs": _xr(table, names, target, obs),
        "expected": expected,
    }


def _effect_actuals(case: Mapping[str, Any], bundle: Mapping[str, Any], probe: Json) -> Json:
    score = bundle["scores"]["forecast_predictive"]
    table = _primary(score, "y_hat")
    cols = table["columns"]
    c = cols.index(_centre(cols) or "")
    lo, hi = (cols.index(x) for x in _band(cols) or [])
    lift = 3.0  # a positive effect on top of the counterfactual
    obs = [
        round(r[c] + lift + TIME_OFFSETS[i % 8] * (r[hi] - r[lo]) / 2, 3)
        for i, r in enumerate(table["rows"])
    ]
    total = score["forecast"]["total"]
    a = math.fsum(obs)
    names = _canonical_names(probe, case["plugin"], score["probe_case"])
    target = _target(probe)
    return {
        "match": "time",
        "against": "forecast_predictive",
        "kpi_column": target,
        "names": names,
        "table": {
            "columns": [*table["dims"], "actual"],
            "rows": [
                [*r[: len(table["dims"])], v] for r, v in zip(table["rows"], obs, strict=True)
            ],
        },
        "inputs": _grid_xr(table, names, target, obs),
        "expected": {
            "n_periods": len(obs),
            "actual_total": a,
            "cf_total": total,
            "effect_total": a - total["mean"],
            # interval orientation: actual minus the counterfactual's upper/lower quantile
            "effect_low": a - total["quantiles"]["0.95"],
            "effect_high": a - total["quantiles"]["0.05"],
            "effect_relative": (a - total["mean"]) / total["mean"],
            "naive_effect_low_from_summed_bounds": a - math.fsum(r[hi] for r in table["rows"]),
            "naive_effect_high_from_summed_bounds": a - math.fsum(r[lo] for r in table["rows"]),
        },
    }


def _aggregates(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """The correct band of a total, where one exists (a draws aggregate or a view)."""
    out: dict[str, Any] = {}
    for role, s in bundle["scores"].items():
        f = s.get("forecast")
        if f is not None and f.get("total") is not None:
            out[f"{role}.forecast_total"] = {"kpi": f["kpi"], "kind": f["kind"], **f["total"]}
        cf = s.get("counterfactual")
        if cf is not None:
            out[f"{role}.counterfactual_delta_total"] = cf["delta_total"]
        if s["output"].get("aggregate"):
            out[f"{role}.aggregate"] = {
                "over": s["output"]["aggregate"]["over"],
                "aggregation_flagged": s["result"].get("aggregation_flagged", False),
                "tables": {
                    t["variable"]: {"columns": t["columns"], "rows": t["rows"][:200]}
                    for t in s["summary"]["tables"]
                },
            }
    return out


def _widening(bundle: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    pairs = [("baseline", "predictive"), ("forecast", "forecast_predictive")]
    for a, b in pairs:
        if a not in bundle["scores"] or b not in bundle["scores"]:
            continue
        fa = bundle["facts"]["tables"][a]
        fb = bundle["facts"]["tables"][b]
        for var, ta in fa.items():
            tb = fb.get(var)
            wa, wb = ta.get("mean_band_width"), (tb or {}).get("mean_band_width")
            if wa is None or wb is None:
                continue
            out[f"{b}.{var}"] = {
                "expected_width": wa,
                "predictive_width": wb,
                "widens": wb > wa * (1 + 1e-6),
            }
    return out


def build(name: str, case: Mapping[str, Any]) -> dict[str, Any]:
    plugin = case["plugin"]
    probe = _load(HERE / plugin / "probe.json")
    bundle: dict[str, Any] = {
        "format": FORMAT,
        "case": name,
        "preset": case["preset"],
        "plugin": plugin,
        "representation": probe["capabilities"]["representation"],
        "primary_output": case["primary"],
        "note": case["note"],
        "derived_from": sorted(
            {f"{plugin}/probe.json"}
            | {
                f"{plugin}/{s}.{k}.json"
                for s in case["scores"].values()
                for k in ("summary", "result", *VIEWS)
                if (HERE / plugin / f"{s}.{k}.json").exists()
            }
        ),
        "manifest": _manifest(plugin),
        "capabilities": probe["capabilities"],
        "input_contract": probe["inputs"],
        "fit_metrics": probe["fit_metrics"],
        "scorecard": probe["scorecard"],
        "scores": {
            role: _score(probe, plugin, s, case.get("trim")) for role, s in case["scores"].items()
        },
        "refusals": [_refusal(probe, s) for s in case.get("refusals", [])],
    }
    bundle["facts"] = {
        "tables": {
            role: {t["variable"]: _table_facts(t) for t in s["summary"]["tables"]}
            for role, s in bundle["scores"].items()
        },
        "scalar_fit_metrics": {
            m["key"]: m["last"] for m in probe["fit_metrics"] if m["labels"] == {}
        },
    }
    bundle["facts"]["aggregates"] = _aggregates(bundle)
    bundle["facts"]["predictive"] = _widening(bundle)
    match case["actuals"]:
        case "time":
            bundle["actuals"] = _time_actuals(case, bundle, probe)
        case "row_value" | "row_binary" | "row_multiclass":
            bundle["actuals"] = _row_actuals(case, bundle, probe)
        case "effect":
            bundle["actuals"] = _effect_actuals(case, bundle, probe)
        case _:
            bundle["actuals"] = None
    return bundle


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, allow_nan=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="fail if a bundle would change")
    args = ap.parse_args(argv)
    GOLDEN.mkdir(exist_ok=True)
    stale = []
    for name, case in CASES.items():
        bundle = build(name, case)
        text = _dump(bundle)
        size = len(text.encode())
        if size > MAX_BYTES:
            raise SystemExit(f"{name}: {size} bytes > {MAX_BYTES}")
        path = GOLDEN / f"{name}.json"
        if args.check:
            # The embedded manifest follows the installed plugin (WP3 adds `meaning`):
            # report its drift, but only the captured/derived content makes a bundle stale.
            old = json.loads(path.read_text()) if path.exists() else None
            if old is None or {**old, "manifest": None} != {**bundle, "manifest": None}:
                stale.append(name)
            elif old["manifest"] != bundle["manifest"]:
                print(f"{name}: manifest of {case['plugin']} changed (rerun to refresh)")
            continue
        path.write_text(text)
        print(f"{path.relative_to(HERE)}  {size:>7} bytes")
    if stale:
        print("stale:", ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
