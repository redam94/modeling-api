# Guided modelling: priors, lift tests and the scorecard <span class="mapi-badge">v0.4</span>

This guide explains what happens behind the Guided-mode tasks of
[Guided mode](../concepts/guided-mode.md) when the model is a marketing-mix model
(`mmm.hierarchical_geo`): how beliefs in business units become model-space priors, how
experiment results calibrate a fit, how the sanity scorecard is measured, and which views
the what-if sliders, forecasts and counterfactuals read. Everything that touches data runs
in the sandbox; the control plane only validates JSON.

| Task | Where it runs | What it writes |
|---|---|---|
| Business priors → model-space priors | a `translate_priors` run | `priors.json` (`PriorTranslationResult`), `plots.json` |
| Lift-test calibration | inside the fit | the posterior (`lift_tests__model`), `learned.calibration` |
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

The model's contribution of channel `c` over the fit window, in KPI units, is

```text
C[c] = sum over geos g and periods t of  kpi_scale[g, t] * beta[g, c] * S[g, c, t]
```

where `kpi_scale` converts model units to KPI units (the pipeline's fitted scaling, read
through its inverse), `beta[g, c]` is the channel effect in geo `g` and `S` the carried-over,
saturated spend, a function of the adstock and saturation parameters. The two business
quantities are

```text
ROI[c]   = C[c] / spend[c]       spend[c]: the channel's total spend over the window
share[c] = C[c] / Y              Y:        the observed KPI total over the window
```

The translation gives the median channel effect a log-normal prior,
`beta_channel[c] = exp(mu[c] + sigma[c] * eps[c])`. With partial pooling
`beta[g, c] = beta_channel[c] * exp(beta_sigma[c] * z[g, c])`, so

```text
log ROI[c] = mu[c] + sigma[c] * eps[c] + log K[c] - log spend[c]
K[c]       = sum over g, t of  kpi_scale[g, t] * exp(beta_sigma[c] * z[g, c]) * S[g, c, t]
```

(Without pooling every geo draws its own effect and the log-sum runs over geos; with complete
pooling `z = 0`.) Two facts make the solve exact up to Monte Carlo error:

- `mu[c]` only **shifts** `log ROI[c]`: every term is proportional to `exp(mu[c])`;
- `K[c]` depends only on the *other* priors (adstock, saturation, pooling spread), which are
  independent of `beta_channel`, so it is sampled once, with common random numbers.

The stated range `[L, H]` held with credibility `p` fixes the `q = (1 - p) / 2` and `1 - q`
quantiles of `log ROI[c]`. Write `R(sigma)` for `log ROI[c]` with `mu[c] = 0`. Then

1. `sigma[c]` solves `Q[1 - q](R(sigma)) - Q[q](R(sigma)) = log(H / L)` by bisection,
   vectorised over channels (the interval widens with `sigma`);
2. `mu[c] = (log L + log H) / 2 - (Q[q] + Q[1 - q])(R(sigma[c])) / 2` centres it.

A share range works the same way with `Y` in place of `spend[c]`. Edge cases are reported as
issues, never hidden:

- if the other priors alone already make the interval wider than stated, `sigma[c]` stops at
  its floor (0.02) and a warning says so - the reported **coverage** shows by how much the
  range is missed;
- a range starting at 0 is read as `[H / 20, H]` (a log-normal has no mass at zero);
- a channel the data does not have, or a ROI for a channel without spend, is an error.

Channels without a belief keep their prior, refitted as the log-normal with the same 5% and
95% quantiles, so `beta_channel` stays one per-channel log-normal.

The **baseline share** (intercepts plus controls) is linear in the population mean of the
geo intercepts `m`: `B = a * (m + s * eps) + E`, so the same two-step solve runs in linear
space and gives `intercept: {distribution: Normal, mu: m, sigma: s}`. When the geo
spread and the control effects alone are wider than the stated range, their priors
(`intercept_sigma`, `gamma_control`) are narrowed by a common factor first, and an `info`
issue says by how much.

Amounts only translate through *linear* preprocessing (`mean_scale`, `max_abs_scale`,
currency conversion). A `log1p` or `standardize` step on the KPI or spend refuses the
translation with `CALIBRATION_UNSUPPORTED`.

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

### Business priors for `mmm.pymc_marketing`

The pymc-marketing adapter takes business priors too. pymc-marketing scales the KPI by its
maximum (`target_scale`) and each channel by its maximum, and every curve it offers here is
`beta * g(adstock(x))` (or `adstock(beta * g(x))` with `adstock_first: false`), `beta` being
its `saturation_beta` prior over `(*dims, channel)`. A channel's contribution in KPI units is
therefore

