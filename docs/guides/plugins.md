# Write a step or a model plugin

A plugin author writes very little: a step implements `fit`/`transform` (and `inverse` when it
can); a model implements `fit`/`score`. The platform owns everything around them — binding,
splits, context tails, coordinates, inverse transforms, summaries and bundles. The **contract
kit** tells you, before anyone else runs your code, whether you followed the rules.

## A preprocessing step

```python
from typing import ClassVar
import xarray as xr
from pydantic import Field, JsonValue
from mapi_sdk import Step, StepContext, StepSettings, StepState
from mapi_spec import StepKind


class ScaleSettings(StepSettings):
    vars: list[str]
    over: list[str] = Field(default=[])     # plain defaults (not default_factory): the
    unseen_levels: str = "error"            # exported JSON Schema must carry them


class ScaleStep(Step[ScaleSettings, StepState]):
    id = "acme.scale"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = ScaleSettings
    State = StepState                        # stateful: learned on train only
    invertible = True
    var_params = ("vars",)                   # settings that name variables
    dim_params = ("over",)                   # settings that name dims
    unseen_levels_param = "unseen_levels"
    effect_template: ClassVar[dict[str, JsonValue]] = {     # static, checked by the API
        "template_params": ["vars"],
        "updates": {"{*vars as v}": {"units": "1"}},
    }

    def fit(self, ds, s, ctx):
        scales = {f"scale__{v}": abs(ds[v]).mean(s.over) for v in s.vars}
        return StepState(arrays=xr.Dataset(scales))

    def transform(self, ds, s, st, ctx):
        # never learn here; skip variables that are absent at score time (e.g. the target)
        return ds.assign({
            v: ds[v] / ctx.align_state(st.array(f"scale__{v}"), ds, s.unseen_levels)
            for v in s.vars if v in ds
        })

    def inverse(self, ds, s, st, ctx):
        return ds.assign({
            v: ds[v] * ctx.align_state(st.array(f"scale__{v}"), ds, s.unseen_levels)
            for v in s.vars if v in ds
        })
```

The rules:

* `transform` never learns. `transform(train, fit(train))` must equal the fit-time output, and a
  subset of the data must transform to the same subset of the output.
* Reductions name the dims they reduce **over**; state stays labelled by the rest.
* Steps that look back (lags, rolling windows) declare `context_template`
  (`{"along": "{along}", "n": "{max:lags}", "within": "{within}"}`). The platform stores the
  last `n` rows per group at the step's input and prepends them at score time.
* Unseen levels go through `ctx.align_state(state, ds, policy)`.
* `effect_template` must describe the real output schema — the control plane validates specs
  with it and never imports your code.

## A model

```python
import xarray as xr
from mapi_sdk import CustomModel, FitOutput, ModelSettings, PREDICTIONS
from mapi_spec import Capabilities, Representation


class MyModel(CustomModel[ModelSettings]):
    id = "acme.my_model"
    version = "1.0.0"
    capabilities = Capabilities(
        inference="point",
        representation=Representation.POINT,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
    )
    schema_template = {...}                      # the input DataSchema (may use settings)
    outputs = {"y_hat": {"dims": ["geo", "time"], "units_of": "y"}}

    def fit(self, ds, s, cfg, ctx):
        ...
        return FitOutput(groups={"params": params})

    def score(self, fitted, ds, req, ctx):
        # `ds` is canonicalised: fit order, new levels flagged by `<dim>__is_new`
        ...
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": y_hat})})
```

`outputs` lists every variable `score` returns with its dims (representation dims such as
`chain`/`draw` or `quantile` may be left out). When the data dims depend on settings, add
`"dims_of": "y"`: the output then also carries every dim of that input variable in the
resolved schema (for example the sklearn models' `settings.rows`).

`FitOutput.native` is for safe native formats only (skops, xgboost JSON, safetensors). Pickle
is rejected when a bundle is written and when it is read.

## Run the contract kit

`mapi-sdk` installs a pytest plugin, so the fixtures are available in any test suite:

```python
from mapi_sdk.testing import ModelFixture


def test_scale_step(mapi_check_step, my_dataset):
    mapi_check_step(ScaleStep, my_dataset, {"vars": ["spend"], "over": ["time"]})


def test_my_model(mapi_check_model, my_spec, my_data):
    mapi_check_model(MyModel, ModelFixture(spec=my_spec, data=my_data))
```

`check_step` checks the exported catalog entry, deterministic fitting, a pure and deterministic
`transform`, that transform never learns, the state's bundle round trip, that the declared
schema effect matches the real output, that a missing target is tolerated, the inverse round
trip, context-window equivalence and every unseen-level policy.

`check_model` fits the fixture and checks the bundle round trip (and that it has no pickle),
shuffled and subset scoring, new-level policies, the typed errors of every dim policy (gaps and
out-of-range coordinates on datetime and integer extend dims, `HORIZON_EXCEEDS_FIT` past
`max_extension`, `MISSING_FUTURE_COVARIATE` for a forecast without a future-required variable,
`RECURSIVE_UNSUPPORTED` for a horizon longer than a target lag), unsupported modes,
context-window equivalence, that the evaluation metrics are reproduced by the score path,
output units (including outputs that inherit the target's units), that every returned
prediction is declared in `outputs` with matching dims, legal aggregation,
additive contributions, seed determinism, that the manifest's `plots` render and pass the
sanitizer, and — when the manifest declares `fast_score` — that fast scoring matches exact
scoring within MCSE. That last check calls the plugin's
`check_fast_score(fitted, ds, *, seed)` hook; `PyMCModel` provides it.

A failed check raises `ContractViolation` with a readable report:

```text
contract kit: step acme.scale@1.0.0
  [PASS] catalog_entry: exported acme.scale@1.0.0
  [FAIL] transform_never_learns: AssertionError: ...
```

`mapi_sdk.testing.toy` holds small, real reference implementations (scale, log1p, lag,
resample, Fourier, FX steps; point, draws and quantile models) to copy from.
