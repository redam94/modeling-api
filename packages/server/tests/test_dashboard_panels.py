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
    assert snap.lead.startswith("We expect 1,072.9 next period, likely between")
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


def _summed_forecast(m: Any) -> Any:
    """A forecast view of the sarimax fixture's ``y_hat`` summed over ``geo`` (one fitted
    period of history, then the forecast dates)."""
    from mapi_spec.views import ForecastView

    table = pn.table_of(m.scores["predictive"].summary, "y_hat")
    assert table is not None and table.time_dim == "date"
    dates = sorted({c.coords["date"] for c in table.cells})
    coords = ["2024-10-27", *dates]
    sums = [sum(c.centre or 0.0 for c in table.cells if c.coords["date"] == d) for d in dates]
    return ForecastView.model_validate({
        "kpi": "y_hat", "dim": "date", "history": 1, "split_at": dates[0],
        "summed_over": ["geo"],
        "fan": {"dim": "date", "coords": coords, "quantiles": [0.05, 0.5, 0.95],
                "values": [[None, *(x - 1 for x in sums)], [None, *sums],
                           [None, *(x + 1 for x in sums)]],
                "observed": [9999.0, *([None] * len(dates))]},
        "total": {"mean": 123456.0, "sd": 1.0, "quantiles": {"0.05": 1.0, "0.95": 2.0}},
    })  # fmt: skip


def test_the_forecast_of_all_series_is_never_used_for_a_selected_one() -> None:
    m = golden.load_case("forecast_sarimax")
    fc = _summed_forecast(m)
    north = {"kind": "output", "variable": "y_hat", "select": {"geo": "north"}}
    spec = with_panels(
        compiled(m),
        kpi("all", "y_hat", reduce={"how": "sum", "over": ["date"]}),
        kpi("north", "y_hat", source=north, reduce={"how": "sum", "over": ["date"]}),
        {"kind": "chart", "id": "line", "title": "North", "intent": "line", "source": north},
        {"kind": "chart", "id": "whole", "title": "All", "intent": "line",
         "source": {"kind": "output", "variable": "y_hat"}, "facet": "geo"},
    )  # fmt: skip
    got = blocks_of(pn.build_snapshot_v2(spec, material(m, "predictive", forecast=fc)))
    assert got["all"].tile.value == pytest.approx(123456.0)  # every series: the run's total
    assert got["north"].kind == "unavailable" or got["north"].tile.value != pytest.approx(123456.0)
    (line,) = got["line"].series
    assert all(p.actual != 9999.0 for p in line.points)  # no all-series history
    assert line.points[0].t != "2024-10-27"


def test_integer_steps_are_ordered_by_value() -> None:
    from mapi_spec.api import ScoreSummaryTable

    steps = list(range(12))
    summary = ScoreSummary(tables=[ScoreSummaryTable(
        variable="y_hat", columns=["step", "mean"], dims=["step"],
        rows=[[k, float(k * 10)] for k in steps],
    )])  # fmt: skip
    m = golden.load_case("regression_point")
    spec = with_panels(
        compiled(m),
        kpi("last", "y_hat", reduce={"how": "last"}),
        {"kind": "chart", "id": "line", "title": "Steps", "intent": "line",
         "source": {"kind": "output", "variable": "y_hat"}},
        subject={**compiled(m).subject.model_dump(mode="json"), "variable": "y_hat"},
    )  # fmt: skip
    mat = pn.Material(kind="baseline", mode="predict", summary=summary)
    table = pn.table_of(summary, "y_hat")
    assert table is not None and table.time_dim == "step"
    got = blocks_of(pn.build_snapshot_v2(spec, mat))
    assert got["last"].tile.value == pytest.approx(110.0)
    (line,) = got["line"].series
    assert [p.t for p in line.points] == [str(k) for k in steps]
    assert pn.period_key("10") > pn.period_key("9")
    assert pn.period_key("2024-01-02") > pn.period_key("2024-01-01")


