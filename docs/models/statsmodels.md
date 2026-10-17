# statsmodels time-series models

The `mapi-plugin-statsmodels` package adds twelve classical time-series models from
[statsmodels](https://www.statsmodels.org/) (BSD-3-Clause) to the platform. These are the
model groups:

- The usual forecast models (ARIMA, exponential smoothing, theta).
- Models with drivers (ARIMAX, ARDL, error correction).
- Systems of series that have an effect on each other (VAR, VECM, VARMAX, dynamic factors).
- Regime switching.

All of these models fit in seconds. They run in the time-series runner image
(`mapi/base-forecast`). They pass the same contract kit as all other plugins.

## Which model should I use?

Each model has an About card in the catalog. *Help me choose* in the web app, `mapi choose`
and `api.catalog.recommend()` rank the models for your goal and data (see
[Choosing a model](../guides/choosing-a-model.md)). This table gives a short summary:

| Plugin | In plain words | Choose it when |
|---|---|---|
| `ts.sarimax` | Forecast with past values and drivers | You forecast one series a few periods ahead and know the future prices, promotions or holidays |
| `ts.arima` | Forecast from past values | You have only the series itself |
| `ts.ets` | Exponential smoothing forecast | You need a fast, robust baseline with trend and season |
| `ts.theta` | Simple strong baseline | You want a simple model that is difficult to beat, to compare other models with |
| `ts.unobserved_components` | Trend, season and noise | You want to see the underlying trend without the noise |
| `ts.ardl` | Effects that increase over time | The effect of a driver spreads over many periods |
| `ts.ecm` | Long-run and short-run effects | The result and its drivers have the same trend. You want the immediate effect and the lasting effect. |
| `ts.var` | Series that have an effect on each other | Many series have an effect on each other. You want shocks and Granger tests. |
| `ts.vecm` | Series that stay tied together | Series with a trend keep a stable long-run relation |
| `ts.varmax` | Series that have an effect on each other, with drivers | Like VAR, with outside drivers that you know ahead |
| `ts.dynamic_factor` | Common movements across many series | Many series follow a few hidden common forces |
| `ts.markov_switching` | Changes between regimes | The behaviour jumps between different modes (normal / with promotions) |

Is the series ready? If you are not sure, use the **time-series readiness** workflow
template. It does these steps:

1. It runs the unit-root tests.
2. It selects the differencing and the AR/MA orders.
3. It does a backtest of `ts.sarimax` against a naive forecast.
4. It keeps `ts.sarimax` only if it wins.

See [Workflows](../concepts/workflows.md).

## Your data, in the model's names

The models use canonical names. Bind the names of your dataset to them in `data.bind`:

| Canonical name | What it is |
|---|---|
| `t` | The time index: dates (default) or period numbers (`settings.time_kind: integer`). Policy `extend`: forecasts continue it. |
| `y` | The target. Univariate models: `y(t)`, or `y(<series...>, t)` for panels. Multivariate models: `y(variable, t)`. |
| `variable` | Multivariate models only: the series that the model uses together (realgdp, realcons, ...). |
| `<series...>` | Panel dims that you name in `settings.series_dims` (`geo`, `product`). The plugin fits one model for each series. |
| `<exog...>` | Drivers that you name in `settings.exog`, as `(<series...>, t)`. Forecasts need their future values. |

A table with one column for each series (realgdp, realcons, realinv) becomes
`y(variable, t)` with the `wide` variables of the ingest mapping. See [From a spreadsheet to
a Guided draft](../guides/guided-data.md).

!!! note "Panels are a per-series loop"
    With `series_dims: [geo]`, each geo gets its own model, with its own orders, parameters
    and residual checks. Geos are a `fixed` dim. The platform refuses a forecast for a geo
    that the model did not see, with `UNSEEN_LEVELS`. Multivariate models do the same loop.
    For example, there is one joint VAR for each geo.

## An ARIMAX per geo

```python
--8<-- "statsmodels_models.py:arimax"
```

The `params` group of the bundle is the parameter table. For each parameter of each series,
it gives the estimate, standard error, z/t statistic, p-value and 95% interval.

## Forecasts and the drivers' future

```python
--8<-- "statsmodels_models.py:forecast"
```

For all models, a score gives these results:

* **In the fitted range**: the one-step-ahead predictions of the model from the fitted
  history. The model does not use the targets and drivers of the payload there. The only
  exception is a forecast with actuals (see below).
* **After the fitted range**: a multi-step forecast from the end of the fit, with the drivers
  in the payload. If a driver or a value of a driver is missing, the score fails with
  `MISSING_FUTURE_COVARIATE` (422). With `future_covariates: carry_last`, `seasonal_naive`
  or `zero`, the platform extends the drivers that you do not give (see
  [Forecasting](../guides/time-series.md)).

A what-if on the future is another payload. Change the planned drivers, for example a
promotion each week or a 5% price increase. Then forecast again.

The outputs are **quantiles** (`y_hat(quantile, ..., t)`, with the representation
`quantiles`). The default quantiles are 2.5%, 5%, 10%, 25%, 50%, 75%, 90%, 95% and 97.5%.
Thus, you get the median and the 50/80/90/95% bands. `settings.quantiles` changes them, but
the outputs always include 5%, 50% and 95%.

The quantiles are **prediction intervals** for the two output kinds. They include the noise
of the next value, because statsmodels has no separate interval for the expected value. Each
quantile is for one cell. Thus, the platform refuses totals across geos or series
(`CAPABILITY_UNSUPPORTED`). To get a total, add the series in the pipeline and model the
total.

The plugin does not give the score modes `counterfactual`, `simulate` and `filter`
(`MODE_NOT_SUPPORTED`). To update a forecast with new data, use a `forecast` with actuals.

## Forecasting from new actuals

All statsmodels models can move their forecast origin forward without a new fit
(`capabilities.conditions_on_actuals`). The rules are on
[Forecasts with actuals](../concepts/forecasting-with-actuals.md). Send the periods that
came after the fit, with their target values and drivers. Then send the periods to forecast,
with an empty target:

```python
--8<-- "statsmodels_models.py:actuals"
```

A univariate model updates each series with its own actuals. Thus, the series of a panel can
end at different dates. The answer includes all the periods that you sent. It gives
one-step-ahead predictions on the actuals, and the forecast after the last actual of each
series. Actuals for periods in the fitted range replace the fitted values (revised data).

The fitted parameters never change. The fitted noise variance, and thus the width of the
bands, also never changes. This table shows how each model uses the new data:

| Model | Updated with the actuals by |
|---|---|
| `ts.sarimax`, `ts.arima`, `ts.unobserved_components`, `ts.varmax`, `ts.dynamic_factor` | the Kalman filter with the fitted parameters over the history and the actuals. This is statsmodels' `results.append(new, refit=False)`. Differencing and seasonal differencing are part of the model. Thus, actuals and forecasts are in the units of the data. The filter uses empty periods in the actuals as missing values (the univariate state-space models accept missing targets). |
| `ts.ets` | the exponential-smoothing recursions with the fitted smoothing weights and initial states |
| `ts.theta` | the fitted SES weight (or IMA(1,1) filter with `use_mle`), drift and seasonal indices |
| `ts.ardl`, `ts.ecm` | the fitted equation on the lags of the result and the drivers that you supply (`apply(refit=False)`; the ECM uses its levels ARDL) |
| `ts.var`, `ts.vecm` | the fitted equations on the lags that you supply (levels for the VECM). The deterministic trend continues. |
| `ts.markov_switching` | the Hamilton filter with the fitted parameters. The regime probabilities after the last actual start the forecast. |

Joint models of series (`ts.var`, `ts.vecm`, `ts.varmax`, `ts.dynamic_factor`) need a value
of each series for each period with actuals after the fit. If not, the score fails with
`ACTUALS_INVALID`. The platform applies the preprocessing steps of the spec (for example a
`core.log1p` or a standardisation) to the actuals before the update. It reverses them on the
forecast, as for all payloads.

A `transform.log`, `transform.box_cox` or `transform.difference` of a workflow is not such a
step. A model that you fit on its output refuses actuals (see
[Forecasts with actuals](../concepts/forecasting-with-actuals.md#which-models-can)). Use the
differencing of the model (`order` `d`, seasonal `D`) instead. Fit the model again from time
to time, so that the parameters learn from the new data.

## The models

Each section starts with the About card of the model. The web app, `mapi about` and
`api.catalog.about()` show this card. A docs test makes sure that the quotes are the same as
the catalog. Then each section gives a minimal spec. For models with drivers, it also gives a
forecast from planned drivers.

All the specs use this small wrapper. The wrapper has the data binding and a holdout of the
last 8 periods. The plugin scores the holdout before it fits the model again on all the data.
The specs use synthetic or public data from `mapi_plugin_statsmodels.datasets`. The
documentation tests run each spec (`docs/snippets/statsmodels_catalog.py`):

```python
--8<-- "statsmodels_catalog.py:common"
```

### `ts.sarimax` - forecast with past values and drivers

> Forecasts a series from its recent past, its seasonal pattern and outside drivers that you know in advance, such as planned prices or holidays.

**Use it when** you forecast one regular series a few periods ahead, and known future drivers
(prices, promotions, holidays) are important. **Use a different model when** you have less
than about 50 periods, or when many series have an effect on each other (use `ts.var`).

```python
--8<-- "statsmodels_catalog.py:sarimax"
```

This is seasonal ARIMA with drivers, that is, a regression with ARIMA errors. The Kalman
filter estimates it by exact maximum likelihood. Missing target values are permitted (the
filter skips them). The forecast needs the planned future values of the drivers (here `plan`
without `sales`). If one is missing, the forecast fails with `MISSING_FUTURE_COVARIATE`. The
exception is when the request tells how to extend the driver.

| Setting | Default | Meaning |
|---|---|---|
| `order` | `[1, 0, 0]` | `(p, d, q)`: past values, times differenced, past errors |
| `seasonal_order` | `[0, 0, 0, 0]` | `(P, D, Q, s)` for a pattern that repeats each `s` periods |
| `trend` | `auto` | `n`, `c`, `t`, `ct`. `auto` is a constant, unless the series is differenced. |
| `exog` | `[]` | drivers |
| `series_dims` | `[]` | panel dims: one model for each series |

Are you not sure about `order`? The [readiness workflow](../guides/workflow-cookbook.md#readiness-to-arimax)
selects it from unit-root tests and a search on an information criterion. It keeps the model
only if it is better than a naive forecast.

### `ts.arima` - forecast from past values

> Forecasts a series from its recent past and trend, without outside drivers.

**Use it when** you have only the series itself and want a solid, well-known method. **Use a
different model when** outside drivers explain most of the movement (use `ts.sarimax`).

```python
--8<-- "statsmodels_catalog.py:arima"
```

This is the same estimator as `ts.sarimax`, without drivers. Here, there is one
ARIMA(1,1,1) for each geo of a panel. `ts.arima` refuses a constant together with
differencing (use `trend: t` for a drift).

### `ts.ets` - exponential smoothing

> Forecasts a series and gives recent values more weight than older values. It has an optional trend and seasonality, and it is quick and robust.

**Use it when** you need a fast, reliable baseline, and the level, trend and season change
slowly. **Use a different model when** outside drivers explain most of the movement.

```python
--8<-- "statsmodels_catalog.py:ets"
```

This is the error-trend-season model (`ETSModel`): `error`, `trend` (`add`/`mul`),
`damped_trend`, and `seasonal` with `seasonal_periods`. Additive models have closed-form
intervals. The plugin simulates the intervals of multiplicative models
(`simulate_repetitions` paths). The seed of the request sets the paths. Thus, the same
request gives the same quantiles.

### `ts.theta` - a simple, strong baseline

> Forecasts a series with a combination of a straight-line trend and smoothed recent values. This simple method is hard to beat.

**Use it when** you want a strong, simple baseline to compare other models against. **Use a
different model when** outside drivers are very important, or when you need detailed
explanations.

```python
--8<-- "statsmodels_catalog.py:theta"
```

This is the theta method with optional seasonal adjustment (`period`, `deseasonalize`,
`method`, `theta`). It is the benchmark to beat in a [forecast bake-off](../guides/workflow-cookbook.md#forecast-bake-off).

### `ts.unobserved_components` - trend, season and noise

> Divides a series into a level and a trend that change slowly, a seasonal pattern and noise. It forecasts the series from these parts.

**Use it when** you want to see the underlying trend without the noise, and the trend and
season change slowly. **Use a different model when** you only need a quick baseline.

```python
--8<-- "statsmodels_catalog.py:unobserved_components"
```

This is a structural model. It has a `level` (`local level`, `local linear trend`,
`smooth trend`, ...), and optional stochastic `seasonal`, `cycle`, `autoregressive` noise
and drivers. The `components` group of the bundle keeps the smoothed level, trend and season.
The decomposition of the Model Explorer shows them. With drivers, forecasts need the planned
values of the drivers.

### `ts.ardl` - effects that build up over time

> Explains a series from its past and from the current and past values of drivers. You can see how an effect increases over several periods.

**Use it when** the effect of a driver spreads over many periods. **Use a different model
when** the effects are only immediate (a simple regression is sufficient).

```python
--8<-- "statsmodels_catalog.py:ardl"
```

This model uses least squares on `lags` of the result and on `order` lags of each driver.
With `auto: true`, the plugin selects them for each series by `ic`, up to those maximum
values. The parameter table shows the effect of each lag. Thus, you can see how a price
change has an effect over the subsequent weeks.

### `ts.ecm` - long-run and short-run effects

> Separates the short-term effect of a driver from its long-term effect on one series. It also tests if a long-run relationship exists.

**Use it when** one result and some drivers have the same trend, and you want the immediate
effect and the lasting effect of a driver. **Use a different model when** the result and the
drivers have no trend.

```python
--8<-- "statsmodels_catalog.py:ecm"
```

This is the same model in error-correction form (statsmodels `UECM`):

- The coefficient on `y.L1` is the **speed of adjustment**. Its half-life is in the `ecm`
  group.
- The `long_run.*` rows of the parameter table are the long-run effects, with delta-method
  standard errors.
- The Pesaran-Shin-Smith **bounds test** tells if a long-run relation exists. The trend sets
  the case (`c` is case 3). The result is `levels_relationship`, `none` or `inconclusive`.
  The scorecard also shows it as a diagnostic (`ECM_BOUNDS_TEST`).

### `ts.var` - series that drive each other

> Models two or more series that change each other over time, such as the prices and sales of two brands. It forecasts them together.

**Use it when** many series move together and have an effect on each other, and you want to
see how a shock to one series spreads to the others. **Use a different model when** you have
one series, or when the series stay tied together in the long run (use `ts.vecm`).

```python
--8<-- "statsmodels_catalog.py:var"
```

This model uses least squares. To select the order by `ic` up to `max_lags`, do not give a
value for `lags`. The data has the series along one dimension (`y(variable, t)`). The bundle
contains these items:

- Orthogonalised impulse responses with asymptotic 95% bands (`irf`).
- The forecast-error variance decomposition (`fevd`).
- Granger causality tests for each pair of series (`granger`).

### `ts.vecm` - series tied together for good

> Models series that wander but stay tied together in the long run, such as the prices of close substitutes. It shows how fast they go back after a gap opens.

**Use it when** the series have a trend but keep a stable long-run relation. **Use a
different model when** the series have no trend, or when a long-run relation is not
plausible.

```python
--8<-- "statsmodels_catalog.py:vecm"
```

This is a Johansen VECM. If you do not give a value for `coint_rank`, the Johansen
`rank_test` (trace or max-eigenvalue) at `rank_signif` selects it. The plugin refuses some
ranks with a `SETTINGS_INVALID` in plain language:

- A rank of 0. There is no long-run relation. Model the changes with `ts.var`.
- A rank that is equal to the number of series. The series are stationary. Use `ts.var` on
  the levels.

The `cointegration` group has the Johansen table, the rank, `alpha` (speed of adjustment) and
`beta` (the long-run relations, normalised on the first series). The
[VAR/VECM recipe](../guides/workflow-cookbook.md#varvecm-cointegration) selects one of the two
models for you.

### `ts.varmax` - series that drive each other, with drivers

> Models two or more series that change each other and also respond to outside drivers that you know in advance. It forecasts them together.

**Use it when** many series have an effect on each other, and outside drivers are also
important. **Use a different model when** you have one series, or many series and a short
history.

```python
--8<-- "statsmodels_catalog.py:varmax"
```

This is VARMA(p, q) with drivers in state-space form (VARX when `q = 0`). As with
`ts.sarimax`, the `exog` drivers need their planned values for a forecast.

### `ts.dynamic_factor` - common movements across many series

> Finds a few hidden forces that move many series together, such as the general economy behind the sales in many categories.

**Use it when** you have many related series and want their shared drivers. **Use a
different model when** you have one or two series.

```python
--8<-- "statsmodels_catalog.py:dynamic_factor"
```

In this state-space model, some hidden factors follow a VAR and drive many series. The
`factors` group has the smoothed factors and loadings. Standardise the series first
(`core.standardize`), because factor models assume comparable scales.

### `ts.markov_switching` - switching between regimes

> Finds if a series changes between regimes, such as periods of high growth and low growth. It estimates the behaviour in each regime.

**Use it when** the behaviour jumps between different modes, and you want the probability of
each mode now. **Use a different model when** the changes are gradual, or when you have a
short history.

```python
--8<-- "statsmodels_catalog.py:markov_switching"
```

This is the regime-switching regression of Hamilton (`order: 0`) or autoregression
(`order > 0`). The level, the driver effects and the noise can change between `k_regimes`
hidden regimes. The `regimes` group has these items:

- The smoothed and filtered probability of each regime for each period.
- The transition matrix.
- The expected durations.
- The mean level of each regime.

Forecasts are mixtures of regimes. They are analytic for regressions. For autoregressions,
they are simulated (`simulations` seeded paths). The start values come from a fit on the
standardised series. This prevents the failures of the default start values of statsmodels
on series that are far from zero.

### Forecasting through an endpoint, the client and the CLI

After you promote a version, you can get the same forecast from the endpoint. Send the plan
of the drivers inline (an `xr.Dataset` with the future rows):

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

The plugin never keeps a statsmodels results object, because pickles cannot cross the
sandbox boundary. A bundle keeps these items:

* The training data at the model stage (`observed_data`, and the drivers in
  `constant_data`).
* All estimated parameters (`params`), with the order that the model needs in `learned`.
* Fit statistics (`fit_stats`: AIC, BIC, HQIC, log-likelihood, observations, noise
  variance, and if the optimiser converged), residuals and residual tests (Ljung-Box,
  Jarque-Bera, ARCH-LM).
* The groups for each model that the sections above give.

When you score, the plugin makes the results again:

* State-space models (SARIMAX, ARIMA, UnobservedComponents, VARMAX, DynamicFactor) and the
  ETS and Markov-switching models apply the stored parameters to the stored data. They use
  their own filter (`model.filter(params)`, `smooth(params)`).
* Least-squares and closed-form models (VAR, VECM, ARDL/ECM, Theta) estimate again on the
  stored data with the stored orders. The plugin compares the result with the stored
  parameters. If they are different, the score fails with `INTEGRITY_FAILURE`.

## The Model Explorer

`StatsmodelsModel.explorer_views(fitted)` changes a bundle into sections of the Model
Explorer (`mapi_spec.views` DTOs):

- `params`: the table with 95% intervals and p-values.
- `diagnostics`: fit statistics, and Granger, Johansen and bounds tests.
- `residuals`: the residuals over time, their autocorrelation, Q-Q points and the residual
  tests for each series.
- `irf`, `fevd` and `decomposition`.

Panels show as facets. `views.forecast_view(predictions, observed=...)` changes the
quantiles of a forecast score run into the `forecast` section. There is one fan for each
series (with the recent history), because you cannot add quantiles of different series into
one fan.

## How it is tested

* The contract kit (`check_model`) passes for each plugin, with About cards necessary.
* Parameters, in-sample predictions and forecast intervals are equal to direct statsmodels
  calls on the same data. This is true for SARIMAX, ARIMA, UnobservedComponents, ETS, Theta,
  ARDL, UECM, VAR, VECM, VARMAX, DynamicFactor and Markov switching. A test also does a check
  of the UECM on the Danish money-demand data of Johansen and Juselius. The VAR(2) on US
  macro growth gives the same coefficient table as the statsmodels documentation.
* Forecasts with actuals are equal to statsmodels with the fitted parameters on the extended
  data. The tests use `results.append(new, refit=False)` for the state-space models and ARDL.
  They also use the VAR/VECM forecast on the supplied lags, the fixed-parameter ETS smoother,
  theta and the Hamilton filter. For each model, the last values of the fit as actuals give
  the plain forecast again. A jump in these values changes the forecast. A model from the
  `ts_readiness_arimax` workflow forecasts from new monthly actuals in the original units.
* The conformance fixtures `sm_arimax` (an ARIMAX panel with drivers) and `sm_vecm_macro`
  (a VECM on the public-domain `macrodata` of statsmodels) run the full path: fit, score and
  evaluate. See [Conformance corpus](../guides/conformance.md).
