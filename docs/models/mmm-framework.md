# Marketing-mix models on mmm-framework

From v0.5, all marketing-mix models (MMM) on the platform run on
[mmm-framework](https://redam94.github.io/mmm-framework/) (PyPI `mmm-framework==1.5.0`,
Apache-2.0). mmm-framework is a Bayesian MMM framework that uses PyMC. `mapi-plugin-mmm`
puts it behind the platform contract as two built-in models:

| Model | In plain words | Engine |
|---|---|---|
| `mmm.bayesian` | Measures how much each marketing channel adds to your sales, in total or per market. It also shows what will occur if you move budget between channels. | mmm-framework `BayesianMMM`: NUTS (nutpie, PyMC or NumPyro), or fast approximations (MAP, ADVI, Pathfinder) |
| `mmm.frequentist` | Gives a first estimate in seconds. Its ranges come from samples of the data, not from the uncertainty of a full Bayesian model. | mmm-framework's ridge estimator with bootstrap intervals |

Each model has an **About card** in plain language. The card tells what the model is for,
when not to use it, what data it needs and what you get from it. *Help me choose* in the web
app recommends these models for the goals "explain my marketing" and "plan my budget"
(`mapi choose -a goal=plan_budget`, see [Choosing a model](../guides/choosing-a-model.md)).
mmm-framework is the only marketing-mix engine on the platform. The platform itself optimises
budgets ([below](#budget-optimisation)).

## What the data looks like

mmm-framework does the **scaling, the carry-over and the conversion back to your units**
(`owns: [scaling, carryover, inverse_units]`). Thus, the pipeline only converts currencies,
adds the data up to weeks and adds controls. The spec validation refuses a scaling step or a
lag step on the KPI or on the spend (`CAPABILITY_UNSUPPORTED`). Thus, the platform does not
scale a value two times.

The table gives the canonical names. Bind the names of your dataset to them in `data.bind`:

| Name | Dims | Role |
|---|---|---|
| `time` | - | the period dim (`extend`: forecasts continue after the data) |
| `channel` | - | one label for each media channel (`fixed`) |
| `geo`, `product` | - | the panel dims in the list `hierarchy.pool_over` (none for one national series) |
| `y` | `(*panel, time)` | the KPI |
| `spend` | `(*panel, channel, time)` | the spend for each channel (`future_required`: a forecast needs it) |
| any other numeric variable over time | `(*panel?, time, ...)` | a control (or put them in the list `controls`). Each label of an extra dim, for example a Fourier mode, becomes one feature. |

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
  split: {kind: time_holdout, dim: time, last: 8, after_step: weekly}
pipeline:
  - {id: to_eur, use: core.fx_convert@^1,
     with: {vars: [y, spend], table: fx, table_along: date, currency_dim: geo,
            currencies: {UK: GBP}, default_currency: EUR}}
  - {id: weekly, use: core.resample@^1, with: {dim: time, freq: W-SUN, how: sum, full_periods_only: true}}
  - {id: hols,   use: core.holidays@^1, with: {table: holidays, table_along: date, along: time}}
model:
  use: mmm.bayesian@^1
  settings:
    hierarchy: {pool_over: [geo]}
    new_levels: hyperprior
    adstock: {kind: geometric, l_max: 6}
    saturation: {kind: hill}
    trend: {kind: linear}
    seasonality: {yearly_order: 3}
  fit: {mode: evaluate_then_refit, engine: nutpie, tune: 1000, draws: 1000, chains: 4, seed: 7}
outputs:
  derived: [contributions, response_curves, marginal_cost]
  plots: [core.ppc_fan, core.convergence, mmm.decomposition, mmm.response_curves]
evaluate: {metrics: [elpd_loo, coverage_90, mape]}
```

The plugin includes a smaller version of this spec that you can run:
`mapi_plugin_mmm.examples.E33_SPEC_YAML`. It has a simulator (`e33_dataset`) that gives
known true contributions. `NATIONAL_SPEC_YAML` with `national_dataset()` is the smallest
national example.

### MFF data

The native format of mmm-framework is the **Master Flat File** (MFF). It has one row for
each `Period`, `Geography`, `Product`, `Campaign`, `Outlet`, `Creative` and `VariableName`.
The value is in `VariableValue`.

Upload the file with the `mff` ingest preset, or as a Guided upload. Each variable becomes
its own data variable. The key columns have the same meaning as in the MFF of mmm-framework:

- `Geography` and `Product` become dims when they have more than one level.
- Some variables have only a total: a blank, `National` or `Total` label. An example is
  national TV next to regional sales. The platform divides such a variable across the levels
  in the same way as `MFFLoader`. Each geography gets an equal part. Each product gets a part
  that agrees with its share of the KPI. `MffOptions.allocation` changes this.
- A variable cannot have both rows for each level and total rows.
- The platform adds `Campaign`, `Outlet` and `Creative` up to the dims of the model, unless
  `MffOptions.split` keeps them.
- The Guided geo template puts the media variables that you select into `spend(channel)`.

The plugin gives the panel to mmm-framework in the same layout that its own `MFFLoader` makes.
The observations are in period order first, then geography, then product. Tests compare an
MFF file that the platform ingests with the same file that `mmm_framework.load_mff` loads.
They make sure that the two give the same panel, with the national media rows and the
campaign rows.

## How the spec maps onto mmm-framework

| Spec | mmm-framework |
|---|---|
| `y`, `spend`, controls | `PanelDataset` (`y`, `X_media`, `X_controls`) with `MFFConfig` (`KPIConfig`, `MediaChannelConfig` for each channel, `ControlVariableConfig` for each control feature) |
| `hierarchy.pool_over: [geo, product]` | the `Geography`/`Product` dimensions of the KPI; `HierarchicalConfig.pool_across_geo/product` (a baseline for each level, partially pooled) |
| `hierarchy.media_by_geo` | `HierarchicalConfig.vary_media_by_geo` (channel effects for each geo, partially pooled) |
| `adstock.kind`, `l_max`, `normalize` | `AdstockConfig` (`geometric`, `delayed`, `weibull`, `none`) on all channels |
| `saturation.kind` | `SaturationConfig` (`logistic`, `hill`, `michaelis_menten`, `tanh`, `root`, `none`) |
| `channels.<c>.adstock/saturation` | the configs of that channel |
| `trend.kind` | `TrendConfig` (`none`, `linear`, `piecewise`, `spline`, `gp`) |
| `seasonality.yearly_order` ... | `SeasonalityConfig.yearly/monthly/weekly` (Fourier pairs) |
| `media_prior: roi` | `ModelConfig.media_prior_mode="roi"`: the return on spend sets the parameters of the channel effects |
| `priors.roi.<c>: {mu, sigma}` | `MediaChannelConfig.roi_prior_mu/roi_prior_sigma` |
| `priors.intercept: {mu, sigma}` | `ModelConfig.intercept_prior_mu/sigma` |
| `likelihood: student_t` | `LikelihoodConfig(family=student_t)` |
| `fit.engine` | `nutpie`/`pymc`/`numpyro`: NUTS with that backend. `map`, `advi`, `fullrank_advi`, `pathfinder`, `laplace`, `smc`: `BayesianMMM.fit(method=...)` |
| `fit.tune`, `draws`, `chains`, `target_accept`, `seed` | the same `fit()` arguments |

The plugin reads `frequency` (`W`, `D` or `M`) from the time labels. If the labels are
integers, set `frequency`. When you score, `new_levels` sets what occurs for new panel
levels:

- `pooled`: the overall level and the average channel effects.
- `hyperprior`: for each draw, a level from the fitted spread. Thus, its range is correctly
  wider.
- `zero`: no channel effects.
- `error`.

## What is stored, and how scoring works

A fit keeps these items. It never keeps them as a pickle:

- The parameters of mmm-framework (`posterior`).
- The sampler statistics.
- A thinned prior of the free parameters (for the prior/posterior overlap on the scorecard).
- The observed KPI.
- A `fit_data` group with the KPI, the spend and the control features at the model stage.

When you score, the plugin makes the `BayesianMMM` again from `fit_data`. The same data give
the same scaling, Fourier features and trend basis. Then the plugin calculates the fitted
model again in NumPy for each posterior draw. This calculation includes the intercept, trend,
seasonality, panel levels, the adstock and saturation of each channel, each control and the
observation noise. It is the same forward pass as the `PosteriorForecaster` of mmm-framework.
Tests compare it with the forecaster and with the deterministics of the graph, to
floating-point precision.

- **Carry-over.** The plugin writes the scored periods over the fitted history. Thus, a TV
  burst of last month has an effect on the sales of next month. A four-week window gives the
  same scores as the same weeks of the full series. Periods after the fit window must follow
  it with no gap.
- **Trends after the data.** The plugin extends a linear trend. Piecewise, spline and GP
  trends stay at their last fitted level (the forecast policy of mmm-framework). The forecast
  tells you this. Their intervals do not become wider with the horizon.
- **Outputs.** `y_hat(*panel, time)` and `contributions(component, *panel, time)`. There is
  one component for each channel, `baseline` (intercept and panel levels), `trend`,
  `seasonality` and one for each control. They add up to `y_hat`. A `predictive` request
  adds noise from a seeded generator. Thus, the scenario and the baseline of a counterfactual
  use the same noise.

## Priors in business terms

The **return on spend** sets the parameters of the channel effects. With `media_prior: roi`
(the default), each channel samples `roi ~ LogNormal(mu, sigma)`. This is the KPI that the
channel returns for each unit of spend over the fit window. mmm-framework calculates the
coefficient from it. Thus, you give a prior in the units that a business owner uses:

```yaml
settings:
  priors:
    roi:
      tv:     {mu: 0.69, sigma: 0.4}    # median ROI e^0.69 = 2.0, 90% within about [1.0, 3.9]
      search: {mu: 1.1, sigma: 0.3}
    roi_default: {mu: 0.0, sigma: 1.0}  # channels not listed: median break-even
    intercept: {mu: 0.2, sigma: 0.3}    # the baseline, in KPI standard deviations
```

The platform changes **business priors** into these priors in closed form. Business priors
come from Guided task 3, from `calibration.business_priors` or from a `translate_priors` run:

- An ROI range `[low, high]` with credibility `p` becomes the log-normal that has this range
  as its central `p` interval.
- A range for the contribution share is an ROI range, multiplied by the KPI total divided by
  the spend of the channel (`share = roi x spend / KPI`).
- The platform reads a range that starts at 0 as `[high / 20, high]`, and gives an `info`
  issue.
- The **baseline share** (the result with no media) sets the intercept prior. The platform
  calculates it by bisection against the prior of mmm-framework for the trend, seasonality,
  levels and controls.

The preview samples the prior predictive of the translated model (the graph of
mmm-framework). It shows the ROI, the share, the baseline share and a KPI fan in business
units. It also shows the prior mass in each range that you gave (`coverage`). This value is
near the credibility that you gave.

## Lift tests

`calibration.lift_tests` become **experiment likelihoods** in mmm-framework
(`BayesianMMM.add_experiment_calibration`). Each is a likelihood term on the estimand that
the model gives. It updates the effect of the channel, its saturation curve and its
carry-over together.

| Lift test | mmm-framework measurement |
|---|---|
| extra spend (`spend_delta > 0`) | `MROAS`, `spend_lift_pct = 100 x spend_delta / window spend`, value `kpi_delta / spend_delta` |
| a holdout that removes all (or almost all) of the spend of the channel (`-spend_delta >= 95%` of it) | `CONTRIBUTION` of `-kpi_delta` |
| a partial cut (less than 95% of the spend) | skipped. The reason is in `learned.calibration.lift_tests.skipped`. This test measures the slope below the current spend, `(f(S) - f((1-c)S)) / cS`. `MROAS` is the slope above it (`spend_lift_pct > 0` only). When returns decrease, the two slopes are different (by `e^(c lam)` for `1 - exp(-lam x)`). Thus, the mapping would bias the curve. |

`geo` limits the measurement to that geo. The plugin skips a test after the data. It also
skips a test that the fit window cuts (a holdout fit that ends in the test). It puts the
skipped tests in a list.

## Derived quantities

Ask for them in `outputs.derived`. The plugin calculates them at fit time over the first
`outputs.ui_draws` draws. It keeps them in the bundle:

- `contributions`: `channel_contribution(sample, channel, *panel, time)`, `baseline`,
  `trend`, `seasonality` and one variable for each control.
- `response_curves`: `contribution(sample, channel, multiplier[, geo])`. This is the total
  contribution over the fit window, with the spend of the channel multiplied by the
  multiplier (0 to 2.5 x by default). It also gives the spend on each grid point, the
  current spend, the largest spend in one period and the baseline. The **what-if sliders**
  of Guided mode interpolate these values for each draw. The plausibility checks of the
  scorecard read them.
- `marginal_cost`: the extra spend for each extra unit of KPI at the current spend, and its
  inverse.

## Budget optimisation

`POST /v1/models/{model_id}/versions/{version}:optimize` asks how to divide a budget across
channels (and, optionally, geos). An `optimize` run in the sandbox makes the response curves
of the fitted model for each posterior draw. It gives them to the `planning` module of
mmm-framework:

- `optimize_budget`: greedy marginal allocation, or a constrained multi-start solver for
  risk objectives, groups and money bounds.
- `budget_frontier`.

```bash
curl -s -X POST "$API/v1/models/$MODEL/versions/3:optimize" \
  -H "Authorization: Bearer $MAPI_TOKEN" -H "X-Mapi-Project: growth" \
  -H "Prefer: wait=60" -H "Content-Type: application/json" -d '{
    "total_budget": 1000000,
    "channels": {"tv": {"min_multiplier": 0.5, "max_multiplier": 1.5},
                 "search": {"min_spend": 50000}},
    "groups": [{"name": "digital", "channels": ["search", "social"], "min_share": 0.4}],
    "objective": "p10",
    "frontier": {"n_points": 10}
  }'
```

| Request | mmm-framework |
|---|---|
| `total_budget` / `budget_change_pct` | the budget (default: the current total) |
| `mode: fixed` / `free` | spend the budget / give each channel money up to break-even (`value_per_kpi` converts KPI to money) |
| `channels.<c>.min_multiplier/max_multiplier` | `bounds` (multiples of the current spend) |
| `channels.<c>.min_spend/max_spend` | `abs_bounds` (money; they override multiples) |
| `channels.<c>.locked` | the multiplier stays at 1 |
| `groups` | portfolio constraints (shares and/or money) |
| `objective` | `mean`, `p<q>` (a cautious percentile) or `cvar<a>` (the mean of the worst draws) |
| `by_geo` | allocate for each (geo, channel) arm together |
| `horizon` | the periods of the plan (the curves are scaled again from the fit window at its average spend) |
| `max_draws`, `n_steps`, `seed` | sampling controls |

The answer is `200` if the run finished in the `Prefer: wait` time. If not, the answer is
`202`. Then poll `GET /v1/optimizations/{run_id}`. The answer contains these items:

- The allocation for each channel, with its expected contribution and marginal return. It
  also tells how stable the recommendation is across draws.
- The expected KPI of the plan and of the current plan.
- The uplift and the probability that it is positive.
- The expected regret if you use one plan.
- The shadow price of the next unit of money.
- The efficient frontier.
- Notes. For example, a note tells you when a recommendation is more than the spend that the
  model has seen.

The run also makes the `mmm.budget_plans` plot. If the data cannot meet the bounds, the run
fails with `OPTIMIZE_INFEASIBLE`.

### End to end with the client

These steps go from a fitted version to a plan that you can defend, in Python. The
equivalent CLI commands follow.

1. **Do a check of the model first.** A plan is only as good as its response curves. Run the
   governance policy for marketing-mix models. It does null tests against shuffled sales and
   false noise channels. It also does a check of the effect signs and the holdout accuracy.
   Read why each check passed:

    ```python
    --8<-- "client_v05_more.py:validate"
    ```

2. **Look at the curves.** The `response_curves` section of the Model Explorer shows the
   contribution of each channel when its spend changes, for each posterior draw. A mark shows
   the current spend. `decomposition` shows the part that the model gives to each channel
   over time:

    ```python
    explored = api.models.explore(model_id, version, sections=["response_curves", "decomposition"])
    curves = api.explorations.view(explored, "response_curves")
    ```

3. **Ask for the plan.** `api.models.optimize` sends `Prefer: wait`. It sends the request
   again with the same `Idempotency-Key` until the run in the sandbox is done. Thus, a slow
   plan never starts two times:

    ```python
    --8<-- "client_v05_more.py:optimize"
    ```

4. **Test the largest change.** Score the weekly spend of the plan as a forecast. Then design
   a lift test for the channel that the plan changes most. Put its result into the
   `calibration` block of the model (see [Lift tests](#lift-tests)). The next plan uses it.

You can do the same steps from a terminal:

```bash
mapi validate model model://growth/geo-mmm@v7
mapi explore model model://growth/geo-mmm@v7 -s response_curves -s decomposition --out views/
--8<-- "cli_v05.sh:optimize"
```

This output shows the plan for the two-channel model of the demo (`eu-geo-mmm`). The model
is the fit from the Python client guide, with very small sampler settings and half a year of
data. The output is from a laptop:

```text
 optimisation                      run_01m53hqpb8690ja1nkrc7b8j12
 objective                         downside P10 (a low quantile — risk-averse)
 budget                            639,176 EUR (now 639,176)
 expected revenue_local            1.656e+07 (now 1.658e+07)
 uplift                            -1.431e+04
 chance it beats the current plan  48%
 draws                             200
Plan
 channel ┃ geo ┃ now     ┃ recommended ┃ change ┃ next unit returns ┃ note
━━━━━━━━━╇━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━
 tv      │     │ 370,845 │ 488,240     │ +32%   │ 2.92              │
 search  │     │ 268,331 │ 150,936     │ -44%   │ 2.94              │
What more (or less) budget buys
 budget    ┃ expected revenue_local ┃ next unit returns
━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━
 255,670   │ 1.504e+07              │ 4.15
 547,865   │ 1.622e+07              │ 3.9
 840,060   │ 1.725e+07              │ 3.43
 986,157   │ 1.766e+07              │ 2.78
 1,132,254 │ 1.784e+07              │ 1.24
 1,278,352 │ 1.79e+07               │ 0.41
 …
note: Non-concave response curve(s) detected for tv, search: the greedy allocator's exactness
precondition fails, so this run used the multi-start constrained solver instead. Treat the
optimum as the best point found, not a proven global one.
```

Read the output as the planning module intends. The plan protects the cautious result (the
10th percentile). Thus, its *expected* revenue is a little less than the current revenue.
The plan is better than the current plan in only 48% of the draws. On this model, money that
moves from search to TV gives insurance, not growth. The next euro returns about the same
(2.9) in the two channels. Thus, the current split is already almost efficient.

The frontier is the more useful answer. Up to about 1 million EUR, each extra euro returns
2.8 or more. Above 1.13 million, the return decreases to 1.2 and then to 0.4. The notes (on
standard error) tell which solver ran and why. With `--objective mean`, the same request
maximises the expected revenue.

A workflow can do all these steps on a schedule. The `mmm_governed` gallery template fits
`mmm.bayesian` on a geo panel. It runs the policy with `submit: true` and keeps the evidence
pack (`mapi workflows init mmm_governed`).

## The frequentist model

`mmm.frequentist` uses the same data and settings (but no priors, likelihood or GP trend).
mmm-framework finds the adstock and saturation with an out-of-sample (rolling-origin)
criterion, and then fixes them. It solves the model, which is now linear, by ridge
regression with channel effects that are not negative. Then it solves the model again on
block-bootstrap samples. These replicates are the "draws" of all outputs. Thus, the ranges
are *confidence* intervals.

This model has no convergence diagnostics, business priors or lift tests. The seed sets its
results, and it fits in seconds.

```yaml
model:
  use: mmm.frequentist@^1
  settings:
    hierarchy: {pool_over: [geo]}
    adstock: {kind: geometric, l_max: 6}
    saturation: {kind: logistic}
    bootstrap: 200
    search_budget: 200
  fit: {mode: evaluate_then_refit, seed: 7}
```

`estimator: constrained` (a convex program) needs the optional `cvxpy` dependency of
mmm-framework. The images of the platform do not install it. Thus, the fit fails with
`CAPABILITY_UNSUPPORTED`.

## Current limits

- The platform does not give these mmm-framework features: the multiplicative (log-log)
  specification, effects that change with time, price and promotion levers, events and
  interactions between channels. The forward pass of the platform is for the additive model
  only.
- The `mmm_extensions` of mmm-framework (nested/mediated models and models with many
  outcomes) are not available yet. `mmm.nested` and `mmm.multivariate` have About cards but
  no plugin.
- Fits run on the CPU. Only the NumPyro backend uses JAX.

## Trust, but verify

The two models pass the SDK contract kit (`mapi_sdk.testing.check_model`). The kit tests
these items:

- Bundle round trips.
- Shuffled markets and subsets of markets.
- New levels.
- Typed errors for gaps and for periods before the data.
- A holdout that the plugin scores through the same path as a forecast.
- Additive contributions.
- The plots.

The conformance corpus fits these specs from start to end: `e33` (a geo panel with a GBP
market, side tables and a weekly plan), `guided_mmm` (business priors and a lift test) and
`mmm_frequentist`. A recovery test fits simulated data with known contributions. It makes
sure that the split of the contributions across channels is correct.
