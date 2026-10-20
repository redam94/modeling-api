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
  - `time_varying.channels` lets the effect of a channel change over time (a random walk
    on the log coefficient, step scale `time_varying.innovation_sigma`).
    `time_varying.forecast` sets the effect after the fit window: `random_walk` (the
    default; the range widens with the horizon), `hold_last` or `mean`. A time-varying
    channel has one effect in all geos and takes no ROI prior.
  - `likelihood` is `normal` (the default), `student_t`, `poisson` or `negative_binomial`
    (counts, log link) or `beta` (shares between 0 and 1, logit link). With a count or
    share likelihood, channel effects are positive coefficients on the link scale
    (`media_effect_sigma`), the contributions are the channels and a baseline, and the
    model takes no business priors or lift tests.
- **`mmm.frequentist`**: mmm-framework's ridge estimator with block-bootstrap draws (the
  fast alternative for exploration and governance null tests).
- **`mmm.nested`** (v0.12): mmm-framework's `NestedMMM`. Channels move the KPI directly and
  through mediator series (`mediator` dim, for example awareness or site visits; a mediator
  can be observed in some periods only, or `latent: true`). Lift tests calibrate the total
  effect of a channel. The derived quantity `mediation` gives the direct, indirect and total
  effect of each channel; the output `mediator_hat` gives the fitted mediators. National data
  only, no control variables.
- **`mmm.multivariate`** (v0.12): mmm-framework's `MultivariateMMM`. One model for several
  outcomes (`outcome` dim), with correlated noise and cross-effects between the outcomes
  (`halo`, `cannibalization` or `unconstrained`). The derived quantity `cross_effects` gives
  each cross-effect; `outcomes[].weight` values the outcomes in a budget plan. National data
  only, no control variables, no lift tests.

The models own scaling, carry-over and the inverse to original units, return `y_hat` and
`contributions` (channels, baseline, trend, seasonality, controls), and plan budgets with
mmm-framework's `planning` module (`CustomModel.optimize`, the `optimize` run kind). The
fit-cached derived quantities of `mmm.bayesian` and `mmm.frequentist` are:

| Name | Content |
|---|---|
| `contributions` | Channel, baseline, trend, seasonality and control contributions per draw |
| `response_curves` | Total contribution per spend multiplier, the spend and the baseline |
| `marginal_cost` | Extra spend per extra unit of the KPI at current spend, and its inverse |
| `adstock` | The share of a unit of spend's effect at each lag, and the mean lag |
| `effectiveness` | KPI per unit of spend at the average spend level, per period |

A fit with a tracker logs the params `mmm.framework_version`, `mmm.n_channels`,
`mmm.n_periods`, `mmm.n_cells`, `mmm.link` (and `mmm.time_varying`), and per channel the
metrics `roi`, `roi_p05`, `roi_p95`, `mroi` and `contribution_share`, plus
`baseline_share` (`mapi_plugin_mmm.tracking`). A fit calibrated with lift tests keeps them
in the bundle group `lift_tests`: the measured value next to the model's estimate.

The MMM plot recipes of `mapi-plots` (v0.12) draw these quantities: `mmm.roi`,
`mmm.adstock`, `mmm.waterfall`, `mmm.spend_effect`, `mmm.lift_tests`, `mmm.effectiveness`,
`mmm.mediation` and `mmm.cross_effects`, next to the response curves, marginal cost, budget
plans and decomposition. The Guided templates `mediated_mmm` and `multi_kpi_mmm` use the two
national models.

`mapi_plugin_mmm.examples` simulates E33-style daily local-currency data with known
parameters and holds ready-to-run specs (`E33_SPEC_YAML`, `NATIONAL_SPEC_YAML`).

Guides: `docs/guides/mmm.md` and `docs/models/mmm-framework.md`. Tests:
`uv run pytest plugins/mmm` (the contract-kit and parameter-recovery tests are marked
`slow`).

License: Apache License 2.0.
