"""Layout 2 panels from the v0.17 golden fixtures (pure: no database, no storage).

Each golden case (``fixtures/dashboards_v017/golden``) is a real score run of one model family:
the preset compiled from its catalog resolves into snapshot blocks; these tests check the
rules of plan D5-D9 and D16 on them (no interval made by adding bounds, point centres, the
predictive band, unavailable blocks, field downsampling, family accuracy, caps, leads).
"""

from __future__ import annotations

import base64
import math
import re
import sys
from array import array
from collections.abc import Iterator
from datetime import date, timedelta
from importlib import util
from pathlib import Path
from typing import Any

import pytest
from mapi_server.services.dashboards import panels as pn
from mapi_server.services.dashboards.fields import block_mean, field_block, pick_frames
from mapi_server.services.dashboards.sources import build_catalog
from mapi_spec.api import ScoreSummary
from mapi_spec.dashboard_presets import compile_preset
from mapi_spec.dashboard_v2 import (
    MAX_SERIES_POINTS_V2,
    AccuracyPoint,
    CatalogMetric,
    ClassAccuracy,
    DashboardSpecV2,
    EffectAccuracy,
    ErrorAccuracy,
    FieldBlock,
    KpiBlock,
    SourceCatalog,
)
from mapi_spec.dashboards import DashboardSource, ReadoutSnapshot
from mapi_spec.endpoints import EndpointSchemaSnapshot
from mapi_spec.views import FieldsView

_p = Path(__file__).parent / "fixtures" / "dashboards_v017" / "loader.py"
_spec = util.spec_from_file_location("dashboards_v017_loader", _p)
assert _spec is not None and _spec.loader is not None
golden = sys.modules.setdefault(_spec.name, util.module_from_spec(_spec))
_spec.loader.exec_module(golden)

CASES = golden.case_names()
MMM_WORDS = re.compile(r"\b(media|channels?|spend|roi|return on|campaign)\b", re.IGNORECASE)
SOURCE = DashboardSource(kind="endpoint", alias="client-model")


# ==================================================================================== helpers


def catalog_of(m: Any) -> SourceCatalog:
    snap = m.input_contract.get("snapshot")
    names = (m.actuals or {}).get("names") or {}
    return build_catalog(
        m.manifest,
        snapshot=EndpointSchemaSnapshot.model_validate(snap) if snap else None,
        binding_dims={v: k for k, v in names.items()},
        metrics=[CatalogMetric(key=x.key, labels=x.labels, last=x.last) for x in m.fit_metrics],
        scorecard=[i.check for i in m.scorecard.items if i.check],
    )


def compiled(m: Any) -> DashboardSpecV2:
    return compile_preset(m.preset, catalog_of(m), title="Client readout", source=SOURCE)


def material(m: Any, role: str = "baseline", **over: Any) -> pn.Material:
    sc = m.scores[role]
    mat = pn.Material(
        kind="baseline",
        mode=sc.mode,
        summary=sc.summary,
        forecast=sc.forecast,
        counterfactual=sc.counterfactual,
        fields=sc.fields,
        metrics=[pn.MetricValue(x.key, x.labels, x.last) for x in m.fit_metrics],
        scorecard=m.scorecard,
        catalog=catalog_of(m),
        band=pn.run_band(sc.output, m.representation),
    )
    for k, v in over.items():
        setattr(mat, k, v)
    return mat


def with_panels(spec: DashboardSpecV2, *panels: dict[str, Any], **over: Any) -> DashboardSpecV2:
    body = spec.model_dump(mode="json")
    body["panels"] = list(panels)
    body.update(over)
    return DashboardSpecV2.model_validate(body)


def kpi(pid: str, variable: str, **over: Any) -> dict[str, Any]:
    return {"kind": "kpi", "id": pid, "title": pid.capitalize(),
            "source": {"kind": "output", "variable": variable}, **over}  # fmt: skip


def blocks_of(snap: ReadoutSnapshot) -> dict[str, Any]:
    return {b.panel_id: b for b in snap.panels}


