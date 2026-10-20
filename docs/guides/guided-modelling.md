# Guided modelling: priors, lift tests and the scorecard <span class="mapi-badge">v0.4</span>

This guide tells what occurs behind the Guided-mode tasks of
[Guided mode](../concepts/guided-mode.md) when the model is a marketing-mix model
(`mmm.bayesian`, on [mmm-framework](../models/mmm-framework.md)). It tells these things:

- How beliefs in business units become priors in model space.
- How experiment results calibrate a fit.
- How the platform measures the sanity scorecard.
- Which views the what-if sliders, forecasts and counterfactuals read.

All the tasks that touch data run in the sandbox. The control plane only validates JSON.

| Task | Where it runs | What it writes |
|---|---|---|
| Business priors → model-space priors | a `translate_priors` run | `priors.json` (`PriorTranslationResult`), `plots.json` |
| Lift-test calibration | in the fit | mmm-framework experiment likelihoods, `learned.calibration` |
| Sanity scorecard | at the end of the fit | `scorecard.json` (`Scorecard`), stored on the model version |
| What-if sliders | at the end of the fit | `ui/grids/response_curves.json` (`ResponseGridView`) |
| Forecast | a `forecast` score run | `ui/guided/forecast.json` (`ForecastView`) and a fan chart with the split line |
| Counterfactual | a `counterfactual` score run | `ui/guided/counterfactual.json` (`CounterfactualView`) |

## The marketing-mix templates <span class="mapi-badge">v0.12</span> { #mmm-templates }

Three Guided templates make a marketing-mix model. Select the template that agrees with your
data:

| Template | Model | Your data | Tasks |
|---|---|---|---|
| `geo_mmm` - Geo marketing mix | `mmm.bayesian` | The result, spend for each channel and optional controls, for each market | what-if, forecast, counterfactual, business priors, lift tests |
| `mediated_mmm` - Marketing mix with an intermediate measure | `mmm.nested` | One national result, spend for each channel and one or more intermediate measures (visits, searches, awareness) | what-if, forecast, counterfactual, lift tests |
| `multi_kpi_mmm` - Marketing mix for related products | `mmm.multivariate` | Two or more national results (one column for each) and spend for each channel | what-if, forecast, counterfactual |

**`mediated_mmm`** asks for the result, the date, the spend columns and the intermediate
measures. The measures become one variable along the `mediator` dim. A measure can have
empty periods, for example a monthly survey. Role detection also suggests columns that are
mostly empty for this question. The labels keep the words of each column (`site_visits`).
The weekly resample takes the mean of each measure in a week and keeps a week without a
value empty. The report shows `mmm.mediation`: the direct and the indirect effect of each
channel.

**`multi_kpi_mmm`** asks for two or more result columns. They become `y` along the
`outcome` dim (`sales_premium` labels `premium`). Use the same units in each column. The
what-if sliders and the budget plan use the total of the results. The report shows
`mmm.waterfall` for each result. The template does not set effects between the results,
because the answers do not tell which result acts on which. To add them, open the spec in
the editor and set `cross_effects` of `mmm.multivariate` (then add the derived quantity
`cross_effects` and the chart `mmm.cross_effects`). These effects are associations, not
causes.

The two templates fit national data only. They take no control variables, and they do not
have business priors. All three templates keep `adstock` and show `mmm.roi`. They use the
same scorecard checks.

## The causal templates <span class="mapi-badge">v0.13</span> { #causal-templates }

Two Guided templates measure the effect of a change. They fit once on all periods
(`train_only`), because the effect is the result of the fit:

| Template | Model | Your data | Tasks |
|---|---|---|---|
| `campaign_impact` - Effect of a campaign | `causal.impact` | One result over time, optional control columns, the start date and the optional end date of the change | — |
| `geo_lift` - Geo-lift test | `causal.synthetic_control` | The result for each market over time, the names of the test markets, the start and end dates of the test | — (the report has a lift-test card) |

**`campaign_impact`** asks for the result, the date, the control columns, the start date and
the end date. The control columns are optional. Each control column becomes its own variable,
and the spec lists them in `controls`. Leave the end date empty if the change did not stop.
The template adds a yearly pattern (3 harmonics) for weekly data, and a weekly pattern for
daily data.

**`geo_lift`** asks for the result, the date, the market column, the test markets, the start
date and the end date. Type the test markets as text, with commas between the names. The
draft has the list of the names in `treated`. The other markets make the synthetic control.

The two templates use the scorecard checks of convergence and of missing values. The report
shows `causal.impact` first. The templates have no tasks, thus their steps do not include the
scenarios step: **Next** on the results goes to **Share**. The share step offers the causal
charts for embedding. See [Measure the effect of a change](causal.md).

## Business priors

