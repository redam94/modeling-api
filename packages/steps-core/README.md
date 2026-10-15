# mapi-steps

The built-in preprocessing steps for modeling-api (`core.*`), plus the static catalog the
control plane validates specs against.

* **28 steps**, each implementing the `mapi_sdk.steps.Step` contract:
  * reshape: `resample`, `count_events`, `filter_coords`, `to_long`, `to_padded`, `stack`,
    `unstack`;
  * value: `fx_convert`, `mean_scale`, `max_abs_scale`, `standardize`, `log1p`, `logit`,
    `impute`, `replace_sentinel`, `clip`;
  * feature: `fourier`, `trend`, `calendar`, `holidays`, `lag`, `lag_tensor`, `one_hot`,
    `encode_levels`, `missing_indicator`, `formula`;
  * precompute: `adstock`, `saturation`.
* **Split catalog** (`mapi_steps.splits`) for the five `data.split` kinds: `time_holdout`,
  `as_of`, `leave_group_out`, `last_per_group` and `mask_block`.
* **Static catalog**: `python -m mapi_steps.catalog [--splits]` prints a `mapi_spec.Catalog`
  JSON with every step's settings JSON Schema, templated `SchemaEffect` and context need. It
  also ships as `mapi_steps/catalog.json`, and a test fails if that file drifts from the code.

```python
import mapi_steps
from mapi_sdk.pipeline import Pipeline

reg = mapi_steps.registry()  # or mapi_sdk.default_registry() via entry points
pipe = Pipeline.build(
    [
        ("weekly", mapi_steps.ResampleStep, {"dim": "time", "freq": "W-SUN"}),
        ("scale", mapi_steps.MeanScaleStep, {"vars": ["spend"], "over": ["time"]}),
        ("lags", mapi_steps.LagTensorStep, {"vars": ["spend"], "within": ["geo"], "l_max": 6}),
    ]
)
trace = pipe.fit(daily)  # states + per-step context tails
replay = pipe.transform(next_weeks, trace.states)  # never refits; prepends context
```

Every step passes the SDK contract kit (`mapi_sdk.testing.check_step`). Run the tests with
`uv run pytest packages/steps-core`. The user guide is at `docs/reference/steps.md`.

`core.formula` parses its expression with a Lark grammar. The grammar compiles to a small,
allow-listed AST of xarray operations. It makes specs easier to write but is **not** a
security boundary; formulas only run inside the sandboxed runner.

License: Apache License 2.0.
