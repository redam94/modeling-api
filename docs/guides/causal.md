# Measure the effect of a change <span class="mapi-badge">v0.13</span>

This guide tells you how to measure the effect of a campaign, a launch or a price change. It
has three procedures:

1. Measure the effect of a change on one series (`causal.impact`).
2. Run a geo-lift test: a campaign in some markets only (`causal.synthetic_control`).
3. Feed the result of the geo-lift test to the marketing mix model (`mmm.bayesian`).

[Causal models](../models/causal.md) gives the settings, the bundle groups and the math of
each model.

## Select the model

| Your data | Model | Guided template |
|---|---|---|
| One series. The change touched all of it from one date. | `causal.impact` | `campaign_impact` - Effect of a campaign |
| The same number for many markets. The campaign ran in some markets only. | `causal.synthetic_control` | `geo_lift` - Geo-lift test |
| The same number for many units. Some units got the change, at the same time or at different times. Some units never got it. | `causal.did` | none (use the spec editor) |

All three models compare the actual values with a prediction of the values **without** the
change. The prediction is only as good as the information that the model has:

- `causal.impact` uses the history of the series and the control series. A control series
  must not change because of the change.
- `causal.synthetic_control` uses the markets without the campaign.
- `causal.did` uses the units that never got the change.

Look at the `causal.pre_fit` chart before you use an effect. Before the change, the actual
values must be in the band of the prediction.

## Measure the effect of a change on one series

1. Upload a dataset with a date column, the result (for example weekly sales) and optional
   control columns. The data must start a minimum of 3 periods before the change. Two years
   before the change is better, because the model then learns the yearly pattern.
2. In Guided mode, select **Effect of a campaign**.
3. Select the result column and the date column.
4. Select the control columns. Use series that the change did not affect, for example the
   sales in other countries or searches for the category. You can select no column.
5. Type the start date of the change as YYYY-MM-DD.
6. If the change stopped, type the end date. If it did not stop, leave the field empty.
7. Fit the model.

The report shows these items:

- `causal.impact`: the actual values, the values without the change, the effect in each
  period and the cumulative effect. The caption gives the total effect with its 90% interval,
  the relative effect and the probability that the effect is more than zero.
- `causal.pre_fit`: the fit before the change, with its error.
- `core.convergence`: the convergence of the sampler.

The same model as a spec:

```yaml
data:
  dataset: dataset://growth/weekly_sales@latest
  bind: {dims: {time: week}, vars: {y: sales}}
model:
  use: causal.impact@^1
  settings:
    intervention_start: "2024-03-04"
    intervention_end: "2024-04-28"
    controls: [sales_de, category_searches]
    seasonality: [{period: 52.18, harmonics: 3}]
  fit: {mode: train_only, tune: 500, draws: 500, chains: 4, seed: 20261020}
outputs:
  plots: [causal.impact, causal.pre_fit, core.convergence]
```

!!! note "Use `train_only`"
    The effect is the result of the fit. A holdout removes the periods that you want to
    measure. Thus the causal templates fit once on all periods.

## Run a geo-lift test

A geo-lift test changes the spend of one channel in some markets (the test markets) for some
weeks. The other markets stay as they were. `causal.synthetic_control` makes a weighted mix of
the other markets that follows each test market before the test. During the test, the mix
gives the value without the campaign.

1. Upload a dataset with a date column, a market column and the result. Include a minimum of
   10 weeks before the test, and all weeks of the test.
2. In Guided mode, select **Geo-lift test**.
3. Select the result column, the date column and the market column.
4. Type the names of the test markets as they are in the market column. Separate the names
   with commas, for example `Leeds, Bristol`.
5. Type the start date and the end date of the test as YYYY-MM-DD.
6. Fit the model.

The report shows `causal.impact` (summed over the test markets), `causal.donor_weights` (the
weight of each market in the mix) and `causal.pre_fit`. If one market has almost all of the
weight, the result depends on that market only.

!!! caution "Do not use a test market that is larger than all other markets with the simplex weights"
    A positive mix of smaller markets cannot follow it. The pre-period chart then shows a bad
    fit. Use `normalize: mean` (the default) and `intercept: true`, or select other test
    markets.

## Feed the result to the marketing mix model

The total effect of a geo-lift test is a lift test for `mmm.bayesian`. A lift test tells the
model: "this extra spend on this channel in these weeks added this quantity of the KPI". The
model then agrees with the test (see
[Guided modelling](guided-modelling.md#lift-tests)).

### From the Guided report

1. Open the report of the `geo_lift` fit.
2. In the **Lift test for your marketing mix** card, type the channel of the test.
3. Type the spend change in the test. Type a negative number for a holdout (less spend).
4. Copy the YAML entry.
5. Paste the entry into `calibration.lift_tests` of the spec of your marketing mix model.
6. Fit the marketing mix model again.

The card does not change the spec of the marketing mix model for you.

### With the workflow template `geo_lift_mmm`

The workflow template `geo_lift_mmm` does all steps in one run:

1. `geolift`: `model.fit` of `causal.synthetic_control` on the data of the experiment.
2. `lift`: `model.lift_test` makes the lift test from the `impact` group. `kpi_delta` is the
   mean of the total effect and `se` is its sd.
3. `impact_plot`: the `causal.impact` chart.
4. `mmm`: `model.fit` of `mmm.bayesian` with `calibration.lift_tests` from the `lift` node.
5. `lift_plot`: the `mmm.lift_tests` chart. It shows if the model agrees with the test.
6. `report`: a report of the lift test, the metrics and the two charts.

The template has two inputs: `experiment` (the KPI for each market and week around the test)
and `data` (the data of the marketing mix model). It has the parameters `treated`,
`test_start`, `test_end`, `channel` and `spend_delta`.

```bash
mapi workflows create --template geo_lift_mmm
mapi workflows run geo-lift-mmm \
  -i experiment=dataset://growth/geo_test@latest \
  -i data=dataset://growth/eu_media_weekly@latest \
  -p 'treated=["Leeds", "Bristol"]' -p test_start=2024-03-04 -p test_end=2024-04-14 \
  -p channel=tv -p spend_delta=50000 --watch
```

These rules apply:

- The dates of the test must be in the data of the marketing mix model.
- With one test market, the lift test has the geo of that market. That market must be a geo
  of the marketing mix model.
- With two or more test markets, the lift test is national (`geo: null`).
- The time labels of the experiment must be dates.

## Before and after, with a comparison group

Use `causal.did` when some units (stores, markets, products) got a change and other units
never got it. Make a `treatment` column: 0 before a unit got the change, 1 from the period of
the change.

```yaml
data:
  bind: {dims: {unit: store, time: month}, vars: {y: sales, treatment: new_layout}}
model:
  use: causal.did@^1
  settings: {design: event_study, leads: 4, lags: 8}
  fit: {mode: train_only, tune: 500, draws: 500, chains: 4, seed: 20261020}
outputs:
  plots: [causal.event_study, causal.impact, causal.pre_fit, core.convergence]
```

The `causal.event_study` chart shows one coefficient for each period before and after the
change. The coefficients before the change must include zero. If they do not include zero,
the units moved differently before the change, and the effect is not reliable.

If the units got the change at different times, the model gives the warning
`STAGGERED_ADOPTION`. The estimate can then be wrong when the effect is different for each
group of units.

## What the fit logs

Each causal fit logs the total effect, its interval, the relative effect and the error before
the change as tracking metrics. Thus you can compare tests on the **Runs** page. See
[What a causal fit logs](tracking.md#what-a-causal-fit-logs).
