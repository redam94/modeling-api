"""One minimal spec per statsmodels model, fitted on small public or synthetic data, with a
forecast from planned drivers where the model takes drivers.

Included section by section by ``docs/models/statsmodels.md``; ``make docs-test`` runs the
whole file (every model fits in a second or two).
"""

from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")  # statsmodels' convergence chatter on tiny examples

# --8<-- [start:common]
from mapi_plugin_statsmodels.datasets import (
    ECM_SCHEMA,
    GROWTH_SCHEMA,
    MACRO_SCHEMA,
    REGIME_SCHEMA,
    ecm_dataset,
    macro_dataset,
    macro_growth,
    regime_dataset,
    sales_dataset,
    sales_schema,
)
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_spec import load_score_request


def spec(model: str, bind: str = "{dims: {t: date}, vars: {y: sales}}") -> str:
    """A complete ModelSpec around a ``model:`` block: the data binding and a holdout of the
    last 8 periods, scored before the model is refitted on everything."""
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: example, project: growth}}
data:
  dataset: dataset://growth/example@latest
  bind: {bind}
  split: {{kind: time_holdout, dim: t, last: 8}}
model:
{model}
  fit: {{mode: evaluate_then_refit, seed: 1}}
evaluate: {{metrics: [rmse, mape, coverage_90]}}
"""


def forecast(bundle, future, periods: int = 8):  # type: ignore[no-untyped-def]
    """Forecast ``periods`` ahead from planned drivers (the future rows of ``future``)."""
    request = load_score_request(f"""
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://growth/example
mode: forecast
horizon: {{dim: t, periods: {periods}}}
output: {{summary: [median, q5, q95]}}
""")
    return score_bundle(bundle, request, future)["summary"].to_dataset()


weekly = sales_dataset(periods=112, seed=0)  # sales(date) with price and promo
history, plan = weekly.isel(date=slice(0, 104)), weekly.isel(date=slice(104, None))
panel = sales_dataset(periods=104, geos=["north", "south"], seed=1)  # sales(geo, date)
# --8<-- [end:common]

# --8<-- [start:sarimax]
SARIMAX = """
  use: ts.sarimax@^1
  settings:
    order: [1, 0, 0]          # AR(1) errors, no differencing, no MA term
    exog: [price, promo]      # drivers you know ahead
"""
sarimax = fit_spec(spec(SARIMAX), history, raw_schema=sales_schema())
fan = forecast(sarimax, plan.drop_vars("sales"))  # next 8 weeks from the planned price/promo
print(fan["y_hat"].sel(stat="median").round(1).values)
# --8<-- [end:sarimax]

# --8<-- [start:arima]
ARIMA = """
  use: ts.arima@^1
  settings: {order: [1, 1, 1], series_dims: [geo]}   # one ARIMA(1,1,1) per geo
"""
arima = fit_spec(spec(ARIMA), panel, raw_schema=sales_schema(geo=True))
# --8<-- [end:arima]

# --8<-- [start:ets]
ETS = """
  use: ts.ets@^1
  settings: {trend: add, damped_trend: true}
"""
ets = fit_spec(spec(ETS), history, raw_schema=sales_schema())  # drivers are not used
# --8<-- [end:ets]

# --8<-- [start:theta]
THETA = """
  use: ts.theta@^1
  settings: {series_dims: [geo]}
"""
theta = fit_spec(spec(THETA), panel, raw_schema=sales_schema(geo=True))
# --8<-- [end:theta]

# --8<-- [start:unobserved_components]
UC = """
  use: ts.unobserved_components@^1
  settings: {level: local linear trend, exog: [price]}
"""
uc = fit_spec(spec(UC), history, raw_schema=sales_schema())
print(sorted(uc.model.groups["components"].data_vars))  # the smoothed level, trend, ...
uc_fan = forecast(uc, plan.drop_vars("sales"))
# --8<-- [end:unobserved_components]

# --8<-- [start:ardl]
ARDL = """
  use: ts.ardl@^1
  settings: {lags: 2, order: 2, exog: [price, promo]}   # or auto: true to choose them
