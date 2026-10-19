# mapi-plots

Plot recipes and the JSON-only PlotSpec sanitizer for modeling-api.

License: Apache License 2.0.

## Install

```bash
uv add mapi-plots              # sanitizer + role/dim templates: JSON only (control plane safe)
uv add "mapi-plots[recipes]"   # + recipe catalog, the mmm/ml/sci/net packs, Altair/Plotly helpers
```

## Sanitize any plot

```python
from mapi_plots import sanitize_plot, check_plot, SanitizeError

result = sanitize_plot(plot_json)  # PlotSpec | dict | str | bytes -> SanitizeResult
result.plot, result.changes  # sanitized PlotSpec, what was rewritten
check_plot(plot_json)  # every error and change, without raising
```

Vega-Lite is validated against the vendored v6.4.1 JSON Schema; Plotly figures against a
structure table derived from plotly.js 4.1.1's `plot-schema.json`. URL data, `href`, external
images, tile maps, prototype keys, script/CSS URLs and non-allow-listed Vega expressions are
rejected with typed errors (`PLOT_UNSAFE`, `SCHEMA_INVALID`, `PAYLOAD_TOO_LARGE`); Plotly HTML
is stripped to `<b> <i> <br> <sub> <sup>`. `scripts/vendor_schemas.py` regenerates the schemas.

## Recipes

```python
from mapi_plots.recipes import get_recipe

plot = get_recipe("core.ppc_fan").render(score_output_tree, facet="market")
plot.caption_plain  # plain-language reading with the numbers
```

The bundled recipes are in five packs. Each pack is registered under the `mapi.plots`
entry-point group (`core`, `mmm`, `ml`, `sci`, `net`):

| Pack | Recipes |
|---|---|
| `core.*` (Vega-Lite) | data overview, prior predictive, convergence, posterior forest, PPC, fit/forecast fan, fit vs actual, residuals, decomposition, model comparison, counterfactual, decision distribution, `core.pair` (corner plot with divergent draws), `core.ridge`, `core.rank_matrix`, `core.calibration` (PIT-ECDF or reliability diagram), `core.state_sequence` |
| `mmm.*` | response curves, marginal cost, budget plans, decomposition, prior beliefs |
| `ml.*` | `ml.feature_importance` |
| `sci.*` (Plotly) | `sci.field_map`, `sci.vector_field`, `sci.profile_bands`, `sci.ensemble`, `sci.area_map`, `sci.phylogeny`, `sci.rose`, `sci.image_residuals`, `sci.ppc_image_grid`, `sci.swatches`, `sci.power_spectrum`, `sci.point_pattern` |
| `net.*` (Plotly) | `net.graph` (a node-link drawing and an adjacency matrix of an edge variable `(..., node, node_)`) |

`get_recipe(id)` finds a recipe in all the packs. `mapi_plots.examples.example_inputs()` gives
synthetic inputs for every recipe.

## Helpers and templates

- `from_altair(chart, id=...)` / `from_plotly(fig, id=...)` turn charts into PlotSpecs with
  named Arrow datasets (Plotly arrays bound with `{"$ref": "trace0.y"}`).
- `apply_template` / `render_template` resolve role/dim-bound templates
  (`{x: "dim:time", y: "role:target", facet: "dim:geo"}`) against a `DataSchema`.

See `docs/guides/plots.md` and `docs/reference/plots.md`.
