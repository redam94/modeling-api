# Built-in models

These model plugins come with the platform (tier T0). They register through the
`mapi.models` entry point. Thus `default_registry()` and the control-plane catalog show them
without configuration. Each plugin passes the contract kit (`mapi_sdk.testing.check_model`).
Each plugin also has a conformance fixture (see [Conformance corpus](../guides/conformance.md)).
From v0.5, the marketing-mix models run on [mmm-framework](../models/mmm-framework.md). This is
the only marketing-mix engine of the platform.

**Calibration** (M3, the `calibration` block of the spec, Guided task 3): each plugin declares
it in `Capabilities.calibration`. A spec can have calibration data for a plugin that does not
declare it. Such a spec fails static validation with `CALIBRATION_UNSUPPORTED`:

| Plugin | Business priors | Lift tests |
|---|---|---|
| `mmm.bayesian` | yes - log-normal ROI priors for each channel (`priors.roi`) and the `intercept` | yes - mmm-framework experiment likelihoods (marginal ROAS or contribution) |
| `mmm.frequentist` | no | no |

[Guided modelling](../guides/guided-modelling.md) gives the math of the translation and of
each lift likelihood.

| Plugin | Package | Representation | Score modes | Corpus fixture |
|---|---|---|---|---|
| `mmm.bayesian@1` | `mapi-plugin-mmm` | draws | predict, forecast, counterfactual, evaluate | `e33`, `guided_mmm` |
| `mmm.frequentist@1` | `mapi-plugin-mmm` | draws (bootstrap) | predict, forecast, counterfactual, evaluate | `mmm_frequentist` |
| `ts.hsgp_trend@1` | `mapi-plugin-ts` | draws | predict, forecast, evaluate | `c08` |
| `ts.gaussian_hmm@1` | `mapi-plugin-ts` | draws | predict, forecast, filter, evaluate | `e24` |
| `tabular.hierarchical_glm@1` | `mapi-plugin-tabular` | draws | predict, counterfactual, evaluate | `e02` |
| `tabular.regressor@1` | `mapi-plugin-tabular` | quantiles | predict, forecast, counterfactual, evaluate | `xgb_store_demand` |
| `choice.multinomial_logit@1` | `mapi-plugin-tabular` | draws (Bayesian bootstrap) | predict, forecast, counterfactual, evaluate | `e84_lite` |
| `ts.sarimax@1` | `mapi-plugin-statsmodels` | quantiles | predict, forecast, evaluate | `sm_arimax` |
| `ts.vecm@1` | `mapi-plugin-statsmodels` | quantiles | predict, forecast, evaluate | `sm_vecm_macro` |
| `sci.lgcp_grid@1` | `mapi-plugin-science` | draws | predict, forecast, evaluate | `e07_lgcp` |
| `sci.fisher_kpp@1` | `mapi-plugin-science` | draws | predict, forecast, evaluate | `e50_kpp` |
| `sci.traction_force@1` | `mapi-plugin-science` | draws | predict, evaluate | `e60_traction` |
| `sci.strong_lens@1` | `mapi-plugin-science` | draws | predict, simulate, evaluate | `e80_lens` |
| `sci.heat_plate@1` | `mapi-plugin-science` | draws | predict, simulate, evaluate | `e51_heat_plate` |
| `sci.turing_pattern@1` | `mapi-plugin-science` | draws | predict, simulate, evaluate | `e61_turing` |
| `sci.storm_localisation@1` | `mapi-plugin-science` | draws | predict, evaluate | `e58_storm` |
| `sci.ibp_features@1` | `mapi-plugin-science` | draws | predict, evaluate | `e31_ibp` |
| `sci.colour_naming@1` | `mapi-plugin-science` | draws | predict, simulate, evaluate | `e77_colour_naming` |
| `sci.cmb_field@1` | `mapi-plugin-science` | draws | predict, simulate, evaluate | `e79_cmb` |
| `sci.areal_bym2@1` | `mapi-plugin-science` | draws | predict, simulate, evaluate | `e18_areal` |
| `sci.network_latent_space@1` | `mapi-plugin-science` | draws | predict, simulate, evaluate | `e21_network` |
| `sci.graphical_model@1` | `mapi-plugin-science` | draws | predict, simulate, evaluate | `e19_ggm` |
| `sci.phylo_regression@1` | `mapi-plugin-science` | draws | predict, simulate, evaluate | `e23_phylo` |
| `sci.circular_regression@1` | `mapi-plugin-science` | draws | predict, simulate, evaluate | `e81_circular` |
| `ts.arima@1`, `ts.ets@1`, `ts.theta@1`, `ts.unobserved_components@1`, `ts.ardl@1`, `ts.ecm@1`, `ts.var@1`, `ts.varmax@1`, `ts.dynamic_factor@1`, `ts.markov_switching@1` | `mapi-plugin-statsmodels` | quantiles | predict, forecast, evaluate | contract kit and reference-value tests |

[statsmodels time-series models](../models/statsmodels.md) describes the statsmodels
plugins. These are the ARIMA family, exponential smoothing, theta, ARDL and error correction,
VAR, VECM, VARMAX, dynamic factors and Markov switching.

Plugins use **canonical names** for their inputs. Bind the names of your dataset to them in
the `data.bind` block of the spec. Outputs come back with your names (reverse binding). New
variables, for example `y_hat`, keep their names.

The `mapi-sklearn` package also registers five general tabular plugins (see
[Tabular models](../guides/sklearn.md)):

- `sklearn.tabular_regressor`
- `sklearn.tabular_quantiles`
- `sklearn.tabular_bootstrap`
- `sklearn.tabular_conformal`
- `sklearn.tabular_classifier`

!!! tip "Every setting, generated"
    The platform makes the [models catalog](generated/models-catalog.md) from the installed
    manifests. It shows the capabilities, outputs and derived quantities. It also shows each
    setting with its default and Guided-mode level, and the contract for the input data.

## `mmm.bayesian`: marketing mix on mmm-framework

This plugin uses the `BayesianMMM` of mmm-framework. It has these parts:

- Adstock (geometric, delayed, Weibull) and saturation (logistic, Hill, Michaelis-Menten,
  tanh, root) for each channel.
- A trend (none, linear, piecewise, spline, GP).
- Fourier seasonality.
- Baselines with partial pooling across geos and products. Channel effects for each geo are
  optional.
- Channel effects as returns on spend.

The model owns scaling, carry-over and units (`owns: [scaling, carryover,
inverse_units]`). Thus the pipeline only converts currencies and resamples. With
`outputs.derived`, the fit caches contributions, response curves for each draw and marginal
costs. The mmm-framework planning optimises budgets (`POST …:optimize`). The model declares a
decision objective.

**Inputs.** `y(*panel, time)`, `spend(*panel, channel, time)`, and other numeric variables
over time as controls. The panel dims are `hierarchy.pool_over` (`geo`, `product`). New panel
levels follow `new_levels` (`pooled` by default). `channel` is `fixed`. `time` extends.

**Outputs.** `y_hat(*panel, time)` and `contributions(component, *panel, time)`. The
components are one for each channel, `baseline`, `trend`, `seasonality` and one for each
control. Their sum is `y_hat`.

[Marketing-mix models on mmm-framework](../models/mmm-framework.md) tells how the settings
map to mmm-framework. It also tells about priors, lift tests and optimisation. For a complete
analysis, read the [marketing-mix guide](../guides/mmm.md).

## `mmm.frequentist`: fast marketing mix

This plugin uses the ridge estimator of mmm-framework. An out-of-sample search selects the
adstock and saturation. The channel effects are not negative. The bootstrap gives confidence
intervals (the replicates are the draws). The data contract and outputs are the same as
`mmm.bayesian`. There are no priors, no lift tests and no convergence diagnostics. The same
seed gives the same result, and a fit takes seconds.

## `ts.hsgp_trend` — smooth trend with seasonality

This plugin has a Hilbert-space Gaussian process (HSGP) trend, an optional linear drift and
Fourier seasonality. It is for one series with regular spaces between periods (CO2 as at
Mauna Loa, sales, traffic).

**Inputs.** `t` (time, policy `extend`) and the target `y(t)`. Missing months are
permitted. Replace sentinel codes with `core.replace_sentinel`. The likelihood then does not
use these months, but the model interpolates them. Put `core.standardize` on `y`. The priors
are on the standardised scale, and the model changes the outputs back to your units.

**Forecast horizon.** `max_extension` (default 24 periods) is part of the input schema of the
model. At fit time, the model sets the size of the HSGP domain from the *anticipated*
coordinates. These are the fitted range plus `max_extension` periods. Thus each forecast up to
that horizon evaluates exactly the fitted functions. The server rejects a longer horizon with
`HORIZON_EXCEEDS_FIT` (HTTP 409) before it starts to compute. Fit again with a larger
`max_extension`.

| Setting | Default | Meaning |
|---|---|---|
| `max_extension` | 24 | The number of periods after the fit that a forecast can reach without a new fit |
| `kernel` | `expquad` | `expquad`, `matern52` or `matern32` |
| `m`, `c` | 30, 1.5 | Basis functions and boundary factor of the approximation |
| `lengthscale_mu`, `lengthscale_sigma` | 0.4, 0.5 | Log-normal lengthscale prior on the scaled axis `[-1, 1]` |
| `eta_sigma` | 1.0 | Half-normal scale of the GP amplitude |
| `linear_trend` | true | Adds `intercept + slope * t` |
| `season_period`, `season_order` | 12, 3 | Fourier seasonality (periods of `t`, order) |
| `noise_sigma` | 0.5 | Half-normal scale of the observation noise |

**Outputs.** `y_hat(t)` (the expected value or a predictive draw) and `trend(t)`. `trend(t)`
is the long-term component without seasonality. The two outputs are in the units of the
target.

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

This plugin is a Gaussian hidden Markov model with K states. It uses many sequences at the
same time (animal tracks, machine regimes, customer phases). The forward algorithm sums out the
hidden states. Thus the sampler is fast and exact.

**Inputs.** A padded layout with these items: `animal` (the sequence dim, `new_levels` with
strategy `pooled`), `step` (`extend`), `y(animal, step)` with NaN padding, and a boolean
`mask(animal, step)`.

**Terminal state.** After the fit, the bundle keeps the filtered state distribution at the
last fitted step. It is the `terminal_state` group of the bundle,
`alpha_T(chain, draw, animal, state)`.

* `predict` / `evaluate` use the one-step-ahead state distribution of the fit in the fitted
  range. After the fitted range, they use `alpha_T`, propagated by the transition matrix. They
  do not use targets in the payload.
* `forecast` propagates `alpha_T` over the horizon.
* `filter` runs the forward filter over new observations that continue the fitted steps. It
  returns an **updated** `terminal_state` group with the filtered `state_prob`.
* New animals start from the initial distribution `pi` at the first fitted step.

After the sampler stops, the model gives new labels to the draws so that the state means
increase (label switching). `identification: ordered` uses an ordered prior instead. The
manifest declares pointwise LOO as not valid, because the steps in a track are dependent.
Validation rejects `elpd_loo`.