"""
ardl = fit_spec(spec(ARDL), history, raw_schema=sales_schema())
ardl_fan = forecast(ardl, plan.drop_vars("sales"))
# --8<-- [end:ardl]

# --8<-- [start:ecm]
ECM = """
  use: ts.ecm@^1
  settings: {lags: 1, order: 1, exog: [income]}
"""
monthly = ecm_dataset(periods=126, seed=4)  # sales tied to income in the long run
ecm = fit_spec(
    spec(ECM, "{dims: {t: month}, vars: {y: sales}}"),
    monthly.isel(month=slice(0, 120)),
    raw_schema=ECM_SCHEMA,
)
group = ecm.model.groups["ecm"]  # speed of adjustment, half-life, the bounds test
print(round(float(group["half_life"]), 1), float(group["bounds_p_upper"]) < 0.05)
ecm_fan = forecast(ecm, monthly.isel(month=slice(120, None)).drop_vars("sales"), periods=6)
# --8<-- [end:ecm]

# --8<-- [start:var]
VAR = """
  use: ts.var@^1
  settings: {max_lags: 4, ic: aic}     # the lag order chosen by AIC
"""
MACRO_BIND = "{dims: {t: quarter, variable: series}, vars: {y: value}}"
var = fit_spec(spec(VAR, MACRO_BIND), macro_growth(quarters=120), raw_schema=GROWTH_SCHEMA)
print(sorted(var.model.groups))  # ... fevd, granger, irf, params, residuals ...
# --8<-- [end:var]

# --8<-- [start:vecm]
VECM = """
  use: ts.vecm@^1
  settings: {k_ar_diff: 2, deterministic: ci}   # the rank from the Johansen test
"""
vecm = fit_spec(spec(VECM, MACRO_BIND), macro_dataset(quarters=160), raw_schema=MACRO_SCHEMA)
print(int(vecm.model.groups["cointegration"]["rank"]))
# --8<-- [end:vecm]

# --8<-- [start:varmax]
VARMAX = """
  use: ts.varmax@^1
  settings: {order: [1, 0]}            # VARX(1): q = 0
"""
varmax = fit_spec(spec(VARMAX, MACRO_BIND), macro_growth(quarters=90), raw_schema=GROWTH_SCHEMA)
# --8<-- [end:varmax]

# --8<-- [start:dynamic_factor]
DFM = """
  use: ts.dynamic_factor@^1
  settings: {k_factors: 1}
"""
dfm = fit_spec(spec(DFM, MACRO_BIND), macro_growth(quarters=90), raw_schema=GROWTH_SCHEMA)
print(sorted(dfm.model.groups["factors"].data_vars))
# --8<-- [end:dynamic_factor]

# --8<-- [start:markov_switching]
MARKOV = """
  use: ts.markov_switching@^1
  settings: {k_regimes: 2, time_kind: integer}   # weeks numbered 0, 1, 2, ...
"""
regimes = regime_dataset(periods=150, seed=3).drop_vars("regime")
markov = fit_spec(
    spec(MARKOV, "{dims: {t: week}, vars: {y: demand}}"), regimes, raw_schema=REGIME_SCHEMA
)
print(markov.model.groups["regimes"]["mean_level"].round(1).values)  # about 10 and 16
# --8<-- [end:markov_switching]

fitted = [sarimax, arima, ets, theta, uc, ardl, ecm, var, vecm, varmax, dfm, markov]
assert all(b.metrics is not None for b in fitted)
assert fan["y_hat"].sizes["date"] == 8 and ardl_fan["y_hat"].sizes["date"] == 8
assert uc_fan["y_hat"].sizes["date"] == 8 and ecm_fan["y_hat"].sizes["month"] == 6
assert int(vecm.model.groups["cointegration"]["rank"]) >= 1
