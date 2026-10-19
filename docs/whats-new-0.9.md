---
description: v0.9 adds the science gallery - 15 science models for images, fields, maps and graphs, 14 new plot recipes, workflow nodes for posterior checks, stacking, scores and plots, six new workflow recipes, a tutorial for each pymc-challenges case and an export helper for its data.
---

# What's new in 0.9 <span class="mapi-badge">v0.9</span>

**v0.9 brings the Bayesian workflow of a research notebook to the platform.** The science
plugin now has 15 models from the pymc-challenges case studies. The models are for images,
fields, maps and graphs. New plot recipes show maps, networks, trees, angles and images.
New workflow nodes check the posterior, compare models by stacking weights and draw plots
in a run. Six new recipes use these nodes. For example, a recipe fits a ladder of spatial
models, keeps the best model and tells if the effect is clear.

Each ported case has a tutorial. The tutorial does the analysis of the notebook in the web
app, with no PyMC code. An export helper writes the data of a case to a file that you can
upload.

<div class="grid cards" markdown>

-   :material-atom-variant:{ .lg .middle } **15 science models**

    ---

    v0.9 adds eleven models to the four models of v0.7. They fit a heat equation, Turing
    patterns, molecule positions in STORM frames, features of small images, colour names,
    the CMB sky, disease maps, networks, graphical models, phylogenies and angles.

    [:octicons-arrow-right-24: Scientific models](reference/models.md#scientific-models-mapi-plugin-science)

-   :material-map-outline:{ .lg .middle } **14 new plot recipes**

    ---

    The new recipes draw area maps, networks, phylogenies, polar roses, image residuals,
    predictive image grids, colour swatches, power spectra and point patterns. Five new core
    recipes give corner plots, ridges, rank matrices, calibration and state sequences. The
    catalog now has 36 recipes.

    [:octicons-arrow-right-24: The plot catalog](reference/plots.md#scientific-model-pack-sci)

-   :material-graph-outline:{ .lg .middle } **Posterior checks in a workflow**

    ---

    Five new nodes: `model.summary`, `evaluate.ppc`, `compare.stacking`, `model.score` and
    `plot.render`. A condition can read a posterior summary. A report can contain plots.
    The catalog now has 56 nodes.

    [:octicons-arrow-right-24: Check and weigh Bayesian models](guides/workflows.md#check-and-weigh-bayesian-models)

-   :material-stairs:{ .lg .middle } **Six new workflow recipes**

    ---

    Model ladders for maps, networks and phylogenies, calibrate-then-design for a heated
    plate, a Bayesian workflow audit and an image model check. The gallery now has ten
    recipes.

    [:octicons-arrow-right-24: Complex workflows](tutorials/complex-workflows.md)

-   :material-school-outline:{ .lg .middle } **A tutorial for each case**

    ---

    Fifteen tutorials, from E07 (earthquakes in Oklahoma) to E81 (wind and animal activity).
    Each tutorial exports the data, uploads it, fits the model, reads the Model Explorer,
    scores the model and runs the recipe of the case.

    [:octicons-arrow-right-24: pymc-challenges in the web app](tutorials/pymc-challenges/index.md)

-   :material-database-export-outline:{ .lg .middle } **An export helper for the data**

    ---

    `python -m mapi_plugin_science.export` reads the data of a notebook and writes a file
    that you can upload. It can also write the suggested spec, or simulated data if you do
    not have the data of pymc-challenges.

    [:octicons-arrow-right-24: Export the data of a case study](reference/models.md#export-the-data-of-a-case-study)

</div>

## The science models

All the models are in `mapi-plugin-science`. Each model has an About card, a section in the
[model reference](reference/models.md#scientific-models-mapi-plugin-science), a conformance
fixture and tests against the real data of its notebook.

| Data | Model | Case | What it does |
|---|---|---|---|
| Fields and grids | `sci.lgcp_grid` | E07 | Event counts on a map over time |
| | `sci.fisher_kpp` | E50 | A reaction-diffusion PDE (Fisher-KPP) |
| | `sci.traction_force` | E60 | Cell forces from the movement of a gel |
| | `sci.heat_plate` <span class="mapi-badge">new</span> | E51 | The 2-D steady heat equation of a heated plate. It predicts the field of a new heater layout. |
| | `sci.turing_pattern` <span class="mapi-badge">new</span> | E61 (part A) | Schnakenberg rates from early images of a Turing pattern, with P(Turing) and simulated patterns |
| | `sci.cmb_field` <span class="mapi-badge">new</span> | E79 | The CMB sky behind a mask, and its bandpowers (Wandelt-Eriksen Gibbs sampler) |
| Images | `sci.strong_lens` | E80 | A galaxy-scale strong gravitational lens |
| | `sci.storm_localisation` <span class="mapi-badge">new</span> | E58 | Molecule positions in raw EMCCD STORM frames, with a super-resolved render |
| | `sci.ibp_features` <span class="mapi-badge">new</span> | E31 | Shared binary features of small images (truncated Indian buffet process) |
| Maps of areas | `sci.areal_bym2` <span class="mapi-badge">new</span> | E18 | Disease risk on a graph of areas, with iid, ICAR, proper CAR or BYM2 effects |
| Graphs and trees | `sci.network_latent_space` <span class="mapi-badge">new</span> | E21 | Ties of a network from pair covariates, sociality and latent positions |
| | `sci.graphical_model` <span class="mapi-badge">new</span> | E19 | Direct links between variables (dense LKJ or horseshoe), with edge probabilities |
| | `sci.phylo_regression` <span class="mapi-badge">new</span> | E23 | Regression on a phylogeny, with iid, Brownian, Pagel lambda or OU residuals |
| Other | `sci.colour_naming` <span class="mapi-badge">new</span> | E77 | How the speakers of a language name colour chips, pooled or with speaker effects |
| | `sci.circular_regression` <span class="mapi-badge">new</span> | E81 | Regression of angles (von Mises, projected normal, mixtures of regimes) and activity densities |

An output can now be an angle (`OutputDecl.circular`). The holdout metrics and the score
summaries of an angle use the circle. For example, the mean of 350 and 10 degrees is 0
degrees, not 180 degrees.

## The new plots

| Pack | Recipes | Library |
|---|---|---|
| Core | `core.pair`, `core.ridge`, `core.rank_matrix`, `core.calibration`, `core.state_sequence` | Vega-Lite |
| Science | `sci.area_map`, `sci.phylogeny`, `sci.rose`, `sci.image_residuals`, `sci.ppc_image_grid`, `sci.swatches`, `sci.power_spectrum`, `sci.point_pattern` | Plotly |
| Networks | `net.graph` (a node-link plot and an adjacency matrix) | Plotly |

Each recipe has an About card, an example and a gallery image. **Catalog → Plots** has a new
**Networks** filter.

## The new workflow nodes

| Node | What it gives |
|---|---|
| `model.summary` | Posterior summaries, R-hat, ESS and divergences. A condition can read `nodes.<id>.params[...]`. The parameter `focus` selects one value, for example `beta[north]`. |
| `evaluate.ppc` | A posterior predictive check: coverage, the PIT KS distance and Bayesian p-values |
| `compare.stacking` | Stacking and pseudo-BMA+ weights from the pointwise `elpd` that `model.fit` now keeps |
| `model.score` | A score mode of a model version, as a dataset |
| `plot.render` | A recipe of the catalog, as a `plot` output |

`model.forecast` and `evaluate.residuals` now also give plots. `report.summary` has a
`plots` input, and the web run view shows the plots of nodes and reports. `model.fit` and
`model.backtest` take `nodes` and `join`, so that a workflow can use a tree dataset.

## The new recipes

| Recipe | Title in the gallery | What it does |
|---|---|---|
| `sci_spatial_ladder` | Disease map: which spatial model? | Fits `sci.areal_bym2` with iid, ICAR and BYM2 effects. It keeps the best model by `elpd_loo`. If the chains agree and the effect is clear, it fits the model again with wider and narrower priors. |
| `sci_network_ladder` | Network ties: which model? | Fits `sci.network_latent_space` with covariates, sociality and a latent space. It gives the pair effects of the best model, a network plot and simulated networks. |
| `sci_phylo_signal` | Trait regression across species, with and without the tree | Fits `sci.phylo_regression` with four residual kinds. It ranks them, selects the best and tells if one slope is clear. |
| `sci_calibrate_then_design` | Calibrate a heated plate, then test a heater layout | Fits `sci.heat_plate` with one run held out. It predicts the field of a new heater layout only if the holdout error and R-hat are in their limits. |
| `bayes_workflow_audit` | Bayesian workflow audit | The checks of a careful Bayesian workflow for a model that gives draws. A gate on R-hat and ESS controls the predictive, prior sensitivity and recovery checks. |
| `sci_image_model_check` | Image model check | Residual maps and predicted images next to the observed image, with the posterior predictive check |

The [workflow cookbook](guides/workflow-cookbook.md#disease-map-which-spatial-model) has a
section for each recipe, with the CLI and the Python client. The
[complex workflows tutorial](tutorials/complex-workflows.md) shows the patterns of the
recipes in the workflow editor: model ladders, gates, predictive checks, governance checks,
calibrate-then-design, plots in reports, tree datasets and sub-workflows.

## A case from start to end

The E18 tutorial makes a disease map of lip cancer in the 56 districts of Scotland. These
commands export the data and the suggested spec, then run the spatial ladder:

```bash
uv run python -m mapi_plugin_science.export e18 \
    --challenges <path to pymc-challenges>/data \
    --out scotland_lip_cancer.nc --spec scotland_lip_cancer.yaml
mapi datasets upload scotland_lip_cancer.nc --name scotland_lip_cancer
mapi workflows init sci_spatial_ladder --name disease-map-ladder
mapi workflows apply disease-map-ladder.yaml
mapi workflows run disease-map-ladder \
  -i data=dataset://epidemiology/scotland_lip_cancer@latest -p engine=nutpie --watch
```

We did the E18 tutorial in the web app on a local dev stack, with the real data. These are
the results:

- The BYM2 fit has a largest R-hat of 1.006 and an `elpd_loo` of −153.0. The notebook gives
  −152.7.
- The effect of outdoor work is `b1` = 0.406 (90% interval 0.201 to 0.625). Thus each 10
  points of outdoor work multiply the risk by about 1.5.
- The recipe took 46 s. It ranked the models `icar`, `bym2`, `iid`. The effect stayed clear
  with wider and narrower priors (`b1` 0.379 and 0.366).

On the 186 primates of the E23 data, `sci_phylo_signal` selects Pagel's lambda with an
`elpd_loo` of 129.9. The notebook gives 130.

## Upgrading from 0.8

- **Install.** `mapi-workflows[runtime]` now depends on `mapi-plots[recipes]`. Install
  `mapi-plugin-science` to get the `sci.*` models.
- **Workflows.** The new node kind is `plot`. Workflow validation finds the `recipe` of
  `plot.render` in the bundled recipes and the plot catalog. If it does not find the recipe,
  it gives `REF_UNRESOLVED`. The server sanitizes each plot of a node. An unsafe plot fails
  its node with `PLOT_UNSAFE`.
- **Fits keep the pointwise elpd.** The SDK `MetricsReport` has an `elpd` field, with a
  maximum of 10 000 points.
- **Score errors are earlier.** A score request in a mode that the model does not support
  gives `MODE_NOT_SUPPORTED` before any other work. A horizon along text labels gives
  `COORD_NOT_ALLOWED`.
- **Angles.** The score summaries of a circular output are on the circle, and
  `summary.json` has a new field `period`. The difference between a scenario and its
  baseline is wrapped to `[-period/2, period/2)`.
- **Scorecard.** The check "Few missing values" (`data.missing`) counts only the variables
  that contain observations. A model can declare variables with gaps that are correct in
  `CustomModel.structural_missing`.
- **Score page.** **No new data** shows only for Forecast, or when a scenario dataset gives
  the values. For the other modes, a note tells why the option is not there.
- **Plugins.** `EnginePlan` (`mapi-pymc`) has `initvals` and `jitter`. `BindingMap` now
  changes the dim names in the `grid` attr of a variable when it changes the dims.

## What is not in 0.9 yet

- `governance.check` with `sensitivity.prior` cannot find the prior widths of
  `sci.areal_bym2`. The check is skipped. The spatial ladder fits the model again with
  different widths in its place.
- The graph check of the network ladder shows only the statistics of the simulated networks.
  It does not mark the statistics of the observed network.
- `core.prior_predictive` cannot draw the prior draws of `sci.heat_plate`. Some science
  models (for example `sci.cmb_field` and `sci.phylo_regression`) keep no prior draws. The
  audit then shows a failed prior plot and continues.
- A backtest of a tree dataset gives the full tree to each fold. A tree-input model that
  reads its target from the tree can see the test window. The node shows a warning.
- `search.grid` has no `nodes` parameter.
- The HTML report does not show plot sections. Only the JSON report and the web run view
  show them.
- The tutorials cover 15 of the 88 notebooks of pymc-challenges. The E61 tutorial covers
  only part A of its notebook.
- Only `sci_spatial_ladder` ran from start to end on a live dev stack and in a browser. The
  six new recipes run through the server orchestrator in a slow test, with small fits. We did
  not run 0.9 on real Google Cloud or in Docker.

The section `[0.9.0]` of `CHANGELOG.md`, in the root of the repository, gives the full list
of changes and repairs.
