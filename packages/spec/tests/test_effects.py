from __future__ import annotations

from typing import Any

import pytest
from mapi_spec import Catalog, DataSchema
from mapi_spec.effects import (
    ContextNeed,
    SchemaEffect,
    VarDerive,
    VarPatch,
    apply_effect,
    check_effect_template,
    propagate,
    resolve_context,
    resolve_effect,
)
from mapi_spec.errors import ErrorCode, Severity
from mapi_spec.schema import DimPolicy, Role
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _schema() -> DataSchema:
    return DataSchema.model_validate(
        {
            "dims": {
                "geo": {
                    "kind": "categorical",
                    "policy": "new_levels",
                    "new_level_strategy": "pooled",
                },
                "channel": {"kind": "categorical"},
                "date": {"kind": "time", "policy": "extend", "freq": "D"},
            },
            "vars": {
                "y": {"dims": ["geo", "date"], "dtype": "float64", "role": "target"},
                "spend": {"dims": ["geo", "channel", "date"], "dtype": "float64", "role": "spend"},
            },
        }
    )


def _apply(effect: dict[str, Any]) -> tuple[DataSchema | None, list[str]]:
    return apply_effect(_schema(), SchemaEffect.model_validate(effect))


def test_identity_effect() -> None:
    eff = SchemaEffect()
    assert eff.is_identity
    out, problems = apply_effect(_schema(), eff)
    assert out == _schema() and problems == []


def test_renames_dims_and_vars() -> None:
    out, problems = _apply({"renames": {"date": "week", "y": "revenue"}})
    assert problems == []
    assert out is not None
    assert "week" in out.dims and "revenue" in out.vars
    assert out.vars["spend"].dims == ("geo", "channel", "week")
    out, problems = _apply({"renames": {"y": "spend"}})
    assert out is None and "already exists" in problems[0]
    _, problems = _apply({"renames": {"nope": "x"}})
    assert "neither a dim nor a variable" in problems[0]
    out, problems = _apply({"renames": {"y": "spend", "spend": "y"}})  # swap is fine
    assert problems == [] and out is not None and out.vars["y"].role is Role.SPEND


def test_drops() -> None:
    out, _ = _apply({"drops": ["spend"]})
    assert out is not None and "spend" not in out.vars
    out, problems = _apply({"drops": ["channel"]})
    assert out is None and "still used" in problems[0]
    out, problems = _apply({"drops": ["spend", "channel"]})
    assert problems == [] and out is not None and "channel" not in out.dims
    _, problems = _apply({"drops": ["ghost"]})
    assert problems


def test_transforms_dims_add_and_replace() -> None:
    out, _ = _apply(
        {
            "transforms_dims": {
                "date": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                "lag": {"kind": "integer"},
            }
        }
    )
    assert out is not None and out.dims["date"].freq == "W-SUN" and "lag" in out.dims
    _, problems = _apply({"transforms_dims": {"y": {"kind": "integer"}}})
    assert "is a variable" in problems[0]


def test_stacks() -> None:
    out, problems = _apply({"stacks": {"obs": ["geo", "date"]}})
    assert problems == [] and out is not None
    assert out.vars["y"].dims == ("obs",)
    assert out.vars["spend"].dims == ("obs", "channel")
    assert out.dims["obs"].kind == "index" and out.dims["obs"].policy is DimPolicy.EXTEND
    out, _ = _apply({"stacks": {"cell": ["geo", "channel"]}})
    assert out is None
    _, problems = _apply({"stacks": {"cell": ["geo", "channel"]}})
    assert "only some components" in problems[0]
    _, problems = _apply({"stacks": {"obs": ["geo", "nope"]}})
    assert "unknown component" in problems[0]
    _, problems = _apply({"stacks": {"y": ["geo", "date"]}})
    assert "already exists" in problems[0]
    with pytest.raises(ValidationError, match="at least two"):
        SchemaEffect.model_validate({"stacks": {"obs": ["geo"]}})


def test_updates_and_derives() -> None:
    out, problems = _apply(
        {
            "transforms_dims": {"lag": {"kind": "integer"}},
            "updates": {"spend": {"units": "EUR", "append_dims": ["lag"]}},
            "derives": {"y_lag": {"from": "y", "append_dims": ["lag"], "role": "covariate"}},
        }
    )
    assert problems == [] and out is not None
    assert out.vars["spend"].dims == ("geo", "channel", "date", "lag")
    assert out.vars["spend"].units == "EUR"
    assert out.vars["y_lag"].role is Role.COVARIATE and out.vars["y_lag"].dims[-1] == "lag"
    _, problems = _apply({"updates": {"ghost": {"units": "x"}}})
    assert "unknown variable" in problems[0]
    _, problems = _apply({"updates": {"y": {"remove_dims": ["channel"]}}})
    assert "does not use it" in problems[0]
    _, problems = _apply({"updates": {"y": {"append_dims": ["geo"]}}})
    assert "already uses it" in problems[0]
    _, problems = _apply({"derives": {"z": {"from": "ghost"}}})
    assert "unknown source" in problems[0]
    _, problems = _apply({"derives": {"spend": {"from": "y"}}})
    assert "already exists" in problems[0]
    out, problems = _apply({"updates": {"y": {"append_dims": ["undeclared"]}}})
    assert out is None and "resulting schema is invalid" in problems[0]


def test_adds() -> None:
    out, _ = _apply({"adds": {"trend": {"dims": ["date"], "dtype": "float64"}}})
    assert out is not None and "trend" in out.vars
    _, problems = _apply({"adds": {"y": {"dims": ["date"], "dtype": "float64"}}})
    assert "use updates" in problems[0]


