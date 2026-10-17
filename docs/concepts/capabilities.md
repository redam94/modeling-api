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
| `checkpointable` | Long fits can write checkpoints and resume. |
| `conditions_on_actuals` | Forecasts can have actuals. The model updates its state with them through its fitted parameters. Then it forecasts from the last actual ([Forecasts with actuals](forecasting-with-actuals.md)). Without this capability, a forecast payload with target values gives `ACTUALS_NOT_SUPPORTED`. |

## The manifest

| Field | What it declares |
|---|---|
| `id`, `version` | `mmm.bayesian`, `1.0.0`. Specs select a version with a semver range: `use: mmm.bayesian@^1`. |
| `schema_template` | The input data contract, in canonical names. It can change with the settings. Then it is a template with `params`, for example `{group_dim}`. The settings of the model give the values of these `params`. |
| `settings_schema`, `fit_schema` | JSON Schemas of `model.settings` and `model.fit`. Each field can have `x-ui-level: basic` or `advanced`. Guided mode shows only `basic`. |
| `outputs` | Output variables with dims and units (`units_of: y` means "in the units of the target") |
| `derived` | Derived quantities. It also tells if the fit caches them (`cache: fit`), or if the platform calculates them when necessary. |
| `plots` | The plot recipes that the report of this model shows by default |
| `resources` | Minimum and default resource class, estimated fit time, GPU |

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
