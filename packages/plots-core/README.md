# mapi-plots

Plot recipes and the JSON-only PlotSpec sanitizer for modeling-api.

License: Apache License 2.0.

## Install

```bash
uv add mapi-plots              # sanitizer + role/dim templates: JSON only (control plane safe)
uv add "mapi-plots[recipes]"   # + recipe catalog, the mmm/ml/sci/net/causal packs, Altair/Plotly helpers
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

The bundled recipes are in six packs. Each pack is registered under the `mapi.plots`
entry-point group (`core`, `mmm`, `ml`, `sci`, `net`, `causal`):

| Pack | Recipes |
|---|---|
| `core.*` (Vega-Lite) | data overview, prior predictive, convergence, posterior forest, PPC, fit/forecast fan, fit vs actual, residuals, decomposition, model comparison, counterfactual, decision distribution, `core.pair` (corner plot with divergent draws), `core.ridge`, `core.rank_matrix`, `core.calibration` (PIT-ECDF or reliability diagram, also from point probabilities), `core.state_sequence` (HMM hidden states, Markov regimes), `core.roc_pr` and `core.confusion` (binary and, from v0.14, multiclass classifiers), `core.loss_curve` (ADVI loss history and logged metrics), `core.loo_pit` (LOO-PIT and PSIS Pareto k), `core.energy` (sampler energy and BFMI), `core.prior_posterior` (prior against posterior, with their overlap) |
| `mmm.*` | response curves, marginal cost, budget plans, decomposition, prior beliefs; v0.12: `mmm.roi`, `mmm.adstock`, `mmm.waterfall`, `mmm.spend_effect`, `mmm.lift_tests`, `mmm.effectiveness`, `mmm.mediation` (`mmm.nested`), `mmm.cross_effects` (`mmm.multivariate`) |
| `ml.*` | `ml.feature_importance`, `ml.training_curves` (train and validation curves by label), `ml.confusion_epochs` (validation confusion matrices at up to 6 epochs), `ml.misclassified` (Plotly grid of the wrong validation images, inline pixels) |
| `sci.*` (Plotly) | `sci.field_map`, `sci.vector_field`, `sci.profile_bands`, `sci.ensemble`, `sci.area_map`, `sci.phylogeny`, `sci.rose`, `sci.image_residuals`, `sci.ppc_image_grid`, `sci.swatches`, `sci.power_spectrum`, `sci.point_pattern` |
| `causal.*` (v0.13) | `causal.impact` (actual against the values without the change, the effect in each period and the cumulative effect), `causal.pre_fit`, `causal.donor_weights`, `causal.event_study` |
| `net.*` (Plotly) | `net.graph` (a node-link drawing and an adjacency matrix of an edge variable `(..., node, node_)`) |

`get_recipe(id)` finds a recipe in all the packs. `mapi_plots.examples.example_inputs()` gives
synthetic inputs for every recipe.

## Helpers and templates

- `from_altair(chart, id=...)` / `from_plotly(fig, id=...)` turn charts into PlotSpecs with
  named Arrow datasets (Plotly arrays bound with `{"$ref": "trace0.y"}`).
- `apply_template` / `render_template` resolve role/dim-bound templates
  (`{x: "dim:time", y: "role:target", facet: "dim:geo"}`) against a `DataSchema`.

See `docs/guides/plots.md` and `docs/reference/plots.md`.
