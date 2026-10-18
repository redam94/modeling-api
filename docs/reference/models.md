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
| `e81` | `sci.circular_regression` | `asos_ksba_2021_2023.csv.gz` | `meteorology/ksba_wind_2021_2023` |

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

### `sci.circular_regression` — directions and times of day (E81)

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

The priors of `von_mises` and `projected_normal` are N(0, `intercept_sd` = 2) on the
intercepts and N(0, `coef_sd` = 1) on the other coefficients. `regime_mixture` does not use
`intercept_sd`, as in the notebook. The intercept of regime r has the prior
N(`regime_strength` (cos μᵣ, sin μᵣ), 1), with μᵣ from `regime_directions_deg`. The gate
intercept has the prior N(0, `gate_intercept_sd` = 1.5). The other coefficients of the regimes
and of the gate have the prior N(0, `coef_sd`). NUTS samples the coefficients. With `fit.keep.log_likelihood: true`, the bundle keeps the log density of
each row (per radian). Thus `elpd_loo` compares the kinds on the same rows, for example in a
workflow ladder `von_mises` → `projected_normal` → `regime_mixture`.

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

Platform holdout metrics such as `rmse` and `coverage_90` are linear. They are not correct for
angles. Use `elpd_loo`, or `CircularRegressionModel().holdout_log_score(bundle.model, ds)` for
the log score of held-out rows. Errors: `SCHEMA_INVALID` for a missing direction, an hour out of
range or a bad group index; `MISSING_FUTURE_COVARIATE` when the score data have no `hour` or
`day`; `SETTINGS_INVALID` for more interaction harmonics than daily harmonics.
