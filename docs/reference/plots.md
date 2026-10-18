# Plots reference (`mapi-plots`)

`mapi-plots` has two parts. Each part has different dependencies:

| Install | Modules | Imports | Runs in |
|---|---|---|---|
| `mapi-plots` | `mapi_plots.sanitize`, `mapi_plots.templates` | `mapi-spec`, `jsonschema` (JSON only) | API server, runner, notebooks |
| `mapi-plots[recipes]` | `mapi_plots.convert`, `mapi_plots.recipes`, `mapi_plots.packs`, `mapi_plots.examples` | + altair, plotly, numpy, pandas, pyarrow, xarray | runner images, notebooks |

An import-linter contract makes sure that the sanitizer and the templates do not import
numpy, pandas, xarray, pyarrow, altair, plotly or `mapi_sdk`. `import mapi_plots` loads no
large modules, because the exports are lazy.

## The PlotSpec envelope

The contract package `mapi_spec.plots` defines the envelope:

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
- Plotly references a column with `{"$ref": "<dataset>.<column>"}` in a data-array attribute.
  It also uses this reference in `arrayOk` attributes, for example `marker.color`. The part
  before the first `.` is the dataset. The remaining part is the column
  (`trace0.marker.color` → dataset `trace0`, column `marker.color`). The JSON Schema of the
  reference is `mapi_plots.sanitize.PLOTLY_REF_SCHEMA`. It is also in the file
  `mapi_plots/sanitize/schemas/plotly-ref.schema.json`:

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

- The maximum size of each inline Arrow dataset is 2 MiB of base64. Recipes write datasets in
  a compact format (float32, int32, dictionary-encoded strings). Timestamps are UTC
  milliseconds.

## Sanitizer

```python
from mapi_plots.sanitize import sanitize_plot, check_plot, sanitize_spec, SanitizeLimits

result = sanitize_plot(doc)            # PlotSpec | dict | JSON str | bytes
result.plot                            # validated, sanitized PlotSpec
result.changes                         # list[Issue] (severity info): HTML stripped, $schema pinned ...

issues = check_plot(doc)               # errors + changes, never raises
spec, changes = sanitize_spec("plotly", figure_dict)   # bare spec, no envelope
```

`SanitizeError` is a `MapiError`. `err.issues` lists all violations, up to `max_issues`. Each
violation has these items:

- An RFC 6901 `pointer`.
- A `message` that the platform wrote.
- The rule, in `context["rule"]`.
- Text from the user, if there is some. This text is only in `untrusted_text`.

`err.rules` is the set of rules that fired. The error code is the most severe code that is
present:

| Code | HTTP | Rules |
|---|---|---|
| `PLOT_UNSAFE` | 422 | `url_data`, `href`, `external_image`, `tile_map`, `proto_key`, `script_url`, `css_url`, `expression`, `format_type`, `config` |
| `PAYLOAD_TOO_LARGE` | 413 | `too_large`, `too_deep`, `too_many_nodes`, `string_too_long`, `array_too_long` |
| `SPEC_PARSE_ERROR` | 400 | `parse` (JSON that is not valid, duplicate keys, NaN/Infinity) |
| `SCHEMA_INVALID` | 422 | `schema`, `envelope`, `dataset_ref`, `typed_array` |

Changes use the rules `html_stripped`, `schema_url` and `template_pruned`. Changes are not
errors.

### Limits

| `SanitizeLimits` field | Default |
|---|---|
| `max_document_bytes` | 16 MiB (the full PlotSpec, with the inline Arrow data) |
| `max_spec_bytes` | 2 MiB (the `spec` member) |
| `max_depth` | 48 |
| `max_nodes` | 200 000 |
| `max_string_bytes` | 64 KiB (the envelope sets the limit for inline Arrow payloads) |
| `max_array_items` | 100 000 |
| `max_expr_chars` | 2 000 |
| `max_typed_array_bytes` | 8 MiB |
| `max_issues` | 50 |

### Vega-Lite rules

