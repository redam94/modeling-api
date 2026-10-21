"""v0.17 contracts: layout 2 dashboard specs (presets, panels, sources, inputs), the union
parser, the snapshot v2 blocks and caps, the additive v0.16 widenings, the endpoint table,
error codes, audit actions and the tool surface."""

from __future__ import annotations

import base64
import copy
import json
from typing import Any, get_args

import pytest
from mapi_spec import api, dashboard_tools, dashboard_v2, dashboards
from mapi_spec.api import CLIENT_OPERATIONS, ROUTE_ACCESS, V017_ENDPOINTS, RouteScope
from mapi_spec.audit import SYSTEM_ACTOR_ID, AuditAction, AuditEvent
from mapi_spec.capabilities import OutputDecl
from mapi_spec.dashboard_v2 import (
    MAX_KPI_PANELS,
    MAX_PANELS,
    MAX_READOUT_BYTES_V2,
    MAX_SERIES_POINTS_V2,
    PRESET_RULES,
    RECIPE_INTENTS,
    UPGRADE_NOTE,
    ActualsInputsV2,
    BatchDef,
    ChartIntent,
    ClassAccuracy,
    DashboardSpecV2,
    FieldBlock,
    HorizonDef,
    KpiPanel,
    PanelBlock,
    PresetKey,
    RecipePanel,
    Reduce,
    ScenarioInputDef,
    ScenarioRowsDef,
    ScenarioRowsRequest,
    SubjectDef,
    TableBlock,
    kpi_of_subject,
    panel_meta,
    parse_dashboard_spec,
)
from mapi_spec.dashboards import (
    GLOSSARY,
    MAX_READOUT_BYTES,
    UPGRADE_NEEDS_REVIEW,
    ClientDashboard,
    DashboardShare,
    DashboardSpec,
    EffectiveInputs,
    GlossaryKey,
    KpiValue,
    Readout,
    ReadoutSnapshot,
    ShareSession,
)
from mapi_spec.errors import ERROR_INFO, ErrorCode
from mapi_spec.tenancy import PROJECT_HEADER, PrincipalKind
from pydantic import BaseModel, TypeAdapter, ValidationError

pytestmark = pytest.mark.unit

V2: dict[str, Any] = DashboardSpecV2.model_json_schema()["examples"][0]
V1: dict[str, Any] = DashboardSpec.model_json_schema()["examples"][0]
_BLOCKS: TypeAdapter[Any] = TypeAdapter(PanelBlock)


def _spec(**patch: Any) -> dict[str, Any]:
    out = copy.deepcopy(V2)
    out.update(patch)
    return out


def _bad(obj: dict[str, Any], match: str | None = None) -> None:
    with pytest.raises(ValidationError, match=match):
        DashboardSpecV2.model_validate(obj)


_NUMBER = {"name": "price", "label": "Price", "bind": {"var": "price"}, "type": "number",
           "min": 10, "max": 30, "step": 0.5, "default": 19.5}  # fmt: skip


# ================================================================================ union parse


def test_union_dispatches_on_layout_version() -> None:
    v1 = {k: v for k, v in V1.items() if k != "layout_version"}
    assert type(parse_dashboard_spec(v1)) is DashboardSpec  # missing → layout 1
    assert type(parse_dashboard_spec(V1)) is DashboardSpec
    v2 = parse_dashboard_spec(V2)
    assert type(v2) is DashboardSpecV2
    assert type(parse_dashboard_spec(v2.model_dump_json())) is DashboardSpecV2
    assert type(parse_dashboard_spec(v2.model_dump_json().encode())) is DashboardSpecV2
    assert parse_dashboard_spec(v2) is v2  # a model passes through
    with pytest.raises(ValidationError):
        parse_dashboard_spec({**V2, "layout_version": 3})
    with pytest.raises(ValidationError):  # a v2 body without its layout is read as v1
        parse_dashboard_spec({k: v for k, v in V2.items() if k != "layout_version"})
    with pytest.raises(ValidationError):  # required: no default
        DashboardSpecV2.model_validate({k: v for k, v in V2.items() if k != "layout_version"})
    # a v1 body cannot claim layout 2
    with pytest.raises(ValidationError):
        parse_dashboard_spec({**V1, "layout_version": 2})


def test_dtos_accept_both_layouts() -> None:
    for model, field in ((dashboards.Dashboard, "draft_spec"),
                         (dashboards.DashboardRevision, "spec")):  # fmt: skip
        base = model.model_json_schema()["examples"][0]
        assert type(getattr(model.model_validate(base), field)) is DashboardSpec
        got = model.model_validate({**base, field: V2})
        assert type(getattr(got, field)) is DashboardSpecV2
        assert model.model_validate_json(got.model_dump_json()) == got
    assert type(dashboards.DashboardCreate(name="x", spec=V2).spec) is DashboardSpecV2  # type: ignore[arg-type]
    assert type(dashboards.DashboardDraftUpdate(spec=V2).spec) is DashboardSpecV2  # type: ignore[arg-type]