def test_a_selected_series_chart_draws_only_its_own_actuals() -> None:
    m = golden.load_case("forecast_sarimax")
    spec = compiled(m)
    mat = material(m, "predictive")
    mat.accuracy, mat.pairs, _ = pn.accuracy_of(spec, mat, uploaded(m), names=m.actuals["names"])
    assert {p.coords.get("geo") for p in mat.pairs} >= {"north", "south"}
    north = {"kind": "output", "variable": "y_hat", "select": {"geo": "north"}}
    spec = with_panels(spec, {"kind": "chart", "id": "line", "title": "North",
                              "intent": "line", "source": north})  # fmt: skip
    (line,) = blocks_of(pn.build_snapshot_v2(spec, mat))["line"].series
    own = {p.t: p.actual for p in mat.pairs if p.coords.get("geo") == "north"}
    drawn = {p.t: p.actual for p in line.points if p.actual is not None}
    assert drawn and all(drawn[t] == pytest.approx(own[t]) for t in drawn)


# ======================================================================================= bands


def test_the_planner_centres_come_from_the_expected_run_and_the_band_from_the_predictive() -> None:
    m = golden.load_case("forecast_bsts")
    expected = pn.table_of(m.scores["baseline"].summary, "y_hat")
    predictive = pn.table_of(m.scores["predictive"].summary, "y_hat")
    merged = pn.table_of(
        pn.with_centres(m.scores["predictive"].summary, m.scores["baseline"].summary), "y_hat"
    )
    assert expected is not None and predictive is not None and merged is not None
    by = {c.coords["date"]: c for c in expected.cells}
    pred = {c.coords["date"]: c for c in predictive.cells}
    assert any(by[k].centre != pred[k].centre for k in by)  # the fixture's means differ
    for c in merged.cells:
        k = c.coords["date"]
        assert c.centre == by[k].centre  # the layout 1 number
        assert (c.low, c.high) == (pred[k].low, pred[k].high)  # the predictive band


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


def test_missing_field_cells_get_their_own_code_never_the_minimum() -> None:
    from mapi_server.services.dashboards.fields import quantise
    from mapi_spec.dashboard_v2 import FIELD_MISSING_CODE, FIELD_VALUE_CODES

    codes = quantise([1.0, None, 3.0, float("nan")], 1.0, 3.0)
    assert list(codes) == [0, FIELD_MISSING_CODE, FIELD_VALUE_CODES, FIELD_MISSING_CODE]
    view = FieldsView.model_validate(
        {"fields": [{"output": "rate", "y_dim": "row", "x_dim": "col", "height": 1, "width": 3,
                     "source_height": 1, "source_width": 3, "y_coords": [0],
                     "x_coords": [0, 1, 2], "log": True,
                     "frames": [{"label": "f", "mean": {"lo": 0.0, "hi": 10.0,
                                                        "data": _raster([0, 30000, 65000])}}]}]}
    )  # fmt: skip
    block = field_block(view.fields[0], panel_id="map", title="Rate")
    assert block.missing_code == FIELD_MISSING_CODE
    raw = base64.b64decode(block.frames[0].codes_b64)
    assert raw[0] == FIELD_MISSING_CODE  # zero on a log map: missing, not vmin
    assert max(c for c in raw if c != FIELD_MISSING_CODE) == FIELD_VALUE_CODES


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


def test_a_conditioned_forecast_echo_never_doubles_the_uploaded_actuals() -> None:
    """A conditioned forecast echoes the upload in ``summary.actuals``: the accuracy is the
    same as without the echo (the two are never added up)."""
    from mapi_spec.api import ScoreSummaryTable

    m = golden.load_case("forecast_bsts")
    spec = compiled(m)
    mat = material(m, "predictive")
    plain, pairs, _ = pn.accuracy_of(spec, mat, uploaded(m), names=m.actuals["names"])
    assert isinstance(plain, ErrorAccuracy) and pairs
    table = pn.table_of(mat.summary, "y_hat")
    assert table is not None and table.time_dim is not None
    echo = ScoreSummaryTable(
        variable="y_hat", columns=[table.time_dim, "actual"], dims=[table.time_dim],
        rows=[[p.t, p.actual] for p in pairs],
    )  # fmt: skip
    assert mat.summary is not None
    mat.summary = mat.summary.model_copy(update={"actuals": [echo]})
    echoed, again, _ = pn.accuracy_of(spec, mat, uploaded(m), names=m.actuals["names"])
    assert isinstance(echoed, ErrorAccuracy) and echoed.n == plain.n
    assert [p.actual for p in again] == [p.actual for p in pairs]
    assert echoed.mae == pytest.approx(plain.mae) and echoed.bias == pytest.approx(plain.bias)
    # with nothing uploaded, the echo alone is the actuals
    alone, only, _ = pn.accuracy_of(spec, mat, [])
    assert isinstance(alone, ErrorAccuracy) and alone.mae == pytest.approx(plain.mae)
    assert len(only) == len(pairs)


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


