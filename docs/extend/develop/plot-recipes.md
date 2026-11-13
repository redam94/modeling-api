---
description: Write a plot recipe plugin for modeling-api - an Altair (Vega-Lite) or Plotly chart with a settings form and a catalog example, registered through mapi.plots and checked by check_recipe.
---

# Develop a plot recipe

A plot recipe is a function that turns a fitted model's outputs into a chart: an Altair
(Vega-Lite) or Plotly figure, the tables it reads by name, and a caption in plain words. The
platform sanitizes the result and draws it in a sandboxed frame. Start from the scaffold:

```bash
mapi plugin init acme --kind recipe
```

## The scaffold

`src/acme/plots.py` adds a model output over every group and draws one bar per week:

```python
--8<-- "extend_pkg/src/acme/plots.py"
```

What each part does:

- `WeeklyTotalSettings` is the settings form. The **Try it** playground in **Catalog ›
  Plots** renders it from this schema. `title`, `width` and `height` come from
  `RecipeSettings`.
- `_example` returns synthetic input for the catalog example. The catalog card, the docs and
  the playground draw it, so a recipe without an example has no picture.
- `named("totals")` makes the chart read the table `totals` by name. The table goes in
  `Figure.datasets`; the platform ships it as Arrow, never inside the spec.
- Colours come from `mapi_plots.palette` (`ESTIMATE`, `BAND`, `OBSERVED`, ...). The plot
  sandbox swaps each one for its dark-mode variant. A colour that is not in the palette is a
  warning in the kit.
- `pack_label` names your pack on the catalog card.
- The recipe has no `version=` argument, so it is `1.0.0`. A spec names it as
  `acme.weekly_total@^1`.

More about recipes, Plotly figures and role-bound templates:
[Plots, recipes and templates](../../guides/plots.md) and
[Plots and the sanitizer](../../reference/plots.md).

## The entry point

```toml
[project.entry-points."mapi.plots"]
acme = "acme.plots:RECIPES"
```

## The contract kit

```python
--8<-- "extend_pkg/tests/test_recipe.py"
```

`check_recipe` checks the catalog entry and the namespace, the About card, the settings
schema (safe, at most 16 KiB, defaults valid, unknown keys refused) and the catalog example:
it renders, its datasets are inline, the sanitizer accepts it unchanged and it has a caption.

## Use it

On a model, drawn after every fit:

```yaml
outputs:
  plots: [acme.weekly_total@^1]
```

In a workflow, with a `plot.render` node. Once installed, the recipe has a card with its
example in **Catalog › Plots**, and **Try it** opens the playground. Changes to the title,
width and height show at once. Other settings draw the chart again in a run on the server:
that takes some seconds (about ten on a local stack), and the app shows that it is working.
A second request with the same settings comes from a cache. Analysts can restyle your
recipe's data with a [chart template](../chart-templates.md).
