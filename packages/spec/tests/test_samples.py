"""Sample projects (v0.18 D20): the definition drafts and validates, the data is pinned, the
life cycle is closed and the library seed parses and compiles."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import itertools
from pathlib import Path
from typing import Any, get_args

import pytest
from mapi_spec.catalog import Catalog
from mapi_spec.errors import Severity
from mapi_spec.guided import GUIDED_TEMPLATES, MMM_TEMPLATE_SETTINGS, draft_schema
from mapi_spec.samples import (
    ACTIVE_SAMPLE_STATES,
    GEO_MMM_SAMPLE,
    SAMPLE_LABEL,
    SAMPLE_STATES,
    SAMPLE_TRANSITIONS,
    SAMPLES,
    SampleDef,
    SampleInfo,
    SampleState,
    can_transition,
    sample_draft,
    sample_project_ids,
)
from mapi_spec.spec import ModelSpec
from mapi_spec.validate import validate_spec
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
SERVER_DATA = ROOT / "packages/server/src/mapi_server/samples/data"


# ======================================================================== a toy catalog

_MMM_MANIFEST: dict[str, Any] = {
    "id": "mmm.bayesian",
    "version": "1.1.0",
    "capabilities": {
        "inference": "mcmc",
        "representation": "draws",
        "score_modes": ["predict", "forecast", "counterfactual", "evaluate"],
        "decision": True,
        "contribution_scale": "additive",
    },
    "schema_template": {
        "params": {
            "panel": {"type": "string_list", "path": "hierarchy.pool_over", "default": []},
        },
        "template": {
            "dims": {
                "time": {"kind": "time", "policy": "extend"},
                "channel": {"kind": "categorical", "policy": "fixed"},
                "{*panel as d}": {"kind": "categorical", "policy": "new_levels",
                                  "new_level_strategy": "pooled"},
            },
            "vars": {
                "y": {"dims": ["{*panel}", "time"], "dtype": "float64", "role": "target"},
                "spend": {"dims": ["{*panel}", "channel", "time"], "dtype": "float64",
                          "role": "spend", "future_required": True},
            },
        },
    },
    "settings_schema": {
        "type": "object",
        "properties": {k: {"type": "object"} for k in MMM_TEMPLATE_SETTINGS},
        "additionalProperties": False,
    },
    "fit_schema": {
        "type": "object",
        "properties": {
            "mode": {"enum": ["evaluate_then_refit", "train_only"]},
            "engine": {"enum": ["nutpie", "pymc"]},
            "tune": {"type": "integer", "minimum": 0},
            "draws": {"type": "integer", "minimum": 1},
            "chains": {"type": "integer", "minimum": 1},
            "target_accept": {"type": "number"},
            "seed": {"type": "integer"},
        },
        "additionalProperties": False,
    },
    "outputs": {"y_hat": {"dims": ["time"], "dims_of": "y", "units_of": "y"}},
    "derived": [
        {"name": n, "dims": ["sample", "channel"], "cache": "fit"}
        for n in ("contributions", "response_curves", "marginal_cost", "adstock")
    ],
    "plots": ["mmm.decomposition", "mmm.response_curves", "mmm.roi"],
    "resources": {"min_class": "S", "default_class": "M"},
}  # fmt: skip
"""The parts of ``mmm.bayesian`` the sample's spec touches (shape of the live manifest)."""

_PLOTS = ["core.ppc_fan", "core.convergence", "mmm.decomposition", "mmm.response_curves",
          "mmm.roi"]  # fmt: skip


@pytest.fixture
def toy_catalog(catalog: Catalog) -> Catalog:
    """The shared toy catalog's steps with a sample-shaped MMM and the sample's plots."""
    return Catalog.model_validate(
        {
            "steps": [s.model_dump(mode="json", by_alias=True) for s in catalog.steps],
            "models": [{"manifest": _MMM_MANIFEST}],
            "plots": [{"id": p, "title": p} for p in _PLOTS],
        }
    )


# ======================================================================== the definition


def test_samples_registry() -> None:
    assert list(SAMPLES) == ["geo_mmm"] and SAMPLES["geo_mmm"] is GEO_MMM_SAMPLE
    with pytest.raises(TypeError):
        SAMPLES["x"] = GEO_MMM_SAMPLE  # type: ignore[index]


