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
| `pathfinder` | Pathfinder from pymc-extras (one chain). Install the extra `mapi-pymc[pathfinder]`. The runner images `mapi/base-bayes` and `mapi/base-forecast` have it. Without the extra, the fit stops with `CAPABILITY_UNSUPPORTED`. |
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

To get `elpd_loo`, keep the log-likelihood (`keep: {log_likelihood: true}`) and list
`elpd_loo` in `evaluate.metrics`. The fit then also keeps the pointwise values in
`MetricsReport.elpd`, for data of up to 10,000 points. The workflow node `compare.stacking`
uses these values to calculate stacking weights. If LOO is not correct for your model (for
example, all observations share one latent field), declare `loo_valid=False` and the reason
in `loo_invalid_reason` in the `Capabilities` of the model. The fit then skips `elpd_loo` with
that reason.

### The loss history of ADVI <span class="mapi-badge">v0.10</span> { #advi-loss-history }

An ADVI fit keeps the loss of each optimization step in the `fit_history` group of the bundle.
The loss is the negative ELBO. A lower value is better. The group has one variable, `loss`, over
the dim `step` (1 to the number of steps). If the fit has more than 5,000 steps, the group keeps
5,000 evenly spaced steps and the last step. The attr `thinned_from` then gives the number of
steps of the fit.

The fit run adds the plot `core.loss_curve` to each fit that has this group. The plot shows the
raw loss and a moving average. If the moving average still goes down by more than 1% over the
last 10% of the steps, the caption tells you so. Then increase `tune` and fit again.

`map` and `pathfinder` do not keep a loss history.

From v0.11, ADVI also sends the loss to the run while it optimises. The run gets the metric
`loss` with the optimisation step as its step. For a fit of more than 5,000 steps, it sends
each k-th step, k = ceil(steps / 5000), and the last step. The **Metrics** tab of the run page
shows the curve while the fit runs.

### Live chains and step metrics <span class="mapi-badge">v0.11</span> { #live-chains }

While nutpie or the PyMC NUTS sampler makes draws, the fit sends the progress of each chain
to the run. The run page shows one lane for each chain, with its draws, divergences and step
size. The fit also logs these step metrics, with the label `chain`:

| Engine | Keys | Step |
|---|---|---|
| `nutpie` | `step_size`, `num_steps` (leapfrog steps of the latest draw), `divergences` (cumulative) | The draws that the chain finished, with tuning (starts at 1) |
| `pymc` (NUTS) | `step_size`, `tree_depth`, `num_steps`, `divergences` (cumulative) | The draw index, with tuning (starts at 1). If a chain has more than 5000 draws, each k-th draw and the last draw, k = ceil(n / 5000) |

The evaluation fit of `evaluate_then_refit` adds the label `fit=evaluation`. Thus its curves
do not mix with the curves of the final fit. Its params get the prefix `evaluation.`. Your model code can log its own metrics, params
and tags with `ctx` (see [Track metrics, params and tags](tracking.md)). The SDK does not
build the step metrics when no tracker records them, for example in a notebook fit without a
recorder.

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

### Start every chain at one point

By default, each MCMC chain starts at the initial point of the model with a random jitter.
Some posteriors have a trap near that point. An example is a feature model where all the
features switch off. For such a model, set two fields of `EnginePlan` (`mapi_pymc`):

* `initvals`: the start values, by the name of each free variable, on the constrained scale
  (as `pm.sample(initvals=...)`).
* `jitter`: `False` starts each chain at exactly that point. The default is `True`.

`engine: pymc`, `nutpie` and the external NUTS bridges (`numpyro`, `blackjax`) use the two
fields. `map`, `advi` and `pathfinder` ignore them. `PyMCModel.fit` does not set them. Thus a
model that needs a start point builds its `pm.Model` in a `CustomModel` and calls
`run_engine` itself, as `sci.ibp_features` does:

```python
from dataclasses import replace
from mapi_pymc import EngineHooks, EnginePlan, run_engine

plan = EnginePlan.from_config(cfg, default_engine="nutpie", seed=seed)
plan = replace(plan, initvals={"A": a0, "nu": nu0}, jitter=False)
result = run_engine(model, plan, EngineHooks(progress=ctx.progress, cancel=ctx.cancel))
posterior = result.group("posterior")
```

The convergence checks then need a call of `mapi_pymc.convergence_diagnostics` in the
`diagnostics` hook of the model (see [Write a step or a model plugin](plugins.md)).

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

The governance check `recovery.simulated` uses these predictive draws. For a Bayesian model
with a `predict` mode, it makes each simulated dataset from one posterior draw and the noise
of the model (`predict` with `kind: predictive`). Then it fits the model again and finds the
values of that draw. If the model gives no predictive draws, or if the draws have the
attribute `kind: expected`, the check adds residuals to the fitted values. If the target holds counts and the model has no predictive draws, the check
is skipped. Thus give real predictive draws, also for counts.

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
