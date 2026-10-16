# Guided modelling: priors, lift tests and the scorecard <span class="mapi-badge">v0.4</span>

This guide explains what happens behind the Guided-mode tasks of
[Guided mode](../concepts/guided-mode.md) when the model is a marketing-mix model
(`mmm.bayesian`, on [mmm-framework](../models/mmm-framework.md)): how beliefs in
business units become model-space priors, how
experiment results calibrate a fit, how the sanity scorecard is measured, and which views
the what-if sliders, forecasts and counterfactuals read. Everything that touches data runs
in the sandbox; the control plane only validates JSON.

| Task | Where it runs | What it writes |
|---|---|---|
| Business priors → model-space priors | a `translate_priors` run | `priors.json` (`PriorTranslationResult`), `plots.json` |
| Lift-test calibration | inside the fit | mmm-framework experiment likelihoods, `learned.calibration` |
| Sanity scorecard | at the end of the fit | `scorecard.json` (`Scorecard`), stored on the model version |
| What-if sliders | at the end of the fit | `ui/grids/response_curves.json` (`ResponseGridView`) |
| Forecast | a `forecast` score run | `ui/guided/forecast.json` (`ForecastView`) and a fan chart with the split line |
| Counterfactual | a `counterfactual` score run | `ui/guided/counterfactual.json` (`CounterfactualView`) |

## Business priors

A business prior states a belief about one channel in the units people think in:

```yaml
calibration:
  business_priors:
    channels:
      tv:     {roi: [1.5, 4.0], credibility: 0.8}           # "80% sure each euro returns 1.5 to 4"
      search: {contribution_share: [0.05, 0.15], credibility: 0.9}
    baseline_share: {share: [0.5, 0.8], credibility: 0.8}   # KPI without any media
```

### From beliefs to priors: the math

`mmm.bayesian` states each channel's effect as its **return on spend**: a channel with an
ROI prior samples

```text
roi[c]  ~ LogNormal(mu[c], sigma[c])
beta[c] = roi[c] * spend[c] / (y_std * sum over observations of S[c](x))
```

where `S[c]` is the channel's carried-over, saturated spend (mmm-framework's adstock and
saturation, with the same parameters) and `y_std` the KPI's scale. On the observed data the
channel's contribution over the fit window, divided by its spend, *is* `roi[c]` - exactly
the quantity a business ROI belief is about. The translation is therefore closed form:

- an ROI range `[L, H]` held with credibility `p` is the log-normal whose central `p`
  interval it is: `mu = (log L + log H) / 2`, `sigma = (log H - log L) / (2 z)` with `z` the
  standard normal's `(1 + p) / 2` quantile;
- a contribution-share range `[l, h]` is the ROI range `[l Y / spend[c], h Y / spend[c]]`,
  because `share[c] = roi[c] * spend[c] / Y` (`Y` the observed KPI total over the window);
- business units convert to model units with the pipeline's fitted factors (the model owns
  scaling, so these are usually 1).

Edge cases are reported as issues, never hidden:

- a range starting at 0 is read as `[H / 20, H]` (a log-normal has no mass at zero);
- a range narrower or wider than a prior can usefully state has its spread clamped to
  `[0.02, 4]` on the log scale, with a warning;
- a channel the data does not have, or a share for a channel without spend, is an error.

Channels without a belief keep their prior (`priors.roi_default`, or mmm-framework's
default). The result is written as `priors.roi.<channel> = {mu, sigma}`.

The **baseline share** - the KPI that happens without media: intercept, trend,
seasonality, panel levels and controls - is linear in the intercept `Normal(m, s)` (in KPI
standard deviations): `B = a (m + s eps) + E` with `a = n y_std / Y` and `E` the rest of the
baseline, drawn from mmm-framework's own prior of the model. `s` is solved by bisection so
the central interval of `B` has the stated width, then `m` centres it; when the rest alone
is already wider than the stated range, `s` stops at its floor and a warning says so. The
result is `priors.intercept = {mu, sigma}`.

The preview then samples the translated model's **prior predictive** - mmm-framework's
own graph, with another seed - and reports ROI, contribution share, baseline share and a
KPI fan in business units, plus the prior mass inside each stated range (`coverage`).

Amounts only translate through *linear* preprocessing (currency conversion; the model owns
scaling). A non-linear step on the KPI or spend refuses the translation with
`CALIBRATION_UNSUPPORTED`.