def ranges(snap: ReadoutSnapshot) -> Iterator[tuple[float | None, float | None]]:
    """Every (low, high) a client sees."""
    for t in snap.tiles:
        yield t.low, t.high
    for s in snap.series:
        for p in s.points:
            yield p.low, p.high
    for b in snap.panels:
        if b.kind == "kpi":
            yield b.tile.low, b.tile.high
        elif b.kind == "bars":
            for r in b.rows:
                yield r.low, r.high
        elif b.kind == "series":
            for s in b.series:
                for p in s.points:
                    yield p.low, p.high


def summed_bounds(summary: ScoreSummary) -> set[float]:
    """Every sum of two or more interval bounds of a table (all rows, and per period)."""
    out: set[float] = set()
    for t in summary.tables:
        table = pn.table_of(summary, t.variable)
        assert table is not None
        if len(table.cells) > 1:
            out.add(math.fsum(c.low or 0.0 for c in table.cells))
            out.add(math.fsum(c.high or 0.0 for c in table.cells))
        if table.time_dim is None:
            continue
        per: dict[str, list[pn.Cell]] = {}
        for c in table.cells:
            per.setdefault(c.coords[table.time_dim], []).append(c)
        for cells in per.values():
            if len(cells) > 1:
                out.add(math.fsum(c.low or 0.0 for c in cells))
                out.add(math.fsum(c.high or 0.0 for c in cells))
    # whole numbers are left out: a sum of count bounds can equal a count by chance
    return {x for x in out if not float(x).is_integer()}


def uploaded(m: Any) -> list[tuple[dict[str, str], float | str]]:
    a = m.actuals
    from mapi_spec.xrjson import XrJsonDataset

    ds = XrJsonDataset.model_validate(a["inputs"])
    column = a.get("kpi_column") or a.get("label_column")
    return pn.actual_cells(ds, column)


# ================================================================================ every preset


@pytest.mark.parametrize("case", CASES)
def test_every_preset_resolves_from_its_fixtures(case: str) -> None:
    m = golden.load_case(case)
    spec = compiled(m)
    for role in [r for r in ("baseline", "predictive") if r in m.scores]:
        snap = pn.build_snapshot_v2(spec, material(m, role))
        assert snap.schema_version == 2 and snap.preset == m.preset
        ids = [p.id for p in spec.panels if p.kind != "mmm"]
        assert [b.panel_id for b in snap.panels] == ids
        # the legacy fields for v0.16-era readers
        assert snap.tiles == [b.tile for b in snap.panels if isinstance(b, KpiBlock)][:6]
        assert all(t.key == "panel" and t.panel_id for t in snap.tiles)
        # the lead: numbers only, plain, no media words outside the planner
        assert snap.lead and len(snap.lead) <= 400 and not MMM_WORDS.search(snap.lead)
        assert ReadoutSnapshot.model_validate(snap.model_dump(mode="json")) == snap


@pytest.mark.parametrize("case", CASES)
def test_no_interval_is_a_sum_of_bounds(case: str) -> None:
    m = golden.load_case(case)
    spec = compiled(m)
    for role, sc in m.scores.items():
        snap = pn.build_snapshot_v2(spec, material(m, role))
        wrong = summed_bounds(sc.summary)
        for lo, hi in ranges(snap):
            for v in (lo, hi):
                assert v is None or not any(math.isclose(v, w, rel_tol=1e-12) for w in wrong), (
                    f"{case}/{role}: {v} is a sum of bounds"
                )


def test_the_forecast_lead_and_legacy_series() -> None:
    m = golden.load_case("forecast_bsts")
    snap = pn.build_snapshot_v2(compiled(m), material(m, "predictive"))
    assert snap.lead.startswith("We expect 1,072.9 prediction next period, likely between")
    (series,) = snap.series
    assert series.name == "y_hat" and series.points[-1].t == "2020-12-01"
    fan = blocks_of(snap)["fan"]
    assert fan.split_at == "2020-01-01" and fan.series[0].points[0].actual is not None


# ==================================================================================== centres


