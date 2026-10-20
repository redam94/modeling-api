"""M3 guided mode: templates are consistent, role detection pre-fills answers, answers
become an ingest mapping plus a ModelSpec draft (and its YAML), ingest presets (MFF,
Meridian)."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.executor import RunKind
from mapi_spec.guided import (
    BUILDER_VALUES,
    GUIDED_TEMPLATES,
    ColumnProfile,
    DetectRolesRequest,
    GuidedAnswers,
    GuidedTemplate,
    build_draft,
    channel_label,
    get_template,
    render_template,
    safe_name,
    suggest_roles,
    template_placeholders,
)
from mapi_spec.ingest import (
    MFF_COLUMNS,
    IngestMapping,
    IngestPreset,
    MeridianColumns,
    MffOptions,
)
from mapi_spec.jobs import IngestJob, parse_job
from mapi_spec.loader import load_document
from mapi_spec.scorecard import SCORECARD_CHECKS
from mapi_spec.spec import ModelSpec
from pydantic import ValidationError

pytestmark = pytest.mark.unit

GEO_COLUMNS = [
    ColumnProfile(name="Week", dtype="date", n_rows=1040, n_distinct=104, detected_freq="W-SUN"),
    ColumnProfile(name="Market", dtype="string", n_rows=1040, n_distinct=10),
    ColumnProfile(name="Revenue (EUR)", dtype="number", n_rows=1040, min=10.0, max=9e4),
    ColumnProfile(name="tv_spend", dtype="number", n_rows=1040, min=0.0, max=5e4),
    ColumnProfile(name="SearchSpend", dtype="number", n_rows=1040, min=0.0, max=2e4),
    ColumnProfile(name="social_cost_eur", dtype="number", n_rows=1040, min=0.0, max=1e4),
    ColumnProfile(name="avg_price", dtype="number", n_rows=1040, min=2.0, max=3.5),
    ColumnProfile(name="notes", dtype="string", n_rows=1040, n_missing=1000, n_distinct=30),
]


# ------------------------------------------------------------------------------- templates


def test_the_eight_built_in_templates() -> None:
    assert list(GUIDED_TEMPLATES) == ["geo_mmm", "mediated_mmm", "multi_kpi_mmm",
                                      "campaign_impact", "geo_lift", "weekly_forecast",
                                      "store_demand", "customer_choice"]  # fmt: skip
    plugins = {t.id: (t.plugin, t.plugin_available) for t in GUIDED_TEMPLATES.values()}
    assert plugins["geo_mmm"] == ("mmm.bayesian@^1", True)
    assert plugins["mediated_mmm"] == ("mmm.nested@^1", True)
    assert plugins["multi_kpi_mmm"] == ("mmm.multivariate@^1", True)
    assert plugins["weekly_forecast"] == ("ts.hsgp_trend@^1", True)
    assert plugins["store_demand"] == ("tabular.regressor@^1", True)
    assert plugins["campaign_impact"] == ("causal.impact@^1", True)
    assert plugins["geo_lift"] == ("causal.synthetic_control@^1", True)
    choice = GUIDED_TEMPLATES["customer_choice"]
    assert (choice.plugin, choice.plugin_available) == ("choice.multinomial_logit@^1", True)
    assert choice.plugin_note is None and choice.version == "1.2.0"


@pytest.mark.parametrize("template", GUIDED_TEMPLATES.values(), ids=lambda t: t.id)
def test_every_template_is_consistent(template: GuidedTemplate) -> None:
    ids = {q.id for q in template.questions}
    sources = ids | set(template.defaults) | set(template.lookups) | BUILDER_VALUES
    assert template_placeholders(template.spec_template) <= sources
    assert set(template.scorecard_checks) <= set(SCORECARD_CHECKS)
    for pointer in template.ui_levels:
        assert pointer.startswith("/")
    # the template round-trips through its own JSON (what GET /v1/guided/templates serves)
    again = GuidedTemplate.model_validate_json(template.model_dump_json())
    assert again == template
    # evaluate_then_refit is the default for promotable models (Q13); a causal model's
    # effect is the result of its fit, so it fits once on every period (v0.13)
    mode = "train_only" if template.family == "causal" else "evaluate_then_refit"
    assert template.spec_template["model"]["fit"]["mode"] == mode  # type: ignore[index,call-overload]


def test_template_validation_catches_unsourced_placeholders() -> None:
    data = GUIDED_TEMPLATES["weekly_forecast"].model_dump(mode="json")
    data["spec_template"]["metadata"]["labels"] = {"x": "{{nobody}}"}
    with pytest.raises(ValidationError, match="nobody"):
        GuidedTemplate.model_validate(data)
    data = GUIDED_TEMPLATES["weekly_forecast"].model_dump(mode="json")
    data["scorecard_checks"] = ["holdout.nope"]
    with pytest.raises(ValidationError):
        GuidedTemplate.model_validate(data)
    with pytest.raises(MapiError) as exc:
        get_template("nope")
    assert exc.value.code is ErrorCode.NOT_FOUND


def test_render_template_replaces_whole_and_partial_placeholders() -> None:
    out = render_template(
        {"a": "{{n}}", "b": "x-{{ s }}-y", "c": ["{{l}}"], "{{s}}": 1}, {"n": 8, "s": "k", "l": [1]}
    )
    assert out == {"a": 8, "b": "x-k-y", "c": [[1]], "k": 1}
    with pytest.raises(KeyError):
        render_template("{{missing}}", {})


# -------------------------------------------------------------------------- role detection


def test_detect_roles_request_needs_one_source() -> None:
    ex = DetectRolesRequest.model_validate(DetectRolesRequest.model_json_schema()["examples"][0])
    assert ex.columns is not None
    with pytest.raises(ValidationError):
        DetectRolesRequest(template_id="geo_mmm")
    with pytest.raises(ValidationError):
        DetectRolesRequest(
            template_id="geo_mmm", dataset="dataset://growth/x@v1", columns=GEO_COLUMNS
        )


def test_suggest_roles_prefills_the_geo_mmm_questions() -> None:
    sug = suggest_roles(GUIDED_TEMPLATES["geo_mmm"], GEO_COLUMNS)
    answers = {q.question_id: q.answer for q in sug.questions}
    assert answers["kpi"] == "Revenue (EUR)"
    assert answers["date"] == "Week"
    assert answers["geo"] == "Market"
    assert answers["spend"] == ["SearchSpend", "social_cost_eur", "tv_spend"]
    assert answers["controls"] == ["avg_price"]
    assert answers["frequency"] == "weekly"
    assert answers["holdout_periods"] == 8
    assert "notes" in sug.unassigned
    kpi = next(q for q in sug.questions if q.question_id == "kpi")
    assert kpi.candidates[0].confidence >= 0.5 and kpi.candidates[0].reason_plain
    assert sug.issues == []


def test_suggest_roles_warns_when_a_required_question_has_no_candidate() -> None:
    sug = suggest_roles(GUIDED_TEMPLATES["geo_mmm"], GEO_COLUMNS[:1])
    pointers = {i.pointer for i in sug.issues}
    assert {"/answers/kpi", "/answers/geo", "/answers/spend"} <= pointers
    assert all(i.severity is Severity.WARNING for i in sug.issues)


def test_daily_dates_suggest_daily() -> None:
    cols = [GEO_COLUMNS[0].model_copy(update={"name": "date", "detected_freq": "D"})]
    sug = suggest_roles(GUIDED_TEMPLATES["weekly_forecast"], cols)
    assert {q.question_id: q.answer for q in sug.questions}["frequency"] == "daily"


def test_names_and_channel_labels() -> None:
    assert safe_name("Revenue (EUR)") == "revenue_eur"
    assert safe_name("2024 sales") == "c_2024_sales"
    assert safe_name("!!!") == "column"
    assert channel_label("TV_Spend_EUR") == "tv"
    assert channel_label("SearchSpend") == "search"
    assert channel_label("social_cost_eur") == "social"
    assert channel_label("spend") == "spend"


# ----------------------------------------------------------------------------------- drafts

GEO_ANSWERS = {
    "kpi": "Revenue (EUR)",
    "date": "Week",
    "geo": "Market",
    "spend": ["tv_spend", "SearchSpend", "social_cost_eur"],
    "controls": ["avg_price"],
}


def _answers(template: str = "geo_mmm", **over: Any) -> GuidedAnswers:
    answers = {**GEO_ANSWERS, **over.pop("answers", {})}
    return GuidedAnswers(
        template_id=template,
        name="eu-geo-mmm",
        dataset="eu_media_weekly",
        answers=answers,
        columns=GEO_COLUMNS,
        **over,
    )


def test_geo_mmm_draft() -> None:
    draft = build_draft(GUIDED_TEMPLATES["geo_mmm"], _answers(), project="growth")
    m = draft.mapping
    assert set(m.dims) == {"week", "market"}
    assert m.dims["week"].column == "Week" and m.dims["week"].spec.freq == "W-SUN"
    assert m.dims["market"].spec.policy.value == "new_levels"
    assert m.vars["revenue_eur"].role.value == "target"
    assert m.vars["avg_price"].role.value == "control"
    assert m.wide["spend"].dim == "channel"
    assert m.wide["spend"].columns == {
        "tv": "tv_spend", "search": "SearchSpend", "social": "social_cost_eur",
    }  # fmt: skip
    spec = ModelSpec.model_validate(draft.spec)
    assert spec.metadata.project == "growth"
    assert str(spec.data.dataset) == "dataset://growth/eu_media_weekly@latest"
    assert spec.data.bind.dims == {"geo": "market", "channel": "channel", "time": "week"}
    assert spec.data.bind.vars == {"y": "revenue_eur", "spend": "spend"}
    assert spec.data.split is not None and spec.data.split.last == 8  # type: ignore[union-attr]
    assert draft.dataset_ref == "dataset://growth/eu_media_weekly@latest"
    # "View as YAML" is the same document
    assert load_document(draft.yaml) == spec
    # the mapping is a valid ingest job
    job = parse_job(RunKind.INGEST, draft.mapping.model_dump(mode="json"))
    assert isinstance(job, IngestJob) and job.mapping == draft.mapping


def test_weekly_forecast_uses_lookups_and_numbers() -> None:
    answers = GuidedAnswers(
        template_id="weekly_forecast",
        name="visits",
        dataset="visits",
        answers={"kpi": "visits", "date": "day", "frequency": "daily", "horizon": 30},
    )
    draft = build_draft(GUIDED_TEMPLATES["weekly_forecast"], answers, project="web")
    settings = draft.spec["model"]["settings"]  # type: ignore[index]
    assert settings == {"max_extension": 30, "season_period": 365.25}  # type: ignore[comparison-overlap]


def test_store_demand_and_choice_drafts() -> None:
    answers = GuidedAnswers(
        template_id="store_demand",
        name="demand",
        dataset="sales",
        answers={"kpi": "units", "date": "week", "store": "store_id", "drivers": ["price"]},
    )
    draft = build_draft(GUIDED_TEMPLATES["store_demand"], answers, project="retail")
    assert draft.mapping.vars["price"].role.value == "covariate"
    assert draft.issues == []
    choice = GuidedAnswers(
        template_id="customer_choice",
        name="choice",
        dataset="panel",
        answers={
            "choice": "brand",
            "customer": "hh_id",
            "occasion": "trip",
            "prices": ["price_a", "price_b"],
        },
    )
    draft = build_draft(GUIDED_TEMPLATES["customer_choice"], choice, project="retail")
    # labels drop the question's keywords: price_a and promo_a both label product "a"
    assert draft.mapping.wide["price"].columns == {"a": "price_a", "b": "price_b"}
    assert draft.mapping.wide["price"].missing == "allow"  # a ragged household panel
    assert draft.mapping.vars["brand"].missing == "allow"
    assert draft.issues == []  # the built-in choice model exists


@pytest.mark.parametrize(
    ("answers", "pointer"),
    [
        ({"kpi": None}, "/answers/kpi"),
        ({"geo": "Revenue (EUR)"}, "/answers/geo"),  # used twice
        ({"spend": "tv_spend"}, "/answers/spend"),  # not a list
        ({"spend": ["tv_spend", "tv_spend"]}, "/answers/spend"),
        ({"frequency": "hourly"}, "/answers/frequency"),
        ({"holdout_periods": 0}, "/answers/holdout_periods"),
        ({"holdout_periods": True}, "/answers/holdout_periods"),
        ({"bogus": 1}, "/answers/bogus"),
    ],
)
def test_bad_answers_are_located(answers: dict[str, Any], pointer: str) -> None:
    with pytest.raises(MapiError) as exc:
        build_draft(GUIDED_TEMPLATES["geo_mmm"], _answers(answers=answers), project="growth")
    assert exc.value.code is ErrorCode.GUIDED_ANSWERS_INVALID
    assert exc.value.pointer == pointer


def test_type_mismatches_are_warnings_and_calibration_is_attached() -> None:
    from mapi_spec.calibration import BusinessPriors, LiftTest

    priors = BusinessPriors.model_validate(BusinessPriors.model_json_schema()["examples"][0])
    lift = LiftTest.model_validate(LiftTest.model_json_schema()["examples"][0])
    draft = build_draft(
        GUIDED_TEMPLATES["geo_mmm"],
        _answers(answers={"controls": ["notes"]}, business_priors=priors, lift_tests=[lift]),
        project="growth",
    )
    assert [i.pointer for i in draft.issues] == ["/answers/controls"]
    spec = ModelSpec.model_validate(draft.spec)
    assert spec.calibration is not None and len(spec.calibration.lift_tests) == 1


# ---------------------------------------------------------------------------------- presets


def test_ingest_mapping_presets_and_compat() -> None:
    for ex in IngestMapping.model_json_schema()["examples"]:
        IngestMapping.model_validate(ex)
    v02 = IngestMapping.model_validate(IngestMapping.model_json_schema()["examples"][0])
    # M3 fields stay off the wire unless used (v0.3 servers reject unknown fields)
    assert set(v02.model_dump(mode="json")) == {"format", "layout", "dims", "vars"}
    mff = IngestMapping(format="mff")
    assert mff.preset is IngestPreset.MFF and mff.effective_mff == MffOptions()
    with pytest.raises(ValidationError):  # only the documented presets
        IngestMapping.model_validate({"format": "csv", "preset": "spreadsheet_magic"})
    assert IngestMapping(format="zarr_zip").effective_mff is None


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"format": "csv"}, "at least one dim"),
        ({"format": "mff", "preset": "none"}, "needs the mff"),
        ({"format": "zarr_zip", "preset": "mff"}, "reads csv"),
        ({"format": "csv", "preset": "meridian"}, "needs 'meridian'"),
        ({"format": "csv", "preset": "mff", "meridian": {"media_channels": ["tv"],
                                                         "media": ["tv"]}}, "only used"),
        ({"format": "csv", "preset": "meridian", "mff": {},
          "meridian": {"media_channels": ["tv"], "media": ["tv"]}}, "'mff' options"),
        ({"format": "csv", "preset": "mff", "model_def": {}}, "Extra inputs"),
        ({"format": "csv", "dims": {"d": {"column": "d", "spec": {"kind": "time"}}},
          "vars": {"spend": {"column": "s"}},
          "wide": {"spend": {"dim": "channel", "columns": {"tv": "tv"}}}}, "twice"),
    ],
)  # fmt: skip
def test_ingest_mapping_rules(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        IngestMapping.model_validate(data)


def test_meridian_contract() -> None:
    cols = MeridianColumns.model_validate(MeridianColumns.model_json_schema()["examples"][0])
    wide = cols.wide_vars()
    assert set(wide) == {"media", "spend"}
    assert wide["spend"].columns == {"tv": "tv_spend", "search": "search_spend"}
    assert wide["spend"].role.value == "spend" and wide["media"].role.value == "media"
    for bad in (
        {"media_channels": ["tv"], "media": ["a", "b"]},
        {"media_channels": ["tv", "tv"], "media": ["a", "b"]},
        {"rf_channels": ["yt"], "reach": ["r"]},
        {"controls": ["x"]},
    ):
        with pytest.raises(ValidationError):
            MeridianColumns.model_validate(bad)


def test_wide_mappings_need_distinct_columns() -> None:
    from mapi_spec.ingest import WideVarMapping

    with pytest.raises(ValidationError):
        WideVarMapping(dim="channel", columns={"tv": "x", "search": "x"})


# ------------------------------------------------------------------------------------ MFF


def test_the_mff_columns() -> None:
    assert MFF_COLUMNS == (
        "Period", "Geography", "Product", "Outlet", "Campaign", "Creative", "VariableValue",
        "VariableName",
    )  # fmt: skip


def test_detected_frequencies_are_explained_in_words() -> None:
    from mapi_spec.guided import freq_plain

    assert freq_plain("W-SUN") == "weekly, on Sundays"
    assert freq_plain("D") == "daily" and freq_plain("MS") == "monthly (on the 1st)"
    assert freq_plain("QS-JAN") == "quarterly" and freq_plain("10min") == "10min"
    geo = get_template("geo_mmm")
    cols = [
        ColumnProfile(name="Week", dtype="date", n_rows=10, detected_freq="W-SUN"),
        ColumnProfile(name="Revenue", dtype="number", n_rows=10),
    ]
    date = next(q for q in suggest_roles(geo, cols).questions if q.question_id == "date")
    assert "weekly, on Sundays" in date.candidates[0].reason_plain
    assert "W-SUN" not in date.candidates[0].reason_plain


def test_the_geo_mmm_fits_long_enough_for_its_scorecard() -> None:
    """Two years of weekly data failed the convergence checks at 500 draws (M3 usability run);
    a Guided user cannot change the fit, so the template samples longer and more carefully."""
    fit = get_template("geo_mmm").spec_template["model"]["fit"]  # type: ignore[index,call-overload]
    assert fit["draws"] >= 1000 and fit["tune"] >= 1000 and fit["target_accept"] >= 0.95  # type: ignore[index]


def test_the_mmm_templates_of_v012() -> None:
    """v0.12: geo_mmm caches adstock and shows ROI; the national templates use the
    extension MMMs with their own derived quantities and charts."""
    geo = get_template("geo_mmm")
    assert geo.version == "2.1.0"
    out = geo.spec_template["outputs"]
    assert out["derived"][-1] == "adstock" and out["plots"][-1] == "mmm.roi"  # type: ignore[index,call-overload]
    mediated, multi = get_template("mediated_mmm"), get_template("multi_kpi_mmm")
    # multi_kpi_mmm has no cross-effects (no question tells which result acts on which)
    for template, extra in ((mediated, ["mediation"]), (multi, [])):
        spec = template.spec_template
        assert spec["model"]["settings"] == {"trend": {"kind": "linear"},  # type: ignore[index,call-overload]
                                             "seasonality": {"yearly_order": 2}}  # fmt: skip
        assert spec["model"]["fit"] == get_template("geo_mmm").spec_template["model"]["fit"]  # type: ignore[index,call-overload]
        assert spec["outputs"]["derived"] == ["contributions", "response_curves",  # type: ignore[index,call-overload]
                                              "marginal_cost", "adstock", *extra]  # fmt: skip
        assert {f"mmm.{x}" for x in extra} <= set(spec["outputs"]["plots"])  # type: ignore[index,call-overload,arg-type]
        assert template.scorecard_checks == geo.scorecard_checks
        assert template.about is not None and template.about.fits["data_shape"] == [
            "several_series"
        ]
    assert mediated.tasks == ["whatif", "forecast", "counterfactual", "lift_tests"]
    assert multi.tasks == ["whatif", "forecast", "counterfactual"]
    q = mediated.question("mediators")
    assert (q.wide_dim, q.missing, q.role.value if q.role else None) == (
        "mediator",
        "allow",
        "covariate",
    )
    k = multi.question("kpis")
    assert (k.wide_dim, k.as_var, k.role.value if k.role else None) == ("outcome", "y", "target")


def test_the_causal_templates_of_v013() -> None:
    """v0.13: campaign_impact (causal.impact) and geo_lift (causal.synthetic_control) fill
    the dates, the control columns and the treated markets from text and column answers."""
    impact = get_template("campaign_impact")
    assert impact.family == "causal" and impact.tasks == []
    assert impact.question("controls").required is False
    assert impact.question("end").required is False
    req = GuidedAnswers(
        template_id="campaign_impact", name="tv", dataset="sales",
        answers={"kpi": "Sales", "date": "Week", "controls": ["Sales DE", "searches"],
                 "start": " 2024-03-04 "},
    )  # fmt: skip
    draft = build_draft(impact, req, project="growth")
    spec = ModelSpec.model_validate(draft.spec)
    assert spec.data.bind.dims == {"time": "week"} and spec.data.bind.vars == {"y": "sales"}
    assert draft.mapping.vars["sales_de"].role.value == "covariate"
    assert spec.model.settings == {
        "controls": ["sales_de", "searches"], "intervention_start": "2024-03-04",
        "intervention_end": "", "seasonality": [{"period": 52.18, "harmonics": 3}],
    }  # fmt: skip
    assert spec.outputs.plots[0].id == "causal.impact"
    daily = build_draft(
        impact,
        GuidedAnswers(template_id="campaign_impact", name="tv", dataset="sales",
                      answers={"kpi": "Sales", "date": "Day", "frequency": "daily",
                               "start": "2024-03-04", "end": "2024-03-31"}),
        project="growth",
    )  # fmt: skip
    settings = daily.spec["model"]["settings"]  # type: ignore[index]
    assert settings["controls"] == [] and settings["intervention_end"] == "2024-03-31"  # type: ignore[index,call-overload]
    assert settings["seasonality"] == [{"period": 7, "harmonics": 3}]  # type: ignore[index,call-overload]

    geo = get_template("geo_lift")
    assert geo.question("geo").dim == "unit" and geo.question("end").required
    answers = {"kpi": "Sales", "date": "Week", "geo": "Market", "treated": "Leeds, Bristol",
               "start": "2024-03-04", "end": "2024-04-14"}  # fmt: skip
    draft = build_draft(
        geo, GuidedAnswers(template_id="geo_lift", name="lift", dataset="sales",
                           answers=answers), project="growth"
    )  # fmt: skip
    spec = ModelSpec.model_validate(draft.spec)
    assert spec.data.bind.dims == {"unit": "market", "time": "week"}
    assert draft.mapping.dims["market"].spec.policy.value == "fixed"
    assert spec.model.settings["treated"] == ["Leeds", "Bristol"]  # as_list: split at commas
    assert spec.model.settings["intervention_end"] == "2024-04-14"
    assert [p.id for p in spec.outputs.plots[:2]] == ["causal.impact", "causal.donor_weights"]
    with pytest.raises(ValidationError, match="only a text question"):
        geo.question("geo").model_validate({**geo.question("geo").model_dump(), "as_list": True})
    assert "as_list" not in geo.question("start").model_dump(mode="json")  # off the wire
    for bad in ({"end": "  "}, {"treated": " , "}, {"start": None}):
        with pytest.raises(MapiError) as exc:
            build_draft(
                geo, GuidedAnswers(template_id="geo_lift", name="lift", dataset="sales",
                                   answers={**answers, **bad}), project="growth"
            )  # fmt: skip
        assert exc.value.pointer == f"/answers/{next(iter(bad))}"
