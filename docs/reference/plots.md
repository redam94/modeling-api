# Plots reference (`mapi-plots`)

`mapi-plots` has two halves with different dependency footprints:

| Install | Modules | Imports | Runs in |
|---|---|---|---|
| `mapi-plots` | `mapi_plots.sanitize`, `mapi_plots.templates` | `mapi-spec`, `jsonschema` (JSON only) | API server, runner, notebooks |
| `mapi-plots[recipes]` | `mapi_plots.convert`, `mapi_plots.recipes`, `mapi_plots.packs`, `mapi_plots.examples` | + altair, plotly, numpy, pandas, pyarrow, xarray | runner images, notebooks |

An import-linter contract keeps the sanitizer and templates free of numpy, pandas, xarray,
pyarrow, altair, plotly and `mapi_sdk`, and `import mapi_plots` loads nothing heavy (exports are
lazy).

## The PlotSpec envelope

Defined in `mapi_spec.plots` (the contract package):

```yaml
id: core.ppc_fan                    # [A-Za-z0-9_.-]{1,128}
title: Fit and forecast
kind: vega-lite                     # or plotly
spec: {...}                         # Vega-Lite JSON, or Plotly {data, layout, frames, config}
datasets:                           # referenced BY NAME only
  fan: {kind: inline_arrow, data: <base64 Arrow IPC stream>, rows: 84}
  big: {kind: data_ref, path: ui/plotdata/fan.arrow, format: arrow}
bindings: {x: "dim:time", y: "role:target", facet: "dim:geo"}   # templates only
schema_version: v6.4.1
persona: both                       # guided | expert | both
caption_plain: "The line is the model's median estimate ..."
provenance: {recipe: core.ppc_fan@1.0.0, bundle_id: ..., generated_at: ...}
```

- Vega-Lite references data with `{"data": {"name": "fan"}}`.
- Plotly references a column with `{"$ref": "<dataset>.<column>"}` in any data-array attribute
  (and in `arrayOk` attributes such as `marker.color`). The part before the first `.` is the
  dataset; the rest is the column (`trace0.marker.color` → dataset `trace0`, column
  `marker.color`). Its JSON Schema ships as `mapi_plots.sanitize.PLOTLY_REF_SCHEMA` and as the
  file `mapi_plots/sanitize/schemas/plotly-ref.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:mapi:schema:plotly-dataset-ref:v1",
  "type": "object",
  "properties": {"$ref": {"type": "string",
    "pattern": "^[A-Za-z_][A-Za-z0-9_\\-]{0,63}\\.[A-Za-z_][A-Za-z0-9_.\\-]{0,127}$"}},
  "required": ["$ref"],
  "additionalProperties": false
}
```

- Inline Arrow datasets are at most 2 MiB of base64 each. Recipe datasets are written
  compactly (float32, int32, dictionary-encoded strings); timestamps are UTC milliseconds.

## Sanitizer

```python
from mapi_plots.sanitize import sanitize_plot, check_plot, sanitize_spec, SanitizeLimits

result = sanitize_plot(doc)            # PlotSpec | dict | JSON str | bytes
result.plot                            # validated, sanitized PlotSpec
result.changes                         # list[Issue] (severity info): HTML stripped, $schema pinned ...

issues = check_plot(doc)               # errors + changes, never raises
spec, changes = sanitize_spec("plotly", figure_dict)   # bare spec, no envelope
```

`SanitizeError` is a `MapiError`; `err.issues` lists every violation (capped at
`max_issues`), each with an RFC 6901 `pointer`, a platform-written `message`, the rule in
`context["rule"]` and any user-supplied text only in `untrusted_text`. `err.rules` is the set of
rules that fired. The error code is the most severe present:

| Code | HTTP | Rules |
|---|---|---|
| `PLOT_UNSAFE` | 422 | `url_data`, `href`, `external_image`, `tile_map`, `proto_key`, `script_url`, `css_url`, `expression`, `format_type`, `config` |
| `PAYLOAD_TOO_LARGE` | 413 | `too_large`, `too_deep`, `too_many_nodes`, `string_too_long`, `array_too_long` |
| `SPEC_PARSE_ERROR` | 400 | `parse` (invalid JSON, duplicate keys, NaN/Infinity) |
| `SCHEMA_INVALID` | 422 | `schema`, `envelope`, `dataset_ref`, `typed_array` |

