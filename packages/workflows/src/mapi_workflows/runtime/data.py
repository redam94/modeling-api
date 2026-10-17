"""Dataset helpers for node implementations: schemas, the time dim, the target, series.

Workflows run on the same labelled datasets as models (``xr.Dataset`` with a raw
:class:`~mapi_spec.schema.DataSchema` in ``attrs["mapi_schema"]``). Tests and searches work
on one series at a time: a variable along the time dim, either a single series or, for a
panel (geo x time, product x time), each series in turn (``per_series``) or their total.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.schema import infer_schema
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema, Role
from mapi_workflows.runtime.values import DatasetValue

__all__ = [
    "SCHEMA_ATTR",
    "Series",
    "data_error",
    "iter_series",
    "label_of",
    "numeric_vars",
    "schema_of",
    "season_length",
    "target_var",
    "time_dim",
    "with_schema",
]

SCHEMA_ATTR: Final = "mapi_schema"

_TIME_NAMES: Final = ("time", "t", "date", "week", "month", "day", "period", "ds")


def data_error(message: str) -> MapiError:
    """A data problem the user can fix (plain message)."""
    return MapiError(message, code=ErrorCode.SCHEMA_INVALID)


def schema_of(value: DatasetValue | xr.Dataset) -> DataSchema:
    """The dataset's raw schema (stored, given or inferred)."""
    if isinstance(value, DatasetValue):
        if value.schema is not None:
            return value.schema
        ds = value.data
    else:
        ds = value
    stored = ds.attrs.get(SCHEMA_ATTR)
    if isinstance(stored, str):
        return DataSchema.model_validate_json(stored)
    if isinstance(stored, dict):
        return DataSchema.model_validate(stored)
    return infer_schema(ds)


def with_schema(ds: xr.Dataset, schema: DataSchema) -> xr.Dataset:
    """``ds`` carrying ``schema`` in its attributes (as ingest writes it)."""
    out = ds.copy()
    out.attrs = {**ds.attrs, SCHEMA_ATTR: schema.model_dump_json(exclude_none=True)}
    return out


def time_dim(ds: xr.Dataset, schema: DataSchema | None = None, prefer: str | None = None) -> str:
    """The time dim: ``prefer`` if given, the schema's ``time`` dim, a datetime coordinate,
    or a conventionally named dim."""
    if prefer:
        if prefer not in ds.dims:
            raise data_error(
                f"the data has no dimension {prefer!r} (it has: {', '.join(map(str, ds.dims))})"
            )
        return prefer
    if schema is not None:
        times = [d for d, spec in schema.dims.items() if spec.kind == "time" and d in ds.dims]
        if len(times) == 1:
            return times[0]
    for d in ds.dims:
        if d in ds.coords and np.issubdtype(ds[d].dtype, np.datetime64):
            return str(d)
    for name in _TIME_NAMES:
        if name in ds.dims:
            return name
    if len(ds.dims) == 1:
        return str(next(iter(ds.dims)))
    raise data_error("cannot tell which dimension is time; name it with the node's 'dim'")


def numeric_vars(ds: xr.Dataset) -> list[str]:
    return [str(v) for v in ds.data_vars if np.issubdtype(ds[v].dtype, np.number)]


def target_var(ds: xr.Dataset, schema: DataSchema | None, var: str | None = None) -> str:
    """The variable a node works on: ``var``, else the schema's single target, else the only
    numeric variable."""
    if var:
        if var not in ds.data_vars:
            raise data_error(
                f"the data has no variable {var!r} (it has: {', '.join(map(str, ds.data_vars))})"
            )
        return var
    if schema is not None:
        targets = [v for v, s in schema.vars.items() if s.role is Role.TARGET and v in ds]
        if len(targets) == 1:
            return targets[0]
    nums = numeric_vars(ds)
    if len(nums) == 1:
        return nums[0]
    raise data_error(
        "the data has several variables and no single target; name one with 'var' "
        f"(variables: {', '.join(nums[:20])})"
    )


def label_of(value: Any) -> str | int | float | bool:
    """A coordinate label as JSON (dates as ISO strings)."""
    if isinstance(value, np.datetime64 | pd.Timestamp):
        ts = pd.Timestamp(value)
        text = ts.isoformat() if ts.time() != pd.Timestamp(0).time() else ts.date().isoformat()
        return str(text)
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)