def test_point_models_have_a_centre_and_flag_sums() -> None:
    m = golden.load_case("regression_point")
    spec = with_panels(
        compiled(m),
        kpi("one", "y_hat", source={"kind": "output", "variable": "y_hat",
                                    "select": {"store": "s0", "sku": "k0", "week": "2024-09-22"}},
            reduce={"how": "select"}),
        kpi("total", "y_hat", reduce={"how": "sum", "over": ["store"]}),
    )  # fmt: skip
    got = blocks_of(pn.build_snapshot_v2(spec, material(m)))
    assert got["one"].tile.value == pytest.approx(17.534727926241715)
    total = got["total"]
    assert total.approximate and total.tile.approximate
    assert total.tile.low is None and total.tile.high is None
    # the run aggregated the point predictions itself: still flagged, still no range
    agg = blocks_of(pn.build_snapshot_v2(spec, material(m, "aggregate")))["total"]
    assert agg.tile.value == pytest.approx(64.1918998501623) and agg.approximate


def test_quantile_sums_are_refused_and_draw_totals_come_from_the_run() -> None:
    sarimax = golden.load_case("forecast_sarimax")
    spec = with_panels(compiled(sarimax), kpi("total", "y_hat",
                       reduce={"how": "sum", "over": ["date"]}))  # fmt: skip
    block = blocks_of(pn.build_snapshot_v2(spec, material(sarimax)))["total"]
    assert block.kind == "unavailable" and "add up its draws" in block.reason
    bsts = golden.load_case("forecast_bsts")
    spec = with_panels(compiled(bsts), kpi("total", "y_hat",
                       reduce={"how": "sum", "over": ["date"]}))  # fmt: skip
    total = blocks_of(pn.build_snapshot_v2(spec, material(bsts, "predictive")))["total"].tile
    want = bsts.facts["aggregates"]["predictive.forecast_total"]
    assert total.value == pytest.approx(want["mean"])
    assert (total.low, total.high) == pytest.approx((want["quantiles"]["0.05"],
                                                     want["quantiles"]["0.95"]))  # fmt: skip
    # a score that aggregated the draws: its own band
    agg = blocks_of(pn.build_snapshot_v2(spec, material(bsts, "aggregate")))["total"].tile
    row = bsts.facts["aggregates"]["aggregate.aggregate"]["tables"]["y_hat"]["rows"][0]
    assert (agg.value, agg.low, agg.high) == pytest.approx(tuple(row)) and not agg.approximate


def test_several_series_at_one_period_add_their_centres_only() -> None:
    m = golden.load_case("forecast_sarimax")
    snap = pn.build_snapshot_v2(compiled(m), material(m))
    nxt = blocks_of(snap)["next"]
    assert nxt.approximate and nxt.tile.low is None and nxt.tile.high is None
    assert "cannot be added up" in nxt.tile.caption


# ======================================================================================= bands


def test_the_outcome_band_says_when_it_is_the_range_of_the_average() -> None:
    m = golden.load_case("forecast_bsts")
    expected = blocks_of(pn.build_snapshot_v2(compiled(m), material(m, "baseline")))["next"]
    predictive = blocks_of(pn.build_snapshot_v2(compiled(m), material(m, "predictive")))["next"]
    assert pn.AVERAGE_RANGE in expected.tile.caption
    assert pn.AVERAGE_RANGE not in predictive.tile.caption
    width = predictive.tile.high - predictive.tile.low
    assert width > 3 * (expected.tile.high - expected.tile.low)  # one result vs the average
    # quantile models: the band is already one of single results
    sarimax = golden.load_case("forecast_sarimax")
    assert pn.run_band({"summary": ["median", "eti_90"]}, "quantiles") == "outcome"
    fan = blocks_of(pn.build_snapshot_v2(compiled(sarimax), material(sarimax)))["fan"]
    assert pn.AVERAGE_RANGE not in (fan.caption or "")


# ================================================================================ unavailable