A business prior gives a belief about one channel, in the units that persons use:

```yaml
calibration:
  business_priors:
    channels:
      tv:     {roi: [1.5, 4.0], credibility: 0.8}           # "80% sure each euro returns 1.5 to 4"
      search: {contribution_share: [0.05, 0.15], credibility: 0.9}
    baseline_share: {share: [0.5, 0.8], credibility: 0.8}   # KPI without any media
```

### From beliefs to priors: the math

`mmm.bayesian` gives the effect of each channel as its **return on spend**. A channel with an
ROI prior samples:

```text
roi[c]  ~ LogNormal(mu[c], sigma[c])
beta[c] = roi[c] * spend[c] / (y_std * sum over observations of S[c](x))
```

In this equation, `S[c]` is the carried-over, saturated spend of the channel. This is the
adstock and saturation of mmm-framework, with the same parameters. `y_std` is the scale of
the KPI. On the observed data, the contribution of the channel over the fit window, divided by
its spend, *is* `roi[c]`. This is the quantity that a business ROI belief is about. Thus the
translation has a closed form:

- An ROI range `[L, H]` with credibility `p` is the log-normal that has this range as its
  central `p` interval. Thus `mu = (log L + log H) / 2` and
  `sigma = (log H - log L) / (2 z)`. Here, `z` is the `(1 + p) / 2` quantile of the standard
  normal.
- A contribution-share range `[l, h]` is the ROI range `[l Y / spend[c], h Y / spend[c]]`.
  The reason is that `share[c] = roi[c] * spend[c] / Y`. Here, `Y` is the observed KPI total over the
  window.
- The fitted factors of the pipeline change business units to model units. The model does
  its own scaling, thus these factors are usually 1.

The platform shows edge cases as issues. It never hides them:

- A range that starts at 0 becomes `[H / 20, H]`, because a log-normal has no mass at zero.
- A range can be narrower or wider than a prior can usefully give. Then the platform clamps
  its spread to `[0.02, 4]` on the log scale. It gives a warning.
- A channel that the data does not have is an error. A share for a channel without spend is
  also an error.

Channels without a belief keep their prior (`priors.roi_default`, or the default of
mmm-framework). The platform writes the result as `priors.roi.<channel> = {mu, sigma}`.

The **baseline share** is the KPI that occurs without media: intercept, trend, seasonality,
panel levels and controls. It is linear in the intercept `Normal(m, s)`, in KPI standard
deviations: `B = a (m + s eps) + E`. Here, `a = n y_std / Y`, and `E` is the rest of the
baseline, drawn from the prior of the model in mmm-framework. The platform uses bisection to
find `s`, so that the central interval of `B` has the given width. Then `m` puts the interval
at the center. If the rest alone is already wider than the given range, `s` stops at its
minimum and a warning tells this.

The result is `priors.intercept = {mu, sigma}`.

The preview then samples the **prior predictive** of the translated model. It uses the graph
of mmm-framework, with a different seed. The preview gives ROI, contribution share, baseline
share and a KPI fan in business units. It also gives the prior mass in each given range
(`coverage`).

Amounts translate only through *linear* preprocessing (currency conversion). The model does
its own scaling. If a non-linear step applies to the KPI or the spend, the platform refuses the
translation with `CALIBRATION_UNSUPPORTED`.

### The prior-translation run

You can ask for a translation while you write a draft. Use an unsaved document or a saved
revision:

```bash
curl -X POST "$MAPI/v1/guided/prior-translations" \
  -H "Authorization: Bearer $MAPI_TOKEN" -H "X-Mapi-Project: acme/growth" \
  -H "Content-Type: application/json" \
  -d '{"spec_id": "spc_…", "revision": 2, "draws": 500,
       "business_priors": {"channels": {"tv": {"roi": [1.5, 4.0], "credibility": 0.8}}}}'
```

First, the API checks the beliefs statically:

- It checks the channels against the levels of the dataset, and finds shares that cannot add
  up (`BUSINESS_PRIORS_INVALID`).
- It finds a model without the `business_priors` capability (`CALIBRATION_UNSUPPORTED`).

Then it answers `202` with the queued run. The run fits the preprocessing of the pipeline on
all the data. These are the scales that a production refit learns. The run solves the
equations. Then it samples the translated priors **again** for the preview.

When the run is done, `GET /v1/guided/prior-translations/{run_id}` gives the
`PriorTranslationResult`:

- `priors`: The model-space priors in the `settings.priors` form, to write at `apply_at`
  (`/model/settings/priors`). `mapi_spec.calibration.apply_prior_translation` writes them. It
  also records the run in `calibration.prior_translation_run`.
- `preview`: The distributions of ROI and contribution share for each channel, and the
  baseline share. It also has a KPI fan in business units against the observed KPI.
