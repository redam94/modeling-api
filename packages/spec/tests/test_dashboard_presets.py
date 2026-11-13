"""v0.17 ``mapi_spec.dashboard_presets``: the preset of every catalog model (golden table),
compiled drafts that validate for every family, the layout 1 upgrade and the checks of a
spec against the source catalog.

Regenerate the golden table after a deliberate change with ``MAPI_UPDATE_SNAPSHOTS=1
.venv/bin/pytest packages/spec/tests/test_dashboard_presets.py``. It is computed with the
manifests' optional ``meaning``/``classes_dim`` removed (the heuristics alone), so a plugin
that declares a meaning later does not change it; the real manifests may differ only where
a declared meaning says so.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.capabilities import ModelManifest, Representation
from mapi_spec.dashboard_presets import (
    LEADS,
    PRESET_ORDER,
    PRESETS,
    catalog_from_manifest,
    catalog_output,
    check_spec_v2,
    compile_preset,
    detect_preset,
    suggest_presets,
    upgrade_v1,
)
from mapi_spec.dashboard_v2 import (
    PRESET_RULES,
    UPGRADE_NOTE_RANGES,
    CatalogInput,
    CatalogMetric,
    CatalogPicture,
    DashboardSpecV2,
    KpiPanel,
    MmmPanel,
    SourceCatalog,
    parse_dashboard_spec,
)
from mapi_spec.dashboards import (
    ChannelDef,
    DashboardSource,
    DashboardSpec,
    dashboard_spec_hash,
)
from mapi_spec.errors import ErrorCode, Severity

GOLDEN = Path(__file__).parent / "golden" / "preset_table.json"
V1_GOLDEN = Path(__file__).parent / "golden" / "dashboard_v1_hashes.json"
SOURCE = DashboardSource(kind="model_version", model_id="demo")
TV = [ChannelDef(name="tv", label="TV")]
MMM_WORDS = ("media", "channel", "spend", "return on", "roi")


@cache
def _manifests() -> dict[str, ModelManifest]:
    sdk = pytest.importorskip("mapi_sdk")
    return {e.id: e.manifest for e in sdk.default_registry().catalog().models}


def _heuristic(m: ModelManifest) -> ModelManifest:
    """The manifest without the optional v0.17 ``meaning``/``classes_dim`` declarations."""
    outputs = {k: v.model_copy(update={"meaning": None, "classes_dim": None})
               for k, v in m.outputs.items()}  # fmt: skip
    return m.model_copy(update={"outputs": outputs})


def _row(m: ModelManifest) -> dict[str, Any]:
    cat = catalog_from_manifest(m)
    sug = suggest_presets(m, m.capabilities, cat)
    spec = compile_preset(sug[0].preset, cat, title="T", source=SOURCE,
                          channels=TV if sug[0].preset == "mmm_planner" else None)  # fmt: skip
    return {
        "presets": [s.preset for s in sug],
        "blocked": {s.preset: s.blocked_inputs for s in sug if s.blocked_inputs},
        "panels": [p.id for p in spec.panels],
    }


def _table() -> dict[str, Any]:
    return {mid: _row(_heuristic(m)) for mid, m in sorted(_manifests().items())}


def test_golden_preset_table() -> None:
    table = _table()
    if os.environ.get("MAPI_UPDATE_SNAPSHOTS") == "1":
        GOLDEN.write_text(json.dumps({"_comment": __doc__.split("\n\n")[0].strip(),
                                      "models": table}, indent=1) + "\n")  # fmt: skip
    golden = json.loads(GOLDEN.read_text())["models"]
    assert table == golden, "regenerate with MAPI_UPDATE_SNAPSHOTS=1 after a deliberate change"
    assert len(table) >= 50
    for mid, row in table.items():
        # every model gets a family preset, or generic with a reason
        assert row["presets"][-1] == "generic", mid
        if row["presets"][0] == "generic":
            # the same manifest the row was computed from (no declared meaning)
            h = _heuristic(_manifests()[mid])
            sug = suggest_presets(h, h.capabilities, catalog_from_manifest(h))
            assert len(sug[0].reasons) >= 2, mid


def test_golden_families() -> None:
    golden = json.loads(GOLDEN.read_text())["models"]
    first = {mid: row["presets"][0] for mid, row in golden.items()}
    assert first["mmm.bayesian"] == "mmm_planner"
    assert first["ts.sarimax"] == first["ts.bsts"] == "forecast"
    assert (
        first["sklearn.tabular_classifier"]
        == first["sklearn.multiclass_classifier"]
        == "classifier"
    )
    assert first["sklearn.tabular_conformal"] == first["sklearn.tabular_regressor"] == "regression"
    assert first["causal.impact"] == first["causal.did"] == "causal_impact"
    assert first["sci.lgcp_grid"] == first["sci.areal_bym2"] == "field_map"
    assert first["dl.cnn_classifier"] == "image"
    assert first["tabular.image_logistic"] == "classifier"


def test_real_manifests_differ_only_by_declared_meaning() -> None:
    golden = json.loads(GOLDEN.read_text())["models"]
    for mid, m in _manifests().items():
        row = _row(m)
        if row["presets"][0] != golden[mid]["presets"][0]:
            assert any(o.meaning is not None for o in m.outputs.values()), mid


def test_every_suggestion_compiles_to_a_valid_spec_without_errors() -> None:
    for mid, m in _manifests().items():
        cat = catalog_from_manifest(m)
        for s in suggest_presets(m, m.capabilities, cat):
            ch = TV if s.preset == "mmm_planner" else None
            spec = compile_preset(s.preset, cat, title="Demo", source=SOURCE, channels=ch)
            again = parse_dashboard_spec(spec.model_dump(mode="json"))
            assert isinstance(again, DashboardSpecV2) and again == spec
            errors = [i for i in check_spec_v2(spec, cat) if i.severity is Severity.ERROR]
            assert errors == [], (mid, s.preset, errors)
            if s.preset != "mmm_planner":
                text = json.dumps(spec.model_dump(mode="json")).lower()
                assert not any(w in text for w in MMM_WORDS), (mid, s.preset)


def test_presets_are_data() -> None:
    assert set(PRESETS) == set(PRESET_RULES) == set(PRESET_ORDER) == set(LEADS)
    for key, p in PRESETS.items():
        assert p.key == key and p.rules == PRESET_RULES[key]
    for key, leads in LEADS.items():
        for text in leads.values():
            assert len(text) <= 400
            if key != "mmm_planner":
                assert not any(w in text.lower() for w in MMM_WORDS)


def test_mmm_planner_needs_channels() -> None:
    m = _manifests()["mmm.bayesian"]
    with pytest.raises(ValueError, match="channels"):
        compile_preset("mmm_planner", catalog_from_manifest(m), title="T", source=SOURCE)


# ============================================================================ real catalogs


def _impact_catalog() -> SourceCatalog:
    m = _manifests()["causal.impact"]
    cat = catalog_from_manifest(m)
    metrics = [CatalogMetric(key=k, last=1.0) for k in
               ("effect_total", "effect_total_p05", "effect_relative", "p_effect_positive",
                "pre_mape")]  # fmt: skip
    pics = [CatalogPicture(plot_id="plt_1", recipe="causal.event_study", title="Event study")]
    return cat.model_copy(update={"metrics": metrics, "pictures": pics})


def test_catalog_metrics_and_pictures_become_panels() -> None:
    cat = _impact_catalog()
    spec = compile_preset("causal_impact", cat, title="T", source=SOURCE)
    ids = [p.id for p in spec.panels]
    assert ids[:4] == ["m_effect_total", "m_effect_relative", "m_p_effect_positive", "m_pre_mape"]
    assert "effect" in ids and "p_event_study" in ids
    assert [i for i in check_spec_v2(spec, cat) if i.severity is Severity.ERROR] == []
    img = catalog_from_manifest(_manifests()["dl.cnn_classifier"]).model_copy(update={
        "metrics": [CatalogMetric(key="accuracy", last=0.9)],
        "pictures": [CatalogPicture(plot_id="plt_2", recipe="ml.training_curves", title="Curves")],
    })  # fmt: skip
    spec = compile_preset("image", img, title="T", source=SOURCE)
    assert [p.id for p in spec.panels] == ["m_accuracy", "p_training_curves"]


def test_detect_from_catalog_alone() -> None:
    for mid, want in [("ts.sarimax", "forecast"), ("mmm.bayesian", "mmm_planner"),
                      ("causal.did", "causal_impact"), ("sci.lgcp_grid", "field_map")]:  # fmt: skip
        assert detect_preset(catalog_from_manifest(_manifests()[mid])) == want


def test_blocked_inputs_are_explained() -> None:
    m = _manifests()["causal.did"]
    cat = catalog_from_manifest(m).model_copy(update={"row_policy": "fixed"})
    sug = {s.preset: s for s in suggest_presets(m, m.capabilities, cat)}
    assert any("refits" in b for b in sug["causal_impact"].blocked_inputs)
    assert any(b.startswith("batch") for b in sug["generic"].blocked_inputs)
    cnn = _manifests()["dl.cnn_classifier"]
    image = suggest_presets(cnn, cnn.capabilities, catalog_from_manifest(cnn))[0]
    assert image.preset == "image" and image.blocked_inputs == [
        "batch: for signed-in analysts only (not for clients or share links)"
    ]


# ================================================================================== upgrade


def _v1_cases() -> dict[str, DashboardSpec]:
    raw = json.loads(V1_GOLDEN.read_text())["cases"]
    return {c["name"]: DashboardSpec.model_validate(c["spec"]) for c in raw}


def test_upgrade_mmm_is_the_planner_equivalent() -> None:
    cat = catalog_from_manifest(_manifests()["mmm.bayesian"])
    for name, v1 in _v1_cases().items():
        v2, notes = upgrade_v1(v1, cat)
        assert v2.preset == "mmm_planner", name
        assert v2.mmm is not None
        assert v2.mmm.channels == v1.channels and v2.mmm.tiles == v1.tiles
        assert v2.mmm.whatif == v1.whatif
        assert v2.subject.variable == v1.kpi.variable  # read as layout 1 reads it
        assert (v2.subject.label, v2.subject.unit, v2.subject.currency) == (
            v1.kpi.label, v1.kpi.unit, v1.kpi.currency,
        )  # fmt: skip
        assert v2.subject.value_per_unit == v1.kpi.value_per_unit
        assert (
            v2.actuals.model_dump(exclude={"match", "id_column", "label_column", "threshold"})
            == v1.actuals.model_dump()
        )
        assert (v2.title, v2.client_name, v2.summary, v2.note) == (
            v1.title, v1.client_name, v1.summary, v1.note,
        )  # fmt: skip
        assert (
            v2.refresh == v1.refresh
            and v2.limits.exact_runs_per_day == v1.limits.exact_runs_per_day
        )
        assert v2.period_label == v1.period_label and v2.show_technical == v1.show_technical
        parts = [p.part for p in v2.panels if isinstance(p, MmmPanel)]
        assert parts[:3] == ["tiles", "scorecard", "changes"]
        assert ("roi_trend" in parts) == ("trends" in v1.tabs)
        assert ("scenarios" in parts) == ("scenarios" in v1.tabs and v1.whatif.enabled)
        assert UPGRADE_NOTE_RANGES in notes
        assert check_spec_v2(v2, cat) == []
        assert dashboard_spec_hash(v2) != dashboard_spec_hash(v1)
        assert parse_dashboard_spec(v2.model_dump(mode="json")) == v2


def test_upgrade_dummy_channel_forecast() -> None:
    cat = catalog_from_manifest(_manifests()["ts.sarimax"])
    v1 = _v1_cases()["forecast_dummy_channel_mock"]
    v2, notes = upgrade_v1(v1, cat)
    assert v2.preset == "forecast" and v2.mmm is None
    assert v2.subject.variable == "y_hat" and v2.subject.label == "Sales"
    assert v2.subject.unit == "currency" and v2.subject.currency == "EUR"
    assert v2.actuals.enabled == v1.actuals.enabled and v2.actuals.match == "time"
    assert v2.refresh.horizon == 6
    assert any("'price'" in n and "placeholder" in n for n in notes)
    assert any("'sales'" in n and "'y_hat'" in n for n in notes)
    assert [i for i in check_spec_v2(v2, cat) if i.severity is Severity.ERROR] == []
    text = json.dumps(v2.model_dump(mode="json")).lower()
    assert "channel" not in text and "price" not in text


def test_upgrade_to_a_preset_without_actuals_turns_them_off() -> None:
    cat = catalog_from_manifest(_manifests()["dl.cnn_classifier"])
    v1 = _v1_cases()["spec_example"]
    v2, notes = upgrade_v1(v1, cat)
    assert v2.preset == "image" and not v2.actuals.enabled
    assert any("actuals" in n for n in notes)


def test_upgrade_without_a_catalog_keeps_media_dashboards_media() -> None:
    v2, _ = upgrade_v1(_v1_cases()["mmm_mock"], SourceCatalog())
    assert v2.preset == "mmm_planner"
    off = _v1_cases()["forecast_dummy_channel_mock"]
    assert not off.whatif.enabled
    assert upgrade_v1(off, SourceCatalog())[0].preset == "generic"


# =================================================================================== checks


def _spec(preset: str, panels: list[dict[str, Any]], **kw: Any) -> DashboardSpecV2:
    body: dict[str, Any] = {
        "layout_version": 2, "preset": preset, "title": "T", "source": SOURCE.model_dump(),
        "subject": {"variable": "y_hat", "label": "Y", "unit": "number"}, "panels": panels,
    }  # fmt: skip
    body.update(kw)
    return DashboardSpecV2.model_validate(body)


def _cat(rep: Representation, **kw: Any) -> SourceCatalog:
    out = catalog_output("y_hat", ["store", "week"], shape="time", representation=rep,
                         time_dim="week")  # fmt: skip
    base: dict[str, Any] = {"outputs": [out], "score_modes": ["predict", "forecast"],
                            "time_dim": "week"}  # fmt: skip
    base.update(kw)
    return SourceCatalog.model_validate(base)


def _kpi(**kw: Any) -> dict[str, Any]:
    return {"kind": "kpi", "id": "k", "title": "K",
            "source": {"kind": "output", "variable": "y_hat"}, **kw}  # fmt: skip


def _codes(spec: DashboardSpecV2, cat: SourceCatalog) -> list[tuple[str, str, str]]:
    return [(i.severity.value, i.code.value, i.pointer) for i in check_spec_v2(spec, cat)]


def test_sum_of_quantiles_is_refused_and_of_points_flagged() -> None:
    spec = _spec("forecast", [_kpi(reduce={"how": "sum", "over": ["store"]})])
    assert _codes(spec, _cat(Representation.QUANTILES)) == [
        ("error", "BINDING_INVALID", "/panels/0/reduce")
    ]
    issue = check_spec_v2(spec, _cat(Representation.QUANTILES))[0]
    assert issue.suggested_action == "show per-period values"
    for rep in (Representation.INTERVAL, Representation.MOMENTS):
        assert _codes(spec, _cat(rep))[0][2] == "/panels/0/reduce"
    assert _codes(spec, _cat(Representation.POINT)) == [("info", "BINDING_INVALID",
                                                         "/panels/0/reduce")]  # fmt: skip
    # draws: a readout holds only the forecast total (the run never aggregates per panel)
    assert _codes(spec, _cat(Representation.DRAWS)) == [
        ("error", "BINDING_INVALID", "/panels/0/reduce")
    ]


def test_draws_are_only_added_up_over_the_forecast_period() -> None:
    """The one total of draws with a range a readout holds is the forecast view's total
    over the horizon: other sums and every mean of draws are refused, never advertised."""
    total = _spec("forecast", [_kpi(reduce={"how": "sum", "over": ["week"]})])
    assert _codes(total, _cat(Representation.DRAWS)) == []
    no_forecast = _cat(Representation.DRAWS, score_modes=["predict"])
    assert ("error", "BINDING_INVALID", "/panels/0/reduce") in _codes(total, no_forecast)
    mean = _spec("forecast", [_kpi(reduce={"how": "mean", "over": ["week"]})])
    assert _codes(mean, _cat(Representation.DRAWS)) == [
        ("error", "BINDING_INVALID", "/panels/0/reduce")
    ]
    out = _cat(Representation.DRAWS).outputs[0]
    assert "sum" in out.reduces and "mean" not in out.reduces
    flat = catalog_output("y", ["area"], shape="areal", representation=Representation.DRAWS)
    assert "sum" not in flat.reduces and "mean" not in flat.reduces


def test_unknown_sources_and_bad_bindings() -> None:
    cat = _cat(Representation.DRAWS, metrics=[{"key": "rmse"}], scorecard=["fit_ok"])
    spec = _spec("forecast", [
        _kpi(id="a", source={"kind": "output", "variable": "nope"}),
        _kpi(id="b", source={"kind": "metric", "key": "mape"}, stat="value"),
        _kpi(id="c", source={"kind": "metric", "key": "rmse"}, stat="value"),
        _kpi(id="d", source={"kind": "scorecard", "check": "missing"}, stat="value"),
        _kpi(id="e", source={"kind": "output", "variable": "y_hat", "select": {"geo": "x"}}),
        {"kind": "chart", "id": "f", "title": "F", "intent": "bars",
         "source": {"kind": "output", "variable": "y_hat"}},
        {"kind": "chart", "id": "g", "title": "G", "intent": "fan", "facet": "zz",
         "source": {"kind": "output", "variable": "y_hat"}},
        {"kind": "table", "id": "h", "title": "H", "columns": ["week", "centre", "q99"],
         "source": {"kind": "output", "variable": "y_hat"}},
        {"kind": "recipe", "id": "i", "title": "I", "recipe": "ml.training_curves",
         "plot_id": "plt_x"},
    ])  # fmt: skip
    ptrs = [p for _, _, p in _codes(spec, cat)]
    assert ptrs == [
        "/panels/0/source/variable", "/panels/1/source/key", "/panels/3/source/check",
        "/panels/4/source/select/geo", "/panels/5/intent", "/panels/6/facet",
        "/panels/7/columns/2", "/panels/8/plot_id",
    ]  # fmt: skip


def test_mode_and_representation_checks() -> None:
    no_forecast = _cat(Representation.DRAWS, score_modes=["predict"])
    spec = _spec("forecast", [_kpi(reduce={"how": "next"})])
    codes = _codes(spec, no_forecast)
    assert ("error", "CAPABILITY_UNSUPPORTED", "/preset") in codes
    assert ("error", "CAPABILITY_UNSUPPORTED", "/panels/0/reduce") in codes
    total = _spec("forecast", [_kpi(source={"kind": "view", "view": "forecast_total"})])
    assert _codes(total, _cat(Representation.DRAWS)) == []
    assert _codes(total, _cat(Representation.QUANTILES)) == [
        ("error", "BINDING_INVALID", "/panels/0/source/view")
    ]
    point = _spec("forecast", [_kpi(stat="low", band="none")])
    assert _codes(point, _cat(Representation.POINT)) == [("error", "BINDING_INVALID",
                                                          "/panels/0/stat")]  # fmt: skip


def test_band_falls_back_with_a_warning() -> None:
    out = catalog_output("total", ["period"], shape="time", representation=Representation.DRAWS,
                         time_dim="period", predictive_ok=False)  # fmt: skip
    cat = SourceCatalog(outputs=[out], score_modes=["predict", "forecast"])
    spec = _spec("field_map", [_kpi(source={"kind": "output", "variable": "total"})],
                 subject={"variable": "total", "label": "Total", "unit": "number"})  # fmt: skip
    assert _codes(spec, cat) == [("warning", "BINDING_INVALID", "/panels/0/band")]


def _rows(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True, "outputs": ["y_hat"],
        "inputs": [{"name": "price", "label": "Price", "bind": {"var": "price"},
                    "type": "number", "min": 0, "max": 50, "default": 10}],
    }  # fmt: skip
    base.update(kw)
    return base


def test_input_checks() -> None:
    inputs = [CatalogInput(var="price", kind="number", bound_min=0, bound_max=40)]
    cat = _cat(Representation.DRAWS, inputs=inputs, row_dim="store", row_policy="fixed")
    spec = _spec("regression", [_kpi(band="mean")], scenario_rows=_rows(),
                 batch={"enabled": True, "outputs": ["y_hat"], "id_column": "case_id"})  # fmt: skip
    ptrs = [p for _, _, p in _codes(spec, cat)]
    assert ptrs == [
        "/scenario_rows/enabled", "/scenario_rows/inputs/0/min", "/batch/enabled",
        "/batch/id_column",
    ]  # fmt: skip
    profiled = [CatalogInput(var="price", kind="number", profile_min=1, profile_max=9)]
    ok = _cat(Representation.DRAWS, inputs=profiled, row_dim="store", row_policy="new_levels")
    assert _codes(_spec("regression", [_kpi()], scenario_rows=_rows()), ok) == [
        ("warning", "BINDING_INVALID", "/scenario_rows/inputs/0/min")
    ]
    unknown = _rows(inputs=[{**_rows()["inputs"][0], "bind": {"var": "cost"}}])
    assert [p for _, _, p in _codes(_spec("regression", [_kpi()], scenario_rows=unknown), ok)] == [
        "/scenario_rows/inputs/0/bind"
    ]


def test_an_off_step_default_and_an_id_column_that_is_no_dim_are_errors() -> None:
    """v0.17 R3/R4 (live): the builder seeded min 1.49931, step 0.0630061, default 4.64961
    (the client's Calculate stayed disabled, the score answered 422) and a batch id column
    ``id`` that is no dim (the file template answered 409); validate passed both."""
    cat = _cat(Representation.DRAWS, inputs=[
        CatalogInput(var="price", kind="number"), CatalogInput(dim="store", kind="choice"),
        CatalogInput(dim="sku", kind="choice"),
    ], row_dim=None)  # fmt: skip
    off = _rows(inputs=[{**_rows()["inputs"][0], "min": 1.49931, "max": 7.79992,
                         "step": 0.0630061, "default": 4.64961}])  # fmt: skip
    spec = _spec("classifier", [_kpi()], scenario_rows=off,
                 batch={"enabled": True, "outputs": ["y_hat"], "id_column": "id"},
                 actuals={"enabled": True, "match": "row", "id_column": "id",
                          "label_column": "bought"})  # fmt: skip
    found = {p: m for p, m in ((i.pointer, i.message) for i in check_spec_v2(spec, cat))}
    assert "a client could not send it" in found["/scenario_rows/inputs/0/default"]
    assert found["/batch/id_column"] == "'id' is not a dim of the model (one of: store, sku)"
    assert "/actuals/id_column" in found and "/actuals/match" not in found
    on = _rows(inputs=[{**_rows()["inputs"][0], "min": 1.5, "max": 7.8, "step": 0.1,
                        "default": 4.6}])  # fmt: skip
    good = _spec("classifier", [_kpi()], scenario_rows=on,
                 batch={"enabled": True, "outputs": ["y_hat"], "id_column": "store"},
                 actuals={"enabled": True, "match": "row", "id_column": "store",
                          "label_column": "bought"})  # fmt: skip
    ptrs = {i.pointer for i in check_spec_v2(good, cat)}
    assert not ptrs & {"/scenario_rows/inputs/0/default", "/batch/id_column",
                       "/actuals/id_column", "/actuals/match"}  # fmt: skip


def test_pictures_must_be_shareable_plots_of_the_recipe() -> None:
    pics = [CatalogPicture(plot_id="plt_1", recipe="ml.training_curves", title="Curves")]
    cat = _cat(Representation.DRAWS, pictures=pics)
    good = {"kind": "recipe", "id": "p", "title": "P", "recipe": "ml.training_curves",
            "plot_id": "plt_1"}  # fmt: skip
    assert _codes(_spec("regression", [good]), cat) == []
    wrong = {**good, "recipe": "ml.misclassified"}
    assert _codes(_spec("regression", [wrong]), cat) == [("error", "BINDING_INVALID",
                                                          "/panels/0/recipe")]  # fmt: skip


def test_kpi_panels_of_compiled_drafts_have_sources_in_the_catalog() -> None:
    m = _manifests()["ts.bsts"]
    cat = catalog_from_manifest(m)
    spec = compile_preset("forecast", cat, title="T", source=SOURCE)
    kpis = [p for p in spec.panels if isinstance(p, KpiPanel)]
    assert [p.id for p in kpis] == ["next", "horizon_total", "typical_error", "inside_range"]
    assert spec.refresh.horizon == 6 and spec.period_label == "period"
    assert not spec.enabled_inputs()
    assert ErrorCode.BINDING_INVALID not in {i.code for i in check_spec_v2(spec, cat)
                                             if i.severity is Severity.ERROR}  # fmt: skip
