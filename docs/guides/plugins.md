# Write a step or a model plugin

A plugin author writes very little code. A step implements `fit`/`transform` (and `inverse` if
it can). A model implements `fit`/`score`. The platform owns all other work: binding, splits,
context tails, coordinates, inverse transforms, summaries and bundles. The **contract kit** tells
you if you obeyed the rules. It tells you before other persons run your code.

## A preprocessing step

```python
--8<-- "plugin_custom.py:step"
```

The rules:

* `transform` never learns. `transform(train, fit(train))` must be equal to the output at fit
  time. A subset of the data must transform to the same subset of the output.
* A reduction names the dims that it reduces **over**. The rest of the dims stay as the labels
  of the state.
* Steps that look back (lags, rolling windows) declare `context_template`
  (`{"along": "{along}", "n": "{max:lags}", "within": "{within}"}`). The platform keeps the last
  `n` rows for each group at the input of the step. At score time, it puts them before the data.
* Unseen levels go through `ctx.align_state(state, ds, policy)`.
* `effect_template` must describe the real schema of the output. The control plane uses it to
  validate specs. The control plane never imports your code.

## A model

```python
--8<-- "plugin_custom.py:model"
```

`outputs` lists each variable that `score` returns, with its dims. You can omit representation
dims, for example `chain`/`draw` or `quantile`. If the data dims change with the settings, add
`"dims_of": "y"`. The output then also gets each dim of that input variable in the resolved
schema (for example `settings.rows` of the sklearn models).

If the NaN of an input are part of the design of the data, list its canonical name in the class
attribute `structural_missing`. An example is a grid of cells where only some cells have a
sensor. The scorecard check `data.missing` then does not count these NaN. Do not use it for
gaps in observed series: the check must show them.

`FitOutput.native` is only for safe native formats (skops, xgboost JSON, safetensors). The
platform rejects pickle when it writes a bundle and when it reads a bundle.

### Settings rules that need no data

Put the rules that join two or more settings in a pydantic `model_validator` of your
`ModelSettings` class. An example is "set `offset_adu` and `scale_adu` together" in
`sci.storm_localisation`. The SDK parses the settings before it transforms, splits or fits the
data. Thus a bad value stops the fit at the start with `SETTINGS_INVALID`
(`/model/settings`). Do not put such rules in `fit`: there, they fail only after the data
steps. Note: spec validation does a check of the JSON Schema only. It does not run the
validator.

### Outputs that are images or maps

If an output is a 2-D field (an image, a map or a vector field), give it a `grid`
(`OutputDecl.grid`):

```python
outputs = {
    "sky": {
        "dims": ["row", "col"],
        "units": "uK",
        "grid": {"y": "row", "x": "col", "origin": "lower", "colormap": "diverging"},
    },
}
```

| Field | What it holds |
|---|---|
| `y`, `x` | The dims of the rows and of the columns of the raster |
| `frame` | Optional. A third dim, with one raster for each level (for example years) |
| `component` | Optional. A dim of length two with the `(x, y)` parts of a vector field |
| `origin` | `upper` (default: row 0 at the top) or `lower` |
| `colormap` | `viridis` (default), `magma`, `cividis`, `gray` or `diverging` |
| `log` | `true` draws log10 of the value (for counts and intensities). The default is `false`. |

The dims in `grid` must be different from each other. The Model Explorer shows the output in
its **Fields** section. The `sci.*` image recipes (`sci.field_map`, `sci.image_residuals`,
`sci.ppc_image_grid`) find the grid in the manifest, or in a `grid` attribute of the scored
variable. When the binding of a spec renames dims, the platform also renames them in the
`grid` attribute.

### Outputs that are angles

If an output is an angle (a direction or a time of day), set `circular: true` in its
declaration. Then, in `score`, set the attribute `period` on the array. The period is one
full turn in the units of the output, for example `360` (degrees), `2π` (radians) or `24`
(hours):

```python
outputs = {"direction_hat": {"dims": ["obs"], "units_of": "direction", "circular": True}}
# in score(): the angles in [0, 360)
pred["direction_hat"] = xr.DataArray(
    theta, dims=("chain", "draw", "obs"), attrs={"kind": "predictive", "period": 360.0}
)
```

The platform then calculates on the circle:

- The holdout metrics use the circular mean of the draws and errors that wrap at half a turn
  (359 against 1 is an error of -2). `crps` uses the arc distance. The intervals are centred
  on the circular mean. The metrics `mape`, `smape`, `wape` and `r2` are skipped.
