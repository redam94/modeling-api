"""Representation-aware summaries, aggregation and draw thinning (plan §5).

Outputs come in one of five representations (see :mod:`mapi_sdk.models`). Which summaries
and aggregations are valid depends on the representation; the rules live in
:data:`mapi_spec.capabilities.REPRESENTATION_RULES` and are enforced here:

* draws: every summary; any aggregation (applied per draw);
* quantiles: median/quantiles/ETI (interpolated on the stored quantile grid, never
  extrapolated); no aggregation;
* interval: the interval and its point; no aggregation;
* point: mean/point; linear aggregation only, flagged;
* moments: mean/sd and normal ETIs; no aggregation.

Summaries are returned as one variable per output with a leading ``stat`` dim whose labels
read like ``mean``, ``hdi_90_lower``, ``hdi_90_upper``, ``q5`` ... HDIs are computed in
numpy (narrowest window over sorted draws).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist

import numpy as np
import xarray as xr
from mapi_spec.capabilities import (
    REPRESENTATION_RULES,
    Aggregation,
    ParsedSummary,
    Representation,
    can_aggregate,
    parse_summary,
)
from mapi_spec.errors import CapabilityError, ErrorCode

__all__ = [
    "REP_DIMS",
    "aggregate",
    "hdi",
    "interp_quantile",
    "select_draws",
    "summarise",
    "thin_index",
]

REP_DIMS: dict[Representation, tuple[str, ...]] = {
    Representation.DRAWS: ("chain", "draw"),
    Representation.QUANTILES: ("quantile",),
    Representation.INTERVAL: ("bound",),
    Representation.POINT: (),
    Representation.MOMENTS: ("moment",),
}


# ------------------------------------------------------------------------------- draw thinning


def thin_index(chains: int, draws: int, *, seed: int = 0) -> np.ndarray:
    """A bundle-wide random permutation of ``(chain, draw)`` pairs, shape ``(n, 2)``.

    Every group and every score request uses the first ``k`` pairs, so subsets of draws
    stay joint across variables and requests.
    """
    pairs = np.stack(np.meshgrid(np.arange(chains), np.arange(draws), indexing="ij"), -1)
    flat = pairs.reshape(-1, 2)
    perm = np.random.default_rng(seed).permutation(flat.shape[0])
    return flat[perm].astype(np.int64)


def select_draws(obj: xr.Dataset, index: np.ndarray, n: int) -> xr.Dataset:
    """The first ``n`` draws of the thinning ``index`` as a ``sample`` dim."""
    if not {"chain", "draw"} <= set(obj.dims):
        raise CapabilityError(
            "draws were requested but the output has no (chain, draw) dims",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    chain_vals = (
        np.asarray(obj["chain"].values) if "chain" in obj.coords else np.arange(obj.sizes["chain"])
    )
    draw_vals = (
        np.asarray(obj["draw"].values) if "draw" in obj.coords else np.arange(obj.sizes["draw"])
    )
    valid = index[
        np.isin(index[:, 0], np.arange(len(chain_vals)))
        & np.isin(index[:, 1], np.arange(len(draw_vals)))
    ][:n]
    stacked = obj.isel(
        chain=xr.DataArray(valid[:, 0], dims="sample"),
        draw=xr.DataArray(valid[:, 1], dims="sample"),
    )
    return stacked.assign_coords(sample=np.arange(len(valid)))


# ---------------------------------------------------------------------------------------- HDI


def hdi(values: np.ndarray, prob: float, axis: int = -1) -> tuple[np.ndarray, np.ndarray]:
    """Highest-density interval of draws along ``axis`` (narrowest window). NaN-propagating."""
    if not 0 < prob < 1:
        raise ValueError("prob must be in (0, 1)")
    arr = np.moveaxis(np.asarray(values, dtype=float), axis, -1)
    n = arr.shape[-1]
    if n == 0:
        nan = np.full(arr.shape[:-1], np.nan)
        return nan, nan.copy()
    srt = np.sort(arr, axis=-1)
    width_idx = min(max(math.floor(prob * n), 1), n - 1) if n > 1 else 0
    if n == 1:
        return srt[..., 0], srt[..., 0]
    widths = srt[..., width_idx:] - srt[..., : n - width_idx]
    start = np.argmin(widths, axis=-1)[..., None]
    lo = np.take_along_axis(srt, start, axis=-1)[..., 0]
    hi = np.take_along_axis(srt, start + width_idx, axis=-1)[..., 0]
    bad = np.isnan(arr).any(axis=-1)
    lo = np.where(bad, np.nan, lo)
    hi = np.where(bad, np.nan, hi)
    return lo, hi


# -------------------------------------------------------------------------------- aggregation


def aggregate(
    ds: xr.Dataset, rep: Representation, agg: Aggregation | None
) -> tuple[xr.Dataset, bool]:
    """Reduce outputs over ``agg.over`` if the representation allows it.

    Returns the reduced dataset and whether the result must be flagged (point outputs).
    Raises ``CAPABILITY_UNSUPPORTED`` for statistically invalid aggregations.
    """
    if agg is None:
        return ds, False
    verdict = can_aggregate(rep, agg.over, agg.how)
    if not verdict.allowed:
        raise CapabilityError(
            f"cannot aggregate {rep.value!r} output over {list(agg.over)}: {verdict.reason}",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            pointer="/output/aggregate",
        )
    out = {}
    for v in ds.data_vars:
        da = ds[v]
        over = [d for d in agg.over if d in da.dims]
        if not over:
            out[v] = da
            continue
        match agg.how:
            case "sum":
                red = da.sum(over, skipna=False)
            case "mean":
                red = da.mean(over, skipna=False)
            case "min":
                red = da.min(over, skipna=False)
            case _:
                red = da.max(over, skipna=False)
        red.attrs = {**da.attrs, "aggregated": f"{agg.how} over {', '.join(over)}"}
        if verdict.flagged:
            red.attrs["aggregation_note"] = verdict.reason
        out[v] = red
    return xr.Dataset(out, attrs=ds.attrs), verdict.flagged


# ---------------------------------------------------------------------------------- summaries


def _unsupported(rep: Representation, name: str, why: str = "") -> CapabilityError:
    return CapabilityError(
        f"summary {name!r} cannot be computed from {rep.value!r} output"
        + (f": {why}" if why else f"; {REPRESENTATION_RULES[rep].note}"),
        code=ErrorCode.CAPABILITY_UNSUPPORTED,
        pointer="/output/summary",
    )


def _draw_stats(
    da: xr.DataArray, parsed: ParsedSummary, name: str
) -> list[tuple[str, xr.DataArray]]:
    dims = ["chain", "draw"]
    stacked = da.stack(__s__=dims).transpose(..., "__s__")
    match parsed.kind:
        case "mean" | "point":
            return [(name, da.mean(dims, skipna=False))]
        case "sd":
            return [(name, da.std(dims, ddof=1, skipna=False))]
        case "median":
            return [(name, da.median(dims, skipna=False))]
        case "hdi":
            assert parsed.level is not None
            lo, hi = hdi(stacked.values, parsed.level)
            template = stacked.isel(__s__=0, drop=True).drop_vars(
                ["chain", "draw"], errors="ignore"
            )
            return [
                (f"{name}_lower", template.copy(data=lo)),
                (f"{name}_upper", template.copy(data=hi)),
            ]
        case "eti":
            assert parsed.level is not None
            a = (1 - parsed.level) / 2
            q = da.quantile([a, 1 - a], dim=dims, skipna=False)
            return [
                (f"{name}_lower", q.isel(quantile=0, drop=True)),
                (f"{name}_upper", q.isel(quantile=1, drop=True)),
            ]
        case "quantile":
            assert parsed.level is not None
            return [(name, da.quantile(parsed.level, dim=dims, skipna=False).drop_vars("quantile"))]
    raise _unsupported(Representation.DRAWS, name)  # pragma: no cover


def interp_quantile(da: xr.DataArray, level: float, name: str = "") -> xr.DataArray:
    """Value at quantile ``level`` from a ``quantile``-dim array (exact match within 1e-9,
    else linear interpolation; never extrapolates)."""
    levels = np.asarray(da["quantile"].values, dtype=float)
    order = np.argsort(levels)
    levels = levels[order]
    if level < levels[0] - 1e-9 or level > levels[-1] + 1e-9:
        raise _unsupported(
            Representation.QUANTILES,
            name or f"q{level * 100:g}",
            f"level {level:g} is outside the stored quantiles {levels.min():g}..{levels.max():g}",
        )
    exact = np.isclose(levels, level, rtol=0, atol=1e-9)
    if exact.any():
        return da.isel(quantile=int(order[np.argmax(exact)]), drop=True)
    return da.interp(quantile=level).drop_vars("quantile")


def _quantile_stats(
    da: xr.DataArray, parsed: ParsedSummary, name: str
) -> list[tuple[str, xr.DataArray]]:
    match parsed.kind:
        case "median" | "point":
            return [(name, interp_quantile(da, 0.5, name))]
        case "quantile":
            assert parsed.level is not None
            return [(name, interp_quantile(da, parsed.level, name))]
        case "eti":
            assert parsed.level is not None
            a = (1 - parsed.level) / 2
            return [
                (f"{name}_lower", interp_quantile(da, a, name)),
                (f"{name}_upper", interp_quantile(da, 1 - a, name)),
            ]
    raise _unsupported(Representation.QUANTILES, name)


def _interval_stats(
    da: xr.DataArray, parsed: ParsedSummary, name: str
) -> list[tuple[str, xr.DataArray]]:
    bounds = [str(b) for b in da["bound"].values]
    match parsed.kind:
        case "interval":
            return [
                (f"{name}_lower", da.sel(bound="lower", drop=True)),
                (f"{name}_upper", da.sel(bound="upper", drop=True)),
            ]
        case "point":
            if "point" in bounds:
                return [(name, da.sel(bound="point", drop=True))]
            mid = (da.sel(bound="lower", drop=True) + da.sel(bound="upper", drop=True)) / 2
            mid.attrs["point_note"] = "midpoint of the interval"
            return [(name, mid)]
    raise _unsupported(Representation.INTERVAL, name)


def _moment_stats(
    da: xr.DataArray, parsed: ParsedSummary, name: str
) -> list[tuple[str, xr.DataArray]]:
    mean = da.sel(moment="mean", drop=True)
    sd = da.sel(moment="sd", drop=True)
    match parsed.kind:
        case "mean" | "point":
            return [(name, mean)]
        case "sd":
            return [(name, sd)]
        case "eti":
            assert parsed.level is not None
            z = NormalDist().inv_cdf(0.5 + parsed.level / 2)
            return [(f"{name}_lower", mean - z * sd), (f"{name}_upper", mean + z * sd)]
    raise _unsupported(Representation.MOMENTS, name)


def _point_stats(
    da: xr.DataArray, parsed: ParsedSummary, name: str
) -> list[tuple[str, xr.DataArray]]:
    if parsed.kind in ("mean", "point"):
        return [(name, da)]
    raise _unsupported(Representation.POINT, name)


_STATS = {
    Representation.DRAWS: _draw_stats,
    Representation.QUANTILES: _quantile_stats,
    Representation.INTERVAL: _interval_stats,
    Representation.MOMENTS: _moment_stats,
    Representation.POINT: _point_stats,
}


def summarise(ds: xr.Dataset, rep: Representation, names: Sequence[str]) -> xr.Dataset:
    """Summaries of every output variable, stacked along a leading ``stat`` dim."""
    rules = REPRESENTATION_RULES[rep]
    parsed = []
    for name in names:
        p = parse_summary(name)
        if p.kind not in rules.summaries:
            raise _unsupported(rep, name)
        parsed.append((name, p))
    out: dict[str, xr.DataArray] = {}
    for v in ds.data_vars:
        da = ds[v]
        missing = [d for d in REP_DIMS[rep] if d not in da.dims]
        if missing:
            continue  # not a representation-shaped output (e.g. a scalar diagnostic)
        pieces: list[tuple[str, xr.DataArray]] = []
        for name, p in parsed:
            pieces += _STATS[rep](da, p, name)
        labels = [label for label, _ in pieces]
        stacked = xr.concat(
            [arr for _, arr in pieces], dim="stat", coords="minimal", compat="override"
        )
        stacked = stacked.assign_coords(stat=labels)
        stacked.attrs = {k: val for k, val in da.attrs.items()}
        out[str(v)] = stacked.transpose("stat", ...)
    return xr.Dataset(out, attrs=ds.attrs)
