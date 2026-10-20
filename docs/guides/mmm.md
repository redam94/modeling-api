# Marketing-mix models

`mapi-plugin-mmm` contains the marketing-mix models (MMMs) of the platform. The four models
use [mmm-framework](../models/mmm-framework.md):

| Plugin | What it is | Use it when |
|---|---|---|
| `mmm.bayesian` | The Bayesian MMM of mmm-framework, national or hierarchical over geos and products | You want the contribution and the return of each channel with honest ranges. You also want business priors and lift tests, what-if answers and budget plans |
| `mmm.frequentist` | The ridge estimator of mmm-framework with bootstrap intervals | You need a first result in seconds, or you want to compare many set-ups quickly |
| `mmm.nested` <span class="mapi-badge">v0.12</span> | The nested (mediated) model of mmm-framework, national | Your channels act through an intermediate measure, such as visits or awareness, and you want to know how much of each effect goes through it |
| `mmm.multivariate` <span class="mapi-badge">v0.12</span> | The multivariate-outcome model of mmm-framework, national | One budget acts on two or more results, such as the sales of related products |

The plugins register through the `mapi.models` entry point. Thus a spec only gives their
names: `use: mmm.bayesian@^1`. This guide shows a complete analysis. The
[model reference](../models/mmm-framework.md) gives all the settings, and how each setting
maps onto mmm-framework.

## From daily spend in local currency to weekly EUR decisions

A typical European media dataset is daily, for each market and channel, in the currency of
each market. mmm-framework does the scaling, the carry-over and the seasonality. Thus the
pipeline of the platform only does these steps:

- It changes the currencies.
- It aggregates the data to weeks.
- It adds a holiday control.

