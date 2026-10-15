"""Plugin authoring: port a PyMC model by writing build() only."""

# --8<-- [start:model]
from typing import Any, ClassVar

import pymc as pm
import xarray as xr
from mapi_pymc import BuildContext, PyMCModel
from mapi_pymc.examples import RADON_SCHEMA  # dims obs (extend) + county (new_levels)
from mapi_sdk import ModelSettings
from mapi_spec import Capabilities, Representation
from pydantic import Field


class RadonSettings(ModelSettings):
    beta_sigma: float = Field(1.0, gt=0)


class Radon(PyMCModel[RadonSettings]):
    """log radon ~ Normal(a[county] + b * floor, sigma), with partially pooled a."""

    id = "acme.radon"
    version = "1.0.0"
    Settings = RadonSettings
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "counterfactual", "evaluate"}),
    )
    schema_template = RADON_SCHEMA
    outputs: ClassVar[dict[str, Any]] = {"y": {"dims": ["obs"], "units_of": "y"}}
    expected: ClassVar[dict[str, str]] = {"y": "mu"}  # output -> Deterministic for E[y]
    pad_extend = False  # long layout: no positional time axis

    def build(self, ds: xr.Dataset, s: RadonSettings, ctx: BuildContext) -> pm.Model:
        with pm.Model(coords=ctx.coords()) as model:
            floor = ctx.data("floor")
            county = ctx.data("county_idx")
            a = ctx.hier_effect("a", "county")  # new counties come from the hyperprior
            b = pm.Normal("b", 0.0, s.beta_sigma)
            mu = pm.Deterministic("mu", a[county] + b * floor, dims="obs")
            sigma = pm.HalfNormal("sigma", 1.0)
            pm.Normal("y", mu, sigma, observed=ctx.observed("y"), dims="obs")
        return model
# --8<-- [end:model]

# --8<-- [start:check]
from mapi_pymc.examples import radon_dataset
from mapi_sdk import Registry
from mapi_sdk.testing import ModelFixture, check_model

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: radon, project: demo}
data: {dataset: dataset://demo/radon@latest}
model:
  use: acme.radon@^1
  fit: {mode: train_only, engine: pymc, tune: 100, draws: 100, chains: 2, seed: 3}
"""
report = check_model(
    Radon, ModelFixture(spec=SPEC, data=radon_dataset(), raw_schema=RADON_SCHEMA, registry=Registry())
)
print(report.summary())
# --8<-- [end:check]
assert report.ok
