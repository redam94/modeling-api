"""The v0.17 dashboard fixtures load as the API models and their derived facts hold.

Pure JSON + pydantic (no database). The ``--check`` run of ``_derive.py`` needs the SDK
(xarray) and is skipped where it is not installed."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from importlib import util
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.api import ScoreSummary
from mapi_spec.views import CounterfactualView, FieldsView, ForecastView

_HERE = Path(__file__).resolve().parent
_spec = util.spec_from_file_location("dashboards_v017_loader", _HERE / "loader.py")
assert _spec is not None and _spec.loader is not None
golden: Any = sys.modules.setdefault(_spec.name, util.module_from_spec(_spec))
_spec.loader.exec_module(golden)

CASES = golden.case_names()
VIEW_MODELS = {
    "summary": ScoreSummary,
    "forecast": ForecastView,
    "counterfactual": CounterfactualView,
    "fields": FieldsView,
}
EXPECTED_PRESETS = {
    "forecast_bsts": "forecast",
    "forecast_sarimax": "forecast",
    "regression_conformal": "regression",
    "regression_point": "regression",
    "classifier_binary": "classifier",
    "classifier_multiclass": "classifier",
    "causal_impact": "causal_impact",
    "causal_did": "causal_impact",
    "field_lgcp": "field_map",
    "field_lgcp_forecast": "field_map",
    "field_areal": "field_map",
    "image_logistic": "image",
}


def _captures() -> list[Path]:
    out = []
    for p in sorted(_HERE.glob("*/*.json")):
        if p.parent.name == "golden":
            continue
        kind = p.name.rsplit(".", 2)[-2] if p.name.count(".") >= 2 else ""
        if kind in VIEW_MODELS:
            out.append(p)
    return out


def test_every_case_is_listed() -> None:
    assert sorted(EXPECTED_PRESETS) == CASES


@pytest.mark.parametrize("path", _captures(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_raw_captures_validate(path: Path) -> None:
    kind = path.name.rsplit(".", 2)[-2]
    VIEW_MODELS[kind].model_validate(json.loads(path.read_text()))


@pytest.mark.parametrize("name", CASES)
def test_bundle_loads_and_is_small(name: str) -> None:
    path = golden.GOLDEN / f"{name}.json"
    assert path.stat().st_size <= golden.MAX_BYTES
    m = golden.load_case(name)
    assert m.preset == EXPECTED_PRESETS[name]
    assert m.manifest.id == m.plugin
    assert m.primary_output in m.manifest.outputs
    for s in m.scores.values():
        assert s.summary.tables, f"{name}/{s.role} has no tables"
        assert not s.summary.truncated
    for rel in m.raw["derived_from"]:
        assert (golden.HERE / rel).exists(), rel


@pytest.mark.parametrize("name", CASES)
def test_every_table_has_a_centre(name: str) -> None:
    m = golden.load_case(name)
    for s in m.scores.values():
        for t in s.summary.tables:
            assert golden.centre_column(t.columns), f"{name}/{s.role}/{t.variable}"
            facts = m.facts["tables"][s.role][t.variable]
            assert facts["n_rows"] == len(t.rows)
            assert facts["centre"] == golden.centre_column(t.columns)


def test_point_aggregates_are_flagged_and_use_point() -> None:
    for name in ("regression_point", "classifier_binary", "classifier_multiclass"):
        agg = golden.load_case(name).facts["aggregates"]["aggregate.aggregate"]
        assert agg["aggregation_flagged"] is True
        assert "point" in agg["tables"]["y_hat"]["columns"]


def test_summed_bounds_are_not_the_band_of_the_total() -> None:
    """A draws total (ForecastView.total) is not the sum of the per-period bounds (bsts:
    summed bounds 33 % too wide; lgcp: skewed HDIs, 7 % too narrow and shifted)."""
    for name, role in (("forecast_bsts", "predictive"), ("field_lgcp_forecast", "forecast")):
        m = golden.load_case(name)
        f = m.scores[role].forecast
        assert f is not None and f.total is not None
        facts = m.facts["tables"][role][f.kpi]
        q = f.total.quantiles
        width = q["0.95"] - q["0.05"]
        assert abs((facts["sum_high"] - facts["sum_low"]) - width) > 0.05 * width
        assert abs(facts["sum_low"] - q["0.05"]) > 0.05 * width


def test_predictive_findings_of_the_probe() -> None:
    def widens(name: str, key: str) -> bool:
        return bool(golden.load_case(name).facts["predictive"][key]["widens"])

    assert widens("forecast_bsts", "predictive.y_hat")
    assert not widens("forecast_bsts", "predictive.level")
    assert not widens("forecast_sarimax", "predictive.y_hat")
    assert not widens("regression_conformal", "predictive.y_hat")
    assert widens("field_lgcp", "predictive.rate")
    assert not widens("field_lgcp", "predictive.total")
    assert widens("field_areal", "predictive.cases")
    assert not widens("field_areal", "predictive.rr")
    assert not widens("image_logistic", "predictive.y_hat")
    for name in ("regression_point", "classifier_binary", "classifier_multiclass"):
        codes = {r["probe_case"]: r["code"] for r in golden.load_case(name).refusals}
        assert codes["predict_tail__predictive"] == "CAPABILITY_UNSUPPORTED"


@pytest.mark.parametrize("name", [n for n in CASES if golden.raw_case(n)["actuals"]])
def test_actuals_are_valid_uploads(name: str) -> None:
    m = golden.load_case(name)
    body = m.actuals_submit("golden")
    assert m.actuals is not None
    target = m.actuals.get("kpi_column") or m.actuals["label_column"]
    assert target in body.inputs.data_vars
    assert len(m.actuals["table"]["rows"]) == (
        m.actuals["expected"].get("n") or m.actuals["expected"].get("n_periods")
    )


@pytest.mark.parametrize(
    "name", ["forecast_bsts", "forecast_sarimax", "regression_conformal", "regression_point"]
)
def test_error_facts_recompute(name: str) -> None:
    """mae/rmse/band hits of the derived actuals, recomputed here in plain Python."""
    m = golden.load_case(name)
    a = m.actuals
    assert a is not None
    t = m.scores[a["against"]].table(m.primary_output)
    cols = t.columns
    c = cols.index(golden.centre_column(cols))
    nd = len(t.dims or [])
    obs = {tuple(r[:-1]): r[-1] for r in a["table"]["rows"]}
    errs, hits = [], []
    band = m.facts["tables"][a["against"]][m.primary_output]["band"]
    for r in t.rows:
        y = obs[tuple(r[:nd])]
        errs.append(r[c] - y)
        if band:
            lo, hi = r[cols.index(band[0])], r[cols.index(band[1])]
            hits.append(lo <= y <= hi)
    exp = a["expected"]
    assert math.isclose(exp["mae"], math.fsum(abs(e) for e in errs) / len(errs), rel_tol=1e-9)
    rmse = math.sqrt(math.fsum(e * e for e in errs) / len(errs))
    assert math.isclose(exp["rmse"], rmse, rel_tol=1e-9)
    if band:
        assert math.isclose(exp["band_hit_rate"], sum(hits) / len(hits), rel_tol=1e-12)


def test_classification_facts_recompute() -> None:
    m = golden.load_case("classifier_binary")
    a = m.actuals
    assert a is not None
    t = m.scores["baseline"].table("y_hat")
    ys = [r[-1] for r in a["table"]["rows"]]
    ps = [r[t.columns.index("mean")] for r in t.rows]
    exp = a["expected"]
    brier = math.fsum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / len(ys)
    assert math.isclose(exp["brier"], brier, rel_tol=1e-9)
    acc = sum((p >= 0.5) == (y == 1) for p, y in zip(ps, ys, strict=True)) / len(ys)
    assert math.isclose(exp["accuracy"], acc, rel_tol=1e-12)
    counts = exp["confusion"]["counts"]
    assert sum(map(sum, counts)) == len(ys)


def test_effect_orientation() -> None:
    exp = golden.load_case("causal_impact").actuals["expected"]
    q = exp["cf_total"]["quantiles"]
    assert math.isclose(exp["effect_low"], exp["actual_total"] - q["0.95"])
    assert math.isclose(exp["effect_high"], exp["actual_total"] - q["0.05"])
    assert exp["effect_low"] <= exp["effect_total"] <= exp["effect_high"]
    # the wrong band (summed per-period bounds) is wider
    assert exp["naive_effect_low_from_summed_bounds"] < exp["effect_low"]


def test_derive_is_up_to_date() -> None:
    pytest.importorskip("xarray")
    pytest.importorskip("mapi_sdk.metrics")
    proc = subprocess.run(
        [sys.executable, str(_HERE / "_derive.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
