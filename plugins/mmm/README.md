# mapi-plugin-mmm

Marketing-mix model plugins for modeling-api, registered through the `mapi.models` entry
point (`mapi_plugin_mmm.plugins:PLUGINS`):

- **`mmm.hierarchical_geo`** (PyMC): the E33 hierarchical geo MMM. Adstock over a
  `core.lag_tensor`, logistic or Hill saturation, channel effects partially pooled across
  geos, new geos drawn from the hyperprior, additive contributions. Fit-cached derived
  quantities in original units: contributions, per-draw response-curve grids and
  marginal cost. A decision objective scores budgets per posterior draw. Business priors
  and lift tests calibrate it.
- **`mmm.pymc_marketing`**: an adapter around `pymc_marketing.mmm.MMM` (pymc-marketing
  1.2 on PyMC 6) that owns scaling, carry-over and the inverse to original units. Business
  priors (translated on pymc-marketing's own model) and lift tests calibrate it.
- **`mmm.bayesinsight`** (PyMC): a BayesInsight `model_def.json` fitted as BayesInsight
  defines it - its likelihoods, priors in KPI units, curves and carry-overs, signs, effects
  per row id, seasons and local trends. Lift tests calibrate it.

`mapi_plugin_mmm.examples` simulates E33-style daily local-currency data with known
parameters and holds a ready-to-run spec (`E33_SPEC_YAML`);
`mapi_plugin_mmm.bayesinsight_examples` does the same for a BayesInsight project.

Guide: `docs/guides/mmm.md`. Tests: `uv run pytest plugins/mmm` (the contract-kit and
parameter-recovery tests are marked `slow`).

License: Apache License 2.0.
