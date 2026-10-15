# Plots: recipes, templates and safe sharing

Every model you fit gets a set of standard plots for free: data overview, convergence, posterior
predictive checks, a fit-and-forecast fan, decomposition, scenario comparisons and more. Plots
are **Vega-Lite** (or **Plotly** for 3D, maps and animation) wrapped in a small envelope called a
`PlotSpec`. The data travels next to the spec as named Arrow tables — never as a URL — and
every plot passes a sanitizer before anyone sees it.

```bash
uv add "mapi-plots[recipes]"   # recipes + Altair/Plotly helpers (runner images, notebooks)
uv add mapi-plots              # sanitizer and templates only (JSON in, JSON out)
```

<figure markdown>
  ![Fit and forecast fan](../assets/plots/core_ppc_fan.png)
  <figcaption>`core.ppc_fan`: model estimate, 50% and 90% bands, actual values and the end of the training data, one panel per market.</figcaption>
</figure>

## Render a recipe in a notebook

A recipe is a function from a fitted bundle or a score output to a `PlotSpec`:

```python
--8<-- "plots.py:recipe"
```

Inputs are flexible: an `xr.DataTree` (bundle or score output), an `xr.Dataset`, or a plain
`{"group": Dataset}` mapping. Recipes find their groups at the root, under `model/` (bundles) or
anywhere in the tree. Options are validated; an unknown option or a missing group raises a typed
`RecipeDataError` (`SCHEMA_INVALID`) that says what was expected.

Every plot carries:

