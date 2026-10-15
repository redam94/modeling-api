# mapi-plugin-mmm

Marketing-mix model plugins for modeling-api, registered through the `mapi.models` entry
point (`mapi_plugin_mmm.plugins:PLUGINS`):

- **`mmm.hierarchical_geo`** (PyMC): the E33 hierarchical geo MMM. Adstock over a
  `core.lag_tensor`, logistic or Hill saturation, channel effects partially pooled across
  geos, new geos drawn from the hyperprior, additive contributions. Fit-cached derived
  quantities in original units: contributions, per-draw response-curve grids and
  marginal cost. A decision objective scores budgets per posterior draw.
- **`mmm.pymc_marketing`**: an adapter around `pymc_marketing.mmm.MMM` (pymc-marketing
  1.2 on PyMC 6) that owns scaling, carry-over and the inverse to original units.

`mapi_plugin_mmm.examples` simulates E33-style daily local-currency data with known
parameters and holds a ready-to-run spec (`E33_SPEC_YAML`).

Guide: `docs/guides/mmm.md`. Tests: `uv run pytest plugins/mmm` (the contract-kit and
parameter-recovery tests are marked `slow`).

License: Apache License 2.0.
