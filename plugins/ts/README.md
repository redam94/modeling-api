# mapi-plugin-ts

Time-series and state-space model plugins for modeling-api (`ts.*`, entry point
`mapi.models: ts`).

| Plugin | Model | Representation | Score modes |
|---|---|---|---|
| `ts.hsgp_trend@1` | HSGP trend + linear drift + Fourier seasonality (Mauna-Loa style, C08) | draws | predict, forecast, evaluate |
| `ts.gaussian_hmm@1` | K-state Gaussian HMM over padded sequences (animal tracks, E24) | draws | predict, forecast, filter, evaluate |
| `ts.bsts@1` | Bayesian structural time series: trend, seasonality and drivers on `pymc_extras.statespace` (E13) | draws | predict, forecast, filter, evaluate |

* `ts.hsgp_trend` fixes its HSGP domain from the *anticipated* coordinates (fit range plus
  `max_extension` periods), so forecasts up to `max_extension` evaluate exactly the fitted
  functions; longer horizons fail with `HORIZON_EXCEEDS_FIT` (409, refit required).
  Sentinel-coded months (via `core.replace_sentinel`) are left out of the likelihood.
* `ts.gaussian_hmm` marginalises the hidden states with the forward algorithm, stores the
  filtered state at the last fitted step as the `terminal_state` group
  `alpha_T(chain, draw, animal, state)`, forecasts by propagating it, and in `filter` mode
  returns an updated terminal state. Draws are relabelled so that state means increase.
* `ts.bsts` samples the Kalman likelihood of pymc-extras (default engine nutpie), then
  thins to at most 250 draws and runs a numpy Kalman filter and smoother
  (`mapi_plugin_ts.ss_math`) on the system matrices of each draw. It stores the
  one-step-ahead predictions (`one_step`), the smoothed components (`components`: `level`,
  `season_<i>`, `regression`) and the filtered state at the last period (`terminal_state`).
  Driver coefficients are static (their block of the initial covariance is zero). Forecasts
  need future driver values; `filter` mode returns an updated terminal state. The
  `causal.impact` model reuses this core.

* Standard plots (v0.10): `ts.gaussian_hmm` declares `core.state_sequence` (it reads the
  `hidden_states` group) and `ts.hsgp_trend` keeps a `components` group for
  `core.decomposition`; `ts.bsts` draws its `components` with `core.decomposition` too. An
  ADVI fit of `ts.gaussian_hmm` keeps its loss for `core.loss_curve`.
* Run tracking (v0.13): `ts.bsts` logs its structure as `bsts.*` params, the noise and the
  error of the one-step-ahead predictions.
* `ts.bsts` needs pymc-extras (a dependency of this package) and runs in the
  `mapi/base-forecast` image.

`mapi_plugin_ts.datasets` holds synthetic data, raw schemas and example specs for the first
two models; `mapi_plugin_ts.bsts_data` holds the ones for `ts.bsts`.

License: Apache License 2.0.
