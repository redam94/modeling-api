"""Manifests, settings/fit JSON Schemas, schema templates and static spec validation."""

from __future__ import annotations

import typing
from collections.abc import Callable
from typing import Any

import pytest
from mapi_plugin_mmm.hierarchical_geo import HierarchicalGeoMMM
from mapi_plugin_mmm.priors import PRIOR_SLOTS
from mapi_plugin_mmm.pymc_marketing import PyMCMarketingMMM
from mapi_plugin_mmm.settings import HierarchicalGeoSettings, Priors, PyMCMarketingSettings
from mapi_sdk.registry import Registry, default_registry
from mapi_spec import DataSchema, load_model_spec, validate_spec
from mapi_spec.errors import ErrorCode
from mapi_spec.schema import DimPolicy
from mapi_spec.validate import validate_settings
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _ui_levels(schema: dict[str, Any]) -> dict[str, str | None]:
    """``x-ui-level`` of every settings property (top level and nested models)."""
    out: dict[str, str | None] = {}
    for name, prop in schema.get("properties", {}).items():
        out[name] = prop.get("x-ui-level")
    for model, sub in schema.get("$defs", {}).items():
        if model == "Priors" or model.endswith("Prior"):
            continue  # the prior dict form; the whole `priors` field is advanced
        for name, prop in sub.get("properties", {}).items():
            out[f"{model}.{name}"] = prop.get("x-ui-level")
    return out


@pytest.mark.parametrize("plugin", [HierarchicalGeoMMM, PyMCMarketingMMM])
def test_every_setting_has_a_ui_level(plugin: type[Any]) -> None:
    m = plugin.manifest()
    levels = _ui_levels(m.settings_schema)
    assert levels and set(levels.values()) <= {"basic", "advanced"}, levels
    assert "basic" in levels.values()
    fit_levels = {k: v.get("x-ui-level") for k, v in m.fit_schema["properties"].items()}
    assert set(fit_levels.values()) <= {"basic", "advanced"}
    assert fit_levels["draws"] == "basic" and fit_levels["engine"] == "advanced"


def test_hierarchical_geo_manifest() -> None:
    m = HierarchicalGeoMMM.manifest()
    caps = m.capabilities
    assert m.id == "mmm.hierarchical_geo" and m.version == "1.3.0"
    assert caps.decision and caps.fast_score and caps.contribution_scale == "additive"
    assert set(caps.score_modes) >= {"predict", "forecast", "counterfactual", "evaluate"}
    assert m.derived_names() == {"contributions", "response_curves", "marginal_cost"}
    assert all(d.cache == "fit" for d in m.derived)
    assert set(m.outputs) == {"y_hat", "contributions"}
    assert "mmm.response_curves" in m.plots
    # settings defaults are visible to the control plane (Guided-mode forms)
    props = m.settings_schema["properties"]
    assert props["pooling"]["default"] == "partial"
    assert props["adstock"]["default"]["kind"] == "geometric"
    schema = m.schema_template.resolve({})
    assert schema.dims["geo"].policy is DimPolicy.NEW_LEVELS
    assert schema.dims["geo"].new_level_strategy == "hyperprior"
    assert schema.vars["spend"].dims == ("geo", "channel", "time", "lag")
    none = m.schema_template.resolve({"pooling": "none", "lag_dim": "l"})
    assert none.dims["geo"].policy is DimPolicy.FIXED
    assert none.vars["spend"].dims == ("geo", "channel", "time", "l")


def test_pymc_marketing_manifest_owns_preprocessing() -> None:
    m = PyMCMarketingMMM.manifest()
    assert set(m.capabilities.owns) == {"scaling", "carryover", "inverse_units"}
    schema = m.schema_template.resolve({"dims": ["geo"]})
    assert schema.vars["y"].dims == ("geo", "time")
    assert schema.vars["spend"].dims == ("geo", "channel", "time")
    assert schema.dims["geo"].policy is DimPolicy.FIXED


