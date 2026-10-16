# Marketing-mix models on mmm-framework

From v0.5 every marketing-mix model (MMM) on the platform runs on
[mmm-framework](https://redam94.github.io/mmm-framework/) (PyPI `mmm-framework==1.5.0`,
Apache-2.0), a Bayesian MMM framework built on PyMC. `mapi-plugin-mmm` wraps it behind the
platform contract as two built-in models:

| Model | In plain words | Engine |
|---|---|---|
| `mmm.bayesian` | Measures how much each marketing channel adds to your sales, in total or per market, and what would happen if you moved budget between channels. | mmm-framework `BayesianMMM`: NUTS (nutpie, PyMC or NumPyro), or fast approximations (MAP, ADVI, Pathfinder) |
| `mmm.frequentist` | A first estimate in seconds, without the ranges a full Bayesian model gives. | mmm-framework's ridge estimator with bootstrap intervals |

Both carry a plain-language **About card** (what the model is for, when not to use it,
what data it needs, what you get out); the web app's *Help me choose* recommends them
for "explain my marketing" and "plan my budget" goals (`mapi choose -a goal=plan_budget`,
see [Choosing a model](../guides/choosing-a-model.md)). mmm-framework is the platform's only
marketing-mix engine, and budgets are optimised by the platform itself
([below](#budget-optimisation)).

## What the data looks like

mmm-framework owns **scaling, carry-over and the conversion back to your units**
(`owns: [scaling, carryover, inverse_units]`), so the pipeline only converts currencies,
aggregates to weeks and adds controls. A scaling or lag step on the KPI or the spend is
refused when the spec is validated (`CAPABILITY_UNSUPPORTED`), so nothing is scaled twice.

Canonical names (bind your dataset's names onto them in `data.bind`):

| Name | Dims | Role |
|---|---|---|
| `time` | - | the period dim (`extend`: forecasts run past the data) |
| `channel` | - | one label per media channel (`fixed`) |
| `geo`, `product` | - | the panel dims listed in `hierarchy.pool_over` (none for one national series) |
| `y` | `(*panel, time)` | the KPI |
| `spend` | `(*panel, channel, time)` | spend per channel (`future_required`: a forecast needs it) |
| any other numeric variable over time | `(*panel?, time, ...)` | a control (or list them in `controls`); extra dims such as Fourier modes become one feature per label |

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

A runnable, scaled-down version of this spec ships as
`mapi_plugin_mmm.examples.E33_SPEC_YAML`, with a simulator (`e33_dataset`) whose true
contributions are known; `NATIONAL_SPEC_YAML` with `national_dataset()` is the smallest
national example.

### MFF data

mmm-framework's native format is the **Master Flat File** (MFF): one row per `Period`,
`Geography`, `Product`, `Campaign`, `Outlet`, `Creative` and `VariableName`, the value in
`VariableValue`. Upload it with the `mff` ingest preset (or as a Guided upload): the key
columns that vary become dims and every variable its own data variable, and the Guided geo
template stacks the media variables you pick into `spend(channel)`. The plugin hands the
panel to mmm-framework in exactly the layout its own `MFFLoader` builds - observations
period-major, then geography, then product - and a test checks that an MFF file ingested by
the platform and the same file loaded with `mmm_framework.load_mff` give the same panel.

## How the spec maps onto mmm-framework

| Spec | mmm-framework |
|---|---|
| `y`, `spend`, controls | `PanelDataset` (`y`, `X_media`, `X_controls`) with `MFFConfig` (`KPIConfig`, `MediaChannelConfig` per channel, `ControlVariableConfig` per control feature) |
| `hierarchy.pool_over: [geo, product]` | the KPI's `Geography`/`Product` dimensions; `HierarchicalConfig.pool_across_geo/product` (a baseline per level, partially pooled) |
| `hierarchy.media_by_geo` | `HierarchicalConfig.vary_media_by_geo` (channel effects per geo, partially pooled) |
| `adstock.kind`, `l_max`, `normalize` | `AdstockConfig` (`geometric`, `delayed`, `weibull`, `none`) on every channel |
| `saturation.kind` | `SaturationConfig` (`logistic`, `hill`, `michaelis_menten`, `tanh`, `root`, `none`) |
| `channels.<c>.adstock/saturation` | that channel's configs |
| `trend.kind` | `TrendConfig` (`none`, `linear`, `piecewise`, `spline`, `gp`) |
| `seasonality.yearly_order` ... | `SeasonalityConfig.yearly/monthly/weekly` (Fourier pairs) |
| `media_prior: roi` | `ModelConfig.media_prior_mode="roi"`: channel effects parameterised by their return on spend |
| `priors.roi.<c>: {mu, sigma}` | `MediaChannelConfig.roi_prior_mu/roi_prior_sigma` |
| `priors.intercept: {mu, sigma}` | `ModelConfig.intercept_prior_mu/sigma` |
| `likelihood: student_t` | `LikelihoodConfig(family=student_t)` |
| `fit.engine` | `nutpie`/`pymc`/`numpyro`: NUTS with that backend; `map`, `advi`, `fullrank_advi`, `pathfinder`, `laplace`, `smc`: `BayesianMMM.fit(method=...)` |
| `fit.tune`, `draws`, `chains`, `target_accept`, `seed` | the same `fit()` arguments |

`frequency` (`W`, `D` or `M`) is read from the time labels; integer labels need it set.
New panel levels at score time follow `new_levels`: `pooled` (the overall level and the
average channel effects), `hyperprior` (a level drawn from the fitted spread for every
draw, so its range is honestly wider), `zero` (no channel effects) or `error`.

## What is stored, and how scoring works

A fit stores, never as a pickle: mmm-framework's parameters (`posterior`), the sampler
statistics, a thinned prior of the free parameters (the scorecard's prior/posterior
overlap), the observed KPI and a `fit_data` group with the model-stage KPI, spend and
control features. Scoring rebuilds the `BayesianMMM` from `fit_data` - the same data give
the same scaling, Fourier features and trend basis - and replays the fitted model per
posterior draw in NumPy: intercept, trend, seasonality, panel levels, every channel's
adstock and saturation, every control, and the observation noise. The replay is the same
forward pass as mmm-framework's own `PosteriorForecaster`; tests check it against both the
forecaster and the graph's deterministics to floating-point precision.

- **Carry-over.** The scored periods are written over the fitted history, so last month's
  TV burst reaches next month's sales and a four-week window scores exactly like the same
  weeks of the full series. Periods after the fit window must follow it without a gap.
- **Trends past the data.** A linear trend is extrapolated; piecewise, spline and GP trends
  are held at their last fitted level (mmm-framework's forecasting policy), and the
  forecast says so - their intervals do not widen with the horizon.
- **Outputs.** `y_hat(*panel, time)` and `contributions(component, *panel, time)`: one
  component per channel, `baseline` (intercept and panel levels), `trend`, `seasonality`
  and one per control. They add up to `y_hat`. A `predictive` request adds noise from a
  seeded generator, so a counterfactual's scenario and baseline share it.

## Priors in business terms

Channel effects are parameterised by their **return on spend**: under `media_prior: roi`
(the default) each channel samples `roi ~ LogNormal(mu, sigma)` - the KPI it returns per
unit of spend over the fit window - and mmm-framework derives the coefficient from it. So a
prior is stated in the units a business owner uses:

```yaml
settings:
  priors:
    roi:
      tv:     {mu: 0.69, sigma: 0.4}    # median ROI e^0.69 = 2.0, 90% within about [1.0, 3.9]
      search: {mu: 1.1, sigma: 0.3}
    roi_default: {mu: 0.0, sigma: 1.0}  # channels not listed: median break-even
    intercept: {mu: 0.2, sigma: 0.3}    # the baseline, in KPI standard deviations
```

**Business priors** (Guided task 3; `calibration.business_priors`, or a
`translate_priors` run) translate into these in closed form:

- an ROI range `[low, high]` held with credibility `p` is the log-normal whose central `p`
  interval it is;
- a contribution-share range is an ROI range scaled by the KPI total over the channel's
  spend (`share = roi x spend / KPI`);
- a range starting at 0 is read as `[high / 20, high]` (with an `info` issue);
- the **baseline share** (what happens without media) sets the intercept prior, solved by
  bisection against mmm-framework's own prior of the trend, seasonality, levels and
  controls.

The preview samples the translated model's prior predictive (mmm-framework's graph) and
reports ROI, share, baseline share and a KPI fan in business units, plus the prior mass
inside every stated range (`coverage`, close to the stated credibility).

## Lift tests

`calibration.lift_tests` become mmm-framework **experiment likelihoods**
(`BayesianMMM.add_experiment_calibration`): a likelihood term on the model-implied
estimand that updates the channel's effect, its saturation curve and its carry-over
jointly.

| Lift test | mmm-framework measurement |
|---|---|
| extra spend (`spend_delta > 0`) | `MROAS`, `spend_lift_pct = 100 x spend_delta / window spend`, value `kpi_delta / spend_delta` |
| a holdout removing (nearly) all the channel's spend (`-spend_delta >= 95%` of it) | `CONTRIBUTION` of `-kpi_delta` |
| a partial cut of at most 25% | `MROAS` of that size (the symmetric finite difference) |
| a larger partial cut | skipped, with the reason in `learned.calibration.lift_tests.skipped` |

`geo` restricts the measurement to that geo. A test after the data, or cut by the fit
window (a holdout fit that ends inside the test), is skipped and listed.

## Derived quantities

Ask for them in `outputs.derived`; they are computed at fit over the first
`outputs.ui_draws` draws and stored in the bundle:

- `contributions`: `channel_contribution(sample, channel, *panel, time)`, `baseline`,
  `trend`, `seasonality` and one variable per control;
- `response_curves`: `contribution(sample, channel, multiplier[, geo])`, the total
  contribution over the fit window with the channel's spend scaled by the multiplier
  (0 to 2.5 x by default), the spend on each grid point, the current spend, the largest
  single-period spend and the baseline - what Guided-mode **what-if sliders** interpolate
  per draw and the scorecard's plausibility checks read;
- `marginal_cost`: the extra spend per extra unit of KPI at current spend, and its inverse.

## Budget optimisation

`POST /v1/models/{model_id}/versions/{version}:optimize` asks how to split a budget across
channels (and optionally geos). A sandboxed `optimize` run builds the fitted model's
response curves per posterior draw and hands them to mmm-framework's `planning` module:
`optimize_budget` (greedy marginal allocation, or a constrained multi-start solver for
risk objectives, groups and money bounds) and `budget_frontier`.

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
| `total_budget` / `budget_change_pct` | the budget (default: today's total) |
| `mode: fixed` / `free` | spend the budget / fund each channel to break-even (`value_per_kpi` converts KPI to money) |
| `channels.<c>.min_multiplier/max_multiplier` | `bounds` (multiples of current spend) |
| `channels.<c>.min_spend/max_spend` | `abs_bounds` (money; they override multiples) |
| `channels.<c>.locked` | the multiplier pinned at 1 |
| `groups` | portfolio constraints (shares and/or money) |
| `objective` | `mean`, `p<q>` (a cautious percentile) or `cvar<a>` (mean of the worst draws) |
| `by_geo` | allocate per (geo, channel) arm jointly |
| `horizon` | the plan's periods (curves rescaled from the fit window at its average spend) |
| `max_draws`, `n_steps`, `seed` | sampling controls |

The answer (`200` when the run finished within `Prefer: wait`, else `202`; poll
`GET /v1/optimizations/{run_id}`) carries the allocation per channel with its expected
contribution, marginal return and how stable the recommendation is across draws, the
expected KPI of the plan and of today's plan, the uplift and the chance it is positive,
the expected regret of committing to one plan, the shadow price of the next unit of money,
the efficient frontier, and notes - for example when a recommendation goes beyond the
spend the model has seen. The run also renders the `mmm.budget_plans` plot. Bounds the data
cannot meet fail the run with `OPTIMIZE_INFEASIBLE`.

### End to end with the client

From a fitted version to a plan you can defend, in Python (the CLI equivalent follows):

1. **Check the model first.** A plan is only as good as the response curves under it. Run the
   governance policy for marketing-mix models - null tests against shuffled sales and fake
   noise channels, effect signs, holdout accuracy - and read why each check passed:

    ```python
    --8<-- "client_v05_more.py:validate"
    ```

2. **Look at the curves.** The Model Explorer's `response_curves` section shows each
   channel's contribution as its spend is scaled, per posterior draw, with the current spend
   marked; `decomposition` shows what the model attributes to each channel over time:

    ```python
    explored = api.models.explore(model_id, version, sections=["response_curves", "decomposition"])
    curves = api.explorations.view(explored, "response_curves")
    ```

3. **Ask for the plan.** `api.models.optimize` sends `Prefer: wait` and repeats the request
   with the same `Idempotency-Key` until the sandboxed run is done, so a slow plan never
   starts twice:

    ```python
    --8<-- "client_v05_more.py:optimize"
    ```

4. **Test the biggest move.** Score the plan's weekly spend as a forecast, then design a lift
   test for the channel the plan moves most; its result goes into the model's `calibration`
   block (see [Lift tests](#lift-tests)) and the next plan knows it.

The same from a terminal:

```bash
mapi validate model model://growth/geo-mmm@v7
mapi explore model model://growth/geo-mmm@v7 -s response_curves -s decomposition --out views/
--8<-- "cli_v05.sh:optimize"
```

```text
 optimisation        run_01m53…
 objective           10th percentile of revenue (cautious)
 budget              1,000,000 EUR (now 1,000,000)
 expected revenue    3.42e+06 (now 3.31e+06)
 uplift              +1.2e+05
 chance it beats the current plan   97%
                                    Plan
 channel   now        recommended   change   next unit returns   note
 tv        600,000    450,000       -25%     1.4
 search    400,000    550,000       +38%     1.4
```

A workflow can do all of it on a schedule: the `mmm_governed` gallery template fits
`mmm.bayesian` on a geo panel, runs the policy with `submit: true` and keeps the evidence pack
(`mapi workflows init mmm_governed`).

## The frequentist model

`mmm.frequentist` takes the same data and settings (no priors, likelihood or GP trend).
mmm-framework searches adstock and saturation by an out-of-sample (rolling-origin)
criterion, fixes them, solves the now-linear model by ridge regression with non-negative
channel effects and repeats the solve on block-bootstrap resamples. The replicates are the
"draws" of every output, so ranges are *confidence* intervals; there are no convergence
diagnostics, business priors or lift tests. It is seed-deterministic and fits in seconds.

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

`estimator: constrained` (a convex program) needs mmm-framework's optional `cvxpy`
dependency, which the platform's images do not install; the fit then fails with
`CAPABILITY_UNSUPPORTED`.

## Current limits

- The multiplicative (log-log) specification, time-varying effects, price and promotion
  levers, events and cross-channel interactions of mmm-framework are not exposed: the
  platform's forward pass covers the additive model.
- mmm-framework's `mmm_extensions` (nested/mediated and multivariate-outcome models) are
  not shipped yet: `mmm.nested` and `mmm.multivariate` have About cards but no plugin.
- Fits run on CPU (JAX is used by the NumPyro backend only).

## Trust, but verify

Both models pass the SDK contract kit (`mapi_sdk.testing.check_model`): bundle round
trips, shuffled and subset markets, new levels, typed errors for gaps and periods before
the data, the holdout scored through the same path as a forecast, additive contributions
and the plots. The conformance corpus fits `e33` (a geo panel with a GBP market, side
tables and a weekly plan), `guided_mmm` (business priors and a lift test) and
`mmm_frequentist` end to end, and a recovery test fits simulated data with known
contributions and checks their split across channels.