def test_a_binary_classifier_without_a_positive_label_picks_it_as_the_sdk_does() -> None:
    assert pn.binary_positive([0.0, 1.0, 1.0]) == "1"
    assert pn.binary_positive([1.0, 2.0]) == "2"  # the larger of two values
    assert pn.binary_positive(["true", "false"]) == "true"
    with pytest.raises(ValueError, match="positive class"):
        pn.binary_positive(["yes", "no"])
    m = golden.load_case("classifier_binary")
    spec = compiled(m)
    assert spec.subject.positive_label is None or spec.subject.positive_label == "1"
    spec = spec.model_copy(update={"subject": spec.subject.model_copy(
        update={"positive_label": None})})  # fmt: skip
    cells = uploaded(m)
    want, _, _ = pn.accuracy_of(spec, material(m), cells, names=m.actuals["names"])
    assert isinstance(want, ClassAccuracy) and want.auc is not None
    # the same outcomes coded 1/2: 2 is the positive class, the numbers do not change
    two = [(c, (v + 1.0) if isinstance(v, float) else v) for c, v in cells]
    got, _, _ = pn.accuracy_of(spec, material(m), two, names=m.actuals["names"])
    assert isinstance(got, ClassAccuracy)
    assert got.auc == pytest.approx(want.auc) and got.accuracy == pytest.approx(want.accuracy)
    # yes/no without a positive label: refused with a plain note, never a guess
    words = [(c, ("yes" if v == 1.0 else "no")) for c, v in cells]
    detail, _, notes = pn.accuracy_of(spec, material(m), words, names=m.actuals["names"])
    assert detail is None and "positive class" in notes[0]


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
    assert lead.startswith("The change added about 9.9 (likely 0.2 to 23.2)")


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


def _grid_summary() -> ScoreSummary:
    """A batch scored on the grid of 3 stores × 2 skus (the file gave 3 rows: a/k0,
    b/k1, c/k0): the cells the rows never gave hold the drivers' gaps."""
    rows = [
        ["a", "k0", 1.0, 0.5, 1.5], ["a", "k1", 99.0, 98.0, 100.0],
        ["b", "k0", 99.0, 98.0, 100.0], ["b", "k1", 2.0, 1.5, 2.5],
        ["c", "k0", 3.0, 2.5, 3.5], ["c", "k1", 99.0, 98.0, 100.0],
    ]  # fmt: skip
    return ScoreSummary.model_validate(
        {
            "tables": [
                {
                    "variable": "y_hat",
                    "dims": ["store", "sku"],
                    "columns": ["store", "sku", "mean", "pi_90_lower", "pi_90_upper"],
                    "rows": rows,
                }
            ]
        }
    )