- The score summaries of draws use the circular mean and the circular standard deviation.
  Each value is in `[0, period)`. An interval with `lower` more than `upper` crosses the seam.
  The summary table (`summary.json`) has the field `period`.
- The difference between a scenario and its baseline wraps to `[-period/2, period/2)`.

The circular metrics need the `draws` representation. An output without a valid `period`
attribute, or an output aggregated over a dim, keeps the summary on a line, and the result has
a warning.

### Score modes

Declare in `capabilities.score_modes` only the modes that `score` can do. The SDK refuses
each other mode with `MODE_NOT_SUPPORTED` before it builds a forecast horizon or a payload.
Thus `score` never gets a mode that you did not declare. Read the mode in `ctx.mode` and the
output kind in `ctx.kind` (`expected` or `predictive`).

In `simulate` mode, return new data that the model makes from its posterior: new latent values
and the noise of the model. For example, `sci.cmb_field` draws a new patch of sky from the
spectrum of each posterior draw, and then adds noise. In `predict` mode with
`kind: predictive`, keep the latent values of each posterior draw and add only the noise. Set
the attribute `kind: predictive` on such draws. The governance check `recovery.simulated`
makes its simulated datasets from these draws.

### Models that give `elpd_loo`

The fit calculates `elpd_loo` and `elpd_waic` from a `log_likelihood` group, if the spec
lists the metric in `evaluate.metrics`. Put the group in `FitOutput.groups`, next to
`posterior`, with the same `chain` and `draw` dims. Usually, keep it when
`cfg.keep.log_likelihood` is true. A model for comparisons can always keep it, as
`sci.areal_bym2` does. For data of up to 10,000 points, the fit also keeps the
pointwise values (`MetricsReport.elpd`), and the workflow node `compare.stacking` uses them.

If pointwise LOO is not correct for your model, declare `loo_valid=False` and a
`loo_invalid_reason` in `Capabilities`. An example is a model where all pixels depend on one
latent field. The fit then skips the metric with that reason.

### A model with its own sampler

A `CustomModel` can run its own MCMC sampler, for example a Gibbs sampler. `sci.cmb_field`
does this. Follow these rules:

- Declare `inference="mcmc"` and `representation=Representation.DRAWS`.
- Read `cfg.tune`, `cfg.draws`, `cfg.chains` and `cfg.seed` (use `ctx.seed` when `cfg.seed`
  is `None`). If the spec names an `engine` that your sampler does not use, tell the user with
  `ctx.info`.
- Call `ctx.check_cancelled()` and `ctx.progress(fraction, message)` in the loop of the
  sampler.
- Return `posterior` and `sample_stats` with the dims `chain` and `draw`, and also
  `observed_data`.
- Override the `diagnostics(fitted, ds)` hook. Call
  `mapi_pymc.convergence_diagnostics(posterior, sample_stats, var_names=[...])` for R-hat
  and ESS, and add the diagnostics of your sampler. `sci.cmb_field` adds `CG_NOT_CONVERGED`.
- Give each draw of a score its own generator, seeded from the seed of the score, the chain
  and the draw. Then declare `deterministic_given_seed=True`.
- From v0.11, report the progress of each chain with `ctx.chains([...])` (a list of
  `mapi_spec.run.ChainProgress`). Log the step metrics of your sampler with the label
  `chain`, for example `ctx.log_metrics({"step_size": eps}, step=i, labels={"chain": str(c)})`.
  Do this only when `ctx.tracking` is true.

### Log metrics, params and tags <span class="mapi-badge">v0.11</span> { #tracking }

`FitContext` and `ScoreContext` have logging methods. The run page shows the values, and the
fit keeps the stepped metrics in the bundle group `metrics_history`. Thus `core.loss_curve`
can draw them.

```python
def fit(self, data, ctx):
    ctx.log_params({"solver": "lbfgs", "max_iter": 200})
    for i in range(1, n_iter + 1):
        loss = step()
        if i % 10 == 0:
            ctx.log_metric("loss", loss, step=i)
    ctx.log_metric("rmse", rmse, labels={"split": "train"})   # a scalar: no step
    ctx.set_tag("converged", "yes" if ok else "no")
    if not ok:
        ctx.log_line("the solver did not converge", level="warning", fields={"iterations": n_iter})
```

Follow these rules:

- Metric keys match `^[a-z][a-z0-9_]{0,63}$`. Put qualifiers in labels: write `loss` with
  `labels={"split": "val"}`, not `val/loss`.
