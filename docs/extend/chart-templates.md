---
description: Give a platform chart your own look with a chart template - a Vega-Lite restyle of the data of an existing plot recipe, previewed live and shown on run and model pages.
---

# Chart templates <span class="mapi-badge">v0.18</span>

A **chart template** draws the data of a platform chart in your own way. You keep the numbers
that a plot recipe already computes (the fit, the bands, the actual values) and change how
they look: small multiples instead of one panel, a line instead of bars, your titles and
your caption.

Chart templates show on run and model pages under **Your charts**, on the real data of each
fit. They are for your team: share links, client portals and dashboard pictures never show
them.

## Make one

- **Catalog › Plots:** on a recipe card, **Make a chart template**.
- **Make your own › Charts › Make a chart template:** pick the source recipe.

Then start from a starter layout, pick what goes on each axis, and give it a title and a
caption. The preview draws your template on the recipe's catalog example a quarter of a second
after you stop typing (`POST /v1/library/charts:preview`: pure JSON work, no run).
**Advanced: Vega-Lite** opens the full spec for the parts that the channel pickers do not
cover.

## The document

```yaml
--8<-- "extend_chart_template.yaml"
```

- `source_recipe` names a plot recipe and a version range. Its stored plot gives the data.
- `plot` is a Vega-Lite spec. It reads data **only by name** (`data: {name: points}`), from the
  datasets of the source recipe's plot. It cannot load a URL, embed data or add a link.
- `bindings` (optional) fill role and dim placeholders from the model's schema when the
  template is applied to a real model version.
- `caption_plain` is the text under the chart. Write it for the person who reads the chart.

## Check and apply

This is what happens when you preview and when a run page draws the chart
(`mapi_plots.chart_templates`):

```python
--8<-- "extend_library.py:chart"
```

- `check_chart_template` refuses a dataset name that the source recipe does not have and, for
  the recipes with published columns, a column that its dataset does not have. Each issue
  points at the field in your spec, for example `/plot/spec/encoding/x/field`.
- `apply_chart_template` swaps the spec, keeps the datasets byte for byte, and runs the plot
  sanitizer. The result has the id `lib.<name>` and `provenance.author` set to the item and
  revision. That author is what keeps the chart out of shared pages.

The columns of four recipes are published for the pickers and the checks:
`core.fit_vs_actual`, `core.ppc_fan`, `core.residuals_over_time` and `mmm.response_curves`
(`mapi_plots.templates.STARTERS`). For other recipes, the platform checks the dataset names
and the sanitizer still runs.

!!! warning "What a template cannot do"
    A template cannot read data from a URL or carry its own values, and `href` links are
    refused. The plot sanitizer then checks the result like every platform chart, and the
    chart renders in the sandboxed plot frame. See
    [Plots and the sanitizer](../reference/plots.md).

## Share it

Chart templates follow the same rules as [model recipes](your-model.md#share-a-recipe). A
model recipe can name chart templates (`charts:`); the platform checks that each one is in
the library when the recipe is used.

## What next

- Need a chart whose numbers do not exist yet? Write a plot recipe in Python:
  [Develop a plot recipe](develop/plot-recipes.md).
