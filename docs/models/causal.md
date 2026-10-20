# Causal models and Bayesian structural time series <span class="mapi-badge">v0.13</span>

v0.13 adds four Bayesian models. One model forecasts a series from its parts. Three models
measure the effect of a change, such as a campaign, a launch or a price change:

| Model | Package | In plain words | Your data |
|---|---|---|---|
| `ts.bsts` | `mapi-plugin-ts` | Bayesian trend, season and drivers | One series over time, optional drivers |
| `causal.impact` | `mapi-plugin-causal` | Effect of a campaign on one series (CausalImpact) | One series, optional control series, the start date of the change |
| `causal.synthetic_control` | `mapi-plugin-causal` | Geo-lift test (Bayesian synthetic control) | The same number for many markets, the test markets and the test dates |
| `causal.did` | `mapi-plugin-causal` | Before and after, with a comparison group (difference-in-differences and event study) | The same number for many units, and a 0/1 treatment column |

All four models sample with NUTS. They run in the time-series runner image
(`mapi/base-forecast`). They log [tracking](../guides/tracking.md#what-a-causal-fit-logs)
metrics and params on each fit. Each model has an About card, so *Help me choose* finds them
for the goal "Measure the effect of a campaign, a launch or a test".

[Measure the effect of a change](../guides/causal.md) is the procedure. This page is the
reference.

## The effect, in one bundle group

The three causal models write the effect to the `impact` group of the bundle. Plots,
workflow nodes and the web app read only this group. The values are in the units of the
target.

| Variable | Dims | What it is |
|---|---|---|
| `actual` | (`unit`,) `time` | The observed value. It can be NaN. |
| `counterfactual` | `sample`, (`unit`,) `time` | Draws of the value without the change. The draws include the observation noise. |
| `effect` | `sample`, (`unit`,) `time` | `actual - counterfactual` |
| `cumulative_effect` | `sample`, (`unit`,) `time` | The sum of `effect` over the cells of the effect window. It is NaN outside them. |
| `post` | (`unit`,) `time` | True in the effect window (from the start to the end, inclusive) |

The time dim is always `time`. `causal.synthetic_control` and `causal.did` add the `unit` dim
(the treated units only). `causal.impact` has no `unit` dim. The group attributes are
`intervention_start`, `intervention_end` (empty when the change did not stop), `model` and
`units`. The `sample` dim has a maximum of 500 draws.

Before the start, `counterfactual` is the fit of the model. Thus the `causal.pre_fit` chart
shows how well the model follows the data before the change.

!!! note "Common random numbers"
    The noise of the counterfactual draws comes from the seed of the fit. Two fits with the
    same seed and the same data give the same noise. A comparison of two scenarios thus shows
    only the difference between the scenarios.

## `ts.bsts`: Bayesian structural time series

`ts.bsts` divides one series into a level, a trend, seasonal patterns and the effect of
drivers. It uses the state-space models of
[pymc-extras](https://github.com/pymc-devs/pymc-extras) (Apache-2.0). The sampler uses the
exact Kalman likelihood.

**Inputs.** `t` (time, policy `extend`) and the target `y(t)`. Missing periods are
permitted. The filter continues over them. The drivers that you name in `regressors` are
variables along `t`. A forecast needs their future values (`MISSING_FUTURE_COVARIATE` if
they are not in the payload). The model standardises `y` and the drivers. It changes the
outputs back to your units.

| Setting | Default | Meaning |
|---|---|---|
| `trend` | `local_linear` | `level` (a random walk), `local_linear` (a level and a slope that both change) or `smooth` (only the slope changes) |
| `seasonality` | `[]` | A maximum of three seasonal patterns: `{period, harmonics, stochastic, sigma}` |
| `regressors` | `[]` | The drivers. Each driver has one effect for all periods. |
| `level_sigma`, `slope_sigma` | 0.1, 0.02 | Half-normal scales of the trend innovations (standardised scale) |
| `obs_sigma` | 0.5 | Half-normal scale of the observation noise |
| `initial_sd` | 1.0 | The prior sd of the first state |
| `regression_prior`, `regression_sigma` | `normal`, 1.0 | `normal` or `horseshoe` (when you have many drivers) |

A seasonal pattern has a `period` in periods of `t` (52.18 for weekly data with a yearly
pattern, 7 for daily data with a weekly pattern). Without `harmonics`, the pattern has one
effect for each season. This needs an integer period of 24 or less. With `harmonics`, the
pattern is a Fourier series of that order (a maximum of half the period). With
`stochastic: true`, the pattern can change over time. The model has a maximum of 40 states.

**Score modes.**

* `predict` / `evaluate`: in the fitted range, the one-step-ahead prediction (the value that
  the model expected before it saw the period). After the fitted range, a forecast.
* `forecast`: a forecast from the state at the end of the fit (the `terminal_state` group).
* `filter`: runs the Kalman filter over new periods that continue the fit. It returns the
  one-step-ahead predictions and an updated `terminal_state`. Thus you can add new periods
  without a new fit (see [Forecasting with actuals](../concepts/forecasting-with-actuals.md)).

**Outputs.** `y_hat(t)` (the expected value or a predictive draw) and `level(t)` (the level
and the trend, expected), in the units of the target. The bundle also has the groups
`components` (`level`, `season_<i>` and `regression` over `sample` and `t`; the
`core.decomposition` chart shows them), `one_step` and `terminal_state`.

The manifest declares pointwise leave-one-out as not valid: the Kalman likelihood is a sum
over the whole series, and the periods depend on each other. Validation rejects `elpd_loo`.

```yaml
data:
  bind: {dims: {t: date}, vars: {y: sales}}
  split: {kind: time_holdout, dim: t, last: 12}
model:
  use: ts.bsts@^1
  settings:
    trend: local_linear
    seasonality: [{period: 12}]
    regressors: [price]
  fit: {mode: evaluate_then_refit, tune: 500, draws: 500, chains: 4, seed: 7}
```

## `causal.impact`: the effect of a change on one series

`causal.impact` is CausalImpact (Brodersen et al. 2015) on the `ts.bsts` core. The model
fits a Bayesian structural time series to the periods **before** the change. The control
series are its drivers. Then it predicts the series after the start from the control series.
The difference between the actual values and the prediction is the effect. Without control
series, this is an interrupted time series.

**Inputs.** `time` (time, policy `extend`), the target `y(time)` (missing values are
permitted) and the control series that you name in `controls`, along `time`. The control
series must have values for all periods.

| Setting | Default | Meaning |
|---|---|---|
| `intervention_start` | (necessary) | The first period of the change: an ISO date (`2024-03-04`) or a period number. A date between two periods selects the next period. |
| `intervention_end` | none | The last period of the change. Empty: the change did not stop. |
| `controls` | `[]` | Series that the change did not touch (sales in other countries, searches for the category) |
| `trend`, `seasonality`, ... | as `ts.bsts` | The structure of the model |

The model needs a minimum of 3 periods before the start. It gives the diagnostic
`SHORT_PRE_PERIOD` when fewer than two seasons of the longest seasonal pattern come before
the start.

The periods must be evenly spaced (`SCHEMA_INVALID`): the model counts periods by position.
Add the missing periods with an empty target.

**Score modes.** `predict` gives `y_hat(time)`, the value without the change, and
`effect(time)`, the payload `y` minus `y_hat`. Before the start, `y_hat` is the one-step-ahead
prediction. From the start, it is a forecast with the control series of the payload.
`forecast` does the same after the data. With `output.units: original`, `effect` is the
difference in the units of the data, also when the pipeline transforms the target (for
example with `core.log1p`).

## `causal.synthetic_control`: a geo-lift test

`causal.synthetic_control` is a Bayesian synthetic control (Abadie, Diamond and Hainmueller
2010). For each treated market, the model finds weights of the other markets (the donors).
Before the test, the weighted mix of the donors follows the treated market. During the test,
the mix gives the value without the campaign.

**Inputs.** `unit` (categorical, `fixed`), `time` (time, policy `extend`) and the target
`y(unit, time)`. The donors must have values for all periods before the test and for all
periods that you score.

| Setting | Default | Meaning |
|---|---|---|
| `treated` | (necessary) | The test markets (labels of `unit`). A text such as `"Leeds, Bristol"` is also correct. |
| `donors` | all other units | The markets for the mix (a minimum of two) |
| `intervention_start`, `intervention_end` | (necessary), none | The dates of the test |
| `weights` | `simplex` | `simplex`: positive weights with a sum of 1 (the classic method). `horseshoe`: weights that can be negative and have no fixed sum. |
| `concentration` | 1.0 | Dirichlet concentration of the simplex weights (less than 1: fewer donors) |
| `intercept` | true | A level difference between a market and its mix |
| `normalize` | `mean` | `mean` divides each market by its mean before the test. `none` keeps the units. |
| `noise_sigma`, `likelihood` | 0.5, `normal` | The noise prior, and `normal` or `student_t` |

**Outputs.** `y_hat(unit, time)` for the treated units only, and the groups `impact` and
`donor_weights` (`weight(sample, unit, donor)` and the error before the test, `pre_rmse(unit)`).
The charts are `causal.impact`, `causal.donor_weights` and `causal.pre_fit`.

!!! warning "Do not use the horseshoe weights without a look at the pre-period chart"
    The horseshoe weights can extrapolate outside the range of the donors. A treated market
    that is larger than all donors then gets a mix that can be wrong. Look at
    `causal.pre_fit` before you use the effect.

### From a geo-lift test to a lift test for the MMM

The `impact` group of a synthetic control fit gives a lift test for the
[marketing mix model](mmm-framework.md). The total effect over the test is the `kpi_delta`.
Its sd is the `se`. These are the three ways to get the lift test:

- The Guided report of the `geo_lift` template shows a "Lift test for your marketing mix"
  card. Type the channel and the spend change. Copy the YAML entry into
  `calibration.lift_tests` of the MMM spec.
- The workflow node `model.lift_test` makes a `LiftTest` from any causal model version.
- The workflow template `geo_lift_mmm` fits the test, makes the lift test and fits
  `mmm.bayesian` with it. See [Feed the result to the MMM](../guides/causal.md#feed-the-result-to-the-marketing-mix-model).

A lift test from one treated market has the geo of that market. A lift test from two or more
markets is national (`geo: null`).

## `causal.did`: difference-in-differences and event study

`causal.did` compares units that got a change with units that never got it. It is a Bayesian
two-way fixed effects model: an effect for each unit, an effect for each period and the
effect of the change.

**Inputs.** `unit` (categorical, `fixed`), `time` (time, `fixed`), the target
`y(unit, time)` (missing values are permitted), `treatment(unit, time)` (0 before the change,
1 from the period of the change) and the covariates that you name in `covariates`. A minimum
of one unit must never get the change. Each unit must keep the change after it starts.

| Setting | Default | Meaning |
|---|---|---|
| `design` | `event_study` | `event_study`: one effect for each period relative to the change. `did`: one average effect. |
| `leads`, `lags` | 4, 8 | The periods before and after the change with their own effect |
| `bin_endpoints` | true | The periods further away share the effect of the first lead or the last lag |
| `time_effects` | `iid` | `iid` or `random_walk` |
| `covariates` | `[]` | Variables along (`unit`, `time`) |
| `likelihood`, `effect_sigma`, `noise_sigma` | `normal`, 1.0, 0.5 | The likelihood and the priors (standardised scale) |

The reference period is the period before the change (event time -1). Its coefficient is 0.

**Outputs.** `y_hat(unit, time)`, and the groups `impact` (the treated units) and
`event_study` (`coef(sample, event_time)`, `n_cells(event_time)` and the average effect after
the change, `att(sample)`). The `counterfactual` score mode sets `treatment` to 0. The
`causal.event_study` chart shows the coefficients. If the coefficients before the change are
near 0, the treated and the other units moved together before the change.

A score payload can have only some periods. The periods that it does not have take the
treatment of the fit, thus the period of the change of each unit stays the same. If the payload
sets the treatment to 0 (the `counterfactual` mode), the earlier periods also get 0.

!!! caution "Do not trust the estimate when the units got the change at different times and the effect is different for each group"
    The two-way fixed effects estimate can then be wrong (Goodman-Bacon 2021, Sun and Abraham
    2021). The model gives the warning `STAGGERED_ADOPTION` when the units got the change at
    different times.

The time effects are only for the periods of the data. Thus `causal.did` does not forecast.

## Known gaps

- `ts.bsts` takes one series. Each driver has one effect for all periods. A forecast does not
  use actual values that you know (use the `filter` mode). A seasonal pattern without
  `harmonics` has a maximum period of 24 (use `harmonics` for longer periods).
- `causal.impact` fits the periods before the change only.
- `causal.synthetic_control` has no placebo tests and does not design a test (choice of the
  markets, power).
- A lift test from two or more markets is national.
- The time effects of `causal.did` are fixed: the model does not forecast. Staggered adoption
  can make the estimates wrong.
- The lift-test card of the Guided report does not write into the MMM spec. Copy the entry.

## References

- Brodersen et al. (2015), Inferring causal impact using Bayesian structural time-series
  models, Annals of Applied Statistics.
- Abadie, Diamond and Hainmueller (2010), Synthetic control methods for comparative case
  studies, JASA.
- Goodman-Bacon (2021), Difference-in-differences with variation in treatment timing.
- Sun and Abraham (2021), Estimating dynamic treatment effects in event studies with
  heterogeneous treatment effects.
- Harvey (1989), Forecasting, Structural Time Series Models and the Kalman Filter. Durbin and
  Koopman (2012), Time Series Analysis by State Space Methods.
