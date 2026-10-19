"""M3.5 About cards: the model and its editorial rules, the catalog rule, completeness of
every built-in card (steps, splits, models, templates, nodes, checks, workflow templates),
dataset summaries and the Help-me-choose ranking."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec.about import (
    CHOOSER_QUESTIONS,
    About,
    AboutKind,
    ChooserAnswer,
    about_issues,
    chooser_answers_from_query,
    describe_dataset,
    recommend,
)
from mapi_spec.abouts import (
    MODEL_ABOUTS,
    PLANNED_MODEL_IDS,
    SCIENCE_MODEL_IDS,
    SPLIT_ABOUTS,
    STEP_ABOUTS,
    SURVIVING_MODEL_IDS,
    TEMPLATE_ABOUTS,
    builtin_about,
    builtin_about_kind,
    builtin_abouts,
)
from mapi_spec.catalog import Catalog, catalog_about_issues
from mapi_spec.coords import CoordSummary, DataSummary
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.guided import GUIDED_TEMPLATES
from mapi_spec.schema import DataSchema
from pydantic import ValidationError

pytestmark = pytest.mark.unit

EXAMPLE: dict[str, Any] = About.model_json_schema()["examples"][0]


def test_the_example_is_complete() -> None:
    about = About.model_validate(EXAMPLE)
    assert about_issues(about) == []
    assert about_issues(None) == ["the item has no About card"]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"use_when": []}, "at least 1"),
        ({"summary_plain": "Short"}, "at least 20"),
        ({"title": ""}, "at least 3"),
        ({"difficulty": "wizard"}, "beginner"),
        ({"typical_runtime": "forever"}, "instant"),
        ({"outputs": ["same text", "Same text"]}, "unique"),
        ({"outputs": ["x"]}, "3-300"),
        ({"related": ["Not An Id"]}, "catalog ids"),
        ({"fits": {"goal": []}}, "non-empty"),
        ({"fits": {"Goal": ["forecast"]}}, "question id"),
        ({"surprise": 1}, "Extra inputs"),
    ],
)
def test_structural_rules(change: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        About.model_validate({**EXAMPLE, **change})


@pytest.mark.parametrize(
    ("change", "issue"),
    [
        ({"summary_plain": "Adds up daily numbers. Into weeks. And months."}, "one or two"),
        ({"summary_plain": "Adds up daily numbers into weeks without any full stop"},
         "one or two"),
        ({"summary_plain": "Uses the posterior of an ARIMA fit to add things up."}, "jargon"),
        ({"fits": {"colour": ["red"]}}, "unknown chooser question"),
        ({"fits": {"goal": ["world_peace"]}}, "unknown answers"),
        ({"example": EXAMPLE["summary_plain"]}, "not repeat the summary"),
    ],
)  # fmt: skip
def test_editorial_rules(change: dict[str, Any], issue: str) -> None:
    about = About.model_validate({**EXAMPLE, **change})
    assert any(issue in i for i in about_issues(about)), about_issues(about)


def test_related_ids_are_checked_against_known_items() -> None:
    about = About.model_validate({**EXAMPLE, "related": ["core.calendar", "core.nope"]})
    assert about_issues(about, known_ids={"core.calendar"}) == [
        "related names unknown items ['core.nope']"
    ]


def test_every_builtin_card_is_complete_and_cross_references_resolve() -> None:
    cards = builtin_abouts()
    known = set(cards)
    problems = {k: about_issues(v, known_ids=known) for k, v in cards.items()}
    assert not {k: v for k, v in problems.items() if v}
    kinds = {builtin_about_kind(k) for k in cards}
    assert kinds == set(AboutKind) - {AboutKind.DATASET, AboutKind.SPEC, AboutKind.PLOT}
    assert builtin_about("core.resample") is STEP_ABOUTS["core.resample"]
    assert builtin_about("nope") is None and builtin_about_kind("nope") is None
    assert builtin_about("kind.branch") is not None
    assert builtin_about("workflow.ts_readiness_arimax") is not None


def test_cards_cover_the_surviving_catalog_and_the_planned_engines() -> None:
    assert len(STEP_ABOUTS) == 28 and all(k.startswith("core.") for k in STEP_ABOUTS)
    assert set(SPLIT_ABOUTS) == {"core.time_holdout", "core.as_of", "core.leave_group_out",
                                 "core.last_per_group", "core.mask_block"}  # fmt: skip
    assert {"tabular.regressor", "tabular.hierarchical_glm", "choice.multinomial_logit",
            "ts.gaussian_hmm", "ts.hsgp_trend", "sklearn.tabular_regressor",
            "sklearn.tabular_quantiles", "sklearn.tabular_bootstrap",
            "sklearn.tabular_conformal", "sklearn.tabular_classifier"
            } == SURVIVING_MODEL_IDS  # fmt: skip
    assert {"mmm.bayesian", "mmm.nested", "mmm.multivariate", "mmm.frequentist", "ts.sarimax",
            "ts.arima", "ts.var", "ts.varmax", "ts.vecm", "ts.ecm", "ts.ardl", "ts.ets",
            "ts.unobserved_components", "ts.dynamic_factor", "ts.markov_switching",
            "ts.theta"} == set(PLANNED_MODEL_IDS)  # fmt: skip
    assert {
        "sci.lgcp_grid",
        "sci.fisher_kpp",
        "sci.traction_force",
        "sci.strong_lens",
        "sci.heat_plate",
        "sci.turing_pattern",
        "sci.storm_localisation",
        "sci.ibp_features",
        "sci.colour_naming",
        "sci.cmb_field",
        "sci.areal_bym2",
        "sci.network_latent_space",
        "sci.graphical_model",
        "sci.phylo_regression",
        "sci.circular_regression",
    } == SCIENCE_MODEL_IDS
    assert set(MODEL_ABOUTS) == SURVIVING_MODEL_IDS | PLANNED_MODEL_IDS | SCIENCE_MODEL_IDS
    for template in GUIDED_TEMPLATES.values():
        assert template.about == TEMPLATE_ABOUTS[template.id]


def test_the_catalog_rule(catalog: Catalog) -> None:
    issues = catalog_about_issues(catalog)
    assert issues and all(i.code is ErrorCode.ABOUT_INCOMPLETE for i in issues)
    assert all(i.severity.value == "error" for i in issues)
    pointers = {i.pointer for i in issues}
    assert "/steps/0/about" in pointers and "/models/0/manifest/about" in pointers
    data = catalog.model_dump(mode="json", by_alias=True)
    for step in data["steps"]:
        step["about"] = copy.deepcopy(EXAMPLE)
    for model in data["models"]:
        model["manifest"]["about"] = copy.deepcopy(EXAMPLE)
    full = Catalog.model_validate(data)
    assert catalog_about_issues(full) == []
    data["steps"][0]["about"]["related"] = ["core.nope", "core.time_holdout"]
    unknown = Catalog.model_validate(data).about_issues()  # built-in ids are known
    assert [(i.pointer, i.severity.value) for i in unknown] == [
        ("/steps/0/about/related", "warning")
    ]
    assert "core.nope" in unknown[0].message and "time_holdout" not in unknown[0].message
    assert catalog_about_issues(Catalog.model_validate(data), builtins=False)[0].severity.value == (
        "warning"
    )
    # yanked entries are exempt
    for step in data["steps"]:
        step["about"] = None
        step["yanked"] = True
    data["models"] = []
    assert catalog_about_issues(Catalog.model_validate(data)) == []


def test_dataset_summaries_come_from_metadata() -> None:
    schema = DataSchema.model_validate({
        "dims": {"week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                 "geo": {"kind": "categorical"}, "channel": {"kind": "categorical"}},
        "vars": {"revenue": {"dims": ["week", "geo"], "dtype": "float64", "role": "target"},
                 "spend": {"dims": ["week", "geo", "channel"], "dtype": "float64",
                           "role": "spend"},
                 "price": {"dims": ["week", "geo"], "dtype": "float64"}},
    })  # fmt: skip
    summary = DataSummary(
        vars=["revenue", "spend", "price"],
        coords={
            "week": CoordSummary(kind="time", size=104, start="2023-01-01", end="2024-12-29",
                                 freq="W-SUN"),
            "geo": CoordSummary(kind="categorical", size=10, values=[f"g{i}" for i in range(10)]),
            "channel": CoordSummary(kind="categorical", size=3, values=["tv", "search", "social"]),
        },
    )  # fmt: skip
    about = describe_dataset(schema, summary, description="From finance.")
    assert about.summary_generated == (
        "Weekly data from 2023-01-01 to 2024-12-29 (104 weeks) for 10 geos and 3 channels, "
        "with 3 measures: revenue (the result you want to explain), spend (marketing spend), "
        "price."
    )
    assert about.highlights == ["104 weeks of history", "10 geos", "3 channels"]
    assert about.description_user == "From finance."
    bare = describe_dataset(None, DataSummary(vars=["x"]))
    assert bare.summary_generated == "Data, with 1 measure: x."


def test_dataset_summaries_count_a_pair_of_dims_once() -> None:
    """A network or a neighbour matrix is on (X, X_): X_ holds the same items, not a group."""
    districts = [f"d{i}" for i in range(56)]
    summary = DataSummary(
        vars=["cases", "adjacency"],
        coords={"district": CoordSummary(kind="categorical", size=56, values=districts),
                "district_": CoordSummary(kind="categorical", size=56, values=districts)},
    )  # fmt: skip
    about = describe_dataset(None, summary)
    assert about.summary_generated.startswith("Data for 56 districts, with 2 measures")
    assert about.highlights == ["56 districts"]
    species = DataSummary(
        vars=["brain"],
        coords={"species": CoordSummary(kind="categorical", size=186)},
    )
    assert describe_dataset(None, species).summary_generated == (
        "Data for 186 species, with 1 measure: brain."
    )


def test_dataset_summaries_show_dates_without_midnight_times() -> None:
    """Ingest summarises datetime coordinates as ISO timestamps; the plain summary shows the
    date alone when the time is midnight (and keeps other times)."""
    summary = DataSummary(
        vars=["y"],
        coords={"t": CoordSummary(kind="time", size=156, start="2023-01-01T00:00:00",
                                  end="2025-12-21T00:00:00", freq="W-SUN")},
    )  # fmt: skip
    text = describe_dataset(None, summary).summary_generated
    assert text.startswith("Weekly data from 2023-01-01 to 2025-12-21 (156 weeks)")
    hourly = DataSummary(
        vars=["y"],
        coords={"t": CoordSummary(kind="time", size=24, start="2023-01-01T00:00:00Z",
                                  end="2023-01-01T23:00:00", freq="h")},
    )  # fmt: skip
    hourly_text = describe_dataset(None, hourly).summary_generated
    assert "from 2023-01-01 to 2023-01-01T23:00:00" in hourly_text


def test_the_questions_are_well_formed() -> None:
    ids = [q.id for q in CHOOSER_QUESTIONS]
    assert ids == ["goal", "data_shape", "frequency", "history", "drivers", "uncertainty",
                   "experience", "speed"]  # fmt: skip
    with pytest.raises(ValidationError, match="unknown chooser question"):
        ChooserAnswer(question_id="colour", values=["red"])
    with pytest.raises(ValidationError, match="unknown answers"):
        ChooserAnswer(question_id="goal", values=["fly"])
    with pytest.raises(ValidationError, match="takes one answer"):
        ChooserAnswer(question_id="history", values=["short", "long"])


def _models() -> list[tuple[AboutKind, str, About]]:
    return [(AboutKind.MODEL, k, v) for k, v in MODEL_ABOUTS.items()]


def test_recommendations_for_a_marketing_question() -> None:
    answers = chooser_answers_from_query(
        {"goal": ["explain_marketing"], "data_shape": ["panel"], "uncertainty": ["essential"],
         "experience": ["intermediate"], "kind": ["model"], "limit": ["5"]}
    )  # fmt: skip
    result = recommend(answers, _models(), limit=5)
    ids = [r.id for r in result.items]
    assert ids[0] == "mmm.bayesian"
    assert all(r.id.startswith("mmm.") for r in result.items)  # goal filter
    assert result.answered == ["goal", "data_shape", "uncertainty", "experience"]
    top = result.items[0]
    assert top.reasons_plain and top.score > result.items[-1].score
    expert = next(r for r in result.items if r.id == "mmm.nested")
    assert any("expert" in c for c in expert.cautions_plain)


def test_recommendations_for_a_quick_beginner_forecast() -> None:
    answers = [ChooserAnswer(question_id="goal", values=["forecast"]),
               ChooserAnswer(question_id="data_shape", values=["one_series"]),
               ChooserAnswer(question_id="drivers", values=["no"]),
               ChooserAnswer(question_id="experience", values=["beginner"]),
               ChooserAnswer(question_id="speed", values=["seconds"])]  # fmt: skip
    result = recommend(answers, _models(), limit=3, include_about=True)
    assert [r.id for r in result.items][:2] == ["ts.ets", "ts.theta"]
    assert result.items[0].about is not None
    ranked = recommend(answers, _models(), limit=50)
    assert [r.score for r in ranked.items] == sorted((r.score for r in ranked.items), reverse=True)
    assert recommend([], _models(), limit=2).items[0].score == 0.5  # nothing answered


def test_forecast_weekly_sales_with_drivers_recommends_arimax() -> None:
    """Items that fit every answer outrank items silent on some at the same score: the
    model made for weekly forecasts *with drivers* comes first (found on the live stack,
    where a smooth-trend model with no drivers came first on difficulty)."""
    answers = chooser_answers_from_query(
        {"goal": ["forecast"], "data_shape": ["one_series"], "frequency": ["weekly"],
         "drivers": ["yes"]}
    )  # fmt: skip
    result = recommend(answers, _models(), limit=3)
    assert result.items[0].id == "ts.sarimax"
    assert len(result.items[0].reasons_plain) == 4 and not result.items[0].cautions_plain
    assert "drivers" not in MODEL_ABOUTS["ts.hsgp_trend"].fits  # silent on drivers: below


def test_items_not_made_for_the_goal_are_never_recommended() -> None:
    """Regression: with no candidate made for the goal ("Check or prepare my data"), every
    candidate came back, the first labelled best match at 0% with "Not made for"."""
    answers = chooser_answers_from_query({"goal": ["check_data"], "experience": ["beginner"]})
    result = recommend(answers, _models(), limit=10)
    assert result.items == []
    steps = [(AboutKind.STEP, k, v) for k, v in STEP_ABOUTS.items()]
    broadened = recommend(answers, _models(), fallback=steps, limit=5)
    assert broadened.items and all(r.kind is AboutKind.STEP for r in broadened.items)
    assert all("Suits: examine or prepare my data" in r.reasons_plain for r in broadened.items)
    assert not any("Not made for" in c for r in broadened.items for c in r.cautions_plain)
    # when a starting point fits, the fallback is not used
    fc = chooser_answers_from_query({"goal": ["forecast"]})
    assert {r.kind for r in recommend(fc, _models(), fallback=steps).items} == {AboutKind.MODEL}


def test_every_goal_has_a_starting_point() -> None:
    starting = {AboutKind.MODEL, AboutKind.TEMPLATE, AboutKind.WORKFLOW_TEMPLATE}
    goals: set[str] = set()
    for item_id, about in builtin_abouts().items():
        if builtin_about_kind(item_id) in starting:
            goals.update(about.fits.get("goal", []))
    assert {c.value for c in CHOOSER_QUESTIONS[0].choices} <= goals


def test_recommendation_query_errors() -> None:
    with pytest.raises(MapiError) as exc:
        chooser_answers_from_query({"goal": ["forecast,teleport"]})
    assert exc.value.code is ErrorCode.CHOOSER_ANSWER_INVALID and exc.value.pointer == "/goal"
    assert chooser_answers_from_query({"goal": ["forecast, plan_budget"]})[0].values == [
        "forecast", "plan_budget"]  # fmt: skip


def test_specs_and_workflows_may_carry_an_about_card() -> None:
    from mapi_spec.examples import E33_YAML
    from mapi_spec.loader import load_model_spec, spec_hash
    from mapi_spec.spec import Metadata

    spec = load_model_spec(E33_YAML)
    assert spec.metadata.about is None
    assert "about" not in spec.model_dump(mode="json", by_alias=True)["metadata"]
    meta = Metadata(name="geo", project="growth", about=About.model_validate(EXAMPLE))
    described = spec.model_copy(update={"metadata": meta})
    assert spec_hash(described) != spec_hash(spec)


def test_plot_examples_are_self_contained_and_keyed_by_ref() -> None:
    from mapi_spec.plots import PlotExamples, PlotSpec

    doc = PlotSpec.model_json_schema()["examples"][0]
    inline = {**doc, "datasets": {"fan": {"kind": "inline_arrow", "data": "QUJD"}}}
    assert PlotExamples.model_validate({"plots": {"core.ppc_fan@1.0.0": inline}}).plots
    with pytest.raises(ValidationError, match="inline_arrow only"):
        PlotExamples.model_validate({"plots": {"core.ppc_fan@1.0.0": doc}})  # a data_ref
    with pytest.raises(ValidationError, match="keyed by recipe ref"):
        PlotExamples.model_validate({"plots": {"core.ppc_fan": inline}})
    assert AboutKind.PLOT.value == "plot"
