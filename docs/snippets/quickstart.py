"""Getting started: fit and score a labelled demand model in a notebook.

Every section between ``--8<--`` markers is included verbatim in the docs, and
``conformance/tests/test_docs_snippets.py`` runs this file top to bottom.
"""

# --8<-- [start:data]
import numpy as np
import pandas as pd
import xarray as xr

rng = np.random.default_rng(7)
stores = ["north", "south", "east"]
skus = ["apples", "pears"]
weeks = pd.date_range("2025-01-05", periods=52, freq="W-SUN")
shape = (len(stores), len(skus), len(weeks))

price = 3.0 * np.exp(rng.normal(0.0, 0.1, shape))
promo = (rng.uniform(size=shape) < 0.2).astype(float)
units = rng.poisson(np.exp(2.5 - 1.2 * np.log(price / 3.0) + 0.5 * promo)).astype(float)

dims = ("store", "sku", "week")
data = xr.Dataset(
    {"units_sold": (dims, units), "price": (dims, price), "promo": (dims, promo)},
    coords={"store": stores, "sku": skus, "week": weeks},
)
# --8<-- [end:data]

# --8<-- [start:schema]
from mapi_spec import DataSchema

schema = DataSchema.model_validate(
    {
        "dims": {
            # a store we have never seen may be scored: it gets the pooled prediction
            "store": {"kind": "categorical", "policy": "new_levels", "new_level_strategy": "pooled"},
            # the product range is fixed: an unknown sku is an error
            "sku": {"kind": "categorical", "policy": "fixed"},
            # time extends into the future, one Sunday-ending week at a time
            "week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
        },
        "vars": {
            "units_sold": {"dims": list(dims), "dtype": "float64", "role": "target"},
            "price": {
                "dims": list(dims),
                "dtype": "float64",
                "role": "covariate",
                "units": "EUR",
                "future_required": True,  # a forecast must say what the price will be
            },
            "promo": {
                "dims": list(dims),
                "dtype": "float64",
                "role": "covariate",
                "future_required": True,
            },
        },
    }
)
# --8<-- [end:schema]

# --8<-- [start:spec]
from mapi_spec import load_model_spec

spec = load_model_spec("""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: fruit-demand, project: quickstart}
data:
  dataset: dataset://quickstart/fruit_weekly@latest
  bind:                                   # canonical name: your name
    dims: {store: store, sku: sku, time: week}
    vars: {y: units_sold, price: price, promo: promo}
  split: {kind: time_holdout, dim: time, last: 8}
pipeline:
  - {id: cal,  use: core.calendar@^1, with: {along: time, features: [week_of_year]}}
  - {id: logy, use: core.log1p@^1,    with: {vars: [y]}}
model:
  use: tabular.regressor@^1
  settings:
    estimator: {kind: xgboost.XGBRegressor, params: {n_estimators: 60, max_depth: 3}}
    rows: [store, sku, time]
    coord_features: {store: category, sku: category}
  fit: {seed: 7}
evaluate: {metrics: [rmse, mape, coverage_80]}
""")
# --8<-- [end:spec]

# --8<-- [start:validate]
from mapi_sdk import default_registry
from mapi_spec import validate_spec

registry = default_registry()  # every installed step, model and plot (entry points)
check = validate_spec(spec, registry.catalog(), schema)
print(check.ok)  # True: names, dims, settings, split stage and leakage all check out
print(check.resolved.model.resolved)  # tabular.regressor@1.0.0 (the semver range, pinned)
# --8<-- [end:validate]
assert check.ok, check.errors

# --8<-- [start:fit]
from mapi_sdk import fit_spec

bundle = fit_spec(spec, data, registry=registry, raw_schema=schema)
print(bundle.metrics.values)  # holdout metrics, in units sold: {'rmse': ..., 'mape': ..., ...}
print(bundle.metrics.n_holdout)  # 48 cells: 3 stores x 2 skus x 8 weeks
# --8<-- [end:fit]
assert set(bundle.metrics.values) == {"rmse", "mape", "coverage_80"}
assert bundle.metrics.n_holdout == 48

# --8<-- [start:forecast]
from mapi_sdk import score_bundle

future_weeks = pd.date_range(weeks[-1] + pd.Timedelta(weeks=1), periods=4, freq="W-SUN")
plan = xr.Dataset(
    {
        "price": (dims, np.full((4, 2, 4), 2.9)),
        "promo": (dims, np.zeros((4, 2, 4))),
    },
    # "west" opens next month: the model has never seen it
    coords={"store": [*stores, "west"], "sku": skus, "week": future_weeks},
)

