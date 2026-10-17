"""Future covariates over a forecast horizon (M3, ``ScoreRequest.future_covariates``).

A forecast needs the future values of its covariates (spend, prices, ...). ``supplied`` (the
default) expects them in the request's payload; the other policies extend each series over
the horizon from its history, per cell of the other dims, inside the sandbox:

* ``carry_last`` - the last observed (finite) value;
* ``seasonal_naive`` - the value one season earlier *by date*, the season being the time
  dim's yearly period (52 weeks, 12 months, 365 days, ...), so the last observed year repeats
  even when the horizon starts after a gap; a missing value steps back whole seasons, and a
  value missing in every season (or a history shorter than a season) falls back to
  ``carry_last`` with a warning;
* ``zero`` - zero (for example "no spend planned"), which needs no history.

:func:`fill_future_covariates` fills only the variables the payload lacks: every non-target
variable of the entry stage's schema that runs along the horizon dim. Variables the payload
supplies are never touched, so a scenario can fix some covariates and let the policy extend
the rest.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from typing import Final

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.coords import parse_freq
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.schema import DataSchema, Role
from mapi_spec.spec import FutureCovariates

__all__ = ["PERIODS_PER_YEAR", "fill_future_covariates", "fill_future_rows", "season_length"]

PERIODS_PER_YEAR: Final[dict[str, float]] = {
    "s": 365 * 86400,
    "min": 365 * 1440,
    "h": 365 * 24,
    "D": 365,
    "W": 52,
    "MS": 12,
    "ME": 12,
    "QS": 4,
    "QE": 4,
    "YS": 1,
    "YE": 1,
}


def season_length(freq: str | None) -> int | None:
    """Periods in one year at ``freq`` (``None`` when the frequency is unknown)."""
    if freq is None:
        return None
    try:
        f = parse_freq(freq)
    except ValueError:
        return None
    per_year = PERIODS_PER_YEAR.get(f.unit)
    if per_year is None:
        return None
    return max(1, round(per_year / f.n))


def _last_finite(values: np.ndarray) -> np.ndarray:
    """Along axis 0, the last finite value per cell (NaN where there is none)."""
    finite = np.isfinite(values)
    n = values.shape[0]
    pos = (n - 1) - np.argmax(finite[::-1], axis=0)
    out = np.take_along_axis(values, pos[None, ...], axis=0)[0]
    return np.where(finite.any(axis=0), out, np.nan)


def _seasonal_by_date(
    values: np.ndarray, history: np.ndarray, horizon: np.ndarray, season: int, freq: str
) -> np.ndarray | None:
    """``(steps, *cells)``: for each horizon date, the history value whole seasons before it
    (the nearest season back with a finite value; NaN where none has one). ``None`` when the
    labels are not dates."""
    if np.asarray(history).dtype.kind != "M" or np.asarray(horizon).dtype.kind != "M":
        return None
    try:
        offset = pd.tseries.frequencies.to_offset(freq)
    except ValueError:
        return None
    hist = pd.DatetimeIndex(history)
    steps = pd.DatetimeIndex(horizon)
    out = np.full((len(steps), *values.shape[1:]), np.nan)
    first = hist.min()
    k = 1
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # non-vectorised offsets
        while True:
            back = steps - (season * k) * offset
            if not (back >= first).any():
                break
            pos = hist.get_indexer(back)
            hit = pos >= 0
            if hit.any():
                got = np.full_like(out, np.nan)
                got[hit] = values[pos[hit]]
                take = np.isnan(out) & np.isfinite(got)
                out[take] = got[take]
                if np.isfinite(out).all():
                    break
            k += 1
    return out


def _to_fill(
    payload: xr.Dataset, schema: DataSchema | None, history: xr.Dataset | None, dim: str
) -> list[str]:
    names: list[str] = []
    if schema is not None:
        for name, var in schema.vars.items():
            if var.role is Role.TARGET or name in payload.data_vars or dim not in var.dims:
                continue
            names.append(name)
    elif history is not None:
        for name in map(str, history.data_vars):
            if name not in payload.data_vars and dim in history[name].dims:
                names.append(name)
    return names


def fill_future_covariates(
    payload: xr.Dataset,
    policy: FutureCovariates | None,
    *,
    dim: str,
    schema: DataSchema | None,
    history: xr.Dataset | None = None,
    targets: Sequence[str] = (),
) -> tuple[xr.Dataset, list[Issue]]:
    """``payload`` (a horizon frame or a partial scenario over ``dim``) with its missing
    covariates extended by ``policy`` from ``history`` (the series at the same stage).

    Returns the payload and ``info``/``warning`` issues naming what was filled.
    """
    if policy is None or policy is FutureCovariates.SUPPLIED or dim not in payload.dims:
        return payload, []
    names = [n for n in _to_fill(payload, schema, history, dim) if n not in targets]
    if not names:
        return payload, []
    issues: list[Issue] = []
    horizon = payload[dim]
    filled: dict[str, xr.DataArray] = {}
    freq = schema.dims[dim].freq if schema is not None and dim in schema.dims else None
    season = season_length(freq)
    for name in names:
        hist = history[name] if history is not None and name in history else None
        if hist is None or dim not in hist.dims:
            if policy is not FutureCovariates.ZERO:
                raise MapiError(
                    f"cannot extend {name!r} with {policy.value!r}: the fitted history is not "
                    "available to this score run",
                    code=ErrorCode.MISSING_FUTURE_COVARIATE,
                    pointer="/future_covariates",
                )
            dims = [d for d in (schema.vars[name].dims if schema is not None else ()) if d != dim]
            missing = [d for d in dims if d not in payload.coords]
            if missing:
                raise MapiError(
                    f"cannot set {name!r} to zero: the payload has no coordinates for {missing}",
                    code=ErrorCode.MISSING_FUTURE_COVARIATE,
                    pointer="/future_covariates",
                )
            shape = [payload.sizes[d] for d in dims] + [horizon.size]
            coords = {d: payload[d].values for d in dims}
            filled[name] = xr.DataArray(
                np.zeros(shape), dims=[*dims, dim], coords={**coords, dim: horizon.values}
            )
            continue
        hist = hist.sortby(dim) if dim in hist.coords else hist
        other = [str(d) for d in hist.dims if d != dim]
        values = np.asarray(hist.transpose(dim, *other).values, dtype=np.float64)
        steps = horizon.size
        if policy is FutureCovariates.ZERO:
            out = np.zeros((steps, *values.shape[1:]))
        elif policy is FutureCovariates.SEASONAL_NAIVE and season and values.shape[0] >= season:
            assert freq is not None
            dated = (
                _seasonal_by_date(values, hist[dim].values, horizon.values, season, freq)
                if dim in hist.coords
                else None
            )
            if dated is None:  # labels are not dates: the last season, by position
                dated = values[-season:][np.arange(steps) % season]
            gaps = ~np.isfinite(dated)
            if gaps.any():
                last = np.broadcast_to(_last_finite(values)[None, ...], dated.shape)
                dated = np.where(gaps, last, dated)
                issues.append(
                    Issue(
                        code=ErrorCode.MISSING_FUTURE_COVARIATE,
                        severity=Severity.WARNING,
                        pointer="/future_covariates",
                        message=f"{name!r} has no value a whole number of seasons before "
                        f"{int(gaps.any(axis=tuple(range(1, gaps.ndim))).sum())} horizon "
                        "period(s); its last value was carried forward there",
                    )
                )
            out = dated
        else:
            if policy is FutureCovariates.SEASONAL_NAIVE:
                issues.append(
                    Issue(
                        code=ErrorCode.MISSING_FUTURE_COVARIATE,
                        severity=Severity.WARNING,
                        pointer="/future_covariates",
                        message=f"{name!r} has less than one season of history "
                        f"({values.shape[0]} periods); its last value was carried forward",
                    )
                )
            out = np.broadcast_to(_last_finite(values)[None, ...], (steps, *values.shape[1:]))
        coords = {d: hist[d].values for d in other if d in hist.coords}
        da = xr.DataArray(np.array(out), dims=[dim, *other], coords={**coords, dim: horizon.values})
        da.attrs = dict(hist.attrs)
        filled[name] = da.transpose(*[d for d in hist.dims])
    issues.insert(
        0,
        Issue(
            code=ErrorCode.MISSING_FUTURE_COVARIATE,
            severity=Severity.INFO,
            pointer="/future_covariates",
            message=f"future values of {sorted(filled)} were filled with {policy.value!r}",
        ),
    )
    return payload.assign(filled), issues


def _ffill(values: np.ndarray) -> np.ndarray:
    """Forward fill along axis 0 (NaN stays where nothing came before)."""
    out = np.array(values, dtype=np.float64, copy=True)
    for i in range(1, out.shape[0]):
        prev, cur = out[i - 1], out[i]
        out[i] = np.where(np.isnan(cur), prev, cur)
    return out


def fill_future_rows(
    payload: xr.Dataset,
    policy: FutureCovariates | None,
    *,
    dim: str,
    future: xr.DataArray,
    names: Sequence[str],
    history: xr.Dataset | None = None,
    schema: DataSchema | None = None,
) -> tuple[xr.Dataset, list[Issue]]:
    """Fill the empty values of the drivers ``names`` on the periods to forecast (forecasts
    with actuals, :mod:`mapi_spec.actuals`).

    ``future`` is a bool array over ``dim`` (and any of the targets' other dims): the periods
    to forecast; it is reduced to each driver's dims (a driver period is future when it is
    for any target cell it feeds). Values the payload supplies are never changed, and nothing
    is filled outside the future periods. Policies: ``carry_last`` - the last value before
    the period in the fitted ``history`` and the payload together (so the actuals' drivers
    count); ``seasonal_naive`` - the value whole seasons earlier by date (else the last one,
    with a warning); ``zero``; ``supplied``/``None`` - nothing is filled. A driver the payload
    lacks entirely is added, empty outside the future periods.
    """
    if policy is None or policy is FutureCovariates.SUPPLIED or dim not in payload.dims:
        return payload, []
    issues: list[Issue] = []
    freq = schema.dims[dim].freq if schema is not None and dim in schema.dims else None
    season = season_length(freq)
    done: list[str] = []
    out = payload
    for name in names:
        hist = history[name] if history is not None and name in history else None
        if name in out:
            current = out[name].astype(np.float64)
        elif hist is not None:
            other = [str(d) for d in hist.dims if d != dim]
            coords = {d: (out[d].values if d in out.coords else hist[d].values) for d in other}
            shape = [len(coords[d]) for d in other] + [out.sizes[dim]]
            current = xr.DataArray(
                np.full(shape, np.nan), dims=[*other, dim], coords={**coords, dim: out[dim]}
            )
        elif policy is FutureCovariates.ZERO and schema is not None and name in schema.vars:
            vdims = [d for d in schema.vars[name].dims if d != dim]
            if any(d not in out.coords for d in vdims):
                continue
            shape = [out.sizes[d] for d in vdims] + [out.sizes[dim]]
            current = xr.DataArray(
                np.full(shape, np.nan),
                dims=[*vdims, dim],
                coords={**{d: out[d].values for d in vdims}, dim: out[dim]},
            )
        else:
            continue  # nothing to extend from: the plugin or the checks report it
        drop = [d for d in future.dims if d not in current.dims]
        fut = future.any(drop) if drop else future
        fut = fut.broadcast_like(current).transpose(*current.dims)
        need = fut & current.isnull()
        if not bool(need.any()):
            continue
        other = [str(d) for d in current.dims if d != dim]
        if policy is FutureCovariates.ZERO:
            fill = xr.zeros_like(current)
        else:
            combined = current
            if hist is not None and dim in hist.dims:
                h = hist.astype(np.float64)
                h = h.reindex(
                    {d: current[d].values for d in other if d in h.dims and d in current.coords}
                )
                combined = current.combine_first(h)
            combined = combined.sortby(dim).transpose(dim, *other)
            values = np.asarray(combined.values, dtype=np.float64)
            labels = np.asarray(combined[dim].values)
            carried = _ffill(values)
            if policy is FutureCovariates.SEASONAL_NAIVE:
                dated = (
                    _seasonal_by_date(values, labels, labels, season, freq)
                    if season and freq is not None
                    else None
                )
                if dated is None or bool(np.isnan(dated[np.isnan(values)]).any()):
                    issues.append(
                        Issue(
                            code=ErrorCode.MISSING_FUTURE_COVARIATE,
                            severity=Severity.WARNING,
                            pointer="/future_covariates",
                            message=f"{name!r} has no value a whole number of seasons before "
                            "some periods to forecast; its last value was carried forward there",
                        )
                    )
                    dated = carried if dated is None else np.where(np.isnan(dated), carried, dated)
                carried = dated
            fill = xr.DataArray(carried, dims=combined.dims, coords=combined.coords)
            fill = fill.sel({dim: current[dim].values}).transpose(*current.dims)
        new = current.where(~need, fill)
        new.attrs = (
            dict(out[name].attrs) if name in out else (dict(hist.attrs) if hist is not None else {})
        )
        out = out.assign({name: new})
        done.append(name)
    if done:
        issues.insert(
            0,
            Issue(
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                severity=Severity.INFO,
                pointer="/future_covariates",
                message=f"empty future values of {sorted(done)} were filled with {policy.value!r}",
            ),
        )
    return out, issues
