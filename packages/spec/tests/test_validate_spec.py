from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec import Catalog, DataSchema, ModelSpec
from mapi_spec.errors import ErrorCode, Severity, ValidationFailed
from mapi_spec.examples import E33_RAW, E33_YAML, XGB_YAML
from mapi_spec.loader import parse_yaml, spec_hash
from mapi_spec.schema import DimPolicy
from mapi_spec.validate import (
    ResolvedSpec,
    SpecValidation,
    apply_defaults,
    validate_settings,
    validate_spec,
)

pytestmark = pytest.mark.unit


def _e33() -> dict[str, Any]:
    data = parse_yaml(E33_YAML)
    assert isinstance(data, dict)
    return data


def _run(
    catalog: Catalog, data: dict[str, Any], raw: dict[str, Any] | None = None
) -> SpecValidation:
    return validate_spec(
        ModelSpec.model_validate(data), catalog, DataSchema.model_validate(raw or E33_RAW)
    )


def _codes(res: SpecValidation, severity: Severity | None = Severity.ERROR) -> list[str]:
    return [i.code.value for i in res.issues if severity is None or i.severity is severity]


def _find(res: SpecValidation, code: ErrorCode) -> Any:
    hits = [i for i in res.issues if i.code is code]
    assert hits, f"no {code} in {res.issues}"
    return hits[0]


# ------------------------------------------------------------------------------- happy path


def test_e33_validates(catalog: Catalog) -> None:
    res = _run(catalog, _e33())
    assert res.ok, res.issues
    assert res.issues == []
    rs = res.raise_for_errors()
    assert rs.split_stage == "weekly"
    assert rs.model_stage == "hols"
    assert rs.model.resolved == "mmm.hierarchical_geo@1.3.2"  # 1.4.0 is yanked
    assert [s.resolved for s in rs.steps][:2] == ["core.fx_convert@1.0.0", "core.resample@1.2.0"]
    assert rs.model.effective_policies["geo"] is DimPolicy.FIXED  # tightened by the spec
    assert rs.model.effective_policies["time"] is DimPolicy.EXTEND
    assert rs.model.input_schema is not None
    assert rs.model.input_schema.dims["geo"].new_level_strategy == "error"
    assert rs.spec_hash == spec_hash(ModelSpec.model_validate(_e33()))
    assert rs.last_reshape_stage == "weekly"
    assert [s.id for s in rs.steps_after("scale_spend")] == ["scale_y", "lags", "season", "hols"]
    assert rs.steps_after("raw") == rs.steps and rs.steps_after("ghost") == []
    lags = next(s for s in rs.steps if s.id == "lags")
    assert lags.context is not None and lags.context.n == 6 and lags.context.within == ["geo"]
    assert not lags.depends_on_target  # lags spend, not the target
    assert next(s for s in rs.steps if s.id == "to_eur").side_tables == ["fx"]
    # stage schemas: units EUR after fx, scaled after mean_scale, lag dim after lags
    st = rs.stages
    assert st["to_eur"] is not None and st["to_eur"].vars["spend"].units == "EUR"
    assert st["scale_spend"] is not None and st["scale_spend"].vars["spend"].units == "1"
    assert st["lags"] is not None and "lag" in st["lags"].vars["spend"].dims
    assert st["weekly"] is not None and st["weekly"].dims["time"].freq == "W-SUN"
    # ResolvedSpec is plain JSON the control plane can store
    assert ResolvedSpec.model_validate_json(rs.model_dump_json()) == rs


def test_xgb_validates(catalog: Catalog, xgb_raw: DataSchema) -> None:
    res = validate_spec(ModelSpec.model_validate(parse_yaml(XGB_YAML)), catalog, xgb_raw)
    assert res.ok, res.issues
    rs = res.raise_for_errors()
    assert rs.split_stage == "raw"  # imp is stateful, so the stateless prefix is empty
    plags = next(s for s in rs.steps if s.id == "plags")
    assert not plags.depends_on_target
    assert plags.context is not None and plags.context.min_lag == 1 and plags.context.n == 2
    assert rs.stages["plags"] is not None and "promo_lagged" in rs.stages["plags"].vars