1. `$schema` must be an official Vega-Lite v5 or v6 URL, or not present. The sanitizer sets it
   to v6.4.1.
2. The spec must be valid against the vendored Vega-Lite v6.4.1 JSON Schema (draft-07, local
   `$ref`s only).
3. Do not use these items: a `url` key at any location, `href`, and the `image` mark.
   `formatType` can only be `number`, `time` or `utc`. Do not enable
   `config.customFormatTypes`.
4. Expressions must use only Vega functions from the allow list. Expressions are:
    - `expr`, `calculate` and `filter`/`test` strings, and their `and`/`or`/`not`
      compositions.
    - Each `…Expr` key, for example `labelExpr`.
    - Filters in brackets in selection event streams.

    The sanitizer rejects these items: method calls, `this`/`new`, assignment, statements,
    template and regex literals, and `\u`/`\x` escapes. It also rejects non-ASCII characters
    outside strings, and computed member access with a key that is not a literal. It also
    rejects browser globals (`window`, `document`, `fetch`, `globalThis` …) and `event.view`.

### Plotly rules

1. The top level has only `data`, `layout`, `frames` and `config`.
2. The sanitizer does a check of trace types, attribute names and value types against a table.
   This table comes from the official plotly.js 4.1.1 `plot-schema.json`. It has enumerations,
   flag lists, subplot ids, colour scales and info arrays. It also has subplot keys of the
   `xaxis2` type, and template keys of the `annotationdefaults` type.
3. Unknown attributes are errors. In `layout.template`, the sanitizer removes unknown
   attributes and tile-map sections, and reports them.
4. Data arrays are inline lists, dataset references or plotly.py typed arrays
   (`{dtype: i1|u1|u1c|i2|u2|i4|u4|f4|f8, bdata, shape?}`). The sanitizer does a check of the
   base64, length and shape of typed arrays.
5. The sanitizer rejects these items:
    - Tile-map traces and subplots.
    - Image sources that are not `data:image/(png|jpeg|gif|webp);base64`. This rule also
      applies in the relayout arguments of `updatemenus`/`sliders`.
    - `url`/`href`/`topojsonURL` keys.
    - Network `config` options.
6. Strings can keep only `<b> <i> <br> <sub> <sup>`. The sanitizer removes attributes and
   escapes a stray `<`. `<extra>` stays in hover templates.

### Vendored schemas

| File | Source | Regenerate |
|---|---|---|
| `vega-lite-v6.4.1.json` | `https://vega.github.io/schema/vega-lite/v6.4.1.json` (byte-for-byte) | `scripts/vendor_schemas.py --download` |
| `plotly-structure-v4.1.1.json` | made from `plotly.js@v4.1.1/dist/plot-schema.json` | same |
| `VENDORED.json` | source URLs and SHA-256 digests (a unit test does a check of them) | same |

## Templates (`mapi_plots.templates`)

```python
resolve_bindings(bindings, schema, *, optional=()) -> dict[channel, BoundField | list[BoundField]]
apply_template(plot, schema, *, optional=(), title=None) -> PlotSpec
template_issues(plot, schema, *, optional=()) -> list[Issue]
```

| Binding | Resolves to | Vega-Lite type |
|---|---|---|
| `dim:<name>` | a dim of the schema | `time` → temporal, numeric → quantitative, ordered/index → ordinal, other → nominal |
| `role:<role>` | the one variable with that role (lists for `tooltip`) | from the dtype |
| `var:<name>` | that variable | from the dtype |
| `coord:<name>` | a coordinate (dim or not) | from the dim, or nominal if it is not a dim |
| `stat:<name>` | a summary statistic column (`mean`, `hdi_90_lower`, `q5` …) | quantitative |

`render_template(template, data, schema=None, *, group=None, value=None, optional=())` (extra
`recipes`) also makes the dataset from an `xr.Dataset` or a tree group. The default schema is
`attrs["mapi_schema"]`. If that is not available, the function infers the schema. It gets the
roles from the `role` attribute of each variable.

## Recipes (`mapi_plots.recipes`)

