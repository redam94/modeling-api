# Built-in models

These model plugins ship with the platform (tier T0). They register through the
`mapi.models` entry point, so `default_registry()` and the control-plane catalog list them
without any configuration. Every one passes the contract kit (`mapi_sdk.testing.check_model`)
and all but `mmm.bayesinsight` have a conformance fixture (see
[Conformance corpus](../guides/conformance.md)); the BayesInsight model is exercised end to
end by the import test of the Guided data path instead.

**Calibration** (M3, the spec's `calibration` block, Guided task 3) is declared per plugin in
`Capabilities.calibration`; a spec with calibration data for a plugin that does not declare
it fails static validation with `CALIBRATION_UNSUPPORTED`:

| Plugin | Business priors | Lift tests |
|---|---|---|
| `mmm.hierarchical_geo` | yes - `beta_channel` and `intercept` priors | yes - with carry-over inside the test window |
| `mmm.pymc_marketing` | yes - `saturation_beta`, `intercept` (and narrowed `gamma_control`/`gamma_fourier`) priors | yes - pymc-marketing's `add_lift_test_measurements` |
| `mmm.bayesinsight` | no - its priors are already in KPI units: state them in `model_def.json` | yes - the incremental KPI under the model's own likelihood |

The math of the translation and of each lift likelihood is in
[Guided modelling](../guides/guided-modelling.md).

| Plugin | Package | Representation | Score modes | Corpus fixture |
|---|---|---|---|---|
| `mmm.hierarchical_geo@1` | `mapi-plugin-mmm` | draws | predict, forecast, counterfactual, simulate, evaluate | `e33` |
| `mmm.pymc_marketing@1` | `mapi-plugin-mmm` | draws | predict, forecast, counterfactual, evaluate | `pymc_marketing` |
| `mmm.bayesinsight@1` | `mapi-plugin-mmm` | draws | predict, forecast, counterfactual, simulate, evaluate | – (contract kit and the BayesInsight import test) |
| `ts.hsgp_trend@1` | `mapi-plugin-ts` | draws | predict, forecast, evaluate | `c08` |
| `ts.gaussian_hmm@1` | `mapi-plugin-ts` | draws | predict, forecast, filter, evaluate | `e24` |
| `tabular.hierarchical_glm@1` | `mapi-plugin-tabular` | draws | predict, counterfactual, evaluate | `e02` |
| `tabular.regressor@1` | `mapi-plugin-tabular` | quantiles | predict, forecast, counterfactual, evaluate | `xgb_store_demand` |
| `choice.multinomial_logit@1` | `mapi-plugin-tabular` | draws (Bayesian bootstrap) | predict, forecast, counterfactual, evaluate | `e84_lite` |

Plugins use **canonical names** for their inputs; bind your dataset's names onto them in the
spec's `data.bind` block. Outputs come back under your names (reverse binding) unless they
are new variables such as `y_hat`.

The `mapi-sklearn` package also registers five general-purpose tabular plugins,
`sklearn.tabular_regressor`, `sklearn.tabular_quantiles`, `sklearn.tabular_bootstrap`,
`sklearn.tabular_conformal` and `sklearn.tabular_classifier`; see
[Tabular models](../guides/sklearn.md).

!!! tip "Every setting, generated"
    The [models catalog](generated/models-catalog.md) is generated from the installed
    manifests: capabilities, outputs, derived quantities, every setting with its default and
    Guided-mode level, and the input data contract.

## `mmm.hierarchical_geo`: geo marketing mix

A hierarchical marketing-mix model: adstock over a `core.lag_tensor` spend tensor, logistic or
Hill saturation, channel effects partially pooled across geos, a hierarchical intercept and
controls. New geos are drawn from the fitted hyperprior. Contributions are additive and, with
`outputs.derived`, the fit caches contributions, per-draw response curves and marginal costs
**in original units**. It declares a decision objective for budget optimisation.

**Inputs.** `y(geo, time)`, `spend(geo, channel, time, lag)` from `core.lag_tensor` (the
`adstock.l_max` setting must equal the step's `l_max`), and any other numeric variable along
time as a control. `geo` is `new_levels` (hyperprior), `channel` is `fixed`, `time` extends.

**Outputs.** `y_hat(geo, time)` and `contributions(component, geo, time)` (one component per
channel, `baseline` and one per control; they sum to `y_hat`).

Read the [marketing-mix guide](../guides/mmm.md) for the model, its priors and a complete
spec.

## `mmm.pymc_marketing`: the pymc-marketing adapter

Runs `pymc_marketing.mmm.MMM` behind the platform. The model scales data, carries spend over
and reports original units itself, so the plugin declares `owns: [scaling, carryover,
inverse_units]` and the platform skips its own versions of those. Bundles store the posterior,
pymc-marketing's `fit_data` and its JSON model description, never a pickle. Markets are a
`fixed` dim: pymc-marketing cannot score a market it has not seen. See
[the adapter section](../guides/mmm.md#the-pymc-marketing-adapter).

**Priors and calibration.** `settings.priors` hands prior overrides to pymc-marketing's
`model_config`, in its scaled units (the KPI divided by its maximum): `saturation_beta` (the
channel effect, one parameter per channel; `saturation_b` for `saturation: none`),
`intercept`, `gamma_control` and `gamma_fourier`. Business priors are translated into them
on prior draws of pymc-marketing's own model, so its scaling, adstock and curves are exactly
the fit's: a log-normal `saturation_beta` per channel matches each ROI or contribution-share
range at its credibility, a Normal `intercept` the baseline share, and the control and
seasonality priors are narrowed when they alone spread the baseline share wider than the
range (an `info` issue says by how much). A `translate_priors` run previews them; a fit
without one translates them itself (`learned.calibration.translated_priors`). Lift tests go
through pymc-marketing's `add_lift_test_measurements` (the saturation curve, no carry-over).

## `mmm.bayesinsight`: a BayesInsight `model_def.json`

Fits a BayesInsight model exactly as its
`model_def.json` defines it - the dedicated target of the BayesInsight import
(`POST /v1/guided/import/bayesinsight` with `"target": "mmm.bayesinsight"`; see
[Guided mode](../concepts/guided-mode.md#bayesinsight-models)). The settings carry
`model_def` (typed by `mapi_spec.bayesinsight_model`), map every analytic variable name to its
model-stage variable and list the panel dims (the model's row ids besides `Period`).

**Inputs.** The KPI `y(*panel, time)` as stored (BayesInsight does not rescale it; priors are
in its units), every media variable as a `core.lag_tensor` with `pad: nan` (the history
before a series starts carries no media, BayesInsight's zero-padded convolution) and the
controls along `(*panel, time)`. Panel dims are `new_levels` (pooled); `time` extends.

**Model.** `eta = intercept + Σ media + Σ controls + Σ seasons + Σ trends`:

| Feature | `model_def.json` | Semantics |
|---|---|---|
| Likelihood | `exog.likelihood.type` | `Normal`, `LogNormal` (on `eta`), `Poisson`, `NegativeBinomial`, `ZeroInflatedPoisson`, `ZeroInflatedNegativeBinomial` (log link, `exp(clip(eta, -20, 20))`); none given means **Poisson**, as in BayesInsight. `likelihood_params` parametrise the dispersion prior, `dispersion_dims` vary it by row id |
| Coefficients | `coeff_prior`, `intercept_prior` | Any of `Normal`, `Laplace`, `StudentT`, `Uniform`, `HalfNormal`, `HalfCauchy`, `Exponential`, `LogNormal`, `Gamma`, `Beta` with its parameters, in KPI units; defaults are BayesInsight's (`LogNormal(log 0.05, log 1.3)` for media, `Normal(0, 3)` otherwise) |
| Effects per row id | `fixed_ind_coeff_dims`, `random_coeff_dims` | One coefficient per level combination of the fixed row ids, plus `sigma * z` per level combination of the random ones with `sigma ~ HalfNormal(partial_pooling_sigma)` (0, BayesInsight's default, pools nothing). New levels at score time get the pooled value; a coefficient estimated per level of a dim has none for a new level (`UNSEEN_LEVELS`) |
| Signs | `sign` | Enforced (BayesInsight declares but does not enforce them): real priors are truncated at 0, positive priors negated for `negative`, random effects multiplicative so no level crosses zero. Media are always positive |
| Media curve | `media_transform`, `media_transform_prior` | First the curve on `transform(x) / index` (`index`: each series' non-zero median or mean, `index_to`): `hill` (`K ~ LogNormal`, `n = 1 + Exponential`), `linear`, or the S-shaped curve of an `SShaped` prior, anchored so no spend has no effect |
| Carry-over | `adstock`, `adstock_prior` | Then `delayed` (`alpha^((l - theta)^2)`), `geometric` (`alpha^l`), `weibull` (pymc-marketing's PDF or CDF weights, a `Weibull` prior) or `none`, over `l_max` lags, normalised to sum to one |
| Controls | `deterministic_transform`, `normalization` | `linear`, `log`, `exp`, `indexp`, `power`; `Global Standardize` over the fit window (or the stored `mean`/`std`) |
| Seasons | `season` | `n_fourier` cos/sin pairs of `period`, one coefficient each, on BayesInsight's time grid |
| Local trends | `localtrend` | A B-spline basis (`num_knots`, `order`) with Gaussian-random-walk coefficients, per level of `random_coeff_dims` or grouped by `grouping_map`; forecasts hold the last fitted level |

Where BayesInsight's own code cannot run a feature it defines (geometric and Weibull
carry-over, the S-shaped curve, linear media, the zero-inflated negative binomial's
`psi`), the plugin's choice is documented on `mapi_spec.bayesinsight_model` and the import
reports it as an `info` issue. `MvNormal` likelihoods, time transforms (moving averages) and
event priors are not supported: the import falls back to BayesInsight's defaults with an
`IMPORT_UNSUPPORTED` warning.

**Outputs.** `y_hat(*panel, time)` - the expected KPI (`eta` for a Normal likelihood,
`exp(eta + sigma^2 / 2)` for a log-normal, the count mean times the zero-inflation share) or,
`predictive`, a draw of the likelihood with common random numbers where the family allows -
and `contributions(component, *panel, time)`: each media variable, `baseline` (the
intercepts), each control, season and trend. They sum to `y_hat`: additive terms for a Normal
likelihood, a log-mean Divisia split of `E[y] - baseline` for the others, exact for terms of
either sign.

**Calibration.** Lift tests (`channel`: a media variable's analytic or model-stage name,
`geo`: a label of the first panel dim) observe the incremental KPI `Σ E[y | x + dx] -
E[y | x]` over the test window under the model's own likelihood, with the extra spend
spread in proportion to the actual spend and carried over. Business priors are not
supported: BayesInsight's coefficient priors are already statements in KPI units.

Response curves, marginal costs and the decision objective are not offered yet, so the
Guided what-if sliders and the scorecard's ROI checks skip this model (they say so).

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

## `choice.multinomial_logit` — which product customers pick

The built-in model of the Guided **customer choice** template (E84-style household panels): on
every shopping trip a household picks one product, and price and promotions shift the choice.

**Inputs.** A dense `hh × trip` panel - `hh` (categorical, `new_levels` with strategy
`hyperprior`), `trip` (`index`, `extend`) - with the alternatives along `product`
(categorical, `fixed`). The target `y(hh, trip)` is text: the product chosen, empty where a
household made no trip (ragged panels are fine). Covariates are alternative-specific
variables along `product`, such as `price(hh, trip, product)` and `promo(hh, trip, product)`;
by default every numeric variable with the `product` dim is one. A product whose covariates
are missing on a trip was not available on that trip (off the shelf).

**Model.** A conditional logit with household-specific product intercepts:
`U[h, t, j] = asc[j] + Σ_k beta[k]·x_k[h, t, j] + u[h, j]`, the choice probabilities a softmax
over the available products, `u[h, j] ~ Normal(0, effect_sd)` (a random-intercept logit). It
is fitted by penalised maximum likelihood, the posterior mode of that model. **Uncertainty**
comes from the Bayesian bootstrap: each of `fit.draws` draws refits with Dirichlet weights on
households, so every household keeps its own effect in every draw and the draws are joint
(`draws` representation: shares, counterfactual deltas and sums over trips are valid).
There is no pointwise log-likelihood, so LOO is not offered.

**Scoring.** `y_hat(hh, trip, product)` is the choice probability (`expected`) or one sampled
choice per draw (`predictive`, one-hot). A household never seen at fit draws its preferences
from `Normal(0, sd)` per draw, `sd` being the spread of the fitted effects in that draw. Trips
never made have probability 0 for every product, so sums over trips count expected
purchases. Price scenarios are interventions, e.g.
`{var: price, sel: {product: cola}, multiply: 1.1}` in a `counterfactual` request. Choice
values match product labels after lower-casing and replacing punctuation with `_`.

| Setting | Default | Meaning |
|---|---|---|
| `covariates` | every numeric variable along `product` | Alternative-specific covariates |
| `random_effects` | `[hh]` | Household product intercepts (`[]`: none) |
| `effect_sd` | 1.0 | Prior sd of the household effects (smaller pools more) |
| `coef_sd` | 10.0 | Prior sd of the product intercepts and coefficients |
| `reference` | the first product | The product whose intercept is fixed at 0 |
| `max_iter` | 500 | Iterations of each L-BFGS fit |

`fit.draws` (default 100, at most 2000) sets the bootstrap draws and `fit.seed` makes them
reproducible. The in-sample hit rate is reported as the `CHOICE_HIT_RATE` diagnostic.

**Holdout metrics.** A text target scored as one probability per product gets the
categorical metrics of `evaluate.metrics`: `accuracy` (the hit rate of the most likely
product), `accuracy_naive` (always guessing the holdout's most common product),
`accuracy_lift` (their difference), `log_score` (mean log probability of the product
bought) and `brier` (multi-class Brier score). The Guided `customer_choice` template
evaluates all five on each household's last trip and its scorecard runs
[`holdout.hit_rate`](../concepts/guided-mode.md#check-holdout-hit-rate); numeric metrics
such as `mape` are skipped for a text target.
