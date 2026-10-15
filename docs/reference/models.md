# Built-in models

These model plugins ship with the platform (tier T0). They register through the
`mapi.models` entry point, so `default_registry()` and the control-plane catalog list them
without any configuration. Every one passes the contract kit (`mapi_sdk.testing.check_model`)
and has a conformance fixture (see [Conformance corpus](../guides/conformance.md)).

| Plugin | Package | Representation | Score modes | Corpus fixture |
|---|---|---|---|---|
| `ts.hsgp_trend@1` | `mapi-plugin-ts` | draws | predict, forecast, evaluate | `c08` |
| `ts.gaussian_hmm@1` | `mapi-plugin-ts` | draws | predict, forecast, filter, evaluate | `e24` |
| `tabular.hierarchical_glm@1` | `mapi-plugin-tabular` | draws | predict, counterfactual, evaluate | `e02` |
| `tabular.regressor@1` | `mapi-plugin-tabular` | quantiles | predict, forecast, counterfactual, evaluate | `xgb_store_demand` |

Plugins use **canonical names** for their inputs; bind your dataset's names onto them in the
spec's `data.bind` block. Outputs come back under your names (reverse binding) unless they
are new variables such as `y_hat`.

## `ts.hsgp_trend` — smooth trend with seasonality

A Hilbert-space Gaussian process (HSGP) trend plus an optional linear drift and Fourier
seasonality, for one regularly spaced series (Mauna-Loa style CO2, sales, traffic).

**Inputs.** `t` (time, policy `extend`) and the target `y(t)`. Missing months are allowed:
replace sentinel codes with `core.replace_sentinel` and they are left out of the likelihood
while the model still interpolates them. Put `core.standardize` on `y`; the priors are on the
standardised scale and outputs are mapped back to your units.

**Forecast horizon.** `max_extension` (default 24 periods) is part of the model's input
schema. The HSGP domain is sized at fit time from the *anticipated* coordinates (the fitted
range plus `max_extension` periods), so every forecast up to that horizon evaluates exactly
the fitted functions. A longer horizon is rejected before any compute with
`HORIZON_EXCEEDS_FIT` (HTTP 409): refit with a larger `max_extension`.

| Setting | Default | Meaning |
|---|---|---|
| `max_extension` | 24 | Periods a forecast may reach past the fit without a refit |
| `kernel` | `expquad` | `expquad`, `matern52` or `matern32` |
| `m`, `c` | 30, 1.5 | Basis functions and boundary factor of the approximation |
| `lengthscale_mu`, `lengthscale_sigma` | 0.4, 0.5 | Log-normal lengthscale prior on the scaled axis `[-1, 1]` |
| `eta_sigma` | 1.0 | Half-normal scale of the GP amplitude |
| `linear_trend` | true | Add `intercept + slope * t` |
| `season_period`, `season_order` | 12, 3 | Fourier seasonality (periods of `t`, order) |
| `noise_sigma` | 0.5 | Half-normal scale of the observation noise |

**Outputs.** `y_hat(t)` (expected value or a predictive draw) and `trend(t)`, the long-term
component without seasonality, both in the target's units.

```yaml
data:
  bind: {dims: {t: date}, vars: {y: co2}}
  split: {kind: time_holdout, dim: t, last: 12}
pipeline:
  - {id: clean, use: core.replace_sentinel@^1, with: {vars: [y], below: 0}}
  - {id: scale, use: core.standardize@^1, with: {vars: [y], over: [t]}}
model:
  use: ts.hsgp_trend@^1
  settings: {max_extension: 24, season_order: 2}
  fit: {mode: evaluate_then_refit, engine: pymc, seed: 11}
```

## `ts.gaussian_hmm` — hidden states with a terminal state

A K-state Gaussian hidden Markov model over many sequences at once (animal tracks, machine
regimes, customer phases). Hidden states are summed out with the forward algorithm, so
sampling is fast and exact.