### The prior-translation run

Ask for a translation while drafting - from an unsaved document or a saved revision:

```bash
curl -X POST "$MAPI/v1/guided/prior-translations" \
  -H "Authorization: Bearer $MAPI_TOKEN" -H "X-Mapi-Project: acme/growth" \
  -H "Content-Type: application/json" \
  -d '{"spec_id": "spc_…", "revision": 2, "draws": 500,
       "business_priors": {"channels": {"tv": {"roi": [1.5, 4.0], "credibility": 0.8}}}}'
```

The API checks the beliefs statically first (channels against the dataset's levels, shares
that cannot add up, `BUSINESS_PRIORS_INVALID`; a model without the `business_priors`
capability, `CALIBRATION_UNSUPPORTED`) and answers `202` with the queued run. The run fits the
pipeline's preprocessing on all of the data (the scales a production refit learns), solves,
and samples the translated priors **afresh** for the preview:

`GET /v1/guided/prior-translations/{run_id}` returns, once done, the `PriorTranslationResult`:

- `priors`: the model-space priors in the `settings.priors` form, to write at `apply_at`
  (`/model/settings/priors`; `mapi_spec.calibration.apply_prior_translation` does it and
  records the run in `calibration.prior_translation_run`);
- `preview`: ROI and contribution-share distributions per channel, the baseline share and a
  KPI fan in business units against the observed KPI;
- `coverage`: the prior mass inside each stated range - close to the stated credibility when
  the translation is faithful;
- `issues`: everything above in plain words, plus lift tests whose implied ROI falls outside
  the prior's 90% range.

The run's plots (`GET /v1/runs/{run_id}/plots`) are `mmm.prior_beliefs` (each channel's prior
interval over the range you stated) and `core.prior_predictive` (the KPI the priors can
generate, against your data).

A spec that carries `business_priors` **without** a recorded translation run is translated at
fit time instead, with the same solve over the training data's scales; the priors used are in
the model's `learned.calibration.translated_priors`.

## Lift tests

A lift test is calibration data: extra spend on one channel (optionally in one geo) during a
window, and the incremental KPI it caused, with its uncertainty:

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
(`BayesianMMM.add_experiment_calibration`): a likelihood term on the model-implied
estimand, which updates the channel's effect, its saturation curve and its adstock kernel
jointly:

| The test | mmm-framework measurement |
|---|---|
| extra spend (`spend_delta > 0`) | marginal ROAS: the lift from scaling the window's spend by `100 x spend_delta / window spend` percent, per extra unit of spend, against `kpi_delta / spend_delta` |
| a holdout removing (nearly) all of the channel's spend in the window | the channel's contribution over the window, against `-kpi_delta` |
| a partial cut of at most 25% | marginal ROAS of the same size (the symmetric finite difference) |
| a larger partial cut | skipped (it cannot be expressed), with the reason |

`geo` restricts the measurement to that geo's observations; `se` (or the half-width of
`ci` over the normal quantile) is the measurement's standard error. Carry-over after the
window is not counted: tests report the lift measured during the test. The KPI likelihood
alone stays in `log_likelihood`, so `elpd_loo` is unchanged in meaning; which tests were
used or skipped, and why, is in `learned.calibration.lift_tests`.

Tests are checked three times, each time where the information is:

1. **at submit**, against the dataset version's summary: the channel and geo exist and the
   window lies inside the data (`SCHEMA_INVALID` at `/calibration/lift_tests/<i>/…`);
2. **in the sandbox, before fitting**, against the raw data itself;
3. **at the model stage**: the window must cover at least one model period, a holdout
   (negative `spend_delta`) may remove at most the spend the data shows, and the time
   labels must be dates.

During `evaluate_then_refit`, a test whose window lies after the training periods, or that
the end of the training periods cuts, is skipped
by the evaluation fit (logged) and used by the production refit.

## The sanity scorecard

