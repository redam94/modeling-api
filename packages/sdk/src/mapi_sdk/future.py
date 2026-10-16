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

__all__ = ["PERIODS_PER_YEAR", "fill_future_covariates", "season_length"]

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