def test_a_file_whose_rows_give_another_dim_reads_only_each_rows_own_cell() -> None:
    """B3 (live): rows with different skus are scored on the store × sku grid; the summary
    and the CSV take each row's own cell, never the grid cells it did not give."""
    from mapi_server.services.dashboards.downloads import render_predictions
    from mapi_spec.xrjson import XrJsonDataset

    m = golden.load_case("regression_conformal")
    spec = compiled(m)
    upload = XrJsonDataset.model_validate(
        {
            "coords": {"store": {"dims": ["store"], "data": ["a", "b", "c"]}},
            "data_vars": {
                "sku": {"dims": ["store"], "data": ["k0", "k1", "k0"]},
                "price": {"dims": ["store"], "data": [4.6, 6.9, 5.0]},
            },
        }
    )
    keys = pn.batch_keys(upload, "store", "store", {"sku": "sku"})
    assert keys == [("a", {"store": "a", "sku": "k0"}), ("b", {"store": "b", "sku": "k1"}),
                    ("c", {"store": "c", "sku": "k0"})]  # fmt: skip
    mat = pn.Material(kind="batch", mode="predict", summary=_grid_summary(), band="outcome")
    got = pn.batch_summary(spec, mat, n_rows=3, id_dim="store", keys=keys)
    assert got.n_scored == 3 and got.stats["y_hat"].max == 3.0
    assert got.stats["y_hat"].mean == pytest.approx(2.0)
    text, n = render_predictions(
        mat.summary, ["y_hat"], id_dim="store", columns=["sku", "price"],
        inputs={"a": {"sku": "k0", "price": 4.6}, "b": {"sku": "k1", "price": 6.9},
                "c": {"sku": "k0", "price": 5.0}},
        keys=keys,
    )  # fmt: skip
    lines = text.splitlines()
    assert n == 3 and lines[0] == "store,sku,price,y_hat,y_hat_low,y_hat_high"
    assert lines[1:] == ["a,k0,4.6,1.0,0.5,1.5", "b,k1,6.9,2.0,1.5,2.5", "c,k0,5.0,3.0,2.5,3.5"]
    # no per-row dims: the old grouping by id
    assert pn.batch_keys(upload, "store", "store", {}) is None


def test_a_fit_metric_kpi_shows_its_recorded_interval() -> None:
    """B11 (live): ``effect_total`` comes with ``effect_total_p05``/``_p95`` from the fit;
    the tile shows them (a metric KPI's stat is ``value``, which used to drop the range)."""
    from mapi_spec.dashboard_v2 import KpiPanel, MetricSource

    m = golden.load_case("causal_impact")
    spec = compiled(m)
    mat = material(m)
    mat.metrics = [
        pn.MetricValue("effect_total", {}, 113.0),
        pn.MetricValue("effect_total_p05", {}, 92.2),
        pn.MetricValue("effect_total_p95", {}, 133.2),
    ]
    panel = KpiPanel(id="m_effect_total", title="Total effect", stat="value", band="none",
                     source=MetricSource(key="effect_total"))  # fmt: skip
    tile = pn._kpi(panel, spec, mat).tile
    assert (tile.value, tile.low, tile.high) == (113.0, 92.2, 133.2)
    mat.metrics = mat.metrics[:1]
    tile = pn._kpi(panel, spec, mat).tile
    assert tile.value == 113.0 and tile.low is None and tile.high is None


def test_caption_parts_never_run_two_sentences_together() -> None:
    """B13 (live): "Period 2012-10-01 The range…" lacked its full stop."""
    assert pn._join("Period 2012-10-01", "The range shows the average.") == (
        "Period 2012-10-01. The range shows the average."
    )
    assert pn._join("Done.", None, "Next") == "Done. Next"
    assert pn._join("Only one") == "Only one"


# ============================================================== v0.17 live follow-ups (B3, B13)


def _class_grid() -> ScoreSummary:
    """A classifier scored on the grid of 3 stores × 3 skus for 3 sent rows (a/k0, b/k1,
    c/k2): the other 6 cells are cases nobody sent (their drivers are gaps)."""
    sent = {("a", "k0"): 0.1, ("b", "k1"): 0.5, ("c", "k2"): 0.9}
    rows = [[s, k, sent.get((s, k))] for s in "abc" for k in ("k0", "k1", "k2")]
    table = {"variable": "y_hat", "dims": ["store", "sku"], "columns": ["store", "sku", "mean"]}
    return ScoreSummary.model_validate({"tables": [{**table, "rows": rows}]})


def test_actuals_charts_show_only_the_rows_the_client_sent() -> None:
    """B3 follow-up (live): "Chance of each result" listed every store × sku cell of the
    score grid (most of them a dash); it shows each sent row's own cell only."""
    m = golden.load_case("classifier_binary")
    spec = compiled(m)
    mat = pn.Material(kind="actuals", mode="predict", summary=_class_grid(), band="mean")
    sent = [({"store": s, "sku": k}, v) for s, k, v in (("a", "k0", 1.0), ("b", "k1", 0.0),
                                                         ("c", "k2", 1.0))]  # fmt: skip
    mat.sent = pn.sent_rows(spec, mat, sent)
    assert mat.sent == [{"store": "a", "sku": "k0"}, {"store": "b", "sku": "k1"},
                        {"store": "c", "sku": "k2"}]  # fmt: skip
    bars = blocks_of(pn.build_snapshot_v2(spec, mat))["chances"]
    assert [r.label for r in bars.rows] == ["a · k0", "b · k1", "c · k2"]
    assert all(r.value is not None for r in bars.rows)
    # without an upload, the grid as before
    mat.sent = ()
    assert len(blocks_of(pn.build_snapshot_v2(spec, mat))["chances"].rows) == 9


