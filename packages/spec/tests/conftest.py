# ruff: noqa: E501
"""Shared fixtures: a hand-written catalog (E33-style steps and models) and datasets."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_spec import Catalog, DataSchema
from mapi_spec.loader import load_model_spec

# --------------------------------------------------------------------------- step entries

STRING_LIST = {"type": "array", "items": {"type": "string"}}


def _step(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"version": "1.0.0", "stateful": False}
    base.update(kw)
    return base


STEPS: list[dict[str, Any]] = [
    _step(
        id="core.fx_convert",
        kind="value",
        invertible=True,
        settings_schema={
            "type": "object",
            "properties": {
                "vars": STRING_LIST,
                "table": {"type": "string"},
                "to": {"type": "string", "pattern": "^[A-Z]{3}$"},
            },
            "required": ["vars", "table", "to"],
            "additionalProperties": False,
        },
        effect={
            "template_params": ["vars", "to"],
            "updates": {"{*vars as v}": {"units": "{to}"}},
        },
        var_params=["vars"],
        side_table_params=["table"],
    ),
    _step(
        id="core.resample",
        kind="reshape",
        settings_schema={
            "type": "object",
            "properties": {
                "dim": {"type": "string"},
                "freq": {"type": "string"},
                "how": {"enum": ["sum", "mean", "last"], "default": "sum"},
                "full_periods_only": {"type": "boolean", "default": True},
            },
            "required": ["dim", "freq"],
            "additionalProperties": False,
        },
        effect={
            "template_params": ["dim", "freq"],
            "transforms_dims": {"{dim}": {"kind": "time", "policy": "extend", "freq": "{freq}"}},
        },
        dim_params=["dim"],
    ),
    _step(
        id="core.mean_scale",
        kind="value",
        stateful=True,
        invertible=True,
        settings_schema={
            "type": "object",
            "properties": {
                "vars": STRING_LIST,
                "over": STRING_LIST,
                "unseen_levels": {"enum": ["error", "pooled", "fill"], "default": "error"},
            },
            "required": ["vars", "over"],
            "additionalProperties": False,
        },
        effect={"template_params": ["vars"], "updates": {"{*vars as v}": {"units": "1"}}},
        var_params=["vars"],
        dim_params=["over"],
    ),
    _step(
        id="core.lag_tensor",
        kind="feature",
        depends_on_target=True,
        target_vars_param="vars",
        settings_schema={
            "type": "object",
            "properties": {
                "vars": STRING_LIST,
                "along": {"type": "string"},
                "within": STRING_LIST,
                "l_max": {"type": "integer", "minimum": 1, "maximum": 104},
                "pad": {"enum": ["first", "zero"], "default": "zero"},
            },
            "required": ["vars", "along", "l_max"],
            "additionalProperties": False,
        },
        effect={
            "template_params": ["vars"],
            "transforms_dims": {"lag": {"kind": "integer", "policy": "fixed"}},
            "updates": {"{*vars as v}": {"append_dims": ["lag"]}},
        },
        context={"along": "{along}", "n": "{l_max}", "within": "{within}", "min_lag": 0},
        var_params=["vars"],
        dim_params=["along", "within"],
    ),
    _step(
        id="core.lag",
        kind="feature",
        depends_on_target=True,
        target_vars_param="vars",
        settings_schema={
            "type": "object",
            "properties": {
                "vars": STRING_LIST,
                "along": {"type": "string"},
                "within": {**STRING_LIST, "default": []},
                "lags": {"type": "array", "items": {"type": "integer", "minimum": 1}},
            },
            "required": ["vars", "along", "lags"],
            "additionalProperties": False,
        },
        effect={
            "template_params": ["vars"],
            "transforms_dims": {"lag": {"kind": "integer", "policy": "fixed"}},
            "derives": {"{*vars as v}_lagged": {"from": "{v}", "append_dims": ["lag"]}},
        },
        context={
            "along": "{along}",
            "n": "{max:lags}",
            "within": "{within}",
            "min_lag": "{min:lags}",
        },
        var_params=["vars"],
        dim_params=["along", "within"],
    ),
    _step(
        id="core.fourier",
        kind="feature",
        stateful=True,
        settings_schema={
            "type": "object",
            "properties": {
                "along": {"type": "string"},
                "period": {"type": "number", "exclusiveMinimum": 0},
                "order": {"type": "integer", "minimum": 1, "maximum": 50},
                "out": {"type": "string", "default": "fourier"},
            },
            "required": ["along", "period", "order"],
            "additionalProperties": False,
        },
        effect={
            "template_params": ["out", "along"],
            "transforms_dims": {"fourier_mode": {"kind": "categorical", "policy": "fixed"}},
            "adds": {
                "{out}": {"dims": ["{along}", "fourier_mode"], "dtype": "float64"},
            },
        },
        dim_params=["along"],
    ),
    _step(
        id="core.holidays",
        kind="feature",
        settings_schema={
            "type": "object",
            "properties": {"table": {"type": "string"}, "along": {"type": "string"}},
            "required": ["table", "along"],
            "additionalProperties": False,
        },
        effect={
            "template_params": ["along"],
            "adds": {"holiday": {"dims": ["{along}"], "dtype": "float64", "role": "control"}},
        },
        dim_params=["along"],
        side_table_params=["table"],
    ),
    _step(
        id="core.impute",
        kind="value",
        stateful=True,
        settings_schema={"type": "object"},
        effect={},
        var_params=["vars"],
    ),
    _step(
        id="core.calendar",
        kind="feature",
        settings_schema={"type": "object"},
        effect={
            "template_params": ["along"],
            "adds": {"calendar": {"dims": ["{along}"], "dtype": "int64"}},
        },
        dim_params=["along"],
    ),
    _step(
        id="core.log1p",
        kind="value",
        invertible=True,
        settings_schema={"type": "object", "properties": {"vars": STRING_LIST}},
        effect={},
        var_params=["vars"],
    ),
    _step(id="core.dyn", kind="reshape", effect={"dynamic": True}),
]

# resample that also renames the dim (date -> week): the split dim only exists afterwards.
STEPS.append(
    _step(
        id="core.resample_to",
        kind="reshape",
        settings_schema={
            "type": "object",
            "properties": {
                "dim": {"type": "string"},
                "to": {"type": "string"},
                "freq": {"type": "string"},
            },
            "required": ["dim", "to", "freq"],
            "additionalProperties": False,
        },
        effect={
            "template_params": ["dim", "to", "freq"],
            "renames": {"{dim}": "{to}"},
            "transforms_dims": {"{to}": {"kind": "time", "policy": "extend", "freq": "{freq}"}},
        },
        dim_params=["dim"],
    )
)

# A second, newer major of resample to exercise semver resolution.
STEPS.append({**STEPS[1], "version": "1.2.0"})
STEPS.append({**STEPS[1], "version": "2.0.0"})

MMM_MANIFEST: dict[str, Any] = {
    "id": "mmm.hierarchical_geo",
    "version": "1.3.2",
    "capabilities": {
        "inference": "mcmc",
        "representation": "draws",
        "score_modes": ["predict", "forecast", "counterfactual", "evaluate"],
        "decision": True,
        "contribution_scale": "additive",
    },
    "schema_template": {
        "params": {"controls": {"type": "string_list", "default": []}},
        "template": {
            "dims": {
                "geo": {
                    "kind": "categorical",
                    "policy": "new_levels",
                    "new_level_strategy": "hyperprior",
                },
                "channel": {"kind": "categorical", "policy": "fixed"},
                "time": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                "lag": {"kind": "integer"},
                "fourier_mode": {"kind": "categorical"},
            },
            "vars": {
                "y": {"dims": ["geo", "time"], "dtype": "float64", "role": "target"},
                "spend": {
                    "dims": ["geo", "channel", "time", "lag"],
                    "dtype": "float64",
                    "role": "spend",
                    "future_required": True,
                },
                "fourier": {"dims": ["time", "fourier_mode"], "dtype": "float64"},
                "{*controls as c}": {
                    "dims": ["geo", "time"],
                    "dtype": "float64",
                    "role": "control",
                },
            },
        },
    },
    "settings_schema": {
        "type": "object",
        "properties": {
            "pooling": {"enum": ["partial", "full", "none"], "default": "partial"},
            "centred": {"type": "boolean", "default": False},
            "adstock": {"type": "object"},
            "saturation": {"type": "object"},
            "priors": {"type": "object"},
            "controls": {**STRING_LIST, "default": []},
        },
        "additionalProperties": False,
    },
    "fit_schema": {
        "type": "object",
        "properties": {
            "mode": {"enum": ["evaluate_then_refit", "train_only"]},
            "engine": {"enum": ["nutpie", "pymc", "numpyro"]},
            "tune": {"type": "integer"},
            "draws": {"type": "integer", "maximum": 10000},
            "chains": {"type": "integer"},
            "target_accept": {"type": "number"},
            "seed": {"type": "integer"},
            "prior_predictive": {"type": "object"},
            "keep": {"type": "object"},
            "checkpoint": {"type": "object"},
        },
        "additionalProperties": False,
    },
    "outputs": {"y_hat": {"dims": ["geo", "time"], "units_of": "y"}},
    "derived": [
        {"name": "contributions", "dims": ["geo", "channel", "time"], "cache": "fit"},
        {"name": "response_curves", "dims": ["geo", "channel"], "cache": "fit"},
        {"name": "marginal_cost", "dims": ["geo", "channel"]},
    ],
    "plots": ["mmm.decomposition", "mmm.response_curves"],
    "resources": {"min_class": "S", "default_class": "M"},
}

XGB_MANIFEST: dict[str, Any] = {
    "id": "sklearn.tabular_regressor",
    "version": "1.0.0",
    "capabilities": {
        "inference": "point",
        "representation": "quantiles",
        "score_modes": ["predict", "forecast", "evaluate"],
    },
    "schema_template": {
        "params": {"rows": {"type": "string_list", "required": True}},
        "template": {"dims": {}, "vars": {}},
    },
    "settings_schema": {"type": "object"},
    "outputs": {"y_hat": {"dims": ["store", "sku", "time"]}},
}

POINT_MANIFEST: dict[str, Any] = {
    **XGB_MANIFEST,
    "id": "sklearn.point_regressor",
    "capabilities": {
        "inference": "point",
        "representation": "point",
        "score_modes": ["predict", "evaluate"],
        "loo_valid": False,
        "loo_invalid_reason": "no likelihood",
    },
    "resources": {"min_class": "M", "default_class": "M"},
}

HSGP_MANIFEST: dict[str, Any] = {
    "id": "ts.hsgp",
    "version": "1.0.0",
    "capabilities": {
        "inference": "mcmc",
        "representation": "draws",
        "score_modes": ["forecast", "evaluate"],
    },
    "schema_template": {
        "params": {"horizon_max": {"type": "integer", "default": 52}},
        "template": {
            "dims": {
                "time": {
                    "kind": "time",
                    "policy": "extend",
                    "freq": "W-SUN",
                    "max_extension": "{horizon_max}",
                }
            },
            "vars": {"y": {"dims": ["time"], "dtype": "float64", "role": "target"}},
        },
    },
    "settings_schema": {
        "type": "object",
        "properties": {"horizon_max": {"type": "integer", "minimum": 1}},
    },
}

PLOTS = [
    {"id": "core.ppc_fan", "title": "PPC fan", "requires_representation": ["draws"]},
    {"id": "core.convergence", "title": "Convergence", "requires_representation": ["draws"]},
    {"id": "core.fit_vs_actual", "title": "Fit vs actual"},
    {"id": "core.residuals_over_time", "title": "Residuals"},
    {"id": "ml.feature_importance", "title": "Feature importance"},
    {"id": "mmm.decomposition", "title": "Decomposition"},
    {"id": "mmm.response_curves", "title": "Response curves"},
]


def make_catalog() -> Catalog:
    return Catalog.model_validate(
        {
            "steps": STEPS,
            "models": [
                {"manifest": MMM_MANIFEST},
                {"manifest": {**MMM_MANIFEST, "version": "1.2.0"}},
                {"manifest": {**MMM_MANIFEST, "version": "1.4.0"}, "yanked": True},
                {"manifest": XGB_MANIFEST},
                {"manifest": POINT_MANIFEST},
                {"manifest": HSGP_MANIFEST},
            ],
            "plots": PLOTS,
        }
    )


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
  use: sklearn.tabular_regressor@^1
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


@pytest.fixture
def catalog() -> Catalog:
    return make_catalog()


@pytest.fixture
def e33_raw() -> DataSchema:
    return DataSchema.model_validate(E33_RAW)


@pytest.fixture
def e33_spec():  # type: ignore[no-untyped-def]
    return load_model_spec(E33_YAML)


@pytest.fixture
def xgb_raw() -> DataSchema:
    return DataSchema.model_validate(XGB_RAW)
