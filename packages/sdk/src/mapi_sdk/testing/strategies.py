"""Hypothesis strategies for labelled datasets (shared by the SDK property tests)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from hypothesis import strategies as st

NAMES = st.sampled_from(["a", "b", "geo", "time", "x_1", "chan", "k"])
FLOATS = st.one_of(
    st.floats(allow_nan=False, allow_infinity=False, width=64, min_value=-1e12, max_value=1e12),
    st.just(float("nan")),
)
# no NUL: numpy's fixed-width ``<U`` strips trailing NULs, so 'a' and 'a\x00' would collapse
# into one label (duplicate coords) after normalisation
TEXT = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), min_size=0, max_size=8
)
JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(2**31), 2**31),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    TEXT,
)
JSON_ATTRS = st.dictionaries(
    st.text(alphabet="abcdefgh_", min_size=1, max_size=6),
    st.one_of(JSON_SCALARS, st.lists(JSON_SCALARS, max_size=3)),
    max_size=3,
)


@st.composite
def coord_values(draw: st.DrawFn, kind: str, n: int) -> np.ndarray:
    if kind == "str":
        texts = draw(st.lists(TEXT, min_size=n, max_size=n, unique=True))
        return np.array(texts, dtype=str) if texts else np.array([], dtype="<U1")
    if kind == "int":
        ints = draw(st.lists(st.integers(-1000, 1000), min_size=n, max_size=n, unique=True))
        return np.array(ints, dtype=np.int64)
    start = draw(
        st.datetimes(min_value=pd.Timestamp("1990-01-01"), max_value=pd.Timestamp("2040-01-01"))
    )
    freq = draw(st.sampled_from(["D", "W-SUN", "h", "MS"]))
    return np.asarray(pd.date_range(start, periods=n, freq=freq).as_unit("ns").values)


@st.composite
def variable_values(draw: st.DrawFn, shape: tuple[int, ...], dtype: str) -> np.ndarray:
    size = int(np.prod(shape)) if shape else 1
    if dtype == "float64":
        vals: list[Any] = draw(st.lists(FLOATS, min_size=size, max_size=size))
        return np.array(vals, dtype=np.float64).reshape(shape)
    if dtype == "int64":
        vals = draw(st.lists(st.integers(-(2**40), 2**40), min_size=size, max_size=size))
        return np.array(vals, dtype=np.int64).reshape(shape)
    if dtype == "bool":
        vals = draw(st.lists(st.booleans(), min_size=size, max_size=size))
        return np.array(vals, dtype=bool).reshape(shape)
    vals = draw(st.lists(TEXT, min_size=size, max_size=size))
    arr = np.array(vals, dtype=str) if vals else np.array([], dtype="<U1")
    return arr.reshape(shape)


@st.composite
def datasets(
    draw: st.DrawFn,
    *,
    max_dims: int = 3,
    max_size: int = 4,
    dtypes: tuple[str, ...] = ("float64", "int64", "bool", "str"),
    min_size: int = 0,
) -> xr.Dataset:
    """Random small datasets with str/int/datetime coords and mixed-dtype variables."""
    dim_names = draw(st.lists(NAMES, min_size=1, max_size=max_dims, unique=True))
    coords: dict[str, Any] = {}
    sizes: dict[str, int] = {}
    for d in dim_names:
        n = draw(st.integers(min_size, max_size))
        kind = draw(st.sampled_from(["str", "int", "time"]))
        coords[d] = draw(coord_values(kind, n))
        sizes[d] = n
    n_vars = draw(st.integers(1, 3))
    data_vars: dict[str, Any] = {}
    for i in range(n_vars):
        vdims = draw(st.lists(st.sampled_from(dim_names), unique=True, max_size=len(dim_names)))
        dtype = draw(st.sampled_from(dtypes))
        shape = tuple(sizes[d] for d in vdims)
        data_vars[f"v{i}"] = (tuple(vdims), draw(variable_values(shape, dtype)), draw(JSON_ATTRS))
    return xr.Dataset(data_vars, coords=coords, attrs=draw(JSON_ATTRS))