**Inputs.** Padded layout: `animal` (the sequence dim, `new_levels` with strategy `pooled`),
`step` (`extend`), `y(animal, step)` with NaN padding and a boolean `mask(animal, step)`.

**Terminal state.** After the fit the filtered state distribution at the last fitted step
is stored as the bundle's `terminal_state` group, `alpha_T(chain, draw, animal, state)`.

* `predict` / `evaluate` use the one-step-ahead state distribution of the fit inside the
  fitted range and `alpha_T` propagated by the transition matrix after it; they never look at
  targets in the payload.
* `forecast` propagates `alpha_T` over the horizon.
* `filter` runs the forward filter over new observations that continue the fitted steps and
  returns an **updated** `terminal_state` group with the filtered `state_prob`.
* New animals start from the initial distribution `pi` at the first fitted step.

Draws are relabelled after sampling so that the state means increase (label switching);
`identification: ordered` uses an ordered prior instead. Pointwise LOO is declared invalid
(`elpd_loo` is rejected at validation): steps within a track are dependent.

| Setting | Default | Meaning |
|---|---|---|
| `n_states` | 2 | Number of hidden states |
| `mu_center`, `mu_sigma` | 0.0, 2.0 | Normal prior of the state means |
| `sigma_scale` | 1.0 | Half-normal scale of the per-state sds |
| `stickiness` | 2.0 | Extra Dirichlet weight on staying in a state |
| `pi_concentration` | 1.0 | Dirichlet weight of the initial distribution |
| `identification` | `relabel` | `relabel` (sort draws by mean) or `ordered` |

**Outputs.** `y_hat(animal, step)` in your units and `state_prob(animal, step, state)`
(units `probability`, never inverse-transformed).

## `tabular.hierarchical_glm` — partial pooling in long layout

Varying intercepts (and optionally slopes) by group with group-level predictors: the radon
model and its many relatives (schools, stores, clinics).

**Inputs.** Long layout: rows along `obs` (`extend`), a group dim (`group_dim`, default
`county`, `new_levels` with strategy `hyperprior`), the index `<group_dim>_idx(obs)`, the
target `y(obs)`, observation-level `covariates` and group-level `group_covariates`. Covariates
are part of the input schema, so a spec naming a missing one fails validation.

**New groups.** A county the model never saw is drawn from the fitted hyperprior for every
posterior draw; its group-level covariates shift its prior mean exactly as for fitted
counties. Score payloads may list counties in any order: the platform remaps `county_idx`.

| Setting | Default | Meaning |
|---|---|---|
| `group_dim` | `county` | The grouping dim |
| `family` | `normal` | `normal`, `student_t`, `poisson` (log link) or `bernoulli` (logit) |
| `covariates` | `[]` | Predictors over `obs` |
| `varying_slopes` | `[]` | Covariates whose slope varies by group |
| `group_covariates` | `[]` | Predictors over the group dim |
| `centered` | false | Centred group effects |

## `tabular.regressor` — quantile regression for panels

The plan's store-demand model: rows are stacked from labelled dims (store × sku × week), the
coordinates become categorical features, and an allow-listed estimator predicts conditional
quantiles. Its `estimator` setting accepts exactly the quantile-capable estimators of the
registry (xgboost, scikit-learn's gradient boosting and histogram boosting, and quantile
regression), so other estimators are rejected at validation.

* Defaults: xgboost (300 trees, depth 5, learning rate 0.05) and quantiles `[0.1, 0.5, 0.9]`.
* New stores are pooled (`new_levels: pooled`); forecasting is direct, so future covariates
  marked `future_required` must be supplied (`MISSING_FUTURE_COVARIATE` otherwise).
* Quantiles are per cell: aggregating them across stores is refused
  (`CAPABILITY_UNSUPPORTED`).
* The manifest lists `core.fit_vs_actual`, `core.residuals_over_time` and
  `ml.feature_importance`.

All other settings are those of `mapi_sklearn` (see
[Tabular models](../guides/sklearn.md)).