def test_v1_spec_is_unchanged_in_what_it_accepts() -> None:
    DashboardSpec.model_validate(V1)
    with pytest.raises(ValidationError, match="layout 2"):
        DashboardSpec.model_validate({**V1, "tiles": ["expected", "panel"]})
    with pytest.raises(ValidationError):
        DashboardSpec.model_validate({**V1, "period_label": "quarter"})
    with pytest.raises(ValidationError):
        DashboardSpec.model_validate({**V1, "preset": "forecast"})
    assert DashboardSpec.model_fields["layout_version"].default == 1


# ================================================================================ spec v2


def test_example_validates_and_round_trips() -> None:
    spec = DashboardSpecV2.model_validate(V2)
    assert DashboardSpecV2.model_validate_json(spec.model_dump_json()) == spec
    assert spec.enabled_inputs() == {"scenario_rows", "actuals"}
    assert spec.limits.batch_runs_per_day == 3 and spec.actuals.threshold == 0.5
    assert spec.refresh.horizon == 6


def test_panel_rules() -> None:
    kpi = {"kind": "kpi", "id": "k", "title": "K", "source": {"kind": "output",
                                                              "variable": "y_hat"}}  # fmt: skip
    _bad(_spec(panels=[]))
    _bad(_spec(panels=[kpi, kpi]), "unique")
    many = [{**kpi, "id": f"k{i}"} for i in range(MAX_KPI_PANELS + 1)]
    _bad(_spec(panels=many), "KPI")
    notes = [{"kind": "note", "id": f"n{i}", "title": "N", "text": "x"}
             for i in range(MAX_PANELS + 1)]  # fmt: skip
    _bad(_spec(panels=notes))
    for bad_id in ("Kpi", "1k", "k-1", "k" * 33, ""):
        _bad(_spec(panels=[{**kpi, "id": bad_id}]))
    _bad(_spec(panels=[{**kpi, "title": "<b>x</b>"}]), "plain")
    _bad(_spec(panels=[{"kind": "note", "id": "n", "title": "N", "text": "a &amp; b"}]))
    _bad(_spec(panels=[{"kind": "note", "id": "n", "title": "N", "text": "x" * 1201}]))
    _bad(_spec(panels=[{"kind": "mmm", "id": "m", "title": "M", "part": "scorecard"}]), "mmm")
    # intents per preset
    field = {"kind": "chart", "id": "f", "title": "F", "intent": "field",
             "source": {"kind": "output", "variable": "rate"}}  # fmt: skip
    _bad(_spec(panels=[field]), "field")
    DashboardSpecV2.model_validate(_spec(preset="field_map", panels=[field], scenario_rows={}))
    DashboardSpecV2.model_validate(_spec(preset="generic", panels=[field]))
    # an accuracy history chart takes an accuracy source and only it
    hist = {"kind": "chart", "id": "h", "title": "H", "intent": "accuracy_history",
            "source": {"kind": "output", "variable": "y_hat"}}  # fmt: skip
    _bad(_spec(panels=[hist]), "accuracy")
    _bad(_spec(panels=[{**field, "intent": "fan", "source": {"kind": "accuracy",
                                                              "metric": "mae"}}]))  # fmt: skip
    # only output sources reduce
    _bad(_spec(panels=[{**kpi, "source": {"kind": "metric", "key": "rmse"},
                        "reduce": {"how": "last"}}]), "reduced")  # fmt: skip
    _bad(_spec(panels=[{**kpi, "source": {"kind": "nope"}}]))
    _bad(_spec(panels=[{**kpi, "stat": "median"}]))
    _bad(_spec(panels=[{**kpi, "band": "wide"}]))


def test_reduce_rules() -> None:
    assert Reduce().how == "none"
    Reduce(how="sum", over=["date"])
    Reduce(how="share_above", threshold=0.5)
    for bad in ({"how": "sum"}, {"how": "mean", "over": []}, {"how": "share_above"},
                {"how": "last", "over": ["date"]}, {"how": "none", "threshold": 1.0},
                {"how": "sum", "over": ["a", "a"]},
                {"how": "share_above", "threshold": float("inf")},
                {"how": "sum", "over": ["a", "b", "c", "d", "e"]}):  # fmt: skip
        with pytest.raises(ValidationError):
            Reduce.model_validate(bad)


def test_recipe_panels_native_or_picture() -> None:
    native = RecipePanel(id="r", title="R", recipe="core.ppc_fan")
    assert not native.is_picture and panel_meta(native).intent == "fan"
    with pytest.raises(ValidationError, match="plot_id"):
        RecipePanel(id="r", title="R", recipe="ml.training_curves")
    pic = RecipePanel(id="r", title="R", recipe="ml.training_curves", plot_id="plt_1")
    assert pic.is_picture and panel_meta(pic).intent is None
    with pytest.raises(ValidationError):
        RecipePanel(id="r", title="R", recipe="NotARecipe")
    assert set(RECIPE_INTENTS.values()) <= set(get_args(ChartIntent))
    assert RECIPE_INTENTS["causal.impact"] == "effect"
    with pytest.raises(TypeError):
        RECIPE_INTENTS["x.y"] = "fan"  # type: ignore[index]  # frozen