def test_effect_model_validation() -> None:
    with pytest.raises(ValidationError, match="duplicate drops"):
        SchemaEffect(drops=["a", "a"])
    with pytest.raises(ValidationError, match="same target"):
        SchemaEffect(renames={"a": "c", "b": "c"})
    with pytest.raises(ValidationError, match="repeats"):
        SchemaEffect.model_validate({"stacks": {"o": ["a", "a"]}})
    VarPatch(units="1")
    assert VarDerive.model_validate({"from": "y"}).from_ == "y"


def test_effect_templates() -> None:
    raw = {
        "template_params": ["vars", "to"],
        "updates": {"{*vars as v}": {"units": "{to}"}},
    }
    check_effect_template(raw)
    eff = resolve_effect(raw, {"vars": ["y", "spend"], "to": "EUR", "table": "fx"})
    assert set(eff.updates) == {"y", "spend"} and eff.template_params == ["vars", "to"]
    with pytest.raises(ValueError, match="not listed in template_params"):
        check_effect_template({"updates": {"{*vars as v}": {"units": "x"}}})
    with pytest.raises(ValueError, match="unknown SchemaEffect fields"):
        check_effect_template({"bogus": 1})
    with pytest.raises(ValueError, match="template_params must"):
        check_effect_template({"template_params": "vars"})
    with pytest.raises(ValidationError):
        check_effect_template({"drops": [1]})
    se = SchemaEffect()
    assert resolve_effect(se, {}) is se


def test_context_templates() -> None:
    ctx = resolve_context(
        {"along": "{along}", "n": "{max:lags}", "within": "{within}", "min_lag": "{min:lags}"},
        {"along": "time", "lags": [2, 4], "within": ["geo"]},
    )
    assert ctx == ContextNeed(along="time", n=4, within=["geo"], min_lag=2)
    assert resolve_context(None, {}) is None
    assert resolve_context(ctx, {}) is ctx


def test_propagate_dynamic_and_errors() -> None:
    res = propagate(
        _schema(),
        [
            ("a", {"adds": {"t": {"dims": ["date"], "dtype": "float64"}}}, {}),
            ("b", {"dynamic": True}, {}),
            ("c", {}, {}),
        ],
    )
    assert [s.name for s in res.stages] == ["raw", "a", "b", "c"]
    assert res.stages[1].schema is not None and res.schemas[2:] == [None, None]
    assert res.issues[0].code is ErrorCode.SCHEMA_DYNAMIC
    assert res.issues[0].severity is Severity.WARNING and res.issues[0].pointer == "/pipeline/1"
    assert res.ok and res.final is None
    assert res.stage("a") is not None
    with pytest.raises(KeyError):
        res.stage("zz")

    res = propagate(
        _schema(),
        [
            ("bad_tpl", {"template_params": ["v"], "drops": ["{v}"]}, {}),
            ("after", {}, {}),
        ],
        pointer_base="/x",
    )
    assert not res.ok and res.issues[0].pointer == "/x/0"
    assert "could not resolve" in res.issues[0].message
    res = propagate(_schema(), [("d", {"drops": ["ghost"]}, {})], pointers=["/p"])
    assert res.issues[0].pointer == "/p" and res.final is None
    res = propagate(_schema(), [("d", {"adds": {"x": {"dims": [], "dtype": "nope"}}}, {})])
    assert not res.ok


def test_e33_like_propagation_with_catalog_effects(catalog: Catalog) -> None:
    """fx_convert → resample(date→week) → mean_scale → lag_tensor → fourier."""
    raw = DataSchema.model_validate(
        {
            "dims": {
                "geo": {
                    "kind": "categorical",
                    "policy": "new_levels",
                    "new_level_strategy": "pooled",
                },
                "channel": {"kind": "categorical"},
                "date": {"kind": "time", "policy": "extend", "freq": "D"},
            },
            "vars": {
                "y": {
                    "dims": ["geo", "date"],
                    "dtype": "float64",
                    "role": "target",
                    "units": "SEK",
                },
                "spend": {
                    "dims": ["geo", "channel", "date"],
                    "dtype": "float64",
                    "role": "spend",
                    "units": "SEK",
                    "future_required": True,
                },
            },
        }
    )
    plan: list[tuple[str, str, dict[str, Any]]] = [
        ("to_eur", "core.fx_convert@^1", {"vars": ["y", "spend"], "table": "fx", "to": "EUR"}),
        ("weekly", "core.resample_to@^1", {"dim": "date", "to": "week", "freq": "W-SUN"}),
        ("scale", "core.mean_scale@^1", {"vars": ["spend"], "over": ["week"]}),
        ("lags", "core.lag_tensor@^1", {"vars": ["spend"], "along": "week", "l_max": 6}),
        ("season", "core.fourier@^1", {"along": "week", "period": 52.18, "order": 3}),
    ]
    steps = []
    for sid, use, settings in plan:
        entry = catalog.resolve_step(use)
        assert entry is not None
        full = {**settings}
        if sid == "season":
            full.setdefault("out", "fourier")
        steps.append((sid, entry.effect, full))
    res = propagate(raw, steps)
    assert res.ok, res.issues
    to_eur, weekly, scale, lags, season = (s.schema for s in res.stages[1:])
    assert to_eur is not None and to_eur.vars["y"].units == "EUR"
    assert weekly is not None and "date" not in weekly.dims
    assert weekly.dims["week"].freq == "W-SUN"
    assert weekly.vars["spend"].dims == ("geo", "channel", "week")
    assert scale is not None and scale.vars["spend"].units == "1"
    assert scale.vars["y"].units == "EUR"
    assert lags is not None and lags.vars["spend"].dims == ("geo", "channel", "week", "lag")
    assert season is not None and season.vars["fourier"].dims == ("week", "fourier_mode")
    assert season.vars["spend"].future_required
