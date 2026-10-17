# Capabilities and manifests

A model plugin describes itself with a **manifest**: static JSON that says what data it needs,
what it can do, what it returns and which settings it takes. The control plane validates specs
and score requests against manifests, and the Guided UI builds its forms from them, all without
ever importing the plugin.

```python
--8<-- "concepts_capabilities.py:manifest"
```

## Capabilities

| Field | Meaning |
|---|---|
| `inference` | `mcmc`, `vi`, `map`, `smc`, `kalman`, `point`, `bootstrap` or `custom` |
| `representation` | `draws`, `quantiles`, `interval`, `point` or `moments`. See [Representations](representations.md) |
| `score_modes` | Which of `predict`, `forecast`, `counterfactual`, `simulate`, `filter`, `evaluate` the model supports. Anything else is `MODE_NOT_SUPPORTED` |
| `decision` | Has an `objective(fitted, decision)` returning a value per draw, for budget optimisation |
| `recommend` | Can recommend candidates (Bayesian optimisation) |
| `fast_score` | Provides a compiled fast-scoring path (PyMC `FrozenPredictor`) |
| `contribution_scale` | `additive` (contributions sum to the prediction; the contract kit checks it), `multiplicative` or `none` |
| `loo_valid`, `loo_invalid_reason` | Whether pointwise LOO is meaningful. An HMM's steps are dependent, so `ts.gaussian_hmm` declares it invalid and `elpd_loo` is rejected at validation |
| `identified_quantities` | Which parameters are identified (for models with label switching or rotations) |
| `owns` | Preprocessing the model does itself: `scaling`, `carryover`, `inverse_units`, `recursion`. The platform steps aside and labels outputs accordingly |
| `deterministic_given_seed` | Whether the same seed gives the same fit (nutpie: no). The contract kit checks it when claimed |
| `checkpointable` | Long fits can be checkpointed and resumed |

## The manifest

| Field | What it declares |
|---|---|
| `id`, `version` | `mmm.bayesian`, `1.0.0`. Specs pick a version with a semver range: `use: mmm.bayesian@^1` |
| `schema_template` | The input data contract, in canonical names. It can be a function of settings: a template with `params` such as `{group_dim}` that are filled from the model's settings |
| `settings_schema`, `fit_schema` | JSON Schemas of `model.settings` and `model.fit`. Every field may carry `x-ui-level: basic` or `advanced`; Guided mode shows only `basic` |
| `outputs` | Output variables with dims and units (`units_of: y` means "in the target's units") |
| `derived` | Derived quantities, and whether they are cached at fit (`cache: fit`) or computed on demand |
| `plots` | The plot recipes this model's report shows by default |
| `resources` | Minimum and default resource class, estimated fit time, GPU |

A schema template with parameters, from a model whose group dim is a setting:

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

Each step exports a **catalog entry**: its settings JSON Schema, kind, whether it is stateful or
invertible, which settings name variables, dims and side tables, its context need, and a
**schema effect template**. The runner images export the catalog when they are built, and the
API validates specs against it. Browse it in the [steps catalog](../reference/generated/steps-catalog.md).

```bash
python -m mapi_steps.catalog --splits > catalog.json   # what the control plane loads
```

## Browse the built-ins

- [Models catalog](../reference/generated/models-catalog.md): every installed model's
  capabilities, outputs, derived quantities and settings.
- [Plot recipes catalog](../reference/generated/plots-catalog.md).
- [Spec schema](../reference/generated/spec-schema.md): every field of `ModelSpec` and
  `ScoreRequest`.