def test_semver_resolution_picks_highest_matching(catalog: Catalog) -> None:
    d = _e33()
    d["pipeline"][1]["use"] = "core.resample@^2"
    rs = _run(catalog, d).raise_for_errors()
    assert rs.steps[1].resolved == "core.resample@2.0.0"
    d["model"]["use"] = "mmm.hierarchical_geo@~1.2"
    assert _run(catalog, d).raise_for_errors().model.resolved == "mmm.hierarchical_geo@1.2.0"


# ------------------------------------------------------------------------------ references


def test_unresolved_refs(catalog: Catalog) -> None:
    d = _e33()
    d["pipeline"][1]["use"] = "core.resample@^9"
    d["model"]["use"] = "mmm.unknown@^1"
    d["outputs"]["plots"].append("core.nope")
    d["outputs"]["derived"].append("roi")
    res = _run(catalog, d)
    assert not res.ok and res.resolved is None
    ptrs = {i.pointer: i for i in res.issues if i.code is ErrorCode.REF_UNRESOLVED}
    assert "available: 1.0.0, 1.2.0, 2.0.0" in ptrs["/pipeline/1/use"].message
    assert "unknown model" in ptrs["/model/use"].message
    # once a step is unresolved, later stages are unknown
    assert all(s.schema is None for s in res.stages[2:])
    with pytest.raises(ValidationFailed):
        res.raise_for_errors()


def test_unknown_plot_derived_and_side_table(catalog: Catalog) -> None:
    d = _e33()
    d["outputs"]["plots"].append("core.nope")
    d["outputs"]["derived"].append("roi")
    d["pipeline"][0]["with"]["table"] = "fx_rates"
    res = _run(catalog, d)
    pointers = {i.pointer for i in res.issues if i.code is ErrorCode.REF_UNRESOLVED}
    assert pointers == {"/outputs/plots/4", "/outputs/derived/3", "/pipeline/0/with/table"}


def test_unknown_step_id(catalog: Catalog) -> None:
    d = _e33()
    d["pipeline"][0]["use"] = "core.teleport@^1"
    res = _run(catalog, d)
    assert _find(res, ErrorCode.REF_UNRESOLVED).message == "unknown step 'core.teleport'"


# --------------------------------------------------------------------------------- settings


def test_step_settings_validation_pointers(catalog: Catalog) -> None:
    d = _e33()
    d["pipeline"][1]["with"]["how"] = "median"
    d["pipeline"][1]["with"]["bogus"] = 1
    del d["pipeline"][1]["with"]["freq"]
    d["pipeline"][0]["with"]["to"] = "euro"
    res = _run(catalog, d)
    issues = [i for i in res.issues if i.code is ErrorCode.SETTINGS_INVALID]
    pointers = {i.pointer for i in issues}
    assert {"/pipeline/1/with/how", "/pipeline/1/with/freq", "/pipeline/0/with/to"} <= pointers
    assert any(i.pointer == "/pipeline/1/with" and i.message == "unknown setting" for i in issues)
    how = next(i for i in issues if i.pointer == "/pipeline/1/with/how")
    assert how.message == "value is not one of the allowed options"
    assert how.untrusted_text is not None and "median" in how.untrusted_text
    assert "median" not in how.message


def test_model_settings_and_fit_validation(catalog: Catalog) -> None:
    d = _e33()
    d["model"]["settings"]["pooling"] = "maximal"
    d["model"]["settings"]["unknown_knob"] = True
    d["model"]["fit"]["draws"] = 20000
    d["model"]["fit"]["engine"] = "stan"
    res = _run(catalog, d)
    pointers = {i.pointer for i in res.issues if i.code is ErrorCode.SETTINGS_INVALID}
    assert {
        "/model/settings/pooling",
        "/model/settings",
        "/model/fit/draws",
        "/model/fit/engine",
    } <= pointers


