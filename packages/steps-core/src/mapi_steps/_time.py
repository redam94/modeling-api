"""Time-axis helpers: ordinal positions on a frequency grid and full-period detection."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from mapi_sdk.schema import infer_freq
from mapi_sdk.steps import StepContext
from mapi_spec.coords import parse_freq
from pydantic import JsonValue

from mapi_steps._common import schema_error

__all__ = [
    "coord_freq",
    "full_period_labels",
    "origin_json",
    "positions",
]

Unit = Literal["step", "days"]
_DAY_NS = 86_400 * 10**9


def coord_freq(values: np.ndarray, ctx: StepContext, dim: str) -> str | None:
    """Frequency of a datetime dim: the input schema's ``freq``, else inferred from labels."""
    schema = ctx.schema
    if schema is not None and dim in schema.dims and schema.dims[dim].freq:
        return schema.dims[dim].freq
    if values.dtype.kind != "M":
        return None
    return infer_freq(values)


def origin_json(value: Any) -> JsonValue:
    """JSON form of a coordinate label (ISO string for datetimes)."""
    if isinstance(value, np.datetime64):
        return str(pd.Timestamp(value).isoformat())
    item: object = value.item() if hasattr(value, "item") else value
    if isinstance(item, bool) or not isinstance(item, int | float):
        raise TypeError("origin must be a datetime or a number")
    return float(item) if isinstance(item, float) else int(item)


def positions(
    values: np.ndarray,
    origin: JsonValue,
    *,
    freq: str | None,
    unit: Unit,
    step_id: str,
    dim: str,
) -> np.ndarray:
    """Float positions of ``values`` relative to ``origin``.

    Datetimes count steps of ``freq`` (``unit="step"``, exact calendar arithmetic, labels
    must lie on the grid) or days (``unit="days"``). Numeric coordinates count raw units.
    """
    if values.dtype.kind == "M":
        if not isinstance(origin, str):
            raise schema_error(step_id, f"dim {dim!r} is datetime but the state origin is not")
        o = pd.Timestamp(origin)
        if unit == "days":
            ns = pd.DatetimeIndex(values).asi8 - o.value
            return np.asarray(ns / _DAY_NS, dtype=np.float64)
        if freq is None:
            raise schema_error(
                step_id,
                f"dim {dim!r} has no known frequency; declare freq on the dim or use unit 'days'",
            )
        f = parse_freq(freq)
        o_ord = f.ordinal(o.to_pydatetime())
        if o_ord is None:
            raise schema_error(step_id, f"origin {origin!r} is not on the {freq!r} grid")
        out = np.empty(values.size, dtype=np.float64)
        for i, t in enumerate(pd.DatetimeIndex(values)):
            k = f.ordinal(t.to_pydatetime())
            if k is None:
                raise schema_error(
                    step_id, f"label {t.isoformat()} of {dim!r} is not on the {freq!r} grid"
                )
            out[i] = k - o_ord
        return out
    if values.dtype.kind in "iuf":
        if isinstance(origin, bool) or not isinstance(origin, int | float):
            raise schema_error(step_id, f"dim {dim!r} is numeric but the state origin is not")
        return np.asarray(np.asarray(values, dtype=np.float64) - float(origin))
    raise schema_error(step_id, f"dim {dim!r} must be datetime or numeric")


def full_period_labels(
    index: pd.DatetimeIndex, freq: str, source_freq: str
) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """Bin labels of ``index`` resampled to ``freq`` and whether each bin is complete.

    A bin is complete when it holds every label the ``source_freq`` grid puts in it.
    """
    ones = pd.Series(np.ones(len(index)), index=index)
    observed = ones.resample(freq).count()
    if observed.empty:
        return pd.DatetimeIndex(observed.index), np.zeros(0, dtype=bool)
    offset = pd.tseries.frequencies.to_offset(freq)
    lo = index.min() - 2 * offset
    hi = index.max() + 2 * offset
    grid = pd.date_range(lo, hi, freq=source_freq)
    expected = pd.Series(np.ones(len(grid)), index=grid).resample(freq).count()
    exp = expected.reindex(observed.index).to_numpy()
    full = observed.to_numpy() == exp
    return pd.DatetimeIndex(observed.index), np.asarray(full, dtype=bool)
