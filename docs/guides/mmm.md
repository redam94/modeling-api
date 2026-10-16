# Marketing-mix models

`mapi-plugin-mmm` ships two marketing-mix models (MMMs) as built-in plugins:

| Plugin | What it is | Use it when |
|---|---|---|
| `mmm.hierarchical_geo` | A hierarchical geo MMM written for the platform (PyMC) | You have several markets, want new markets and budgets scored honestly, and want response curves and marginal costs in your own currency |
| `mmm.pymc_marketing` | An adapter around [pymc-marketing](https://www.pymc-marketing.io)'s `MMM` | You already use pymc-marketing and want its model behind the platform: versioned bundles, forecasts, counterfactuals and plots |

Both are registered through the `mapi.models` entry point, so a spec only has to name
them: `use: mmm.hierarchical_geo@^1.3`.

## From daily spend in local currency to weekly EUR decisions

A typical European media dataset is daily, per market and channel, in each market's own
currency. The platform's preprocessing turns that into the weekly EUR panel the model
fits, and remembers every learned step so scoring replays exactly the same work:

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
  - {id: weekly,      use: core.resample@^1,   with: {dim: time, freq: W-SUN, how: sum}}
  - {id: scale_spend, use: core.mean_scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
  - {id: scale_y,     use: core.mean_scale@^1, with: {vars: [y], over: [time], unseen_levels: pooled}}
  - {id: lags,        use: core.lag_tensor@^1, with: {vars: [spend], along: time, within: [geo], l_max: 6, pad: first}}
  - {id: season,      use: core.fourier@^1,    with: {along: time, period: 52.18, order: 3, out: fourier}}
  - {id: hols,        use: core.holidays@^1,   with: {table: holidays, table_along: date, along: time}}
model:
  use: mmm.hierarchical_geo@^1.3
  settings:
    pooling: partial
    adstock: {kind: geometric, l_max: 6}
    saturation: {kind: logistic}
    priors:
      beta_channel: {distribution: HalfNormal, sigma: 1.0, dims: [channel]}
      lam:          {distribution: Gamma, alpha: 3, beta: 1, dims: [channel]}
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
- **Context.** `lag_tensor` stores each market's last weeks of spend; a forecast
  prepends them, so carry-over from last month's TV burst reaches next month's sales.
- **Original units.** Predictions and contributions come back in **weekly EUR**: the
  scaling steps are inverted, and the inversion stops at the weekly resample (daily
  values cannot be recovered from weekly sums). Every output says so in its
  `output_space` and `units` attributes.

A runnable version of this spec, scaled down, ships as
`mapi_plugin_mmm.examples.E33_SPEC_YAML`, together with a simulator
(`e33_dataset`) whose true parameters are known.

## The model

For geo `g`, channel `c` and week `t` (model units: the target and spend are
mean-scaled):

```text
adstock   a[g,c,t] = sum_l spend[g,c,t,l] * w[c,l]          w = alpha**l (normalised)
saturate  s[g,c,t] = (1 - exp(-lam[c] a)) / (1 + exp(-lam[c] a))
effects   beta[g,c] = beta_channel[c] * exp(beta_sigma[c] * z[g,c])     partial pooling
mean      mu[g,t]  = intercept[g] + sum_c beta[g,c] s[g,c,t] + controls[g,t]
target    y[g,t]  ~ Normal(mu[g,t], sigma)
```

`intercept[g]` is a hierarchical effect over geos, and every other numeric variable
along time (Fourier terms, holidays, prices…) is a control with one coefficient per
feature.

### Settings

| Setting | Level | Default | Meaning |
|---|---|---|---|
| `pooling` | basic | `partial` | `partial` shares channel effects across geos; `complete` uses one effect for all geos; `none` estimates each geo separately (and then new geos cannot be scored) |
| `adstock.kind` | basic | `geometric` | `geometric` decays from the spend week; `delayed` peaks after `theta` weeks |
| `saturation.kind` | basic | `logistic` | `logistic`, `hill` or `none` (linear) |
| `centred` | advanced | `false` | Centred parameterisation of geo effects; non-centred samples better with few geos |
| `adstock.l_max` | advanced | from the data | Must equal the `lag_tensor` `l_max` |
| `adstock.normalize` | advanced | `true` | Lag weights sum to one, so a constant spend has steady-state adstock equal to the spend |
| `priors` | advanced | see below | Prior overrides by parameter |
| `controls` | advanced | auto | Control variables (default: every other numeric variable along time) |
| `grid` | advanced | 0–2× in 21 steps | The spend-multiplier grid of the response curves |

Guided mode shows the **basic** settings only; every field carries `x-ui-level` in the
manifest's JSON Schema.

### Priors

Priors use pymc-marketing's dict form: a distribution, numeric parameters and the
canonical dims the parameter varies over.

| Parameter | Support | Default | Meaning |
|---|---|---|---|
| `intercept` | real | `Normal(0.5, 0.5)` | Population mean of the geo intercepts |
| `intercept_sigma` | positive | `HalfNormal(0.25)` | Spread of the geo intercepts |
| `beta_channel` | positive | `HalfNormal(1)` per channel | Typical channel effect at full saturation |
| `beta_sigma` | positive | `HalfNormal(0.3)` per channel | How much a channel's effect varies across geos |
| `alpha` | unit interval | `Beta(1, 3)` per channel | Weekly adstock decay |
| `theta` | positive | `HalfNormal(2)` per channel | Delayed adstock: weeks to the peak |
| `lam` | positive | `Gamma(3, 1)` per channel | Logistic saturation rate |
| `slope`, `half` | positive | `Gamma(3, 2)`, `Gamma(2, 2)` | Hill curve |
| `gamma_control` | real | `Normal(0, 0.5)` | Each control feature's coefficient |
| `sigma` | positive | `HalfNormal(0.5)` | Observation noise |

The allow-list covers `Normal`, `StudentT`, `Laplace`, `HalfNormal`, `HalfStudentT`,
`HalfCauchy`, `Exponential`, `Gamma`, `InverseGamma`, `LogNormal`, `Weibull`, `Beta`,
`Uniform` and `TruncatedNormal`. A prior whose support does not fit its parameter (a
`Normal` media effect, a `HalfNormal` adstock decay) is rejected when the spec is
validated, before anything runs. A list-valued parameter gives one value per label:

```yaml
priors:
  lam: {distribution: Gamma, alpha: [3, 2, 2], beta: 1, dims: [channel]}
```

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

    Add a market the model has never seen (`FR`). Its intercept and channel effects are
    drawn from the fitted hyperpriors for every posterior draw, so its forecast is honest
    about what the model does not know: wider than a fitted market's. The response
    carries an `UNSEEN_LEVELS` notice.

=== "A weekly EUR plan"

    ```yaml
    mode: forecast
    horizon: {dim: time, periods: 8}
    inputs_stage: weekly          # enter after fx conversion and resampling
    ```

    Planners and optimisers send weekly EUR budgets (`spend_local(market, channel, date)`
    on W-SUN weeks) instead of inventing daily local-currency spend. No FX table is needed:
    the steps that use it are skipped. The forecast equals the raw-data path for the same
    spend.

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
channel, `baseline` and one per control. They add up to `y_hat`.

## Derived quantities: what a Guided report reads

Ask for them in `outputs.derived`; they are computed once, at fit, over the first
`outputs.ui_draws` posterior draws, and stored in the bundle **in original units**:

- **`contributions`**: `channel_contribution(sample, channel, geo, time)`, `baseline` and
  one variable per control, over the fit window. `mmm.decomposition` plots them.
- **`response_curves`**: `contribution(sample, channel, multiplier, geo)`, the total
  contribution over the fit window when a channel's spend is scaled by the multiplier,
  plus the EUR spend on every grid point. `mmm.response_curves` and `mmm.marginal_cost`
  plot them, and Guided-mode **what-if sliders** interpolate them per draw: instant, and
  the uncertainty bands stay joint.
- **`marginal_cost`**: the extra EUR of spend needed for one extra EUR of revenue at
  current spend (and its inverse, marginal ROI), per geo and per channel.

`response_curves` also carries the `baseline` (intercepts and controls, per draw and per
geo), so the what-if grid view closes the expected KPI. Business priors (ROI or share
ranges) and lift tests calibrate this model through the spec's `calibration` block: see
[Guided modelling](guided-modelling.md).

## Optimising budgets

`mmm.hierarchical_geo` declares `decision: true`. Its objective scores a budget per
posterior draw:

```python
import xarray as xr
from mapi_plugin_mmm import HierarchicalGeoMMM
from mapi_sdk.models import ScoreContext

budget = xr.Dataset(
    {"spend": ("channel", [40_000.0, 25_000.0, 10_000.0])},
    coords={"channel": ["tv", "search", "social"]},
    attrs={"periods": 13},                       # EUR per week, for a quarter
)
value = HierarchicalGeoMMM().objective(bundle.model, budget, ScoreContext())
value.dims   # ('chain', 'draw'): expected revenue of the plan, per draw
```

Budgets are in original units (EUR per week here) and so is the result; this uses the
unit conversions cached with `response_curves`, so request that derived quantity (or pass
`attrs={"units": "model"}` to work in model units). A channel budget is split across geos
by each geo's share of that channel's spend; pass `spend(geo, channel)` to set it
yourself. The value assumes the spend is sustained long
enough for carry-over to settle (steady state).

## The pymc-marketing adapter

`mmm.pymc_marketing` runs `pymc_marketing.mmm.MMM` (version 1.2, on PyMC 6). pymc-marketing
scales the data, carries spend over and reports original units itself, so the adapter
declares `owns: [scaling, carryover, inverse_units]` and the platform steps aside:

- keep scaling and lag steps **out** of the pipeline (currency conversion, resampling and
  holidays are fine);
- scoring prepends the last `l_max` fit weeks before any window, so a four-week window
  scores exactly like the same weeks of the full series;
- outputs are labelled `model-owned`; holdout metrics are not inverted a second time.

```yaml
model:
  use: mmm.pymc_marketing@^1
  settings:
    dims: [geo]                    # panel dims besides time and channel
    adstock: {l_max: 4}
    saturation: {kind: logistic}
    controls: [holiday]
    yearly_seasonality: 2
  fit: {engine: nutpie, tune: 1000, draws: 1000, chains: 4}
```

Bundles never contain pickle: the adapter stores the posterior, pymc-marketing's
`fit_data` and its JSON model description, and rebuilds the model through
pymc-marketing's own loading path when it scores. Markets are a fixed dim for this
adapter: pymc-marketing cannot score a market it has not seen.

`settings.priors` overrides pymc-marketing's priors (`saturation_beta`, `intercept`,
`gamma_control`, `gamma_fourier`, in its scaled units). Business priors and lift tests in the
spec's `calibration` block calibrate the adapter as they do the geo MMM; the business priors
are translated on pymc-marketing's own model (see
[Guided modelling](guided-modelling.md#business-priors-for-mmmpymc_marketing)).

## BayesInsight models

`mmm.bayesinsight` fits a BayesInsight `model_def.json` as BayesInsight defines it -
likelihoods, priors in KPI units, curves, carry-over, signs, effects per row id, seasons and
local trends. Import a BayesInsight project with `"target": "mmm.bayesinsight"` (see
[the import](guided-data.md#importing-for-mmmbayesinsight)); the
[models reference](../reference/models.md#mmmbayesinsight-a-bayesinsight-model_defjson)
documents every feature.

## Trust, but verify

Both plugins pass the SDK contract kit (`mapi_sdk.testing.check_model`): bundle round
trips, shuffled and subset markets, typed errors, the holdout scored through the same
path as a forecast, additive contributions. The conformance corpus runs both through the
whole spec → fit → score path, and a parameter-recovery test fits simulated data with
known adstock, saturation and channel effects and checks they come back (adstock decay
within 0.1, EUR contributions within 20%).
