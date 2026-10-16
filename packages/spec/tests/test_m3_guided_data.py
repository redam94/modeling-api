"""M3 guided data path (pure parts): role detection on realistic column profiles, profiles
of stored datasets, the schema a draft's mapping implies and MFF drafts."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec.coords import CoordSummary, DataSummary
from mapi_spec.errors import ErrorCode, Severity
from mapi_spec.guided import (
    GUIDED_TEMPLATES,
    ColumnProfile,
    GuidedAnswers,
    build_draft,
    channel_label,
    draft_schema,
    guided_lock_issues,
    profiles_from_dataset,
    suggest_roles,
)
from mapi_spec.ingest import (
    IngestMapping,
    IngestPreset,
    MffOptions,
)
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ModelSpec
from mapi_spec.views import ProfileView
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _col(name: str, dtype: str, **kw: Any) -> ColumnProfile:
    return ColumnProfile.model_validate({"name": name, "dtype": dtype, "n_rows": 1040, **kw})


def _answers(template: str, columns: list[ColumnProfile]) -> dict[str, Any]:
    sug = suggest_roles(GUIDED_TEMPLATES[template], columns)
    return {q.question_id: q.answer for q in sug.questions}


# ------------------------------------------------------------------------- role detection

MMM_COLUMNS = [
    _col("Week", "date", n_distinct=104, detected_freq="W-SUN", monotonic=False),
    _col("Region", "string", n_distinct=10, sample=["north", "south"]),
    _col("Revenue", "number", n_distinct=1040, min=1200.0, max=9.8e4),
    _col("TV_Spend", "number", n_distinct=900, min=0.0, max=5e4),
    _col("SearchCost", "number", n_distinct=900, min=0.0, max=2e4),
    _col("social_budget_eur", "number", n_distinct=900, min=0.0, max=1e4),
    _col("Avg Price", "number", n_distinct=300, min=2.1, max=3.9),
    _col("promo_flag", "integer", n_distinct=2, min=0.0, max=1.0),
    _col("Temperature", "number", n_distinct=400, min=-8.0, max=31.0),
    _col("country", "string", n_distinct=1),
    _col("Notes", "string", n_missing=1000, n_distinct=12),
]


def test_detection_on_a_geo_marketing_table() -> None:
    sug = suggest_roles(GUIDED_TEMPLATES["geo_mmm"], MMM_COLUMNS)
    answers = {q.question_id: q.answer for q in sug.questions}
    assert answers["kpi"] == "Revenue"
    assert answers["date"] == "Week"
    assert answers["geo"] == "Region"  # 'country' never varies: no geo
    assert answers["spend"] == ["SearchCost", "TV_Spend", "social_budget_eur"]
    assert answers["controls"] == ["Avg Price", "Temperature", "promo_flag"]
    assert answers["frequency"] == "weekly"
    assert set(sug.unassigned) == {"country", "Notes"}
    spend = next(q for q in sug.questions if q.question_id == "spend")
    assert "never negative" in spend.candidates[0].reason_plain
    assert not sug.issues


def test_negative_and_constant_columns_lose_points() -> None:
    cols = [
        _col("tv_spend", "number", min=-5.0, max=10.0),
        _col("radio_spend", "number", min=0.0, max=10.0),
        _col("print_spend", "number", min=0.0, max=0.0, n_distinct=1),
    ]
    spend = suggest_roles(GUIDED_TEMPLATES["geo_mmm"], cols).questions[3]
    conf = {c.column: c.confidence for c in spend.candidates}
    assert conf["radio_spend"] > conf["tv_spend"] and conf["radio_spend"] > conf["print_spend"]
    reasons = {c.column: c.reason_plain for c in spend.candidates}
    assert "has negative values" in reasons["tv_spend"]
    assert "always the same value" in reasons["print_spend"]


def test_detection_on_a_daily_series() -> None:
    cols = [
        _col("ds", "date", n_distinct=1040, detected_freq="D", monotonic=True),
        _col("sales", "number", min=0.0, max=900.0),
        _col("weekday", "string", n_distinct=7),
    ]
    sug = suggest_roles(GUIDED_TEMPLATES["weekly_forecast"], cols)
    answers = {q.question_id: q.answer for q in sug.questions}
    assert (answers["kpi"], answers["date"], answers["frequency"]) == ("sales", "ds", "daily")
    date = next(q for q in sug.questions if q.question_id == "date")
    assert "values in order" in date.candidates[0].reason_plain


def test_detection_on_a_store_panel() -> None:
    cols = [
        _col("week_start", "date", n_distinct=104, detected_freq="W-MON"),
        _col("store_id", "string", n_distinct=40),
        _col("units_sold", "integer", min=0.0, max=400.0),
        _col("shelf_price", "number", min=0.5, max=4.0),
        _col("promo", "integer", n_distinct=2, min=0.0, max=1.0),
        _col("display", "integer", n_distinct=2, min=0.0, max=1.0),
    ]
    answers = _answers("store_demand", cols)
    assert answers["kpi"] == "units_sold" and answers["store"] == "store_id"
    assert answers["date"] == "week_start"
    assert answers["drivers"] == ["display", "promo", "shelf_price"]


CHOICE_COLUMNS = [
    _col("household_id", "string", n_distinct=200),
    _col("trip_no", "integer", n_distinct=8, min=1.0, max=8.0),
    _col("brand", "string", n_distinct=3, sample=["cola", "fizz", "store"]),
    _col("price_cola", "number", min=0.8, max=1.4),
    _col("price_fizz", "number", min=0.9, max=1.6),
    _col("price_store", "number", min=0.5, max=1.1, n_missing=90),
    _col("promo_cola", "integer", n_distinct=2, min=0.0, max=1.0),
    _col("promo_fizz", "integer", n_distinct=2, min=0.0, max=1.0),
    _col("promo_store", "integer", n_distinct=2, min=0.0, max=1.0),
]


def test_detection_and_draft_on_a_choice_panel() -> None:
    answers = _answers("customer_choice", CHOICE_COLUMNS)
    assert answers["choice"] == "brand"
    assert answers["customer"] == "household_id"
    assert answers["occasion"] == "trip_no"
    assert answers["prices"] == ["price_cola", "price_fizz", "price_store"]
    assert answers["promos"] == ["promo_cola", "promo_fizz", "promo_store"]
    req = GuidedAnswers(
        template_id="customer_choice", name="soda", dataset="soda_trips", answers=answers,
        columns=CHOICE_COLUMNS,
    )  # fmt: skip
    draft = build_draft(GUIDED_TEMPLATES["customer_choice"], req, project="retail")
    assert draft.issues == []
    assert draft.mapping.wide["price"].columns == {
        "cola": "price_cola", "fizz": "price_fizz", "store": "price_store",
    }  # fmt: skip
    assert set(draft.mapping.wide["promo"].columns) == {"cola", "fizz", "store"}
    spec = ModelSpec.model_validate(draft.spec)
    assert str(spec.model.use) == "choice.multinomial_logit@^1"
    assert spec.data.bind.dims == {"hh": "household_id", "trip": "trip_no", "product": "product"}
    schema = draft_schema(draft.mapping, CHOICE_COLUMNS)
    assert schema.dims["trip_no"].kind == "index" and schema.dims["product"].kind == "categorical"
    assert schema.vars["brand"].dtype == "str" and schema.vars["brand"].missing == "allow"
    assert schema.vars["price"].dims == ("household_id", "trip_no", "product")


def test_mismatched_wide_labels_are_flagged_in_the_draft() -> None:
    answers = _answers("customer_choice", CHOICE_COLUMNS)
    answers["promos"] = ["promo_cola", "promo_fizz"]
    req = GuidedAnswers(template_id="customer_choice", name="soda", dataset="soda", answers=answers)
    [issue] = build_draft(GUIDED_TEMPLATES["customer_choice"], req, project="retail").issues
    assert issue.pointer == "/mapping/wide/promo" and issue.severity is Severity.WARNING


def test_channel_labels_drop_the_question_keywords() -> None:
    assert channel_label("price_coke") == "price_coke"
    assert channel_label("price_coke", ["price"]) == "coke"
    assert channel_label("PromoDeal_Coke", ["promo", "deal"]) == "coke"
    assert channel_label("price", ["price"]) == "price"  # nothing left: the safe name


# --------------------------------------------------------------- stored datasets and MFF


def test_profiles_of_a_stored_dataset() -> None:
    schema = DataSchema.model_validate(
        {
            "dims": {"date": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                     "market": {"kind": "categorical"}},
            "vars": {"revenue": {"dims": ["date", "market"], "dtype": "float64"},
                     "tv": {"dims": ["date", "market"], "dtype": "float64"},
                     "market_share": {"dims": ["date", "market"], "dtype": "float64"}},
        }
    )  # fmt: skip
    summary = DataSummary(
        vars=["revenue", "tv", "market_share"],
        coords={
            "date": CoordSummary(kind="time", size=104, start="2024-01-07", end="2025-12-28",
                                 freq="W-SUN"),
            "market": CoordSummary(kind="categorical", size=4, values=["a", "b", "c", "d"]),
        },
    )  # fmt: skip
    profile = ProfileView.model_validate(
        {"vars": {"tv": {"dtype": "float64", "size": 416, "count": 416, "min": 0.0,
                         "max": 9.0}}}
    )  # fmt: skip
    cols = profiles_from_dataset(schema, summary, profile)
    by = {c.name: c for c in cols}
    assert by["date"].dtype == "date" and by["date"].detected_freq == "W-SUN"
    assert by["date"].origin == "dim" and by["date"].n_rows == 416
    assert by["market"].n_distinct == 4 and by["market"].sample == ["a", "b", "c", "d"]
    assert by["tv"].min == 0.0 and by["revenue"].origin == "variable"
    answers = _answers("geo_mmm", cols)
    # 'market_share' is a variable, so it is never the geo dim (and 'market' never a role)
    assert answers["geo"] == "market" and answers["date"] == "date"
    assert answers["kpi"] == "revenue" and "market" not in answers["controls"]
    assert profiles_from_dataset(schema)[0].n_distinct is None  # no summary: no counts


MFF_COLUMNS = [
    _col("Period", "date", n_distinct=104, detected_freq="W-MON"),
    _col("Geography", "string", n_distinct=6),
    _col("Sales", "number", min=10.0, max=900.0, origin="mff_variable"),
    _col("TV_Spend", "number", min=0.0, max=90.0, origin="mff_variable"),
    _col("Search_Spend", "number", min=0.0, max=50.0, origin="mff_variable"),
    _col("Price", "number", min=1.0, max=2.0, origin="mff_variable"),
]


def test_mff_variables_answer_questions_and_draft_an_mff_mapping() -> None:
    answers = _answers("geo_mmm", MFF_COLUMNS)
    assert answers["kpi"] == "Sales" and answers["geo"] == "Geography"
    assert answers["spend"] == ["Search_Spend", "TV_Spend"]
    req = GuidedAnswers(
        template_id="geo_mmm", name="mff-mmm", dataset="mff", format="mff", answers=answers,
        columns=MFF_COLUMNS,
    )  # fmt: skip
    draft = build_draft(GUIDED_TEMPLATES["geo_mmm"], req, project="growth")
    assert draft.mapping.format == "mff" and draft.mapping.preset is IngestPreset.MFF
    assert draft.mapping.dims["period"].column == "Period"
    assert draft.mapping.dims["period"].spec.freq == "W-MON"
    assert draft.mapping.wide["spend"].columns == {"search": "Search_Spend", "tv": "TV_Spend"}
    again = IngestMapping.model_validate(draft.mapping.model_dump(mode="json"))
    assert again == draft.mapping
    schema = draft_schema(draft.mapping, MFF_COLUMNS)
    assert schema.vars["spend"].dims == ("period", "geography", "channel")


def test_draft_schema_needs_dims() -> None:
    with pytest.raises(ValueError, match="no dims"):
        draft_schema(IngestMapping(format="mff"))


def test_mff_combine_option() -> None:
    opts = MffOptions(combine={"geo": ["Geography", "Product"]})
    assert MffOptions.model_validate(opts.model_dump(mode="json")) == opts
    assert "combine" not in MffOptions().model_dump(mode="json")  # off the wire unless set
    for bad in ({"geo": []}, {"a": ["Geography"], "b": ["Geography"]}, {"g": ["Geography"] * 2}):
        with pytest.raises(ValidationError):
            MffOptions(combine=bad)


def test_index_coords_have_ordinals() -> None:
    cs = CoordSummary(kind="index", size=3, start=1, end=3, step=1)
    assert cs.ordinal(2) == 2
    assert CoordSummary(kind="categorical", size=1, values=["a"]).ordinal("a") is None


# ======================================================================== persona lock


def _geo_draft() -> dict[str, Any]:
    answers = _answers("geo_mmm", MMM_COLUMNS)
    req = GuidedAnswers(template_id="geo_mmm", name="mmm", dataset="media", answers=answers)
    return copy.deepcopy(dict(build_draft(GUIDED_TEMPLATES["geo_mmm"], req, project="growth").spec))


@pytest.mark.parametrize("template", sorted(GUIDED_TEMPLATES))
def test_every_draft_passes_the_guided_lock(template: str) -> None:
    answers = {
        "geo_mmm": _answers("geo_mmm", MMM_COLUMNS),
        "weekly_forecast": {"kpi": "sales", "date": "week"},
        "store_demand": {"kpi": "units", "date": "week", "store": "store_id"},
        "customer_choice": _answers("customer_choice", CHOICE_COLUMNS),
    }[template]
    req = GuidedAnswers(template_id=template, name="n", dataset="d", answers=answers)
    draft = build_draft(GUIDED_TEMPLATES[template], req, project="p")
    assert guided_lock_issues(draft.spec) == []
    assert guided_lock_issues(draft.spec, parent=draft.spec) == []


def test_the_guided_lock_keeps_advanced_and_hidden_fields() -> None:
    doc = _geo_draft()
    basic = copy.deepcopy(doc)
    basic["model"]["settings"]["hierarchy"] = {"pool_over": []}  # a basic setting
    basic["calibration"] = {"business_priors": {"channels": {"tv": {"roi": [1.0, 3.0]}}}}
    assert guided_lock_issues(basic, parent=doc) == []
    fit = copy.deepcopy(doc)
    fit["model"]["fit"] = {**fit["model"].get("fit", {}), "draws": 50}
    [issue] = guided_lock_issues(fit, parent=doc)
    assert issue.pointer == "/model/fit" and issue.code is ErrorCode.FORBIDDEN
    assert "advanced setting" in issue.message and issue.suggested_action
    hidden = copy.deepcopy(doc)
    hidden["pipeline"][0]["with"]["how"] = "mean"
    assert [i.pointer for i in guided_lock_issues(hidden)] == ["/pipeline"]  # a new spec too
    plain = copy.deepcopy(doc)
    del plain["metadata"]["labels"]["guided"]
    assert [i.pointer for i in guided_lock_issues(plain)] == ["/metadata/labels/guided"]
    other = copy.deepcopy(doc)
    other["model"]["use"] = "tabular.regressor@^1"
    assert "/model/use" in {i.pointer for i in guided_lock_issues(other)}
    assert guided_lock_issues({"kind": "nonsense"})[0].pointer == ""
