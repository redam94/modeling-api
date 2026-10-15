"""PyMC guide: compile a FrozenPredictor for fast what-if grids, and check it."""

# --8<-- [start:fast]
import numpy as np
import xarray as xr
from mapi_pymc.examples import RADON_SCHEMA, RADON_SPEC_YAML, RadonModel, radon_dataset
from mapi_pymc.testing import check_fast_score
from mapi_sdk import Registry, fit_spec

registry = Registry()
registry.register(RadonModel)  # declares fast_score_spec = FrozenPredictorSpec(
#                                  response_exprs={"y": "mu"}, input_vars=["floor"])
data = radon_dataset()
bundle = fit_spec(RADON_SPEC_YAML, data, registry=registry, raw_schema=RADON_SCHEMA)

model = RadonModel()
predictor = model.frozen_predictor(bundle.model, data, num_samples=200, seed=1)
floors = xr.DataArray(
    np.outer([0.0, 0.5, 1.0], np.ones(data.sizes["obs"])),
    dims=["scenario", "obs"],
    coords={"scenario": ["basement", "mixed", "ground floor"]},
)
grid = predictor.predict({"floor": floors})  # one compiled call for every scenario
print(grid["y"].dims)  # ('sample', 'scenario', 'obs')

report = check_fast_score(model, bundle.model, data)  # fast path == exact path within MCSE
print(report.ok)  # True
# --8<-- [end:fast]
assert grid["y"].dims == ("sample", "scenario", "obs") and report.ok
