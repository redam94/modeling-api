"""The four library kinds, compiled the way the server compiles them (docs/extend/*.md).

Each section loads one document from docs/snippets/extend_*.yaml and runs the pure
``mapi_spec``/``mapi_plots`` code that the control plane runs when you save or use an item.
Nothing here needs a server; ``make docs-test`` runs the file top to bottom.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --8<-- [start:load]
from mapi_sdk import default_registry
from mapi_spec.library import parse_library_text

catalog = default_registry().catalog()  # what `describe` publishes for this checkout


def load(name: str):  # noqa: ANN201 - a library document of any kind
    return parse_library_text((HERE / name).read_text("utf-8"))
# --8<-- [end:load]


# --8<-- [start:schema]
from mapi_spec.schema import DataSchema

# the dataset you pick when you use the recipe: weekly sales of each store
schema = DataSchema.model_validate(
    {
        "dims": {
            "week": {"kind": "time", "freq": "W-SUN"},
            "store": {"kind": "categorical"},
        },
        "vars": {
            "sales": {"dims": ["week", "store"], "dtype": "float64", "role": "target"},
            "price": {"dims": ["week", "store"], "dtype": "float64"},
            "promo": {"dims": ["week", "store"], "dtype": "float64"},
        },
    }
)
# --8<-- [end:schema]

# --8<-- [start:model]
from mapi_spec.model_recipes import compile_model_recipe

recipe = load("extend_model_recipe.yaml")
compiled = compile_model_recipe(
    recipe, catalog, schema=schema, item_ref="lib_01jaf3q2x8m9v7k5h4g3f2e1d0-r1"
)
assert compiled.ok, [(i.pointer, i.message) for i in compiled.issues]
fragment = compiled.spec_fragment()
print(fragment["model"]["use"])  # tabular.declarative_glm@^1
print([s["use"] for s in fragment["pipeline"]])  # shape to long rows, then the transforms
print(fragment["metadata"]["labels"])  # {'mapi.library/model': 'lib_...-r1'}
# --8<-- [end:model]
assert fragment["model"]["use"] == "tabular.declarative_glm@^1"
assert fragment["model"]["settings"]["terms"]["price"]["sign"] == "negative"

# --8<-- [start:preset]
from mapi_spec.model_recipes import exposed_settings_schema

preset = load("extend_model_preset.yaml")
form = exposed_settings_schema(preset, catalog)  # the "Use" form: only the exposed keys
assert form is not None
print(sorted(form["properties"]))  # ['damped_trend', 'seasonal_periods']
used = compile_model_recipe(preset, catalog, exposed={"seasonal_periods": 13})
assert used.ok, [(i.pointer, i.message) for i in used.issues]
print(used.model.use, used.model.settings["seasonal_periods"])  # ts.ets@^1 13
# --8<-- [end:preset]
assert used.model.settings["trend"] == "add"

# --8<-- [start:prep]
from mapi_spec.prep_presets import apply_prep_preset

prep = load("extend_prep_preset.yaml")
steps, issues = apply_prep_preset(
    prep, {"gaps": ["price", "promo"]}, schema=schema, catalog=catalog
)
assert issues == [], [(i.pointer, i.message) for i in issues]
for step in steps:
    print(step.model_dump(mode="json", by_alias=True, exclude_defaults=True))
# {'id': 'p_fill', 'use': 'core.impute@^1', 'with': {'vars': ['price', 'promo'], ...}}
# {'id': 'p_promo_price', 'use': 'core.formula@^1', 'with': {'out': 'promo_price', ...}}
# --8<-- [end:prep]
assert [s.id for s in steps] == ["p_fill", "p_promo_price"]

# --8<-- [start:chart]
from mapi_plots.chart_templates import apply_chart_template, check_chart_template
from mapi_plots.recipes import get_recipe

chart = load("extend_chart_template.yaml")
example = get_recipe(chart.source_recipe.id).example_spec()  # the catalog example
assert example is not None
problems = [i for i in check_chart_template(chart, example) if i.severity != "info"]
assert problems == [], problems
plot, issues = apply_chart_template(chart, example, author="lib_01jaf3q2x8m9v7k5h4g3f2e1d0-r1")
assert plot is not None, issues
print(plot.id, plot.provenance.author)  # lib.fit-small-multiples lib_...-r1
print(sorted(plot.datasets) == sorted(example.datasets))  # True: the data is not touched
# --8<-- [end:chart]
assert plot.id == "lib.fit-small-multiples"

# --8<-- [start:workflow]
from mapi_spec.library import workflow_template_issues
from mapi_workflows.runtime.nodes import all_node_contracts

template = load("extend_workflow_template.yaml")
issues = workflow_template_issues(template, nodes=all_node_contracts())
print([f"{i.pointer}: {i.message}" for i in issues if i.severity == "error"])  # []
# --8<-- [end:workflow]
assert not [i for i in issues if i.severity == "error"], issues

print(json.dumps({"ok": True}))
