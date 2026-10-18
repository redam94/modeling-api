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
| `sci.ibp_features@1` | `mapi-plugin-science` | draws | predict, evaluate | `e31_ibp` |
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
[pymc-challenges](https://github.com/redam94/pymc-challenges). Their data are maps, images and
fields on a regular grid. Their outputs are fields with a range for each cell. Each output that
is a field declares a `grid` (`OutputDecl.grid`: the row dim `y`, the column dim `x`, an
optional `frame` dim and an optional `component` dim for a vector field). The platform then
shows the output in two places:

- The **Fields** section of the Model Explorer. It shows the mean, the sd, mean ÷ sd, the 5% and
  95% maps and single draws, with a frame slider and a draw player.
- The `sci.*` plot recipes (see [Plot recipes](plots.md#scientific-model-pack-sci)).

The runner images of `base-bayes` contain these plugins (namespace `sci`).

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
uses `MAPI_PYMC_CHALLENGES_DATA`. The suffix of `--out` sets the format:

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
| `e50` | `sci.fisher_kpp` | `scratch_assay_pc3.csv` | `biology/scratch_assay_pc3` |
| `e60` | `sci.traction_force` | `pytfm_colony_ko04.npz` | `biophysics/pytfm_colony_ko04` |
| `e80` | `sci.strong_lens` | `slacs_j1627_f814w.npz` | `astronomy/slacs_j1627_f814w` |
| `e31` | `sci.ibp_features` | `optdigits_train.csv`, `optdigits_test.csv` | `vision/optdigits_e31` |

In Python, `export_case("e07", "quakes.nc", data_dir=...)` does the same work, and
`write_export(data, path, schema=...)` writes any dataset or tree. To add a case, add one
`ExportCase` entry to `EXPORTS` (the docstring of the module shows an example).

### `sci.lgcp_grid` — event counts on a map over time (E07)

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

### `sci.fisher_kpp` — a reaction-diffusion PDE (E50)

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

### `sci.ibp_features` — shared parts of small images (E31)

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
prevents this (the notebook does the same). The engines `pymc` and `nutpie` use the start
point with no jitter (`EnginePlan.initvals` and `jitter=False`).

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
predictive density for each digit and the RMSE). With the notebook settings (`K = 8`,
`noise_sd = 0.25`, 4 chains of 1000 draws) a fit on the 300 training digits completes the test
halves with an RMSE of 0.283 and −0.34 nats for each digit. The notebook gives 0.283 and −0.3.
The fit takes approximately 4 minutes with the `pymc` engine.
