# statsmodels time-series models

The `mapi-plugin-statsmodels` package brings twelve classical time-series models from
[statsmodels](https://www.statsmodels.org/) (BSD-3-Clause) into the platform. They cover
the everyday forecasting workhorses (ARIMA, exponential smoothing, theta), models with
drivers (ARIMAX, ARDL, error correction), systems of series that drive each other (VAR,
VECM, VARMAX, dynamic factors) and regime switching. All of them fit in seconds, run in the
time-series runner image (`mapi/base-forecast`) and pass the same contract kit as every
other plugin.

## Which model should I use?

Every model has an About card in the catalog: the web app's *Help me choose*, `mapi choose`
and `api.catalog.recommend()` rank them for your goal and data (see
[Choosing a model](../guides/choosing-a-model.md)). In short:

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

Each section starts with the model's About card (what the web app, `mapi about` and
`api.catalog.about()` show; a docs test keeps the quotes identical to the catalog), then a
minimal spec and, for models with drivers, a forecast from planned drivers. The specs all use
this small wrapper - the data binding, a holdout of the last 8 periods scored before the
model is refitted on everything - and synthetic or public data from
`mapi_plugin_statsmodels.datasets`; the documentation tests run every one of them
(`docs/snippets/statsmodels_catalog.py`):

```python
--8<-- "statsmodels_catalog.py:common"
```

### `ts.sarimax` - forecast with past values and drivers

> Forecasts a series from its own recent past, its seasonal pattern and outside drivers you know ahead, such as planned prices or holidays.

**Use it when** you forecast one regular series a few periods ahead and known future drivers
(prices, promotions, holidays) matter. **Pick something else when** you have less than about
50 periods, or several series drive each other (use `ts.var`).

```python
--8<-- "statsmodels_catalog.py:sarimax"
```

Seasonal ARIMA with drivers - a regression with ARIMA errors - estimated by exact maximum
likelihood with the Kalman filter. Missing target values are allowed (the filter skips them).
The forecast needs the drivers' planned future values (here `plan` without `sales`); a missing
one fails with `MISSING_FUTURE_COVARIATE` unless the request says how to extend it.

| Setting | Default | Meaning |
|---|---|---|
| `order` | `[1, 0, 0]` | `(p, d, q)`: past values, times differenced, past errors |
| `seasonal_order` | `[0, 0, 0, 0]` | `(P, D, Q, s)` for a pattern repeating every `s` periods |
| `trend` | `auto` | `n`, `c`, `t`, `ct`; `auto` is a constant unless the series is differenced |
| `exog` | `[]` | drivers |
| `series_dims` | `[]` | panel dims: one model per series |

