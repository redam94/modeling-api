# mapi-plugin-statsmodels

Classical time-series models from [statsmodels](https://www.statsmodels.org/) (BSD-3-Clause)
as modeling-api plugins (`ts.*`, entry point `mapi.models: statsmodels`).

| Plugin | Model | Inference |
|---|---|---|
| `ts.sarimax@1` | seasonal ARIMA with drivers (ARIMAX/SARIMAX) | Kalman filter MLE |
| `ts.arima@1` | ARIMA without drivers | Kalman filter MLE |
| `ts.unobserved_components@1` | level/trend + season + cycle + noise | Kalman filter MLE |
| `ts.ets@1` | exponential smoothing (ETSModel) | MLE |
| `ts.theta@1` | theta method | OLS + SES |
| `ts.ardl@1` | autoregressive distributed lags | OLS |
| `ts.ecm@1` | single-equation error correction (UECM + bounds test) | OLS |
| `ts.var@1` | vector autoregression (IRF, FEVD, Granger) | OLS |
| `ts.vecm@1` | vector error correction (Johansen rank) | Johansen ML |
| `ts.varmax@1` | VARMA / VARX with drivers | Kalman filter MLE |
| `ts.dynamic_factor@1` | dynamic factor model | Kalman filter MLE |
| `ts.markov_switching@1` | Markov regime-switching regression / autoregression | Hamilton filter MLE |

* **Canonical names**: time `t`, target `y`, `variable` (multivariate models), panel dims
  from `settings.series_dims`, drivers from `settings.exog` (future values required).
* **Panels** loop over the series cells: one model per geo/product.
* **Outputs** are prediction-interval quantiles `y_hat(quantile, ..., t)`: one-step-ahead
  inside the fitted range, a multi-step forecast after it.
* **No pickle**: bundles hold the training data and parameters; scoring rebuilds the
  results with the model's filter (state-space, ETS, Markov) or by deterministic
  re-estimation checked against the stored parameters (VAR, VECM, ARDL/ECM, Theta).
* Fits store parameter tables (SE, p-values, 95% CI), fit statistics, residual tests, and
  per model impulse responses, FEVD, Granger tests, the Johansen table, ECM bounds tests,
  smoothed components, factors or regime probabilities; `explorer_views()` returns Model
  Explorer sections.

* Standard plots (v0.10): `ts.unobserved_components` and `ts.ets` keep a `components` group
  for `core.decomposition` (the trend slope has the attr `decomposition: exclude`), and
  `ts.markov_switching` declares `core.state_sequence` (it reads the `regimes` group).

`mapi_plugin_statsmodels.datasets` holds example data (synthetic sales with drivers,
statsmodels' public-domain US `macrodata`, regimes, an error-correction series) and
`mapi_plugin_statsmodels.examples` a small worked spec per plugin.

Documentation: `docs/models/statsmodels.md`.

License: Apache License 2.0.