forecast = score_bundle(
    bundle,
    """
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: endpoint://quickstart/fruit-demand
    mode: forecast
    horizon: {dim: time, periods: 4}
    output: {summary: [median, q10, q90]}
    """,
    plan,
    registry=registry,
)
summary = forecast["summary"].to_dataset()
print(summary["y_hat"].dims)  # ('stat', 'store', 'sku', 'week'), in your names
print(summary["y_hat"].sel(stat="median", store="west").values.round(1))
print(forecast.attrs["representation"])  # quantiles: what this model's uncertainty is
print(forecast.attrs["output_space"])  # {"y_hat": "raw"}: log1p undone, back in units sold
print(forecast.attrs["issues"])  # an info notice: 1 new level of 'store' uses 'pooled'
# --8<-- [end:forecast]
assert set(summary["store"].values) == {"north", "south", "east", "west"}
assert summary.sizes["week"] == 4

# --8<-- [start:errors]
from mapi_spec import ErrorCode, MapiError


def try_score(request_yaml: str, payload: xr.Dataset) -> ErrorCode:
    try:
        score_bundle(bundle, request_yaml, payload, registry=registry)
    except MapiError as err:
        print(err.code, "-", err)
        return err.code
    raise AssertionError("expected a typed error")


FORECAST = """
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://quickstart/fruit-demand
mode: forecast
horizon: {dim: time, periods: 4}
"""

# 1. a forecast without next month's promotions: promo is future_required
code_1 = try_score(FORECAST, plan.drop_vars("promo"))

# 2. quantiles are per cell, so "total over all stores" would be a made-up number
code_2 = try_score(FORECAST + "output: {aggregate: {over: [store]}}\n", plan)

# 3. a product outside the fitted range: sku is a fixed dim
code_3 = try_score(FORECAST, plan.assign_coords(sku=["apples", "kiwis"]))
# --8<-- [end:errors]
assert code_1 is ErrorCode.MISSING_FUTURE_COVARIATE
assert code_2 is ErrorCode.CAPABILITY_UNSUPPORTED
assert code_3 is ErrorCode.UNSEEN_LEVELS

# --8<-- [start:static_score]
from mapi_sdk import CoordPlan
from mapi_spec import load_score_request, validate_score_request

# What the API stores next to a model version: JSON only, no arrays, no plugin code.
meta = CoordPlan(load_score_request(FORECAST), bundle.resolved, bundle.fit_coords).metadata()
print(f"{len(meta.model_dump_json()) / 1024:.0f} KiB of metadata")

request = load_score_request(FORECAST + "output: {summary: [hdi_90], draws: 100}\n")
for issue in validate_score_request(request, meta):  # no data, no model: milliseconds
    print(issue.code, issue.pointer, "-", issue.message)
# MISSING_FUTURE_COVARIATE /data - forecasting needs future values of ['price', 'promo'] ...
# CAPABILITY_UNSUPPORTED /output/draws - draws were requested but the model's
#                                        representation is 'quantiles'
# CAPABILITY_UNSUPPORTED /output/summary/0 - summary 'hdi_90' cannot be computed from
#                                            'quantiles' output
# --8<-- [end:static_score]
assert any(
    i.code is ErrorCode.CAPABILITY_UNSUPPORTED for i in validate_score_request(request, meta)
)

# --8<-- [start:bundle]
import tempfile
from pathlib import Path

from mapi_sdk import Bundle

out = Path(tempfile.mkdtemp()) / "fruit-demand-v1"
manifest = bundle.write(out)
print(sorted(p.name for p in out.iterdir()))
# ['bundle.zarr.zip', 'diagnostics.json', 'evaluation', 'manifest.json', 'metrics.json',
#  'plots.json']  - `evaluation/` is the train-only fit that produced the holdout metrics

again = Bundle.read(out, registry)  # every file's size and hashes are verified here
assert again.bundle_id == bundle.bundle_id
# --8<-- [end:bundle]

# --8<-- [start:inspect]
print(manifest.role, manifest.plugin["id"], manifest.spec_hash[:19])
# production tabular.regressor sha256:...
print(manifest.files["bundle.zarr.zip"])  # size, crc32c, md5 (GCS forms) and sha256
print(manifest.lineage["evaluation_bundle"])  # the train-only fit behind the metrics

tree = again.to_tree()  # the whole bundle as one xr.DataTree
print(sorted(tree.children))
# ['binding', 'fit_coords', 'model', 'native', 'preprocess', 'schema', 'spec']
print(sorted(tree["preprocess"].children))  # ['cal', 'logy']: one node per step
# --8<-- [end:inspect]
assert {"model", "preprocess", "native", "spec"} <= set(tree.children)