def test_missing_sources_become_unavailable_blocks() -> None:
    m = golden.load_case("forecast_bsts")
    spec = with_panels(
        compiled(m),
        kpi("gone", "nothing_here"),
        {"kind": "kpi", "id": "metric", "title": "Metric", "stat": "value",
         "source": {"kind": "metric", "key": "not_logged"}},
        {"kind": "kpi", "id": "check", "title": "Check", "stat": "value",
         "source": {"kind": "scorecard", "check": "holdout.nothing"}},
        {"kind": "recipe", "id": "pic", "title": "Training", "recipe": "ml.training_curves",
         "plot_id": "p1"},
        {"kind": "note", "id": "about", "title": "About", "text": "Plain words."},
    )  # fmt: skip
    got = blocks_of(pn.build_snapshot_v2(spec, material(m)))
    assert [got[k].kind for k in ("gone", "metric", "check", "pic")] == ["unavailable"] * 4
    assert got["about"].kind == "note" and got["about"].text == "Plain words."
    for status, want in (("queued", "being prepared"), ("failed", "could not be drawn")):
        mat = material(m, assets={"p1": pn.AssetInfo("dsa_" + "0" * 26, status)})
        assert want in blocks_of(pn.build_snapshot_v2(spec, mat))["pic"].reason
    ready = pn.AssetInfo("dsa_" + "1" * 26, "ready", 1600, 900)
    pic = blocks_of(pn.build_snapshot_v2(spec, material(m, assets={"p1": ready})))["pic"]
    assert (pic.kind, pic.asset_id, pic.width, pic.height) == ("picture", ready.asset_id, 1600, 900)


def test_an_oversized_block_becomes_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    m = golden.load_case("forecast_bsts")
    monkeypatch.setattr(pn, "block_budget", lambda block: 300)
    got = blocks_of(pn.build_snapshot_v2(compiled(m), material(m, "predictive")))
    assert got["fan"].kind == "unavailable" and "too much" in got["fan"].reason


# ====================================================================================== fields


def _raster(values: list[int]) -> str:
    return base64.b64encode(array("H", values).tobytes()).decode()


def test_field_maps_are_block_averaged_to_64_cells_a_side() -> None:
    h = w = 130
    frames = [
        {"label": f"q{k}",
         "mean": {"lo": 0.0, "hi": 1.0, "data": _raster([(y * w + x + k) % 65534 for y in range(h)
                                                          for x in range(w)])}}
        for k in range(12)
    ]  # fmt: skip
    view = FieldsView.model_validate(
        {"fields": [{"output": "rate", "y_dim": "row", "x_dim": "col", "height": h, "width": w,
                     "source_height": h, "source_width": w, "y_coords": list(range(h)),
                     "x_coords": list(range(w)), "frames": frames, "origin": "lower"}]}
    )  # fmt: skip
    block = field_block(view.fields[0], panel_id="map", title="Rate")
    assert isinstance(block, FieldBlock) and (block.width, block.height) == (44, 44)
    assert len(block.frames) == 8 and pick_frames(12) == [0, 2, 3, 5, 6, 8, 9, 11]
    codes = base64.b64decode(block.frames[0].codes_b64)
    assert len(codes) == 44 * 44 and min(codes) == 0 and max(codes) <= 255
    # origin lower: the top row of the block is the last raster row
    assert codes[0] > codes[-1]
    cells, bh, bw = block_mean([1.0, None, 3.0, 5.0], 2, 2, side=1)
    assert (cells, bh, bw) == ([3.0], 1, 1)


def test_the_lgcp_map_comes_from_the_runs_fields_view() -> None:
    m = golden.load_case("field_lgcp")
    got = blocks_of(pn.build_snapshot_v2(compiled(m), material(m)))
    field = got["map"]
    assert field.kind == "field" and field.width <= 64 and field.height <= 64
    assert 1 <= len(field.frames) <= 8 and field.vmin <= field.vmax


# ==================================================================================== accuracy