```python
get_recipe(id) -> Recipe                 # bundled recipes (core + packs)
standard_recipes() -> list[Recipe]       # entry point  mapi.plots: core
mapi_plots.packs.mmm.mmm_recipes()       # entry point  mapi.plots: mmm
mapi_plots.packs.ml.ml_recipes()         # entry point  mapi.plots: ml
mapi_plots.packs.sci.sci_recipes()       # entry point  mapi.plots: sci (Plotly)
mapi_plots.packs.net.net_recipes()       # entry point  mapi.plots: net (Plotly)
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

To write your own recipe, use the decorator. The result works with `Registry.register` and
the `mapi.plots` entry-point group:

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

A recipe can also return a Plotly figure (`plotly.graph_objects.Figure` or a
`{data, layout, frames}` dict) in `Figure.chart`. `Recipe.render` then sends it through
`from_plotly` with the same title, caption, persona and provenance. The recipe can also give
its own DataFrames in `Figure.datasets`. The traces then refer to their columns with
`{"$ref": "<dataset>.<column>"}`. The function `plotly_plot` applies the inline-text rules to
the string columns of these DataFrames.

### Settings shared by every recipe

`title` (override), `width` (480), `height` (180). The recipe rejects unknown settings.

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
| `core.pair` | `var_names`, `max_params` (5), `max_draws` (500), `bins` (20), `width` (520) |
| `core.ridge` | `var`, `group` (auto \| posterior \| predictions \| posterior_predictive), `dim`, `select`, `hdi_prob` (0.94), `max_levels` (30), `max_draws` (4 000), `points` (100), `overlap` (1.6), `descending` (true) |
| `core.rank_matrix` | `var`, `group`, `dim`, `select`, `descending` (true: rank 1 is the largest value), `max_items` (30), `max_draws` (4 000), `labels` (auto \| always \| never) |
| `core.calibration` | `var`, `observed_var`, `kind` (auto \| continuous \| binary), `prob` (0.95), `bins` (10), `n_sim` (1 000), `seed` (0) |
| `core.state_sequence` | `var`, `group` (auto \| posterior \| predictions \| posterior_predictive \| root), `state_dim` (state), `along`, `observed_var`, `select`, `max_states` (10), `max_times` (2 000), `max_draws` (4 000), `width` (640), `height` (140) |
| `mmm.response_curves` | `var`, `channel`, `grid`, `spend_var` (spend), `current_var` (current_spend), `hdi_prob`, `facet` |
| `mmm.marginal_cost` | the same as response curves, + `metric` (cost \| roi), `clip_quantile` (0.98) |
| `mmm.budget_plans` | `spend_var`, `outcome_var`, `plan`, `channel`, `reference`, `hdi_prob` |
| `ml.feature_importance` | `top` (20), `feature_dim` (feature), `source` (auto \| importance \| coefficients) |
| `sci.field_map` | `var`, `group` (auto \| posterior \| predictions \| posterior_predictive), `y`, `x`, `frame`, `select`, `stats` ([mean, sd]; also median, z, q05, q95, draws), `n_draws` (3), `max_draws` (400), `max_cells` (40 000), `max_frames` (24), `colormap`, `log`, `origin`, `aspect` (equal \| auto), `columns` (3), `width` (760), `height` (280, one row of panels) |
| `sci.vector_field` | the same as `sci.field_map` (no `n_draws`), + `component`, `stats` ([mean, sd]; also q05, q95), `arrows` (20) |
| `sci.profile_bands` | `var`, `group`, `observed_var`, `along`, `by`, `select`, `levels` ([0.5, 0.9]), `point` (median \| mean), `max_curves` (12), `max_draws` (1 000), `colormap` (viridis), `log` (false), `width` (640), `height` (360) |
| `sci.ensemble` | `var`, `group`, `observed_var`, `along`, `select`, `n_draws` (40), `levels` ([0.9]), `point` (median \| mean \| none), `max_draws` (2 000), `seed` (0), `log` (false), `width` (640), `height` (320) |
| `sci.area_map` | `var`, `group`, `observed`, `stats` (observed, mean, sd, exceedance; also median), `threshold` (1 for a positive output, else 0), `mark_above` (0.95), `log_scale` (false), `show_edges` (false), `select`, `colormap` (viridis), `classes` (9), `max_draws` (2 000), `max_vertices` (40 000), `columns` (2), `width` (760), `height` (340, one row of panels) |
| `sci.phylogeny` | `var`, `group`, `tip_dim`, `observed`, `interval` (0.9), `point` (mean \| median), `max_tips` (400), `max_draws` (2 000), `labels` (up to 80 tips), `select`, `colormap` (viridis), `width` (780), `height` (0: from the number of tips) |
| `sci.rose` | `var`, `group`, `observed`, `angle_dim`, `units` (radians \| degrees \| hours \| days), `period`, `bins` (24), `frame`, `select`, `interval` (0.9), `scale` (auto \| count \| share), `clockwise`, `max_draws` (1 000), `max_frames` (24), `width` (560), `height` (520) |
| `sci.image_residuals` | the same as `sci.field_map` (no `stats`, `n_draws`), + `observed_var`, `sigma` (a variable or a number), `sigma_kind` (sd \| variance \| inverse_variance), `columns` (2), `hist_height` (200; 0: no histogram). `group` auto reads posterior_predictive, predictions, posterior |
| `sci.ppc_image_grid` | the same as `sci.field_map` (no `stats`), + `observed_var`, `n_draws` (5, at most 11), `shuffle` (false), `seed` (0). `group` auto reads posterior_predictive, predictions, posterior |
| `sci.swatches` | `var`, `group`, `observed_var`, `channel` (rgb), `item`, `row` (row), `col` (col), `arrange` (auto \| list \| map), `prob`, `prob_as` (border \| opacity), `select`, `n_draws` (3), `max_draws` (400), `max_items` (60, list only), `tile` (20 px), `columns` (2), `seed` (0), `width` (640) |
| `sci.power_spectrum` | `var`, `group`, `observed_var`, `error_var` (default `<observed>_sd`), `theory`, `along`, `x_coord`, `select`, `levels` ([0.5, 0.9]), `point` (median \| mean), `max_draws` (2 000), `log_x`, `log_y` (false), `width` (640), `height` (360) |
| `sci.point_pattern` | `points` (the point dim), `x` (x), `y` (y), `sd_x` (sd_x), `sd_y` (sd_y), `radius` (radius), `group` (auto \| none \| a variable), `field` (auto \| none \| a variable), `field_stat` (mean \| median \| sd), `ellipses` (auto), `level` (0.9), `observed` (true), `select`, `max_points` (20 000), `max_ellipses` (2 000), `max_cells` (40 000), `colormap`, `origin`, `marker_size` (6), `width` (640), `height` (560) |
| `net.graph` | `var`, `node_dim`, `positions` (auto \| none \| a variable), `threshold` (auto), `signed` (auto), `directed` (auto), `group` (auto \| none \| a variable), `truth` (auto \| none \| a variable), `panels` (both \| graph \| matrix), `select`, `labels` (auto), `max_nodes` (300), `max_edges` (2 000), `seed` (0), `width` (900), `height` (440) |

### Extra core recipes

These five recipes are in the core pack (`mapi_plots.recipes.extras`, Vega-Lite):

| Recipe | What it shows |
|---|---|
| `core.pair` | A corner plot of up to `max_params` scalar parameter elements. Below the diagonal: thinned draws of each pair. On the diagonal: a histogram of each parameter. Red points are divergent transitions (`sample_stats.diverging`). |
| `core.ridge` | One density for each level of `dim`, sorted by the mean, with a dot for the mean and a bar for the HDI. Each curve has the same peak height. |
| `core.rank_matrix` | P(rank = k) for each item, sorted by the expected rank, and the matrix of P(row above column). Input: draws `(…, item)`. |
| `core.calibration` | Continuous targets: the PIT-ECDF difference with a simultaneous envelope (simulated). Targets with values 0 and 1: a reliability diagram with a binomial band for each bin and a histogram of the predicted probabilities. |
| `core.state_sequence` | The observed series (points coloured by the most likely state) above the stacked state probabilities. Input: probabilities `(…, state, time)` with or without draws, or integer state draws `(chain, draw, time)`. |

### Scientific-model pack (`sci.*`)

The `sci.*` recipes make Plotly figures for models with gridded outputs, vector fields,
profiles and trajectories. They read draws (`chain`/`draw` or `sample` dims) from
`posterior`, `predictions` or `posterior_predictive`. They read the actual values from
`observed_data`.

| Recipe | What it shows |
|---|---|
| `sci.field_map` | One heatmap panel for each statistic of a gridded output. A slider and a Play button show one `frame` at a time. |
| `sci.vector_field` | The length of a vector as colours, arrows of the mean vector, and the sd of the length. |
| `sci.profile_bands` | The median and the 50% and 90% bands along `along`, one curve for each level of `by`, and the actual values. A slider shows one level with its bands. |
| `sci.ensemble` | Some draws as thin lines, the 90% band of all draws, the median and the actual values. |
| `sci.area_map` | A map of areas (polygons) coloured by the observed value, the mean, the sd and the probability that the value is above `threshold`. A cross marks each area where this probability is above `mark_above`. |
| `sci.phylogeny` | A tree of the tips (species) next to a strip with the estimate and its interval for each tip, and the observed values. |
| `sci.rose` | A polar chart of angles: a rose of the observed angles with the 90% range of simulated data in each sector, or a curve around the circle with its limits. |
| `sci.image_residuals` | The observed image, the model mean, the residual and the normalised residual `(obs - mean) / sqrt(sd² + σ²)`, with a histogram of the normalised residuals against N(0, 1). |
| `sci.ppc_image_grid` | The observed image next to `n_draws` images from the model on one colour scale. With `shuffle`, the panels are in a random order and the caption names the real one. |
| `sci.swatches` | Squares in the colours of an sRGB output `(…, item, rgb)` in [0, 1]: observed, mean and some draws. A map when the item dim has `row` and `col` coords. |
| `sci.power_spectrum` | A binned spectrum `(…, bin)` against a coord such as `ell`: median, 50% and 90% bands, a theory curve and the observed values with error bars. |
| `sci.point_pattern` | One dot for each point (localisation, event) at its mean position, an error ellipse for each point, and an optional image or intensity field below the points. |

Data for the map, tree and polar recipes:

- `sci.area_map` reads the outlines of the areas from `ring_x(area, vertex)` and
  `ring_y(area, vertex)`. Fill the unused vertices with NaN. A NaN between points starts
  a new ring, for example an island. The first dim is the area dim. The optional
  `adjacency(area, area_)` matrix (not zero for neighbours) gives the lines of
  `show_edges`. The recipe finds these variables in `observed_data`, `constant_data`, a
  `geometry` group, the group of the output or the root. It draws the areas as filled
  shapes on axes with equal scales. It does not use a geographic projection or a base
  map. If the outlines have too many points, the recipe uses fewer points.
- `sci.phylogeny` reads the tree from `parent(node)`: the position of the parent node, or
  -1 for the root. The optional variables are `branch_length(node)` (default 1),
  `is_tip(node)` (default: the nodes that are not a parent) and `tip_label(node)`. The
  tips match the levels of the output by label. If there are no labels, tip k (in node
  order) is level k.
- `sci.rose` reads angles in `units`. One turn is 2π radians, 360 degrees, 24 hours or
  365.25 days, or `period`. If the output has an angle dim (`angle_dim`, or a dim called
  `angle`, `direction`, `hour` and so on), the recipe draws a curve. Otherwise it counts
  the angles in `bins` sectors.

Notes on the image and spectrum recipes (`mapi_plots.packs.sci_images`):

- `sci.image_residuals` and `sci.ppc_image_grid` need the observed image in `observed_data`,
  with the grid dims of the output. The recipe uses the variable with the same name, the
  `observed_var` setting or the only observed variable.
- Draws from `posterior_predictive` already hold the noise of the data, so
  `sci.image_residuals` adds no σ to them. For a noise-free output (`posterior` or
  `predictions`), it uses `sigma` or a variable named `sigma`.
- With `log`, `sci.image_residuals` shows the observed, mean and residual panels as log10.
  It computes the normalised residual and its histogram in the data's units, because σ is
  in those units.
- `sci.swatches` writes the colours as `#rrggbb` text in the dataset. The plot sandbox does
  not change these colours in dark mode, because they are data.

