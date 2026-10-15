"""Step contract primitives, StepContext helpers and the registry."""

from __future__ import annotations

import importlib.metadata
from typing import Any, ClassVar

import mapi_sdk.registry as registry_module
import numpy as np
import pytest
import xarray as xr
from mapi_sdk.errors import RunCancelled, StepError
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.steps import CancelToken, Step, StepContext, StepSettings, StepState, states_equal
from mapi_sdk.testing import toy
from mapi_spec import Catalog, StepKind
from mapi_spec.catalog import PlotCatalogEntry
from mapi_spec.errors import CoordPlanError, ErrorCode, MapiError
from pydantic import JsonValue


def _scale(levels: list[str], values: list[float]) -> xr.DataArray:
    return xr.DataArray(values, dims=["geo"], coords={"geo": levels})


def test_align_state_policies() -> None:
    state = _scale(["a", "b"], [2.0, 4.0])
    ds = xr.Dataset(coords={"geo": ["b", "c", "a"]})
    ctx = StepContext(step_id="s")
    with pytest.raises(CoordPlanError) as err:
        ctx.align_state(state, ds, "error")
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    assert err.value.untrusted_text == "c"
    pooled = ctx.align_state(state, ds, "pooled")
    assert pooled.sel(geo="c").item() == 3.0
    assert list(pooled.geo.values) == ["b", "c", "a"]
    custom = ctx.align_state(state, ds, "pooled", pooled=10.0)
    assert custom.sel(geo="c").item() == 10.0
    fill = ctx.align_state(state, ds, "fill", fill_value=1.0)
    assert fill.sel(geo="c").item() == 1.0 and fill.sel(geo="a").item() == 2.0
    subset = ctx.align_state(state, xr.Dataset(coords={"geo": ["b"]}), "error")
    assert subset.values.tolist() == [4.0]
    untouched = ctx.align_state(state, xr.Dataset(coords={"x": [1]}), "error")
    assert untouched.identical(state)


def test_context_side_tables_policy_and_cancel() -> None:
    ctx = StepContext(step_id="s", side_tables={"fx": xr.Dataset()})
    assert isinstance(ctx.side_table("fx"), xr.Dataset)
    with pytest.raises(MapiError) as err:
        ctx.side_table("missing")
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE
    assert ctx.policy_for("geo") is None
    logs: list[str] = []
    token = CancelToken()
    ctx2 = StepContext(step_id="s", log=logs.append, cancel=token)
    ctx2.info("hello")
    ctx2.check_cancelled()
    token.cancel("user asked")
    assert token.cancelled
    with pytest.raises(RunCancelled, match="user asked"):
        ctx2.check_cancelled()
    assert logs == ["hello"]


def test_state_payload_roundtrip_and_equality() -> None:
    st = toy.FourierState(
        step_id="season",
        origin="2024-01-07T00:00:00",
        step_ns=7,
        arrays=xr.Dataset({"a": (("x",), [1.0])}),
    )
    payload = st.json_payload()
    assert payload["origin"] == "2024-01-07T00:00:00"
    assert "arrays" not in payload
    back = toy.FourierState.from_payload(payload, st.arrays, None)
    assert states_equal(st, back)
    assert not states_equal(st, back.model_copy(update={"step_ns": 8}))
    assert not states_equal(st, back.model_copy(update={"arrays": None}))
    assert not states_equal(
        st, back.model_copy(update={"arrays": xr.Dataset({"a": (("x",), [2.0])})})
    )
    assert not states_equal(st, StepState())
    with pytest.raises(StepError):
        toy.FourierState.from_payload({"bogus": 1})
    with pytest.raises(StepError, match="no array"):
        st.array("nope")


def test_catalog_entries_validate_and_effects_render() -> None:
    for cls in (
        toy.FxStep,
        toy.ResampleStep,
        toy.ScaleStep,
        toy.Log1pStep,
        toy.LagStep,
        toy.FourierStep,
    ):
        entry = cls.catalog_entry()
        assert entry.ref == cls.ref()
        assert entry.stateful is cls.stateful()
        assert entry.settings_schema["type"] == "object"
    step = toy.LagStep()
    s = step.parse_settings({"vars": ["spend"], "within": ["geo"], "lags": [1, 3]})
    need = step.context(s)
    assert need is not None and (need.along, need.n, need.within, need.min_lag) == (
        "time",
        3,
        ["geo"],
        1,
    )
    eff = step.effect(s)
    assert "spend_lag" in eff.derives
    assert step.touched_vars(s) == set()
    scale = toy.ScaleStep()
    ss = scale.parse_settings({"vars": ["y"]})
    assert scale.touched_vars(ss) == {"y"}
    assert scale.parse_settings(ss) is ss
    assert scale.settings_hash(ss).startswith("sha256:")
    with pytest.raises(MapiError) as err:
        scale.parse_settings({"vars": ["y"], "bogus": 1})
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    assert toy.Log1pStep().context(toy.Log1pStep().parse_settings({"vars": ["y"]})) is None