def test_geo_mmm_definition() -> None:
    s = GEO_MMM_SAMPLE
    assert s.template_id == "geo_mmm" and s.template_id in GUIDED_TEMPLATES
    assert s.answers.template_id == s.template_id and s.answers.dataset == s.dataset
    assert s.minutes == 1.0
    # WP0 (g): 4 chains x 300 draws after 300 tuning steps, one fit on the train window
    assert s.fit == {"mode": "train_only", "engine": "nutpie", "tune": 300, "draws": 300,
                     "chains": 4}  # fmt: skip
    assert s.labels == {SAMPLE_LABEL: "geo_mmm"}
    assert len(s.library_seed) == 3


def test_the_definition_is_strict() -> None:
    doc = GEO_MMM_SAMPLE.model_dump(mode="json", by_alias=True)
    SampleDef.model_validate(doc)
    for change in (
        {"template_id": "no_such_template"},
        {"data_sha256": "8413b6c0"},
        {"data_file": "../etc/passwd"},
        {"minutes": 0},
        {"surprise": 1},
    ):
        with pytest.raises(ValidationError):
            SampleDef.model_validate({**doc, **change})


def test_the_draft_validates_on_a_toy_catalog(toy_catalog: Catalog) -> None:
    draft, doc = sample_draft(GEO_MMM_SAMPLE, project="sample")
    assert draft.issues == []
    assert draft.dataset_ref == "dataset://sample/sample-geo-mmm@latest"
    m = draft.mapping
    assert set(m.dims) == {"week", "region"} and m.dims["week"].spec.freq == "W-SUN"
    assert m.vars["revenue_gbp"].role.value == "target"
    assert m.wide["spend"].columns == {
        "search": "search_spend", "social": "social_spend", "tv": "tv_spend",
    }  # fmt: skip
    spec = ModelSpec.model_validate(doc)
    assert spec.metadata.project == "sample" and spec.metadata.name == "sample-geo-mmm"
    assert spec.metadata.labels == {"family": "mmm", "guided": "geo_mmm", SAMPLE_LABEL: "geo_mmm"}
    fit = doc["model"]["fit"]
    assert {k: fit[k] for k in GEO_MMM_SAMPLE.fit} == GEO_MMM_SAMPLE.fit
    template_fit: Any = GUIDED_TEMPLATES["geo_mmm"].spec_template["model"]
    assert fit["seed"] == template_fit["fit"]["seed"]
    schema = draft_schema(draft.mapping, GEO_MMM_SAMPLE.answers.columns)
    result = validate_spec(spec, toy_catalog, schema)
    assert result.ok, [(i.pointer, i.message) for i in result.errors]


def test_the_draft_is_deterministic_and_project_scoped() -> None:
    _, a = sample_draft(GEO_MMM_SAMPLE, project="sample")
    _, b = sample_draft(GEO_MMM_SAMPLE, project="sample")
    _, c = sample_draft(GEO_MMM_SAMPLE, project="sample-2")
    assert a == b
    assert c["metadata"]["project"] == "sample-2"
    assert c["data"]["dataset"] == "dataset://sample-2/sample-geo-mmm@latest"


def test_the_draft_does_not_mutate_the_definition() -> None:
    before = copy.deepcopy(GEO_MMM_SAMPLE.model_dump(mode="json"))
    sample_draft(GEO_MMM_SAMPLE, project="sample")
    assert GEO_MMM_SAMPLE.model_dump(mode="json") == before


def test_profiles_name_the_sample_columns() -> None:
    cols = [c.name for c in GEO_MMM_SAMPLE.answers.columns or []]
    assert cols == ["week", "region", "Revenue (GBP)", "tv_spend", "search_spend",
                    "social_spend", "avg_price"]  # fmt: skip


# ======================================================================== the data


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_data_sha256_pins_the_server_file() -> None:
    path = SERVER_DATA / GEO_MMM_SAMPLE.data_file
    if not path.exists():
        pytest.skip(f"{path.relative_to(ROOT)} is not there yet (WP-T3 ships it)")
    data = path.read_bytes()
    assert _sha(data) == GEO_MMM_SAMPLE.data_sha256
    header = data.split(b"\n", 1)[0].decode()
    assert header.split(",") == [c.name for c in GEO_MMM_SAMPLE.answers.columns or []]


