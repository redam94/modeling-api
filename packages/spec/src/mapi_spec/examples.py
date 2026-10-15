"""Canonical example documents (from the architecture plan) and their raw dataset schemas.

Used by tests across packages, by the docs and as starting templates. They are plain strings
and JSON dicts, so importing this module costs nothing.
"""

# ruff: noqa: E501
from __future__ import annotations

from typing import Any

__all__ = ["E33_RAW", "E33_YAML", "SCORE_YAML", "XGB_RAW", "XGB_YAML"]


E33_RAW: dict[str, Any] = {
    "dims": {
        "market": {"kind": "categorical", "policy": "new_levels", "new_level_strategy": "pooled"},
        "channel": {"kind": "categorical"},
        "date": {"kind": "time", "policy": "extend", "freq": "D"},
    },
    "vars": {
        "revenue_local": {"dims": ["market", "date"], "dtype": "float64", "role": "target"},
        "spend_local": {
            "dims": ["market", "channel", "date"],
            "dtype": "float64",
            "role": "spend",
            "future_required": True,
        },
    },
}

E33_YAML = """\
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: eu-geo-mmm, project: growth, labels: {family: mmm}}

data:
  dataset: dataset://growth/eu_media_daily@latest
  side_tables:
    fx:       {ref: dataset://growth/fx_daily@latest, score_policy: supplied}
    holidays: {ref: dataset://shared/holidays_eu@v3, score_policy: pinned}
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 8, after_step: weekly}

pipeline:
  - {id: to_eur,      use: core.fx_convert@^1, with: {vars: [y, spend], table: fx, to: EUR}}
  - {id: weekly,      use: core.resample@^1,   with: {dim: time, freq: W-SUN, how: sum, full_periods_only: true}}
  - {id: scale_spend, use: core.mean_scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
  - {id: scale_y,     use: core.mean_scale@^1, with: {vars: [y], over: [time], unseen_levels: pooled}}
  - {id: lags,        use: core.lag_tensor@^1, with: {vars: [spend], along: time, within: [geo], l_max: 6, pad: first}}
  - {id: season,      use: core.fourier@^1,    with: {along: time, period: 52.18, order: 3, out: fourier}}
  - {id: hols,        use: core.holidays@^1,   with: {table: holidays, along: time}}

model:
  use: mmm.hierarchical_geo@^1.3
  settings:
    pooling: partial
    centred: false
    adstock: {kind: geometric, l_max: 6}
    saturation: {kind: logistic}
    priors:
      beta_channel: {distribution: HalfNormal, sigma: 1.0, dims: [channel]}
      lam:          {distribution: Gamma, alpha: 3, beta: 1, dims: [channel]}
    dim_policies: {geo: fixed}
  fit:
    mode: evaluate_then_refit
    engine: nutpie
    tune: 400
    draws: 4000
    chains: 4
    target_accept: 0.95
    seed: 20261014
    prior_predictive: {draws: 1000}
    keep: {log_likelihood: true, posterior_predictive: {thin: 4}}
    checkpoint: {every_draws: 500}

outputs:
  derived: [contributions, response_curves, marginal_cost]
  plots: [core.ppc_fan, core.convergence, mmm.decomposition, mmm.response_curves]
  ui_draws: 300
evaluate: {metrics: [elpd_loo, coverage_90, mape]}
execution: {resource_class: M, timeout: 45m}
"""

XGB_YAML = """\
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: store-demand-xgb, project: retail}
data:
  dataset: dataset://retail/store_sku_weekly@latest
  bind:
    dims: {store: store_id, sku: sku, time: week}
    vars: {y: units_sold, price: price, promo: promo_flag}
  split: {kind: time_holdout, dim: time, last: 12}
pipeline:
  - {id: imp,   use: core.impute@^1,  with: {vars: [price], method: ffill, along: time, within: [store, sku]}}
  - {id: plags, use: core.lag@^1,     with: {vars: [promo], along: time, within: [store, sku], lags: [1, 2]}}
  - {id: cal,   use: core.calendar@^1, with: {along: time, features: [week_of_year, month]}}
  - {id: logy,  use: core.log1p@^1,   with: {vars: [y]}}
model:
  use: sklearn.tabular_quantiles@^1   # quantile uncertainty: a manifest declares one representation
  settings:
    estimator: {kind: xgboost.XGBRegressor, params: {n_estimators: 600, max_depth: 6, learning_rate: 0.05}}
    rows: [store, sku, time]
    columns: {roles: [covariate]}
    coord_features: {store: category, sku: category}
    uncertainty: {kind: quantile, quantiles: [0.1, 0.5, 0.9]}
    forecast_strategy: direct
    retransform: median
  fit: {seed: 7, mode: evaluate_then_refit}
evaluate: {metrics: [rmse, mape, coverage_80]}
outputs: {plots: [core.fit_vs_actual, core.residuals_over_time, ml.feature_importance]}
execution: {resource_class: S, timeout: 10m}
"""

XGB_RAW: dict[str, Any] = {
    "dims": {
        "store_id": {"kind": "categorical", "policy": "new_levels", "new_level_strategy": "pooled"},
        "sku": {"kind": "categorical"},
        "week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
    },
    "vars": {
        "units_sold": {"dims": ["store_id", "sku", "week"], "dtype": "float64", "role": "target"},
        "price": {"dims": ["store_id", "sku", "week"], "dtype": "float64", "missing": "allow"},
        "promo_flag": {"dims": ["store_id", "sku", "week"], "dtype": "float64"},
    },
}

SCORE_YAML = """\
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://growth/eu-geo-mmm-prod
mode: forecast
horizon: {dim: time, periods: 26}
inputs_stage: scale_spend
scenario: dataset://growth/plan_2027h1
interventions: [{var: spend, sel: {channel: meta}, multiply: 1.2}]
output: {kind: expected, summary: [mean, hdi_90], draws: 200, units: original}
"""