The pipeline keeps each learned step. Thus a score does exactly the same work again:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: eu-geo-mmm, project: growth}
data:
  dataset: dataset://growth/eu_media_daily@latest
  side_tables:
    fx:       {ref: dataset://growth/fx_daily@latest, score_policy: supplied}
    holidays: {ref: dataset://shared/holidays_eu@v3, score_policy: pinned}
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 8, after_step: weekly}   # weeks exist after `weekly`
pipeline:
  - id: to_eur
    use: core.fx_convert@^1
    with: {vars: [y, spend], table: fx, table_along: date, currency_dim: geo,
           currencies: {UK: GBP}, default_currency: EUR}
  - {id: weekly, use: core.resample@^1, with: {dim: time, freq: W-SUN, how: sum}}
  - {id: hols,   use: core.holidays@^1, with: {table: holidays, table_along: date, along: time}}
model:
  use: mmm.bayesian@^1
  settings:
    hierarchy: {pool_over: [geo]}       # one baseline per market, partially pooled
    new_levels: hyperprior              # a new market is drawn from the fitted spread
    adstock: {kind: geometric, l_max: 6}
    saturation: {kind: logistic}
    seasonality: {yearly_order: 3}
  fit: {mode: evaluate_then_refit, engine: nutpie, tune: 1000, draws: 1000, chains: 4, seed: 7}
outputs:
  derived: [contributions, response_curves, marginal_cost]
  plots: [core.ppc_fan, core.convergence, mmm.decomposition, mmm.response_curves]
evaluate: {metrics: [elpd_loo, coverage_90, mape]}
```

The platform does these things with this spec. You do not write model code:

- **Binding.** Your names (`market`, `revenue_local`) become the canonical names of the model
  (`geo`, `y`). The platform changes them back on each output.
- **Split at the right stage.** The holdout is the last 8 *weeks*. The platform cuts the
  holdout after the resample. Thus no partial week goes into the training data.
- **Evaluate, then refit.** The platform fits the model on the training weeks. It scores the
  holdout *through the score path*, which is the same code that a forecast uses. Then it fits
  the model again on all the weeks. Thus forecasts start from the latest data.
- **Owned scaling and carry-over.** mmm-framework scales the data itself. It carries spend
  over with its own adstock kernel. The platform puts a score window on top of the stored fit
  history. Thus carry-over from the TV burst of last month gets to the sales of next month.
  Validation refuses a scaling or lag step on `y` or `spend` (`CAPABILITY_UNSUPPORTED`).
- **Weekly EUR.** Predictions and contributions come back in the units of the model stage.
  Here, these are weekly EUR. The outputs tell this in their `output_space` (`model-owned`) and
  `units`.

`mapi_plugin_mmm.examples.E33_SPEC_YAML` is a smaller version of this spec that you can run.
It comes with a simulator (`e33_dataset`) with known true contributions.

## Settings at a glance

| Setting | Level | Default | Meaning |
|---|---|---|---|
| `hierarchy.pool_over` | basic | `[]` | Panel dims of the data (`geo`, `product`). Each level gets its own baseline, partially pooled |
| `adstock.kind` / `l_max` | basic | `geometric`, 8 | Carry-over shape (`geometric`, `delayed`, `weibull`, `none`) and length |
| `saturation.kind` | basic | `logistic` | Diminishing returns (`logistic`, `hill`, `michaelis_menten`, `tanh`, `root`, `none`) |
| `trend.kind` | basic | `linear` | `none`, `linear`, `piecewise`, `spline` or `gp` |
| `seasonality.yearly_order` | basic | 2 | Yearly Fourier pairs |
| `channels.<c>` | advanced | - | Adstock/saturation overrides for each channel |
| `hierarchy.media_by_geo` | advanced | `false` | Channel effects for each geo, partially pooled |
| `media_prior` | advanced | `roi` | Channel effects given as returns on spend |
| `priors` | advanced | - | `roi.<c>: {mu, sigma}`, `roi_default`, `intercept` |
| `likelihood` | advanced | `normal` | `student_t` for heavy-tailed outliers; `poisson`, `negative_binomial` for counts; `beta` for shares |
| `media_effect_sigma` | advanced | 0.5 | The prior scale of the channel effects of a count or share likelihood |
| `time_varying` | advanced | none | `channels`, `innovation_sigma` (0.15) and `forecast` (`random_walk`) of time-varying channel effects |
| `new_levels` | advanced | `pooled` | How the model scores unseen geos/products |
| `controls` | advanced | auto | Control variables (default: all other numeric variables over time) |
| `grid` | advanced | 0-2.5x in 26 steps | The spend-multiplier grid of the response curves |

Guided mode shows only the **basic** settings. Each field has `x-ui-level` in the JSON Schema
of the manifest. The [models catalog](../reference/generated/models-catalog.md) lists all the
settings with their defaults.

## Scoring

Score requests for MMMs work as for all other models. MMM users need these requests most.

=== "Forecast"

    ```yaml
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: endpoint://growth/eu-geo-mmm-prod
    mode: forecast
    horizon: {dim: time, periods: 8}
    side_tables: {fx: dataset://growth/fx_daily@latest}    # fx is `supplied` at score time
    output: {kind: expected, summary: [mean, hdi_90], draws: 200}
    ```

    Send daily spend in local currency for the next 8 weeks. You get weekly EUR revenue.

=== "A new market"

    Add a market that the model did not see (`FR`). With `new_levels: hyperprior`, the model
    draws the baseline of the market from the fitted spread for each posterior draw. Thus its
    forecast is honest about what the model does not know. The forecast is wider than the
    forecast of a fitted market. The response has an `UNSEEN_LEVELS` notice.

=== "A weekly EUR plan"

    ```yaml
    mode: forecast
    horizon: {dim: time, periods: 8}
    inputs_stage: weekly          # enter after fx conversion and resampling
    ```

    Planners send weekly EUR budgets. They do not need to invent daily spend in local
    currency. You do not need an FX table, because the platform skips the steps that use it.
    The forecast is the same as the forecast from the raw data for the same spend.

=== "What if"

    ```yaml
    mode: counterfactual
    interventions: [{var: spend_local, sel: {channel: tv}, multiply: 1.2}]
    output: {kind: predictive, draws: 200}
    ```

    The scenario and the baseline share their noise (common random numbers). Thus the
    `delta` group shows the effect of the change, not sampling noise. Only the contribution
    of the changed channel moves.

Each score gives `y_hat` and `contributions(component, geo, time)`. There is one component for
each channel, `baseline`, `trend`, `seasonality`, and one component for each control. The
components add up to `y_hat`.

## What a Guided report reads

The fit calculates `contributions`, `response_curves` and `marginal_cost` (in
`outputs.derived`) one time. It uses the first `outputs.ui_draws` posterior draws. These items
read the values:

- `mmm.decomposition`, `mmm.response_curves` and `mmm.marginal_cost` plot them.
- The what-if sliders of Guided mode interpolate the response curves for each draw. The answer
  is instant, with joint uncertainty bands.
- The plausibility checks of the scorecard read them: negative or absurd ROI, the share of a
  channel, and the baseline share.

Business priors (ROI or share ranges) and lift tests calibrate the model through the
`calibration` block of the spec. See [Guided modelling](guided-modelling.md) and
[the model reference](../models/mmm-framework.md#priors-in-business-terms).

## Calibrate with a geo-lift test <span class="mapi-badge">v0.13</span> { #geo-lift }

A geo-lift test changes the spend of one channel in some markets for some weeks. The result
of the test is a good lift test for the model, because the test measures the effect directly.

1. Fit `causal.synthetic_control` on the KPI of all markets around the test. The `geo_lift`
   Guided template does this for you.
2. Get the lift test from the fit: the lift-test card of the Guided report, or the workflow
   node `model.lift_test`.
3. Add the lift test to `calibration.lift_tests` of the MMM spec.
4. Fit the MMM again.
5. Look at the `mmm.lift_tests` chart. The estimate of the model must be near the test.

```yaml
calibration:
  lift_tests:
    - id: tv-geo-lift-2024-03
      channel: tv
      geo: geo_03            # one test market; null for two or more markets
      start: 2024-03-04
      end: 2024-04-14
      spend_delta: 50000
      kpi_delta: 4100        # the mean of the total effect
      se: 1100               # the sd of the total effect
```

The workflow template `geo_lift_mmm` does all the steps in one run. See
[Measure the effect of a change](causal.md#feed-the-result-to-the-marketing-mix-model).

## Effects that change over time <span class="mapi-badge">v0.12</span> { #time-varying }

Use this when the effect of a channel changes during the data, for example when an
advertisement gets old.

1. Put the channel in `time_varying.channels`. Remove its ROI prior, if it has one.
2. Select the forecast policy after the fit window.
3. Add `effectiveness` to `outputs.derived` and `mmm.effectiveness` to `outputs.plots`.

```yaml
model:
  use: mmm.bayesian@^1
  settings:
    adstock: {kind: geometric, l_max: 8}
    saturation: {kind: hill}
    time_varying: {channels: [tv], innovation_sigma: 0.15, forecast: hold_last}
outputs:
  derived: [contributions, response_curves, marginal_cost, adstock, effectiveness]
  plots: [core.ppc_fan, mmm.decomposition, mmm.effectiveness, mmm.roi]
```

The chart shows the effect of one unit of spend in each period, with a range. A forecast with
`random_walk` has a range that becomes wider with the horizon. Use `hold_last` if you think
that the last effect continues. Response curves and budget plans use the average effect over
the fit window. Refer to
[Effects that change over time](../models/mmm-framework.md#time-varying-effects).

## Counts and shares <span class="mapi-badge">v0.12</span> { #counts-and-shares }

Use a count likelihood when the KPI is a number of items, such as orders, sign-ups or
visits. Use `beta` when the KPI is a share between 0 and 1.

1. Set `likelihood` to `poisson`, `negative_binomial` or `beta`.
2. Remove `priors.roi` and `priors.roi_default`. These likelihoods use coefficient priors.
3. Remove the `calibration` block. Business priors and lift tests need a normal or Student-t
   likelihood.

```yaml
model:
  use: mmm.bayesian@^1
  settings:
    likelihood: negative_binomial   # use poisson only when the variance is near the mean
    media_effect_sigma: 0.5
    adstock: {kind: geometric, l_max: 8}
    saturation: {kind: hill}
```

The contributions still add up to `y_hat`, in KPI units. The baseline then includes the
trend, the seasonality and the controls. A predictive score gives whole numbers for a count
and values between 0 and 1 for a share. Refer to
[Counts and shares](../models/mmm-framework.md#counts-and-shares).

## Effects through an intermediate measure <span class="mapi-badge">v0.12</span> { #nested }

Use `mmm.nested` when you measure a step between the spend and the KPI, for example site
visits or brand awareness from a monthly survey. The Guided template **Marketing mix with an
intermediate measure** (`mediated_mmm`) makes this spec from your columns.

1. Put the measures in one variable along a `mediator` dim. Leave the periods without a value
   empty.
2. Bind the dims `channel`, `mediator` and `time`, and bind the measures to `mediators`.
3. Optionally, give each mediator its channels and signs in `mediators`.

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: online-mmm, project: growth}
data:
  dataset: dataset://growth/online_weekly@latest
  bind:
    dims: {channel: channel, mediator: mediator, time: week}
    vars: {y: online_sales, spend: spend, mediators: measures}
  split: {kind: time_holdout, dim: time, last: 8}
model:
  use: mmm.nested@^1
  settings:
    trend: {kind: linear}
    seasonality: {yearly_order: 2}
    mediators:
      - {name: awareness, channels: [tv, social], observation_sigma: 0.2}
      - {name: visits, outcome_sign: positive}
  fit: {mode: evaluate_then_refit, engine: nutpie, tune: 1000, draws: 1000, chains: 4, seed: 7}
outputs:
  derived: [contributions, response_curves, marginal_cost, adstock, mediation]
  plots: [core.ppc_fan, core.convergence, mmm.decomposition, mmm.mediation, mmm.roi]
```

`mmm.mediation` shows the direct and the indirect effect of each channel. Lift tests
calibrate the total effect of a channel, as for `mmm.bayesian`.

!!! note
    The nested model uses national data only and takes no control variables. If price or
    promotions change your sales much, `mmm.bayesian` with controls is a better start.

## Several results <span class="mapi-badge">v0.12</span> { #multivariate }

Use `mmm.multivariate` when one budget acts on two or more results, for example the sales of
a premium range and a basic range. The Guided template **Marketing mix for related products**
(`multi_kpi_mmm`) makes a start of this spec from your columns, without weights and without
cross-effects. Add them in the spec editor.

1. Put the results in `y` along an `outcome` dim.
2. Give each outcome a `weight`: the value of one unit of it in a budget plan.
3. Add the cross-effects that you expect, with their kind.

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: range-mmm, project: growth}
data:
  dataset: dataset://growth/ranges_weekly@latest
  bind:
    dims: {channel: channel, outcome: outcome, time: week}
    vars: {y: sales, spend: spend}
  split: {kind: time_holdout, dim: time, last: 8}
model:
  use: mmm.multivariate@^1
  settings:
    trend: {kind: linear}
    seasonality: {yearly_order: 2}
    outcomes: [{name: premium, weight: 3.0}, {name: basic, weight: 1.0}]
    cross_effects: [{source: premium, target: basic, kind: halo}]
  fit: {mode: evaluate_then_refit, engine: nutpie, tune: 1000, draws: 1000, chains: 4, seed: 7}
outputs:
  derived: [contributions, response_curves, marginal_cost, adstock, cross_effects]
  plots: [core.ppc_fan, core.convergence, mmm.decomposition, mmm.cross_effects, mmm.roi]
```

Response curves, the ROI and budget plans use the weighted total of the outcomes. The
decomposition and the waterfall show the primary outcome. This is also true for a score that
gives the dataset's names, where the outcome dim has the name that you bound (for example,
`product`). Set `outcome` on `mmm.decomposition` or `mmm.waterfall` to show a different one.

!!! warning "Read cross-effects as associations"
    Do not use a cross-effect as proof that one product takes sales from a different product.
    The model cannot separate it from the correlation of the noise. Use a test to measure a
    cause.

## Charts for a marketing team <span class="mapi-badge">v0.12</span> { #charts }

Add these recipes to `outputs.plots`. Each chart has a caption in plain words.

| Question | Recipe | Needs in `outputs.derived` |
|---|---|---|
| Which channel returns the most for each unit of spend? | `mmm.roi` | `response_curves` (and `marginal_cost` for the marginal return) |
| How long does the effect of spend continue? | `mmm.adstock` | `adstock` |
| What adds up to the total result? | `mmm.waterfall` | `contributions` |
| Does each channel get spend in proportion to its effect? | `mmm.spend_effect` | `response_curves` |
| Do the lift tests agree with the model? | `mmm.lift_tests` | none (the fit keeps the `lift_tests` group) |
| Does the effect of a channel change over time? | `mmm.effectiveness` | `effectiveness` |
| Did the sampler explore the posterior well? | `core.energy` | none |
| How much did the data change each prior? | `core.prior_posterior` | none |

## Optimising budgets

The platform optimises budgets with the planning module of mmm-framework. You do not need an
external optimiser:

```python
import httpx

with httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}",
                                         "X-Mapi-Project": "growth"}) as http:
    r = http.post(
        f"/v1/models/{model_id}/versions/3:optimize",
        json={"budget_change_pct": 10, "objective": "mean", "frontier": {"n_points": 8}},
        headers={"Prefer": "wait=60"},
    )
    plan = r.json()["result"]          # None while the run is still going (202)
