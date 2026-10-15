# mapi-plugin-ts

Time-series and state-space model plugins for modeling-api (`ts.*`, entry point
`mapi.models: ts`).

| Plugin | Model | Representation | Score modes |
|---|---|---|---|
| `ts.hsgp_trend@1` | HSGP trend + linear drift + Fourier seasonality (Mauna-Loa style, C08) | draws | predict, forecast, evaluate |
| `ts.gaussian_hmm@1` | K-state Gaussian HMM over padded sequences (animal tracks, E24) | draws | predict, forecast, filter, evaluate |

* `ts.hsgp_trend` fixes its HSGP domain from the *anticipated* coordinates (fit range plus
  `max_extension` periods), so forecasts up to `max_extension` evaluate exactly the fitted
  functions; longer horizons fail with `HORIZON_EXCEEDS_FIT` (409, refit required).
  Sentinel-coded months (via `core.replace_sentinel`) are left out of the likelihood.
* `ts.gaussian_hmm` marginalises the hidden states with the forward algorithm, stores the
  filtered state at the last fitted step as the `terminal_state` group
  `alpha_T(chain, draw, animal, state)`, forecasts by propagating it, and in `filter` mode
  returns an updated terminal state. Draws are relabelled so that state means increase.

`mapi_plugin_ts.datasets` holds synthetic data, raw schemas and example specs for both.

License: Apache License 2.0.
