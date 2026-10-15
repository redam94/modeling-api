"""Plugin authoring: a step and a CustomModel, checked by the contract kit."""

# --8<-- [start:data]
import numpy as np
import pandas as pd
import xarray as xr

rng = np.random.default_rng(3)
geos = ["north", "south", "east", "west"]
time = pd.date_range("2025-01-05", periods=40, freq="W-SUN")
spend = rng.gamma(4.0, 25.0, (4, 40))
sales = 200.0 + 40.0 * np.arange(4)[:, None] + 1.5 * spend + rng.normal(0, 10, (4, 40))
data = xr.Dataset(
    {"y": (("geo", "time"), sales), "spend": (("geo", "time"), spend)},
    coords={"geo": geos, "time": time},
)
# --8<-- [end:data]

# --8<-- [start:step]
from typing import ClassVar

from mapi_sdk import Step, StepContext, StepSettings, StepState
from mapi_spec import StepKind
from pydantic import Field, JsonValue


class ScaleSettings(StepSettings):
    vars: list[str]
    over: list[str] = Field(default=[])  # plain defaults: the exported JSON Schema keeps them
    unseen_levels: str = "error"  # error | pooled | fill: what a new level's scale becomes
    fill_value: float = 1.0  # the scale used by 'fill'


class ScaleStep(Step[ScaleSettings, StepState]):
    """Divide each variable by its mean absolute value over `over` (learned on train)."""

    id = "acme.scale"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = ScaleSettings
    State = StepState  # stateful: fitted on the training data only
    invertible = True
    var_params = ("vars",)  # settings that name variables ...
    dim_params = ("over",)  # ... and dims, so the control plane can check them
    unseen_levels_param = "unseen_levels"
    effect_template: ClassVar[dict[str, JsonValue]] = {  # static: how the schema changes
        "template_params": ["vars"],
        "updates": {"{*vars as v}": {"units": "1"}},
    }
    summary = "Divide by the mean absolute value over the named dims"  # catalog and UI text
    summary_plain = "Puts each series on a comparable scale"

    def fit(self, ds: xr.Dataset, s: ScaleSettings, ctx: StepContext) -> StepState:
        return StepState(arrays=xr.Dataset({f"scale__{v}": abs(ds[v]).mean(s.over) for v in s.vars}))

    def _scale(self, v: str, ds: xr.Dataset, s: ScaleSettings, st: StepState, ctx: StepContext):
        state = st.array(f"scale__{v}")
        return ctx.align_state(state, ds, s.unseen_levels, fill_value=s.fill_value)

    def transform(self, ds, s, st, ctx):  # never learns; skips absent vars (the target at score)
        return ds.assign({v: ds[v] / self._scale(v, ds, s, st, ctx) for v in s.vars if v in ds})

    def inverse(self, ds, s, st, ctx):
        return ds.assign({v: ds[v] * self._scale(v, ds, s, st, ctx) for v in s.vars if v in ds})
# --8<-- [end:step]

# --8<-- [start:check_step]
from mapi_sdk import infer_schema
from mapi_sdk.testing import check_step

report = check_step(
    ScaleStep,
    data,
    {"vars": ["spend"], "over": ["time"], "unseen_levels": "pooled"},
    schema=infer_schema(data, targets=["y"]),
)
print(report.summary())
# contract kit: step acme.scale@1.0.0
#   [PASS] catalog_entry: exported acme.scale@1.0.0
#   [PASS] transform_never_learns: ...
#   ...
# --8<-- [end:check_step]
assert report.ok

# --8<-- [start:model]
from mapi_sdk import PREDICTIONS, CustomModel, FitOutput, ModelSettings, new_level_mask
from mapi_spec import Capabilities, DataSchema, Representation


class BaselineSettings(ModelSettings):
    ridge: float = 1e-6


class GeoBaseline(CustomModel[BaselineSettings]):
    """y = intercept[geo] + beta * spend, by least squares (point predictions)."""

    id = "acme.geo_baseline"
    version = "1.0.0"
    Settings = BaselineSettings
    capabilities = Capabilities(
        inference="point",
        representation=Representation.POINT,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
    )
    # the data contract the model needs, in canonical names (a DataSchema, or a
    # DataSchemaTemplate whose names come from settings)
    schema_template = DataSchema.model_validate(
        {
            "dims": {
                "geo": {"kind": "categorical", "policy": "new_levels", "new_level_strategy": "pooled"},
                "time": {"kind": "time", "policy": "extend"},
            },
            "vars": {
                "y": {"dims": ["geo", "time"], "dtype": "float64", "role": "target"},
                "spend": {"dims": ["geo", "time"], "dtype": "float64", "role": "covariate"},
            },
        }
    )
    outputs: ClassVar[dict] = {"y_hat": {"dims": ["geo", "time"], "units_of": "y"}}
    pad_extend = False  # time is not indexed by position

    def fit(self, ds, s, cfg, ctx):
        g, t = ds.sizes["geo"], ds.sizes["time"]
        x = np.concatenate(
            [np.repeat(np.eye(g), t, axis=0), ds["spend"].values.reshape(-1, 1)], axis=1
        )
        coef = np.linalg.solve(x.T @ x + s.ridge * np.eye(g + 1), x.T @ ds["y"].values.ravel())
        params = xr.Dataset(
            {"intercept": ("geo", coef[:g]), "beta": coef[g]}, coords={"geo": ds["geo"].values}
        )
        return FitOutput(groups={"params": params})

    def score(self, fitted, ds, req, ctx):
        # `ds` arrives canonicalised: fit order, unseen geos appended and flagged
        p = fitted.group("params")
        new = new_level_mask(ds, "geo")
        known = p["intercept"].reindex(geo=ds["geo"].values)
        pooled = 0.0 if ctx.strategy("geo") == "zero" else float(p["intercept"].mean())
        intercept = xr.where(new, pooled, known.fillna(pooled))
        y_hat = (intercept + p["beta"] * ds["spend"]).transpose("geo", "time")
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": y_hat})})
# --8<-- [end:model]

# --8<-- [start:check_model]
from mapi_sdk import default_registry
from mapi_sdk.testing import ModelFixture, check_model

registry = default_registry().copy()
registry.register(ScaleStep)  # the model is registered by check_model itself

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: acme-baseline, project: demo}
data:
  dataset: dataset://demo/sales@latest
  split: {kind: time_holdout, dim: time, last: 6}
pipeline:
  - {id: scale, use: acme.scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
model:
  use: acme.geo_baseline@^1
  fit: {seed: 1}
evaluate: {metrics: [rmse, mape]}
"""
raw_schema = infer_schema(data, targets=["y"], policies={"geo": "new_levels"})
report = check_model(
    GeoBaseline,
    ModelFixture(spec=SPEC, data=data, raw_schema=raw_schema, registry=registry),
)
print(report.summary())
# contract kit: model acme.geo_baseline@1.0.0
#   [PASS] fit: ...
#   [PASS] bundle_roundtrip_no_pickle: ...
#   [PASS] shuffled_scoring: ...
#   ...
# --8<-- [end:check_model]
assert report.ok