def test_apply_defaults_and_validate_settings_helpers() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"default": 1},
            "nested": {"type": "object", "properties": {"b": {"default": 2}}, "default": {}},
        },
    }
    assert apply_defaults(schema, {}) == {"a": 1, "nested": {"b": 2}}
    assert apply_defaults({}, {"x": 1}) == {"x": 1}
    assert apply_defaults({"properties": {"a": 3}}, {}) == {}
    assert validate_settings({}, {"anything": 1}, "/x") == []
    issues = validate_settings(
        {"type": "object", "required": ["a", "b"]}, {"a": 1}, "/p", code=ErrorCode.SCHEMA_INVALID
    )
    assert [i.pointer for i in issues] == ["/p/b"]
    many = validate_settings({"type": "array", "items": {"type": "integer"}}, ["x"] * 80, "/arr")
    assert len(many) == 50


# -------------------------------------------------------------------------- binding/schema


def test_binding_errors(catalog: Catalog) -> None:
    d = _e33()
    d["data"]["bind"]["dims"]["geo"] = "region"
    del d["data"]["bind"]["dims"]["channel"]
    d["data"]["bind"]["vars"]["spend"] = "channel"
    res = _run(catalog, d)
    msgs = {i.pointer: i.message for i in res.issues if i.code is ErrorCode.BINDING_INVALID}
    assert msgs["/data/bind/dims/geo"] == "dataset has no dim named 'region'"
    assert "it is a dim" in msgs["/data/bind/vars/spend"]
    assert res.stages[0].schema is None


def test_binding_collision(catalog: Catalog) -> None:
    raw = copy.deepcopy(E33_RAW)
    raw["vars"]["y"] = {"dims": ["market"], "dtype": "float64"}
    res = _run(catalog, _e33(), raw)
    issue = _find(res, ErrorCode.BINDING_INVALID)
    assert issue.pointer == "/data/bind/vars/y" and "collides" in issue.message


def test_step_input_checks(catalog: Catalog) -> None:
    d = _e33()
    d["pipeline"][2]["with"]["vars"] = ["spend", "budget"]
    d["pipeline"][2]["with"]["over"] = ["week"]
    res = _run(catalog, d)
    issues = [i for i in res.issues if i.code is ErrorCode.SCHEMA_INVALID]
    pointers = {i.pointer for i in issues}
    assert pointers >= {"/pipeline/2/with/vars/1", "/pipeline/2/with/over/0"}
    assert "does not exist at this step's input stage" in issues[0].message


def test_context_dims_checked(catalog: Catalog) -> None:
    d = _e33()
    d["pipeline"][4]["with"]["within"] = ["store"]
    res = _run(catalog, d)
    assert any(i.pointer == "/pipeline/4/with/within/0" for i in res.issues)


def test_effect_failure_reported_at_step(catalog: Catalog) -> None:
    d = _e33()
    # fourier adds 'fourier'; a second fourier with the same out name collides
    d["pipeline"].append(
        {"id": "season2", "use": "core.fourier@^1", "with": dict(d["pipeline"][5]["with"])}
    )
    res = _run(catalog, d)
    issue = _find(res, ErrorCode.SCHEMA_INVALID)
    assert issue.pointer == "/pipeline/7" and "already exists" in issue.message


def test_dynamic_step_stops_static_checks(catalog: Catalog) -> None:
    d = _e33()
    d["pipeline"].insert(6, {"id": "dyn", "use": "core.dyn@^1", "with": {}})
    res = _run(catalog, d)
    dyn = _find(res, ErrorCode.SCHEMA_DYNAMIC)
    assert dyn.severity is Severity.WARNING and dyn.pointer == "/pipeline/6"
    assert res.stages[-1].schema is None
    assert res.ok  # a warning, not an error