@dataclass(frozen=True, slots=True)
class Series:
    """One series along the time dim (``coords`` names it within a panel)."""

    coords: dict[str, str | int | float | bool]
    values: np.ndarray
    labels: np.ndarray

    def clean(self) -> tuple[np.ndarray, np.ndarray]:
        """Values and labels with leading and trailing missing values removed; interior
        gaps are filled by linear interpolation (and counted by :attr:`gaps`)."""
        v = np.asarray(self.values, dtype=float)
        ok = np.isfinite(v)
        if not ok.any():
            return v[:0], self.labels[:0]
        lo, hi = int(np.argmax(ok)), len(v) - int(np.argmax(ok[::-1]))
        v, lab = v[lo:hi], self.labels[lo:hi]
        bad = ~np.isfinite(v)
        if bad.any():
            idx = np.arange(len(v))
            v = v.copy()
            v[bad] = np.interp(idx[bad], idx[~bad], v[~bad])
        return v, lab

    @property
    def gaps(self) -> int:
        v = np.asarray(self.values, dtype=float)
        ok = np.isfinite(v)
        if not ok.any():
            return 0
        lo, hi = int(np.argmax(ok)), len(v) - int(np.argmax(ok[::-1]))
        return int((~ok[lo:hi]).sum())

    @property
    def name(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.coords.items()) or "the series"


def iter_series(
    ds: xr.Dataset, var: str, tdim: str, *, per_series: bool = True, max_series: int = 10_000
) -> Iterator[Series]:
    """Each series of ``var`` along ``tdim``; with ``per_series=False`` a panel is summed
    over its other dims into one total series."""
    da = ds[var]
    if tdim not in da.dims:
        raise data_error(f"{var!r} does not vary along {tdim!r}")
    others = [str(d) for d in da.dims if d != tdim]
    labels = np.asarray(ds[tdim].values) if tdim in ds.coords else np.arange(da.sizes[tdim])
    if not others:
        yield Series({}, np.asarray(da.transpose(tdim).values, dtype=float), labels)
        return
    if not per_series:
        total = da.sum(dim=others, min_count=1)
        yield Series({}, np.asarray(total.transpose(tdim).values, dtype=float), labels)
        return
    levels = [list(ds[d].values) if d in ds.coords else list(range(da.sizes[d])) for d in others]
    for count, combo in enumerate(itertools.product(*levels), start=1):
        if count > max_series:
            raise data_error(f"the panel has more than {max_series} series")
        sel = dict(zip(others, combo, strict=True))
        sub = (
            da.sel(sel)
            if all(d in ds.coords for d in others)
            else da.isel({d: levels[i].index(c) for i, (d, c) in enumerate(sel.items())})
        )
        yield Series(
            {k: label_of(v) for k, v in sel.items()},
            np.asarray(sub.transpose(tdim).values, dtype=float),
            labels,
        )


_PERIODS: Final = {
    "W": 52,
    "M": 12,
    "MS": 12,
    "ME": 12,
    "Q": 4,
    "QS": 4,
    "QE": 4,
    "D": 7,
    "B": 5,
    "H": 24,
    "h": 24,
    "Y": 1,
    "A": 1,
}


def season_length(
    ds: xr.Dataset, schema: DataSchema | None, tdim: str, given: int | None = None
) -> int:
    """The season length in periods: ``given``, or from the time dim's frequency (52 for
    weekly, 12 for monthly, 4 for quarterly, 7 for daily, 24 for hourly)."""
    if given:
        return int(given)
    freq: str | None = None
    if schema is not None and tdim in schema.dims:
        freq = schema.dims[tdim].freq
    if freq is None and tdim in ds.coords and np.issubdtype(ds[tdim].dtype, np.datetime64):
        try:
            freq = pd.infer_freq(pd.DatetimeIndex(ds[tdim].values))
        except (TypeError, ValueError):
            freq = None
    if freq:
        base = freq.split("-")[0].lstrip("0123456789")
        if base in _PERIODS and _PERIODS[base] > 1:
            return _PERIODS[base]
    raise data_error("cannot tell the season length from the data; give 'period' or 'season'")


def json_labels(labels: Sequence[Any]) -> list[Any]:
    return [label_of(x) for x in labels]


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
