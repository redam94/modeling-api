# mapi-plugin-mmm

Marketing-mix model plugins for modeling-api on [mmm-framework](https://pypi.org/project/mmm-framework/)
(`mmm-framework==1.5.0`, Apache-2.0), the platform's only MMM engine. They are registered
through the `mapi.models` entry point (`mapi_plugin_mmm.plugins:PLUGINS`):

- **`mmm.bayesian`**: mmm-framework's `BayesianMMM`, national or partially pooled over geos
  and products (`hierarchy.pool_over`, optional `media_by_geo`): geometric adstock, Hill or
  logistic saturation, trend options and Fourier seasonality. It fits with NUTS (nutpie,
  PyMC or NumPyro) or with MAP, ADVI, full-rank ADVI, Pathfinder, Laplace or SMC. Business
  priors become log-normal ROI priors and an intercept prior; lift tests become
  mmm-framework experiment likelihoods. Scoring replays every posterior draw in NumPy,
  with the fitted history prepended so carry-over reaches the scored periods, and
  `new_levels` decides how unseen geos and products are handled.
- **`mmm.frequentist`**: mmm-framework's ridge estimator with block-bootstrap draws (the
  fast alternative for exploration and governance null tests).

Both own scaling, carry-over and the inverse to original units, return `y_hat` and
`contributions` (channels, baseline, trend, seasonality, controls), cache contributions,
response curves and marginal costs, and plan budgets with mmm-framework's `planning`
module (`CustomModel.optimize`, the `optimize` run kind).

`mapi_plugin_mmm.examples` simulates E33-style daily local-currency data with known
parameters and holds ready-to-run specs (`E33_SPEC_YAML`, `NATIONAL_SPEC_YAML`).

Guides: `docs/guides/mmm.md` and `docs/models/mmm-framework.md`. Tests:
`uv run pytest plugins/mmm` (the contract-kit and parameter-recovery tests are marked
`slow`).

License: Apache License 2.0.