@pytest.mark.parametrize(
    ("case", "against"),
    [("forecast_bsts", "predictive"), ("forecast_sarimax", "predictive"),
     ("regression_conformal", "baseline"), ("regression_point", "baseline")],
)  # fmt: skip
def test_error_accuracy_matches_the_sdk_metrics(case: str, against: str) -> None:
    m = golden.load_case(case)
    mat = material(m, against)
    detail, pairs, _ = pn.accuracy_of(compiled(m), mat, uploaded(m), names=m.actuals["names"])
    want = m.actuals["expected"]
    assert isinstance(detail, ErrorAccuracy) and detail.n == want["n"] == len(pairs)
    for k in ("mae", "rmse", "mape", "smape", "wape", "bias", "r2"):
        assert getattr(detail, k) == pytest.approx(want[k], rel=1e-9), k
    if "band_hit_rate" in want:
        assert detail.band_hit_rate == pytest.approx(want["band_hit_rate"] * 100, rel=1e-9)
    else:
        assert detail.band_hit_rate is None  # point predictions have no band


def test_binary_classifier_accuracy_matches_the_sdk_metrics() -> None:
    m = golden.load_case("classifier_binary")
    spec = compiled(m)
    mat = material(m)
    detail, _, _ = pn.accuracy_of(spec, mat, uploaded(m), names=m.actuals["names"])
    want = m.actuals["expected"]
    assert isinstance(detail, ClassAccuracy) and detail.n == want["n"]
    for k in ("accuracy", "auc", "brier", "log_score", "rate_pred", "rate_obs"):
        assert getattr(detail, k) == pytest.approx(want[k], rel=1e-9), k
    assert detail.confusion is not None and detail.confusion.counts == want["confusion"]["counts"]
    mat.accuracy = detail
    snap = pn.build_snapshot_v2(spec, mat)
    got = blocks_of(snap)
    assert got["calibration"].kind == "calibration" and got["confusion"].kind == "confusion"
    assert snap.lead == "On the 45 cases you sent, the model was right 78% of the time."
    assert snap.accuracy is None and snap.accuracy_detail == detail


def test_multiclass_accuracy_matches_the_sdk_metrics() -> None:
    m = golden.load_case("classifier_multiclass")
    detail, _, _ = pn.accuracy_of(compiled(m), material(m), uploaded(m), names=m.actuals["names"])
    want = m.actuals["expected"]
    assert isinstance(detail, ClassAccuracy) and detail.n == want["n"]
    for k in ("accuracy", "auc", "brier", "log_score"):
        assert getattr(detail, k) == pytest.approx(want[k], rel=1e-9), k


def test_causal_impact_actuals_update_the_effect_with_its_range() -> None:
    m = golden.load_case("causal_impact")
    spec = compiled(m)
    mat = material(m, "forecast_predictive")
    detail, _, _ = pn.accuracy_of(spec, mat, uploaded(m), names=m.actuals["names"])
    want = m.actuals["expected"]
    assert isinstance(detail, EffectAccuracy) and detail.n_periods == want["n_periods"]
    assert detail.total == pytest.approx(want["effect_total"], rel=1e-9)
    assert (detail.low, detail.high) == pytest.approx((want["effect_low"], want["effect_high"]))
    assert detail.relative == pytest.approx(want["effect_relative"] * 100, rel=1e-9)
    mat.accuracy = detail
    lead = pn.build_snapshot_v2(spec, mat).lead
    assert lead.startswith("The change added about 9.9 prediction (likely 0.2 to 23.2)")


def test_scatter_and_residuals_show_after_actuals() -> None:
    m = golden.load_case("regression_conformal")
    spec = compiled(m)
    before = blocks_of(pn.build_snapshot_v2(spec, material(m)))
    assert before["fit"].kind == "unavailable" == before["residuals"].kind
    mat = material(m)
    mat.accuracy, mat.pairs, _ = pn.accuracy_of(spec, mat, uploaded(m), names=m.actuals["names"])
    after = blocks_of(pn.build_snapshot_v2(spec, mat))
    assert after["fit"].kind == "scatter" and len(after["fit"].points) == 72
    p0 = mat.pairs[0]
    r0 = after["residuals"].points[0]
    assert (r0.x, r0.y) == pytest.approx((p0.predicted, p0.actual - p0.predicted))


