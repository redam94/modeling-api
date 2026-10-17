# Bring a PyMC model

With `mapi_pymc.PyMCModel`, you write one method, `build()`. It returns a `pm.Model`. The
platform does the rest of the work:

- It fits the model with the engine that the spec names.
- It keeps the ArviZ groups that you ask for.
- It changes the convergence checks into scorecard items.
- It predicts on new data, because it builds your model again: new geos, forecast horizons and
  counterfactual scenarios.

You never work with coordinates, `pm.set_data` or the records of the posterior.

## A hierarchical model in 30 lines

This example is the classic radon model (intercepts that change by county) in the long layout.
Each `obs` row has a `county_idx` that points into the `county` coordinate.

```python
--8<-- "plugin_pymc.py:model"
```

`mapi_pymc.examples.RadonModel` is a version that you can run, with synthetic data.

### What the `ctx` helpers do

| Helper | At fit | At score |
|---|---|---|
| `ctx.coords()` | The labels of the data | Adds `<dim>__fit` and `<dim>__new` when unseen levels come in |
| `ctx.data(name)` | `pm.Data` over the training values | `pm.Data` over the score values (the platform builds the model again) |
| `ctx.observed(name)` | The target | A zero placeholder of the correct shape (never imputed) |
| `ctx.hier_effect(name, dim)` | `name ~ Normal(mu, sigma)` over `dim` (non-centred by default) | The fit levels from the posterior, then `name__new` for unseen levels |
| `ctx.position(dim)` | 0, 1, 2, … along an `extend` dim | The same positions, continued into the horizon |
| `ctx.anticipated_coords` | The fit labels and `max_extension` future labels | The same |

The `new_level_strategy` of the dim controls new levels:

* `hyperprior`: each posterior draw gets a new level `mu + sigma * z` from the fitted
  hyperprior. The noise `z` stays the same for each seed. Thus, if you score two times, you get
  the same answer.
* `pooled`: the population mean `mu`.
* `zero`: `0`.
* `error`: the platform rejects the request with `UNSEEN_LEVELS`.

Your model can use a `new_levels` dim as the index of a plain parameter
(`pm.Normal("a", dims="county")`). If you then score a new county, the score fails with a clear
`UNSEEN_LEVELS` error. The error tells you to use `ctx.hier_effect`.

## Choosing an engine

Set `model.fit.engine` in the spec:

| Engine | What you get |
|---|---|
| `nutpie` (default) | Fast NUTS with parallel chains. A seed does not make the result the same bit for bit. |
| `pymc` | The NUTS of PyMC. A seed makes the result the same. It has **segmented sampling**. |
| `map` | `pm.find_MAP`: one point (1 chain, 1 draw). |
| `advi` | Mean-field ADVI. `tune` is the number of optimization steps (default 10,000). |
| `pathfinder` | Pathfinder from pymc-extras, if the image has it. |
| `numpyro`, `blackjax` | The JAX bridges of PyMC, if the image has them. |

```yaml
model:
  use: acme.radon@^1
  fit:
    engine: pymc
    tune: 1000
    draws: 2000
    chains: 4
    seed: 20261014
    checkpoint: {every_draws: 500}      # segmented sampling
    keep: {log_likelihood: true, posterior_predictive: {thin: 4}}
    prior_predictive: {draws: 500}
```

You can add these optional groups:

* `prior` and `prior_predictive` (`prior_predictive: {draws: N}`)
* `log_likelihood`, which LOO and WAIC need
* A thinned `posterior_predictive`

The bundle always contains `posterior`, `sample_stats`, `observed_data` and `constant_data`.

### Segmented sampling and checkpoints

With `engine: pymc` and `checkpoint.every_draws`, the sampler tunes one time. Then it makes the
draws in chunks. After each chunk, it writes a checkpoint with three items:

* The draws until that time
* The position of each chain
* The full state of the sampler (step size, mass matrix and random state), encoded as JSON.
  The sampler never uses pickle.

If a run stops and starts again, it continues from the end of the last chunk. The result is the
same as the result of a run that did not stop.

nutpie cannot export its adaptation state. Thus its checkpoints are *snapshots*. A snapshot
contains the draws until that time. This is sufficient for a partial result. But when the run
starts again, the sampler starts from the beginning.

Each engine reports the progress of each chain ("chain 2/4: sampling 150/500, 0 divergences").

### Diagnostics

Each MCMC fit adds these items to the sanity scorecard. Each item has a message for experts, a
message in plain language and a suggested action:

* `RHAT`: the rank-normalised split r̂ is less than 1.01.
* `ESS_BULK` and `ESS_TAIL`: each chain has 100 effective draws or more.
* `DIVERGENCES`
* `TREE_DEPTH`

## Expected vs predictive, and counterfactuals

`output.kind: expected` returns the Deterministic that you named in `expected` (without
observation noise). `output.kind: predictive` returns draws of the observed variable.

Predictive draws use **common random numbers**: `y = F⁻¹(U)`. The seed and the cell set the
uniforms `U`, and they do not change. A counterfactual uses `mode: counterfactual` with
`interventions`. In a counterfactual, the scenario and the baseline thus use the same noise. The
delta then shows only the intervention, not two independent noise draws. Some distributions do
not have an inverse CDF (Poisson, negative binomial). For these distributions, the platform uses
the seeded stream of the sampler. The `noise` attribute of the output records the method that
the platform used.

## Fast scoring with a FrozenPredictor

If you declare a `FrozenPredictorSpec`, the platform can compile one PyTensor function. This
function calculates your responses for many inputs and scenarios at the same time. Use it for
response curves and what-if grids:

```python
--8<-- "pymc_fast.py:fast"
```

The contract check `mapi_pymc.testing.check_fast_score(plugin, fitted, ds)` makes sure that the
fast path agrees with the exact scores within the Monte Carlo error.

## Test it

Use the contract kit of the SDK:

```python
--8<-- "plugin_pymc.py:check"
```

Keep the test fits very small (`tune: 100, draws: 100, chains: 2`, `engine: pymc`). Then the
tests are fast and give the same result each time.