def test_preset_rules_and_mmm() -> None:
    assert set(PRESET_RULES) == set(get_args(PresetKey))
    for key, rules in PRESET_RULES.items():
        assert ("mmm" in rules.kinds) is (key == "mmm_planner")
        assert rules.intents <= set(get_args(ChartIntent))
        assert {"kpi", "chart", "note"} <= rules.kinds
    assert "batch" not in PRESET_RULES["causal_impact"].inputs
    assert "whatif" in PRESET_RULES["mmm_planner"].inputs
    mmm = {"channels": [{"name": "tv", "label": "TV"}], "whatif": {"enabled": True}}
    note = [{"kind": "note", "id": "n", "title": "N", "text": "x"}]
    tiles = {"kind": "mmm", "id": "m", "title": "M", "part": "tiles"}
    ok = _spec(preset="mmm_planner", mmm=mmm, scenario_rows={}, panels=[*note, tiles])
    spec = DashboardSpecV2.model_validate(ok)
    assert spec.mmm is not None and spec.mmm.tiles[0] == "expected"
    assert spec.enabled_inputs() == {"whatif", "actuals"}
    _bad({**ok, "mmm": None}, "mmm")
    _bad(_spec(mmm=mmm), "mmm")
    _bad({**ok, "mmm": {**mmm, "channels": [mmm["channels"][0]] * 2}}, "unique")
    _bad({**ok, "mmm": {**mmm, "tiles": ["panel"]}})
    # inputs the preset does not allow
    _bad(_spec(preset="causal_impact", panels=note), "scenario_rows")
    batch = {"enabled": True, "outputs": ["y_hat"], "id_column": "case_id"}
    _bad(_spec(batch=batch), "batch")
    DashboardSpecV2.model_validate(_spec(preset="regression", batch=batch, panels=note))


def test_text_and_size_rules() -> None:
    _bad(_spec(title="Two\nlines"))
    _bad(_spec(summary="<script>"))
    _bad(_spec(note="x" * 2001))
    _bad(_spec(period_label="fortnight"))
    _bad(_spec(refresh={"follow_endpoint": True}, source={"kind": "model_version",
                                                           "model_id": "m"}))  # fmt: skip
    _bad(_spec(refresh={"horizon": 105}))
    _bad(_spec(limits={"batch_runs_per_day": 51}))
    _bad(_spec(unknown=1))
    big = [{"kind": "note", "id": f"n{i}", "title": "N", "text": "x" * 1200}
           for i in range(MAX_PANELS)]  # fmt: skip
    long = {"title": "T" * 120, "summary": "s" * 600, "note": "n" * 2000}
    choice = {"label": "L" * 60, "help": "h" * 200, "type": "choice", "bind": {"var": "c"},
              "choices": [f"{i:03d}{'c' * 60}" for i in range(200)]}  # fmt: skip
    rows = {"enabled": True, "outputs": ["y_hat"],
            "inputs": [{**choice, "name": f"v{i}", "default": choice["choices"][0]}
                       for i in range(16)]}  # fmt: skip
    _bad(_spec(scenario_rows={"fixed": {"x": "y" * 129}}))
    _bad(_spec(panels=big, scenario_rows=rows, **long), "bytes")


def test_subject() -> None:
    s = SubjectDef(variable="y_hat", label="Chance", unit="probability", positive_label="yes")
    assert kpi_of_subject(s).unit == "percent"
    assert (
        kpi_of_subject(SubjectDef(variable="y", label="Y", unit="number", decimals=6)).decimals == 4
    )
    with pytest.raises(ValidationError, match="currency"):
        SubjectDef(variable="y", label="Y", unit="currency")
    with pytest.raises(ValidationError, match="currency"):
        SubjectDef(variable="y", label="Y", unit="count", currency="EUR")
    with pytest.raises(ValidationError):
        SubjectDef(variable="y", label="<i>Y</i>", unit="count")
    with pytest.raises(ValidationError):
        SubjectDef(variable="y", label="Y", unit="count", decimals=7)


# ================================================================================ inputs