```text
C_c = target_scale * sum over panel cells k of beta[k, c] * A[k, c],
A[k, c] = sum over periods t of g(...)[t, k, c]
```

and `A` depends on the adstock and curve priors only. The translation draws `A` from
**pymc-marketing's own model** - prior draws of `channel_contribution / beta` - so its scaling,
carry-over padding and curves are exactly the fit's, and then solves as above for a
log-normal `saturation_beta` with one `mu` and `sigma` per channel (drawn independently per
panel cell, as pymc-marketing draws it): a bisection on `sigma` matches the range's width at
its credibility, `mu` centres it. The baseline share is linear in the intercept
(`intercept ~ Normal(m, s)` per cell, `B = a (m + s zbar) + E` with `a = target_scale * T *
cells / Y` and `E` the controls' and seasonality's share). pymc-marketing's default
seasonality prior (`Laplace(0, 1)` on the scaled KPI) is wide, so when `E` alone spreads the
baseline share over more than 80% of the range, the `gamma_control` and `gamma_fourier`
priors are narrowed by a common factor first - their contributions are linear in them, so the
spread shrinks by the same factor - and an `info` issue says by how much.

The priors land in the adapter's `settings.priors` (`saturation_beta`, `intercept`,
`gamma_control`, `gamma_fourier`, in pymc-marketing's scaled units), which it hands to
pymc-marketing as `model_config` priors; the preview, coverage and plots are the same as for
the geo MMM.

### `mmm.bayesinsight`: no business priors

The BayesInsight model's coefficient priors are already statements in the KPI's own units
(`coeff_prior` in `model_def.json`), so it does not declare business priors: state the ROI
you believe directly as the media coefficient's prior. It does take lift tests (below).

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

The fit adds one observation per test,

```text
kpi_delta ~ Normal(lift, se)
lift      = sum over the test's geos g and periods t in W of
            kpi_scale[g, t] * beta[g, c] * (S[g, c, t](x + dx) - S[g, c, t](x))
```

where `W` are the model periods whose label lies in the window and `dx` is the extra spend in
model units, spread over the window's cells in proportion to the channel's
actual spend there (evenly when there was none) and **carried over through the lag tensor**
within the window. Carry-over after the window is not counted: tests report the lift measured
during the test. (pymc-marketing's `add_lift_test_measurements` uses the saturation curve
alone; this version also carries the extra spend over.) The model's implied lift per draw is
kept as `lift_tests__model(lift_test)`; the KPI likelihood alone stays in
`log_likelihood`, so `elpd_loo` is unchanged in meaning.

Tests are checked three times, each time where the information is:

1. **at submit**, against the dataset version's summary: the channel and geo exist and the
   window lies inside the data (`SCHEMA_INVALID` at `/calibration/lift_tests/<i>/…`);
2. **in the sandbox, before fitting**, against the raw data itself;
3. **at the model stage**: the window must cover at least one model period, and a holdout
   (negative `spend_delta`) may remove at most the spend the data shows.

During `evaluate_then_refit`, a test whose window lies after the training periods, or that
the end of the training periods cuts, is skipped
by the evaluation fit (logged) and used by the production refit.

`mmm.pymc_marketing` accepts lift tests too, through pymc-marketing's own
`add_lift_test_measurements`: each test becomes rows of its window's average spend, extra
spend, lift and sigma *per period*, conditioned on the saturation curve (a Gamma on the
absolute lift, pymc-marketing's model). With panel `dims`, a test must name its `geo`.

`mmm.bayesinsight` observes each test under its own likelihood: `channel` names a media
variable (its analytic name, `TV_Total_Total_Total`, or its model-stage name), `geo` a label
of the first panel dim (the test then covers every level of the other panel dims there), and

```text
lift = sum over the test's cells p and periods t in W of
       mean(eta[p, t] + d_eta[p, t]) - mean(eta[p, t]),
d_eta[p, t] = coef[p] * sum over lags l of w[l] * (f(z + dz)[p, t, l] - f(z)[p, t, l])
```

with `mean` the likelihood's mean function (the identity for a Normal KPI, `exp` and the
zero-inflation share for counts, `exp(. + sigma^2 / 2)` for a log-normal), `f` the media
curve and `z` the indexed media - so the extra spend goes through BayesInsight's curve, then
its carry-over, then the link. The extra spend is spread and carried over within the window
as above; the implied lift is kept as `lift_tests__model`.

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
| `seasonal_naive` | the last observed year repeated (52 weeks, 12 months, 365 days...); a shorter history falls back to `carry_last` with a warning |
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
