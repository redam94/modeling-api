"""Manifests, About cards, settings and schema templates of the mmm-framework plugins."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_plugin_mmm.bayesian import MMMBayesian
from mapi_plugin_mmm.frequentist import MMMFrequentist
from mapi_plugin_mmm.plugins import PLUGINS
from mapi_plugin_mmm.settings import BayesianSettings, FrequentistSettings, GridSettings
from mapi_sdk.testing import check_about
from mapi_spec.abouts import PLANNED_MODEL_IDS
from mapi_spec.catalog import Catalog, catalog_about_issues
from mapi_spec.errors import MapiError
from mapi_spec.guided import GUIDED_TEMPLATES, MMM_MODEL_ID, MMM_TEMPLATE_SETTINGS
from pydantic import ValidationError


def test_the_package_registers_the_mmm_framework_models() -> None:
    assert [p.id for p in PLUGINS] == ["mmm.bayesian", "mmm.frequentist"]
    assert {p.id for p in PLUGINS} <= PLANNED_MODEL_IDS
    ids = {p.id for p in PLUGINS}
    assert not {"mmm.hierarchical_geo", "mmm.pymc_marketing", "mmm.bayesinsight"} & ids


@pytest.mark.parametrize("plugin", PLUGINS, ids=lambda p: p.id)
def test_every_model_has_a_complete_about_card(plugin: Any) -> None:
    m = plugin.manifest()
    assert m.about is not None, plugin.id
    report = check_about(plugin, raise_on_failure=False)
    assert report.ok, report.summary()
    assert m.family == "mmm"


def test_the_catalog_rule_finds_no_about_problems() -> None:
    catalog = Catalog(models=[p.catalog_entry() for p in PLUGINS])
    errors = [i for i in catalog_about_issues(catalog) if i.severity == "error"]
    assert errors == []


def test_capabilities_and_ownership() -> None:
    caps = MMMBayesian.manifest().capabilities
    assert caps.owns == {"scaling", "carryover", "inverse_units"}
    assert caps.calibration == {"business_priors", "lift_tests"}
    assert caps.decision and caps.contribution_scale == "additive"
    assert {"predict", "forecast", "counterfactual", "evaluate"} <= caps.score_modes
    freq = MMMFrequentist.manifest().capabilities
    assert freq.inference == "bootstrap" and not freq.calibration
    assert freq.deterministic_given_seed


def test_fit_schema_lists_the_mmm_framework_engines() -> None:
    engines = MMMBayesian.manifest().fit_schema["properties"]["engine"]["enum"]
    assert {"nutpie", "pymc", "numpyro", "map", "advi", "pathfinder"} <= set(engines)
    assert MMMBayesian.manifest().fit_schema["properties"]["engine"]["default"] == "nutpie"


@pytest.mark.parametrize(
    ("pool", "dims"),
    [
        ([], {"time", "channel"}),
        (["geo"], {"time", "channel", "geo"}),
        (["product", "geo"], {"time", "channel", "geo", "product"}),
    ],
)
def test_schema_template_follows_the_panel_dims(pool: list[str], dims: set[str]) -> None:
    schema = MMMBayesian.manifest().schema_template.resolve({"hierarchy": {"pool_over": pool}})
    assert set(schema.dims) == dims
    # the template keeps the settings' order (dim order is not significant in xarray)
    assert list(schema.vars["y"].dims) == [*pool, "time"]
    assert list(schema.vars["spend"].dims) == [*pool, "channel", "time"]
    assert schema.vars["spend"].future_required
    for d in pool:
        assert schema.dims[d].policy.value == "new_levels"
        assert schema.dims[d].new_level_strategy == "pooled"


def test_the_guided_geo_template_settings_are_accepted() -> None:
    s = MMMBayesian().parse_settings(MMM_TEMPLATE_SETTINGS)
    assert isinstance(s, BayesianSettings)
    assert s.panel_dims == ["geo"]
    assert s.saturation.kind == "hill" and s.adstock.l_max == 8
    tpl = GUIDED_TEMPLATES["geo_mmm"]
    assert tpl.plugin.startswith(MMM_MODEL_ID)


def test_settings_reject_inconsistent_values() -> None:
    with pytest.raises(ValidationError):
        BayesianSettings.model_validate({"hierarchy": {"media_by_geo": True}})
    with pytest.raises(ValidationError):
        BayesianSettings.model_validate({"hierarchy": {"pool_over": ["geo", "geo"]}})
    with pytest.raises(ValidationError):
        BayesianSettings.model_validate({"controls": ["y"]})
    with pytest.raises(ValidationError):
        BayesianSettings.model_validate({"adstock": {"kind": "exotic"}})
    with pytest.raises(ValidationError):
        BayesianSettings.model_validate({"priors": {"roi": {"tv": {"mu": 0, "sigma": 0}}}})
    with pytest.raises(ValidationError):
        FrequentistSettings.model_validate({"trend": {"kind": "gp"}})
    with pytest.raises(MapiError):
        MMMBayesian().parse_settings({"unknown": 1})


def test_hierarchy_order_is_canonical() -> None:
    s = BayesianSettings.model_validate({"hierarchy": {"pool_over": ["product", "geo"]}})
    assert s.panel_dims == ["geo", "product"]


def test_per_channel_overrides() -> None:
    s = BayesianSettings.model_validate(
        {"adstock": {"l_max": 6}, "channels": {"tv": {"adstock": {"kind": "weibull", "l_max": 12}}}}
    )
    assert s.adstock_for("tv").kind == "weibull" and s.adstock_for("tv").l_max == 12
    assert s.adstock_for("search").l_max == 6
    assert s.saturation_for("tv").kind == "logistic"


def test_grid_multipliers_always_hold_today() -> None:
    assert 1.0 in GridSettings(max_multiplier=2.5, points=26).multipliers()
    grid = GridSettings(max_multiplier=3.0, points=4).multipliers()
    assert grid == [0.0, 1.0, 2.0, 3.0]
    odd = GridSettings(max_multiplier=2.0, points=4).multipliers()
    assert 1.0 in odd and len(odd) == 5


@pytest.mark.parametrize(
    ("strategy", "policy"),
    [
        ("pooled", "new_levels"),
        ("hyperprior", "new_levels"),
        ("zero", "new_levels"),
        ("error", "fixed"),
    ],
)
def test_new_levels_set_the_panel_dims_policy(strategy: str, policy: str) -> None:
    schema = MMMBayesian.manifest().schema_template.resolve(
        {"hierarchy": {"pool_over": ["geo", "product"]}, "new_levels": strategy}
    )
    for d in ("geo", "product"):
        assert schema.dims[d].policy.value == policy
        assert schema.dims[d].new_level_strategy == strategy