| Setting | Default | Meaning |
|---|---|---|
| `n_states` | 2 | Number of hidden states |
| `mu_center`, `mu_sigma` | 0.0, 2.0 | Normal prior of the state means |
| `sigma_scale` | 1.0 | Half-normal scale of the sds for each state |
| `stickiness` | 2.0 | Additional Dirichlet weight to stay in the same state |
| `pi_concentration` | 1.0 | Dirichlet weight of the initial distribution |
| `identification` | `relabel` | `relabel` (sort the draws by mean) or `ordered` |

**Outputs.** `y_hat(animal, step)` in your units, and `state_prob(animal, step, state)`. The
units of `state_prob` are `probability`. The model does not inverse-transform it.

## `tabular.hierarchical_glm` — partial pooling in long layout

This plugin has intercepts that change by group, and optionally slopes that change by group.
It has group-level predictors. Examples are the radon model and many similar models (schools,
stores, clinics).

**Inputs.** A long layout with these items:

- Rows along `obs` (`extend`).
- A group dim (`group_dim`, default `county`, `new_levels` with strategy `hyperprior`).
- The index `<group_dim>_idx(obs)`.
- The target `y(obs)`.
- Observation-level `covariates` and group-level `group_covariates`.

Covariates are part of the input schema. Thus, if a spec names a covariate that is missing,
the spec fails validation.

**New groups.** For a county that the model did not see, the model draws its effect from the
fitted hyperprior for each posterior draw. Its group-level covariates move its prior mean in
the same way as for fitted counties. Score payloads can list counties in any sequence. The
platform maps `county_idx` again.

| Setting | Default | Meaning |
|---|---|---|
| `group_dim` | `county` | The grouping dim |
| `family` | `normal` | `normal`, `student_t`, `poisson` (log link) or `bernoulli` (logit) |
| `covariates` | `[]` | Predictors over `obs` |
| `varying_slopes` | `[]` | Covariates whose slope varies by group |
| `group_covariates` | `[]` | Predictors over the group dim |
| `centered` | false | Use centred group effects |

## `tabular.regressor` — quantile regression for panels

This plugin is the store-demand model of the plan. The model stacks rows from labelled dims
(store × sku × week). The coordinates become categorical features. An estimator from the allow
list predicts conditional quantiles. The `estimator` setting accepts only the estimators of the
registry that can predict quantiles. These are xgboost, the gradient boosting and histogram
boosting of scikit-learn, and quantile regression. Validation rejects other estimators.

* Defaults: xgboost (300 trees, depth 5, learning rate 0.05) and quantiles `[0.1, 0.5, 0.9]`.
* The model pools new stores (`new_levels: pooled`).
* The forecast is direct. Thus you must supply the future covariates that have the
  `future_required` mark. If you do not, the error is `MISSING_FUTURE_COVARIATE`.
* Quantiles are for each cell. The server refuses to aggregate them across stores
  (`CAPABILITY_UNSUPPORTED`).
* The manifest lists `core.fit_vs_actual`, `core.residuals_over_time` and
  `ml.feature_importance`.

All other settings are the settings of `mapi_sklearn` (see
[Tabular models](../guides/sklearn.md)).

## `choice.multinomial_logit` — which product customers pick

This plugin is the built-in model of the Guided **customer choice** template (household panels
as in E84). On each shopping trip, a household selects one product. Price and promotions change
the choice.

**Inputs.** A dense `hh × trip` panel, with the alternatives along `product` (categorical,
`fixed`). `hh` is categorical, with `new_levels` with strategy `hyperprior`. `trip` is `index`
and `extend`. The target `y(hh, trip)` is text: the product that the household selected. It
is empty when a household made no trip (ragged panels are permitted). Covariates are variables
for each alternative along `product`, for example `price(hh, trip, product)` and
`promo(hh, trip, product)`. By default, each numeric variable with the `product` dim is a
covariate. If the covariates of a product are missing on a trip, that product was not
available on that trip (not on the shelf).

**Model.** A conditional logit with product intercepts for each household:
`U[h, t, j] = asc[j] + Σ_k beta[k]·x_k[h, t, j] + u[h, j]`. The choice probabilities are a
softmax over the available products. `u[h, j] ~ Normal(0, effect_sd)` (a random-intercept
logit). The fit uses penalised maximum likelihood, that is, the posterior mode of that model.

**Uncertainty** comes from the Bayesian bootstrap. Each of the `fit.draws` draws is a new fit
with Dirichlet weights on households. Thus each household keeps its own effect in each draw,
and the draws are joint. In the `draws` representation, shares, counterfactual deltas and sums
over trips are valid. There is no pointwise log-likelihood, so LOO is not available.

**Scoring.** `y_hat(hh, trip, product)` is the choice probability (`expected`), or one sampled
choice for each draw (`predictive`, one-hot). The fit did not see some households. For each
draw, the model draws their preferences from `Normal(0, sd)`. `sd` is the spread of the fitted
effects in that draw. A trip that the household did not make has probability 0 for all
products. Thus sums over trips count the expected purchases.

Price scenarios are interventions, for example
`{var: price, sel: {product: cola}, multiply: 1.1}` in a `counterfactual` request. To match
choice values to product labels, the model changes them to lower case and replaces
punctuation with `_`.

| Setting | Default | Meaning |
|---|---|---|
| `covariates` | each numeric variable along `product` | Covariates for each alternative |
| `random_effects` | `[hh]` | Household product intercepts (`[]`: none) |
| `effect_sd` | 1.0 | Prior sd of the household effects (a smaller value pools more) |
| `coef_sd` | 10.0 | Prior sd of the product intercepts and coefficients |
| `reference` | the first product | The product whose intercept is fixed at 0 |
| `max_iter` | 500 | Iterations of each L-BFGS fit |

`fit.draws` (default 100, maximum 2000) sets the number of bootstrap draws. With `fit.seed`,
you get the same draws each time. The `CHOICE_HIT_RATE` diagnostic gives the in-sample hit
rate.

**Holdout metrics.** A text target with one score probability for each product gets the
categorical metrics of `evaluate.metrics`:

- `accuracy`: the hit rate of the most likely product.
- `accuracy_naive`: the hit rate if you always select the most common product of the holdout.
- `accuracy_lift`: the difference between the two.
- `log_score`: the mean log probability of the product that the household bought.
- `brier`: the multi-class Brier score.