class _Bare(Step[StepSettings, StepState]):
    id = "test.bare"
    version = "0.1.0"
    kind = StepKind.VALUE
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["missing"],
        "updates": {"{missing}": {}},
    }


def test_base_step_defaults() -> None:
    step = _Bare()
    s = step.parse_settings(None)
    assert isinstance(step.fit(xr.Dataset(), s, StepContext("b")), StepState)
    with pytest.raises(NotImplementedError):
        step.transform(xr.Dataset(), s, StepState(), StepContext("b"))
    with pytest.raises(StepError, match="not invertible"):
        step.inverse(xr.Dataset(), s, StepState(), StepContext("b"))
    assert step.touched_vars(s) == set()  # effect cannot render; no var params


def test_registry_resolution_and_catalog() -> None:
    reg = toy.toy_registry()
    assert reg.step("toy.scale@^1") is toy.ScaleStep
    assert reg.step("toy.scale@1.0.0") is toy.ScaleStep
    assert reg.model("toy.linear@^1") is toy.LinearModel
    with pytest.raises(MapiError) as err:
        reg.step("toy.scale@^2")
    assert "available: 1.0.0" in err.value.detail
    with pytest.raises(MapiError):
        reg.model("nope.model@^1")
    cat = reg.catalog()
    assert isinstance(cat, Catalog)
    assert cat.resolve_step("toy.lag@^1") is not None
    assert cat.resolve_model("toy.bayes_linear@^1") is not None
    copy = reg.copy()
    assert copy.step("toy.lag@^1") is toy.LagStep
    with pytest.raises(ValueError, match="already registered"):

        class Other(toy.ScaleStep):
            pass

        reg.register_step(Other)
    reg.register_step(toy.ScaleStep)  # same object again is fine
    with pytest.raises(TypeError):
        reg.register(42)
    with pytest.raises(TypeError):
        reg.register_step(int)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        reg.register_model(int)


class _Plot:
    @classmethod
    def catalog_entry(cls) -> PlotCatalogEntry:
        return PlotCatalogEntry(id="core.test_plot", title="Test")

    def render(self, data: xr.DataTree, **options: Any) -> Any:
        return None


class _BadPlot(_Plot):
    @classmethod
    def catalog_entry(cls) -> Any:
        return {"id": "x"}


def test_registry_plots_and_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_default", None)
    reg = Registry()
    reg.register(_Plot())
    assert isinstance(reg.plot("core.test_plot@^1"), _Plot)
    assert reg.catalog().plots[0].id == "core.test_plot"
    with pytest.raises(TypeError):
        reg.register_plot(_BadPlot())

    class EP:
        def __init__(self, name: str, obj: Any) -> None:
            self.name, self._obj = name, obj

        def load(self) -> Any:
            if isinstance(self._obj, Exception):
                raise self._obj
            return self._obj

    eps = {
        "mapi.steps": [
            EP("scale", toy.ScaleStep),
            EP("many", lambda: [toy.LagStep, toy.Log1pStep]),
        ],
        "mapi.models": [EP("lin", toy.LinearModel), EP("broken", ImportError("no module"))],
        "mapi.plots": [],
    }
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: eps.get(group, []))
    fresh = Registry()
    errors = fresh.load_entry_points()
    assert len(errors) == 1 and "broken" in errors[0]
    assert fresh.step("toy.lag@^1") is toy.LagStep
    with pytest.raises(RuntimeError, match="broken"):
        Registry().load_entry_points(strict=True)
    reg_default = default_registry(reload=True)
    assert reg_default.step("toy.scale@^1") is toy.ScaleStep
    assert default_registry() is reg_default
    custom = default_registry(reload=True, loader=lambda r: r.register(toy.FxStep))
    assert [s.id for s in custom.steps()] == ["toy.fx"]


def test_scale_step_rejects_missing_var_at_fit() -> None:
    step = toy.ScaleStep()
    with pytest.raises(MapiError, match="missing"):
        step.fit(
            xr.Dataset({"x": (("t",), np.ones(2))}),
            step.parse_settings({"vars": ["y"]}),
            StepContext("s"),
        )