- `coverage`: The prior mass in each given range. If the translation is correct, this value is
  near the given credibility.
- `issues`: All the items above in plain words. Also, the lift tests with an implied ROI that
  is outside the 90% range of the prior.

The plots of the run (`GET /v1/runs/{run_id}/plots`) are these:

- `mmm.prior_beliefs`: The prior interval of each channel over the range that you gave.
- `core.prior_predictive`: The KPI that the priors can make, against your data.

A spec can have `business_priors` **without** a recorded translation run. In this case, the
platform translates the priors at fit time. It uses the same solve over the scales of the
training data. The priors that the fit used are in `learned.calibration.translated_priors` of
the model.

## Lift tests

A lift test is calibration data. It gives the extra spend on one channel during a window,
optionally in one geo. It also gives the incremental KPI that the spend caused, with its
uncertainty:

```yaml
calibration:
  lift_tests:
    - id: tv-holdout-2026q2
      channel: tv
      geo: DE
      start: 2026-04-06
      end: 2026-05-03
      spend_delta: -120000      # TV off: the spend removed
      kpi_delta: -260000        # the revenue lost
      ci: [-380000, -140000]    # or se: 73000
      ci_level: 0.9
```

The fit adds each test to mmm-framework as an **experiment likelihood**
(`BayesianMMM.add_experiment_calibration`). This is a likelihood term on the estimand that the
model implies. It updates the effect of the channel, its saturation curve and its adstock
kernel together:

| The test | mmm-framework measurement |
|---|---|
| extra spend (`spend_delta > 0`) | Marginal ROAS: the lift when the spend of the window increases by `100 x spend_delta / window spend` percent, for each extra unit of spend, against `kpi_delta / spend_delta` |
| a holdout that removes all (or almost all) of the spend of the channel in the window | The contribution of the channel over the window, against `-kpi_delta` |
| a partial cut (less than a full holdout) | Skipped, with the reason. A partial cut measures the return *below* the current spend. mmm-framework can calibrate only on the return above the current spend (an increase) or on a full holdout. With diminishing returns, the two are different. Thus, if the platform used one for the other, the curve would have a bias |

`geo` limits the measurement to the observations of that geo. `se` is the standard error of
the measurement. If you give `ci`, the standard error is the half-width of `ci` divided by the
normal quantile. The fit does not count carry-over after the window, because tests give the
lift measured during the test. Only the KPI likelihood stays in `log_likelihood`. Thus the
meaning of `elpd_loo` does not change. `learned.calibration.lift_tests` tells which tests the
fit used or skipped, and why.

The platform checks tests three times. Each check occurs where the information is:

1. **At submit**, against the summary of the dataset version. The channel and the geo must
   exist, and the window must be in the data (`SCHEMA_INVALID` at
   `/calibration/lift_tests/<i>/…`).
2. **In the sandbox, before the fit**, against the raw data.
3. **At the model stage**. The window must include a minimum of one model period. A holdout
   (negative `spend_delta`) can remove a maximum of the spend that the data shows. The time
   labels must be dates.

During `evaluate_then_refit`, the evaluation fit skips some tests and logs them. These are
tests with a window after the training periods, or a window that the end of the training
periods cuts. The production refit uses these tests.

### A lift test from a geo-lift fit <span class="mapi-badge">v0.13</span> { #lift-test-from-a-geo-lift-fit }

