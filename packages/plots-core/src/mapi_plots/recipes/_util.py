"""Data-shaping helpers shared by the recipes (xarray in, tidy pandas out)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr

from mapi_plots.recipes._base import RecipeDataError

__all__ = [
    "SAMPLE",
    "check_levels",
    "element_label",
    "facet_dim",
    "flatten_samples",
    "fmt",
    "has_samples",
    "interval_columns",
    "observed_name",
    "pick_var",
    "sample_dims",
    "stack_samples",
    "summary_bands",
    "time_dim",
]

SAMPLE = "sample"
_TIME_NAMES = ("time", "date", "week", "month", "day", "period", "t", "ds")
_STAT_INTERVAL = re.compile(r"^(hdi|eti)_(\d+(?:\.\d+)?)_(lower|upper)$")
_STAT_QUANTILE = re.compile(r"^q(\d+(?:\.\d+)?)$")


def sample_dims(obj: xr.Dataset | xr.DataArray) -> tuple[str, ...]:
    """The sample dims of an output: ``(chain, draw)``, ``(sample,)`` or ``(draw,)``."""
    dims = set(obj.dims)
    if {"chain", "draw"} <= dims:
        return ("chain", "draw")
    if SAMPLE in dims:
        return (SAMPLE,)
    if "draw" in dims:
        return ("draw",)
    return ()


def has_samples(obj: xr.Dataset | xr.DataArray) -> bool:
    return bool(sample_dims(obj))


def stack_samples(da: xr.DataArray) -> xr.DataArray:
    """Flatten the sample dims into one leading ``sample`` dim (plain integer labels)."""
    dims = sample_dims(da)
    if not dims:
        raise RecipeDataError(f"{da.name!r} has no draws (chain/draw or sample dims)")
    if dims == (SAMPLE,):
        out = da.drop_vars([c for c in da.coords if SAMPLE in da[c].dims], errors="ignore")
    elif len(dims) == 1:
        out = da.rename({dims[0]: SAMPLE}).drop_vars(SAMPLE, errors="ignore")
    else:
        drop = [c for c in da.coords if set(da[c].dims) & set(dims)]
        out = da.drop_vars(drop).stack({SAMPLE: dims}, create_index=False)
    out = out.transpose(SAMPLE, ...)
    return out.assign_coords({SAMPLE: np.arange(out.sizes[SAMPLE])})


def flatten_samples(da: xr.DataArray) -> np.ndarray:
    """``(sample, cells)`` matrix from a sample-stacked array."""
    st = stack_samples(da) if SAMPLE not in da.dims else da
    return np.asarray(st.values, dtype=float).reshape(st.sizes[SAMPLE], -1)


def time_dim(obj: xr.Dataset | xr.DataArray, prefer: str | None = None) -> str | None:
    """The time-like dim: ``prefer`` if given, else a datetime coord, else a conventional name."""
    dims = [str(d) for d in obj.dims if d not in ("chain", "draw", SAMPLE, "stat")]
    if prefer is not None:
        if prefer not in dims:
            raise RecipeDataError(f"no dim {prefer!r} (dims: {', '.join(dims)})")
        return prefer
    for d in dims:
        if d in obj.coords and np.issubdtype(obj[d].dtype, np.datetime64):
            return d
    for name in _TIME_NAMES:
        if name in dims:
            return name
    return None


def facet_dim(
    obj: xr.Dataset | xr.DataArray,
    exclude: Iterable[str | None],
    *,
    prefer: str | None = None,
    max_levels: int = 12,
) -> str | None:
    """A categorical dim to facet or colour by (or ``prefer``), excluding ``exclude``."""
    skip = {"chain", "draw", SAMPLE, "stat", "quantile", "bound", "moment"} | {
        e for e in exclude if e
    }
    if prefer is not None:
        if prefer not in obj.dims:
            raise RecipeDataError(f"no dim {prefer!r} to facet by")
        return prefer
    for d in obj.dims:
        d = str(d)
        if d in skip or obj.sizes[d] > max_levels or obj.sizes[d] < 2:
            continue
        coord = obj[d] if d in obj.coords else None
        if coord is None or coord.dtype.kind in "OUSb":
            return d
    return None


def pick_var(ds: xr.Dataset, var: str | None, *, what: str = "variable") -> str:
    """``var`` if given (must exist), else the first data variable."""
    names = [str(v) for v in ds.data_vars]
    if var is not None:
        if var not in names:
            raise RecipeDataError(f"no {what} {var!r} (available: {', '.join(names) or 'none'})")
        return var
    if not names:
        raise RecipeDataError(f"the group has no {what}s")
    return names[0]


def observed_name(obs: xr.Dataset | None, var: str, explicit: str | None = None) -> str | None:
    """The observed variable matching prediction ``var``.

    Score outputs name predictions by the model output (``y_hat``) and observations by the
    dataset's target (``revenue_local``), so: ``explicit`` if given, else ``var`` itself, else
    the only variable of the observed group.
    """
    if obs is None:
        return None
    if explicit is not None:
        if explicit not in obs.data_vars:
            raise RecipeDataError(f"no observed variable {explicit!r}")
        return explicit
    if var in obs.data_vars:
        return var
    names = [str(n) for n in obs.data_vars]
    return names[0] if len(names) == 1 else None


def element_label(var: str, idx: dict[str, Any]) -> str:
    """``beta[tv, north]`` style labels for one element of a variable."""
    if not idx:
        return var
    return f"{var}[{', '.join(str(v) for v in idx.values())}]"


def iter_elements(da: xr.DataArray, dims: Sequence[str]) -> Iterable[tuple[str, xr.DataArray]]:
    """``(label, sub-array)`` for every element over the non-sample dims."""
    other = [d for d in da.dims if d not in dims]
    if not other:
        yield str(da.name), da
        return
    stacked = da.stack(_el=other)
    for i in range(stacked.sizes["_el"]):
        sub = stacked.isel(_el=i)
        idx = {str(d): sub[d].item() if d in sub.coords else i for d in other}
        yield (
            element_label(str(da.name), idx),
            sub.drop_vars([c for c in sub.coords if c not in sub.dims], errors="ignore"),
        )


def interval_columns(levels: Sequence[float]) -> list[tuple[float, str, str]]:
    """``(level, lower_col, upper_col)`` names for band levels, e.g. ``lo_90``/``hi_90``."""
    out = []
    for lv in sorted(levels, reverse=True):
        pct = f"{lv * 100:g}".replace(".", "_")
        out.append((lv, f"lo_{pct}", f"hi_{pct}"))
    return out


def summary_bands(da: xr.DataArray, levels: Sequence[float]) -> dict[str, xr.DataArray]:
    """Point and interval arrays from a ``stat``-dim summary (score ``summary`` group).

    Returns ``point`` plus ``lo_<pct>``/``hi_<pct>`` for every requested level that the
    summary contains (HDI preferred, then ETI, then matching quantiles).
    """
    labels = [str(s) for s in da["stat"].values]
    out: dict[str, xr.DataArray] = {}
    for name in ("median", "point", "mean", "q50"):
        if name in labels:
            out["point"] = da.sel(stat=name, drop=True)
            break
    intervals: dict[float, dict[str, str]] = {}
    for lab in labels:
        m = _STAT_INTERVAL.match(lab)
        if m:
            lv = float(m.group(2)) / 100
            intervals.setdefault(lv, {}).setdefault(m.group(3), lab)
    quants = {}
    for lab in labels:
        m = _STAT_QUANTILE.match(lab)
        if m:
            quants[round(float(m.group(1)) / 100, 6)] = lab
    for lv, lo_col, hi_col in interval_columns(levels):
        pair = intervals.get(lv)
        if pair and "lower" in pair and "upper" in pair:
            out[lo_col] = da.sel(stat=pair["lower"], drop=True)
            out[hi_col] = da.sel(stat=pair["upper"], drop=True)
            continue
        a = round((1 - lv) / 2, 6)
        if a in quants and round(1 - a, 6) in quants:
            out[lo_col] = da.sel(stat=quants[a], drop=True)
            out[hi_col] = da.sel(stat=quants[round(1 - a, 6)], drop=True)
    if "point" not in out:
        raise RecipeDataError("the summary has no mean, median or point statistic")
    return out


def check_levels(v: list[float]) -> list[float]:
    """Validate interval levels (each in (0, 1)); returns them sorted and de-duplicated."""
    if not v or any(not 0 < x < 1 for x in v):
        raise ValueError("levels must be in (0, 1)")
    return sorted(set(v))


def fmt(x: float) -> str:
    """Compact human number: 1.2M, 34.5k, 0.123."""
    if x is None or not np.isfinite(x):
        return "n/a"
    a = abs(x)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if a >= div:
            return f"{x / div:.3g}{suf}"
    if a >= 1:
        return f"{x:.3g}"
    return f"{x:.2g}"


def pct(x: float) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:.0f}%"


def coord_values(obj: xr.Dataset | xr.DataArray, dim: str) -> np.ndarray:
    if dim in obj.coords:
        return np.asarray(obj[dim].values)
    return np.arange(obj.sizes[dim])


def normalise_time(values: Any) -> Any:
    """Coerce a requested split point to the coord's dtype (datetimes stay datetimes)."""
    if isinstance(values, str):
        try:
            return pd.Timestamp(values).to_datetime64()
        except ValueError:
            return values
    return values
