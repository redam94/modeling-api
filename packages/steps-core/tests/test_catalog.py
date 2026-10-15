"""Catalog export, the entry point and static validation of the plan's example specs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import mapi_steps
import pytest
from mapi_sdk.registry import Registry, default_registry
from mapi_spec import Catalog, DataSchema, ModelSpec
from mapi_spec.effects import check_effect_template
from mapi_spec.examples import E33_RAW, E33_YAML, XGB_RAW, XGB_YAML
from mapi_spec.loader import load_model_spec
from mapi_spec.validate import apply_defaults, validate_spec
from mapi_steps import catalog as cat

pytestmark = pytest.mark.unit


def test_entry_point_registers_every_step() -> None:
    reg = default_registry(reload=True)
    try:
        ids = {s.id for s in reg.steps()}
        assert ids >= {s.id for s in mapi_steps.STEPS}
        assert not [e for e in reg.load_errors if "mapi_steps" in e]
        assert reg.step("core.resample@^1") is mapi_steps.ResampleStep
    finally:
        default_registry(reload=True)


def test_ids_versions_and_kinds() -> None:
    for step in mapi_steps.STEPS:
        assert step.id.startswith("core.") and step.version.startswith("1.")
        entry = step.catalog_entry()
        check_effect_template(entry.effect)
        assert entry.summary and entry.summary_plain
        assert entry.settings_schema.get("additionalProperties") is False
    assert len(mapi_steps.STEPS) == 28
    assert len({s.id for s in mapi_steps.STEPS}) == 28


def test_shipped_catalog_is_up_to_date() -> None:
    """``src/mapi_steps/catalog.json`` must be regenerated when a step changes (from the
    package dir): ``python -m mapi_steps.catalog --splits --out src/mapi_steps/catalog.json``.
    """
    shipped = Path(mapi_steps.__file__).with_name("catalog.json").read_text("utf-8")
    assert shipped == cat.catalog_json(splits=True)
    assert cat.shipped_catalog() == cat.catalog()


def test_cli_writes_and_prints(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "catalog.json"
    assert cat.main(["--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert "splits" not in data
    Catalog.model_validate(data)
    assert cat.main(["--splits"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in printed["splits"]][:2] == ["core.time_holdout", "core.as_of"]


@pytest.mark.integration
def test_module_entry_runs_without_importing_into_the_caller() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "mapi_steps.catalog"], capture_output=True, text=True, check=True
    )
    assert json.loads(proc.stdout)["steps"]


def test_defaults_render_every_effect_and_context() -> None:
    """Settings with only the required fields render the static templates."""
    minimal: dict[str, dict[str, Any]] = {
        "core.resample": {"freq": "W-SUN"},
        "core.count_events": {"freq": "D"},
        "core.filter_coords": {"dim": "geo", "keep": ["a"]},
        "core.to_long": {"group_dim": "hh", "within_dim": "trip", "vars": ["y"]},
        "core.to_padded": {
            "group": "hh_id",
            "group_dim": "hh",
            "within_dim": "trip",
            "vars": ["y"],
        },
        "core.stack": {"dims": ["a", "b"], "new_dim": "ab"},
        "core.unstack": {"dim": "ab", "into": ["a", "b"]},
        "core.fx_convert": {"vars": ["y"], "table": "fx"},
        "core.fourier": {"period": 52},
        "core.calendar": {"features": ["month"]},
        "core.holidays": {"table": "h"},
        "core.lag": {"vars": ["y"], "lags": [1]},
        "core.lag_tensor": {"vars": ["y"], "l_max": 3},
        "core.encode_levels": {"dims": ["geo"]},
        "core.formula": {"out": "z", "expr": "y + 1"},
        "core.adstock": {"vars": ["x"], "alpha": 0.5},
        "core.saturation": {"vars": ["x"], "lam": 1.0},
        "core.replace_sentinel": {"vars": ["x"], "sentinels": [-1]},
        "core.clip": {"vars": ["x"], "lower": 0},
        "core.impute": {"vars": ["x"], "method": "constant"},
    }
    catalog = cat.catalog()
    for entry in catalog.steps:
        settings = minimal.get(entry.id, {"vars": ["y"], "over": ["time"]})
        settings = apply_defaults(entry.settings_schema, settings)
        entry.resolved_effect(settings)
        entry.resolved_context(settings)


# ----------------------------------------------------------- the plan's example specs

STRING_LIST = {"type": "array", "items": {"type": "string"}}
MMM: dict[str, Any] = {
    "id": "mmm.hierarchical_geo",
    "version": "1.3.0",
    "capabilities": {
        "inference": "mcmc",
        "representation": "draws",
        "score_modes": ["predict", "forecast", "counterfactual", "evaluate"],
        "contribution_scale": "additive",
    },
    "schema_template": {
        "template": {
            "dims": {
                "geo": {
                    "kind": "categorical",
                    "policy": "new_levels",
                    "new_level_strategy": "hyperprior",
                },
                "channel": {"kind": "categorical"},
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
                },
                "fourier": {"dims": ["time", "fourier_mode"], "dtype": "float64"},
                "holiday": {"dims": ["time"], "dtype": "float64", "role": "control"},
            },
        }
    },
    "settings_schema": {"type": "object"},
    "fit_schema": {"type": "object"},
    "outputs": {"y_hat": {"dims": ["geo", "time"], "units_of": "y"}},
    "derived": [
        {"name": "contributions", "dims": ["geo", "channel", "time"], "cache": "fit"},
        {"name": "response_curves", "dims": ["geo", "channel"], "cache": "fit"},
        {"name": "marginal_cost", "dims": ["geo", "channel"]},
    ],
}
XGB: dict[str, Any] = {
    "id": "sklearn.tabular_quantiles",
    "version": "1.0.0",
    "capabilities": {
        "inference": "point",
        "representation": "quantiles",
        "score_modes": ["predict", "forecast", "evaluate"],
    },
    "schema_template": {
        "template": {
            "dims": {
                "store": {"kind": "categorical", "policy": "new_levels"},
                "sku": {"kind": "categorical"},
                "time": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                "lag": {"kind": "integer"},
                "calendar_feature": {"kind": "categorical"},
            },
            "vars": {
                "y": {"dims": ["store", "sku", "time"], "dtype": "float64", "role": "target"},
                "promo_lagged": {"dims": ["store", "sku", "time", "lag"], "dtype": "float64"},
                "calendar": {"dims": ["time", "calendar_feature"], "dtype": "float64"},
            },
        }
    },
    "settings_schema": {"type": "object"},
}


def _catalog() -> Catalog:
    base = cat.catalog().model_dump(mode="json")
    base["models"] = [{"manifest": MMM}, {"manifest": XGB}]
    base["plots"] = [
        {"id": pid, "title": pid}
        for pid in (
            "core.ppc_fan",
            "core.convergence",
            "mmm.decomposition",
            "mmm.response_curves",
            "core.fit_vs_actual",
            "core.residuals_over_time",
            "ml.feature_importance",
        )
    ]
    return Catalog.model_validate(base)


def test_plan_e33_example_validates_against_the_real_steps() -> None:
    res = validate_spec(load_model_spec(E33_YAML), _catalog(), DataSchema.model_validate(E33_RAW))
    assert res.ok, [i.message for i in res.errors]
    rs = res.raise_for_errors()
    assert rs.split_stage == "weekly"
    assert [s.resolved for s in rs.steps][:2] == ["core.fx_convert@1.0.0", "core.resample@1.0.0"]
    lags = next(s for s in rs.steps if s.id == "lags")
    assert lags.context is not None and lags.context.n == 6 and lags.context.min_lag == 0
    assert rs.stages["hols"] is not None and "holiday" in rs.stages["hols"].vars


def test_plan_xgboost_example_validates_against_the_real_steps() -> None:
    res = validate_spec(load_model_spec(XGB_YAML), _catalog(), DataSchema.model_validate(XGB_RAW))
    assert res.ok, [i.message for i in res.errors]
    rs = res.raise_for_errors()
    plags = next(s for s in rs.steps if s.id == "plags")
    assert not plags.depends_on_target  # lags promo, not the target
    assert rs.stages["logy"] is not None


def test_static_checks_catch_bad_settings_and_names() -> None:
    spec = load_model_spec(E33_YAML)
    data = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    data["pipeline"][2]["with"]["over"] = ["week"]
    data["pipeline"][4]["with"]["vars"] = ["y"]  # lagging the target in place
    res = validate_spec(
        ModelSpec.model_validate(data), _catalog(), DataSchema.model_validate(E33_RAW)
    )
    codes = {i.code.value for i in res.errors}
    assert "SCHEMA_INVALID" in codes
    assert any(i.pointer == "/pipeline/2/with/over/0" for i in res.errors)
    bad = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    bad["pipeline"][1]["with"]["how"] = "median"
    res = validate_spec(
        ModelSpec.model_validate(bad), _catalog(), DataSchema.model_validate(E33_RAW)
    )
    assert any(i.pointer.startswith("/pipeline/1/with") for i in res.errors)


def test_registry_helper_extends_a_base_registry() -> None:
    base = Registry()
    reg = mapi_steps.registry(base)
    assert not base.steps() and len(reg.steps()) == 28
