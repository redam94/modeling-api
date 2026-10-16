# statsmodels time-series models

The `mapi-plugin-statsmodels` package brings twelve classical time-series models from
[statsmodels](https://www.statsmodels.org/) (BSD-3-Clause) into the platform. They cover
the everyday forecasting workhorses (ARIMA, exponential smoothing, theta), models with
drivers (ARIMAX, ARDL, error correction), systems of series that drive each other (VAR,
VECM, VARMAX, dynamic factors) and regime switching. All of them fit in seconds, run in the
time-series runner image (`mapi/base-forecast`) and pass the same contract kit as every
other plugin.

## Which model should I use?

Every model has an About card in the catalog (the web app's "Help me choose" uses them).
In short:

| Plugin | In plain words | Choose it when |
|---|---|---|
| `ts.sarimax` | Forecast with past values and drivers | You forecast one series a few periods ahead and know future prices, promotions or holidays |
| `ts.arima` | Forecast from past values | You have only the series itself |
| `ts.ets` | Exponential smoothing forecast | You need a fast, robust baseline with trend and season |
| `ts.theta` | Simple strong baseline | You want something simple that is hard to beat, to compare other models with |
| `ts.unobserved_components` | Trend, season and noise | You want to see the underlying trend without the noise |
| `ts.ardl` | Effects that build up over time | A driver's effect is spread over several periods |
| `ts.ecm` | Long-run and short-run effects | The result and its drivers trend together; you want the immediate and the lasting effect |
| `ts.var` | Series that drive each other | Several series affect each other; you want shocks and Granger tests |
| `ts.vecm` | Series tied together for good | Trending series keep a stable long-run relationship |
| `ts.varmax` | Series that drive each other, with drivers | Like VAR, with outside drivers known ahead |
| `ts.dynamic_factor` | Common movements across many series | Many series follow a few hidden common forces |
| `ts.markov_switching` | Switching between regimes | Behaviour jumps between distinct modes (normal / promotion-driven) |

Not sure the series is ready? The **time-series readiness** workflow template runs the
unit-root tests, chooses the differencing and the AR/MA orders, backtests `ts.sarimax`
against a naive forecast and only keeps it if it wins; see [Workflows](../concepts/workflows.md).

## Your data, in the model's names

The models use canonical names; bind your dataset's names onto them in `data.bind`:

| Canonical name | What it is |
|---|---|
| `t` | The time index: dates (default) or period numbers (`settings.time_kind: integer`). Policy `extend`: forecasts continue it. |
| `y` | The target. Univariate models: `y(t)`, or `y(<series...>, t)` for panels. Multivariate models: `y(variable, t)`. |
| `variable` | Multivariate models only: the series modelled jointly (realgdp, realcons, ...). |
| `<series...>` | Panel dims you name in `settings.series_dims` (`geo`, `product`). One model is fitted per series. |
| `<exog...>` | Drivers you name in `settings.exog`; `(<series...>, t)`; their future values are required for forecasts. |

A table with one column per series (realgdp, realcons, realinv) becomes `y(variable, t)`
with the ingest mapping's `wide` variables; see [From a spreadsheet to a Guided
draft](../guides/guided-data.md).

!!! note "Panels are a per-series loop"
    With `series_dims: [geo]` every geo gets its own model (its own orders, parameters and
    residual checks). Geos are a `fixed` dim: a forecast for a geo the model never saw is
    refused with `UNSEEN_LEVELS`. Multivariate models loop the same way: one joint VAR per
    geo, for example.

## An ARIMAX per geo

```python
--8<-- "statsmodels_models.py:arimax"
```

The bundle's `params` group is the parameter table: estimate, standard error, z/t
statistic, p-value and 95% interval for every parameter of every series.

## Forecasts and the drivers' future

```python
--8<-- "statsmodels_models.py:forecast"
```

What a score returns, for every model:

* **inside the fitted range**: the model's one-step-ahead predictions from the fitted
  history (the payload's targets and drivers are not used there);
* **after it**: a multi-step forecast from the end of the fit, using the drivers in the
  payload. A missing driver - or a missing value of one - fails with
  `MISSING_FUTURE_COVARIATE` (422). With `future_covariates: carry_last`,
  `seasonal_naive` or `zero` the platform extends drivers you leave out (see
  [Forecasting](../guides/time-series.md)).

A what-if on the future is just another payload: change the planned drivers (a promotion
every week, a 5% price rise) and forecast again.

Outputs are **quantiles** (`y_hat(quantile, ..., t)`; the representation `quantiles`): by
default 2.5%, 5%, 10%, 25%, 50%, 75%, 90%, 95% and 97.5%, so you get the median and the
50/80/90/95% bands (`settings.quantiles` changes them; 5%, 50% and 95% are always
included). They are **prediction intervals** - they include the noise of the next value -
for both output kinds, because statsmodels has no separate interval for the expected
value. Quantiles describe one cell each, so totals across geos or series are refused
(`CAPABILITY_UNSUPPORTED`); sum the series in the pipeline and model the total instead.

`counterfactual`, `simulate` and `filter` score modes are not offered (`MODE_NOT_SUPPORTED`).

## The models

### `ts.sarimax` and `ts.arima`

Seasonal ARIMA, with drivers (`ts.sarimax`, regression with ARIMA errors) or without
(`ts.arima`), estimated by exact maximum likelihood with the Kalman filter. Missing target
values are allowed (the filter skips them).

| Setting | Default | Meaning |
|---|---|---|
| `order` | `[1, 0, 0]` | `(p, d, q)`: past values, times differenced, past errors |
| `seasonal_order` | `[0, 0, 0, 0]` | `(P, D, Q, s)` for a pattern repeating every `s` periods |
| `trend` | `auto` | `n`, `c`, `t`, `ct`; `auto` is a constant unless the series is differenced |
| `exog` | `[]` | drivers (`ts.sarimax` only) |

`ts.arima` refuses a constant together with differencing (use `trend: t` for a drift).

### `ts.ets` and `ts.theta`

`ts.ets` is the error-trend-season model (`ETSModel`): `error`, `trend` (`add`/`mul`),
`damped_trend`, `seasonal` with `seasonal_periods`. Additive models have closed-form
intervals; multiplicative ones are simulated (`simulate_repetitions` paths, seeded by the
request's seed, so the same request gives the same quantiles). `ts.theta` is the theta
method with optional seasonal adjustment (`period`, `deseasonalize`, `method`, `theta`).

### `ts.unobserved_components`

A structural model: `level` (`local level`, `local linear trend`, `smooth trend`, ...),
optional stochastic `seasonal`, `cycle`, `autoregressive` noise and drivers. The smoothed
level, trend and season are stored in the bundle's `components` group and shown in the Model
Explorer's decomposition.

### `ts.ardl` and `ts.ecm`

`ts.ardl` regresses the result on its own `lags` and on `order` lags of each driver (least
squares); `auto: true` chooses them per series by `ic` up to those maxima. `ts.ecm` writes
the same model in error-correction form (statsmodels `UECM`): the coefficient on `y.L1` is
the **speed of adjustment** (half-life in the `ecm` group), the `long_run.*` rows of the
parameter table are the long-run effects with delta-method standard errors, and the
Pesaran-Shin-Smith **bounds test** (case from the trend: `c` is case 3) says whether a
long-run relationship exists - `levels_relationship`, `none` or `inconclusive`, also raised
as a scorecard diagnostic (`ECM_BOUNDS_TEST`).

### `ts.var`, `ts.vecm` and `ts.varmax`

`ts.var` fits a VAR by least squares; leave `lags` empty to choose the order by `ic` up to
`max_lags`. `ts.vecm` fits a Johansen VECM; leave `coint_rank` empty and the Johansen
`rank_test` (trace or max-eigenvalue) at `rank_signif` chooses it. A rank of 0 (no long-run
relationship: model the changes with `ts.var`) or of the number of series (they are
stationary: use `ts.var` on the levels) is refused with a plain-language
`SETTINGS_INVALID`. `ts.varmax` is VARMA(p, q) with drivers in state-space form (VARX when
`q = 0`).

```python
--8<-- "statsmodels_models.py:vecm"
```

Their bundles carry:

* `irf` - orthogonalised impulse responses `irf(response, shock, horizon)` up to
  `irf_horizon` (with asymptotic 95% bands for `ts.var`);
* `fevd` - the forecast-error variance decomposition `share(variable, horizon, shock)`;
* `granger` - pairwise Granger causality tests (`ts.var`, `ts.vecm`);
* `cointegration` - the Johansen table (statistic and critical value per tested rank),
  the chosen rank, `alpha` (speed of adjustment) and `beta` (the long-run relations,
  normalised on the first series) (`ts.vecm`).

### `ts.dynamic_factor`

A few hidden factors following a VAR drive many series (state space). The smoothed factors
and loadings are in the `factors` group. Standardise the series first
(`core.standardize`), as factor models assume comparable scales.

### `ts.markov_switching`

Hamilton's regime-switching regression (`order: 0`) or autoregression (`order > 0`): the
level, driver effects and noise can switch between `k_regimes` hidden regimes. The
`regimes` group holds the smoothed and filtered probability of each regime per period, the
transition matrix, expected durations and each regime's mean level. Forecasts are regime
mixtures: analytic for regressions, simulated (`simulations` seeded paths) for
autoregressions. Start values come from a fit on the standardised series, which avoids the
failures of statsmodels' default start values on series far from zero.

## What is stored, and why there is no pickle

A statsmodels results object is never stored: pickles are not allowed across the sandbox
boundary. A bundle keeps

* the model-stage training data (`observed_data`, and the drivers in `constant_data`);
* every estimated parameter (`params`), with the order the model needs in `learned`;
* fit statistics (`fit_stats`: AIC, BIC, HQIC, log-likelihood, observations, noise variance,
  whether the optimiser converged), residuals and residual tests (Ljung-Box, Jarque-Bera,
  ARCH-LM);
* the model-specific groups above.

Scoring rebuilds the results: state-space models (SARIMAX, ARIMA, UnobservedComponents,
VARMAX, DynamicFactor) and the ETS and Markov-switching models apply the stored parameters
to the stored data with their own filter (`model.filter(params)`, `smooth(params)`);
least-squares and closed-form models (VAR, VECM, ARDL/ECM, Theta) are re-estimated on the
stored data with the stored orders and checked against the stored parameters - a mismatch
fails with `INTEGRITY_FAILURE`.

## The Model Explorer

`StatsmodelsModel.explorer_views(fitted)` turns a bundle into Model Explorer sections
(`mapi_spec.views` DTOs): `params` (the table with 95% intervals and p-values),
`diagnostics` (fit statistics, Granger, Johansen and bounds tests), `residuals` (residuals
over time, their autocorrelation, Q-Q points and the residual tests per series), `irf`,
`fevd` and `decomposition`. Panels appear as facets.

## How it is tested

* The contract kit (`check_model`) passes for every plugin with About cards required.
* Parameters, in-sample predictions and forecast intervals equal direct statsmodels calls
  on the same data (SARIMAX, ARIMA, UnobservedComponents, ETS, Theta, ARDL, UECM, VAR,
  VECM, VARMAX, DynamicFactor, Markov switching); the UECM is also checked on Johansen and
  Juselius' Danish money-demand data, and the VAR(2) on US macro growth reproduces the
  coefficient table of the statsmodels documentation.
* Conformance fixtures `sm_arimax` (an ARIMAX panel with drivers) and `sm_vecm_macro`
  (a VECM on statsmodels' public-domain `macrodata`) run the whole fit, score and evaluate
  path; see [Conformance corpus](../guides/conformance.md).