Not sure about `order`? The [readiness workflow](../guides/workflow-cookbook.md#readiness-to-arimax)
chooses it from unit-root tests and an information-criterion search, and keeps the model
only if it beats a naive forecast.

### `ts.arima` - forecast from past values

> Forecasts a series from its own recent past and trend, without outside drivers.

**Use it when** you have only the series itself and want a solid, well-known method. **Pick
something else when** outside drivers explain most of the movement (use `ts.sarimax`).

```python
--8<-- "statsmodels_catalog.py:arima"
```

The same estimator as `ts.sarimax` without drivers; here one ARIMA(1,1,1) per geo of a panel.
`ts.arima` refuses a constant together with differencing (use `trend: t` for a drift).

### `ts.ets` - exponential smoothing

> Forecasts a series by giving recent values more weight than older ones, with optional trend and seasonality, and is quick and robust.

**Use it when** you need a fast, reliable baseline and the level, trend and season shift
slowly. **Pick something else when** outside drivers explain most of the movement.

```python
--8<-- "statsmodels_catalog.py:ets"
```

The error-trend-season model (`ETSModel`): `error`, `trend` (`add`/`mul`), `damped_trend`,
`seasonal` with `seasonal_periods`. Additive models have closed-form intervals;
multiplicative ones are simulated (`simulate_repetitions` paths, seeded by the request's
seed, so the same request gives the same quantiles).

### `ts.theta` - a simple, strong baseline

> Forecasts a series by combining a straight-line trend with smoothed recent values, a simple method that is hard to beat.

**Use it when** you want a strong, simple baseline to compare other models against. **Pick
something else when** outside drivers matter a lot or you need detailed explanations.

```python
--8<-- "statsmodels_catalog.py:theta"
```

The theta method with optional seasonal adjustment (`period`, `deseasonalize`, `method`,
`theta`). It is the benchmark to beat in a [forecast bake-off](../guides/workflow-cookbook.md#forecast-bake-off).

### `ts.unobserved_components` - trend, season and noise

> Splits a series into a slowly changing level and trend, a seasonal pattern and noise, and forecasts it from those parts.

**Use it when** you want to see the underlying trend without the noise, and the trend and
season change slowly. **Pick something else when** you only need a quick baseline.

```python
--8<-- "statsmodels_catalog.py:unobserved_components"
```

A structural model: `level` (`local level`, `local linear trend`, `smooth trend`, ...),
optional stochastic `seasonal`, `cycle`, `autoregressive` noise and drivers. The smoothed
level, trend and season are stored in the bundle's `components` group and shown in the Model
Explorer's decomposition; with drivers, forecasts need their planned values.

### `ts.ardl` - effects that build up over time

> Explains a series from its own past and the current and past values of drivers, so you can see how an effect builds up over several periods.

**Use it when** a driver's effect is spread over several periods. **Pick something else when**
effects are immediate only (a simple regression is enough).

```python
--8<-- "statsmodels_catalog.py:ardl"
```

Least squares on `lags` of the result and `order` lags of each driver; `auto: true` chooses
them per series by `ic` up to those maxima. The parameter table shows each lag's effect, so
you can read how a price change works through over the following weeks.

### `ts.ecm` - long-run and short-run effects

> Separates the short-term effect of a driver from its long-term effect on one series, and tests whether a long-run relationship exists at all.

**Use it when** one result and a few drivers trend together, and you want both the immediate
and the lasting effect of a driver. **Pick something else when** neither the result nor the
drivers trend.

```python
--8<-- "statsmodels_catalog.py:ecm"
```

The same model in error-correction form (statsmodels `UECM`): the coefficient on `y.L1` is
the **speed of adjustment** (its half-life is in the `ecm` group), the `long_run.*` rows of
the parameter table are the long-run effects with delta-method standard errors, and the
Pesaran-Shin-Smith **bounds test** (the case from the trend: `c` is case 3) says whether a
long-run relationship exists - `levels_relationship`, `none` or `inconclusive`, also raised as a
scorecard diagnostic (`ECM_BOUNDS_TEST`).

### `ts.var` - series that drive each other

> Models several series that influence each other over time, such as prices and sales of two brands, and forecasts them together.

**Use it when** several series move together and affect each other, and you want to see how a
shock to one spreads to the others. **Pick something else when** you have one series, or the
series trend together for good (use `ts.vecm`).

```python
--8<-- "statsmodels_catalog.py:var"
```

Least squares; leave `lags` empty to choose the order by `ic` up to `max_lags`. The data holds
the series along one dimension (`y(variable, t)`). The bundle carries orthogonalised impulse
responses with asymptotic 95% bands (`irf`), the forecast-error variance decomposition
(`fevd`) and pairwise Granger causality tests (`granger`).

### `ts.vecm` - series tied together for good

> Models series that wander but stay tied together in the long run, such as prices of close substitutes, and shows how fast they snap back after a gap opens.

**Use it when** series trend but keep a stable long-run relationship. **Pick something else
when** the series do not trend, or the long-run tie is not plausible.

```python
--8<-- "statsmodels_catalog.py:vecm"
```

A Johansen VECM; leave `coint_rank` empty and the Johansen `rank_test` (trace or
max-eigenvalue) at `rank_signif` chooses it. A rank of 0 (no long-run relationship: model the
changes with `ts.var`) or of the number of series (they are stationary: use `ts.var` on the
levels) is refused with a plain-language `SETTINGS_INVALID`. The `cointegration` group holds
the Johansen table, the rank, `alpha` (speed of adjustment) and `beta` (the long-run
relations, normalised on the first series). The
[VAR/VECM recipe](../guides/workflow-cookbook.md#varvecm-cointegration) chooses between the two
for you.

### `ts.varmax` - series that drive each other, with drivers

> Models several series that influence each other and also respond to outside drivers you know ahead, and forecasts them together.

**Use it when** several series affect each other and outside drivers matter too. **Pick
something else when** you have one series, or many series and little history.

```python
--8<-- "statsmodels_catalog.py:varmax"
```

VARMA(p, q) with drivers in state-space form (VARX when `q = 0`); `exog` drivers need their
planned values to forecast, as with `ts.sarimax`.

### `ts.dynamic_factor` - common movements across many series

> Finds a few hidden forces that move many series together, such as the overall economy behind sales in many categories.

**Use it when** you have many related series and want their shared drivers. **Pick something
else when** you have one or two series.

```python
--8<-- "statsmodels_catalog.py:dynamic_factor"
```

A few hidden factors following a VAR drive many series (state space). The smoothed factors and
loadings are in the `factors` group. Standardise the series first (`core.standardize`), as
factor models assume comparable scales.

### `ts.markov_switching` - switching between regimes

> Detects whether a series switches between regimes, such as high-growth and low-growth periods, and estimates the behaviour in each.

**Use it when** behaviour seems to jump between distinct modes and you want the probability of
being in each mode now. **Pick something else when** changes are gradual or you have little
history.

```python
--8<-- "statsmodels_catalog.py:markov_switching"
```

Hamilton's regime-switching regression (`order: 0`) or autoregression (`order > 0`): the level,
driver effects and noise can switch between `k_regimes` hidden regimes. The `regimes` group
holds the smoothed and filtered probability of each regime per period, the transition matrix,
expected durations and each regime's mean level. Forecasts are regime mixtures: analytic for
regressions, simulated (`simulations` seeded paths) for autoregressions. Start values come from
a fit on the standardised series, which avoids the failures of statsmodels' default start
values on series far from zero.

### Forecasting through an endpoint, the client and the CLI

Once a version is promoted, the same forecast comes from the endpoint, with the drivers' plan
sent inline (an `xr.Dataset` with the future rows):

```python
from mapi_client import Client

api = Client()
fan = api.endpoint("weekly-sales").score(plan_ds, mode="forecast", horizon={"dim": "t", "periods": 8})
print(fan["y_hat"].sel(stat="median").values)
```

```bash
mapi endpoints score weekly-sales --data plan.json --periods 8 -o forecast.zarr.zip
mapi explore model model://growth/weekly-sales@v3 -s forecast -s residuals --horizon 8
```

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
`fevd` and `decomposition`. Panels appear as facets. `views.forecast_view(predictions,
observed=...)` turns a forecast score run's quantiles into the `forecast` section: one fan
per series (with the recent history), because quantiles of different series cannot be
added into one fan.

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