def test_model_schema_mismatch(catalog: Catalog) -> None:
    d = _e33()
    d["pipeline"] = [p for p in d["pipeline"] if p["id"] != "season"]
    d["model"]["settings"]["controls"] = ["price"]
    res = _run(catalog, d)
    msgs = [i.message for i in res.issues if i.code is ErrorCode.SCHEMA_INVALID]
    assert any("needs dim 'fourier_mode'" in m for m in msgs)
    assert any("needs variable 'fourier'" in m for m in msgs)
    assert any("needs variable 'price'" in m for m in msgs)

    d = _e33()
    d["pipeline"] = [p for p in d["pipeline"] if p["id"] != "lags"]
    res = _run(catalog, d)
    msgs = [i.message for i in res.issues if i.code is ErrorCode.SCHEMA_INVALID]
    assert any("variable 'spend': the model needs dims" in m for m in msgs)

    d = _e33()
    d["pipeline"][1]["with"]["freq"] = "D"
    res = _run(catalog, d)
    assert any("frequency 'W-SUN'" in i.message for i in res.issues)


def test_model_dtype_kind_and_role_mismatch(catalog: Catalog) -> None:
    raw = copy.deepcopy(E33_RAW)
    raw["vars"]["revenue_local"]["dtype"] = "str"
    raw["vars"]["revenue_local"]["role"] = "covariate"
    raw["dims"]["channel"] = {"kind": "integer"}
    res = _run(catalog, _e33(), raw)
    msgs = [(i.severity, i.message) for i in res.issues]
    assert any("dtype float64" in m for s, m in msgs if s is Severity.ERROR)
    assert any("expects role 'target'" in m for s, m in msgs if s is Severity.WARNING)
    assert any("kind 'categorical'" in m for _, m in msgs)


def test_template_resolution_failure(catalog: Catalog) -> None:
    d = _e33()
    d["model"]["settings"]["controls"] = ["y"]  # duplicate var in the template
    res = _run(catalog, d)
    issue = _find(res, ErrorCode.SCHEMA_INVALID)
    assert issue.pointer == "/model/settings" and "template" in issue.message


# ---------------------------------------------------------------------------- dim policies


def test_dim_policy_loosening_rejected(catalog: Catalog) -> None:
    d = _e33()
    d["model"]["settings"]["dim_policies"] = {"channel": "new_levels", "time": "fixed"}
    res = _run(catalog, d)
    issue = _find(res, ErrorCode.DIM_POLICY_LOOSENED)
    assert issue.pointer == "/model/settings/dim_policies/channel"
    assert len([i for i in res.issues if i.code is ErrorCode.DIM_POLICY_LOOSENED]) == 1


def test_dim_policy_extend_vs_new_levels_and_unknown_dim(catalog: Catalog) -> None:
    d = _e33()
    d["model"]["settings"]["dim_policies"] = {"geo": "extend", "store": "fixed"}
    res = _run(catalog, d)
    assert _find(res, ErrorCode.DIM_POLICY_LOOSENED).pointer.endswith("/geo")
    assert any(
        i.pointer == "/model/settings/dim_policies/store" and i.code is ErrorCode.SCHEMA_INVALID
        for i in res.issues
    )


def test_data_policy_tightens_plugin_policy(catalog: Catalog) -> None:
    raw = copy.deepcopy(E33_RAW)
    raw["dims"]["market"] = {"kind": "categorical", "policy": "fixed"}
    d = _e33()
    del d["model"]["settings"]["dim_policies"]
    rs = _run(catalog, d, raw).raise_for_errors()
    assert rs.model.effective_policies["geo"] is DimPolicy.FIXED
    # and the spec cannot loosen it back to the plugin's new_levels
    d["model"]["settings"]["dim_policies"] = {"geo": "new_levels"}
    assert ErrorCode.DIM_POLICY_LOOSENED.value in _codes(_run(catalog, d, raw))