@pytest.mark.parametrize(
    "patch",
    [
        {"min": 30, "max": 10},
        {"min": 10, "max": 10},
        {"min": None},
        {"max": float("inf")},
        {"step": 0},
        {"step": 25},
        {"step": 0.001},  # 20 000 steps
        {"default": 31},
        {"default": "x"},
        {"default": True},
        {"choices": ["a"]},
        {"date_min": "2026-01-01"},
        {"type": "integer", "step": 0.5, "default": 20},
        {"type": "integer", "default": 19.5},
        {"type": "choice"},
        {
            "type": "choice",
            "min": None,
            "max": None,
            "step": None,
            "choices": ["a", "b"],
            "default": "c",
        },
        {
            "type": "choice",
            "min": None,
            "max": None,
            "step": None,
            "choices": ["a", "a"],
            "default": "a",
        },
        {
            "type": "choice",
            "min": None,
            "max": None,
            "step": None,
            "choices": ["<b>"],
            "default": "<b>",
        },
        {
            "type": "date",
            "min": None,
            "max": None,
            "step": None,
            "date_min": "2026-02-01",
            "date_max": "2026-01-01",
            "default": "2026-01-15",
        },
        {
            "type": "date",
            "min": None,
            "max": None,
            "step": None,
            "date_min": "2026-01-01",
            "date_max": "2026-02-01",
            "default": "2026-03-01",
        },
        {
            "type": "date",
            "min": None,
            "max": None,
            "step": None,
            "date_min": "2026-01-01",
            "date_max": "2026-02-01",
            "default": "soon",
        },
        {"type": "toggle", "min": None, "max": None, "step": None, "default": 1.0},
        {"type": "toggle"},
        {"bind": {"var": "a", "dim": "b"}},
        {"bind": {}},
        {"label": "x" * 61},
        {"help": "x" * 201},
    ],
)
def test_scenario_input_refusals(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ScenarioInputDef.model_validate({**_NUMBER, **patch})


def test_scenario_inputs_ok_per_type() -> None:
    ScenarioInputDef.model_validate(_NUMBER)
    ScenarioInputDef.model_validate({**_NUMBER, "step": None})
    i = ScenarioInputDef.model_validate({**_NUMBER, "type": "integer", "min": 0, "max": 9000,
                                         "step": None, "default": 3})  # fmt: skip
    assert i.type == "integer"
    ScenarioInputDef.model_validate(
        {"name": "plan", "label": "Plan", "bind": {"var": "plan"}, "type": "choice",
         "choices": ["basic", "plus"], "default": "plus"}
    )  # fmt: skip
    ScenarioInputDef.model_validate(
        {"name": "week", "label": "Week", "bind": {"dim": "week"}, "type": "date",
         "date_min": "2026-01-01", "date_max": "2026-12-31", "default": "2026-06-01"}
    )  # fmt: skip
    t = ScenarioInputDef.model_validate(
        {"name": "promo", "label": "Promo", "bind": {"var": "promo"}, "type": "toggle",
         "default": False}
    )  # fmt: skip
    assert t.default is False


def test_scenario_rows_and_horizon() -> None:
    assert ScenarioRowsDef().enabled is False
    ok = ScenarioRowsDef(enabled=True, outputs=["y_hat"], inputs=[_NUMBER])  # type: ignore[list-item]
    assert ok.max_rows == 5
    for bad in (
        {"enabled": True, "inputs": [_NUMBER]},
        {"enabled": True, "outputs": ["y_hat"]},
        {"outputs": ["a", "a"]},
        {"inputs": [_NUMBER, _NUMBER]},
        {"inputs": [_NUMBER], "fixed": {"price": 1.0}},
        {"fixed": {"x": float("nan")}},
        {"inputs": [{**_NUMBER, "applies": "horizon"}]},
        {"max_rows": 21},
        {"max_rows": 0},
        {"outputs": ["a", "b", "c", "d", "e"]},
        {"inputs": [{**_NUMBER, "name": f"v{i}"} for i in range(17)]},
    ):
        with pytest.raises(ValidationError):
            ScenarioRowsDef.model_validate(bad)
    ScenarioRowsDef(enabled=True, outputs=["y_hat"],
                    horizon=HorizonDef(dim="date", max=12, default=6))  # fmt: skip
    for bad_h in ({"dim": "d", "max": 12, "default": 13}, {"dim": "d", "max": 105, "default": 1},
                  {"dim": "d", "min": 5, "max": 12, "default": 4}):  # fmt: skip
        with pytest.raises(ValidationError):
            HorizonDef.model_validate(bad_h)


def test_actuals_v2_and_batch() -> None:
    a = ActualsInputsV2(enabled=True, match="row", id_column="case_id", label_column="churned")
    assert a.threshold == 0.5 and a.publish == "review"
    for bad in ({"enabled": True, "match": "row"}, {"id_column": "x"},
                {"threshold": 0.01}, {"threshold": 0.99},
                {"enabled": False, "allow_refit_requests": True}):  # fmt: skip
        with pytest.raises(ValidationError):
            ActualsInputsV2.model_validate(bad)
    b = BatchDef(enabled=True, outputs=["y_hat"], id_column="case_id")
    assert b.max_rows == 2000 and b.enforce_ranges
    for bad in ({"outputs": [], "id_column": "c"}, {"outputs": ["y"], "id_column": "c",
                                                    "max_rows": 5001},
                {"outputs": ["y", "y"], "id_column": "c"}, {"outputs": ["y"]}):  # fmt: skip
        with pytest.raises(ValidationError):
            BatchDef.model_validate(bad)


def test_scenario_rows_request() -> None:
    r = ScenarioRowsRequest.model_validate({"rows": [{"price": 18.0, "promo": True}]})
    assert r.rows[0]["promo"] is True
    for bad in ({"rows": []}, {"rows": [{}] * 21}, {"rows": [{"p": float("nan")}]},
                {"rows": [{"p": "x" * 129}]}, {"rows": [{f"v{i}": 1 for i in range(65)}]},
                {"rows": [{"bad key": 1}]}, {"rows": [{"p": 1}], "label": "<b>"},
                {"rows": [{"p": [1]}]}):  # fmt: skip
        with pytest.raises(ValidationError):
            ScenarioRowsRequest.model_validate(bad)


# ================================================================================ snapshots


def _snapshot_example() -> dict[str, Any]:
    return dict(Readout.model_json_schema()["examples"][0]["snapshot"])


def test_v016_snapshots_read_back_as_schema_1() -> None:
    old = _snapshot_example()
    snap = ReadoutSnapshot.model_validate(old)
    assert snap.schema_version == 1 and snap.panels == [] and snap.preset is None
    readout = Readout.model_validate(Readout.model_json_schema()["examples"][0])
    assert readout.snapshot is not None and readout.snapshot.schema_version == 1
    session = ShareSession.model_validate(ShareSession.model_json_schema()["examples"][0])
    assert session.dashboard.layout_version == 1 and session.dashboard.panels_meta == []
    # old KpiValue rows read back with the new defaults
    tile = KpiValue.model_validate(old["tiles"][0])
    assert tile.panel_id is None and tile.approximate is False
    with pytest.raises(ValidationError, match="schema_version 2"):
        ReadoutSnapshot.model_validate(
            {**old, "panels": [{"kind": "note", "panel_id": "n", "title": "N", "text": "x"}]}
        )


def test_caps_by_schema_version() -> None:
    base: dict[str, Any] = {"lead": "We expect 12 sales next month."}
    point = {"t": "2026-10-18", "mean": 1.0, "low": 0.5, "high": 1.5}
    s600 = [{"name": "y", "label": "Y", "points": [point] * 300} for _ in range(2)]
    with pytest.raises(ValidationError, match="points"):
        ReadoutSnapshot.model_validate({**base, "series": s600})  # 600 > 520 on v1
    ReadoutSnapshot.model_validate({**base, "series": s600, "schema_version": 2})

    def fan(i: int, n: int) -> dict[str, Any]:
        return {"kind": "series", "panel_id": f"fan{i}", "title": "Fan", "intent": "fan",
                "series": [{"name": "y", "label": "Y", "points": [point] * n}]}  # fmt: skip

    ReadoutSnapshot.model_validate(
        {**base, "series": s600, "schema_version": 2, "panels": [fan(0, 220), fan(1, 220)]}
    )  # 1040 points: the cap
    with pytest.raises(ValidationError, match="points"):
        ReadoutSnapshot.model_validate(
            {**base, "series": s600, "schema_version": 2,
             "panels": [fan(0, 220), fan(1, 220), fan(2, 1)]}
        )  # fmt: skip
    with pytest.raises(ValidationError, match="panel fan0"):  # one block over 24 KiB
        ReadoutSnapshot.model_validate({**base, "schema_version": 2, "panels": [fan(0, 441)]})
    scatter = {"kind": "scatter", "panel_id": "sc", "title": "S",
               "points": [{"x": 1.0, "y": 2.0}] * 441}  # fmt: skip
    with pytest.raises(ValidationError, match="points"):
        ReadoutSnapshot.model_validate(
            {**base, "series": s600, "schema_version": 2, "panels": [scatter]}
        )
    # bytes: 300 kB passes on v2, not on v1
    big = {"k": "x" * 300_000}
    with pytest.raises(ValidationError, match="bytes"):
        ReadoutSnapshot.model_validate({**base, "technical": big})
    ok = ReadoutSnapshot.model_validate({**base, "technical": big, "schema_version": 2})
    assert MAX_READOUT_BYTES < len(ok.model_dump_json()) <= MAX_READOUT_BYTES_V2
    with pytest.raises(ValidationError, match="bytes"):
        ReadoutSnapshot.model_validate(
            {**base, "technical": {"k": "x" * 400_000}, "schema_version": 2}
        )
    # a block over its own budget
    rows = [[f"{'x' * 60}{i}"[:64]] * 12 for i in range(50)]
    table = {"kind": "table", "panel_id": "t", "title": "T", "columns": ["c"] * 12,
             "rows": rows}  # fmt: skip
    TableBlock.model_validate(table)
    with pytest.raises(ValidationError, match="panel t"):
        ReadoutSnapshot.model_validate({**base, "schema_version": 2, "panels": [table]})
    with pytest.raises(ValidationError):
        ReadoutSnapshot.model_validate({**base, "schema_version": 3})
    assert MAX_SERIES_POINTS_V2 == 1040 and MAX_READOUT_BYTES_V2 == 393_216


def _field(w: int = 4, h: int = 3, n: int | None = None) -> dict[str, Any]:
    codes = base64.b64encode(bytes(i % 256 for i in range(w * h if n is None else n))).decode()
    return {"kind": "field", "panel_id": "f", "title": "F", "width": w, "height": h,
            "frames": [{"label": "Q1", "codes_b64": codes}], "vmin": 0.0, "vmax": 1.0}  # fmt: skip


def test_blocks() -> None:
    examples = [
        {"kind": "kpi", "panel_id": "k", "title": "K",
         "tile": {"key": "panel", "label": "K", "unit": "probability", "value": 0.4,
                  "panel_id": "k"}},
        {"kind": "bars", "panel_id": "b", "title": "B", "rows": [{"label": "a", "value": 1}]},
        {"kind": "calibration", "panel_id": "c", "title": "C",
         "bins": [{"p_mean": 0.1, "observed": 0.12, "n": 40}]},
        {"kind": "confusion", "panel_id": "m", "title": "M", "labels": ["a", "b"],
         "counts": [[3, 1], [0, 4]]},
        _field(),
        {"kind": "table", "panel_id": "t", "title": "T", "columns": ["a", "b"],
         "rows": [["x", 1.5], [None, 2.0]]},
        {"kind": "note", "panel_id": "n", "title": "N", "text": "Plain."},
        {"kind": "picture", "panel_id": "p", "title": "P",
         "asset_id": "dsa_01jaf3r1s2t3v4w5x6y7z8a9b0", "alt": "Training curves",
         "width": 800, "height": 500},
        {"kind": "unavailable", "panel_id": "u", "title": "U",
         "reason": "The picture is being prepared."},
    ]  # fmt: skip
    for ex in examples:
        block = _BLOCKS.validate_python(ex)
        assert _BLOCKS.validate_json(_BLOCKS.dump_json(block)) == block
    snap = ReadoutSnapshot.model_validate(
        {"lead": "x", "schema_version": 2, "preset": "classifier", "panels": examples}
    )
    assert ReadoutSnapshot.model_validate_json(snap.model_dump_json()) == snap
    bad = [
        {**examples[3], "counts": [[3, 1]]},
        {**examples[3], "counts": [[3, -1], [0, 4]]},
        _field(n=11),
        {**_field(), "frames": [{"label": "Q1", "codes_b64": "***"}]},
        {**_field(), "width": 65},
        {**_field(), "vmin": 2.0},
        {**_field(), "frames": [_field()["frames"][0]] * 9},
        {**examples[5], "rows": [["x"]]},
        {**examples[5], "rows": [["<b>bold</b>", 1.0]]},
        {**examples[7], "asset_id": "../etc/passwd"},
        {**examples[2], "bins": [{"p_mean": 1.2, "observed": 0.1, "n": 1}]},
        {**examples[0], "panel_id": "Bad"},
        {**examples[1], "kind": "pie"},
    ]
    for b in bad:
        with pytest.raises(ValidationError):
            _BLOCKS.validate_python(b)
    assert FieldBlock.model_validate(_field(64, 64)).width == 64


def test_accuracy_prediction_batch_shapes() -> None:
    snap = ReadoutSnapshot.model_validate(
        {
            "lead": "On the 40 cases you sent, the model was right 85% of the time.",
            "schema_version": 2,
            "accuracy_detail": {"kind": "classification", "n": 40, "threshold": 0.5,
                                "accuracy": 0.85, "auc": 0.9,
                                "confusion": {"labels": ["no", "yes"],
                                              "counts": [[20, 3], [3, 14]]}},
            "accuracy_history": [{"seq": 3, "created_at": "2026-10-18T09:00:00Z", "n": 40,
                                  "values": {"accuracy": 0.85}}],
            "prediction": {"rows": [{"inputs": {"tenure": 12.0},
                                     "outputs": {"y_hat": {"centre": 0.3, "label": "no",
                                                           "probability": 0.7}}}],
                           "band": "mean"},
            "batch": {"n_rows": 2000, "n_scored": 2000, "outputs": ["y_hat"],
                      "stats": {"y_hat": {"mean": 0.3, "p50": 0.28}}, "share_positive": 0.2,
                      "download": {"available": True, "rows": 2000, "bytes_estimate": 90000}},
            "inputs_echo": [{"tenure": 12.0, "plan": "basic", "promo": None}],
        }
    )  # fmt: skip
    assert isinstance(snap.accuracy_detail, ClassAccuracy)
    assert ReadoutSnapshot.model_validate_json(snap.model_dump_json()) == snap
    with pytest.raises(ValidationError):
        ReadoutSnapshot.model_validate({"lead": "x", "accuracy_detail": {"kind": "vibes"}})
    with pytest.raises(ValidationError):
        ReadoutSnapshot.model_validate(
            {"lead": "x", "batch": {"n_rows": 1, "n_scored": 1, "share_positive": 1.5}}
        )


# ================================================================================ widenings


def test_additive_literals() -> None:
    assert {"prediction", "batch"} <= set(get_args(dashboards.ReadoutKind))
    assert {"scenario_rows", "batch"} <= set(get_args(dashboards.ReadoutTrigger))
    assert all(len(t) <= 16 for t in get_args(dashboards.ReadoutTrigger))  # String(16)
    assert all(len(k) <= 16 for k in get_args(dashboards.ReadoutKind))
    assert set(get_args(dashboards.ShareInput)) == {"whatif", "actuals", "scenario_rows", "batch"}
    assert {"probability", "number", "label", "rate", "index"} <= set(get_args(dashboards.TileUnit))
    assert "panel" in get_args(dashboards.TileKey)
    share = DashboardShare.model_validate(
        {**DashboardShare.model_json_schema()["examples"][0],
         "allowed_inputs": ["whatif", "actuals", "scenario_rows", "batch"]}
    )  # fmt: skip
    assert len(share.allowed_inputs) == 4
    e = EffectiveInputs()
    assert (e.scenario_rows, e.batch, e.accuracy_kind) == (False, False, None)
    acc = dashboards.ActualsAccuracy(n=3, mae=1.0, r2=0.5)
    assert acc.rmse is None
    t = dashboards.ActualsTemplate.model_validate(
        dashboards.ActualsTemplate.model_json_schema()["examples"][0]
    )
    assert t.match == "time" and t.id_column is None


def test_client_dashboard_v2_fields() -> None:
    base = ClientDashboard.model_json_schema()["examples"][0]
    v1 = ClientDashboard.model_validate(base)
    assert v1.layout_version == 1 and v1.upgrade_note is None and v1.sections == []
    v2 = ClientDashboard.model_validate(
        {**base, "layout_version": 2, "preset": "forecast", "period_label": "quarter",
         "subject": V2["subject"], "upgrade_note": UPGRADE_NOTE,
         "panels_meta": [{"id": "next", "kind": "kpi", "title": "Sales next month"}],
         "scenario_form": {"max_rows": 3, "outputs": ["y_hat"], "inputs": [_NUMBER]},
         "batch_form": {"max_rows": 2000, "id_column": "case_id", "columns": ["tenure"]},
         "sections": ["overview", "try", "accuracy", "history"]}
    )  # fmt: skip
    assert ClientDashboard.model_validate_json(v2.model_dump_json()) == v2
    assert UPGRADE_NOTE == "This dashboard has a new layout. The numbers come from the same model."
    spec = DashboardSpecV2.model_validate(V2)
    metas = [panel_meta(p) for p in spec.panels]
    assert [m.id for m in metas] == [p.id for p in spec.panels]
    dash = dashboards.Dashboard.model_validate(
        {**dashboards.Dashboard.model_json_schema()["examples"][0],
         "problem": UPGRADE_NEEDS_REVIEW,
         "upgrade_issues": [{"code": "BINDING_INVALID", "pointer": "/channels/0/name",
                             "message": "channel 'tv' is not in the catalog"}]}
    )  # fmt: skip
    assert dash.problem in get_args(dashboards.DashboardProblem)


def test_glossary_v017_keys() -> None:
    new = {"prediction", "likely_range", "probability", "share_positive", "accuracy_rate",
           "calibration", "typical_error", "effect", "cumulative_effect",
           "without_intervention", "chance_of_effect", "map", "batch", "approximate",
           "expected_count"}  # fmt: skip
    assert new <= set(get_args(GlossaryKey)) and set(GLOSSARY) == set(get_args(GlossaryKey))
    for key in new:
        text = GLOSSARY[key]  # type: ignore[index]
        assert text.endswith(".") and "!" not in text and len(text) <= 600
        assert "media" not in text.lower() and "channel" not in text.lower(), key


def test_output_decl_meaning_is_optional_and_hash_neutral() -> None:
    plain = OutputDecl(dims=["time"])
    assert "meaning" not in plain.model_dump() and "classes_dim" not in plain.model_dump()
    prob = OutputDecl(dims=["row", "class"], meaning="class_probability", classes_dim="class")
    assert prob.model_dump()["meaning"] == "class_probability"
    OutputDecl(dims=[], dims_of="y", meaning="class_probability", classes_dim="class")
    with pytest.raises(ValidationError, match="classes_dim"):
        OutputDecl(dims=["class"], meaning="value", classes_dim="class")
    with pytest.raises(ValidationError, match="not a dim"):
        OutputDecl(dims=["row"], meaning="class_probability", classes_dim="class")
    with pytest.raises(ValidationError):
        OutputDecl(dims=["row"], meaning="odds")


# ================================================================================ HTTP contract


def test_v017_table_conventions_and_access() -> None:
    assert api.ENDPOINTS[-len(V017_ENDPOINTS) :] == V017_ENDPOINTS
    ops = {e.operation_id for e in V017_ENDPOINTS}
    assert len(ops) == len(V017_ENDPOINTS) == 13
    assert set(ROUTE_ACCESS) == {e.operation_id for e in api.ENDPOINTS}
    for e in V017_ENDPOINTS:
        acc = ROUTE_ACCESS[e.operation_id]
        for dto in filter(None, (e.request, e.response)):
            obj = getattr(api, dto)
            assert isinstance(obj, type) and issubclass(obj, BaseModel), dto
        for code in e.errors:
            assert code in ERROR_INFO
        if e.path.startswith("/v1/shared/"):
            assert acc.scope is RouteScope.PUBLIC and dashboards.SHARE_SESSION_HEADER in e.headers
            assert PROJECT_HEADER not in e.headers
        else:
            assert acc.scope is RouteScope.PROJECT and PROJECT_HEADER in e.headers
            assert (acc.name_param == "dashboard_id") is ("{dashboard_id}" in e.path)
        if e.method == "POST" and e.request in ("ScenarioRowsRequest", "BatchSubmit"):
            assert e.success == 202 and e.response == "Readout"
            assert api.IDEMPOTENCY_KEY_HEADER in e.headers
    # clients reach the inputs, the downloads and the pictures; never the analyst routes
    client_v017 = ops & CLIENT_OPERATIONS
    assert client_v017 == {
        "submit_dashboard_predictions", "get_dashboard_batch_template", "submit_dashboard_batch",
        "download_dashboard_predictions", "get_dashboard_asset",
    }  # fmt: skip
    for op in ("list_dashboard_presets", "get_dashboard_sources", "upgrade_dashboard"):
        assert op not in CLIENT_OPERATIONS
    assert "If-Match" in next(e for e in V017_ENDPOINTS if e.operation_id ==
                              "upgrade_dashboard").headers  # fmt: skip
    create = next(e for e in api.ENDPOINTS if e.operation_id == "create_dashboard")
    assert ErrorCode.DASHBOARD_LAYOUT_RETIRED in create.errors
    for name in ("V017_ENDPOINTS", "DashboardSpecV2", "SourceCatalog", "DashboardPresets",
                 "DashboardUpgrade", "ScenarioRowsRequest", "BatchTemplate",
                 "BatchSubmit"):  # fmt: skip
        assert name in api.__all__, name


def test_error_codes_and_audit() -> None:
    want = {
        ErrorCode.DASHBOARD_INPUT_INVALID: 422,
        ErrorCode.DASHBOARD_BATCH_TOO_LARGE: 409,
        ErrorCode.DASHBOARD_DOWNLOAD_GONE: 410,
        ErrorCode.DASHBOARD_LAYOUT_RETIRED: 422,
    }
    for code, status in want.items():
        assert ERROR_INFO[code].status == status
    assert AuditAction.DASHBOARD_UPGRADED == "dashboard.upgraded"
    assert AuditAction.DASHBOARD_BATCH_DOWNLOAD == "dashboard.batch_download"
    row = AuditEvent.model_validate(
        {"id": "aud_01jaf3w1x2y3z4a5b6c7d8e9f0", "ts": "2026-10-21T09:00:00Z",
         "tenant_id": "acme", "project_id": "growth", "actor_kind": PrincipalKind.SERVICE,
         "actor_id": SYSTEM_ACTOR_ID, "action": "dashboard.upgraded", "resource_kind":
         "dashboard", "resource_id": "dsh_01jaf3r1s2t3v4w5x6y7z8a9b0", "outcome": "success",
         "details": {"from_revision": 3, "to_revision": 4, "draft": False,
                     "preset": "mmm_planner", "notes": ["ranges are predictive"]}}
    )  # fmt: skip
    assert row.actor_id == "system"
    assert UPGRADE_NEEDS_REVIEW == "upgrade_needs_review"


def test_new_dtos_have_examples_that_round_trip() -> None:
    for model in (dashboard_v2.DashboardPresets, dashboard_v2.DashboardUpgrade,
                  dashboard_v2.SourceCatalog, dashboard_v2.BatchTemplate,
                  dashboard_v2.BatchSubmit, ScenarioRowsRequest, DashboardSpecV2):  # fmt: skip
        examples = model.model_json_schema()["examples"]
        assert examples, model.__name__
        for ex in examples:
            obj = model.model_validate(ex)
            assert model.model_validate_json(obj.model_dump_json()) == obj
    up = dashboard_v2.DashboardUpgrade.model_validate(
        dashboard_v2.DashboardUpgrade.model_json_schema()["examples"][0]
    )
    assert isinstance(up.dashboard.draft_spec, DashboardSpecV2)
    with pytest.raises(ValidationError):
        dashboard_v2.CatalogInput(kind="number")
    json.dumps(DashboardSpecV2.model_json_schema())  # exportable


# ================================================================================ tools


_V016_ARGS = {
    "get_dashboard": set(),
    "get_latest_readout": set(),
    "list_readouts": {"kind", "before_seq", "limit"},
    "get_readout": {"seq"},
    "compare_readouts": {"a_seq", "b_seq"},
    "get_channel": {"name", "last_n"},
    "explain_metric": {"metric"},
    "evaluate_whatif": {"multipliers"},
}


def test_tools_are_a_superset_of_v016() -> None:
    names = dashboard_tools.DASHBOARD_TOOL_NAMES
    assert names[: len(_V016_ARGS)] == tuple(_V016_ARGS) == dashboard_tools.V016_TOOL_NAMES
    assert set(names) - set(_V016_ARGS) == {
        "list_panels", "get_panel", "get_accuracy", "list_inputs", "get_prediction",
        "get_batch_summary",
    }  # fmt: skip
    exported = {t["name"]: t for t in dashboard_tools.tools_json_schema()}
    for name, args in _V016_ARGS.items():
        params = exported[name]["parameters"]
        assert isinstance(params, dict)
        assert set(params.get("properties", {})) == args, name  # no argument added or removed
    for t in dashboard_tools.DASHBOARD_TOOLS:
        assert t.read_only is True and 10 <= len(t.summary_plain) <= 300
    # compare_readouts keeps its fields and gains panel_deltas
    cmp = dashboard_tools.ReadoutComparison.model_json_schema()["properties"]
    assert {"a", "b", "same_model", "tiles", "channels", "lead", "panel_deltas"} == set(cmp)
    cmp_ex = dashboard_tools.ReadoutComparison.model_json_schema()["examples"][0]
    assert dashboard_tools.ReadoutComparison.model_validate(cmp_ex).panel_deltas == []
    with pytest.raises(ValidationError):
        dashboard_tools.PanelArgs.model_validate({"panel_id": "Bad"})
    assert dashboard_tools.OptionalReadoutRef().seq is None
    assert dashboard_tools.tool("get_batch_summary").result is dashboard_tools.BatchReport
    kpi = KpiPanel.model_validate(V2["panels"][0])
    assert kpi.compare == "previous"