Changes (never errors) use the rules `html_stripped`, `schema_url` and `template_pruned`.

### Limits

| `SanitizeLimits` field | Default |
|---|---|
| `max_document_bytes` | 16 MiB (whole PlotSpec, inline Arrow included) |
| `max_spec_bytes` | 2 MiB (the `spec` member) |
| `max_depth` | 48 |
| `max_nodes` | 200 000 |
| `max_string_bytes` | 64 KiB (inline Arrow payloads are capped by the envelope instead) |
| `max_array_items` | 100 000 |
| `max_expr_chars` | 2 000 |
| `max_typed_array_bytes` | 8 MiB |
| `max_issues` | 50 |

### Vega-Lite rules

1. `$schema` must be an official Vega-Lite v5 or v6 URL (or absent); it is pinned to v6.4.1.
2. The spec must validate against the vendored Vega-Lite v6.4.1 JSON Schema (draft-07,
   local `$ref`s only).
3. No `url` key anywhere; no `href`; no `image` mark; `formatType` only `number`, `time` or
   `utc`; `config.customFormatTypes` must not be enabled.
4. Expressions — `expr`, `calculate`, `filter`/`test` strings (and their `and`/`or`/`not`
   compositions), any `…Expr` key such as `labelExpr`, and bracketed filters inside selection
   event streams — must use only allow-listed Vega functions. Method calls, `this`/`new`,
   assignment, statements, template and regex literals, `\u`/`\x` escapes, non-ASCII outside
   strings, computed member access with a non-literal key, browser globals (`window`,
   `document`, `fetch`, `globalThis` …) and `event.view` are rejected.

### Plotly rules

1. Top level has only `data`, `layout`, `frames` and `config`.
2. Trace types, attribute names and value types are checked against a table derived from the
   official plotly.js 4.1.1 `plot-schema.json` (enumerations, flag lists, subplot ids, colour
   scales, info arrays; `xaxis2`-style subplot keys; `annotationdefaults`-style keys in
   templates). Unknown attributes are errors — except inside `layout.template`, where they
   (and tile-map sections) are pruned and reported.
3. Data arrays are inline lists, dataset references or plotly.py typed arrays
   (`{dtype: i1|u1|u1c|i2|u2|i4|u4|f4|f8, bdata, shape?}`, validated for base64, length and
   shape).
4. Tile-map traces/subplots, non-`data:image/(png|jpeg|gif|webp);base64` image sources (also
   inside `updatemenus`/`sliders` relayout arguments), `url`/`href`/`topojsonURL` keys and
   network `config` options are rejected.
5. Strings are stripped to `<b> <i> <br> <sub> <sup>` (attributes dropped, stray `<` escaped);
   `<extra>` survives in hover templates.

### Vendored schemas

| File | Source | Regenerate |
|---|---|---|
| `vega-lite-v6.4.1.json` | `https://vega.github.io/schema/vega-lite/v6.4.1.json` (byte-for-byte) | `scripts/vendor_schemas.py --download` |
| `plotly-structure-v4.1.1.json` | derived from `plotly.js@v4.1.1/dist/plot-schema.json` | same |
| `VENDORED.json` | source URLs and SHA-256 digests (checked by a unit test) | same |

## Templates (`mapi_plots.templates`)

```python
resolve_bindings(bindings, schema, *, optional=()) -> dict[channel, BoundField | list[BoundField]]
apply_template(plot, schema, *, optional=(), title=None) -> PlotSpec
template_issues(plot, schema, *, optional=()) -> list[Issue]
```

| Binding | Resolves to | Vega-Lite type |
|---|---|---|
| `dim:<name>` | a dim of the schema | `time` → temporal, numeric → quantitative, ordered/index → ordinal, else nominal |
| `role:<role>` | the single variable with that role (lists for `tooltip`) | from the dtype |
| `var:<name>` | that variable | from the dtype |
| `coord:<name>` | a coordinate (dim or not) | from the dim, else nominal |
| `stat:<name>` | a summary statistic column (`mean`, `hdi_90_lower`, `q5` …) | quantitative |

