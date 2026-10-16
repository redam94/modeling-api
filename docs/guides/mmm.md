# Marketing-mix models

`mapi-plugin-mmm` ships the platform's marketing-mix models (MMMs), both on
[mmm-framework](../models/mmm-framework.md):

| Plugin | What it is | Use it when |
|---|---|---|
| `mmm.bayesian` | mmm-framework's Bayesian MMM, national or hierarchical over geos and products | You want each channel's contribution and return with honest ranges, business priors and lift tests, what-if answers and budget plans |
| `mmm.frequentist` | mmm-framework's ridge estimator with bootstrap intervals | You need a first read in seconds, or want to compare many set-ups quickly |

Both are registered through the `mapi.models` entry point, so a spec only names them:
`use: mmm.bayesian@^1`. This guide walks through a complete analysis; the
[model reference](../models/mmm-framework.md) has every setting and how it maps onto
mmm-framework.

## From daily spend in local currency to weekly EUR decisions

A typical European media dataset is daily, per market and channel, in each market's own
currency. mmm-framework owns scaling, carry-over and seasonality, so the platform's
pipeline only converts currencies, aggregates to weeks and adds a holiday control - and
remembers every learned step so scoring replays exactly the same work:

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

What the platform does with this, without a line of model code:

- **Binding.** Your names (`market`, `revenue_local`) become the model's canonical names
  (`geo`, `y`) and are reversed on every output.
- **Split at the right stage.** The holdout is the last 8 *weeks*, cut after resampling,
  so no partial week leaks into training.
- **Evaluate, then refit.** The model is fitted on the training weeks, scored on the
  holdout *through the scoring path* (the same code a forecast uses), then refitted on all
  weeks so forecasts start from the latest data.
- **Owned scaling and carry-over.** mmm-framework scales the data itself and carries
  spend over with its own adstock kernel; a scoring window is replayed on top of the
  stored fit history, so carry-over from last month's TV burst reaches next month's sales.
  A scaling or lag step on `y` or `spend` is refused at validation (`CAPABILITY_UNSUPPORTED`).
- **Weekly EUR.** Predictions and contributions come back in the model stage's units -
  weekly EUR here - and say so in their `output_space` (`model-owned`) and `units`.

A runnable version of this spec, scaled down, ships as
`mapi_plugin_mmm.examples.E33_SPEC_YAML`, together with a simulator (`e33_dataset`) whose
true contributions are known.

## Settings at a glance

| Setting | Level | Default | Meaning |
|---|---|---|---|
| `hierarchy.pool_over` | basic | `[]` | Panel dims of the data (`geo`, `product`); each level gets its own baseline, partially pooled |
| `adstock.kind` / `l_max` | basic | `geometric`, 8 | Carry-over shape (`geometric`, `delayed`, `weibull`, `none`) and length |
| `saturation.kind` | basic | `logistic` | Diminishing returns (`logistic`, `hill`, `michaelis_menten`, `tanh`, `root`, `none`) |
| `trend.kind` | basic | `linear` | `none`, `linear`, `piecewise`, `spline` or `gp` |
| `seasonality.yearly_order` | basic | 2 | Yearly Fourier pairs |
| `channels.<c>` | advanced | - | Per-channel adstock/saturation overrides |
| `hierarchy.media_by_geo` | advanced | `false` | Channel effects per geo, partially pooled |
| `media_prior` | advanced | `roi` | Channel effects stated as returns on spend |
| `priors` | advanced | - | `roi.<c>: {mu, sigma}`, `roi_default`, `intercept` |
| `likelihood` | advanced | `normal` | `student_t` for heavy-tailed outliers |
| `new_levels` | advanced | `pooled` | How unseen geos/products are scored |
| `controls` | advanced | auto | Control variables (default: every other numeric variable over time) |
| `grid` | advanced | 0-2.5x in 26 steps | The response curves' spend-multiplier grid |

Guided mode shows the **basic** settings only; every field carries `x-ui-level` in the
manifest's JSON Schema. The [models catalog](../reference/generated/models-catalog.md)
lists them all with their defaults.

## Scoring

Score requests work like for every other model; these are the ones MMM users need most.

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

    Send daily spend in local currency for the next 8 weeks; get weekly EUR revenue.

=== "A new market"

    Add a market the model has never seen (`FR`). With `new_levels: hyperprior` its
    baseline is drawn from the fitted spread for every posterior draw, so its forecast is
    honest about what the model does not know: wider than a fitted market's. The response
    carries an `UNSEEN_LEVELS` notice.

=== "A weekly EUR plan"

    ```yaml
    mode: forecast
    horizon: {dim: time, periods: 8}
    inputs_stage: weekly          # enter after fx conversion and resampling
    ```

    Planners send weekly EUR budgets instead of inventing daily local-currency spend. No
    FX table is needed: the steps that use it are skipped. The forecast equals the
    raw-data path for the same spend.

=== "What if"

    ```yaml
    mode: counterfactual
    interventions: [{var: spend_local, sel: {channel: tv}, multiply: 1.2}]
    output: {kind: predictive, draws: 200}
    ```

    The scenario and the baseline share their noise (common random numbers), so the
    `delta` group shows the effect of the change, not sampling noise. Only the treated
    channel's contribution moves.

Every score returns `y_hat` and `contributions(component, geo, time)`: one component per
channel, `baseline`, `trend`, `seasonality` and one per control. They add up to `y_hat`.

## What a Guided report reads

`contributions`, `response_curves` and `marginal_cost` (in `outputs.derived`) are computed
once, at fit, over the first `outputs.ui_draws` posterior draws. `mmm.decomposition`,
`mmm.response_curves` and `mmm.marginal_cost` plot them; Guided-mode what-if sliders
interpolate the response curves per draw (instant, with joint uncertainty bands); the
scorecard's plausibility checks (negative or absurd ROI, a channel's share, the baseline
share) read them too. Business priors (ROI or share ranges) and lift tests calibrate the
model through the spec's `calibration` block: see
[Guided modelling](guided-modelling.md) and [the model reference](../models/mmm-framework.md#priors-in-business-terms).

## Optimising budgets

The platform optimises budgets with mmm-framework's planning module - no external
optimiser needed:

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

See [Optimise a budget](../tutorials/optimise-a-budget.md) for a walk-through and
[the model reference](../models/mmm-framework.md#budget-optimisation) for every option.
In Python, a fitted bundle's decision objective is also available directly:
`MMMBayesian().objective(bundle.model, budget, ScoreContext())` values a per-period budget
`spend(channel)` (or `spend(geo, channel)`) at steady state, per posterior draw.

## The fast frequentist model

`mmm.frequentist` takes the same data and settings and answers in seconds: transforms are
searched out of sample, the model is solved by ridge regression with non-negative channel
effects, and the ranges are bootstrap confidence intervals. Use it for a first look, then
fit `mmm.bayesian` before making decisions.

## Trust, but verify

Both plugins pass the SDK contract kit (`mapi_sdk.testing.check_model`): bundle round
trips, shuffled and subset markets, new levels, typed errors, the holdout scored through
the same path as a forecast, additive contributions. The conformance corpus runs them
through the whole spec -> fit -> score path (`e33`, `guided_mmm`, `mmm_frequentist`), and
the plugin's tests check the forward pass against mmm-framework's own forecaster and graph.
