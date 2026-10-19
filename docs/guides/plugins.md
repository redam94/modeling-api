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