`render_template(template, data, schema=None, *, group=None, value=None, optional=())` (extra
`recipes`) also builds the dataset from an `xr.Dataset`/tree group; the schema defaults to
`attrs["mapi_schema"]` or is inferred (roles from each variable's `role` attribute).

## Recipes (`mapi_plots.recipes`)

```python
get_recipe(id) -> Recipe                 # bundled recipes (core + packs)
standard_recipes() -> list[Recipe]       # entry point  mapi.plots: core
mapi_plots.packs.mmm.mmm_recipes()       # entry point  mapi.plots: mmm
Recipe.render(data, **settings) -> PlotSpec       # also Recipe(data, **settings)
Recipe.catalog_entry() -> PlotCatalogEntry        # id, version, persona, required groups/representation

# what the runner calls for outputs.plots: never raises for a single bad plot
report = render_plots(tree, ["core.ppc_fan", "mmm.response_curves@^1"],
                      registry=None, options={"core.ppc_fan": {"facet": "market"}})
report.plots          # sanitized PlotSpecs
report.issues         # skipped plots: REF_UNRESOLVED / SCHEMA_INVALID / USER_CODE_ERROR
                      # (warnings) and PLOT_UNSAFE (error)
report.to_json()      # plots.json sidecar body
```

Write your own with the decorator — the result works with `Registry.register` and the
`mapi.plots` entry-point group:

```python
import altair as alt
from mapi_plots.recipes import Figure, RecipeSettings, named, recipe


class Settings(RecipeSettings):
    var: str = "y_hat"


@recipe("acme.weekly_total", title="Weekly total", settings=Settings, persona="guided")
def weekly_total(inp, s: Settings) -> Figure:
    ds = inp.require("summary")
    frame = ds[s.var].sel(stat="mean").sum("market").to_dataframe().reset_index()
    chart = alt.Chart(named("weekly")).mark_line().encode(x="date:T", y=f"{s.var}:Q")
    return Figure(chart, {"weekly": frame}, caption=f"Total {s.var} per week.")
```

### Settings shared by every recipe

`title` (override), `width` (480), `height` (180). Unknown settings are rejected.

### Per-recipe settings

| Recipe | Settings (defaults) |
|---|---|
| `core.data_overview` | `group`, `vars`, `along`, `facet`, `max_vars` (8), `max_levels` (12), `max_rows` (20 000) |
| `core.prior_predictive` | `var`, `num_draws` (50), `along`, `levels` ([0.5, 0.9]) |
| `core.convergence` | `var_names`, `max_parameters` (40), `max_traces` (6), `max_draws` (400), `max_elements` (2 000), `rhat_threshold` (1.01), `ess_per_chain` (100), `bins` (20), `table_rows` (15) |
| `core.posterior_forest` | `var_names`, `hdi_prob` (0.94), `inner_prob` (0.5), `point` (median), `max_rows` (60) |
| `core.ppc` | `var`, `observed_var`, `num_draws` (50), `bins` (20), `levels` |
| `core.ppc_fan` | `var`, `observed_var`, `along`, `facet`, `max_facets` (12), `levels` ([0.5, 0.9]), `point` (median), `split_at`, `columns` (3), `max_rows` |
| `core.fit_vs_actual`, `core.residuals_over_time` | `var`, `observed_var`, `along`, `facet`, `max_facets`, `point` |
| `core.decomposition`, `mmm.decomposition` | `vars`, `component` (auto / `channel`), `along`, `hdi_prob` (0.9), `max_components` (20) |
| `core.model_comparison` | `ic` (auto: precomputed table, else WAIC), `var`, `model_dim` (model) |
| `core.counterfactual` | `var`, `along`, `hdi_prob` (0.9), `bins` (30) |
| `core.decision_distribution` | `var`, `candidate`, `hdi_prob`, `higher_is_better` (true), `max_candidates` (12) |
| `mmm.response_curves` | `var`, `channel`, `grid`, `spend_var` (spend), `current_var` (current_spend), `hdi_prob`, `facet` |
| `mmm.marginal_cost` | as response curves + `metric` (cost \| roi), `clip_quantile` (0.98) |
| `mmm.budget_plans` | `spend_var`, `outcome_var`, `plan`, `channel`, `reference`, `hdi_prob` |

Convergence diagnostics are rank-normalised split R-hat and bulk/tail ESS (Vehtari et al. 2021),
implemented in numpy and tested to match ArviZ exactly.
