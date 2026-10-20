"""Derived quantities of the mmm-framework plugins, from posterior draws.

Everything is computed with a :class:`~mapi_plugin_mmm.forward.ReplayLike` - the fitted
model's own forward pass - on the fit window, over the thinned draws the harness asks for, in KPI
and spend units of the model stage (the model owns scaling, so these are business units;
``to_original`` maps them further when the pipeline converts them).

* ``contributions`` - ``channel_contribution(sample, channel, *panel, time)``,
  ``baseline`` (intercept and geo/product levels), ``trend``, ``seasonality`` and one
  ``(sample, *panel, time)`` variable per control, over the fit window. They sum to the
  model's expected KPI. A count or proportion likelihood has only the channels and the
  baseline (the expected KPI without media).
* ``response_curves`` - ``contribution(sample, channel, multiplier[, geo])``: the total
  contribution over the fit window when a channel's spend is scaled by ``multiplier``;
  ``spend(channel, multiplier)``, ``spend_by_geo(geo, channel, multiplier)``,
  ``current_spend(channel)``, ``max_period_spend(channel)``, and ``baseline(sample)`` /
  ``baseline_by_geo(sample, geo)`` - the KPI no channel explains - so ``baseline +
  sum_c contribution(at 1)`` is the expected KPI (the what-if grid view and the scorecard
  read these). Product panels are summed over products.
* ``marginal_cost`` - ``marginal_cost(sample, channel[, geo])``: extra spend per extra unit
  of the KPI at current spend (central difference of ``grid.marginal_step``), its inverse
  ``marginal_roi`` and channel totals.
* ``adstock`` - ``weight(sample, channel, lag)``: the share of a unit of spend's total
  effect that lands ``lag`` periods after the spend (the carry-over kernel normalised to
  one), and ``mean_lag(sample, channel)``.
* ``effectiveness`` - ``effectiveness(sample, channel, time)``: KPI per unit of spend at
  each cell's average spend level with the channel coefficient of period ``time``
  (constant for channels that are not time-varying), and ``time_varying(channel)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError

from mapi_plugin_mmm.forward import Components, ReplayLike, history_arrays
from mapi_plugin_mmm.framework import CHANNEL, GEO, PRODUCT, TIME, PanelData, infer_frequency
from mapi_plugin_mmm.settings import GridSettings

__all__ = [
    "SAMPLE",
    "FitWindow",
    "ToOriginal",
    "adstock_dataset",
    "contributions_dataset",
    "effectiveness_dataset",
    "marginal_cost_dataset",
    "response_curves_dataset",
]

SAMPLE = "sample"
ToOriginal = Callable[[xr.DataArray, str], xr.DataArray]


def _convert(
    to_original: ToOriginal | None, da: xr.DataArray, src: str, win: FitWindow
) -> xr.DataArray:
    """``da`` (model-stage units of input ``src``) in the output space, labelled.

    The model owns scaling, so the pipeline after its last reshape step must leave ``y``
    and ``spend`` unscaled (or scaled by one constant): the conversion is read once from a
    probe array with ``src``'s own dims and applied as a factor, which also labels the
    units (``to_original`` cannot label a total that has no time dim by itself)."""
    if to_original is None:
        out = da.copy()
        out.attrs = {**da.attrs, "output_space": "model", "units_of": src}
        return out
    factor, attrs = win.unit_map(to_original, src)
    out = da * factor if factor != 1.0 else da.copy()
    out.attrs = {**da.attrs, **attrs, "units_of": src}
    return out


@dataclass(slots=True)
class FitWindow:
    """The replay over the fit window: fitted cells, history arrays and labels."""

    replay: ReplayLike
    fit: PanelData
    _units: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)

    def unit_map(self, to_original: ToOriginal, src: str) -> tuple[float, dict[str, Any]]:
        """``(factor, attrs)`` of ``to_original`` for input ``src`` (``y`` or ``spend``)."""
        if src in self._units:
            return self._units[src]
        fit = self.fit
        dims: list[str] = [d for d in (GEO, PRODUCT) if d in fit.panel]
        coords: dict[str, Any] = {TIME: fit.times, **self.panel_coords()}
        if src == "spend":
            dims = [*dims, CHANNEL, TIME]
            coords[CHANNEL] = np.asarray(fit.channels)
        else:
            dims = [*dims, TIME]
        shape = [len(coords[d]) for d in dims]
        probe = xr.DataArray(np.ones(shape), dims=dims, coords={d: coords[d] for d in dims})
        out = to_original(probe, src)
        vals = np.asarray(out.values, dtype=float)
        finite = vals[np.isfinite(vals)]
        factor = float(finite.flat[0]) if finite.size else 1.0
        if finite.size and not np.allclose(finite, factor, rtol=1e-9, atol=1e-12):
            raise MapiError(
                f"the pipeline rescales {src!r} after its last reshape step; the mmm-framework "
                "model owns scaling, so drop that step",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/pipeline",
            )
        attrs = {k: v for k, v in out.attrs.items() if k in ("units", "output_space")}
        self._units[src] = (factor, attrs)
        return factor, attrs

    @property
    def cells(self) -> list[Any]:
        return self.replay.fit_cells()

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        return history_arrays(self.fit)

    def evaluate(self, multiplier: float = 1.0) -> Components:
        spend, ctrl = self.arrays()
        return self.replay.evaluate(
            spend * multiplier, ctrl, np.arange(self.fit.n_periods), self.cells
        )

    def channel_totals(self, multiplier: float) -> np.ndarray:
        """``(S, C, n_cell)`` channel contributions over the window at ``multiplier``
        (:meth:`~mapi_plugin_mmm.forward.ReplayLike.channel_totals`)."""
        spend, ctrl = self.arrays()
        return self.replay.channel_totals(
            spend, ctrl, np.arange(self.fit.n_periods), self.cells, float(multiplier)
        )

    def expected_cells(self) -> np.ndarray:
        """``(S, n_cell)`` expected KPI over the window at the actual spend."""
        return np.asarray(self.evaluate(1.0).mu.sum(axis=2))

    def baseline_cells(self) -> np.ndarray:
        """``(S, n_cell)`` KPI over the window that no channel explains: the expected KPI
        minus every channel's total."""
        return self.expected_cells() - self.channel_totals(1.0).sum(axis=1)

    # labelled helpers ----------------------------------------------------------------
    def panel_dims(self) -> list[str]:
        return [d for d in (GEO, PRODUCT) if d in self.fit.panel]

    def panel_coords(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.fit.geos:
            out[GEO] = np.asarray(self.fit.geos)
        if self.fit.products:
            out[PRODUCT] = np.asarray(self.fit.products)
        return out

    def cube(
        self, values: np.ndarray, lead: list[str], lead_coords: dict[str, Any]
    ) -> xr.DataArray:
        """``(*lead, n_cell, T)`` to a labelled ``(*lead, *panel, time)`` array."""
        shape = [*values.shape[: len(lead)], self.fit.n_geos, self.fit.n_products, values.shape[-1]]
        arr = values.reshape(shape)
        dims = [*lead, GEO, PRODUCT, TIME]
        da = xr.DataArray(arr, dims=dims, coords={**lead_coords, TIME: self.fit.times})
        drop = [d for d in (GEO, PRODUCT) if d not in self.fit.panel]
        da = da.isel({d: 0 for d in drop}, drop=True)
        return da.assign_coords(self.panel_coords())


def contributions_dataset(win: FitWindow, to_original: ToOriginal | None) -> xr.Dataset:
    """Channel, baseline, trend, seasonality and control contributions over the window
    (a count or proportion likelihood: channels and baseline only)."""
    comps = win.evaluate(1.0)
    channel, terms, controls = comps.kpi_parts()
    n = comps.baseline.shape[0]
    sample = {SAMPLE: np.arange(n)}
    out: dict[str, xr.DataArray] = {}
    ch = win.cube(channel, [SAMPLE, CHANNEL], {**sample, CHANNEL: win.fit.channels})
    out["channel_contribution"] = _convert(to_original, ch, "y", win)
    names = ("baseline", "trend", "seasonality") if comps.link == "identity" else ("baseline",)
    for name in names:
        out[name] = _convert(
            to_original, win.cube(np.asarray(terms[name]), [SAMPLE], sample), "y", win
        )
    if comps.link == "identity":
        for k, name in enumerate(win.fit.controls.variables):
            label = name if name not in out else f"{name}_contribution"
            out[label] = _convert(to_original, win.cube(controls[:, k], [SAMPLE], sample), "y", win)
    ds = xr.Dataset(out)
    ds.attrs = {"description": "Additive contributions over the fit window"}
    return ds


def _by_geo(values: np.ndarray, fit: PanelData) -> np.ndarray:
    """``(..., n_cell)`` summed over products to ``(..., G)``."""
    return values.reshape(*values.shape[:-1], fit.n_geos, fit.n_products).sum(axis=-1)


def response_curves_dataset(
    win: FitWindow,
    grid: GridSettings,
    to_original: ToOriginal | None,
    *,
    multipliers: list[float] | None = None,
) -> xr.Dataset:
    """Per-draw response grids (spend multiplier x channel [x geo]) over the fit window,
    with the spend on each grid point and the baseline (see the module doc)."""
    fit = win.fit
    mults = np.asarray(multipliers if multipliers is not None else grid.multipliers(), dtype=float)
    if 1.0 not in mults:
        mults = np.sort(np.append(mults, 1.0))
    per_geo = bool(fit.geos)
    spend, _ = win.arrays()  # (T, n_cell, C)
    base_cells = spend.sum(axis=0).T  # (C, n_cell)
    resp: list[np.ndarray] = []
    for m in mults:
        tot = win.channel_totals(float(m))  # (S, C, n_cell)
        resp.append(_by_geo(tot, fit) if per_geo else tot.sum(axis=2))
    n = resp[0].shape[0]
    channels = list(fit.channels)
    if per_geo:
        contrib = np.stack(resp, axis=2)  # (S, C, M, G)
        dims = [SAMPLE, CHANNEL, "multiplier", GEO]
        coords: dict[str, Any] = {
            SAMPLE: np.arange(n), CHANNEL: channels, "multiplier": mults,
            GEO: np.asarray(fit.geos),
        }  # fmt: skip
    else:
        contrib = np.stack(resp, axis=2)  # (S, C, M)
        dims = [SAMPLE, CHANNEL, "multiplier"]
        coords = {SAMPLE: np.arange(n), CHANNEL: channels, "multiplier": mults}
    contribution = _convert(to_original, xr.DataArray(contrib, dims=dims, coords=coords), "y", win)
    contribution.attrs["description"] = "total contribution over the fit window"
    by_geo_spend = _by_geo(base_cells, fit)  # (C, G)
    spend_by_geo = xr.DataArray(
        by_geo_spend.T[:, :, None] * mults[None, None, :],
        dims=[GEO, CHANNEL, "multiplier"],
        coords={GEO: np.asarray(fit.geos or ["__all__"]), CHANNEL: channels, "multiplier": mults},
    )
    spend_by_geo = _convert(to_original, spend_by_geo, "spend", win)
    spend_by_geo.attrs["description"] = "total spend over the fit window"
    total = spend_by_geo.sum(GEO)
    total.attrs = dict(spend_by_geo.attrs)
    current = spend_by_geo.sel(multiplier=1.0).sum(GEO).drop_vars("multiplier")
    current.attrs = dict(spend_by_geo.attrs)
    peak = xr.DataArray(
        spend.max(axis=(0, 1)), dims=[CHANNEL], coords={CHANNEL: channels}
    )  # largest single-cell, single-period spend
    peak = _convert(to_original, peak, "spend", win)
    peak.attrs["description"] = "largest spend of one cell in one period"
    base_cell = win.baseline_cells()  # (S, n_cell)
    variables: dict[str, xr.DataArray] = {
        "contribution": contribution,
        "spend": total,
        "current_spend": current,
        "max_period_spend": peak,
    }
    base_geo = _by_geo(base_cell, fit)  # (S, G)
    if per_geo:
        variables["spend_by_geo"] = spend_by_geo
        bg = _convert(
            to_original,
            xr.DataArray(
                base_geo,
                dims=[SAMPLE, GEO],
                coords={SAMPLE: np.arange(n), GEO: np.asarray(fit.geos)},
            ),
            "y",
            win,
        )
        bg.attrs["description"] = "KPI over the fit window that no channel explains"
        variables["baseline_by_geo"] = bg
    base = _convert(
        to_original,
        xr.DataArray(base_geo.sum(axis=1), dims=[SAMPLE], coords={SAMPLE: np.arange(n)}),
        "y",
        win,
    )
    base.attrs["description"] = "KPI over the fit window that no channel explains"
    variables["baseline"] = base
    ds = xr.Dataset(variables)
    ds.attrs = {
        "periods": int(fit.n_periods),
        "cells": int(fit.n_cells),
        "description": "Response of the KPI to scaling each channel's spend over the fit "
        "window, per thinned posterior draw",
    }
    return ds


def marginal_cost_dataset(
    win: FitWindow, grid: GridSettings, to_original: ToOriginal | None
) -> xr.Dataset:
    """Marginal cost and ROI at current spend (central difference of ``marginal_step``)."""
    fit = win.fit
    h = grid.marginal_step
    lo = win.channel_totals(1.0 - h)  # (S, C, n_cell)
    hi = win.channel_totals(1.0 + h)
    spend, _ = win.arrays()
    base = spend.sum(axis=0).T  # (C, n_cell)
    per_geo = bool(fit.geos)
    d_out = _by_geo(hi - lo, fit) if per_geo else (hi - lo).sum(axis=2)
    d_in = (_by_geo(base, fit) if per_geo else base.sum(axis=1)) * 2 * h
    channels = list(fit.channels)
    n = d_out.shape[0]
    if per_geo:
        dims = [SAMPLE, CHANNEL, GEO]
        coords: dict[str, Any] = {
            SAMPLE: np.arange(n),
            CHANNEL: channels,
            GEO: np.asarray(fit.geos),
        }
        din = xr.DataArray(
            d_in, dims=[CHANNEL, GEO], coords={CHANNEL: channels, GEO: np.asarray(fit.geos)}
        )
    else:
        dims = [SAMPLE, CHANNEL]
        coords = {SAMPLE: np.arange(n), CHANNEL: channels}
        din = xr.DataArray(d_in, dims=[CHANNEL], coords={CHANNEL: channels})
    dout = _convert(to_original, xr.DataArray(d_out, dims=dims, coords=coords), "y", win)
    din = _convert(to_original, din, "spend", win)
    with np.errstate(divide="ignore", invalid="ignore"):
        cost = xr.where(dout > 0, din / dout, np.nan)
        roi = xr.where(din > 0, dout / din, np.nan)
        if per_geo:
            total_out = dout.sum(GEO)
            total_in = din.sum(GEO)
            cost_total = xr.where(total_out > 0, total_in / total_out, np.nan)
            roi_total = xr.where(total_in > 0, total_out / total_in, np.nan)
        else:
            cost_total, roi_total = cost, roi
    ds = xr.Dataset(
        {
            "marginal_cost": cost.transpose(*dims),
            "marginal_roi": roi.transpose(*dims),
            "marginal_cost_total": cost_total.transpose(SAMPLE, CHANNEL),
            "marginal_roi_total": roi_total.transpose(SAMPLE, CHANNEL),
        }
    )
    for v in ds.data_vars:
        ds[v].attrs = {"output_space": dout.attrs.get("output_space", "model")}
    ds["marginal_cost"].attrs["description"] = "extra spend per extra unit of the KPI"
    ds["marginal_roi"].attrs["description"] = "extra KPI per extra unit of spend"
    ds.attrs = {"step": float(h), "description": "Marginal cost and ROI at current spend"}
    return ds


def _frequency(win: FitWindow) -> str | None:
    freq = getattr(win.replay, "frequency", None)
    if freq in ("W", "D", "M"):
        return str(freq)
    try:
        return infer_frequency(win.fit.times)
    except MapiError:
        return None


def adstock_dataset(win: FitWindow) -> xr.Dataset:
    """Carry-over shape per draw: ``weight(sample, channel, lag)`` (the kernel normalised
    to sum to one; shorter kernels padded with zeros) and ``mean_lag(sample, channel)``."""
    kernels = [np.asarray(k, dtype=float) for k in win.replay.kernels]
    n = int(win.replay.n)
    lags = max((k.shape[1] for k in kernels), default=1)
    weight = np.zeros((n, len(kernels), lags))
    for c, k in enumerate(kernels):
        k = np.broadcast_to(k, (n, k.shape[1]))
        total = k.sum(axis=1, keepdims=True)
        weight[:, c, : k.shape[1]] = np.divide(k, total, out=np.zeros_like(k), where=total != 0)
    lag = np.arange(lags)
    coords: dict[str, Any] = {SAMPLE: np.arange(n), CHANNEL: list(win.fit.channels), "lag": lag}
    w = xr.DataArray(weight, dims=[SAMPLE, CHANNEL, "lag"], coords=coords)
    w.attrs["description"] = (
        "share of a unit of spend's total effect that lands this many periods after the spend"
    )
    mean_lag = xr.DataArray(
        (weight * lag[None, None, :]).sum(axis=2),
        dims=[SAMPLE, CHANNEL],
        coords={SAMPLE: coords[SAMPLE], CHANNEL: coords[CHANNEL]},
    )
    mean_lag.attrs["description"] = "average delay of the effect, in periods"
    ds = xr.Dataset({"weight": w, "mean_lag": mean_lag})
    ds.attrs = {"description": "Carry-over of spend into later periods, per thinned draw"}
    freq = _frequency(win)
    if freq is not None:
        ds.attrs["frequency"] = freq
    return ds


def effectiveness_dataset(win: FitWindow, to_original: ToOriginal | None) -> xr.Dataset:
    """KPI per unit of spend at each cell's average spend over the fit window, with the
    channel coefficient of every fitted period (see the module doc)."""
    fit = win.fit
    spend, _ = win.arrays()  # (T, n_cell, C)
    level = spend.mean(axis=0)  # (n_cell, C)
    den = level.sum(axis=0)  # (C,)
    cells = win.cells
    tvp = set(getattr(win.replay, "tvp", {}) or {})
    t = fit.n_periods

    def per_spend(position: int) -> np.ndarray:
        kpi = win.replay.steady_state(level, cells, position=position).sum(axis=1)  # (S, C)
        return np.divide(
            kpi,
            den[None, :],
            out=np.full_like(kpi, np.nan),
            where=den[None, :] > 0,
        )

    if tvp:
        eff = np.stack([per_spend(p) for p in range(t)], axis=2)  # (S, C, T)
    else:
        one = per_spend(t - 1)
        eff = np.repeat(one[:, :, None], t, axis=2)
    channels = list(fit.channels)
    n = eff.shape[0]
    da = xr.DataArray(
        eff,
        dims=[SAMPLE, CHANNEL, TIME],
        coords={SAMPLE: np.arange(n), CHANNEL: channels, TIME: fit.times},
    )
    da = _convert(to_original, da, "y", win)
    if to_original is not None:
        factor, _ = win.unit_map(to_original, "spend")
        if factor != 1.0:
            attrs = dict(da.attrs)
            da = da / factor
            da.attrs = attrs
    da.attrs.pop("units", None)
    da.attrs["units_of"] = "y per spend"
    da.attrs["description"] = "KPI per unit of spend at the average spend level"
    flags = xr.DataArray(
        np.array([c in tvp for c in range(len(channels))], dtype=bool),
        dims=[CHANNEL],
        coords={CHANNEL: channels},
    )
    ds = xr.Dataset({"effectiveness": da, "time_varying": flags})
    ds.attrs = {
        "description": "Channel effectiveness over the fit window (KPI per unit of spend at "
        "the average spend level); constant for channels that are not time-varying"
    }
    return ds