def test_new_levels_kept_when_not_tightened(catalog: Catalog) -> None:
    d = _e33()
    del d["model"]["settings"]["dim_policies"]
    rs = _run(catalog, d).raise_for_errors()
    assert rs.model.effective_policies["geo"] is DimPolicy.NEW_LEVELS
    assert rs.model.input_schema is not None
    assert rs.model.input_schema.dims["geo"].new_level_strategy == "hyperprior"


# ------------------------------------------------------------------------- split & leakage


def test_default_split_stage_is_end_of_stateless_prefix(catalog: Catalog) -> None:
    d = _e33()
    del d["data"]["split"]["after_step"]
    rs = _run(catalog, d).raise_for_errors()
    assert rs.split_stage == "weekly"


def test_leakage_risk(catalog: Catalog) -> None:
    d = _e33()
    d["data"]["split"]["after_step"] = "scale_y"
    res = _run(catalog, d)
    leaks = [i for i in res.issues if i.code is ErrorCode.LEAKAGE_RISK]
    assert len(leaks) == 2  # scale_spend and scale_y run before the split
    assert leaks[0].pointer == "/data/split/after_step"
    assert "scale_spend" in leaks[0].message and leaks[0].severity is Severity.ERROR
    assert res.resolved is None

    d["data"]["split"]["allow_leakage"] = True
    res = _run(catalog, d)
    assert res.ok and all(
        i.severity is Severity.WARNING for i in res.issues if i.code is ErrorCode.LEAKAGE_RISK
    )


def test_split_dim_must_exist_at_split_stage(catalog: Catalog) -> None:
    """E33 with resample renaming date -> week: 'week' only exists after resampling."""
    d = _e33()
    d["data"]["bind"]["dims"] = {"geo": "market", "channel": "channel"}
    d["pipeline"][1] = {
        "id": "weekly",
        "use": "core.resample_to@^1",
        "with": {"dim": "date", "to": "time", "freq": "W-SUN"},
    }
    d["data"]["split"]["after_step"] = "raw"
    res = _run(catalog, d)
    issue = _find(res, ErrorCode.SPLIT_INVALID)
    assert issue.pointer == "/data/split/dim"
    assert "does not exist at stage 'raw'" in issue.message
    assert issue.suggested_action and "after_step" in issue.suggested_action
    # the default stage (end of the stateless prefix) is after the resample: valid
    del d["data"]["split"]["after_step"]
    rs = _run(catalog, d).raise_for_errors()
    assert rs.split_stage == "weekly"
    assert rs.stages["raw"] is not None and "date" in rs.stages["raw"].dims


def test_split_variants_dims_checked(catalog: Catalog) -> None:
    d = _e33()
    d["data"]["split"] = {"kind": "last_per_group", "dim": "time", "within": ["store"]}
    res = _run(catalog, d)
    assert _find(res, ErrorCode.SPLIT_INVALID).pointer == "/data/split/within/0"
    d["data"]["split"] = {"kind": "mask_block", "dims": ["geo", "sku"], "fraction": 0.2}
    res = _run(catalog, d)
    assert _find(res, ErrorCode.SPLIT_INVALID).pointer == "/data/split/dims/1"
    d["data"]["split"] = {"kind": "time_holdout", "dim": "channel", "last": 2}
    res = _run(catalog, d)
    assert "ordered dim" in _find(res, ErrorCode.SPLIT_INVALID).message
    d["data"]["split"] = {"kind": "leave_group_out", "dim": "geo", "groups": ["DE"]}
    assert _run(catalog, d).ok


def test_no_split_with_evaluate_then_refit_warns(catalog: Catalog) -> None:
    d = _e33()
    del d["data"]["split"]
    res = _run(catalog, d)
    assert res.ok
    w = _find(res, ErrorCode.SPLIT_INVALID)
    assert w.severity is Severity.WARNING and w.pointer == "/data"
    d["model"]["fit"]["mode"] = "train_only"
    assert _run(catalog, d).issues == []


# ------------------------------------------------------------------------------ recursion


