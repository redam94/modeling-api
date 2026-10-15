"""Shared fixtures: a hand-written catalog (E33-style steps and models) and datasets."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_spec import Catalog, DataSchema
from mapi_spec.examples import E33_RAW, E33_YAML, XGB_RAW
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