- **`persona`** — `guided` (analysts), `expert` (data scientists) or `both`;
- **`caption_plain`** — a plain-language reading of the chart with the actual numbers
  ("The scenario changes total revenue by 184 (90% interval 139 to 251) compared with the
  baseline. Probability of an increase: 100%.");
- **`provenance`** — the recipe and version, the bundle id and a timestamp.

!!! tip "Try every recipe without a model"
    `mapi_plots.examples.example_inputs()` returns synthetic bundle and score trees for every
    recipe, built with xarray only:

    ```python
    --8<-- "plots.py:all"
    ```

## The standard catalog

| Recipe | What it shows | Reads |
|---|---|---|
| `core.data_overview` | Each variable over time per group, a missingness heatmap, group sizes | raw data / `observed_data` |
| `core.prior_predictive` | Datasets the model generates before seeing data vs your data, plus a prior fan | `prior_predictive`, `observed_data` |
| `core.convergence` | R-hat and ESS per parameter, a table of the worst, trace and rank plots, divergences | `posterior`, `sample_stats` |
| `core.posterior_forest` | Median, 50% and 94% HDI per parameter, one panel per variable | `posterior` |
| `core.ppc` | Predictive densities vs observed, PIT histogram, interval coverage | `posterior_predictive`, `observed_data` |
| `core.ppc_fan` | Fit + forecast fan with the train/forecast split line | draws or `summary`, `observed` |
| `core.fit_vs_actual` | Predicted against actual with R² and MAPE | predictions + observed |
| `core.residuals_over_time` | Actual minus predicted over time | predictions + observed |
| `core.decomposition` | Stacked contributions over time and each component's total share | `contributions` |
| `core.model_comparison` | elpd per model and the difference to the best (PSIS-LOO table or WAIC) | `comparison`, or one child per model with `log_likelihood` |
| `core.counterfactual` | Scenario vs baseline over time and the distribution of the total change | `draws`/`summary`, `baseline/…`, `delta/…` |
| `core.decision_distribution` | Distribution of a decision objective per option and P(best) | `decision`/`objective` |

The **marketing-mix pack** (`mmm.*`) adds response curves with current spend marked, marginal
cost (or marginal ROI) per channel, a budget-plan comparison and a channel decomposition.
The **tabular pack** (`ml.*`) adds `ml.feature_importance`: ranked tree importances, or signed
coefficients for linear estimators, read from the groups the scikit-learn and xgboost plugins
store in their bundles.

<div class="grid" markdown>

![Convergence](../assets/plots/core_convergence.png)

![Response curves](../assets/plots/mmm_response_curves.png)

![Counterfactual](../assets/plots/core_counterfactual.png)

![Budget plans](../assets/plots/mmm_budget_plans.png)

</div>

Recipes respect each output's **representation**: they sum joint draws when a dim has to go,
but never add up quantiles or intervals — a summary with an extra dim asks you to request an
aggregated output instead (summed means are shown without a band). Predicted-vs-actual and
residual plots never need a total, so they compare such summaries cell by cell (one dot or line
per `store × sku`, coloured by the facet dim).

## Bring your own chart

Build with Altair or Plotly as usual, then wrap it:

```python
--8<-- "plots.py:altair"
```

- `from_altair` moves Altair's inline data (and any `data.values`) into named Arrow datasets
  and rewrites the references; named data (`alt.Data(name="pts")`) is filled from
  `datasets={"pts": frame}`.
- `from_plotly` decodes plotly.py 6 typed arrays (`{"dtype": "f8", "bdata": ...}`) into Arrow
  datasets, one per trace, and binds them with `{"$ref": "trace0.y"}`. The default plotly.py
  template is dropped (the renderer themes plots) unless `keep_template=True`.

Both run the sanitizer and raise a `SanitizeError` if the chart breaks a rule. `from_plotly`
also strips HTML from the strings it moves into datasets (see below).

**Colours and dark mode.** An explicit mark colour wins over any theme, so the platform
recipes draw only with `mapi_plots.palette` (for example `OBSERVED`, `ESTIMATE`, `BAND`,
`ALERT`). In dark mode the plot sandbox swaps each palette colour for its dark variant; every
stroke, dot and text colour keeps at least 3:1 contrast on both card backgrounds. Use the
palette in your own charts to get the same behaviour. Any other colour is drawn as written in
both themes.

## Role- and dim-bound templates

A template binds channels to **roles and dims** instead of one model's variable names, so a
"target over time, one line per geo" plot applies to every model of a family:

```python
--8<-- "plots.py:template"
```

Inside a template spec, `{"$bind": "y"}` becomes a Vega-Lite field definition and `"{{y}}"`
(also `{{y.field}}`, `{{y.type}}`, `{{y.title}}`) is replaced by the bound name. Binding kinds
are `dim:`, `role:`, `var:`, `coord:` and `stat:` (a summary statistic such as
`stat:hdi_90_lower`). The JSON-only half, `mapi_plots.templates.apply_template`, runs in the API
to check that a saved template applies to a model before anything is rendered.

## What the sanitizer allows

Plots run in an isolated iframe on a separate origin, but the server checks every plot first:

| Rejected | Why |
|---|---|
| `url` data (anywhere, including lookups and topojson) | data must be referenced by name; no fetching |
| `href` encodings and mark properties | no navigation out of the plot |
| `image` marks, `url` channels, `layout.images` / `image` traces with non-`data:` sources | no external fetches (only base64 PNG/JPEG/GIF/WebP) |
| Tile-map traces and subplots (`scattermap`, `choroplethmap`, `densitymap`, `layout.map`) | tiles come from third-party servers; use `scattergeo`/`choropleth` |
| `__proto__`, `constructor`, `prototype` keys or field paths | prototype pollution |
| `javascript:`, `data:text/html`, SVG data URIs, CSS `url()` in any string | script and fetch vectors |
| Vega expressions outside a small allow-list of functions, method calls, `window`/`document`/`fetch`, computed member names | expression sandbox escapes |
| Custom format types, Plotly network options (`topojsonURL`, `showLink`, ...) | code and network hooks |
| Anything that does not match the pinned Vega-Lite v6.4.1 / plotly.js 4.1.1 schemas | unknown keys are where exploits hide |

Plotly text keeps only `<b>`, `<i>`, `<br>`, `<sub>` and `<sup>` (plus `<extra>` in hover
templates); everything else is stripped and reported as a change. The server never decodes
Arrow, so text that `from_plotly` moves into a dataset (`text`, `hovertext`, category labels)
is stripped and scanned on the sandbox side during conversion; the plot iframe must still
treat dataset strings as untrusted and re-strip them before handing them to plotly.js. Vega
renders dataset strings as text (and the iframe's tooltip handler escapes them). Method
`args` of Plotly buttons and sliders are checked in both the `{attr: value}` and positional
`[attr, value]` forms, and `usermeta.embedOptions` is rejected in Vega-Lite specs. Specs are
capped in size, depth, node count and string length. See the [plots reference](../reference/plots.md) for the
full rule list and error codes.

```python
--8<-- "plots.py:sanitize"
```