def test_history_and_the_change_against_the_previous_readout() -> None:
    m = golden.load_case("forecast_bsts")
    spec = compiled(m)
    history = [
        AccuracyPoint(seq=s, created_at=f"2026-0{s}-01T00:00:00Z", n=12,
                      values={"mape": v, "mae": v * 10})
        for s, v in ((2, 4.0), (5, 3.5))
    ]  # fmt: skip
    mat = material(m, "predictive", history=history, previous={"next": 1000.0})
    snap = pn.build_snapshot_v2(spec, mat)
    got = blocks_of(snap)
    assert [p.mean for p in got["accuracy"].series[0].points] == [4.0, 3.5]
    assert got["typical_error"].tile.value == 35.0  # the latest point, no new actuals
    assert got["next"].tile.delta == pytest.approx((1072.883529617231 - 1000.0) / 1000.0)
    assert got["next"].tile.delta_dir == "up"
    assert snap.accuracy_history == history


def test_history_points_of_both_layouts() -> None:
    v1 = ReadoutSnapshot.model_validate(
        {"lead": "x", "accuracy": {"mape": 5.0, "bias": 1.0, "band_hit_rate": 80.0, "n": 3}}
    )
    p = pn.history_point(3, "2026-01-01T00:00:00Z", None, v1)
    assert p is not None and p.n == 3 and p.values["mape"] == 5.0
    assert pn.history_point(4, "2026-01-01T00:00:00Z", None, ReadoutSnapshot(lead="x")) is None


# ======================================================================================== caps


def test_long_series_are_thinned_to_the_layout_2_point_budget() -> None:
    start = date(2000, 1, 1)
    days = [(start + timedelta(days=k)).isoformat() for k in range(3000)]
    summary = ScoreSummary.model_validate(
        {"tables": [{"variable": "y_hat", "columns": ["t", "mean", "hdi_90_lower", "hdi_90_upper"],
                     "dims": ["t"], "time_dim": "t",
                     "rows": [[d, float(k), k - 1.0, k + 1.0] for k, d in enumerate(days)]}]}
    )  # fmt: skip
    m = golden.load_case("forecast_bsts")
    spec = with_panels(
        compiled(m),
        {"kind": "chart", "id": "a", "title": "A", "intent": "line",
         "source": {"kind": "output", "variable": "y_hat"}},
        {"kind": "chart", "id": "b", "title": "B", "intent": "fan",
         "source": {"kind": "output", "variable": "y_hat"}},
    )  # fmt: skip
    snap = pn.build_snapshot_v2(spec, pn.Material(summary=summary, band="outcome"))
    points = sum(len(s.points) for s in snap.series) + sum(
        len(s.points) for b in snap.panels if b.kind == "series" for s in b.series
    )
    assert 0 < points <= MAX_SERIES_POINTS_V2 and snap.schema_version == 2
    assert {b.kind for b in snap.panels} == {"series"}  # thinned to fit, not dropped


# ============================================================================ prediction/batch


def test_scenario_rows_give_a_prediction_per_row() -> None:
    m = golden.load_case("regression_conformal")
    spec = compiled(m)
    summary = m.scores["rows"].summary
    t = pn.table_of(summary, "y_hat")
    assert t is not None
    res = pn.prediction_result(spec, material(m, "rows"), [{"price": 18.0}])
    assert res.band == "outcome" and res.rows[0].inputs == {"price": 18.0}
    first = res.rows[0].outputs["y_hat"]
    assert first.centre is not None and first.low is not None and first.low <= first.centre
    multi = golden.load_case("classifier_multiclass")
    res = pn.prediction_result(compiled(multi), material(multi, "rows_two"), [{}, {}])
    assert len(res.rows) == 2 and res.band == "none"
    top = res.rows[0].outputs["y_hat"]
    assert top.label in ("low", "mid", "high") and top.probability is not None


def test_a_batch_summary_spreads_row_centres_never_intervals() -> None:
    m = golden.load_case("classifier_binary")
    spec = compiled(m)
    got = pn.batch_summary(spec, material(m), n_rows=45)
    assert got.n_rows == 45 and got.n_scored == 45 and len(got.preview) == 20
    stats = got.stats["y_hat"]
    assert stats.min is not None and stats.max is not None and stats.min <= stats.p50 <= stats.max
    assert got.share_positive is not None and 0 <= got.share_positive <= 1
    assert sum(b.count for b in got.histogram) == 45 and got.download.available
