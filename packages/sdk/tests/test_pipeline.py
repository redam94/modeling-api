"""The pipeline engine: stages, fit/replay, context and the inverse chain."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_sdk.context import drop_context
from mapi_sdk.errors import StepError
from mapi_sdk.pipeline import Pipeline, stage_coords
from mapi_sdk.registry import Registry
from mapi_sdk.steps import Step, StepContext, StepSettings, StepState
from mapi_sdk.testing import toy
from mapi_spec import DataSchema, ModelSpec, Representation, StepKind, validate_spec
from mapi_spec.errors import MapiError
from pydantic import JsonValue

E33_STEPS: list[tuple[str, Any, dict[str, Any]]] = [
    ("to_eur", toy.FxStep, {"vars": ["y", "spend"], "table": "fx", "table_along": "date"}),
    ("weekly", toy.ResampleStep, {"dim": "time", "freq": "W-SUN"}),
    (
        "scale_spend",
        toy.ScaleStep,
        {"vars": ["spend"], "over": ["time"], "unseen_levels": "pooled"},
    ),
    ("scale_y", toy.ScaleStep, {"vars": ["y"], "over": ["time"], "unseen_levels": "pooled"}),
    ("lags", toy.LagStep, {"vars": ["spend"], "within": ["geo"], "lags": [1, 2], "fill": 0.0}),
    ("season", toy.FourierStep, {"period": 52.18, "order": 2}),
]


def test_stages_and_lookup() -> None:
    pipe = Pipeline.build(E33_STEPS)
    assert pipe.stage_names[0] == "raw" and pipe.final_stage == "season"
    assert pipe.index("weekly") == 2
    assert pipe.input_stage("scale_spend") == "weekly"
    assert [s.id for s in pipe.between("weekly", "lags")] == ["scale_spend", "scale_y", "lags"]
    assert pipe.last_reshape_stage == "weekly"
    assert pipe.step("lags").need is not None
    assert pipe.step("lags").kind is StepKind.FEATURE
    with pytest.raises(MapiError):
        pipe.index("nope")
    with pytest.raises(KeyError):
        pipe.step("nope")
    with pytest.raises(ValueError, match="before"):
        pipe.between("lags", "weekly")
    with pytest.raises(ValueError, match="unique"):
        Pipeline.build(
            [("a", toy.Log1pStep, {"vars": ["y"]}), ("a", toy.Log1pStep, {"vars": ["y"]})]
        )
    assert Pipeline.build([]).last_reshape_stage == "raw"


def test_fit_records_states_contexts_and_coords(
    canonical_panel: xr.Dataset, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    pipe = Pipeline.build(E33_STEPS)
    trace = pipe.fit(canonical_panel, side_tables={"fx": panel[1]}, keep_stages=True)
    assert set(trace.states) == {s[0] for s in E33_STEPS}
    lag_state = trace.states["lags"]
    assert lag_state.context is not None and lag_state.context.sizes["time"] == 2
    assert lag_state.context_need is not None and lag_state.step_type == "toy.lag"
    assert trace.states["scale_y"].arrays is not None
    assert trace.states["to_eur"].context is None
    assert pd.infer_freq(pd.DatetimeIndex(trace.coords["weekly"].time.values)) == "W-SUN"
    assert set(trace.coords) == {"raw", *(s[0] for s in E33_STEPS)}
    assert trace.stages["weekly"].sizes["time"] == 24
    replay = pipe.transform(
        canonical_panel, trace.states, side_tables={"fx": panel[1]}, replay=False
    )
    xr.testing.assert_identical(replay.data, trace.data)
    assert stage_coords(trace.data).data_vars == {}


@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 10_000), weeks=st.integers(6, 20))
def test_property_transform_of_fit_equals_fit_output(seed: int, weeks: int) -> None:
    rng = np.random.default_rng(seed)
    time = pd.date_range("2024-01-07", periods=weeks, freq="W-SUN").as_unit("ns")
    ds = xr.Dataset(
        {
            "y": (("geo", "time"), rng.gamma(2, 2, (3, weeks))),
            "x": (("geo", "time"), rng.normal(size=(3, weeks))),
        },
        coords={"geo": ["a", "b", "c"], "time": time},
    )
    pipe = Pipeline.build(
        [
            ("scale", toy.ScaleStep, {"vars": ["x", "y"], "over": ["time"]}),
            ("logy", toy.Log1pStep, {"vars": ["y"]}),
            ("lags", toy.LagStep, {"vars": ["x"], "within": ["geo"], "lags": [1, 3]}),
            ("season", toy.FourierStep, {"period": 13.0, "order": 2}),
        ]
    )
    trace = pipe.fit(ds)
    again = pipe.transform(ds, trace.states, replay=False)
    xr.testing.assert_identical(again.data, trace.data)
    # transform never learns: a later window replayed with context equals the fit output
    k = weeks // 2
    tail = pipe.transform(ds.isel(time=slice(k, None)), trace.states, replay=True)
    assert not tail.marks  # in-sample window: context stored at fit ends after it starts
    xr.testing.assert_allclose(
        tail.data["fourier"], trace.data["fourier"].isel(time=slice(k, None))
    )


@settings(max_examples=30, deadline=None)
@given(
    values=st.lists(st.floats(0.0, 1e6, allow_nan=False), min_size=6, max_size=6),
    draws=st.integers(1, 3),
)
def test_property_inverse_of_transform_is_identity(values: list[float], draws: int) -> None:
    ds = xr.Dataset(
        {"y": (("geo", "time"), np.array(values).reshape(2, 3))},
        coords={"geo": ["a", "b"], "time": np.arange(3)},
    )
    pipe = Pipeline.build(
        [
            ("scale", toy.ScaleStep, {"vars": ["y"], "over": ["time"]}),
            ("logy", toy.Log1pStep, {"vars": ["y"]}),
        ]
    )
    trace = pipe.fit(ds)
    model_space = trace.data["y"].expand_dims(draw=np.arange(draws))
    inv = pipe.inverse_outputs(
        xr.Dataset({"y_hat": model_space}), trace.states, {"y_hat": "y"}, to_stage="raw"
    )
    back = inv.data["y_hat"].isel(draw=0, drop=True)
    np.testing.assert_allclose(back.values, ds["y"].values, rtol=1e-9, atol=1e-6)
    assert inv.space == {"y_hat": "raw"}


def test_inverse_chain_and_labels(
    canonical_panel: xr.Dataset, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    schemas = {
        "weekly": DataSchema.model_validate(
            {
                "dims": {"geo": {"kind": "categorical"}, "time": {"kind": "time"}},
                "vars": {"y": {"dims": ["geo", "time"], "dtype": "float64", "units": "EUR"}},
            }
        )
    }
    steps = [*E33_STEPS, ("logy", toy.Log1pStep, {"vars": ["y"]})]
    pipe = Pipeline.build(steps, schemas=schemas)
    trace = pipe.fit(canonical_panel, side_tables={"fx": panel[1]})
    chain, reached = pipe.inverse_chain("y", to_stage="weekly")
    assert [p.id for p in chain] == ["logy", "scale_y"] and reached == "weekly"
    chain2, reached2 = pipe.inverse_chain("spend", to_stage="raw")
    assert [p.id for p in chain2] == ["scale_spend"] and reached2 == "weekly"
    y_model = trace.data[["y"]].rename(y="y_hat")
    point = pipe.inverse_outputs(
        y_model, trace.states, {"y_hat": "y"}, representation=Representation.POINT
    )
    assert point.retransform == {"y_hat": "median"}
    assert point.data["y_hat"].attrs["units"] == "EUR"
    assert point.data["y_hat"].attrs["output_space"] == "weekly"
    resid = {"y": xr.DataArray(np.array([-0.1, 0.0, 0.1]), dims=["r"])}
    smear = pipe.inverse_outputs(
        y_model,
        trace.states,
        {"y_hat": "y"},
        representation=Representation.POINT,
        retransform="smearing",
        residuals=resid,
    )
    assert smear.retransform == {"y_hat": "smearing"}
    assert bool((smear.data["y_hat"] > point.data["y_hat"]).all())  # Jensen: E[exp] > exp(E)
    sd = xr.full_like(trace.data["y"], 0.01)
    moments = xr.concat([trace.data["y"], sd], dim="moment").assign_coords(moment=["mean", "sd"])
    mom = pipe.inverse_outputs(
        xr.Dataset({"y_hat": moments}),
        trace.states,
        {"y_hat": "y"},
        representation=Representation.MOMENTS,
    )
    assert mom.retransform == {"y_hat": "delta_method"}
    assert bool((mom.data["y_hat"].sel(moment="sd") > 0).all())
    untouched = pipe.inverse_outputs(
        xr.Dataset({"fourier": trace.data["fourier"]}), trace.states, {"fourier": "fourier"}
    )
    assert untouched.retransform == {"fourier": "none"}
    skip = pipe.inverse_outputs(xr.Dataset(), trace.states, {"missing": "y"})
    assert skip.space == {}


class _Boom(Step[StepSettings, StepState]):
    id = "test.boom"
    version = "1.0.0"
    kind = StepKind.VALUE
    effect_template: ClassVar[dict[str, JsonValue]] = {}

    def transform(
        self, ds: xr.Dataset, s: StepSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        raise ZeroDivisionError("kaput")


class _NotDataset(_Boom):
    id = "test.notds"

    def transform(
        self, ds: xr.Dataset, s: StepSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        return ds["y"]  # type: ignore[return-value]


class _BadFit(_Boom):
    id = "test.badfit"

    def fit(self, ds: xr.Dataset, s: StepSettings, ctx: StepContext) -> StepState:
        return {}  # type: ignore[return-value]


def test_step_failures_are_typed(weekly_panel: Callable[..., xr.Dataset]) -> None:
    ds = weekly_panel()
    with pytest.raises(StepError) as err:
        Pipeline.build([("boom", _Boom, {})]).fit(ds)
    assert err.value.untrusted_text == "ZeroDivisionError: kaput"
    with pytest.raises(StepError, match="must return an xarray Dataset"):
        Pipeline.build([("nd", _NotDataset, {})]).fit(ds)
    with pytest.raises(StepError, match="StepState"):
        Pipeline.build([("bf", _BadFit, {})]).fit(ds)
    good = Pipeline.build([("logy", toy.Log1pStep, {"vars": ["y"]})])
    with pytest.raises(StepError, match="no fitted state"):
        good.transform(ds, {})
    with pytest.raises(StepError, match="must return an xarray Dataset"):
        Pipeline.build([("nd", _NotDataset, {})]).transform(ds, {"nd": StepState()})


def test_from_spec_with_and_without_resolution(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    from mapi_sdk.schema import infer_schema

    spec = e33_spec()
    raw = infer_schema(panel[0], targets=["revenue_local"])
    resolved = validate_spec(spec, registry.catalog(), raw).raise_for_errors()
    pipe = Pipeline.from_spec(spec, registry, resolved)
    assert pipe.schema_at("weekly") is not None
    assert pipe.step("lags").settings.model_dump()["within"] == ["geo"]  # type: ignore[index]
    bare = Pipeline.from_spec(spec, registry)
    assert bare.schema_at("weekly") is None and len(bare.steps) == 6


def test_replay_with_context_matches_full_transform(
    canonical_panel: xr.Dataset, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    pipe = Pipeline.build(E33_STEPS)
    side = {"fx": panel[1]}
    train = canonical_panel.isel(time=slice(0, 7 * 16))
    trace = pipe.fit(train, side_tables=side)
    future = canonical_panel.isel(time=slice(7 * 16, None)).drop_vars("y")
    rp = pipe.transform(future, trace.states, side_tables=side)
    assert len(rp.marks) == 1 and rp.marks[0].step_id == "lags"
    out = drop_context(rp.data, rp.marks)
    full = pipe.transform(
        canonical_panel.drop_vars("y"), trace.states, side_tables=side, replay=False
    )
    xr.testing.assert_allclose(out["spend_lag"], full.data["spend_lag"].sel(time=out.time))