- A bad key, a bad label or a negative step raises `ValueError` at the call, also without a
  run. The SDK ignores NaN and infinity.
- A param does not change. A second, different value for the same key raises `ValueError`.
- The methods are thread-safe. You can call them from the callback of a sampler.
- Use `ctx.tracking` to skip metrics that take a long time to calculate.
- The fit already logs the params of the spec (`model`, `fit.*`, `settings.*`). Do not log
  them again.

To see the values in a test, give the fit a recorder:
`fit_spec(spec, data, hooks=FitHooks(tracker=MetricRecorder()))`. The recorder keeps the
points, the params, the tags and the log lines (`mapi_sdk.MetricRecorder`). See
[Track metrics, params and tags](tracking.md) for the full API.

### Data for a case study (`mapi-plugin-science`)

If you add a model to `mapi-plugin-science`, add an export case so that users can upload its
data. `mapi_plugin_science.export.EXPORTS` maps a case id to an `ExportCase`. The case id is
`e` and the notebook number (`e18`), with a letter for a second dataset of the same notebook
(`e81b`). Add one `_register(ExportCase(...))` entry at the end of `export.py`:

```python
_register(
    ExportCase(
        case="e18",
        title="Disease mapping of lip cancer in Scotland",
        notebook="E18_areal_models_icar_bym2.py",
        model="sci.areal_bym2",
        files=(areal_data.CHALLENGES_FILE,),
        loader=lambda d: areal_data.from_challenges(d / areal_data.CHALLENGES_FILE),
        synthetic=lambda: areal_data.areal_dataset(),
        schema=areal_data.AREAL_SCHEMA,
        project="epidemiology",
        dataset="scotland_lip_cancer",
        spec=areal_data.SCOTLAND_SPEC_YAML,
    )
)
```

- `files` lists the files that the loader reads from the `data/` directory of
  pymc-challenges. Use an empty tuple when the notebook simulates its data (E51, E61).
- `synthetic` returns the simulated data of the model, in the same layout.
- `schema` is the raw `DataSchema` of the model (a `TreeSchema` for a tree dataset).
- `spec` is the suggested spec as YAML, or a function of the exported data that returns it.
  Its `data.dataset` must be `dataset://<project>/<dataset>@latest`.

The tests make sure that each suggested spec validates against the schema of its exported
file. A slow test fits each spec on the simulated data. Then
`python -m mapi_plugin_science.export --list` shows the case.

## Run the contract kit

`mapi-sdk` installs a pytest plugin. Thus the fixtures are available in all test suites:

```python
--8<-- "test_my_plugin.py:pytest"
```

`check_step` does a check of these items:

- The exported catalog entry
- A deterministic fit
- A pure and deterministic `transform`
- That transform never learns
- The bundle round trip of the state
- That the declared schema effect agrees with the real output
- That the step accepts a missing target
- The inverse round trip
- The equivalence of the context window
- Each policy for unseen levels

`check_model` fits the fixture and does a check of these items:

- The bundle round trip, and that the bundle has no pickle
- Scores with shuffled data and with a subset of the data
- The policies for new levels
- The typed errors of each dim policy:
    - Gaps and coordinates out of range on datetime and integer extend dims
    - `HORIZON_EXCEEDS_FIT` after `max_extension`
    - `MISSING_FUTURE_COVARIATE` for a forecast without a variable that the future needs
    - `RECURSIVE_UNSUPPORTED` for a horizon that is longer than a target lag
- Modes that the model does not support
- The equivalence of the context window
- That the score path makes the same evaluation metrics again
- The output units (also outputs that get the units of the target)
- That `outputs` declares each returned prediction with the same dims
- Legal aggregation
- Additive contributions
- Seed determinism
- That the `plots` of the manifest render and pass the sanitizer
- If the manifest declares `fast_score`: that fast scores agree with exact scores within MCSE

The last check calls the `check_fast_score(fitted, ds, *, seed)` hook of the plugin.
`PyMCModel` gives this hook.

If a check fails, it raises `ContractViolation` with a report that is easy to read:

```text
contract kit: step acme.scale@1.0.0
  [PASS] catalog_entry: exported acme.scale@1.0.0
  [FAIL] transform_never_learns: AssertionError: ...
```

`mapi_sdk.testing.toy` contains small, real reference implementations that you can copy. It has
the scale, log1p, lag, resample, Fourier and FX steps, and the point, draws and quantile models.
