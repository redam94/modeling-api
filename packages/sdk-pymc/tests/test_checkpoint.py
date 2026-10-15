"""Checkpoint sinks and the pickle-free sampling-state codec."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_pymc.checkpoint import (
    CheckpointSink,
    DirectoryCheckpointSink,
    MemoryCheckpointSink,
    SamplingCheckpoint,
    decode_state,
    encode_state,
    fit_key,
)

KEY = "0123456789abcdef0123456789abcdef"


def _checkpoint(**kw: Any) -> SamplingCheckpoint:
    base: dict[str, Any] = {
        "key": KEY,
        "engine": "pymc",
        "resumable": True,
        "chains": 2,
        "tune": 10,
        "draws": 20,
        "completed": 5,
        "data": xr.Dataset({"value__a": (("chain", "draw"), np.arange(10.0).reshape(2, 5))}),
        "state": {"names": ["a"], "chains": []},
    }
    base.update(kw)
    return SamplingCheckpoint(**base)


def test_checkpoint_validates_key_and_progress() -> None:
    ck = _checkpoint()
    assert ck.fraction == 0.25
    with pytest.raises(ValueError, match="hex"):
        _checkpoint(key="../escape")
    with pytest.raises(ValueError, match="completed"):
        _checkpoint(completed=21)
    assert _checkpoint(draws=0, completed=0).fraction == 1.0


@pytest.mark.parametrize("kind", ["memory", "directory"])
def test_sinks_round_trip_and_clear(kind: str, tmp_path: Path) -> None:
    sink: CheckpointSink = (
        MemoryCheckpointSink() if kind == "memory" else DirectoryCheckpointSink(tmp_path)
    )
    assert isinstance(sink, CheckpointSink)
    assert sink.load(KEY) is None
    sink.save(_checkpoint())
    back = sink.load(KEY)
    assert back is not None
    assert back.completed == 5 and back.resumable and back.state["names"] == ["a"]
    np.testing.assert_array_equal(back.data["value__a"].values, np.arange(10.0).reshape(2, 5))
    sink.clear(KEY)
    assert sink.load(KEY) is None
    sink.clear(KEY)  # idempotent


def test_directory_sink_writes_atomically_and_rejects_traversal(tmp_path: Path) -> None:
    sink = DirectoryCheckpointSink(tmp_path)
    sink.save(_checkpoint())
    files = sorted(p.name for p in (tmp_path / KEY).iterdir())
    assert files == ["draws.zarr.zip", "meta.json"]
    meta = json.loads((tmp_path / KEY / "meta.json").read_text())
    assert meta["engine"] == "pymc" and meta["completed"] == 5
    with pytest.raises(ValueError):
        sink.load("../../etc")


def test_codec_round_trips_a_real_nuts_state() -> None:
    with pm.Model() as model:
        pm.Normal("a", 0, 1, shape=3)
        pm.HalfNormal("s", 1)
    _, step = pm.init_nuts(chains=1, model=model, random_seed=[1], progressbar=False, quiet=True)
    state = step.sampling_state
    encoded = encode_state(state)
    text = json.dumps(encoded, allow_nan=False)  # strictly JSON, no NaN tokens
    back = decode_state(json.loads(text))
    assert type(back) is type(state)
    step.sampling_state = back  # PyMC accepts the rebuilt state
    for f in dataclasses.fields(state):
        a, b = getattr(state, f.name), getattr(back, f.name)
        if isinstance(a, np.ndarray):
            np.testing.assert_array_equal(a, b)


def test_codec_handles_special_values_and_refuses_foreign_types() -> None:
    value = {
        1: (np.float64(np.nan), float("inf"), [np.array([np.nan, 1.0])]),
        "big": 2**127 + 5,
        "b": np.bool_(True),
    }
    back = decode_state(json.loads(json.dumps(encode_state(value), allow_nan=False)))
    assert back["big"] == 2**127 + 5 and back["b"] is np.True_
    nan_scalar, inf, (arr,) = back[1][0], back[1][1], back[1][2]
    assert np.isnan(nan_scalar) and inf == float("inf") and np.isnan(arr[0])
    with pytest.raises(TypeError, match="cannot checkpoint"):
        encode_state(object())
    with pytest.raises(TypeError, match="dtype"):
        encode_state(np.array(["a"], dtype=object))

    @dataclasses.dataclass
    class Foreign:
        x: int = 1

    encoded = encode_state(Foreign())
    with pytest.raises(ValueError, match="refusing"):
        decode_state(encoded)
    with pytest.raises(ValueError, match="refusing"):
        decode_state({"__ndarray__": {"dtype": "|O", "shape": [1], "b64": ""}})
    with pytest.raises(ValueError, match="unrecognised"):
        decode_state({"weird": 1})
    with pytest.raises(ValueError, match="not a dataclass"):
        decode_state({"__dataclass__": "pymc.model.core:Model", "fields": {}})


_leaf = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**130), max_value=2**130)
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=8)
)
_arrays = st.builds(
    lambda vals: np.asarray(vals, dtype=float),
    st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=6),
)
_values = st.recursive(
    _leaf | _arrays,
    lambda inner: (
        st.lists(inner, max_size=4)
        | st.tuples(inner, inner)
        | st.dictionaries(st.text(max_size=5), inner, max_size=4)
    ),
    max_leaves=12,
)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, np.ndarray):
        return isinstance(b, np.ndarray) and np.array_equal(a, b, equal_nan=True)
    if isinstance(a, float):
        return isinstance(b, float) and (a == b or (np.isnan(a) and np.isnan(b)))
    if isinstance(a, tuple | list):
        return type(a) is type(b) and len(a) == len(b) and all(map(_same, a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return bool(a == b) and type(a) is type(b)


@settings(max_examples=150, deadline=None)
@given(_values)
def test_property_codec_round_trip(value: Any) -> None:
    text = json.dumps(encode_state(value), allow_nan=False)
    assert _same(value, decode_state(json.loads(text)))


def test_fit_key_tracks_data_settings_and_config() -> None:
    ds = xr.Dataset({"y": (("obs",), np.arange(4.0))}, coords={"obs": np.arange(4)})
    base = {"model_id": "m", "version": "1", "settings": {"a": 1}, "data": ds}
    k1 = fit_key(fit_config={"draws": 10, "checkpoint": {"every_draws": 5}}, **base)
    k2 = fit_key(fit_config={"draws": 10, "checkpoint": {"every_draws": 2}}, **base)
    assert k1 == k2 and len(k1) == 32  # checkpoint cadence does not change the fit
    assert k1 != fit_key(fit_config={"draws": 11}, **base)
    assert k1 != fit_key(fit_config={"draws": 10}, **{**base, "settings": {"a": 2}})
    other = ds.assign(y=ds["y"] + 1)
    assert k1 != fit_key(fit_config={"draws": 10}, **{**base, "data": other})
    labelled = ds.assign_coords(g=("obs", ["a", "b", "c", "d"]))
    assert k1 != fit_key(fit_config={"draws": 10}, **{**base, "data": labelled})
