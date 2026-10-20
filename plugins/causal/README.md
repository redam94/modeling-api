# mapi-plugin-causal

Causal-inference model plugins for modeling-api (`causal.*`, entry point
`mapi.models: causal`). Each model estimates the effect of an intervention (a campaign, a
launch, a price change, a geo test) as the difference between what happened and a
counterfactual: what would have happened without the intervention.

| Plugin | Model | Representation | Score modes |
|---|---|---|---|
| `causal.impact@1` | CausalImpact / interrupted time series on a Bayesian structural time series (`ts.bsts` core) | draws | predict, forecast |
| `causal.synthetic_control@1` | Bayesian synthetic control (geo-lift): the treated markets as a weighted mix of donor markets | draws | predict |
| `causal.did@1` | Bayesian difference-in-differences / event study with unit and time effects | draws | predict, counterfactual |

## The `impact` group

Every model writes the plugin-owned `impact` bundle group (target units):

| Variable | Dims | Meaning |
|---|---|---|
| `actual` | `[unit,] time` | observed y (NaN allowed) |
| `counterfactual` | `sample, [unit,] time` | predictive draws of y without the intervention; in the pre-period the model's fit |
| `effect` | `sample, [unit,] time` | actual - counterfactual |
| `cumulative_effect` | `sample, [unit,] time` | cumulative sum of `effect` over the `post` cells, NaN outside them |
| `post` | `[unit,] time` | the cells of the effect window |

Attributes: `intervention_start`, `intervention_end` (`""` when open), `model`, `units`,
`effect_kind` (`"predictive"`). The `unit` dim (treated units only) exists for the panel
models. The charts `causal.impact` and `causal.pre_fit` and the workflow node
`model.lift_test` read only this group.

## `causal.impact`

* Canonical inputs: dim `time` (time, extend); `y(time)` target (missing values allowed);
  `controls` (setting, a list of variable names) as covariates over `time`, needed for
  every period including forecasts.
* The BSTS (trend `level` / `local_linear` / `smooth`, up to three seasonalities, a static
  regression on the controls) is fitted on the periods **before** `intervention_start`
  only. The counterfactual is the one-step-ahead prediction inside that pre-period and a
  forecast from the filtered state at its end, driven by the observed controls, after it.
  Draws use common random numbers seeded by `(fit seed, "counterfactual")`.
* Groups: `posterior`, `sample_stats`, `observed_data`, `constant_data` (controls),
  `impact`, `components` and `one_step` (pre-period), and the `terminal_state` at the end
  of the pre-period.
* Score outputs: `y_hat` (the counterfactual) and `effect` (`y - y_hat`; only when the
  payload has y). `forecast` continues past the data with the payload's controls.
* Diagnostics: MCMC convergence and `SHORT_PRE_PERIOD` (fewer than two cycles of the
  longest seasonal period before the start).
* Tracking: the `bsts.*` params, `causal.intervention_start/end`, `causal.n_pre`,
  `causal.n_post`, `causal.n_treated`, and the metrics `effect_total` (+ `_sd`, `_p05`,
  `_p95`), `effect_average`, `effect_relative`, `p_effect_positive`, `pre_rmse`,
  `pre_mape`.

`mapi_plugin_causal.datasets` holds the simulator `impact_dataset` and the example spec
`IMPACT_SPEC_YAML`; `mapi_plugin_causal._common` the window parser, the `impact` group
builder, the effect summary and the tracking helper the three models share.

## `causal.synthetic_control`

* Canonical inputs: dims `unit` (categorical, fixed) and `time` (time, extend);
  `y(unit, time)` target (missing values allowed). `settings.treated` names the test markets;
  every other unit (or `settings.donors`) is a donor.
* For each treated unit, the pre-period is fitted as a weighted mix of the donors:
  `weights: simplex` (a Dirichlet with `concentration`, the classic convex synthetic control)
  or `weights: horseshoe` (sparse, signed, can extrapolate). `normalize: mean` divides each
  unit by its pre-period mean.
* Groups: `impact` and `donor_weights` (the weights per draw and the pre-period RMSE of each
  treated unit). Score output: `y_hat(unit, time)` for the treated units from the payload's
  donor values (donor rows are NaN). No pointwise LOO.
* Tracking: the shared effect summary, `causal.n_donors`, `causal.weights`, and the metric
  `donor_weight` (labels `unit` and `donor`) for the largest donors.

## `causal.did`

* Canonical inputs: dims `unit` and `time` (both fixed); `y(unit, time)` target,
  `treatment(unit, time)` (0/1, 1 from the adoption period on) and the covariates in
  `settings.covariates`.
* Two-way fixed effects: `design: event_study` (one coefficient per period relative to the
  adoption, `leads` and `lags`, `bin_endpoints`) or `design: did` (one treatment effect).
* Groups: `impact` (treated units) and `event_study` (the coefficients with the reference
  period at 0, and the ATT). `counterfactual` mode sets `treatment` to 0. A payload may cover
  only some periods. Staggered adoption gives the diagnostic `STAGGERED_ADOPTION`.
* Tracking: the shared effect summary, `causal.design`, `causal.staggered`,
  `causal.leads`, `causal.lags`, the metrics `att` (+ `_p05`, `_p95`) and `event_coef`
  (label `event_time`).

## Plots, templates and the MMM

The plot recipes `causal.impact`, `causal.pre_fit`, `causal.donor_weights` and
`causal.event_study` (`mapi-plots[recipes]`) read the groups above. The workflow node
`model.lift_test` turns the `impact` group into a lift test for `mmm.bayesian` (the workflow
template `geo_lift_mmm`), and the Guided templates `campaign_impact` and `geo_lift` use
`causal.impact` and `causal.synthetic_control`. The models run in the `mapi/base-forecast`
runner image (with `mapi-plugin-ts` and pymc-extras).

Documentation: `docs/models/causal.md` (the models) and `docs/guides/causal.md` (measure the
effect of a change).

## Known gaps

* The counterfactual is only as good as the controls: a control that the intervention
  also moved biases the effect towards zero.
* `causal.impact` fits the pre-period only; the post-period does not update the
  parameters. Only one series per model.
* With a pipeline step that rescales y (for example `core.standardize`), the score output
  `effect` is converted like a level of y; fit the model on y in its own units.

License: Apache License 2.0.
