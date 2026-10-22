"""Part 2 of the extender tutorial (docs/tutorials/your-first-extensions.md): your plugin and
the no-code library together, before you install anything.

``docs/snippets/extend_pkg`` is the package that ``mapi plugin init acme --kind model --kind
step --kind recipe --kind node --kind template`` writes (a test keeps the two equal). This
file loads it by path, registers its model, step and recipe next to the built-ins, then
compiles library documents that use them: the same pure code the server runs when an
analyst saves or uses an item. ``make docs-test`` runs it top to bottom.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ----------------------------------------------------------------- part 1: no code, in the app
# --8<-- [start:sample]
from mapi_sdk import default_registry
from mapi_spec.guided import draft_schema
from mapi_spec.library import parse_library_text
from mapi_spec.samples import GEO_MMM_SAMPLE, sample_draft

# the schema of the sample project's dataset, as the app ingests it
draft, _ = sample_draft(GEO_MMM_SAMPLE, project="sample")
sample = draft_schema(draft.mapping, GEO_MMM_SAMPLE.answers.columns)
print(sorted(sample.dims), sorted(sample.vars))
# --8<-- [end:sample]

# --8<-- [start:part1-recipe]
from mapi_spec.model_recipes import compile_model_recipe

builtins = default_registry().catalog()
recipe = parse_library_text((HERE / "extend_tutorial_recipe.yaml").read_text("utf-8"))
compiled = compile_model_recipe(recipe, builtins, schema=sample)
assert compiled.ok, [(i.pointer, i.message) for i in compiled.issues]
print([s.id for s in compiled.pipeline])  # to long rows, then the transforms of each term
# --8<-- [end:part1-recipe]

# --8<-- [start:part1-prep]
from mapi_spec.prep_presets import apply_prep_preset

prep = parse_library_text((HERE / "extend_tutorial_prep.yaml").read_text("utf-8"))
spend = ["spend"]  # the value you pick for the slot: the columns to fill
steps, issues = apply_prep_preset(prep, {"spend": spend}, schema=sample, catalog=builtins)
assert issues == [], [(i.pointer, i.message) for i in issues]
print([s.id for s in steps])  # ['p_fill', 'p_total']
# --8<-- [end:part1-prep]

# ------------------------------------------------------------- part 2: your plugin, by path
SRC = str(HERE / "extend_pkg" / "src")  # in the tutorial: "acme/src", next to you
try:
    # --8<-- [start:registry]
    import sys

    sys.path.insert(0, SRC)  # SRC = "acme/src": not installed yet (step 8 does that), by path
    from acme.model import MODELS
    from acme.plots import RECIPES
    from acme.steps import STEPS

    registry = default_registry().copy()  # every built-in step, model and recipe
    registry.register([*MODELS, *STEPS, *RECIPES])
    catalog = registry.catalog()  # what `describe` publishes once acme is installed
    print([m.manifest.id for m in catalog.models if m.manifest.id.startswith("acme.")])
    # --8<-- [end:registry]

    # --8<-- [start:spec]
    from mapi_spec.loader import parse_text
    from mapi_spec.spec import ModelSpec
    from mapi_spec.validate import validate_spec

    spec = ModelSpec.model_validate(
        parse_text((HERE / "extend_tutorial_spec.yaml").read_text("utf-8"))
    )
    result = validate_spec(spec, catalog, sample)  # what `mapi check` and Build's Validate do
    print(result.ok, [i.message for i in result.errors])  # True []
    # --8<-- [end:spec]
    assert result.ok, result.issues

    # --8<-- [start:preset]
    from mapi_spec.library import parse_library_doc
    from mapi_spec.model_recipes import compile_model_recipe

    preset = parse_library_doc(
        {
            "apiVersion": "mapi/v1",
            "kind": "ModelRecipe",
            "metadata": {"name": "store-baseline", "title": "Store baseline"},
            "base": "acme.group_mean@^0.1",  # your model, as a team preset
            "settings": {"shrinkage": 0.2},
            "exposed": ["shrinkage"],
            "plots": ["acme.weekly_total@^1"],  # your recipe, drawn after every fit
        }
    )
    used = compile_model_recipe(preset, catalog, exposed={"shrinkage": 0.5})
    assert used.ok, [(i.pointer, i.message) for i in used.issues]
    print(used.spec_fragment()["model"])
    # {'use': 'acme.group_mean@^0.1', 'settings': {'shrinkage': 0.5}}
    # --8<-- [end:preset]
    assert used.model.settings == {"shrinkage": 0.5}

    # --8<-- [start:prep]
    from mapi_spec.prep_presets import apply_prep_preset

    prep = parse_library_doc(
        {
            "apiVersion": "mapi/v1",
            "kind": "PrepPreset",
            "metadata": {"name": "cap-then-log", "title": "Cap outliers, then log"},
            "slots": {"spiky": {"kind": "var_list", "label": "Spiky columns"}},
            "steps": [
                {"id": "cap", "use": "acme.winsorize@^0.1",  # your step
                 "with": {"vars": ["$slot:spiky"], "upper": 0.98}},
                {"id": "log", "use": "core.log1p@^1", "with": {"vars": ["$slot:spiky"]}},
            ],
        }
    )  # fmt: skip
    steps, issues = apply_prep_preset(prep, {"spiky": ["sales"]}, schema=None, catalog=catalog)
    assert issues == [], [(i.pointer, i.message) for i in issues]
    print([(s.id, str(s.use)) for s in steps])
    # [('p_cap', 'acme.winsorize@^0.1'), ('p_log', 'core.log1p@^1')]
    # --8<-- [end:prep]

    # --8<-- [start:chart]
    from mapi_plots.chart_templates import apply_chart_template

    weekly_total = registry.plot("acme.weekly_total")
    example = weekly_total.example_spec()  # your recipe's catalog example
    chart = parse_library_doc(
        {
            "apiVersion": "mapi/v1",
            "kind": "ChartTemplate",
            "metadata": {"name": "weekly-total-line", "title": "Weekly totals as a line"},
            "source_recipe": "acme.weekly_total@^1",
            "plot": {
                "data": {"name": "totals"},  # a dataset of the recipe's plot, by name
                "mark": {"type": "line", "point": True},
                "encoding": {
                    "x": {"field": "week", "type": "temporal", "title": "Week"},
                    "y": {"field": "total", "type": "quantitative", "title": "Total"},
                },
            },
            "caption_plain": "The total of every store, week by week.",
        }
    )
    plot, issues = apply_chart_template(chart, example, author="lib_01jaf3q2x8m9v7k5h4g3f2e1d0-r1")
    assert plot is not None, issues
    print(plot.id, [i.message for i in issues])  # lib.weekly-total-line ['added $schema']
    # --8<-- [end:chart]
    assert sorted(plot.datasets) == sorted(example.datasets)

    # --8<-- [start:workflow]
    from acme.nodes import ZERO_SHARE
    from acme.templates import WEEKLY_CHECK
    from mapi_spec.library import workflow_template_issues
    from mapi_workflows.kit import check_template
    from mapi_workflows.runtime.nodes import all_node_contracts

    nodes = all_node_contracts([ZERO_SHARE.contract])  # built-ins + your node
    assert check_template(WEEKLY_CHECK, nodes=nodes) == []  # your plugin template

    team = parse_library_doc(  # a team template that uses your node
        {
            "apiVersion": "mapi/v1",
            "kind": "WorkflowTemplate",
            "metadata": {"name": "zero-check", "title": "Too many zeros?"},
            "document": {
                "apiVersion": "mapi/v1",
                "kind": "Workflow",
                "metadata": {"name": "zero-check", "project": "default"},
                "inputs": {"data": {"type": "dataset"}},
                "nodes": [
                    {"id": "zeros", "use": "test.acme_zero_share",
                     "with": {"var": "sales", "threshold": 0.05},
                     "inputs": {"data": "inputs.data"}},
                ],
                "outputs": {"zeros": "zeros.result"},
            },
        }
    )  # fmt: skip
    errors = [i for i in workflow_template_issues(team, nodes=nodes) if i.severity == "error"]
    print(errors)  # []: the node is known, its parameters match its contract
    # --8<-- [end:workflow]
    assert errors == [], errors
    # without the plugin's contract, the same template is refused
    unknown = workflow_template_issues(team, nodes=all_node_contracts([]))
    assert any(i.severity == "error" for i in unknown)
finally:
    sys.path.remove(SRC)
    for name in [m for m in sys.modules if m == "acme" or m.startswith("acme.")]:
        del sys.modules[name]