def test_a_picked_case_is_compared_only_with_the_same_case() -> None:
    """B13 (live): "Chance of a positive ▲11% more than the readout before" compared the first
    cell of two readouts of different cases."""
    m = golden.load_case("classifier_binary")
    spec = compiled(m)
    panel = next(p for p in spec.panels if p.id == "chance")
    panel = panel.model_copy(update={"compare": "previous"})
    mat = material(m)
    first = pn._kpi(panel, spec, mat).tile
    assert first.caption.startswith("For store s0")
    mat.previous = {"chance": 0.3}
    mat.previous_captions = {"chance": first.caption}
    mat.previous_same_inputs = False  # another upload: other cases
    assert pn._kpi(panel, spec, mat).tile.delta is None
    mat.previous_same_inputs = True
    mat.previous_captions = {"chance": "For store s9, sku k2, week 2024-09-22"}
    assert pn._kpi(panel, spec, mat).tile.delta is None
    mat.previous_captions = {"chance": first.caption}
    tile = pn._kpi(panel, spec, mat).tile
    assert tile.delta == pytest.approx((first.value - 0.3) / 0.3)
    assert tile.delta_basis == "the readout before"


def test_fit_and_sent_accuracy_are_named_apart_and_classes_are_words() -> None:
    """B13 (live): "How often the model is right 56.7% - From the model fit" next to "On the
    40 cases you sent, the model was right 55%"; the confusion grid showed 0/1."""
    m = golden.load_case("classifier_binary")
    spec = compiled(m)
    mat = material(m)
    fit = blocks_of(pn.build_snapshot_v2(spec, mat))["m_accuracy"].tile
    assert fit.caption == pn.FIT_CAPTION
    detail, _, _ = pn.accuracy_of(spec, mat, uploaded(m), names=m.actuals["names"])
    assert isinstance(detail, ClassAccuracy) and detail.confusion is not None
    assert detail.confusion.labels == ["No", "Yes"] and detail.positive == "Yes"
    mat.accuracy = detail
    fit = blocks_of(pn.build_snapshot_v2(spec, mat))["m_accuracy"].tile
    assert fit.caption == "When the model was built. On the 45 cases you sent: 78%"
    assert (
        pn.class_word("1", ["0", "1"]) == "Yes"
        and pn.class_word("false", ["true", "false"]) == "No"
    )
    assert pn.class_word("mid", ["low", "mid", "high"]) == "mid"
    assert pn.class_word("churned", ["churned", "stayed"]) == "churned"


def test_default_labels_and_raw_columns_never_reach_the_copy() -> None:
    """B13 (live): "Predicted prediction", "We expect 176.2 prediction next period" and the
    raw ``t, centre, low, high`` headers of a forecast table."""
    from mapi_spec.dashboard_v2 import TablePanel

    reg = compiled(golden.load_case("regression_point"))
    assert reg.subject.label == "Prediction"
    assert next(p for p in reg.panels if p.id == "predicted").title == "Prediction"
    m = golden.load_case("forecast_bsts")
    spec = compiled(m)
    mat = material(m)
    variable = spec.subject.variable
    table = pn.table_of(mat.summary, variable)
    assert table is not None and table.time_dim is not None
    source = {"kind": "output", "variable": variable}
    panel = TablePanel.model_validate(
        {"kind": "table", "id": "tbl", "title": "Numbers", "source": source}
    )
    block = pn._table_block(panel, spec, mat)
    assert "centre" not in block.columns and "Expected" in block.columns
    assert block.columns[0] == (spec.period_label or "period").capitalize()
    assert all(len(r) == len(block.columns) for r in block.rows)