At the end of every fit the runner measures each check of the
[catalog](../concepts/guided-mode.md#scorecard) it can answer and writes `scorecard.json`; the
finalizer stores it on the model version (`GET /v1/models/{id}/versions/{v}` → `scorecard`) and
the promotion gate reads it. A guided spec (`metadata.labels.guided: <template>`) runs its
template's checks with its thresholds; any other spec runs the whole catalog.

| Metric | Measured on |
|---|---|
| `r_hat_max`, `ess_bulk_min` | the production posterior's free parameters (the plugin's convergence diagnostics) |
| `divergence_rate` | `sample_stats.diverging`, as a share of all draws |
| `mape`, `coverage_90` | the holdout, through the score path (needs them in `evaluate.metrics`). `metrics.json` reports `mape` in percent (`1.4` is 1.4%); the check compares it as a fraction (`0.014` against `0.10`/`0.25`) |
| `accuracy_lift` | choice models: the holdout hit rate (the most likely product is the one bought) minus that of always guessing the holdout's most common product (`accuracy`, `accuracy_naive`; the `customer_choice` template evaluates them) |
| `negative_roi_share`, `roi_max` | the `response_curves` grid at the actual spend: per channel the share of draws with a negative return, and the largest posterior-mean ROI |
| `max_channel_share`, `baseline_share` | a channel's (or the baseline's) share of baseline + every channel, posterior mean |
| `prior_overlap_max` | the overlap of prior and posterior draws of the model's media parameters (`PyMCModel.prior_overlap_vars`, forward-sampled at fit time; cheap) |
| `missing_fraction` | the raw dataset's numeric variables |
| `history_years` | the model stage's time dim, in years: its periods over the periods in a year at the dim's frequency (or the one its dates follow), so 105 days and 15 weeks both count as about 0.3 years |
| `spend_cv_min` | the raw spend per channel over time |

A check that cannot be measured - no holdout split, no response curves, a non-MCMC model - is
listed in `skipped` with the reason. Every item carries `message_plain`, `message_expert`
(what was measured, on which parameter or channel), a `suggested_action` when it is not a pass
and a `docs_anchor` back to the catalog.

## What-if grids and "Run exact"

Fits with `response_curves` write `ui/grids/response_curves.json`: per thinned draw (up to
`outputs.ui_draws`, at most 500), channel and spend multiplier, the channel's total
contribution over the fit window in KPI units, plus the spend at every grid point, the actual
spend, the baseline (intercepts and controls) and the per-geo grids when they fit the size
cap. The sliders interpolate it per draw ([the contract](../concepts/guided-mode.md#5-what-if-sliders)).

"Run exact" compiles the scenario into a `counterfactual` score request with one `multiply`
intervention per changed channel. At grid points the two agree **per draw** to float
precision: the grid is computed from the same fitted curves, and linear preprocessing passes
the multiplier through unchanged (`packages/runner/tests/test_guided.py` checks it).

## Future covariates

A forecast needs the future values of its covariates - spend, prices. Guided forecasts do not
ask for an upload: `ScoreRequest.future_covariates` says how the runner extends the ones the
payload lacks, per series, from the fitted history (the server passes the model's fit dataset
to the score run as its `history` input):

| Policy | Future values |
|---|---|
| `supplied` (default for API callers) | from the request's data or scenario; missing ones are `MISSING_FUTURE_COVARIATE` |
| `carry_last` (default in Guided mode) | the last observed value of each series |
| `seasonal_naive` | the value one year earlier by date (52 weeks, 12 months, 365 days...), so the last observed year repeats even after a gap; a missing value steps back another year, and one missing in every year (or a history shorter than a year) falls back to `carry_last` with a warning |
| `zero` | zero - "no spend planned" |

Values the payload does supply are never replaced, and interventions apply after the fill, so
**budget × k** over a horizon scales the carried-forward spend. The score result's `issues`
name what was filled.

## Forecast and counterfactual views

A `forecast` score run writes `ui/guided/forecast.json` (`mapi.forecast/v1`): the actual KPI
over the fitted history (`fan.observed`), then the forecast quantiles after `split_at`
(`fan.values`), summed over geos per draw before the quantiles are taken, and the KPI total
over the horizon. Its `core.ppc_fan` plot shows the history and the dashed split line.

A `counterfactual` score run - for example a preset from
`mapi_spec.whatif.counterfactual_request` such as **channel off** or **budget × k** - writes
`ui/guided/counterfactual.json` (`mapi.counterfactual/v1`): the scenario, baseline and change
totals, the change as a share of the baseline and a per-period band of the change. Scenario
and baseline share their noise (common random numbers), so the change is precise even when
the totals are uncertain.

Both views exist only for models whose outputs are posterior draws; other representations
cannot be summed across geos.