Statistics of `sci.field_map`:

- `mean`, `median`: the average value and the middle value of the draws.
- `sd`: the uncertainty. A high sd shows where the data do not fix the value.
- `z`: the mean divided by the sd. Values far from zero are clear.
- `q05`, `q95`: the limits of the 90% interval.
- `draws`: `n_draws` possible fields from the model.

The grid dims come from the settings. If you do not set them, the recipe uses the `grid` of
the output (`OutputDecl.grid`). It reads the `grid` from the `grid` attribute of the variable
or from the model manifest in the bundle. The `colormap`, `log` and `origin` settings also
come from the `grid` when you do not set them. If the output has more dims than the plot
uses, use `select` to pick one level of each, for example `select: {species: 0}`.

The size of a field plot has limits:

- All the cells of a plot are in one dataset: `x`, `y` and one column for each statistic
  and frame. The recipe keeps this dataset below the inline Arrow limit (2 MB).
- If the field has too many cells, the recipe calculates the average of each block of
  k × k cells. A cell with no value (NaN) stays empty. The caption tells the block size.
- A field of 189 × 193 cells with six panels and one frame has its full resolution.
- `max_frames` sets the maximum number of frames. The slider shows frames at equal
  intervals.

`sci.point_pattern` reads the variables `x` and `y` with one value for each level of the
point dim. Draws give the posterior mean and an ellipse from the 2 × 2 covariance of the
draws. A score summary gives its point statistic and an ellipse from its `sd`. Plain values
use the `sd_x` and `sd_y` variables (or a `radius` variable, drawn as a circle) when they
exist. The ellipse holds the position with probability `level`. The `field` is a gridded
output (by default the first output with a `grid`), drawn below the points in the same units.

