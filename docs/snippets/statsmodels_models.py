"""statsmodels plugins: an ARIMAX per geo, a forecast with planned drivers, a VECM's views."""

# --8<-- [start:arimax]
from mapi_plugin_statsmodels.datasets import sales_dataset, sales_schema
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_spec import load_score_request

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: weekly-sales-arimax, project: retail}
data:
  dataset: dataset://retail/weekly_sales@latest
  bind:
    dims: {t: date}            # the model's time dim is called t
    vars: {y: sales}           # ... and its target y
  split: {kind: time_holdout, dim: t, last: 8}
model:
  use: ts.sarimax@^1
  settings:
    order: [1, 0, 0]           # one past value (AR 1), no differencing, no MA term
    exog: [price, promo]       # drivers you know ahead
    series_dims: [geo]         # one model per geo
  fit: {mode: evaluate_then_refit, seed: 1}
evaluate: {metrics: [rmse, mape, coverage_90]}
"""

history = sales_dataset(periods=104, geos=["north", "south"], seed=0)
bundle = fit_spec(SPEC, history, raw_schema=sales_schema(geo=True))
print(bundle.metrics.values)  # holdout metrics of the evaluation fit
params = bundle.model.groups["params"]
print(params["estimate"].sel(param=["price", "promo"]).round(2).values)
# [[-7.96 11.62] [-7.32 12.18]] - per geo, close to the true -8 and +12
# --8<-- [end:arimax]

# --8<-- [start:forecast]
plan = sales_dataset(periods=112, geos=["north", "south"], seed=0).isel(date=slice(104, None))
request = load_score_request("""
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://retail/weekly-sales-arimax
mode: forecast
horizon: {dim: t, periods: 8}
output: {summary: [median, q5, q95]}
""")
forecast = score_bundle(bundle, request, plan.drop_vars("sales"))
print(forecast["summary"].to_dataset()["y_hat"].sel(geo="north").round(1).values)
# rows median, q5, q95 - the 90% prediction interval around the median
# --8<-- [end:forecast]

# --8<-- [start:actuals]
import json

# Weeks 105-108 arrived for north, 105-106 for south: send them with the weeks to forecast
# (sales left empty) and the planned drivers. Nothing is refitted.
latest = sales_dataset(periods=112, geos=["north", "south"], seed=0).isel(date=slice(104, None))
weeks = latest["date"].values
latest["sales"].loc[{"geo": "north", "date": weeks[4:]}] = float("nan")
latest["sales"].loc[{"geo": "south", "date": weeks[2:]}] = float("nan")
updated = score_bundle(bundle, request, latest)
print(json.loads(updated.attrs["conditioning"])["message"])
# Forecast updated with 6 actuals (2024-12-29 to 2025-01-19) across 2 of 2 series after the
# fitted history, which ends 2024-12-22; the model's parameters were not re-estimated. ...
north = updated["summary"].to_dataset()["y_hat"].sel(geo="north")
print(north.sel(date=weeks[4:]).round(1).values)  # the forecast from north's last actual
# --8<-- [end:actuals]

# --8<-- [start:vecm]
from mapi_plugin_statsmodels.datasets import MACRO_SCHEMA, macro_dataset
from mapi_plugin_statsmodels.views import explorer_views

VECM = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: us-macro-vecm, project: macro}
data:
  dataset: dataset://macro/us_quarterly@latest
  bind: {dims: {t: quarter, variable: series}, vars: {y: value}}
model:
  use: ts.vecm@^1
  settings: {k_ar_diff: 2, deterministic: ci}   # rank from the Johansen test
"""
vecm = fit_spec(VECM, macro_dataset(quarters=192), raw_schema=MACRO_SCHEMA)
print(int(vecm.model.groups["cointegration"]["rank"]))  # 1 long-run relationship
views = explorer_views(vecm.model)  # Model Explorer sections, as spec DTOs
print(sorted(views))  # ['diagnostics', 'fevd', 'irf', 'params', 'residuals']
# --8<-- [end:vecm]

assert bundle.metrics.values["coverage_90"] is not None
assert abs(float(params["estimate"].sel(param="price").mean()) + 8) < 1.5
assert forecast["summary"].to_dataset()["y_hat"].sizes["date"] == 8
assert int(vecm.model.groups["cointegration"]["rank"]) == 1
assert {"irf", "fevd", "params"} <= set(views)
