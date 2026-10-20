"""Manifests, About cards, settings validation and registration of the statsmodels plugins."""

from __future__ import annotations

from importlib import metadata

import pytest
from mapi_plugin_statsmodels import PLUGIN_IDS, PLUGINS
from mapi_plugin_statsmodels.multivariate import VarSettings
from mapi_plugin_statsmodels.statespace import SarimaxModel, SarimaxSettings
from mapi_sdk.registry import Registry
from mapi_sdk.testing import check_about
from mapi_spec.abouts import MODEL_ABOUTS, PLANNED_MODEL_IDS, builtin_abouts
from mapi_spec.capabilities import Representation
from mapi_spec.catalog import catalog_about_issues
from pydantic import ValidationError

STATSMODELS_IDS = {i for i in PLANNED_MODEL_IDS if i.startswith("ts.")}


def test_the_plugin_implements_every_planned_statsmodels_id() -> None:
    assert set(PLUGIN_IDS) == STATSMODELS_IDS
    assert len(PLUGIN_IDS) == 12


def test_entry_point_exports_the_plugins() -> None:
    eps = metadata.entry_points(group="mapi.models")
    ep = next(e for e in eps if e.name == "statsmodels")
    assert tuple(ep.load()) == PLUGINS


@pytest.mark.parametrize("plugin", PLUGINS, ids=lambda p: p.id)
def test_manifest(plugin: type) -> None:
    man = plugin.manifest()  # type: ignore[attr-defined]
    assert man.version == "1.0.0"
    assert man.capabilities.representation is Representation.QUANTILES
    assert man.capabilities.score_modes == frozenset({"predict", "forecast", "evaluate"})
    assert not man.capabilities.loo_valid and man.capabilities.loo_invalid_reason
    assert man.family in ("forecast", "ts_multivariate", "regime")
    assert man.outputs["y_hat"].units_of == "y" and man.outputs["y_hat"].dims_of == "y"
    # About cards come from the shared catalog text and are complete
    assert man.about == MODEL_ABOUTS[man.id]
    check_about(plugin, known_ids=list(builtin_abouts()))
    # the schema template resolves with defaults and with a panel + drivers
    base = man.schema_template.resolve({})
    assert base.dims["t"].policy.value == "extend"
    settings = {"series_dims": ["geo"]}
    if "exog" in man.settings_schema.get("properties", {}):
        settings["exog"] = ["price"]
    panel = man.schema_template.resolve(settings)
    assert panel.dims["geo"].policy.value == "fixed"
    assert panel.vars["y"].dims[0] == "geo" and panel.vars["y"].dims[-1] == "t"
    if "exog" in settings:
        assert panel.vars["price"].future_required and panel.vars["price"].dims == ("geo", "t")
    integer = man.schema_template.resolve({"time_kind": "integer"})
    assert integer.dims["t"].kind == "integer"


@pytest.mark.parametrize(
    ("model_id", "extra"),
    [
        ("ts.markov_switching", "core.state_sequence"),
        ("ts.unobserved_components", "core.decomposition"),
        ("ts.ets", "core.decomposition"),
    ],
)
def test_models_with_states_or_parts_list_their_plot(model_id: str, extra: str) -> None:
    plugin = next(p for p in PLUGINS if p.id == model_id)
    plots = plugin.manifest().plots  # type: ignore[attr-defined]
    assert list(plots) == ["core.ppc_fan", "core.fit_vs_actual", "core.residuals_over_time", extra]


def test_multivariate_templates_have_a_variable_dim() -> None:
    by_id = {p.id: p for p in PLUGINS}
    for mid in ("ts.var", "ts.vecm", "ts.varmax", "ts.dynamic_factor"):
        schema = by_id[mid].manifest().schema_template.resolve({})
        assert schema.vars["y"].dims == ("variable", "t")
        assert schema.dims["variable"].policy.value == "fixed"


def test_catalog_has_no_about_issues(registry: Registry) -> None:
    catalog = registry.catalog()
    models = [e.id for e in catalog.models]
    issues = catalog_about_issues(catalog)
    ours = {models.index(pid) for pid in PLUGIN_IDS}
    bad = [
        i
        for i in issues
        if i.pointer.startswith("/models/") and int(i.pointer.split("/")[2]) in ours
    ]
    assert not bad, [(i.pointer, i.message) for i in bad]


def test_registry_resolves_every_plugin(registry: Registry) -> None:
    for pid in PLUGIN_IDS:
        assert registry.model(f"{pid}@^1").id == pid


@pytest.mark.parametrize(
    "bad",
    [
        {"quantiles": [0.1, 0.5, 0.9]},  # no 90% band
        {"quantiles": [0.95, 0.5, 0.05]},  # not ascending
        {"order": [1, 3, 0]},  # d > 2
        {"seasonal_order": [1, 0, 0, 1]},  # season of 1
        {"series_dims": ["t"]},  # reserved name
        {"exog": ["y"]},  # the target
        {"exog": ["price", "price"]},
        {"unknown": 1},
    ],
)
def test_sarimax_settings_reject_bad_values(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SarimaxSettings.model_validate(bad)


def test_sarimax_settings_resolve_trend_and_season() -> None:
    s = SarimaxSettings.model_validate({"order": [1, 1, 0], "seasonal_order": [0, 0, 0, 52]})
    assert s.effective_seasonal() == (0, 0, 0, 0)
    assert s.effective_trend() == "n"
    assert SarimaxSettings().effective_trend() == "c"
    # the workflow gallery passes lists from expressions
    assert SarimaxModel().parse_settings({"order": [2, 1, 1], "exog": ["price"]}).order == (2, 1, 1)


def test_var_settings_defaults() -> None:
    s = VarSettings()
    assert s.lags is None and s.ic == "aic" and s.irf_horizon == 12


def test_distributed_lag_models_need_a_driver_before_any_compute(registry: Registry) -> None:
    from mapi_plugin_statsmodels.ardl import ArdlSettings, EcmSettings
    from mapi_plugin_statsmodels.datasets import ECM_SCHEMA
    from mapi_spec import load_model_spec, validate_spec

    for settings in (ArdlSettings, EcmSettings):
        with pytest.raises(ValidationError):
            settings.model_validate({})
    spec = load_model_spec(
        """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: ecm, project: tests}
data: {dataset: "dataset://tests/ecm@latest", bind: {dims: {t: month}, vars: {y: sales}}}
model: {use: "ts.ecm@^1", settings: {lags: 1}}
"""
    )
    result = validate_spec(spec, registry.catalog(), ECM_SCHEMA)
    assert not result.ok
    assert {i.code.value for i in result.errors} == {"SETTINGS_INVALID"}