def test_settings_validation() -> None:
    with pytest.raises(ValidationError, match="unique"):
        HierarchicalGeoSettings.model_validate({"controls": ["a", "a"]})
    with pytest.raises(ValidationError, match="cannot be controls"):
        HierarchicalGeoSettings.model_validate({"controls": ["y"]})
    with pytest.raises(ValidationError, match="HalfNormal"):
        HierarchicalGeoSettings.model_validate(
            {"priors": {"beta_channel": {"distribution": "Normal", "sigma": 1}}}
        )
    with pytest.raises(ValidationError, match="positive support"):
        HierarchicalGeoSettings.model_validate(
            {"priors": {"beta_channel": {"distribution": "Uniform", "lower": -1, "upper": 2}}}
        )
    ok = HierarchicalGeoSettings.model_validate(
        {"priors": {"lam": {"distribution": "Gamma", "alpha": 3, "beta": 1, "dims": ["channel"]}}}
    )
    assert ok.resolved_priors()["lam"].params == {"alpha": 3.0, "beta": 1.0}
    assert ok.resolved_priors()["sigma"].distribution == "HalfNormal"  # default kept
    with pytest.raises(ValidationError):
        HierarchicalGeoSettings.model_validate({"pooling": "some"})
    with pytest.raises(ValidationError, match="reserved"):
        PyMCMarketingSettings.model_validate({"dims": ["channel"]})
    with pytest.raises(ValidationError, match="geometric"):
        PyMCMarketingSettings.model_validate({"adstock": {"kind": "delayed", "l_max": 3}})
    grid = HierarchicalGeoSettings().grid
    assert grid.multipliers()[0] == 0.0 and grid.multipliers()[-1] == 2.0
    assert len(grid.multipliers()) == grid.points


def test_settings_schema_rejects_bad_priors_statically() -> None:
    schema = HierarchicalGeoMMM.manifest().settings_schema
    issues = validate_settings(
        schema, {"priors": {"lam": {"distribution": "Exec", "x": "y"}}}, "/model/settings"
    )
    assert issues and all(i.code is ErrorCode.SETTINGS_INVALID for i in issues)
    assert any(i.pointer.startswith("/model/settings/priors/lam") for i in issues)
    assert validate_settings(schema, {"priors": {"nope": {"distribution": "Normal"}}}, "/s")
    # support and dims are part of the static schema: no fit needed to catch them
    for bad in (
        {"beta_channel": {"distribution": "Normal", "sigma": 1}},
        {"alpha": {"distribution": "Gamma", "alpha": 2, "beta": 1}},
        {"sigma": {"distribution": "HalfNormal", "sigma": 1, "dims": ["geo"]}},
        {"lam": {"distribution": "Gamma", "alpha": 2, "beta": 1, "dims": ["geo"]}},
    ):
        assert validate_settings(schema, {"priors": bad}, "/s"), bad
    plan = {
        "beta_channel": {"distribution": "HalfNormal", "sigma": 1.0, "dims": ["channel"]},
        "lam": {"distribution": "Gamma", "alpha": 3, "beta": 1, "dims": ["channel"]},
    }
    assert validate_settings(schema, {"priors": plan}, "/s") == []


def test_every_prior_slot_is_a_typed_setting() -> None:
    fields = Priors.model_fields
    assert set(fields) == set(PRIOR_SLOTS)
    for name, slot in PRIOR_SLOTS.items():
        assert slot.model in typing.get_args(fields[name].annotation), name
        slot.model.model_validate(slot.default.model_dump())  # defaults satisfy their slot


def test_plan_spec_validates_against_the_real_catalog(
    registry: Registry, raw_schema: DataSchema, spec_text: Callable[..., str]
) -> None:
    spec = load_model_spec(spec_text())
    res = validate_spec(spec, registry.catalog(), raw_schema)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]
    rs = res.raise_for_errors()
    assert rs.model.resolved == "mmm.hierarchical_geo@1.3.0"
    assert rs.split_stage == "weekly" and rs.model_stage == "hols"
    assert rs.model.effective_policies["geo"] is DimPolicy.NEW_LEVELS
    # the plan's dim_policies tightening (geo: fixed) is accepted and takes effect
    tight = load_model_spec(
        spec_text(**{"pooling: partial": "pooling: partial\n    dim_policies: {geo: fixed}"})
    )
    rs2 = validate_spec(tight, registry.catalog(), raw_schema).raise_for_errors()
    assert rs2.model.effective_policies["geo"] is DimPolicy.FIXED
    # bad fit settings are caught by the plugin's fit schema
    bad = load_model_spec(spec_text(**{"engine: pymc": "engine: smc"}))
    res3 = validate_spec(bad, registry.catalog(), raw_schema)
    assert any(i.pointer == "/model/fit/engine" for i in res3.errors)


def test_entry_points_register_both_plugins() -> None:
    reg = default_registry(reload=True)
    refs = {f"{m.id}@{m.version}" for m in (e.manifest for e in reg.catalog().models)}
    assert {"mmm.hierarchical_geo@1.3.0", "mmm.pymc_marketing@1.0.0"} <= refs
