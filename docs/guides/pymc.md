# Bring a PyMC model

With `mapi_pymc.PyMCModel` you write one method, `build()`, that returns a `pm.Model`. The
platform fits it with the engine named in the spec, keeps the ArviZ groups you ask for,
turns convergence checks into scorecard items, and predicts by rebuilding your model on new
data: new geos, forecast horizons, counterfactual scenarios. You never handle coordinates,
`pm.set_data` or posterior bookkeeping yourself.

## A hierarchical model in 30 lines

This is the classic radon model (varying intercepts by county) in long layout. `obs` rows
carry a `county_idx` that points into the `county` coordinate.

```python
--8<-- "plugin_pymc.py:model"
```

A runnable version, with synthetic data, ships as `mapi_pymc.examples.RadonModel`.

### What the `ctx` helpers do

| Helper | At fit | At score |
|---|---|---|
| `ctx.coords()` | the data's labels | adds `<dim>__fit` and `<dim>__new` when unseen levels arrive |
| `ctx.data(name)` | `pm.Data` over the training values | `pm.Data` over the score values (the model is rebuilt) |
| `ctx.observed(name)` | the target | a zero placeholder of the right shape (never imputed) |
| `ctx.hier_effect(name, dim)` | `name ~ Normal(mu, sigma)` over `dim` (non-centred by default) | fit levels from the posterior, then `name__new` for unseen levels |
| `ctx.position(dim)` | 0, 1, 2, … along an `extend` dim | the same positions continued into the horizon |
| `ctx.anticipated_coords` | fit labels plus `max_extension` future labels | the same |

New levels follow the dim's `new_level_strategy`:

* `hyperprior`: every posterior draw gets a fresh level `mu + sigma * z` from the fitted
  hyperprior. The noise `z` is frozen per seed, so scoring twice returns the same answer.
* `pooled`: the population mean `mu`.
* `zero`: `0`.
* `error`: the request is rejected with `UNSEEN_LEVELS`.

If your model indexes a plain parameter by a `new_levels` dim (`pm.Normal("a", dims="county")`),
scoring a new county fails with a clear `UNSEEN_LEVELS` error that points you at
`ctx.hier_effect`.

## Choosing an engine

Set `model.fit.engine` in the spec:

| Engine | What you get |
|---|---|
| `nutpie` (default) | Fast NUTS with parallel chains. It is not bit-for-bit reproducible from a seed. |
| `pymc` | PyMC's NUTS, reproducible from a seed, with **segmented sampling**. |
| `map` | `pm.find_MAP`: one point (1 chain, 1 draw). |
| `advi` | Mean-field ADVI. `tune` is the number of optimisation steps (default 10,000). |
| `pathfinder` | Pathfinder from pymc-extras, when installed in the image. |
| `numpyro`, `blackjax` | PyMC's JAX bridges, when installed in the image. |

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

The opt-in groups are:

* `prior` and `prior_predictive` (`prior_predictive: {draws: N}`);
* `log_likelihood`, which LOO and WAIC need;
* a thinned `posterior_predictive`.

The bundle always holds `posterior`, `sample_stats`, `observed_data` and `constant_data`.

### Segmented sampling and checkpoints

With `engine: pymc` and `checkpoint.every_draws`, the sampler tunes once and then draws in
chunks. After each chunk it writes a checkpoint with three things:

* the draws so far;
* every chain's position;
* the full sampler state (step size, mass matrix and random state), encoded as JSON, never pickled.

If a run is interrupted and retried, it resumes where the last chunk ended. The result is
identical to an uninterrupted run.

nutpie cannot export its adaptation state, so its checkpoints are *snapshots*. They hold the
draws so far, which is enough for a partial result, but a retry starts sampling again.

Every engine reports progress for each chain ("chain 2/4: sampling 150/500, 0 divergences").

### Diagnostics

Each MCMC fit adds these items to the sanity scorecard, each with an expert message, a
plain-language message and a suggested action:

* `RHAT`: rank-normalised split r̂ below 1.01;
* `ESS_BULK` and `ESS_TAIL`: at least 100 effective draws per chain;
* `DIVERGENCES`;
* `TREE_DEPTH`.

## Expected vs predictive, and counterfactuals

`output.kind: expected` returns the Deterministic you named in `expected` (no observation
noise). `output.kind: predictive` returns draws of the observed variable.

Predictive draws use **common random numbers**: `y = F⁻¹(U)` with uniforms `U` frozen by the
seed and the cell. In a counterfactual (`mode: counterfactual` with `interventions`), the
scenario and the baseline therefore share their noise. The delta then reflects only the
intervention, not two independent noise draws. Distributions without an inverse CDF (Poisson,
negative binomial) fall back to the seeded sampler stream, and the output's `noise` attribute
records which method was used.

## Fast scoring with a FrozenPredictor

Declare a `FrozenPredictorSpec` and the platform can compile one PyTensor function that
evaluates your responses for many inputs and scenarios at once. Use it for response curves
and what-if grids:

```python
--8<-- "pymc_fast.py:fast"
```

The contract check `mapi_pymc.testing.check_fast_score(plugin, fitted, ds)` confirms that the
fast path agrees with exact scoring within Monte Carlo error.

## Test it

Use the SDK contract kit:

```python
--8<-- "plugin_pymc.py:check"
```

Keep test fits tiny (`tune: 100, draws: 100, chains: 2`, `engine: pymc`) so they are fast and
reproducible.
