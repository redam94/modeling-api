"""Hypothesis properties: invertible steps round trip, replay equals the full transform."""

from __future__ import annotations

from typing import Any

import mapi_steps as ms
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mapi_sdk.context import drop_context, extract_context, prepend_context
from mapi_sdk.steps import StepContext

pytestmark = pytest.mark.unit

FAST = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@st.composite
def panels(
    draw: st.DrawFn,
    *,
    low: float = -1e3,
    high: float = 1e3,
    nan_rate: float = 0.0,
    min_time: int = 3,
) -> xr.Dataset:
    """(geo, channel, time) weekly panels with values in ``[low, high]``."""
    g = draw(st.integers(1, 4))
    c = draw(st.integers(1, 3))
    t = draw(st.integers(min_time, 16))
    seed = draw(st.integers(0, 2**32 - 1))
    rng = np.random.default_rng(seed)
    x = rng.uniform(low, high, size=(g, c, t))
    if nan_rate:
        x[rng.uniform(size=x.shape) < nan_rate] = np.nan
    time = pd.date_range("2023-01-01", periods=t, freq="W-SUN").as_unit("ns")
    return xr.Dataset(
        {"x": (("geo", "channel", "time"), x)},
        coords={
            "geo": [f"g{i}" for i in range(g)],
            "channel": [f"c{i}" for i in range(c)],
            "time": time,
        },
    )


def _roundtrip(step: Any, ds: xr.Dataset, raw: dict[str, Any], **ctx: Any) -> None:
    inst = step()
    s = inst.parse_settings(raw)
    c = StepContext(step_id="p", **ctx)
    st_ = inst.fit(ds, s, c)
    out = inst.transform(ds, s, st_, c)
    back = inst.inverse(out[["x"]], s, st_, c)
    np.testing.assert_allclose(
        back["x"].transpose(*ds["x"].dims).values, ds["x"].values, rtol=1e-9, atol=1e-6
    )
    # model outputs carry leading draw dims: the inverse must broadcast over them
    draws = out[["x"]].expand_dims(chain=1, draw=2)
    back = inst.inverse(draws, s, st_, c)
    assert back["x"].dims[:2] == ("chain", "draw")
    np.testing.assert_allclose(
        back["x"].isel(chain=0, draw=1).transpose(*ds["x"].dims).values,
        ds["x"].values,
        rtol=1e-9,
        atol=1e-6,
    )


@FAST
@given(ds=panels(), over=st.sampled_from([["time"], ["time", "geo"], ["channel"]]))
def test_scaling_steps_invert(ds: xr.Dataset, over: list[str]) -> None:
    for step in (ms.MeanScaleStep, ms.MaxAbsScaleStep, ms.StandardizeStep):
        _roundtrip(step, ds, {"vars": ["x"], "over": over})


@FAST
@given(ds=panels(low=-0.99, high=1e4))
def test_log1p_inverts(ds: xr.Dataset) -> None:
    _roundtrip(ms.Log1pStep, ds, {"vars": ["x"]})


@FAST
@given(ds=panels(low=0.001, high=0.999))
def test_logit_inverts(ds: xr.Dataset) -> None:
    _roundtrip(ms.LogitStep, ds, {"vars": ["x"], "eps": 1e-4})


@FAST
@given(ds=panels(low=0, high=1e5), rate=st.floats(0.01, 100))
def test_fx_inverts(ds: xr.Dataset, rate: float) -> None:
    dates = ds["time"].values
    fx = xr.Dataset(
        {"rate": (("date",), rate * (1 + 0.1 * np.sin(np.arange(dates.size))))},
        coords={"date": dates},
    )
    _roundtrip(
        ms.FxConvertStep,
        ds,
        {"vars": ["x"], "table": "fx", "table_along": "date"},
        side_tables={"fx": fx},
    )


def _replay_equals_full(step: Any, ds: xr.Dataset, raw: dict[str, Any], k: int) -> None:
    """Transforming the last ``k`` rows after prepending the stored context equals the
    full transform's last ``k`` rows (the score path sees exactly the fitted history)."""
    inst = step()
    s = inst.parse_settings(raw)
    c = StepContext(step_id="p")
    st_ = inst.fit(ds, s, c)
    full = inst.transform(ds, s, st_, c)
    need = inst.context(s)
    assert need is not None
    n = ds.sizes["time"]
    head, tail = ds.isel(time=slice(0, n - k)), ds.isel(time=slice(n - k, None))
    combined, mark, _ = prepend_context(tail, extract_context(head, need), need, freq="W-SUN")
    out = drop_context(inst.transform(combined, s, st_, c), [mark] if mark else [])
    xr.testing.assert_allclose(out, full.isel(time=slice(n - k, None)))


@FAST
@given(ds=panels(nan_rate=0.2, min_time=6), k=st.integers(1, 4))
def test_lag_replays_exactly_with_missing_values(ds: xr.Dataset, k: int) -> None:
    _replay_equals_full(ms.LagStep, ds, {"vars": ["x"], "within": ["geo"], "lags": [1, 3]}, k)


@FAST
@given(
    ds=panels(min_time=6),
    k=st.integers(1, 4),
    pad=st.sampled_from(["first", "zero", "nan"]),
)
def test_lag_tensor_replays_exactly(ds: xr.Dataset, k: int, pad: str) -> None:
    # Series start = first observed value; with a fully observed tail the replay is exact.
    # (A group whose whole stored tail is missing is padded on replay: documented gap.)
    _replay_equals_full(ms.LagTensorStep, ds, {"vars": ["x"], "l_max": 4, "pad": pad}, k)


@FAST
@given(ds=panels(low=0, high=100, min_time=6), k=st.integers(1, 4), alpha=st.floats(0, 0.95))
def test_adstock_replays_exactly(ds: xr.Dataset, k: int, alpha: float) -> None:
    _replay_equals_full(ms.AdstockStep, ds, {"vars": ["x"], "alpha": alpha, "l_max": 5}, k)


@FAST
@given(
    ds=panels(nan_rate=0.4, min_time=6),
    split=st.integers(2, 5),
    limit=st.one_of(st.none(), st.integers(1, 6)),
)
def test_ffill_carry_over_equals_full_series(ds: xr.Dataset, split: int, limit: int | None) -> None:
    raw = {"vars": ["x"], "method": "ffill", "within": ["geo", "channel"], "limit": limit}
    inst = ms.ImputeStep()
    s = inst.parse_settings(raw)
    c = StepContext(step_id="p")
    n = ds.sizes["time"]
    cut = min(split, n - 1)
    head, tail = ds.isel(time=slice(0, cut)), ds.isel(time=slice(cut, None))
    full = inst.transform(ds, s, inst.fit(ds, s, c), c).isel(time=slice(cut, None))
    got = inst.transform(tail, s, inst.fit(head, s, c), c)
    xr.testing.assert_identical(got, full)


@FAST
@given(a=st.floats(-1e3, 1e3), b=st.floats(0.5, 1e3))
def test_formula_matches_numpy(a: float, b: float) -> None:
    from mapi_steps.formula import compile_formula, evaluate

    ds = xr.Dataset({"a": ((), a), "b": ((), b)})
    got = evaluate(compile_formula("a * b - a / b + maximum(a, b) ** 2 / (1 + abs(a))"), ds)
    want = a * b - a / b + max(a, b) ** 2 / (1 + abs(a))
    assert float(got) == pytest.approx(want, rel=1e-12, abs=1e-9)