for a in plan["allocation"]:
    print(a["channel"], round(a["current_spend"]), "->", round(a["optimal_spend"]))
print("uplift", plan["uplift"]["mean"], "chance it helps", plan["prob_positive_uplift"])
```

See [Optimise a budget](../tutorials/optimise-a-budget.md) for a walk-through. See
[the model reference](../models/mmm-framework.md#budget-optimisation) for all the options. In
Python, you can also use the decision objective of a fitted bundle directly.
`MMMBayesian().objective(bundle.model, budget, ScoreContext())` gives the value of a budget for
each period, `spend(channel)` or `spend(geo, channel)`. It gives this value at steady state,
for each posterior draw.

## Live progress and cancel <span class="mapi-badge">v0.11</span> { #live-progress }

`mmm.bayesian` reports its progress while it samples. Before v0.11, the progress went from
10% to 90% in one step.

- The progress bar moves with the draws of the slowest chain.
- The run page shows one lane for each chain, with its draws, divergences and step size.
- The fit logs the step metrics `step_size`, `num_steps` and `divergences` for each chain
  (label `chain`). With `nuts_sampler: pymc`, it also logs `tree_depth`. The **Metrics** tab
  shows them. See
  [Live chains and step metrics](pymc.md#live-chains).
- If you cancel the run while it samples, the sampler stops. The run becomes `cancelled`
  and makes no model version.

This applies to `nuts_sampler: nutpie` and `nuts_sampler: pymc`. The approximate methods
(ADVI, Pathfinder) of mmm-framework do not send progress while they fit. From v0.12,
`mmm.nested` and `mmm.multivariate` send the same progress and step metrics. Each MMM fit
also logs summary metrics, such as the ROI of each channel (see
[What an MMM fit logs](tracking.md#mmm)).

## The fast frequentist model

`mmm.frequentist` takes the same data and settings, and answers in seconds:

- It searches the transforms out of sample.
- It solves the model with ridge regression, with non-negative channel effects.
- The ranges are bootstrap confidence intervals.

Use it for a first look. Then fit `mmm.bayesian` before you make decisions.

## Trust, but verify

The four plugins pass the SDK contract kit (`mapi_sdk.testing.check_model`). The kit tests
these items:

- Bundle round trips.
- Shuffled and subset markets.
- New levels.
- Typed errors.
- The holdout scored through the same path as a forecast.
- Additive contributions.

The conformance corpus runs the plugins through the full spec -> fit -> score path (`e33`,
`guided_mmm`, `mmm_frequentist`). The tests of the plugin check the forward pass against the
forecaster and the graph of mmm-framework.
