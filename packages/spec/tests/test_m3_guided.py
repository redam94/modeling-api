"""M3 guided mode: templates are consistent, role detection pre-fills answers, answers
become an ingest mapping plus a ModelSpec draft (and its YAML), ingest presets (MFF,
Meridian, BayesInsight) and the BayesInsight import."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.executor import RunKind
from mapi_spec.guided import (
    BUILDER_VALUES,
    GUIDED_TEMPLATES,
    BayesInsightImport,
    ColumnProfile,
    DetectRolesRequest,
    GuidedAnswers,
    GuidedTemplate,
    build_draft,
    channel_label,
    draft_from_bayesinsight,
    get_template,
    render_template,
    safe_name,
    suggest_roles,
    template_placeholders,
)
from mapi_spec.ingest import (
    MFF_COLUMNS,
    BayesInsightMetadata,
    BayesInsightModelDef,
    IngestMapping,
    IngestPreset,
    MeridianColumns,
    MffOptions,
    bayesinsight_var_ref,
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


def test_the_four_built_in_templates() -> None:
    assert set(GUIDED_TEMPLATES) == {"geo_mmm", "weekly_forecast", "store_demand",
                                     "customer_choice"}  # fmt: skip
    plugins = {t.id: (t.plugin, t.plugin_available) for t in GUIDED_TEMPLATES.values()}
    assert plugins["geo_mmm"] == ("mmm.hierarchical_geo@^1", True)
    assert plugins["weekly_forecast"] == ("ts.hsgp_trend@^1", True)
    assert plugins["store_demand"] == ("tabular.regressor@^1", True)
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
    # evaluate_then_refit is the default for promotable models (Q13)
    assert template.spec_template["model"]["fit"]["mode"] == "evaluate_then_refit"


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
    bi = IngestMapping(
        format="csv",
        preset="bayesinsight",
        bayesinsight=BayesInsightMetadata(periodicity="Daily"),
    )
    assert bi.effective_mff is not None and bi.effective_mff.freq == "D"
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
        ({"format": "csv", "preset": "mff", "bayesinsight": {}}, "'bayesinsight' metadata"),
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


# ----------------------------------------------------------------------------- BayesInsight

BI_METADATA = {
    "metadata": {
        "allowed_geos": ["CA", "UK", "US"],
        "allowed_products": ["Laptops", "Phones"],
        "allowed_outlets": ["Total"],
        "allowed_campaigns": ["Total"],
        "allowed_creatives": ["Total"],
        "periodicity": "Weekly",
        "row_ids": ["Product", "Geography", "Period"],
    }
}
BI_MODEL_DEF = {
    "variable_details": [
        {"variable_name": "annual_seasonality", "variable_type": "season",
         "coeff_prior": {"coeff_dist": "Normal", "coeff_params": {"mu": 0.0, "sigma": 0.1}}},
        {"variable_name": "Promotion_Total_Total_Total", "variable_type": "control",
         "normalization": "Global Standardize", "random_coeff_dims": ["Geography", "Product"]},
        {"variable_name": "media_var_0_Total_Total_Total", "variable_type": "media",
         "adstock": "delayed", "media_transform": "hill", "sign": "positive",
         "media_transform_prior": {"type": "Hill", "K_ave": 0.85}},
        {"variable_name": "Stock_Total_Total_Total", "variable_type": "exog",
         "likelihood": {"type": "Poisson"}},
    ],
    "artifact": None,
    "fitted": False,
    "VOF": None,
}  # fmt: skip


def test_the_mff_contract_matches_bayesinsight() -> None:
    assert MFF_COLUMNS == (
        "Period", "Geography", "Product", "Outlet", "Campaign", "Creative", "VariableValue",
        "VariableName",
    )  # fmt: skip
    meta = BayesInsightMetadata.model_validate(BI_METADATA)
    assert meta.row_ids == ("Geography", "Product", "Period")  # canonical order
    assert meta.suffix_columns == ("Outlet", "Campaign", "Creative")
    with pytest.raises(ValidationError):
        BayesInsightMetadata(row_ids=("Store",))


def test_analytic_names_map_to_mff_variables() -> None:
    meta = BayesInsightMetadata.model_validate(BI_METADATA)
    ref = bayesinsight_var_ref("media_var_0_Total_Total_Total", meta)
    assert (ref.variable, ref.sel) == ("media_var_0", {})
    multi = meta.model_copy(update={"allowed_outlets": ["TV", "Radio"]})
    ref = bayesinsight_var_ref("Spend_TV_Total_Total", multi)
    assert (ref.variable, ref.sel) == ("Spend", {"outlet": "TV"})
    with pytest.raises(ValueError):
        bayesinsight_var_ref("Spend", meta)
    flat = BayesInsightMetadata(row_ids=tuple(["Geography", "Product", "Outlet", "Campaign",
                                               "Creative", "Period"]))  # fmt: skip
    assert bayesinsight_var_ref("Sales", flat).variable == "Sales"


def test_bayesinsight_import_draft() -> None:
    req = BayesInsightImport(
        name="bi-import",
        dataset="bi_mff",
        model_def=BayesInsightModelDef.model_validate(BI_MODEL_DEF),
        metadata=BayesInsightMetadata.model_validate(BI_METADATA),
    )
    draft = draft_from_bayesinsight(req, project="growth")
    assert draft.mapping.preset is IngestPreset.BAYESINSIGHT
    assert draft.mapping.effective_mff is not None
    assert draft.mapping.effective_mff.roles == {
        "Promotion": "control", "media_var_0": "spend", "Stock": "target",
    }  # fmt: skip
    spec = ModelSpec.model_validate(draft.spec)
    assert str(spec.model.use) == "mmm.bayesinsight@^1"
    settings = spec.model.settings
    assert settings["kpi"] == "Stock"
    assert settings["variables"]["media_var_0_Total_Total_Total"] == {  # type: ignore[index]
        "var": "media_var_0", "sel": {},
    }  # fmt: skip
    kept = settings["model_def"]["variable_details"][2]  # type: ignore[index]
    assert kept["media_transform_prior"] == {"type": "Hill", "K_ave": 0.85}  # extras kept
    assert "VOF" not in settings["model_def"]  # type: ignore[operator]
    example = BayesInsightImport.model_validate(
        BayesInsightImport.model_json_schema()["examples"][0]
    )
    assert draft_from_bayesinsight(example, project="p").spec["model"]["settings"]["kpi"] == "Sales"  # type: ignore[index]


def test_bayesinsight_import_needs_one_kpi_and_matching_names() -> None:
    no_kpi = {"variable_details": BI_MODEL_DEF["variable_details"][:3]}  # type: ignore[index]
    req = BayesInsightImport(
        name="x", dataset="x", model_def=BayesInsightModelDef.model_validate(no_kpi),
        metadata=BayesInsightMetadata.model_validate(BI_METADATA),
    )  # fmt: skip
    with pytest.raises(MapiError) as exc:
        draft_from_bayesinsight(req, project="p")
    assert exc.value.code is ErrorCode.IMPORT_UNSUPPORTED
    bad = {"variable_details": [{"variable_name": "Sales", "variable_type": "exog"}]}
    req = req.model_copy(update={"model_def": BayesInsightModelDef.model_validate(bad)})
    with pytest.raises(MapiError) as exc:
        draft_from_bayesinsight(req, project="p")
    assert exc.value.pointer == "/model_def/variable_details/0/variable_name"
    trend = {
        "variable_details": [
            *BI_MODEL_DEF["variable_details"],  # type: ignore[misc]
            {"variable_name": "trend", "variable_type": "localtrend"},
        ]
    }
    req = req.model_copy(update={"model_def": BayesInsightModelDef.model_validate(trend)})
    assert [i.code for i in draft_from_bayesinsight(req, project="p").issues] == [
        ErrorCode.IMPORT_UNSUPPORTED
    ]
