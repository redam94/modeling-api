---
description: The public contract kits of modeling-api - check_model, check_step, check_recipe, check_node and check_template - that test a plugin against the rules the platform depends on.
---

# Contract kits

A contract kit runs your extension the way the platform will, and checks the rules that the
platform depends on. It tells you about a problem before anybody else runs your code. Every
scaffold has one kit test for each kind, marked `mapi_contract`; `mapi plugin test` runs
them, and `mapi plugin test --contract-only` runs only them.

| Kind | Kit | Import from | Returns |
|---|---|---|---|
| Model | `check_model(plugin, fixture, *, require_about=False, raise_on_failure=True, checks=None)` | `mapi_sdk.testing` | `ContractReport` |
| Step | `check_step(step, ds, settings, *, schema=None, require_about=False, raise_on_failure=True)` | `mapi_sdk.testing` | `ContractReport` |
| Plot recipe | `check_recipe(recipe, *, require_about=True, raise_on_failure=True)` | `mapi_plots.testing` | `RecipeReport` |
| Workflow node | `check_node(export, *, inputs, params=None, seed=0, raise_on_failure=True)` | `mapi_workflows.kit` | `ContractReport` |
| Workflow template | `check_template(t, *, nodes=None, warnings=False)` | `mapi_workflows.kit` | `list[Issue]` |

With `raise_on_failure=False`, a kit returns its report: `report.ok` and
`report.summary()`, one line for each check. Pass `require_about=True` for the model and step
kits, as the scaffold does: the catalog shows the About card, and the loader marks an
incomplete card with `about_incomplete`.

`mapi_workflows.testing` re-exports `check_node` and `check_template`, but it needs the
`testing` extra and imports the statsmodels registry. Import them from `mapi_workflows.kit`.

## Models

```python
--8<-- "extend_pkg/tests/test_model.py"
```

`ModelFixture` holds a small spec and data; `mapi_sdk.testing.toy` has panels and a registry
with the `toy.*` steps that a fixture spec may use. The checks: fit and score on the fixture,
bundle round trips (no pickle), scoring in another order, on a subset and with new levels,
typed errors for each dim policy, context-window equivalence, the evaluate path, output
units, aggregation legality, seed determinism and the About card. `checks=` runs a subset.

## Steps

```python
--8<-- "extend_pkg/tests/test_step.py"
```

The checks: a deterministic `fit`, a pure `transform` that never learns from its input, a
state that survives a bundle round trip, an output that matches the declared effect, an
absent target, and the About card.

## Plot recipes

```python
--8<-- "extend_pkg/tests/test_recipe.py"
```

The checks, each with its own name in the report: the catalog entry and the namespace, the
About card, the settings schema (safe, at most 16 KiB after sanitizing, defaults valid, an
unknown key refused), and the catalog example (it renders, its datasets are inline, the
sanitizer accepts it unchanged, it has a caption). A colour that is not in
`mapi_plots.palette` is a warning.

## Workflow nodes

```python
--8<-- "extend_pkg/tests/test_node.py"
```

The checks: the plugin rules of the contract (id, kind, sandbox, ports, parameter schema,
About card), one run on `inputs` with `params`, the outputs against their port types, the
result fields, the size of the result and, for a cacheable node, that two runs agree.

## Workflow templates

```python
--8<-- "extend_pkg/tests/test_template.py"
```

The checks: the id is in your namespace and is not a built-in template id, the About card,
and the document validated against `nodes` (the built-in contracts when you pass none). An
empty list means the template is valid.

## Run them in CI

```bash
mapi plugin test --contract-only
mapi --json plugin validate > report.json   # exit 0, 1 or 2; the report as JSON
```

`mapi plugin validate` runs the same checks as the loader of the runner, in a subprocess, on
the package's entry points. In v0.19, the same kits will run in the sandbox when a plugin is
uploaded.