The Guided `customer_choice` template evaluates all five on the last trip of each household.
Its scorecard runs [`holdout.hit_rate`](../concepts/guided-mode.md#check-holdout-hit-rate).
For a text target, the server skips numeric metrics, for example `mape`.

## Scientific models (`mapi-plugin-science`)

These plugins come from the case studies of
[pymc-challenges](https://github.com/redam94/pymc-challenges). Most of their data are maps,
images and fields on a regular grid. Some read other shapes: the areas of a map
(`sci.areal_bym2`), a graph of nodes (`sci.network_latent_space`), a network of variables
(`sci.graphical_model`), a phylogeny (`sci.phylo_regression`) or angles
(`sci.circular_regression`). Their outputs have a range for each cell, area or node. Each output
that is a field declares a `grid` (`OutputDecl.grid`: the row dim `y`, the column dim `x`, an
optional `frame` dim and an optional `component` dim for a vector field). The platform then
shows the output in two places:

- The **Fields** section of the Model Explorer. It shows the mean, the sd, mean ÷ sd, the 5% and
  95% maps and single draws, with a frame slider and a draw player.
- The `sci.*` plot recipes (see [Plot recipes](plots.md#scientific-model-pack-sci)).

An output that is an angle (a direction or a time of day) declares `circular`
(`OutputDecl.circular`). Its metrics and score summaries are then on the circle (see
[Circular outputs](sdk.md#circular-outputs)).

The runner images of `base-bayes` contain these plugins (namespace `sci`).

!!! tip "Tutorials"
    The [pymc-challenges tutorials](../tutorials/pymc-challenges/index.md) have one page for
    each case study. Each page exports the data, fits the model in the web app, reads the
    results, scores the model and runs a workflow recipe. The
    [complex workflows tutorial](../tutorials/complex-workflows.md) and the
    [workflow cookbook](../guides/workflow-cookbook.md#disease-map-which-spatial-model) show the
    six workflow recipes for these models.

!!! note "Real data"
    Each data module has a `from_challenges(path)` function. It reads the real data file of the
    case study from a local copy of pymc-challenges. To run the tests that use the real files,
    set `MAPI_PYMC_CHALLENGES_DATA` to the `data/` directory of that copy. Without it, these
    tests are skipped.

### Export the data of a case study

`mapi_plugin_science.export` writes the dataset of a case study to a file. You can then upload
the file in the web app (**Datasets → New dataset**), with the CLI or with the Python client.
The file contains the raw schema of the model (the `mapi_schema` attribute). Thus the ingest
gives the correct role to each variable, for example `target` and `mask`.

```bash
# the cases that you can export
python -m mapi_plugin_science.export --list
# the real data, from a local copy of pymc-challenges, and the suggested spec
python -m mapi_plugin_science.export e07 \
    --challenges ~/pymc-challenges/data --out quakes.nc --spec quakes.yaml
# the simulated data of the model (you do not need pymc-challenges)
python -m mapi_plugin_science.export e50 --synthetic --out assay.nc
```

`--challenges` is the `data/` directory of pymc-challenges. If you do not give it, the command
uses `MAPI_PYMC_CHALLENGES_DATA`. A case without a data file (`e51`, `e61`) needs neither.
The suffix of `--out` sets the format:

| Suffix | Format |
|---|---|
| `.nc` | NetCDF-4 (h5netcdf). A tree dataset has one HDF5 group for each node |
| `.zarr.zip` | A zarr zip of a plain dataset (the `data.zarr.zip` wire format) |
| `.tree.zip` | A tree dataset (the `data.tree.zip` wire format) |

If you do not give `--out`, the file is `<dataset>.nc`. With `--spec`, the command also writes
the suggested spec. That spec reads the suggested dataset name
(`dataset://<project>/<dataset>@latest`). Use that name when you upload the file.

| Case | Model | Data file in `data/` | Suggested dataset |
|---|---|---|---|
| `e07` | `sci.lgcp_grid` | `oklahoma_quakes.csv` | `geoscience/oklahoma_quakes_grid` |
| `e18` | `sci.areal_bym2` | `scotland_lip.geojson` | `epidemiology/scotland_lip_cancer` |
| `e50` | `sci.fisher_kpp` | `scratch_assay_pc3.csv` | `biology/scratch_assay_pc3` |
| `e60` | `sci.traction_force` | `pytfm_colony_ko04.npz` | `biophysics/pytfm_colony_ko04` |
| `e80` | `sci.strong_lens` | `slacs_j1627_f814w.npz` | `astronomy/slacs_j1627_f814w` |
| `e51` | `sci.heat_plate` | none (the notebook simulates the data) | `engineering/heat_plate_e51` |
| `e61` | `sci.turing_pattern` | none (the notebook simulates its images) | `biology/e61_turing_early_images` |
| `e58` | `sci.storm_localisation` | `dannhauser2022_unc13_dstorm_roi.npz` | `microscopy/dannhauser2022_unc13_dstorm` |
| `e31` | `sci.ibp_features` | `optdigits_train.csv`, `optdigits_test.csv` | `vision/optdigits_e31` |
| `e77` | `sci.colour_naming` | `wcs_naming.npz`, `wcs_terms.csv`, `wcs_chips.csv`, `wcs_languages.csv` | `linguistics/wcs_nafaanra` |
| `e79` | `sci.cmb_field` | `planck_smica_patches.npz`, `planck_theory_tt.txt` | `astronomy/planck_smica_lmc` |
| `e21` | `sci.network_latent_space` | `lazega_cowork.txt`, `lazega_attributes.txt` | `sociology/lazega_coworkers` |
| `e19` | `sci.graphical_model` | `sachs2005.txt`, `sachs2005_consensus.txt` | `biology/sachs_cd3cd28` |
| `e23` | `sci.phylo_regression` | `primates301.csv`, `primates301_tree.rda` | `biology/primates301` |
| `e81` | `sci.circular_regression` | `asos_ksba_2021_2023.csv.gz` | `meteorology/ksba_wind_2021_2023` |
| `e81b` | `sci.circular_regression` | `bci_camera_trap_times.csv` | `ecology/bci_camera_traps` |

In Python, `export_case("e07", "quakes.nc", data_dir=...)` does the same work, and
`write_export(data, path, schema=...)` writes any dataset or tree. To add a case, add one
`ExportCase` entry to `EXPORTS` (the docstring of the module shows an example).

### `sci.lgcp_grid` — event counts on a map over time (E07)

Tutorial: [E07 from start to end](../tutorials/pymc-challenges/e07-oklahoma-earthquakes.md).

A log-Gaussian Cox process on a space-time grid. The total for each period follows a random
walk. The share of each cell comes from a 2-D Hilbert-space GP (Matérn 5/2) whose map changes
from one era (a group of periods) to the next.

**Inputs.** `period` (time, policy `extend`), `row` and `col` (integer, fixed) and the target
`count(period, row, col)`. Row 0 is the south edge of the grid.

**Outputs.** `rate(period, row, col)` (a field with a frame for each period, log colour scale)
and `total(period)`. The sum of `rate` over the cells is equal to `total`.

| Setting | Default | Meaning |
|---|---|---|
| `max_extension` | 8 | The number of periods after the fit that a forecast can reach without a new fit |
| `era_periods` | 4 | The number of periods in one era. The map changes from era to era |
| `field` | `era_walk` | `era_walk` (a new map for each era) or `static` (one map) |
| `cell_km` | 25 | The width of one cell in km |
| `m_row`, `m_col`, `c` | 10, 14, 1.5 | Basis functions and boundary factor of the approximation |
| `ell_lower_km`, `ell_upper_km` | 15, 100 | The 5% and 95% quantiles of the lengthscale prior |
| `likelihood` | `negbin` | `negbin` or `poisson` |

`from_challenges` puts the Oklahoma earthquake catalogue into 12 × 9 cells of 25 km for each
quarter from 2009 to 2019. A fit on 2009–2017 takes approximately one minute with nutpie.

### `sci.areal_bym2` — disease risk on a map of areas (E18)

Tutorial: [E18 from start to end](../tutorials/pymc-challenges/e18-scottish-lip-cancer.md).

A disease map for counts in areas (districts, counties). The count of each area is Poisson
around its expected count times a relative risk `rr`. The log of `rr` is an intercept, plus a
covariate effect, plus an area effect `u`. The `kind` setting selects the prior of `u`:

| `kind` | Area effect `u` |
|---|---|
| `poisson` | No area effect |
| `iid` | Independent effects with sd `sigma` |
| `icar` | An intrinsic CAR (ICAR) on the graph of neighbours: each effect is close to the mean of its neighbours |
| `car` | A proper CAR with `alpha ~ Uniform(0, 1)`. `alpha` is often near 1, and then the intercept mixes slowly |
| `bym2` | A scaled ICAR plus independent effects. `sigma` is the total sd and `rho` is the share that follows the map |

For `bym2`, the model divides the ICAR by the square root of the scaling factor `s` of the graph
(the geometric mean of the diagonal of the generalised inverse of the graph Laplacian). Thus
`sigma` and `rho` have the same meaning on every map. The fit always keeps the log-likelihood of
each area, so `elpd_loo` compares the kinds. Many Pareto k values are high, because each area has
its own effect.

**Inputs.** `area` and `area_` (categorical, fixed: the same areas twice), `vertex` (integer,
fixed), the target `cases(area)` (missing values are permitted), `expected(area)` (the expected
count, for example age-standardised), one `covariate(area)`, `adjacency(area, area_)` (1 for
neighbours) and the polygon outlines `ring_x(area, vertex)` and `ring_y(area, vertex)` (NaN
padded; a NaN inside a row starts a new ring).

**Islands.** An area without neighbours makes the BYM2 scaling factor undefined, and under an
ICAR its effect gets no information from other areas. The `islands` setting selects what
happens:

- `link_nearest` (default): the model links each such area to the area with the closest polygon
  vertex. Then it links each remaining separate piece of the graph in the same way. The links
  are in `learned["links"]`.
- `isolate`: the graph stays as it is. Only `iid` and `icar` accept this.
- `error`: a graph with more than one piece stops the fit with `SCHEMA_INVALID`.

**Outputs.** `cases(area)` (the expected count `expected × rr`, or a Poisson draw), `rr(area)`
(the relative risk), `p_exceed(area)` (1 when `rr` is more than `exceed_threshold` in a draw,
so its mean is the probability) and `effect(area)` (`u`). The bundle keeps the map for the plots
in `constant_data`: `ring_x`, `ring_y`, `adjacency` (the graph that the model used, with the
links), `centroid(area, xy)` and `smr(area)` (observed ÷ expected). Thus `sci.area_map` draws
the maps (use `observed: smr` for the raw ratio) and `net.graph` draws the graph at the
centroids.

**Score modes.** A score needs only the area labels. `expected` and `covariate` come from the
fit when the payload does not give them. A payload can change them (for example, a different
covariate). `simulate` draws a new map of area effects for each draw from the fitted `sigma`,
`rho` and `alpha`, and then new counts. A hand-written sampler draws the ICAR, because
`pm.ICAR` cannot draw values. Areas that are not in the fit stop the score with
`UNSEEN_LEVELS`.

**Leave-areas-out.** A `mask_block` split over `area` removes the counts of the held-out areas
from the likelihood, but the areas stay in the graph. The model then predicts them from their
neighbours.

| Setting | Default | Meaning |
|---|---|---|
| `kind` | `bym2` | `poisson`, `iid`, `icar`, `car` or `bym2` |
| `islands` | `link_nearest` | `link_nearest`, `isolate` or `error` |
| `covariate_scale` | 10 | The model divides the centred covariate by this value. `b1` is the effect of one unit |
| `center_covariate` | true | Subtract the mean of the covariate |
| `intercept_sd`, `slope_sd` | 1, 1 | Prior sd of `b0` and `b1` |
| `sigma_scale` | 1 | Half-normal scale of `sigma` |
| `rho_alpha`, `rho_beta` | 1, 1 | Beta prior of `rho` (`bym2`) |
| `exceed_threshold` | 1 | The threshold of `p_exceed` |

`from_challenges` reads `scotland_lip.geojson`: the 56 districts of Scotland with the lip cancer
cases from 1975 to 1980, the expected cases and `aff` (the percentage of workers in agriculture,
fishing and forestry). The adjacency is queen contiguity from the polygons (117 edges), as in the
notebook. Orkney, Shetland and the Western Isles have no neighbours, and `link_nearest` links
them to Caithness, Orkney and Ross-Cromarty. Use `graph="rook"` for shared border segments, or
`graph="published"` for the hand-edited WinBUGS list of `scotland_lip_adjacency.csv`. With
`kind: bym2` the AFF effect `b1` is 0.41 (0.67 with `iid`), `rho` is about 0.8, and 19 districts
have `P(rr > 1) > 0.95`. The fit takes a few seconds.

Typed errors: `SETTINGS_INVALID` for `bym2` or `car` on a graph with more than one piece, and for
`simulate` of an ICAR with isolated areas. `SCHEMA_INVALID` for counts that are not whole
non-negative numbers, for an expected count that is not more than zero, and for a graph with
islands when `islands` is `error`.

### `sci.fisher_kpp` — a reaction-diffusion PDE (E50)

Tutorial: [E50 from start to end](../tutorials/pymc-challenges/e50-scratch-assay.md).

The Fisher-KPP equation `du/dt = D d²u/dx² + lam u (1 - u)` moves and grows cells into a
scratch wound. The model solves the equation in the model graph (explicit Euler in
`pytensor.scan`, zero flux at the edges). The counts are Poisson. With `delay: true`, the
movement and the growth start slowly after the scratch (`tanh(beta t)`).

**Inputs.** `t` (integer hours, policy `extend`), `x` (float positions in µm, equal spaces,
fixed) and the target `count(t, x)`. The first time is the initial condition. The likelihood
does not use it. Each time after the first must be on the Euler grid.

**Outputs.** `density(t, x)` (a field: time on the rows, position on the columns) and the
scalar `wave_speed` (`2 sqrt(D lam)`, µm/h). A forecast continues the PDE to later times.

| Setting | Default | Meaning |
|---|---|---|
| `steps_per_hour` | 10 | Euler steps for each hour |
| `delay` | true | Multiply the movement and the growth by `tanh(beta t)` |
| `max_extension` | 4 | The number of periods after the fit that a forecast can reach |
| `diffusivity_median`, `growth_median` | 1000, 0.05 | Prior medians of `D` (µm²/h) and `lam` (1/h) |
| `capacity_mu`, `capacity_sigma` | 122, 15 | Prior of the capacity `K` (cells for each column) |
| `cfl_quantile` | 0.99 | The prior quantile of `D` that must keep the Euler steps stable |

If the Euler steps are not stable at the `cfl_quantile` of the prior of `D`, the model stops
with `SETTINGS_INVALID`. The message gives the smallest `steps_per_hour` that is stable.

### `sci.traction_force` — cell forces from gel movement (E60)

Tutorial: [E60 from start to end](../tutorials/pymc-challenges/e60-traction-force.md).

Fourier-transform traction cytometry as a Gaussian inverse problem. The data are a displacement
field of a soft gel. The output is the traction field that the cells apply. NUTS samples only
the noise sd and the prior spectrum. The model integrates the field out, with one closed-form
likelihood for each Fourier mode. At score time, the model draws each traction field exactly
with two FFTs for each posterior draw.

**Inputs.** `component` (categorical `x`, `y`), `row` and `col` (integer, fixed) and the target
`displacement(component, row, col)` in µm. The raster must have no missing pixels. Thus the
model has no split: evaluate it in sample. With `force_mask: true`, the data also has a
`force_mask(row, col)`.

**Outputs.** `traction(component, row, col)` (a vector field, Pa), `displacement_hat`, and the
scalars `strain_energy` (fJ) and `contractility` (µN).

| Setting | Default | Meaning |
|---|---|---|
| `grid_um`, `young_pa`, `poisson` | 1, 10000, 0.49 | Grid spacing (µm), Young's modulus (Pa) and Poisson ratio of the gel |
| `young_cv` | 0 | Relative uncertainty of Young's modulus (0 = known) |
| `prior` | `matern` | `white` (Tikhonov), `smooth` or `matern` |
| `field_draws` | 100 | The number of traction fields to draw for each score |
| `force_mask` | false | Sum the strain energy and the contractility inside `force_mask` only |

The `NOISE_SPECTRUM_MISMATCH` diagnostic compares the power of the data in the finest modes with
the model. Correlated noise from overlapping PIV windows causes it, and then the ranges are too
narrow. `challenges_settings(ds)` gives the settings for the real pyTFM colony of
`from_challenges`.

### `sci.strong_lens` — a galaxy-scale strong lens (E80)

Tutorial: [E80 from start to end](../tutorials/pymc-challenges/e80-strong-lens.md).

A pixel forward model of a strong gravitational lens in a telescope image. The mass is a
singular isothermal ellipsoid with an optional external shear. Rays from each pixel (and from
sub-pixels) go back to the source plane. The source and the lens light are Sérsic profiles,
and the model applies the PSF of the image. The model integrates the linear light amplitudes
out in closed form. A staged search for the mode starts the sampler, and NUTS samples near
that mode with a Laplace preconditioner.

**Inputs.** `row` and `col` (integer, fixed, row 0 at the bottom), the target `image(row, col)`,
the inverse variance `weight(row, col)` (0 leaves a pixel out) and a constant
`psf(psf_row, psf_col)` with odd sides. With `use_mask: true`, the data also has a bool
`mask(row, col)`. The model also reads tree datasets (`capabilities.input` is `tree`): select
the image node in `data.nodes`. When the tree has a `/psf` node with `psf(psf_row, psf_col)`,
the fit uses that PSF. Otherwise it uses the flat `psf`. With `psf_source: tree`, the flat input
needs no `psf` and the `/psf` node is required.

**Outputs.** `model_image(row, col)` (a field, grey scale), `source(source_row, source_col)` (the
source on a source-plane grid), the scalar `einstein_radius` (arcsec) and, when `z_lens` and
`z_source` are set, `einstein_mass` (solar masses). The fit keeps the critical curve, the
caustic and the cut of each posterior draw as the derived group `critical_curves`. In
`simulate` mode, the model makes new noisy images from the posterior.

| Setting | Default | Meaning |
|---|---|---|
| `pixel_scale` | 0.05 | Pixel size (arcsec) |
| `subsample` | 2 | Rays for each pixel along each axis |
| `lens_light_components` | 2 | Sérsic profiles of the lens galaxy light (0 to 2) |
| `external_shear` | true | Add an external shear to the mass |
| `noise_correlation` | 0 | Correlation of the noise between neighbour pixels (drizzled images) |
| `theta_e_median`, `theta_e_log_sd` | 1.0, 0.5 | Log-normal prior of the Einstein radius |
| `n_starts` | 5 | Starts of the search for the mode |
| `source_grid`, `source_half_width` | 30, 0.45 | Size of the source-plane grid (pixels, arcsec) |
| `curve_draws` | 100 | Posterior draws with critical curves |
| `psf_source` | auto | `auto`: the `/psf` node of a tree dataset, else the flat `psf`; `tree`: only the `/psf` node |

The model has no LOO (`loo_valid` is false). `from_challenges` reads the HST F814W image of
SDSS J1627-0053 (an 81 × 81 cutout and a PSF from the star stamps). On that image the
posterior Einstein radius is 1.223 ± 0.001 arcsec. The fit takes approximately 5 to 7 minutes.

### `sci.heat_plate` — temperature of a heated plate (E51)

Tutorial: [E51 from start to end](../tutorials/pymc-challenges/e51-heat-plate-design.md).

The 2-D steady heat equation of a thin plate. Heat moves in the plane of the plate
(conductivity `k`) and goes out through the two faces (`h`) and the outer edges (`h_edge`). A
part `eta` of the heater power goes into the plate. The model divides the plate into square
cells (finite volumes). This gives a linear system `A(θ) u = eta q` for the rise `u` above the
room. The model solves the system for each run in the model graph
(`pytensor.tensor.linalg.solve`). The readings are Normal around `T_room + u` with the noise
`sigma`.

The field is linear in the heater powers. Thus one fit predicts the field of any new heater
layout. To do this, score a payload with new `run` labels and their `heater_power`.

**Inputs.** `run` (categorical, policy `new_levels`), `y` and `x` (float cell centres in cm,
equal steps, square cells, fixed), the input `heater_power(run, y, x)` in W and the target
`temperature(run, y, x)` in °C. Cells without a sensor have no value (NaN). The fit uses only
the cells with a value. The model declares these NaN in `structural_missing`, so the scorecard
check `data.missing` does not count them. With `use_insulation: true`, the data also has a bool
`insulation(y, x)`: `True` where the back face of the cell has insulation.

**Outputs.** `field(run, y, x)` (a field with a frame for each run, in °C) and the scalar
`spread_length` (`sqrt(k δ / 2h)`, cm). `predict` gives the steady field. `simulate` gives
simulated sensor readings in all cells (the field plus the noise). The pointwise
log-likelihood has one value for each reading. Thus `elpd_loo` works when the spec keeps it
(`keep: {log_likelihood: true}`).

| Setting | Default | Meaning |
|---|---|---|
| `thickness_mm` | 5 | Thickness of the plate (mm) |
| `room_temperature` | 20 | Temperature of the room (°C) |
| `use_insulation` | false | The data has `insulation(y, x)` |
| `insulated_back_loss` | 0.1 | Back-face loss under insulation, as a part of the loss without insulation |
| `k_median`, `k_log_sd` | 50, 0.5 | Log-normal prior of the conductivity `k` (W/m/K) |
| `h_median`, `h_log_sd` | 10, 0.5 | Log-normal prior of the face coefficient `h` (W/m²/K) |
| `h_edge_median`, `h_edge_log_sd` | 10, 0.7 | Log-normal prior of the edge coefficient `h_edge` |
| `eta_alpha`, `eta_beta` | 40, 4 | Beta prior of the heater efficiency `eta` |
| `sigma_scale` | 0.5 | Half-normal scale of the sensor noise sd (°C) |

The notebook simulates its data, so E51 has no data file. `from_challenges()` makes the
calibration experiment of the notebook again with the same generator and seed: a 40 × 20 cm
plate on 24 × 12 cells, three runs with one 10 W heater each and twelve sensors. The readings
are the same as in the notebook. On these data, `E51_SPEC_YAML` (4 × 1000 draws, nutpie)
gives `k` = 59.1 ± 2.7, `h` = 9.05 ± 0.42, `eta` = 0.910 ± 0.040 and a spread length of
12.8 cm in approximately 45 s. These values are the same as in the notebook. `h_edge` stays
near its prior (6.9 ± 3.7; the true value is 15): few sensors are near an edge.

The model stops with `SCHEMA_INVALID` when the cells are not square, when the positions do not
have equal steps, when the fit data has no reading or when `use_insulation` is set and the
data has no `insulation`. A new layout without `heater_power` gives
`MISSING_FUTURE_COVARIATE`. Cells that are not on the fitted plate give `UNSEEN_LEVELS`.

### `sci.turing_pattern` — Turing patterns from early images (E61)

Tutorial: [E61 from start to end](../tutorials/pymc-challenges/e61-turing-patterns.md).

The Schnakenberg activator-substrate system makes spots or stripes (a Turing pattern). The
model gets its rates from images of the activator that you take while the pattern starts to
grow. In this phase the growth is linear. Thus each Fourier mode of the images is a complex
Gaussian, and the modes are independent. The model groups the modes by their integer `|k|²`
and uses a Woodbury identity. Thus the likelihood needs only 2 × 2 algebra for each
wavenumber, and it is exact for the linear dynamics. The model finds the mode first, and NUTS
samples near that mode with a Laplace preconditioner.

The units have no dimensions. Lengths are in units where the activator diffuses at a rate of 1.
The parameters are the supply `s = a + b`, the activator share `rho = a / s`, the diffusion
ratio `d`, the reaction scale `g`, the initial noise `s0` and the camera noise `sigma`. The
priors are those of the notebook.

**Inputs.** `t` (float, extend: the time since the start of the pattern, `t > 0`), `y` and `x`
(integer, fixed) and the target `activator(t, y, x)`. The raster must be square, with an even
number of pixels on each side, and must have no missing pixels. The model treats the square as
periodic.

**Outputs.**

- `pattern(t, y, x)`: a field with a frame for each time. In `predict` and `evaluate`, it is a
  draw of the activator at the requested times, given the fitted images. In `simulate`, it is a
  new pattern from the nonlinear spectral solver, with camera noise.
- `settled(y, x)`: only in `simulate`. The pattern of each run at `settle_time`.
- `growth(wavenumber)`: the dispersion relation (the growth rate of each wavenumber).
- `p_turing`: 1 when the draw meets the Turing conditions, else 0. Its mean is the probability.
- `wavelength`: the wavelength `2π / k_m` of the fastest-growing mode.
- `turing_space(b_supply, a_supply)`: a field. Its mean is P(Turing) for each pair of supply
  rates.

The posterior also has `a`, `b`, `wavelength`, `max_growth` and `p_turing`. With
`fit.keep.log_likelihood: true`, the bundle keeps the log-likelihood of each Fourier mode
(`log_likelihood/activator(chain, draw, mode)`). The modes are independent, thus `elpd_loo`
over the modes is valid.

| Setting | Default | Meaning |
|---|---|---|
| `box_length` | 100 | The side of the square in the length unit of the model |
| `k2_max` | 1.5 | Use only the Fourier modes with a squared wavenumber less than this value |
| `max_extension` | 2 | Image times after the fit that a prediction can go to |
| `d_median`, `d_log_sd` | 10, 1 | Log-normal prior of the diffusion ratio |
| `initial_noise_median`, `camera_noise_median` | 0.001, 0.001 | Prior medians of `s0` and `sigma` |
| `field_draws` | 40 | Posterior draws for each score |
| `simulate_draws` | 4 | Nonlinear runs for each `simulate` score |
| `simulate_dt` | auto | Time step of the spectral solver (default `min(0.025, 0.1 / (g (1 + s²)))`) |
| `settle_time` | 300 | Time of the settled pattern in `simulate` |
| `wavenumber_max`, `wavenumber_points` | 1.2, 120 | Largest wavenumber and points of the `growth(wavenumber)` output |
| `a_max`, `b_max`, `supply_points` | 0.6, 3, 60 | Largest supply rates and grid points of the `turing_space(b_supply, a_supply)` output |
| `precondition` | true | Sample near the mode with a Laplace preconditioner (the model does not change) |

E61 part A has no data file: the notebook simulates its four 128 × 128 images (t = 4, 8, 12,
16). `from_challenges` and `notebook_dataset()` do the same simulation, and
`NOTEBOOK_SPEC_YAML` fits it with the sampler settings of the notebook. The 89% intervals are
d 17.7–24.6, g 0.90–1.07, b 0.85–1.04 and a 0.057–0.130 (the notebook gives 17.7–25.2,
0.90–1.08, 0.84–1.04 and 0.054–0.133). The truth is 20, 1, 0.9 and 0.1.

Typed errors: `SCHEMA_INVALID` for missing pixels, a raster that is not square or times that
are not after the start. `SETTINGS_INVALID` when no mode has `|k|²` less than `k2_max`, or when
the spectral solver diverges in `simulate` (set a smaller `simulate_dt`). `forecast` and
`counterfactual` give `MODE_NOT_SUPPORTED`. To predict later images, use `predict` with the
next times. The `NONLINEAR_REGIME` diagnostic shows when the images are more than 10% from
their mean, because then the linear likelihood is not correct.

### `sci.storm_localisation` — molecule positions in STORM frames (E58)

Tutorial: [E58 from start to end](../tutorials/pymc-challenges/e58-storm-localisation.md).

Single-molecule localisation microscopy (STORM, PALM, dSTORM). The data are the raw frames of
an EMCCD camera. The model finds isolated spots and gives the position of each molecule with an
error bar in nm. Detection, camera calibration and the patches use NumPy and `scipy.ndimage` at
fit time. Then NUTS fits all patches in one model. Each patch has one emitter with its own
position, photons, background and PSF sd. The image of the emitter is a Gaussian PSF that is
integrated over each pixel (`erf`). The counts are Poisson.

**Camera.** An EMCCD at high gain doubles the variance of the photon counts. The effective
counts `z = round((ADU - offset_adu) / scale_adu)` are Poisson with a mean of half the photons.
`scale_adu` is twice the EM gain. Set `offset_adu` and `scale_adu` together. If you do not set
them, the model fits the photon-transfer curve of the frames: the robust temporal variance of
each pixel against its median, without the brightest 10 % of the pixels. This calibration needs
pixels with a range of background brightness. A small crop over one bright structure is not
sufficient. For such data, calibrate on a larger field and set the two values.

**Detection.** The model subtracts the per-pixel time median from each frame, smooths with a
Gaussian (`smooth_px`) and keeps the connected regions above `threshold_sd` robust noise sds.
A blob is isolated when no other blob is in its `patch_px` × `patch_px` patch and it has
`min_blob_px` to `max_blob_px` pixels. The fit uses `max_spots` isolated blobs, evenly spaced
in frame order.

**Inputs.** `frame`, `row` and `col` (integer, fixed; rows and columns are consecutive pixel
numbers) and the target `frames(frame, row, col)` (int32, ADU). The frames must have no missing
values. Thus the model has no split: evaluate it in sample.

**Outputs.**

- `x`, `y` (spot): the position in nm. It is the column (row) number times `pixel_nm`.
- `photons`, `background` (photons per pixel) and `psf_sd` (nm) of each spot.
- `render(render_y, render_x)`: the super-resolution image (a field). Each draw puts each spot
  at its drawn position as a small Gaussian. The mean of the draws is the usual image where
  each localisation is a Gaussian with its own error.
- `patch_fit(spot, py, px)`: the effective counts of each patch (a field with a frame for each
  spot). With `kind: predictive`, these are Poisson draws.
- `frames_hat(frame, row, col)`: the camera image in ADU. Inside a fitted patch it is
  `offset_adu + scale_adu × patch_fit`. Outside the patches it is the per-pixel time median.
  `evaluate` compares it with the frames. Spots that the model did not fit count as misfit.

The bundle also keeps `posterior_predictive/patch_fit` (`ppc_draws` draws) and
`observed_data/patch_fit`. Thus `sci.image_residuals` shows the data, the fit and the residuals
of each spot. The bundle keeps `predictions/render` (`score_draws` draws) as well, so
`sci.point_pattern` (the positions with error ellipses over `render`) and `sci.field_map` draw
the super-resolved image from the fit alone. A whole long movie, such as the 2,000 real
frames, is too large for the in-sample evaluate (see `PAYLOAD_TOO_LARGE` below).

| Setting | Default | Meaning |
|---|---|---|
| `pixel_nm` | 100 | Size of a camera pixel in the sample (nm) |
| `kind` | `flat` | Background in each patch: `flat`, or `plane` (a log-linear slope in x and y) |
| `offset_adu`, `scale_adu` | unset | Camera offset (ADU) and ADU per effective count; unset: from the frames |
| `max_spots` | 200 | Isolated spots to fit |
| `threshold_sd`, `smooth_px` | 5, 1 | Detection threshold (robust sds) and smoothing (px) |
| `patch_px` | 11 | Side of each patch (odd, px) |
| `min_blob_px`, `max_blob_px` | 13, 39 | Size limits of a fitted blob (px) |
| `amplitude_band` | 0, 1 | Quantile band of the blob amplitude to fit |
| `photons_median`, `photons_log_sd` | 1500, 1 | Log-normal prior of the photons |
| `psf_sd_px`, `psf_log_sd` | 1, 0.2 | Log-normal prior of the PSF sd (px) |
| `position_sd_px` | 2 | Prior sd of the position around the blob centre (px) |
| `background_photons`, `background_log_sd` | unset, 0.5 | Log-normal prior of the background; unset: twice the median effective count |
| `gradient_sd` | 0.05 | Prior sd of the background slopes (`kind: plane`) |
| `score_draws`, `ppc_draws` | 40, 20 | Posterior draws in a score, and predictive patch draws in the bundle |
| `render_px_nm` | 16 | Pixel size of `render` (nm, at least 1) |

To compare `flat` and `plane` by `elpd_loo`, fit both with `fit.keep.log_likelihood: true`. The
pointwise values are per patch pixel, and the two kinds use the same patches. The
`PATCH_MISFIT` diagnostic warns when the Pearson χ² per patch pixel at the posterior mean is
more than 1.5.

Typed errors:

- `SETTINGS_INVALID`: only one of `offset_adu` and `scale_adu`, an even `patch_px`,
  `min_blob_px` > `max_blob_px`, or a bad `amplitude_band` (refused when the settings are
  parsed, before the data is read); a `render` of `score_draws` draws at `render_px_nm` with
  more than 50 million values (refused at the start of the fit).
- `SCHEMA_INVALID`: missing frames or pixels, rows or columns that are not consecutive, fewer
  than 3 frames, frames smaller than a patch, no spot above the threshold, no isolated spot, or
  frames that cannot calibrate the camera.
- `UNSEEN_LEVELS`: a score with frames, rows or columns that the fit did not have.
- `PAYLOAD_TOO_LARGE`: `frames_hat` for the scored frames has more than 50 million values.
  Score fewer frames, for example the frames with a fitted spot (`constant_data/spot_frame`).
- `MODE_NOT_SUPPORTED`: `forecast`, `counterfactual`, `simulate` and `filter`.

`from_challenges` reads `dannhauser2022_unc13_dstorm_roi.npz`: 2,000 frames of 40 × 40 pixels
(127 nm) of a dSTORM recording of Unc-13 at a *Drosophila* synapse. Its attributes keep the
camera calibration of the notebook (offset 457 ADU, 13.86 ADU per effective count), from the
photon-transfer curve of the full 200 × 200 field. `challenges_settings(ds)` gives these
values as settings. The detector finds 4,192 blobs, as in the notebook. With
`amplitude_band: [0.45, 0.55]` and `max_spots: 1`, the fit is the spot of the notebook (blob
12, frame 5): x = 4.46 ± 0.05 px, about 2,160 photons, a PSF sd of 125 nm and a position error
of 7 nm. 200 spots take approximately one minute with nutpie (4 chains of 1,000 draws). On
these frames, `kind: plane` has more than one mode for approximately 8 % of the spots (spots
at the edge of the crop, or near a bright spot outside the patch). Then the chains do not
agree. Use `flat` for these frames. The model does not do the multi-emitter selection, the
Laplace fits for the full movie or the blink counting of the notebook.

### `sci.ibp_features` — shared parts of small images (E31)

Tutorial: [E31 from start to end](../tutorials/pymc-challenges/e31-indian-buffet-digits.md).

A latent feature model for small images, for example handwritten digits. Each image is the
average image plus the features that it owns, plus Gaussian noise:
`x = mu + z A + e`. Each `z` is 0 or 1. The prior of `z` is an Indian buffet process
(stick-breaking, truncated at `K = n_features`): `alpha ~ Gamma`, `nu_k ~ Beta(alpha, 1)`,
`pi_k = nu_1 ... nu_k`. The model sums `z` out exactly over the `2^K` patterns of each image
(a `pm.Potential`). Thus NUTS samples only the feature images `A`, the sticks, `alpha` and
`sigma_A`. Each extra feature doubles the time, so `n_features` is 10 or less.

**Inputs.** `digit` (categorical, fixed), `y` and `x` (integer, fixed, row 0 at the top) and the
target `ink(digit, y, x)` from 0 (blank) to 1 (full ink). An empty value is a pixel that the
model does not see. With `shown_mask: true`, the data also has a bool `shown(digit, y, x)`.
The fit does not use a pixel where `shown` is false, but the bundle keeps its value for
comparison. The average image `mu` is the mean of the images with no hidden pixels.

**Outputs.** `ink_hat(digit, y, x)` (a field for each digit, grey scale): the expected image
given the seen pixels. Where the model did not see a pixel, this is the completion. The
`predictive` kind draws a pattern and the noise. `features(feature, y, x)` (a field for each
feature, diverging scale) gives the change of ink that each feature adds to the average image.
`feature_prob(feature)` gives `pi_k`, and `owns(digit, feature)` gives the probability that
each image owns each feature. A score uses only the pixels that the bundle keeps, never the
target values in the score data. Thus a held-out pixel cannot get into its own prediction.

| Setting | Default | Meaning |
|---|---|---|
| `n_features` | 8 | Truncation `K` (1 to 10). Each image sums over `2^K` patterns |
| `kind` | `fixed_noise` | `fixed_noise`: the pixel noise is `noise_sd`; `free_noise`: the model learns it (HalfNormal(1) prior) |
| `noise_sd` | 0.25 | Pixel noise `sigma_X`. It sets the level of detail: a smaller value makes more features useful |
| `alpha_shape`, `alpha_rate` | 1, 1 | Gamma prior of the IBP mass `alpha` |
| `feature_scale_sd` | 1 | HalfNormal scale of `sigma_A`, the scale of the feature images |
| `start` | `digits` | `digits`: all chains start, without jitter, at half of `K` random images with `nu = 0.8`; `default`: the jittered start of the engine |
| `score_draws` | 200 | Posterior draws for each score |
| `shown_mask` | false | Read `shown(digit, y, x)` and hide the pixels where it is false |
| `truncation_warn` | 0.05 | Warn when the mean of the last `pi_K` is more than this value |

**Start.** From the default start, some chains can go to a trap: all `pi_k` go to zero and
the images use no features. Then no feature gets a gradient again. The `digits` start
prevents this (the notebook does the same). The engines `pymc`, `nutpie` and the external
NUTS bridges use the start point with no jitter (`EnginePlan.initvals` and `jitter=False`).

**Diagnostics.** The posterior of `A` has many modes, as in a mixture model. Chains usually
find different features, so expect r-hat and ESS warnings for `logpi` and `sigma_A`. The
predictions of the chains are almost the same. `TRUNCATION_BINDS` warns when the last `pi_K`
is not small: more features would be used. `FEATURES_COLLAPSED` warns when a chain has no
features.

**Splits and comparisons.** `digit` is fixed: a score can use only the digits of the fit. To
complete a new image, add it to the data with its unseen pixels empty, and fit again. To hold
out halves of images, use a `mask_block` split over `[digit, y]` with `block: {y: 4}`. With
`fit.keep.log_likelihood: true`, the bundle keeps the log-likelihood of each image. Then
`elpd_loo` compares `n_features`, `noise_sd` and `kind`. With `fit.keep.posterior_predictive`,
the bundle keeps replicate images for `sci.ppc_image_grid` and `sci.image_residuals`.

`from_challenges` reads the UCI optdigits of pymc-challenges as the E31 notebook does: 30
training digits for each class and 20 test digits for each class by 13 other writers, 8 × 6
pixels, ink from 0 to 1. The `visible` mask hides the bottom half of each test digit. The
`learned.heldout` field of the fit gives the completion score of those halves (the mean log
predictive density for each digit and the RMSE). The E31 spec of the export (`K = 8`,
`noise_sd = 0.25`, 4 chains of 1000 draws, all 500 digits with the test bottom halves hidden by
the mask) completes the test halves with an RMSE of 0.284 and −0.70 nats for each digit. The
fit takes approximately 10 minutes with the `pymc` engine, and the convergence checks warn, as
the notebook predicts. The notebook fits the 300 training digits only and gives 0.283 and −0.3.
The export spec also fits the top halves of the test digits, so its log density differs. A
separate fit on the 300 training digits only, as in the notebook, gives 0.283 and −0.34 nats
in approximately 4 minutes.

### `sci.colour_naming` — how the speakers of a language name colours (E77)

Tutorial: [E77 from start to end](../tutorials/pymc-challenges/e77-colour-naming.md).

The World Color Survey asked the speakers of 110 languages to name 330 colour chips. This model
fits the naming of one language. The probability that a speaker gives term `t` to chip `c` is
a softmax over the terms. The logit of each term is a quadratic surface in CIELAB colour space:
a multinomial logistic regression on 10 standardised features
(`1, L, a, b, L², a², b², ab, La, Lb`). The weights `W` are zero-sum over the terms
(`ZeroSumNormal(w_sd)`).

- `kind: pooled` gives all speakers the same naming. The likelihood uses the chip × term counts.
- `kind: hierarchical` gives each speaker an intercept and linear weights (`L, a, b`) of their
  own, around the weights of the language: `W_s = W + sd z_s`, with `sd ~ HalfNormal(sd_scale)`
  and `z ~ ZeroSumNormal(1)`. A term that only some speakers use gets a large `sd` on its
  intercept.
- `features: grid_line` and `features: grid_circle` use the place of the chip on the printed
  palette (row and hue column) instead of its colour. `grid_line` treats the hue as a line, and
  `grid_circle` treats it as a circle. Fit the variants with `elpd_loo` in a workflow to compare
  them.

**Inputs.** `speaker` (categorical, policy `new_levels`, strategy `hyperprior`), `chip`
(integer, fixed) and the target `naming(speaker, chip)`. The target holds the term labels as
text. An empty cell is a speaker who gave no answer. The chip variables are `lab_l`, `lab_a`,
`lab_b` (the CIELAB colour) and `grid_row`, `grid_col` (the place on the palette; WCS rows A to
J are 0 to 9 and the greys are in column 0). The model orders the terms from the most used to
the least used.

**Outputs.**

- `p_name(speaker, chip, term)`: the probability that the speaker gives the term to the chip.
  A fitted speaker keeps their own effects. A new speaker gets new effects in each draw, so the
  mean over the draws is the prediction for a new speaker. With `kind: predictive`, each draw
  holds one sampled term (one-hot). The `evaluate` mode always uses the probabilities.
- `p_term(term, lightness, hue)`: the share of the speakers of the language who give the term
  to the chip, on the palette grid (a field with one frame for each term). The fit averages
  `new_speakers` simulated speakers in each draw. A grid cell without a chip is empty in the
  fitted model and 0 in a score.
- `mode_term_rgb(chip, rgb)`: each chip in the display colour of its most probable term in the
  draw. The display colour of a term is the CIELAB mean of the chips where the term is most
  probable. The `row` and `col` coords put the chips on the palette for `sci.swatches`. The
  observed panel paints each chip with the term that most speakers gave.
- `p_top(chip)`: the share of speakers who give the chip its most probable term.
- `k_eff_speaker` and `k_eff_language`: the effective number of colour categories of one
  speaker and of the language, `exp` of the mutual information between chip and term.

The fitted model keeps these language-level outputs for every draw. Thus the fit-time plots
(`sci.field_map`, `sci.swatches`) show them without a score. The pointwise log-likelihood has
one value for each chip (the sum over its speakers). Thus `elpd_loo` is leave-one-chip-out, and
the variants have the same points. Use the categorical metrics (`log_score`, `accuracy`,
`accuracy_lift`, `brier`) with a `leave_group_out` split on `speaker`: they score the held-out
speakers as new speakers.

| Setting | Default | Meaning |
|---|---|---|
| `kind` | `hierarchical` | `hierarchical` (speaker effects) or `pooled` |
| `features` | `cielab` | `cielab`, `grid_line` or `grid_circle` |
| `w_sd` | 3 | Prior sd of the zero-sum weights |
| `sd_scale` | 1 | Half-normal scale of the sds between speakers |
| `n_random` | 4 | The number of leading features with speaker effects (the intercept and `L, a, b`) |
| `hue_columns` | the largest `grid_col` | The number of hue columns around the circle (`grid_circle`) |
| `new_speakers` | 20 | Simulated new speakers in each draw for the language-level shares |
| `score_draws` | 200 | Posterior draws that one score uses |

`from_challenges(path)` reads `wcs_naming.npz` and the three WCS tables next to it. It takes
one language (`language=77`, Nafaanra) and keeps the major terms (used by at least a third of
the speakers). All other terms become `other`. `NAFAANRA_SPEC_YAML` holds out the same 9
speakers as the notebook. On Nafaanra the pooled fit scores the held-out speakers at -0.392 nats
for each answer, and the hierarchical fit gives 1.92 effective categories for one speaker and
1.84 for the language.

The model stops with `SCHEMA_INVALID` when the naming has fewer than 2 or more than 40 terms,
a chip has no CIELAB colour, or two chips have the same palette place. It stops with
`SETTINGS_INVALID` when `kind: hierarchical` has only one speaker, or `n_random` is larger than
the number of features (refused when the settings are parsed). Chips that are not in the fit give `UNSEEN_LEVELS`.

### `sci.cmb_field` — the CMB sky behind a mask (E79)

Tutorial: [E79 from start to end](../tutorials/pymc-challenges/e79-cmb-lmc-mask.md).

Field-level inference of a temperature map of the cosmic microwave background (CMB). Each pixel
of the sky is a parameter. The model samples the sky together with the power spectrum in bins
(bandpowers). Masked pixels have no data term, and each pixel has its own noise level. Thus the
model needs no correction for the mask, as a simple spectrum estimate does.

The sky is a Gaussian random field on a flat, periodic torus. The torus is the map plus a
border of `border` pixels without data on each side, because a real patch does not repeat at
its edges. The prior power of each Fourier (Hartley) mode is the reference spectrum after the
beam, the pixel windows and aliasing, times `exp(q_b)` for bin `b`. `q_b` has a flat prior.

The sampler is not NUTS. It is the Wandelt-Eriksen Gibbs sampler of the notebook, in NumPy:

1. A constrained realisation of the sky for the current bandpowers. Preconditioned conjugate
   gradients (CG) solve one linear system. The preconditioner is dense below `ell_dense` and
   diagonal above it.
2. A draw of each bandpower from its inverse-gamma distribution, given the sky.
3. A rescaling move for each bin (Metropolis). It helps in the bins where the noise is larger
   than the signal.

`tune` sets the burn-in iterations and `draws` the kept iterations of each chain. All chains run
together. The fit is deterministic for a given seed.

**Inputs.** `row` and `col` (integer, fixed, row 0 at the bottom) and `multipole` (integer,
fixed, 0, 1, 2, ...). The target `temperature(row, col)` is in µK, with NaN where the mask
removes a pixel. `noise_sd(row, col)` is the noise sd of each pixel (µK). `reference_dl(multipole)`
is the reference spectrum D_ℓ = ℓ(ℓ+1)C_ℓ/2π in µK². With `pixel_window: true`, the data also has
`pixel_window(multipole)`, the pixel window of the source map (for example HEALPix).

**Outputs.** `sky(row, col)` and `temperature_hat(row, col)` are fields (diverging colours).
`sky` is a sky map for each of `field_draws` posterior draws. `temperature_hat` is the same
map, plus noise with `noise_sd` for `kind: predictive`. `bandpower(bin)` (µK²) and
`bandpower_ratio(bin)` have the coords `ell`, `ell_low` and `ell_high` on `bin`. The scalars
`log_amplitude` and `tilt` compare the bandpowers with the reference: they come from the exact
Gaussian posterior of `q_b = log A + tilt · log(ℓ_b / tilt_pivot)`, with the bins below
`tilt_max_ell`. The fit keeps the mean and the sd of each pixel over all kept Gibbs draws as the
derived group `sky_moments`. In `simulate` mode, the model makes skies of a new patch with the
spectrum of each posterior draw, plus noise. The mean of a new patch comes from the spectrum
too (a few µK), not from the vague `mean_prior_sd` prior of the fitted patch's mean.

| Setting | Default | Meaning |
|---|---|---|
| `pixel_arcmin`, `beam_fwhm_arcmin` | 7.5, 5 | Pixel side and FWHM of the Gaussian beam (arcmin) |
| `pixel_subsamples` | 8 | Point samples along each side that a map pixel is the mean of (1: no box window) |
| `pixel_window` | false | The data has `pixel_window(multipole)` |
| `bin_edges` | the 26 bins of the notebook | Multipole edges of the bandpower bins |
| `projection` | `gnomonic` | `gnomonic`: correct the multipoles for the mean stretch of a tangent-plane map; `flat`: no correction |
| `border` | 8 | Pixels without data around the map on the torus |
| `ell_dense` | 300 | Modes below this multipole get the dense preconditioner |
| `cg_tol`, `cg_max_iter` | 1e-5, 5000 | Stop rule of CG |
| `rescale` | true | Use the rescaling move |
| `tilt_pivot`, `tilt_max_ell`, `tilt_prior_sd` | 500, 1450, 0.5 | The amplitude-and-tilt summary |
| `field_draws` | 40 | The number of sky maps to draw for each score |
| `n_alias` | 3 | Aliased replicas folded into the prior power of each mode |
| `mean_prior_sd` | 100 | Prior sd of the patch mean (µK) in the fit |
| `q_init_sd` | 0.3 | Sd of the dispersed chain starts of the log bandpowers |

The model has no LOO (`loo_valid` is false), because all pixels share one latent field. To
compare `projection: gnomonic` with `flat`, compare the bandpowers, the amplitude and the tilt.
The diagnostic `CG_NOT_CONVERGED` shows that a solve stopped at `cg_max_iter`. Then the
large-scale bandpowers can be too low.

Typed errors: `SCHEMA_INVALID` when a variable or a dim is missing, when all pixels are masked,
when `noise_sd` is not positive at an unmasked pixel, when `reference_dl` is negative or all
zero, or when the map is smaller than 8 × 8 pixels. `SETTINGS_INVALID` when a bin has no mode on
the torus, when the torus has more than 512 × 512 pixels, or when `ell_dense` puts more than
2,500 modes in the dense preconditioner.

`from_challenges(path, patch="lmc")` reads the Planck 2018 SMICA patch around the Large
Magellanic Cloud (or `patch="north"`) from `planck_smica_patches.npz` and the Planck best-fit
spectrum from `planck_theory_tt.txt`. It prepares the map as the notebook does: 128 × 128 pixels
of 7.5 arcmin, a pixel is usable only if all its samples are inside the common mask (16.5% of
the pixels are masked), and the noise sd is the local rms of the half-mission half-difference
map. `PLANCK_SPEC_YAML` is the notebook fit (4 chains of 80 iterations, the first 16 are
burn-in). It takes approximately one minute. On this patch, the log amplitude is
−0.017 ± 0.022 and the tilt is −0.001 ± 0.028 (the notebook: −0.014 ± 0.023 and −0.009 ± 0.030).
The pixel sd is 2.5 µK where there are data, 32 µK in the small holes and 89 µK in the hole of
the LMC.

### `sci.network_latent_space` — who works with whom (E21)

Tutorial: [E21 from start to end](../tutorials/pymc-challenges/e21-law-firm-network.md).

A model of an undirected graph. Each pair of nodes `i < j` has a tie or not
(`Bernoulli`, logit link). The log odds have three parts:

- `alpha + x_ij beta`: the pair covariates (homophily), for example the same office.
- `s_i + s_j`: the sociality of the two nodes (how many ties each node has).
- `-||z_i - z_j||`: the distance between the latent positions of the two nodes.

The `kind` setting selects the parts: `covariates` (the first part), `sociality` (the first
two) or `latent_space` (all three). With `covariates: false`, the model has no pair covariates.
`kind: covariates` with `covariates: false` is the Erdős-Rényi graph.

The distances do not change when the positions turn, flip or move together. Thus the raw
positions `z_raw` do not converge, but each distance does. After the fit, the model aligns each
draw to one reference with Procrustes analysis. The aligned draws are the posterior variable
`position`. The reference is `constant_data/position_ref`. The fit then drops the raw draws
`z_raw` and `s_raw` from the posterior (`position` and `sociality` hold them, with the same
distances). Thus the convergence diagnostics and the plots `core.convergence` and
`core.posterior_forest` use only identified quantities: `alpha`, `beta`, `sigma_s`, `sigma_z`,
`sociality`, `position` and `tie_prob`, in that order.

**Inputs.** `node` and `node_` (the same labels, fixed) and the target `tie(node, node_)`: 1
for a tie, 0 for no tie, and a missing value when you do not know. The matrix must be
symmetric. The likelihood uses a pair only when its two cells are not missing. With
`covariates: true`, the data has `dyad_cov(node, node_, cov)` (`cov` is categorical, fixed).
With `node_groups: true`, the data has `node_group(node)`, an integer code for the colours of
the plots.

**Outputs.**

- `tie_prob(node, node_)`: the probability of a tie (a field, 0 on the diagonal).
- `tie(node, node_)`: the probability, or a simulated graph with `kind: predictive` and in
  `simulate` mode. The graphs are symmetric. The random numbers belong to the pair, so a
  subset of the nodes gets the same ties.
- `degree(node)`: the ties of each node with the scored nodes (the `node_` labels of the
  score data), expected or in a simulated graph. Score all the nodes for the degree in the
  graph: on a subset, `degree` counts only the ties inside the subset.
- `sociality(node)` and `position(node, latent_dim)`, when the kind has them.
- `graph_stat(statistic)`: statistics of each simulated graph (`density`, `sd_degree`,
  `max_degree`, `isolates`, `triangles`, `transitivity`, `esp0`, `mean_geodesic`,
  `unreachable`). `esp0` is the number of ties whose two nodes have no shared neighbour.
  Compare them with the statistics of the data (posterior predictive checks). The statistics
  need simulated graphs of all the nodes of the fit. A request that names `graph_stat` in
  `output.vars` for a subset of the nodes, or for expected probabilities, fails with
  `CAPABILITY_UNSUPPORTED`.

The posterior also keeps `tie_prob` (float32) for the plots of the fit, and
`constant_data/adjacency` keeps the observed matrix. The fit always keeps the log-likelihood of
each observed pair (dim `dyad`; the pairs are in `constant_data/dyad_i` and `dyad_j`). Thus
`elpd_loo` is leave-one-pair-out, and the kinds of a workflow ladder use the same pairs in the
same order. A `mask_block` split over `[node, node_]` holds out random cells. A pair is out of
the fit when one of its two cells is held out.

| Setting | Default | Meaning |
|---|---|---|
| `kind` | `latent_space` | `covariates`, `sociality` or `latent_space` |
| `covariates` | true | Use `dyad_cov` |
| `node_groups` | false | The data has `node_group` |
| `latent_dims` | 2 | Dimensions of the latent space (1 to 3) |
| `alpha_mu`, `alpha_sigma` | -2, 2 | Normal prior of the intercept |
| `beta_sigma` | 1 | Normal prior sd of each covariate effect |
| `sociality_scale`, `latent_scale` | 1, 2 | Half-normal priors of `sigma_s` and `sigma_z` |
| `procrustes_iters` | 10 | Passes of the Procrustes alignment |

`from_challenges` reads the coworker network of the 71 lawyers of the Lazega firm (378 ties)
and makes the six pair covariates of the notebook: same office, practice, status, gender and
law school, and the gap in years with the firm ÷ 10. `office` is the node group. With 4 chains
of 1000 draws, the fit takes approximately 15 s (`sociality`) to 45 s (`latent_space`).
`elpd_loo` is -1060 (Erdős-Rényi), -894 (`covariates`), -764 (`sociality`), -795
(`latent_space` without covariates) and -761 (`latent_space`), as in the notebook.

A score with fewer covariates than the fit stops with `MISSING_FUTURE_COVARIATE`. A node that
the fit did not see stops with `UNSEEN_LEVELS`. The model has no `forecast` and no
`counterfactual` mode. The mixed-membership block models of the notebook are not part of the
model.

### `sci.graphical_model` — direct links between variables (E19)

Tutorial: [E19 from start to end](../tutorials/pymc-challenges/e19-protein-signalling.md).

A Gaussian graphical model. The model measures the partial correlation of each pair of
variables: the correlation that stays when all the other variables are held constant. A zero
partial correlation means that the two variables have no direct link. A link (an edge) is
present when `|partial correlation| > threshold`, and the model gives the probability of each
edge. The model centres and scales each variable with the mean and the sd of the fit data.

There are two kinds:

- `dense` (default): one multivariate normal with an LKJ prior on the correlations
  (`eta = 2`) and an Exponential(1) prior on the sds. The precision matrix comes from a
  triangular solve. This kind also predicts and simulates new samples.
- `neighbourhood`: a sparse (regularised horseshoe) regression of each variable on all the
  others, in one model. The `edge_rule` setting makes the two coefficients of a pair into one
  edge: `and` (both pass the threshold), `or` (one passes) or `rho` (their symmetric
  combination). This kind conditions on the other variables of each sample. Thus it does not
  predict, simulate or evaluate new samples, and a spec with this kind has no split.

**Inputs.** `cell` (integer, policy `new_levels` with the `pooled` strategy: the samples are
exchangeable, so new samples can be scored), `var` (categorical, fixed: the variables) and the
target `value(cell, var)`. With `reference: true`, the data also has a known network
`reference(var, var_)` (1 = edge, constant). The fit keeps it in `constant_data`, and
`net.graph` draws it below the learned edges.

**Outputs.** `pcor(var, var_)` (the partial correlations, a field with a diverging colour
scale), `edge_prob(var, var_)` (the edge indicator of each draw: its mean is the edge
probability) and, for kind `dense`, `value(cell, var)`. With kind `expected`, `value` is the
mean of each variable. With kind `predictive` or in `simulate` mode, it is a new sample from the
model. The fit also keeps the posterior `precision(var, var_)` and the learned values `edges`
(the pairs with a probability above 0.5), `undecided_pairs` (between 0.2 and 0.8) and
`pcor_sd_mean`.

| Setting | Default | Meaning |
|---|---|---|
| `kind` | `dense` | `dense` (LKJ multivariate normal) or `neighbourhood` (horseshoe regressions) |
| `threshold` | 0.1 | An edge is a partial correlation larger than this value (in absolute value) |
| `reference` | false | The data has a known network `reference(var, var_)` |
| `standardise` | true | Centre and scale each variable with the fit data |
| `log_likelihood` | `auto` | The log-likelihood that the fit keeps for `elpd_loo`: `joint` (dense only) or `conditional`. `auto` is `joint` for dense and `conditional` for neighbourhood |
| `lkj_eta`, `sd_rate`, `mean_sd` | 2, 1, 1 | dense: the LKJ shape, the rate of the sd prior and the sd of the mean prior |
| `expected_neighbours` | 2 | neighbourhood: the prior guess of the links of each variable |
| `slab_alpha`, `slab_beta` | 2, 2 | neighbourhood: the slab prior `c² ~ InvGamma(alpha, beta)` |
| `edge_rule` | `and` | neighbourhood: `and`, `or` or `rho` |

The fit keeps one log-likelihood value for each sample when `fit.keep.log_likelihood` is true.
To compare a `dense` fit and a `neighbourhood` fit with `elpd_loo`, set
`log_likelihood: conditional` on both. Then both values are the sum of the log densities of
each variable given the other variables of the same sample.

Typed errors: `SETTINGS_INVALID` for `log_likelihood: joint` with kind `neighbourhood` (when
the settings are parsed), or `expected_neighbours` that is not smaller than the number of other
variables; `SCHEMA_INVALID` for score data without a `cell` dim, missing values, a variable with no variation, fewer than two variables,
or `reference: true` without a `reference` variable; `MODE_NOT_SUPPORTED` for `simulate`,
`evaluate` or `predictive` outputs with kind `neighbourhood`, and for `forecast` and
`counterfactual`.

`from_challenges` reads the Sachs et al. (2005) flow cytometry data: the 853 cells with only the
CD3/CD28 stimulus and 11 proteins (log of the intensity + 10). When `sachs2005_consensus.txt` is
in the same directory, it adds the 20 edges of the consensus network as `consensus`. With
`observational=False`, it reads all 7466 cells of the nine conditions (the notebook shows why
you must not pool them). On the 853 cells, the dense model finds 8 edges with a probability
above 0.5. Seven of them are in the consensus network. The fit takes approximately 40 seconds
with nutpie. The neighbourhood model finds 6 edges, all in the consensus network, in
approximately 2 minutes.

### `sci.phylo_regression` — traits of related species on a tree (E23)

Tutorial: [E23 from start to end](../tutorials/pymc-challenges/e23-phylogenetic-regression.md).

A regression of one trait of species on other traits. Related species are alike because
they share ancestors, so the residuals are correlated by a phylogeny (a tree with branch
lengths). The model reads the correlation from the shared branch length of two species. On
standardised log values it is `trait ~ MvNormal(intercept + X slope, K)`. The `kind` setting
selects `K`:

| `kind` | Residual correlation |
|---|---|
| `iid` | None: `sigma² I` (the tree is not used) |
| `brownian` | Brownian motion along the branches: `sigma² R` (`R` = shared length ÷ tree depth) |
| `lambda` | Pagel's lambda: `sigma² (lam R + (1 - lam) I)`, a tree part and a species part |
| `ou` | Ornstein-Uhlenbeck: `eta2 exp(-rho D / 2T)` on the distance `D` through the tree (`T` = tree depth) |

For `ou`, the data identify only the Brownian rate `rate = eta2 rho`. Thus the default prior
is on `rate` (`ou_parameterisation: rate`). This removes the ridge of the `(eta2, rho)`
posterior.

**Inputs.** `species` (categorical, fixed) and `node` (integer, fixed). The target is
`trait(species)`; missing values are permitted. Each name in `predictors` is a variable on
`species` with no missing values. The tree is four variables on `node`: `parent` (the
position of the parent node, -1 at the root), `branch_length` (the branch above the node),
`is_tip` and `tip_label` (the species name of each tip). Each species label must be a tip
label. The tree can have unary nodes.

**Outputs.** `trait(species)`: the trait of each species, predicted from the other species
by Gaussian conditioning, in the units of the target. For a fitted species, the prediction
leaves that species out. For a species without a trait value, it uses all fitted species.
With `log_target`, the expected value is the conditional median. `slope(predictor)`: the
slopes. If the score data changes a fitted species' predictors, its prediction moves by the
change in its regression mean. The fit also keeps `posterior_predictive/trait(species)` (the
same predictions as conditional means, beside `observed_data/trait` for the `sci.phylogeny`
plot) and the tree in `constant_data`.

**Score modes.** `predict` and `evaluate` predict each requested species from its relatives.
`simulate` draws new traits for the requested species from the fitted parameters, without
the observed traits. Use a `mask_block` split on `species` to hold out species: the model
predicts them from the species that remain.

**LOO.** `log_likelihood/trait` holds the leave-one-out conditional density of each observed
species, `log p(y_i | y_-i)`, from `Q = K⁻¹`. Thus `elpd_loo` compares the four kinds
correctly (a workflow can fit each `kind` and compare them). The marginal density of each
species gives the opposite ranking.

| Setting | Default | Meaning |
|---|---|---|
| `kind` | `lambda` | `iid`, `brownian`, `lambda` or `ou` |
| `predictors` | `[body, group_size]` | The predictor variables on `species` |
| `log_target`, `log_predictors` | true, true | Take the log first (the values must be positive) |
| `standardise` | true | Centre and scale by the fitted species |
| `intercept_sd`, `slope_sd` | 1, 0.5 | Normal prior sd of the intercept and of each slope |
| `sigma_rate` | 1 | Rate of the exponential prior of `sigma` |
| `lambda_alpha`, `lambda_beta` | 1, 1 | Beta prior of `lam` |
| `ou_parameterisation` | `rate` | `rate`: prior on `eta2 rho`; `variance`: priors on `eta2` and `rho` |
| `rate_sd`, `rho_sd`, `eta2_sd` | 1, 5, 5 | Half-normal prior sd for the `ou` parameters |
| `jitter` | 1e-6 | `ou`: added to the diagonal of `K` |

`from_challenges` reads `primates301.csv` and the tree `primates301_tree.rda` (an R
`ape::phylo` object). It reads the R file without R. `read_tree` also reads Newick files. The
dataset has the 186 species with a body mass and a group size; 151 of them have a brain
volume. The tree is pruned to these species. With the notebook settings (4 chains of 1000
draws), the `lambda` fit gives a group-size slope of 0.027 ± 0.020 and `lam` = 0.954. The
`iid` fit gives 0.123. `elpd_loo` is about 130 for `lambda`, 91 for `brownian` and `ou`, and
15 for `iid`.

**Typed errors.** `SCHEMA_INVALID` when a species has no tip of the same label, the tree has
no single root or has a cycle, a predictor has missing values, a value is not positive for a
log, or fewer than 3 species have a trait value. `UNSEEN_LEVELS` when a score requests a
species that the fit did not see.

### `sci.circular_regression` — directions and times of day (E81)

Tutorial: [E81 from start to end](../tutorials/pymc-challenges/e81-wind-and-activity.md).

The data are angles: the direction that a wind comes from, or the time of day of a
photograph. 359 degrees is next to 1 degree, and 23:59 is next to 00:01. Thus the model uses
densities on the circle, as the E81 case study does. It has four kinds:

- `von_mises` (default). The hour of the day and the day of the year are features: an
  intercept, `cos` and `sin` of `k 2π h / 24` and of `k 2π d / 365.25`, and the products of the
  first daily harmonics with the first annual harmonic. The defaults give 19 features `F`. The
  natural parameters of a von Mises are `η = F B` (the canonical link). The mean direction is
  `atan2(η₂, η₁)` and the concentration is `|η|`. This model has one mode. It has no wrapped
  prior and no seam at ±π.
- `projected_normal`. The angle of a vector `z ~ N(F B, I)`.
- `regime_mixture`. Two von Mises regimes (for example the easterly night wind and the
  westerly sea breeze). Each regime uses fewer features. A logistic gate on all features gives
  the probability of the second regime. The prior directions of the regimes keep their labels.
- `activity`. A log-Fourier density for each group (species):
  `log f(θ) = Σ aₖ cos kθ + bₖ sin kθ − log Z`, with the prior `aₖ, bₖ ~ N(0, (τ/k)²)`. The
  model uses only the sums of `cos kθ` and `sin kθ` for each group.

The priors of `von_mises` and `projected_normal` are N(0, `intercept_sd` = 2) on the intercepts
and N(0, `coef_sd` = 1) on the other coefficients. `regime_mixture` does not use
`intercept_sd`, as in the notebook. The intercept of regime r has the prior N(`regime_strength`
(cos μᵣ, sin μᵣ), 1), with μᵣ from `regime_directions_deg`. The gate intercept has the prior
N(0, `gate_intercept_sd` = 1.5). The other coefficients of the regimes and of the gate have the
prior N(0, `coef_sd`). NUTS samples the coefficients. With `fit.keep.log_likelihood: true`, the
bundle keeps the log density of each row (per radian). Thus `elpd_loo` compares the kinds on
the same rows, for example in a workflow ladder `von_mises` → `projected_normal` →
`regime_mixture`.

**Inputs.** `obs` (integer, policy `extend`, one row for each report) and the target
`direction(obs)` in `units`. The regression kinds also need `hour(obs)` (hours since midnight,
0 to 24) and `day(obs)` (days since 1 January). The kind `activity` needs a categorical dim
(`group_dim`, fixed) and the index `<group_dim>_idx(obs)`. Remove rows with no direction (a calm
wind has no direction) before the fit.

**Outputs.**

- `direction_hat(obs)`: the mean direction of the row, in `units` from 0 to one turn. With
  `kind: predictive` and in `simulate` mode, it is one simulated angle.
- `resultant(obs)`: the mean resultant length of the row (1: one direction, 0: no preferred
  direction).
- `density(frame, angle)`: the density per unit of `units` on `angle_points` angles (a field:
  frames on the rows, angles on the columns). For the regression kinds, a frame is a block of
  `24 / frames` hours. Its density is the mean of the densities of up to `rows_per_frame` fit
  rows in that block (the rose diagram of the model). For `activity`, a frame is a group.
- `mean_direction(frame)` and `resultant_length(frame)`: the mean resultant vector of each
  frame.
- `activity` only: `activity_level(frame)` (`1 / (2π max f)`, the part of the day that the
  group is active) and `overlap(frame, frame_)` (the area under the lower of two densities, a
  field).

The fit also keeps the fields `density(frame, angle)` and, for `activity`, `overlap(frame,
frame_)` in the posterior. They have the same values as in a score. Thus `sci.field_map` and
`sci.profile_bands` show the densities (and, with `var: overlap`, the overlaps) in the plots
of a fit run.

With `fit.keep.posterior_predictive`, the bundle keeps simulated angles `theta` (radians, 0 to
2π) and the observed `theta` in `observed_data`. `sci.rose` uses them in its default
settings: it shows the observed rose with the range of the model. The default settings use
the orientation of mathematics: 0 is at the right (east) and the angles increase
anticlockwise. Thus a wind from the west (270°) is at the bottom. For a compass or a clock (0
at the top, clockwise), pass `clockwise: true` to `sci.rose` in a `plot.render` step or in the
plots request. For the density of each frame, use `sci.rose` with `var: density`, `frame:
frame` and `units` set to the units of the data. The default rose of a score result reads
`direction_hat`, which is in the units of the data, as radians. Pass `var: direction_hat` and
`units` set to the units of the data.

| Setting | Default | Meaning |
|---|---|---|
| `kind` | `von_mises` | `von_mises`, `projected_normal`, `regime_mixture` or `activity` |
| `units` | `degrees` | Units of the direction: `degrees`, `radians` or `hours` |
| `daily_harmonics`, `annual_harmonics` | 3, 2 | Harmonics of the hour and of the day of the year |
| `interaction_harmonics` | 2 | Daily harmonics multiplied by the first annual harmonic |
| `intercept_sd`, `coef_sd` | 2, 1 | Prior sd of the intercepts (not for `regime_mixture`) and of the other coefficients |
| `regime_directions_deg`, `regime_names` | [90, 250], [easterly, westerly] | `regime_mixture`: prior directions and names of the two regimes |
| `regime_daily_harmonics`, `regime_annual_harmonics` | 2, 1 | `regime_mixture`: features of each regime |
| `regime_strength` | 2 | `regime_mixture`: prior mean concentration of each regime (the length of the prior mean of its intercept) |
| `gate_intercept_sd` | 1.5 | `regime_mixture`: prior sd of the gate intercept |
| `group_dim` | `group` | `activity`: the dim of the groups |
| `harmonics`, `tau_sd`, `grid_points` | 16, 2, 256 | `activity`: Fourier terms, prior scale of `τ`, angles of the sum for `Z` |
| `frames`, `angle_points`, `rows_per_frame` | 8, 72, 200 | Blocks of the day, angles and fit rows of the `density` output |
| `score_draws` | 400 | Posterior draws for each score |

`from_challenges` reads the hourly wind reports of Santa Barbara airport (2021–2023). It removes
the calm reports and uses Pacific standard time (UTC − 8). `ksba_spec_yaml(ds)` holds out 2023 as
the notebook does. On the 12,023 reports of 2021–2022, `elpd_loo` is −17,385 (the notebook:
−17,385). The mean log score of 2023 is −1.517 for each report (the notebook: −1.517).
`from_challenges_activity` reads the camera-trap times of Barro Colorado Island for the ocelot
and five species that it hunts. With `BCI_SPEC_YAML`, the overlap of the ocelot and the agouti
is 0.30 (the notebook: 0.30).

`direction_hat` and `mean_direction` are circular outputs (`OutputDecl.circular`, with their
`period` attr: 360, 2π or 24). Their score summaries are on the circle: the circular mean, and
intervals centred on it in `[0, period)`. The holdout metrics of `evaluate` are circular: the point is the circular mean of the
draws, `mae`, `rmse` and `bias` use the error wrapped to half a turn (359° against 1° is an
error of 2°), `coverage_<p>` and `interval_width_<p>` use central intervals of the draws around
the circular mean (an interval may cross 0), and `crps` uses the arc distance. `mape`,
`smape`, `wape` and `r2` are skipped. For a proper score of the whole density, use
`elpd_loo`, or `CircularRegressionModel().holdout_log_score(bundle.model, ds)` for the log
score of held-out rows. Errors: `SCHEMA_INVALID` for a missing direction, an hour out of
range or a bad group index; `MISSING_FUTURE_COVARIATE` when the score data have no `hour` or
`day`; `SETTINGS_INVALID` for more interaction harmonics than daily harmonics.