The `geo_lift` template fits `causal.synthetic_control` on the markets of a test. Its report
has the card **Lift test for your marketing mix**. Type the channel and the spend change of the
test. The card then shows the YAML entry for `calibration.lift_tests`: `kpi_delta` is the
mean of the total effect, and `se` is its sd. With one test market, the entry has the geo of
that market. With two or more test markets, the entry is national. The card uses the test
markets of the revision that the last fit used, also when you changed them after that fit. Copy
the entry into the spec of the marketing mix model. The workflow template `geo_lift_mmm` does
the same steps in one run. See [Measure the effect of a change](causal.md#feed-the-result-to-the-marketing-mix-model).

## The sanity scorecard

At the end of each fit, the runner measures each check of the
[catalog](../concepts/guided-mode.md#scorecard) that it can answer. It writes
`scorecard.json`. The finalizer stores the scorecard on the model version
(`GET /v1/models/{id}/versions/{v}` → `scorecard`). The promotion gate reads it. A guided spec
(`metadata.labels.guided: <template>`) runs the checks of its template with its thresholds.
All other specs run the full catalog.

| Metric | Measured on |
|---|---|
| `r_hat_max`, `ess_bulk_min` | The free parameters of the production posterior (the convergence diagnostics of the plugin) |
| `divergence_rate` | `sample_stats.diverging`, as a share of all draws |
| `mape`, `coverage_90` | The holdout, through the score path. They must be in `evaluate.metrics`. `metrics.json` gives `mape` in percent (`1.4` is 1.4%). The check compares it as a fraction (`0.014` against `0.10`/`0.25`) |
| `accuracy_lift` | Choice models: the holdout hit rate minus the hit rate of a guess that is always the most common product of the holdout. A hit is when the most probable product is the product bought (`accuracy`, `accuracy_naive`). The `customer_choice` template evaluates them |
| `negative_roi_share`, `roi_max` | The `response_curves` grid at the actual spend. For each channel: the share of draws with a negative return, and the largest posterior-mean ROI |
| `max_channel_share`, `baseline_share` | The share of a channel (or of the baseline) in the baseline plus all channels, posterior mean |
| `prior_overlap_max` | The overlap of the prior and posterior draws of the media parameters of the model (`PyMCModel.prior_overlap_vars`, forward-sampled at fit time, at a low cost) |
| `missing_fraction` | The numeric variables of the raw dataset that contain observations. The check does not count the variables with the role `constant`, `index` or `mask` (for example the padding of the outlines of a map). It also does not count the variables that the model declares in `structural_missing` (for example the cells without a sensor of `sci.heat_plate`). The expert message names the variables that the check does not count |
| `history_years` | The time dim of the model stage, in years. This is its periods divided by the periods in a year at the frequency of the dim (or the frequency of its dates). Thus 105 days and 15 weeks both count as about 0.3 years |
| `spend_cv_min` | The raw spend for each channel over time |

If the runner cannot measure a check, the check is in `skipped` with the reason. Examples are
no holdout split, no response curves, or a model that is not MCMC. Each item has these fields:

- `message_plain`.
- `message_expert`: What the runner measured, and on which parameter or channel.
- `suggested_action`, if the item is not a pass.
- `docs_anchor`: A link back to the catalog.

## What-if grids and "Run exact"

Fits with `response_curves` write `ui/grids/response_curves.json`. For each thinned draw,
channel and spend multiplier, the file has the total contribution of the channel over the fit
window, in KPI units. The number of draws is a maximum of `outputs.ui_draws`, and never more
than 500. The file also has these items:

- The spend at each grid point.
- The actual spend.
- The baseline (intercepts and controls).
- The grids for each geo, if they are in the size cap.

The sliders interpolate the grid for each draw ([the contract](../concepts/guided-mode.md#5-what-if-sliders)).

"Run exact" compiles the scenario into a `counterfactual` score request. The request has one
`multiply` intervention for each changed channel. At grid points, the two results agree **for
each draw** to float precision. The reason is that the grid comes from the same fitted curves.
Also, linear preprocessing does not change the multiplier. `packages/runner/tests/test_guided.py`
checks this.

## Future covariates

A forecast needs the future values of its covariates, for example spend and prices. Guided
forecasts do not ask for an upload. `ScoreRequest.future_covariates` tells how the runner
extends the covariates that the payload does not have. It does this for each series, from the
fitted history. The server gives the fit dataset of the model to the score run as its
`history` input:

| Policy | Future values |
|---|---|
| `supplied` (default for API callers) | From the data or the scenario of the request. Missing values get `MISSING_FUTURE_COVARIATE` |
| `carry_last` (default in Guided mode) | The last observed value of each series |
| `seasonal_naive` | The value one year before, by date (52 weeks, 12 months, 365 days, and so on). Thus the last observed year repeats, also after a gap. If a value is missing, the runner goes back one more year. If the value is missing in all years, or the history is shorter than one year, the runner uses `carry_last` with a warning |
| `zero` | Zero: "no spend planned" |

The runner never replaces values that the payload gives. Interventions apply after the fill.
Thus **budget × k** over a horizon scales the carried-forward spend. The `issues` of the score
result tell which values the runner filled.

## Forecast and counterfactual views

A `forecast` score run writes `ui/guided/forecast.json` (`mapi.forecast/v1`). The file has
these items:

- The actual KPI over the fitted history (`fan.observed`).
- The forecast quantiles after `split_at` (`fan.values`). The run adds the geos together for
  each draw before it calculates the quantiles.
- The KPI total over the horizon.

Its `core.ppc_fan` plot shows the history and the dashed split line.

A `counterfactual` score run writes `ui/guided/counterfactual.json`
(`mapi.counterfactual/v1`). An example is a preset from
`mapi_spec.whatif.counterfactual_request`, such as **channel off** or **budget × k**. The file
has these items:

- The totals of the scenario, the baseline and the change.
- The change as a share of the baseline.
- A band of the change for each period.

The scenario and the baseline share their noise (common random numbers). Thus the change is
precise, also when the totals are uncertain.

The two views exist only for models with outputs that are posterior draws. The platform cannot
add other representations across geos.
