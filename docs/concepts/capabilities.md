# Capabilities and manifests

A model plugin describes itself with a **manifest**. The manifest is static JSON. It tells
which data the plugin needs, what it can do, what it returns and which settings it accepts.
The control plane validates specs and score requests against manifests. The Guided UI makes
its forms from manifests. Neither of them imports the plugin.

```python
--8<-- "concepts_capabilities.py:manifest"
```

## Capabilities

| Field | Meaning |
|---|---|
| `inference` | `mcmc`, `vi`, `map`, `smc`, `kalman`, `point`, `bootstrap` or `custom` |
| `representation` | `draws`, `quantiles`, `interval`, `point` or `moments`. See [Representations](representations.md) |
| `score_modes` | The modes from `predict`, `forecast`, `counterfactual`, `simulate`, `filter`, `evaluate` that the model can do. All other modes give `MODE_NOT_SUPPORTED`. |
| `decision` | The model has an `objective(fitted, decision)` that returns a value for each draw, for budget optimisation. |
| `recommend` | The model can recommend candidates (Bayesian optimisation). |
| `fast_score` | The model has a compiled path for fast scoring (PyMC `FrozenPredictor`). |
| `contribution_scale` | `additive`, `multiplicative` or `none`. With `additive`, the sum of the contributions is the prediction. The contract kit checks this. |
| `loo_valid`, `loo_invalid_reason` | Tells if pointwise LOO has a meaning. The steps of an HMM are dependent. Thus `ts.gaussian_hmm` declares LOO invalid, and validation rejects `elpd_loo`. |
| `identified_quantities` | The parameters that are identified (for models with label switching or rotations). |
| `owns` | The preprocessing that the model does itself: `scaling`, `carryover`, `inverse_units`, `recursion`. The platform does not do these steps, and it labels the outputs to agree. |
| `deterministic_given_seed` | Tells if the same seed gives the same fit (nutpie: no). If a model claims this, the contract kit checks it. |
| `checkpointable` | Long fits can write checkpoints and resume. A sampler saves a checkpoint every N draws (`fit.checkpoint.every_draws`). From v0.15, a network saves one every N epochs (`fit.checkpoint.every_epochs`). If a spec sets `every_epochs` for a model that cannot save checkpoints, validation shows a warning. |
| `calibration` | The calibration data that the model uses from the `calibration` block of a spec: `business_priors`, `lift_tests` or both. |
| `input` | `dataset` (the default), `tree` or `collection` (v0.15). A `tree` model also gets the full tree dataset, next to the flat output of the pipeline ([Tree datasets](../guides/tree-data.md#models-that-read-the-whole-tree)). A `collection` model trains on batches of all images of an image collection (`fit_batches`, [Train on batches](../guides/plugins.md#fit-batches)). It also reads plain datasets. |
| `conditions_on_actuals` | Forecasts can have actuals. The model updates its state with them through its fitted parameters. Then it forecasts from the last actual ([Forecasts with actuals](forecasting-with-actuals.md)). Without this capability, a forecast payload with target values gives `ACTUALS_NOT_SUPPORTED`. |

## The manifest

| Field | What it declares |
|---|---|
| `id`, `version` | `mmm.bayesian`, `1.0.0`. Specs select a version with a semver range: `use: mmm.bayesian@^1`. |
| `schema_template` | The input data contract, in canonical names. It can change with the settings. Then it is a template with `params`, for example `{group_dim}`. The settings of the model give the values of these `params`. |
| `settings_schema`, `fit_schema` | JSON Schemas of `model.settings` and `model.fit`. Each field can have `x-ui-level: basic` or `advanced`. Guided mode shows only `basic`. |
| `outputs` | Output variables with dims and units. `units_of: y` means "in the units of the target". Two optional fields tell how to show and summarise an output: `grid` and `circular` (see [Outputs on a grid or a circle](#outputs-on-a-grid-or-a-circle)). |
| `derived` | Derived quantities. It also tells if the fit caches them (`cache: fit`), or if the platform calculates them when necessary. |
| `plots` | The standard plots of the model. The **Insert** panel of the spec editor shows them first (v0.10). A declared plot is not drawn automatically. A fit draws the plots in `outputs.plots` of the spec. It also adds the plots of a training history that the bundle keeps: `core.loss_curve` (ADVI) and, from v0.15, `ml.training_curves`, `ml.confusion_epochs` and `ml.misclassified`. |
| `resources` | Minimum and default resource class, estimated fit time, and `gpu` (the model can use the `GPU` class, v0.15) |
| `side_table_params` | v0.15: the settings whose values name side tables that the model reads, for example pretrained weights ([Read a side table in a model](../guides/plugins.md#model-side-tables)) |
| `about`, `family` | The [About card](about-and-choosing.md) in plain language, and the model family that governance policies match (`mmm`, `forecast`, `ts_multivariate`, `tabular`, `choice`, `regime`). If `family` is not set, it is the namespace of the id, for example `causal` or `dl`. |

This example is a schema template with parameters. It is from a model that has the group dim
as a setting:

```yaml
params:
  group_dim: {type: string, default: geo}
  time_dim:  {type: string, default: time}
template:
  dims:
    "{group_dim}": {kind: categorical, policy: new_levels, new_level_strategy: pooled}
    "{time_dim}":  {kind: time, policy: extend}
  vars:
    y: {dims: ["{group_dim}", "{time_dim}"], dtype: float64, role: target}
```

## Outputs on a grid or a circle

An output can declare a **`grid`** (`OutputDecl.grid`). The output is then a 2-D field, for
example an image or a map. The grid has these fields:

| Field | Meaning |
|---|---|
| `y`, `x` | The dims of the rows and the columns. These dims must be dims of the output. |
| `frame` | Optional. A third dim with one field for each label (for example years or times). |
| `component` | Optional. A dim of length two with the `(x, y)` parts of a vector field. |
| `origin` | `upper` (the default: row 0 is at the top) or `lower` |
| `colormap` | `viridis` (the default), `magma`, `cividis`, `gray` or `diverging` |
| `log` | `true` shows log10 of the value, for counts and intensities. The default is `false`. |

The **Fields** section of the [Model Explorer](../guides/model-explorer.md) shows each grid
output with its uncertainty. The `sci.*` plot recipes also read the grid
([Scientific models](../reference/models.md#scientific-models-mapi-plugin-science)).

An output can declare **`circular: true`** (`OutputDecl.circular`). The output is then an
angle, for example a direction or a time of day. The model sets a `period` attr (one turn, in
the units of the output) on the scored array. The platform then does these calculations on
the circle:

- The holdout metrics use the circular mean, errors wrapped to half a turn, intervals around
  the circular mean and an arc-distance CRPS. The metrics `mape`, `smape`, `wape` and `r2`
  are skipped.
- The score summaries of draws use the circular mean and intervals centred on it
  ([Circular outputs](representations.md#circular-outputs)).

## The default plots of a score run follow the outputs <span class="mapi-badge">v0.10</span> { #score-plots-by-output-shape }

A score run draws default plots when the job names no plots. Before v0.10, each mode had one
default, for example a forecast fan for `predict` and `forecast`. A fan of a map adds the map
over all cells, so it hides the result. From v0.10, the runner selects the defaults from the
declaration and the dims of each output:

| Output | Default plot |
|---|---|
| A `grid` output (the result has the `y` and `x` dims of the grid) | `sci.field_map`. The `frame` dim (for example `quarter`) is the slider. |
| A `grid` output with a `component` of size 2 | `sci.vector_field` |
| An output on areas (the bundle has the polygons `ring_x`, `ring_y`) | `sci.area_map` |
| Probabilities over a dim named `state` or `regime` (each cell has a sum of 1) | `core.state_sequence` |
| `evaluate`: an output with exactly two different observed values and mean predictions in [0, 1] | `core.calibration`, `core.roc_pr` and `core.confusion`, in place of `core.fit_vs_actual` and `core.residuals_over_time` |
| An output over time that is not a grid, with a maximum of one other dim | `core.ppc_fan` (`predict` and `forecast`) |

These rules also apply:

- A model with grid or area outputs gets the fan only for an output over time, for example
  `total[period]`. A model without such an output gets no fan.
- A model with no grid or area outputs always gets the fan for `predict` and `forecast`.
- `evaluate` adds `core.residuals_over_time` only when an output is over time.
- `counterfactual` keeps `core.counterfactual`.
- A list of plots in the score job replaces all defaults.

A dim named `state` is a hidden state only when its values are probabilities. Thus a panel
dim such as a US `state` with sales does not get `core.state_sequence`.

A predict, forecast or evaluate run of a model with grid outputs also writes
`ui/fields.json` (`FieldsView`). This is the same field view as in the
[Model Explorer](../guides/model-explorer.md), made from the predictions. The output
variables of a grid in `score.zarr.zip` carry the attr `grid` (the `GridDecl` as JSON). Thus
you can draw a downloaded result as a map. Each table of `summary.json`
(`ScoreSummaryTable`) gives the ids of the plots that draw it (`plots`), its coordinate
columns (`dims`) and its time dim (`time_dim`). The **Score** page of the web app uses these
fields to show a view for each output: a map with a frame slider, a line with a band,
intervals or tiles ([The web app](../guides/web-ui.md#score)).

## Example: the science models

The 15 models of `mapi-plugin-science` all use `inference: mcmc` and `representation: draws`.
Their score modes are different:

| Score modes | Models |
|---|---|
| `predict`, `forecast`, `evaluate` | `sci.lgcp_grid`, `sci.fisher_kpp` |
| `predict`, `simulate`, `evaluate` | `sci.strong_lens`, `sci.heat_plate`, `sci.turing_pattern`, `sci.colour_naming`, `sci.cmb_field`, `sci.areal_bym2`, `sci.network_latent_space`, `sci.graphical_model`, `sci.phylo_regression`, `sci.circular_regression` |
| `predict`, `evaluate` | `sci.traction_force`, `sci.storm_localisation`, `sci.ibp_features` |

Some of these models also show other capabilities:

- Most of them declare a `grid` on one or more outputs. `sci.areal_bym2` and
  `sci.phylo_regression` have no grid outputs.
- `sci.circular_regression` declares `direction_hat` and `mean_direction` as circular.
- `sci.strong_lens` has `input: tree`.
- `sci.traction_force`, `sci.strong_lens` and `sci.cmb_field` declare LOO invalid
  (`loo_valid: false`). Thus validation refuses `elpd_loo` for them.

A score request for a mode that the model does not declare gives `MODE_NOT_SUPPORTED`. The
platform refuses it before it does work for that mode. The
[models reference](../reference/models.md) gives the outputs and settings of each model.

## Example: the causal and deep-learning models

The models of v0.13 and v0.15 show the capabilities for draws without MCMC, for batches and
for the GPU:

| Model | `inference` | Score modes | `input` | `resources.gpu` | `loo_valid` |
|---|---|---|---|---|---|
| `ts.bsts` | `mcmc` | `predict`, `forecast`, `filter`, `evaluate` | `dataset` | no | no |
| `causal.impact` | `mcmc` | `predict`, `forecast` | `dataset` | no | no |
| `causal.synthetic_control` | `mcmc` | `predict` | `dataset` | no | no |
| `causal.did` | `mcmc` | `predict`, `counterfactual` | `dataset` | no | yes |
| `dl.mlp` | `custom` | `predict`, `evaluate` | `dataset` | no | no |
| `dl.cnn_classifier` | `custom` | `predict`, `evaluate` | `collection` | yes | no |
| `dl.finetune_classifier` | `custom` | `predict`, `evaluate` | `collection` | yes | no |

Some notes on these models:

- All of them have `representation: draws` and `checkpointable: true`. The `dl.*` models get
  their draws from a Laplace approximation of the last layer or from MC dropout, not from a
  sampler ([Deep learning](../guides/deep-learning.md#draws)).
- The `dl.*` models declare `deterministic_given_seed: true`. The samplers of the other
  models do not.
- `dl.finetune_classifier` has `side_table_params: [pretrained]`. Its `pretrained` setting
  names the weights dataset that it reads ([Image data and collections](image-collections.md#weights)).
- `resources.gpu: true` lets a spec select the `GPU` class. Validation refuses the `GPU`
  class for a model without it, because the GPU Job runs only these models
  ([Quotas and cost](quotas-and-cost.md#resource-classes)).
- The causal models write the `impact` group ([Measure the effect of a change](causal-effects.md)).

## Steps have manifests too

Each step exports a **catalog entry**. The entry contains these items:

- the settings JSON Schema and the kind of the step
- if the step is stateful or invertible
- the settings that name variables, dims and side tables
- the context that the step needs
- a **schema effect template**.

The runner images export the catalog when they are built. The API validates specs against
the catalog. To see the catalog, read the [steps catalog](../reference/generated/steps-catalog.md).

```bash
python -m mapi_steps.catalog --splits > catalog.json   # what the control plane loads
```

## Browse the built-ins

- [Models catalog](../reference/generated/models-catalog.md): the capabilities, outputs,
  derived quantities and settings of each installed model.
- [Plot recipes catalog](../reference/generated/plots-catalog.md).
- [Spec schema](../reference/generated/spec-schema.md): all fields of `ModelSpec` and
  `ScoreRequest`.
