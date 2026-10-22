"""v0.18 library documents (WP2): parsing, caps, hashing, DTOs, untrusted text."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest
from mapi_spec._types import LabelKey, LabelValue
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.library import (
    LABEL_MODEL,
    LABEL_PREP,
    LABEL_TEMPLATE,
    LIBRARY_DOC_MAX_BYTES,
    PLOT_SPEC_MAX_BYTES,
    ChartPreview,
    ChartPreviewRequest,
    ChartTemplateDoc,
    LibraryCreate,
    LibraryItem,
    LibraryRevision,
    LibraryRevisionCreate,
    LibraryUseRequest,
    LibraryUseResult,
    LibraryValidateRequest,
    LibraryValidation,
    ModelRecipeDoc,
    PrepPresetDoc,
    WorkflowTemplateDoc,
    library_doc_hash,
    library_kind,
    library_label,
    parse_library_doc,
    parse_library_text,
    workflow_template_issues,
)
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from pydantic import BaseModel, TypeAdapter, ValidationError

pytestmark = pytest.mark.unit

ITEM = "lib_01jaf3q2x8m9v7k5h4g3f2e1d0"

RECIPE: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ModelRecipe",
    "metadata": {"name": "store-sales", "title": "Store sales with promo lift", "tags": ["retail"]},
    "base": "declarative",
    "declarative": {
        "outcome": {"var": "sales", "family": "negative_binomial"},
        "terms": [
            {
                "var": "price",
                "transform": [{"use": "log1p"}, {"use": "standardize", "with": {"over": ["obs"]}}],
                "prior": {"dist": "normal", "sigma": 0.5},
                "sign": "negative",
            },
            {"var": "promo", "prior": {"sigma": 1}, "sign": "positive", "varies_by": "store"},
        ],
        "groups": [{"dim": "store"}],
    },
    "plots": ["core.ppc"],
}
PRESET: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ModelRecipe",
    "metadata": {"name": "weekly-sarimax", "title": "Weekly SARIMAX"},
    "base": "ts.sarimax@^1",
    "settings": {"seasonal_order": [1, 0, 0, 52]},
    "exposed": ["order", "exog"],
}
PREP: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "PrepPreset",
    "metadata": {"name": "media-scale", "title": "Scale media"},
    "slots": {"media": {"kind": "var_list", "label": "Media", "role": "media"}},
    "steps": [
        {
            "id": "scale",
            "use": "core.max_abs_scale@^1",
            "with": {"vars": "$slot:media", "over": ["time"]},
        }
    ],
}
CHART: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ChartTemplate",
    "metadata": {"name": "fit-dots", "title": "Fit as dots"},
    "source_recipe": "core.fit_vs_actual@^1",
    "plot": {
        "data": {"name": "points"},
        "mark": "point",
        "encoding": {"x": {"field": "observed", "type": "quantitative"}},
    },
}
WORKFLOW: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "WorkflowTemplate",
    "metadata": {"name": "readiness", "title": "Readiness checks"},
    "document": WORKFLOW_TEMPLATES["ts_readiness_arimax"].document,
}


def _issues(doc: Any) -> list[tuple[str, str]]:
    with pytest.raises(MapiError) as info:
        parse_library_doc(doc)
    assert info.value.code is ErrorCode.LIBRARY_INVALID
    return [(i.pointer, i.message) for i in info.value.issues]


@pytest.mark.parametrize(
    ("doc", "cls", "kind"),
    [
        (RECIPE, ModelRecipeDoc, "model_recipe"),
        (PRESET, ModelRecipeDoc, "model_recipe"),
        (PREP, PrepPresetDoc, "prep_preset"),
        (CHART, ChartTemplateDoc, "chart_template"),
        (WORKFLOW, WorkflowTemplateDoc, "workflow_template"),
    ],
)
def test_every_kind_parses_and_round_trips(doc: dict[str, Any], cls: type, kind: str) -> None:
    parsed = parse_library_doc(doc)
    assert isinstance(parsed, cls)
    assert library_kind(parsed) == kind
    dumped = parsed.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    again = parse_library_doc(dumped)
    assert again == parsed
    assert library_doc_hash(again) == library_doc_hash(parsed)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", library_doc_hash(parsed))


def test_hash_ignores_key_order_and_spelt_out_defaults() -> None:
    a = parse_library_doc(RECIPE)
    spelt = copy.deepcopy(RECIPE)
    spelt["declarative"]["terms"][1]["prior"] = {"dist": "normal", "mu": 0.0, "sigma": 1.0}
    spelt["metadata"] = dict(reversed(list(spelt["metadata"].items())))
    assert library_doc_hash(parse_library_doc(spelt)) == library_doc_hash(a)
    changed = copy.deepcopy(RECIPE)
    changed["declarative"]["terms"][0]["prior"]["sigma"] = 0.6
    assert library_doc_hash(parse_library_doc(changed)) != library_doc_hash(a)


def test_preset_base_is_a_use_ref_and_declarative_the_default() -> None:
    preset = parse_library_doc(PRESET)
    assert isinstance(preset, ModelRecipeDoc)
    assert not preset.is_declarative
    assert str(preset.base) == "ts.sarimax@^1"
    minimal = {k: v for k, v in RECIPE.items() if k != "base"}
    doc = parse_library_doc(minimal)
    assert isinstance(doc, ModelRecipeDoc) and doc.is_declarative
    assert _issues({**PRESET, "base": "not a ref"})[0][0] == "/base"


@pytest.mark.parametrize(
    ("mutate", "pointer"),
    [
        (
            lambda d: d["declarative"]["terms"][1]["prior"].update(sigma=0),
            "/declarative/terms/1/prior/sigma",
        ),
        (
            lambda d: d["declarative"]["terms"][1]["prior"].update(sigma=2e6),
            "/declarative/terms/1/prior/sigma",
        ),
        (
            lambda d: d["declarative"]["terms"][0]["prior"].update(dist="cauchy"),
            "/declarative/terms/0/prior/dist",
        ),
        (
            lambda d: d["declarative"]["terms"][0]["prior"].update(nu=0),
            "/declarative/terms/0/prior/nu",
        ),
        (lambda d: d["declarative"]["terms"][0].update(sign="up"), "/declarative/terms/0/sign"),
        (
            lambda d: d["declarative"]["terms"][0]["transform"][0].update(use="exp"),
            "/declarative/terms/0/transform/0/use",
        ),
        (
            lambda d: d["declarative"]["outcome"].update(family="tweedie"),
            "/declarative/outcome/family",
        ),
        (
            lambda d: d["declarative"]["outcome"].update(n_categories=1),
            "/declarative/outcome/n_categories",
        ),
        (
            lambda d: d["declarative"]["terms"][0].update(formula="price**2"),
            "/declarative/terms/0/formula",
        ),
        (lambda d: d["declarative"].update(terms=[]), "/declarative/terms"),
        (lambda d: d["metadata"].update(name="Has Spaces"), "/metadata/name"),
        (lambda d: d["metadata"].update(tags=["a", "a"]), "/metadata/tags"),
        (lambda d: d.update(prep={"item": "lib_nope"}), "/prep/item"),
        (lambda d: d.update(apiVersion="mapi/v0"), "/apiVersion"),
    ],
)
def test_refusals_point_into_the_document(mutate: Any, pointer: str) -> None:
    doc = copy.deepcopy(RECIPE)
    mutate(doc)
    pointers = [p for p, _ in _issues(doc)]
    assert pointer in pointers, pointers


def test_unknown_kind_and_non_mapping() -> None:
    assert _issues({**RECIPE, "kind": "ModelSpec"}) == [
        ("/kind", "kind must be one of ModelRecipe, PrepPreset, ChartTemplate, WorkflowTemplate")
    ]
    assert _issues(["not", "a", "mapping"])[0][0] == ""


@pytest.mark.parametrize(
    ("field", "n"),
    [("terms", 65), ("interactions", 17), ("groups", 4)],
)
def test_declarative_caps(field: str, n: int) -> None:
    doc = copy.deepcopy(RECIPE)
    if field == "terms":
        doc["declarative"]["terms"] = [{"var": f"x{i}", "prior": {}} for i in range(n)]
    elif field == "interactions":
        doc["declarative"]["interactions"] = [{"a": "price", "b": "promo"}] * n
    else:
        doc["declarative"]["groups"] = [{"dim": f"g{i}"} for i in range(n)]
    assert f"/declarative/{field}" in [p for p, _ in _issues(doc)]


def test_other_caps() -> None:
    assert "/exposed" in [
        p for p, _ in _issues({**PRESET, "exposed": [f"k{i}" for i in range(13)]})
    ]
    assert "/metadata/tags" in [
        p
        for p, _ in _issues(
            {**PRESET, "metadata": {**PRESET["metadata"], "tags": [f"t{i}" for i in range(9)]}}
        )
    ]
    slots = {f"s{i}": {"kind": "var", "label": "x"} for i in range(17)}
    assert "/slots" in [p for p, _ in _issues({**PREP, "slots": slots})]
    assert "/steps" in [p for p, _ in _issues({**PREP, "steps": []})]
    assert "/declarative/terms/0/transform" in [
        p for p, _ in _issues(_with_transforms([{"use": "log1p"}] * 4))
    ]
    dup = {**PREP, "steps": [PREP["steps"][0], PREP["steps"][0]]}
    assert any("duplicate step ids" in m for _, m in _issues(dup))


def _with_transforms(ts: list[dict[str, Any]]) -> dict[str, Any]:
    doc = copy.deepcopy(RECIPE)
    doc["declarative"]["terms"][0]["transform"] = ts
    return doc


def test_document_size_caps() -> None:
    big = copy.deepcopy(WORKFLOW)
    big["metadata"] = {**big["metadata"], "summary": "x" * 300}
    big["document"] = {**big["document"], "padding": "x" * LIBRARY_DOC_MAX_BYTES}
    issues = _issues(big)
    assert issues[0][0] == "" and "limit is 262144" in issues[0][1]
    plot = {**CHART["plot"], "description": "x" * PLOT_SPEC_MAX_BYTES}
    assert "/plot" in [p for p, _ in _issues({**CHART, "plot": plot})]


def test_text_bodies_use_the_budgeted_parser() -> None:
    text = (
        "apiVersion: mapi/v1\nkind: PrepPreset\nmetadata: {name: p, title: P}\n"
        "steps:\n  - {id: a, use: core.log1p@^1, with: {vars: [x]}}\n"
    )
    doc = parse_library_text(text)
    assert isinstance(doc, PrepPresetDoc)
    assert parse_library_text(json.dumps(PREP)) == parse_library_doc(PREP)
    with pytest.raises(MapiError) as info:  # YAML anchors and aliases are refused
        parse_library_text("a: &x [1]\nb: *x\n")
    assert info.value.code is ErrorCode.SPEC_PARSE_ERROR
    with pytest.raises(MapiError) as info:
        parse_library_text("x: " + "y" * (LIBRARY_DOC_MAX_BYTES + 1))
    assert info.value.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_html_in_titles_is_kept_verbatim() -> None:
    hostile = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    doc = parse_library_doc(
        {
            **RECIPE,
            "metadata": {
                **RECIPE["metadata"],
                "title": hostile[:120],
                "summary": hostile,
                "about": {"title": hostile[:120], "summary_plain": hostile},
            },
        }
    )
    assert doc.metadata.title == hostile[:120]
    assert doc.metadata.summary == hostile
    item = LibraryItem.model_validate(
        {**LibraryItem.model_json_schema()["examples"][0], "title": hostile[:120]}
    )
    assert json.loads(item.model_dump_json())["title"] == hostile[:120]


def test_labels_fit_the_label_types() -> None:
    keys = TypeAdapter(LabelKey)
    values = TypeAdapter(LabelValue)
    for k in (LABEL_MODEL, LABEL_PREP, LABEL_TEMPLATE):
        keys.validate_python(k)
    assert library_label(ITEM, 3) == f"{ITEM}-r3"
    assert library_label(ITEM) == ITEM
    values.validate_python(library_label(ITEM, 200))


def test_library_ref() -> None:
    ok = parse_library_doc({**RECIPE, "prep": {"item": ITEM, "revision": 2}})
    assert isinstance(ok, ModelRecipeDoc) and ok.prep is not None and ok.prep.revision == 2
    latest = parse_library_doc({**RECIPE, "prep": {"item": ITEM}})
    assert isinstance(latest, ModelRecipeDoc) and latest.prep is not None
    assert latest.prep.revision == "latest"
    assert "/prep/revision" in [
        p for p, _ in _issues({**RECIPE, "prep": {"item": ITEM, "revision": 0}})
    ]


DTOS: list[type[BaseModel]] = [
    LibraryItem,
    LibraryRevision,
    LibraryCreate,
    LibraryRevisionCreate,
    LibraryValidateRequest,
    LibraryValidation,
    LibraryUseRequest,
    LibraryUseResult,
    ChartPreviewRequest,
    ChartPreview,
]


@pytest.mark.parametrize("cls", DTOS, ids=lambda c: c.__name__)
def test_dto_examples_validate_and_round_trip(cls: type[BaseModel]) -> None:
    examples = cls.model_json_schema().get("examples")
    assert examples, f"{cls.__name__} has no examples"
    for ex in examples:
        obj = cls.model_validate(ex)
        assert cls.model_validate_json(obj.model_dump_json(by_alias=True)) == obj


def test_example_documents_parse() -> None:
    for cls in (LibraryCreate, LibraryRevisionCreate, LibraryValidateRequest, ChartPreviewRequest):
        for ex in cls.model_json_schema()["examples"]:
            parse_library_doc(ex["document"])
    rev = LibraryRevision.model_json_schema()["examples"][0]
    parse_library_doc(rev["document"])


def test_document_or_text_exactly_one() -> None:
    with pytest.raises(ValidationError):
        LibraryCreate.model_validate({})
    with pytest.raises(ValidationError):
        LibraryCreate.model_validate({"document": RECIPE, "text": "x"})
    assert LibraryCreate.model_validate({"text": "kind: PrepPreset"}).scope == "project"
    with pytest.raises(ValidationError):
        LibraryCreate.model_validate({"text": "x" * (LIBRARY_DOC_MAX_BYTES + 1)})
    with pytest.raises(ValidationError):
        LibraryValidateRequest.model_validate({"document": RECIPE, "dataset": "s3://bucket/x"})


def test_workflow_template_issues_point_under_document() -> None:
    doc = parse_library_doc(WORKFLOW)
    assert isinstance(doc, WorkflowTemplateDoc)
    assert workflow_template_issues(doc) == []
    broken = copy.deepcopy(WORKFLOW)
    broken["document"]["nodes"][0]["use"] = "test.acme_missing"
    bad = parse_library_doc(broken)
    assert isinstance(bad, WorkflowTemplateDoc)
    issues = workflow_template_issues(bad)
    assert [i.pointer for i in issues if i.code is ErrorCode.NODE_UNKNOWN] == [
        "/document/nodes/0/use"
    ]


def test_non_json_values_are_refused() -> None:
    doc = copy.deepcopy(RECIPE)
    doc["declarative"]["terms"][0]["prior"]["mu"] = float("nan")
    assert _issues(doc) == [("", "The document is not plain JSON.")]