### Network pack (`net.*`)

| Recipe | What it shows |
|---|---|
| `net.graph` | A node-link drawing of the links above `threshold` (line width shows the value, blue and red show the sign, arrows show the direction) and a heatmap of the full matrix. Dotted lines and open squares show the known or observed links. |

`net.graph` reads an edge variable with two node dims: the second dim has the name of the
first plus `_`, for example `(node, node_)` or `(var, var_)`. The values can be draws (the
recipe uses the posterior mean), a score summary (its point statistic) or plain values. They
can be probabilities, weights or partial correlations.

- `positions`: a variable `(node, latent_dim)`, for example the latent positions of a latent
  space model. The recipe uses the posterior mean of the first two coordinates. Without it,
  the recipe calculates a layout from the mean weights (a spectral start and seeded
  Fruchterman–Reingold steps). The layout puts strongly linked nodes near each other.
- `group`: a variable with one value for each node (by default `node_group` or `group`).
  Categories get one colour each and a legend. Continuous values get a colour scale.
- `truth`: a known or observed adjacency `(node, node_)`. By default the recipe uses one from
  `observed_data` or `constant_data`.
- `threshold`: by default 0.5 for values in [0, 1], else 10% of the largest absolute value.
  With draws, the tooltip at the middle of a link shows the share of draws above the
  threshold.

The plot sandbox shows the Plotly `frames`, `sliders` and `updatemenus` of these plots. In
dark mode, it changes the palette colours of the traces to their dark variants, the same as
for Vega-Lite.

The convergence diagnostics are rank-normalised split R-hat and bulk/tail ESS (Vehtari et al.
2021). The package calculates them with numpy. Tests make sure that the values agree exactly
with ArviZ.
