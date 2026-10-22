"""v0.18 chart templates (WP2): restyle a platform plot's data, never its data path."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from mapi_plots.chart_templates import (
    apply_chart_template,
    chart_id,
    check_chart_template,
    known_columns,
)
from mapi_plots.recipes import get_recipe
from mapi_plots.templates import STARTERS
from mapi_spec.errors import ErrorCode, Issue, Severity
from mapi_spec.library import ChartTemplateDoc, parse_library_doc
from mapi_spec.plots import PlotSpec
from mapi_spec.schema import DataSchema
from mapi_spec.sharing import embeddable

pytestmark = pytest.mark.unit

AUTHOR = "lib_01jaf3q2x8m9v7k5h4g3f2e1d0-r3"
POINTS: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ChartTemplate",
    "metadata": {"name": "fit-dots", "title": "Fit as dots"},
    "source_recipe": "core.fit_vs_actual@^1",
    "plot": {
        "data": {"name": "points"},
        "mark": "point",
        "encoding": {
            "x": {"field": "observed", "type": "quantitative"},
            "y": {"field": "point", "type": "quantitative"},
            "color": {"field": "group", "type": "nominal"},
        },
    },
    "caption_plain": "Dots near the diagonal are good predictions.",
}


def _doc(raw: dict[str, Any] | None = None, **plot: Any) -> ChartTemplateDoc:
    data = copy.deepcopy(raw or POINTS)
    if plot:
        data["plot"] = plot
    doc = parse_library_doc(data)
    assert isinstance(doc, ChartTemplateDoc)
    return doc


@pytest.fixture(scope="module")
def example() -> PlotSpec:
    ex = get_recipe("core.fit_vs_actual").example_spec()
    assert ex is not None
    return ex


@pytest.fixture(scope="module")
def stored(example: PlotSpec) -> PlotSpec:
    """A stored fit plot: the same datasets as data_ref files of a model version."""
    refs = {
        name: {
            "kind": "data_ref",
            "path": f"ui/plotdata/fit_vs_actual_{name}.arrow",
            "size": 1234,
            "crc32c": "AAAAAA==",
        }
        for name in example.datasets
    }
    return PlotSpec.model_validate(
        {**example.model_dump(mode="json", by_alias=True), "datasets": refs}
    )


def _errors(issues: list[Issue]) -> list[tuple[str, str]]:
    return [(i.pointer, i.message) for i in issues if i.severity is Severity.ERROR]


@pytest.mark.parametrize(
    ("recipe_id", "name"),
    [(rid, s["name"]) for rid, entry in STARTERS.items() for s in entry["starters"]],
)
def test_every_starter_checks_clean_and_applies(recipe_id: str, name: str) -> None:
    ex = get_recipe(recipe_id).example_spec()
    assert ex is not None
    starter = next(s for s in STARTERS[recipe_id]["starters"] if s["name"] == name)
    doc = _doc(
        {
            **POINTS,
            "metadata": {"name": name.replace("_", "-"), "title": starter["title"]},
            "source_recipe": f"{recipe_id}@^1",
            "plot": starter["spec"],
        }
    )
    assert _errors(check_chart_template(doc, ex)) == []
    plot, issues = apply_chart_template(doc, ex, author=AUTHOR)
    assert plot is not None, _errors(issues)
    assert plot.datasets == ex.datasets


def test_apply_swaps_the_spec_and_keeps_the_datasets(stored: PlotSpec) -> None:
    doc = _doc()
    plot, issues = apply_chart_template(doc, stored, author=AUTHOR)
    assert plot is not None, _errors(issues)
    assert plot.id == chart_id(doc) == "lib.fit-dots"
    assert plot.kind == "vega-lite"
    assert plot.spec["mark"] == "point"
    assert plot.spec["$schema"]  # pinned by the sanitizer
    assert json.dumps(plot.model_dump(mode="json")["datasets"], sort_keys=True) == json.dumps(
        stored.model_dump(mode="json")["datasets"], sort_keys=True
    )
    assert plot.provenance.author == AUTHOR
    assert plot.provenance.recipe is None
    assert plot.provenance.generated_at == stored.provenance.generated_at
    assert plot.title == "Fit as dots"
    assert plot.caption_plain == "Dots near the diagonal are good predictions."
    assert plot.bindings is None
    # what the sanitizer changed is reported in plain words, pointing into the template
    assert all(i.severity is Severity.INFO for i in issues)
    assert "/plot/$schema" in [i.pointer for i in issues]


def test_authored_charts_are_never_embeddable(stored: PlotSpec) -> None:
    plot, _ = apply_chart_template(_doc(), stored, author=AUTHOR)
    assert plot is not None
    assert not embeddable("model_plot", plot.provenance.source, plot.provenance.recipe)
    assert embeddable("model_plot", stored.provenance.source, stored.provenance.recipe)


def test_preview_on_the_catalog_example_stays_inline(example: PlotSpec) -> None:
    plot, _ = apply_chart_template(_doc(), example, author=AUTHOR)
    assert plot is not None
    assert {d.kind for d in plot.datasets.values()} == {"inline_arrow"}
    assert plot.datasets == example.datasets


@pytest.mark.parametrize(
    ("plot", "pointer", "message"),
    [
        (
            {"data": {"name": "fan"}, "mark": "point"},
            "/plot/data/name",
            "The source plot has no dataset 'fan'.",
        ),
        ({"mark": "point"}, "/plot/data", "The chart reads no dataset."),
        (
            {
                "data": {"name": "points"},
                "mark": "point",
                "encoding": {"x": {"field": "revenue", "type": "quantitative"}},
            },
            "/plot/encoding/x/field",
            "The dataset points has no column 'revenue'.",
        ),
        (
            {
                "data": {"name": "points"},
                "mark": "point",
                "encoding": {"x": {"field": "v", "type": "quantitative"}},
            },
            "/plot/encoding/x/field",
            "The dataset points has no column 'v'.",
        ),
        (
            {
                "data": {"name": "points"},
                "transform": [
                    {
                        "aggregate": [{"op": "sum", "field": "pt", "as": "total"}],
                        "groupby": ["group"],
                    }
                ],
                "mark": "bar",
            },
            "/plot/transform/0/aggregate/0/field",
            "The dataset points has no column 'pt'.",
        ),
        (
            {"data": {"values": [{"x": 1}]}, "mark": "point"},
            "/plot/data",
            'A chart template reads data only by dataset name: {"name": "<dataset>"}.',
        ),
        (
            {"data": {"url": "https://evil.example/x.csv"}, "mark": "point"},
            "/plot/data",
            'A chart template reads data only by dataset name: {"name": "<dataset>"}.',
        ),
        (
            {"data": {"name": "points"}, "datasets": {"points": [{"x": 1}]}, "mark": "point"},
            "/plot/datasets",
            "A chart template cannot carry its own data. It reads the datasets of the source plot.",
        ),
    ],
)
def test_structure_refusals(
    example: PlotSpec, plot: dict[str, Any], pointer: str, message: str
) -> None:
    doc = _doc(**plot)
    assert (pointer, message) in _errors(check_chart_template(doc, example))
    out, issues = apply_chart_template(doc, example, author=AUTHOR)
    assert out is None
    assert (pointer, message) in _errors(issues)
    assert all(i.code is ErrorCode.LIBRARY_INVALID for i in issues if i.is_error)


def test_known_columns_layers_and_transforms(example: PlotSpec) -> None:
    doc = _doc(
        layer=[
            {
                "data": {"name": "identity"},
                "mark": "rule",
                "encoding": {"x": {"field": "v", "type": "quantitative"}},
            },
            {
                "data": {"name": "points"},
                "transform": [
                    {"calculate": "datum.point - datum.observed", "as": "gap"},
                    {"fold": ["point", "observed"]},
                ],
                "mark": "point",
                "encoding": {
                    "x": {"field": "gap", "type": "quantitative"},
                    "y": {"field": "value", "type": "quantitative"},
                    "color": {"field": "key", "type": "nominal"},
                    "tooltip": [{"field": "x", "type": "temporal"}],
                },
            },
        ],
    )
    assert _errors(check_chart_template(doc, example)) == []
    assert known_columns("core.fit_vs_actual") == {
        "points": ["x", "group", "point", "observed"],
        "identity": ["v"],
    }
    assert known_columns("acme.weekly") is None


@pytest.mark.parametrize(
    ("plot", "pointer"),
    [
        (
            {
                "data": {"name": "points"},
                "mark": "point",
                "usermeta": {"embedOptions": {"actions": True}},
            },
            "/plot/usermeta",
        ),
        (
            {"data": {"name": "points"}, "mark": "point", "usermeta": {"note": "x"}},
            "/plot/usermeta",
        ),
        (
            {
                "data": {"name": "points"},
                "mark": "point",
                "encoding": {"href": {"field": "group", "type": "nominal"}},
            },
            "/plot/encoding/href",
        ),
        ({"data": {"name": "points"}, "mark": "image"}, "/plot/mark"),
        (
            {"data": {"name": "points"}, "mark": "point", "loader": {"baseURL": "https://x"}},
            "/plot",
        ),
    ],
)
def test_the_sanitizer_refuses_unsafe_specs(
    example: PlotSpec, plot: dict[str, Any], pointer: str
) -> None:
    doc = _doc(**plot)
    out, issues = apply_chart_template(doc, example, author=AUTHOR)
    assert out is None
    errors = [i for i in issues if i.is_error]
    assert errors and errors[0].pointer.startswith(pointer), _errors(issues)
    assert any(i.pointer.startswith(pointer) for i in check_chart_template(doc, example))


def test_the_base_must_come_from_the_source_recipe(example: PlotSpec) -> None:
    fan = get_recipe("core.ppc_fan").example_spec()
    assert fan is not None
    _, issues = apply_chart_template(_doc(), fan, author=AUTHOR)
    assert _errors(issues)[0] == (
        "/source_recipe",
        "The plot is made by 'core.ppc_fan', not by 'core.fit_vs_actual'.",
    )
    newer = _doc({**POINTS, "source_recipe": "core.fit_vs_actual@^2"})
    _, issues = apply_chart_template(newer, example, author=AUTHOR)
    assert _errors(issues)[0] == (
        "/source_recipe",
        "Version 1.0.0 of 'core.fit_vs_actual' is not in the range '^2'.",
    )
    authored, _ = apply_chart_template(_doc(), example, author=AUTHOR)
    assert authored is not None
    _, issues = apply_chart_template(_doc(), authored, author=AUTHOR)
    assert _errors(issues) == [("/source_recipe", "The plot is not made by a platform recipe.")]


BOUND: dict[str, Any] = {
    **POINTS,
    "metadata": {"name": "bound", "title": "{{y.title}} against the fit"},
    "plot": {
        "data": {"name": "points"},
        "mark": "point",
        "encoding": {
            "x": {"field": "observed", "type": "quantitative", "title": "{{y.title}}"},
            "y": {"field": "point", "type": "quantitative"},
            "facet": {"$bind": "facet"},
        },
    },
    "bindings": {"y": "role:target", "facet": "dim:geo"},
}
SCHEMA = DataSchema.model_validate(
    {
        "dims": {"week": {"kind": "time", "policy": "extend"}, "geo": {"kind": "categorical"}},
        "vars": {
            "revenue": {
                "dims": ["week", "geo"],
                "dtype": "float64",
                "role": "target",
                "units": "GBP",
            }
        },
    }
)


def test_bindings_apply_on_a_real_model(stored: PlotSpec) -> None:
    doc = _doc(BOUND)
    plot, issues = apply_chart_template(doc, stored, schema=SCHEMA, author=AUTHOR)
    assert plot is not None, _errors(issues)
    assert plot.title == "revenue (GBP) against the fit"
    enc = plot.spec["encoding"]
    assert isinstance(enc, dict)
    assert enc["x"] == {"field": "observed", "type": "quantitative", "title": "revenue (GBP)"}
    assert enc["facet"] == {"field": "geo", "type": "nominal", "title": "geo"}
    # without a facet dim the optional channel disappears
    flat = DataSchema.model_validate(
        {
            "dims": {"week": {"kind": "time", "policy": "extend"}},
            "vars": {"revenue": {"dims": ["week"], "dtype": "float64", "role": "target"}},
        }
    )
    plot, _ = apply_chart_template(doc, stored, schema=flat, author=AUTHOR)
    assert plot is not None
    enc = plot.spec["encoding"]
    assert isinstance(enc, dict) and "facet" not in enc
    # a required channel that does not resolve is refused
    no_target = DataSchema.model_validate(
        {
            "dims": {"week": {"kind": "time", "policy": "extend"}},
            "vars": {"revenue": {"dims": ["week"], "dtype": "float64"}},
        }
    )
    plot, issues = apply_chart_template(doc, stored, schema=no_target, author=AUTHOR)
    assert plot is None
    assert _errors(issues) == [
        ("/bindings/y", "binding y='role:target': no variable has role 'target'")
    ]


def test_bindings_are_left_empty_in_a_preview(example: PlotSpec) -> None:
    plot, issues = apply_chart_template(_doc(BOUND), example, author=AUTHOR)
    assert plot is not None, _errors(issues)
    assert (
        "/bindings",
        "Role bindings apply on a real model. The preview leaves them empty.",
    ) in [(i.pointer, i.message) for i in issues if i.severity is Severity.INFO]
    enc = plot.spec["encoding"]
    assert isinstance(enc, dict) and "facet" not in enc
    assert plot.title == " against the fit"