@pytest.mark.skipif(
    importlib.util.find_spec("pandas") is None or importlib.util.find_spec("numpy") is None,
    reason="the table generator needs numpy and pandas",
)
def test_data_sha256_matches_the_generator() -> None:
    """``scripts/make_demo_grid.py`` makes the table byte for byte (seed 20261021)."""
    spec = importlib.util.spec_from_file_location(
        "make_demo_grid", ROOT / "scripts/make_demo_grid.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = mod.sample_table().to_csv(index=False, lineterminator="\n")
    assert _sha(text.encode("utf-8")) == GEO_MMM_SAMPLE.data_sha256


# ======================================================================== life cycle


def test_states_and_transitions() -> None:
    assert get_args(SampleState) == SAMPLE_STATES
    assert set(SAMPLE_TRANSITIONS) == set(SAMPLE_STATES)
    # v0.18 review: the loop polls provisioning too, and the partial index must cover it
    assert {"provisioning", "ingesting", "fitting"} == ACTIVE_SAMPLE_STATES
    happy: list[SampleState] = ["provisioning", "ingesting", "fitting", "ready"]
    for a, b in itertools.pairwise(happy):
        assert can_transition(a, b)
    for s in ("provisioning", "ingesting", "fitting"):
        assert can_transition(s, "failed") and can_transition(s, "retired")
    assert can_transition("failed", "provisioning")  # retry
    assert not can_transition("ready", "failed") and not can_transition("ready", "fitting")
    assert not any(can_transition("retired", s) for s in SAMPLE_STATES)
    assert not can_transition("provisioning", "ready")


def test_project_ids() -> None:
    ids = sample_project_ids()
    assert ids[0] == "sample" and ids[1] == "sample-2" and ids[-1] == "sample-9"
    assert len(ids) == len(set(ids)) == 9


def test_sample_info_examples_and_state_names() -> None:
    for ex in SampleInfo.model_json_schema()["examples"]:
        info = SampleInfo.model_validate(ex)
        assert SampleInfo.model_validate(info.model_dump(mode="json")) == info
    base = {"id": "geo_mmm", "title": "t", "summary": "s", "minutes": 1.0}
    with pytest.raises(ValidationError):
        SampleInfo.model_validate({**base, "state": "installing"})
    with pytest.raises(ValidationError):
        SampleInfo.model_validate({**base, "progress": 1.5})


# ======================================================================== the library seed

_GLM_MANIFEST: dict[str, Any] = {
    "id": "tabular.declarative_glm",
    "version": "1.0.0",
    "capabilities": {
        "inference": "mcmc",
        "representation": "draws",
        "score_modes": ["predict", "evaluate"],
    },
    "schema_template": {"template": {"dims": {}, "vars": {}}},
    "settings_schema": {"type": "object"},
}
"""Enough of ``tabular.declarative_glm`` for the compiler (its settings are WP-X7's)."""

_STANDARDIZE: dict[str, Any] = {
    "id": "core.standardize",
    "version": "1.0.0",
    "kind": "value",
    "stateful": True,
    "invertible": True,
    "settings_schema": {
        "type": "object",
        "properties": {
            "vars": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "over": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["vars"],
        "additionalProperties": False,
    },
    "var_params": ["vars"],
}


def _sample_schema() -> Any:
    draft, _ = sample_draft(GEO_MMM_SAMPLE, project="sample")
    return draft_schema(draft.mapping, GEO_MMM_SAMPLE.answers.columns)


def _seed(kind: str) -> dict[str, Any]:
    [doc] = [d for d in GEO_MMM_SAMPLE.library_seed if d["kind"] == kind]
    return doc


def test_the_seed_is_one_of_each_kind_and_parses() -> None:
    from mapi_spec.library import parse_library_doc

    kinds = [d["kind"] for d in GEO_MMM_SAMPLE.library_seed]
    assert kinds == ["ModelRecipe", "PrepPreset", "ChartTemplate"]
    names = set()
    for d in GEO_MMM_SAMPLE.library_seed:
        doc = parse_library_doc(d)
        assert doc.metadata.tags == ["sample"]
        names.add(doc.metadata.name)
    assert len(names) == 3


def test_the_seed_model_recipe_compiles(toy_catalog: Catalog) -> None:
    from mapi_spec.library import ModelRecipeDoc, parse_library_doc
    from mapi_spec.model_recipes import DECLARATIVE_MODEL_ID, compile_model_recipe

    doc = parse_library_doc(_seed("ModelRecipe"))
    assert isinstance(doc, ModelRecipeDoc)
    cat = Catalog.model_validate(
        {
            "steps": [
                *(s.model_dump(mode="json", by_alias=True) for s in toy_catalog.steps),
                _STANDARDIZE,
            ],
            "models": [{"manifest": _GLM_MANIFEST}],
        }
    )
    compiled = compile_model_recipe(doc, cat, item_ref="lib_01jaf3q2x8m9v7k5h4g3f2e1d0-r1")
    assert compiled.ok, [(i.pointer, i.message) for i in compiled.issues]
    assert str(compiled.model.use).startswith(DECLARATIVE_MODEL_ID)
    assert compiled.bind.vars == {"y": "revenue_gbp"}
    assert [s.use.id for s in compiled.pipeline] == ["core.standardize"]


def test_the_seed_model_recipe_fits_the_sample_data() -> None:
    """With the sample's schema and the live steps: the (week, region) panel goes long and
    every effect checks out."""
    sdk = pytest.importorskip("mapi_sdk.registry")
    from mapi_spec.library import ModelRecipeDoc, parse_library_doc
    from mapi_spec.model_recipes import compile_model_recipe

    live = sdk.default_registry().catalog()
    cat = Catalog.model_validate(
        {
            "steps": [s.model_dump(mode="json", by_alias=True) for s in live.steps],
            "models": [{"manifest": _GLM_MANIFEST}],
        }
    )
    doc = parse_library_doc(_seed("ModelRecipe"))
    assert isinstance(doc, ModelRecipeDoc)
    compiled = compile_model_recipe(doc, cat, schema=_sample_schema())
    assert compiled.ok, [(i.pointer, i.message) for i in compiled.issues]
    assert [s.use.id for s in compiled.pipeline] == ["core.to_long", "core.standardize"]


def test_the_seed_prep_preset_applies(toy_catalog: Catalog) -> None:
    from mapi_spec.library import PrepPresetDoc, parse_library_doc
    from mapi_spec.prep_presets import apply_prep_preset

    doc = parse_library_doc(_seed("PrepPreset"))
    assert isinstance(doc, PrepPresetDoc)
    slots: dict[str, Any] = {"time": "week", "skewed": ["revenue_gbp", "avg_price"]}
    steps, issues = apply_prep_preset(doc, slots, schema=_sample_schema(), catalog=toy_catalog)
    assert issues == []
    assert [s.model_dump(mode="json", by_alias=True) for s in steps] == [
        {"id": "p_weekly", "use": "core.resample@^1",
         "with": {"dim": "week", "freq": "W-SUN", "how": "sum", "full_periods_only": True}},
        {"id": "p_log", "use": "core.log1p@^1", "with": {"vars": ["revenue_gbp", "avg_price"]}},
    ]  # fmt: skip
    # a slot left empty or naming a column the sample does not have is refused
    _, issues = apply_prep_preset(
        doc, {"time": "week", "skewed": ["nope"]}, schema=_sample_schema()
    )
    assert [i.pointer for i in issues] == ["/slots/skewed"]


def test_the_seed_chart_template_fits_its_source_recipe() -> None:
    from mapi_spec.library import ChartTemplateDoc, parse_library_doc

    templates = pytest.importorskip("mapi_plots.templates")
    doc = parse_library_doc(_seed("ChartTemplate"))
    assert isinstance(doc, ChartTemplateDoc)
    assert doc.source_recipe.id == "core.ppc_fan" and doc.source_recipe.id in _PLOTS
    # every field the template reads is a column of the recipe's dataset
    starter = templates.STARTERS[doc.source_recipe.id]
    columns = set(starter["datasets"]["fan"])
    fields: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("field"), str):
                fields.add(node["field"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc.plot)
    assert doc.plot["data"] == {"name": "fan"} and fields and fields <= columns


def test_the_seed_chart_template_checks_and_applies() -> None:
    from mapi_spec.library import ChartTemplateDoc, parse_library_doc

    charts = pytest.importorskip("mapi_plots.chart_templates")
    recipes = pytest.importorskip("mapi_plots.recipes")
    doc = parse_library_doc(_seed("ChartTemplate"))
    assert isinstance(doc, ChartTemplateDoc)
    example = recipes.get_recipe(doc.source_recipe.id).example_spec()
    issues = charts.check_chart_template(doc, example)
    assert [i for i in issues if i.severity is not Severity.INFO] == [], issues
    plot, issues = charts.apply_chart_template(
        doc, example, author="lib_01jaf3q2x8m9v7k5h4g3f2e1d0-r1"
    )
    assert plot is not None, issues
    assert plot.id == "lib.fit-with-band"