def _target_lag_spec(last: int, lags: list[int]) -> dict[str, Any]:
    d = _e33()
    d["pipeline"].insert(
        4,
        {
            "id": "ylag",
            "use": "core.lag@^1",
            "with": {"vars": ["y"], "along": "time", "within": ["geo"], "lags": lags},
        },
    )
    d["data"]["split"]["last"] = last
    return d


def test_recursive_forecasting_rejected(catalog: Catalog) -> None:
    res = _run(catalog, _target_lag_spec(8, [1, 2]))
    issue = _find(res, ErrorCode.RECURSIVE_UNSUPPORTED)
    assert issue.pointer == "/pipeline/4" and issue.severity is Severity.ERROR
    assert "lags the target by 1" in issue.message
    assert res.resolved is None


def test_recursion_ok_when_lag_covers_holdout(catalog: Catalog) -> None:
    rs = _run(catalog, _target_lag_spec(8, [8, 12])).raise_for_errors()
    ylag = next(s for s in rs.steps if s.id == "ylag")
    assert ylag.depends_on_target and ylag.context is not None and ylag.context.min_lag == 8


def test_recursion_ok_when_model_owns_it(catalog: Catalog) -> None:
    data = catalog.model_dump(mode="json")
    for m in data["models"]:
        if m["manifest"]["id"] == "mmm.hierarchical_geo":
            m["manifest"]["capabilities"]["owns"] = ["recursion"]
    owning = Catalog.model_validate(data)
    assert _run(owning, _target_lag_spec(8, [1])).ok


def test_recursion_unknown_horizon_warns(catalog: Catalog) -> None:
    d = _target_lag_spec(8, [1])
    d["data"]["split"] = {
        "kind": "as_of",
        "dim": "time",
        "as_of": "2026-06-07",
        "after_step": "weekly",
    }
    res = _run(catalog, d)
    w = _find(res, ErrorCode.RECURSIVE_UNSUPPORTED)
    assert w.severity is Severity.WARNING


# --------------------------------------------------------------------- capabilities etc.


def test_point_model_capability_checks(catalog: Catalog, xgb_raw: DataSchema) -> None:
    data = parse_yaml(XGB_YAML)
    assert isinstance(data, dict)
    data["model"]["use"] = "sklearn.point_regressor@^1"
    data["evaluate"]["metrics"] = ["rmse", "coverage_80", "elpd_loo", "made_up"]
    data["outputs"]["plots"] = ["core.ppc_fan"]
    res = validate_spec(ModelSpec.model_validate(data), catalog, xgb_raw)
    by_ptr = {i.pointer: i for i in res.issues}
    assert by_ptr["/evaluate/metrics/1"].code is ErrorCode.CAPABILITY_UNSUPPORTED
    elpd = [i for i in res.issues if i.pointer == "/evaluate/metrics/2"]
    assert {i.code for i in elpd} == {ErrorCode.CAPABILITY_UNSUPPORTED}
    assert any(i.untrusted_text == "no likelihood" for i in elpd)
    assert by_ptr["/evaluate/metrics/3"].severity is Severity.WARNING
    assert by_ptr["/outputs/plots/0"].code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert by_ptr["/execution/resource_class"].code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_target_dependence_after_dynamic_step(catalog: Catalog) -> None:
    """Behind a dynamic step, lags of non-target vars are still not treated as recursive."""
    d = _e33()
    d["pipeline"].insert(4, {"id": "dyn", "use": "core.dyn@^1", "with": {}})
    lag = {"vars": ["spend"], "along": "time", "within": ["geo"], "lags": [1]}
    d["pipeline"].insert(5, {"id": "slag", "use": "core.lag@^1", "with": lag})
    res = _run(catalog, d)
    assert ErrorCode.RECURSIVE_UNSUPPORTED.value not in _codes(res, None)
    lag["vars"] = ["y"]
    res = _run(catalog, d)
    assert ErrorCode.RECURSIVE_UNSUPPORTED.value in _codes(res)
