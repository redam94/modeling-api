"""Derived quantities of ``mmm.hierarchical_geo``, computed from posterior draws.

All curves come from :mod:`mapi_plugin_mmm.transforms` (the functions that build the PyMC
graph), evaluated with NumPy on the thinned posterior, so they agree with the fitted model
exactly. Results are mapped to original units with the platform's ``to_original`` callback
(the inverse of the pipeline back to its last reshape step, e.g. weekly EUR): channel
contributions and the target through ``y``'s inverse chain, spend through ``spend``'s.
Without a callback (a direct call outside the harness) values stay in model units and are
labelled ``output_space = "model"``.

* ``contributions`` - ``channel_contribution(sample, channel, geo, time)``,
  ``baseline(sample, geo, time)`` and one ``(sample, geo, time)`` variable per control,
  over the fit window. They sum to the model's expected target.
* ``response_curves`` - ``contribution(sample, channel, multiplier, geo)``: the total
  contribution over the fit window when a channel's spend is scaled by ``multiplier``
  (``0 .. grid.max_multiplier``); ``spend(channel, multiplier)`` and
  ``spend_by_geo(geo, channel, multiplier)`` give the spend on each grid point and
  ``current_spend(channel)`` the actual spend; ``baseline(sample)`` and
  ``baseline_by_geo(sample, geo)`` are the KPI over the window that no channel explains
  (intercepts plus controls), so ``baseline + sum_c contribution(at 1)`` is the expected
  KPI (M3: the what-if grid view). ``target_scale(geo)`` and
  ``spend_scale(geo, channel)`` convert model units to original units (used by the decision
  objective). Guided-mode sliders interpolate these grids per draw, so bands stay joint.
* ``marginal_cost`` - ``marginal_cost(sample, channel, geo)``: extra spend per extra unit
  of the target at current spend (central difference of ``grid.marginal_step``), its
  inverse ``marginal_roi`` and channel totals over all geos.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import xarray as xr
from mapi_sdk.summarise import select_draws
from mapi_spec.errors import ErrorCode, MapiError

from mapi_plugin_mmm.design import BASELINE, CHANNEL, GEO, TIME, ControlDesign
from mapi_plugin_mmm.settings import GridSettings, HierarchicalGeoSettings
from mapi_plugin_mmm.transforms import adstock_weights, saturate, saturation_params

__all__ = [
    "SAMPLE",
    "ChannelCurves",
    "ToOriginal",
    "baseline_cube",
    "contributions_dataset",
    "draw_samples",
    "marginal_cost_dataset",
    "response_curves_dataset",
]

SAMPLE = "sample"
ToOriginal = Callable[[xr.DataArray, str], xr.DataArray]


def draw_samples(posterior: xr.Dataset, draw_index: np.ndarray | None) -> xr.Dataset:
    """Posterior draws along one ``sample`` dim.

    ``draw_index`` is the bundle thinning index (``(n, 2)`` chain/draw pairs): its pairs are
    used in order. ``None`` uses every draw; an empty index uses the posterior mean.
    """
    if draw_index is None:
        stacked = posterior.stack({SAMPLE: ("chain", "draw")}, create_index=False)
        stacked = stacked.drop_vars(["chain", "draw"], errors="ignore")
        return stacked.assign_coords({SAMPLE: np.arange(stacked.sizes[SAMPLE])})
    if len(draw_index) == 0:
        return posterior.mean(("chain", "draw")).expand_dims({SAMPLE: [0]})
    out = select_draws(posterior, np.asarray(draw_index), len(draw_index))
    return out.drop_vars(["chain", "draw"], errors="ignore")


def _per_channel(post: xr.Dataset, name: str, channels: np.ndarray) -> np.ndarray:
    """``(sample, channel)`` values of a parameter that may be shared across channels."""
    if name not in post:
        raise MapiError(
            f"the posterior has no {name!r} (was the model fitted with other settings?)",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    da = post[name]
    if CHANNEL not in da.dims:
        da = da.expand_dims({CHANNEL: channels})
    da = da.sel({CHANNEL: channels}) if CHANNEL in da.coords else da
    return np.asarray(da.transpose(SAMPLE, CHANNEL).values, dtype=np.float64)


@dataclass(slots=True)
class ChannelCurves:
    """Adstock + saturation + channel effect per posterior sample.

    ``x`` is the model-stage spend tensor ``(geo, channel, time, lag)``.
    """

    settings: HierarchicalGeoSettings
    x: np.ndarray
    geos: np.ndarray
    channels: np.ndarray
    times: np.ndarray
    weights: np.ndarray  # (sample, channel, lag)
    sat: dict[str, np.ndarray]  # name -> (sample, channel)
    beta: np.ndarray  # (sample, geo, channel)
    _adstocked: np.ndarray | None = field(default=None, init=False)

    @classmethod
    def from_samples(
        cls,
        post: xr.Dataset,
        x: np.ndarray,
        settings: HierarchicalGeoSettings,
        *,
        geos: np.ndarray,
        channels: np.ndarray,
        times: np.ndarray,
    ) -> ChannelCurves:
        lags = np.arange(x.shape[-1])
        alpha = _per_channel(post, "alpha", channels)
        theta = (
            _per_channel(post, "theta", channels)[..., None]
            if settings.adstock.kind == "delayed"
            else None
        )
        weights = adstock_weights(
            settings.adstock.kind,
            lags,
            alpha=alpha[..., None],
            theta=theta,
            normalize=settings.adstock.normalize,
        )
        sat = {
            p: _per_channel(post, p, channels) for p in saturation_params(settings.saturation.kind)
        }
        beta_da = post["beta"]
        if GEO in beta_da.coords:
            beta_da = beta_da.sel({GEO: geos})
        if CHANNEL in beta_da.coords:
            beta_da = beta_da.sel({CHANNEL: channels})
        beta = np.asarray(beta_da.transpose(SAMPLE, GEO, CHANNEL).values, dtype=np.float64)
        return cls(settings, x, geos, channels, times, np.asarray(weights), sat, beta)

    @property
    def n_samples(self) -> int:
        return int(self.beta.shape[0])

    def adstocked(self) -> np.ndarray:
        """``(sample, geo, channel, time)`` adstocked spend at the observed spend."""
        if self._adstocked is None:
            self._adstocked = np.einsum("gctl,scl->sgct", self.x, self.weights)
        return self._adstocked

    def contribution(self, multiplier: float = 1.0) -> np.ndarray:
        """``(sample, geo, channel, time)`` channel contributions with every channel's
        spend scaled by ``multiplier`` (channels do not interact, so each channel's curve
        is read off independently)."""
        ad = self.adstocked() * multiplier
        params = {k: v[:, None, :, None] for k, v in self.sat.items()}
        return self.beta[:, :, :, None] * saturate(ad, self.settings.saturation.kind, **params)

    def steady_state(self, spend: np.ndarray) -> np.ndarray:
        """``(sample, geo, channel)`` contribution per period of a constant spend
        ``(geo, channel)`` sustained long enough for the carry-over to settle."""
        level = spend[None, :, :] * self.weights.sum(axis=-1)[:, None, :]
        params = {k: v[:, None, :] for k, v in self.sat.items()}
        return self.beta * saturate(level, self.settings.saturation.kind, **params)


def _model_units(da: xr.DataArray, src: str) -> xr.DataArray:
    out = da.copy()
    out.attrs = {**da.attrs, "output_space": "model", "units_of": src}
    return out


def _convert(to_original: ToOriginal | None, da: xr.DataArray, src: str) -> xr.DataArray:
    if to_original is None:
        return _model_units(da, src)
    out = to_original(da, src)
    out.attrs = {**out.attrs, "units_of": src}
    return out


def _cube(values: np.ndarray, curves: ChannelCurves) -> xr.DataArray:
    """``(sample, geo, channel, time)`` NumPy values as a labelled array."""
    return xr.DataArray(
        values,
        dims=[SAMPLE, GEO, CHANNEL, TIME],
        coords={
            SAMPLE: np.arange(values.shape[0]),
            GEO: curves.geos,
            CHANNEL: curves.channels,
            TIME: curves.times,
        },
    )


def baseline_cube(curves: ChannelCurves, post: xr.Dataset, design: ControlDesign) -> xr.DataArray:
    """``(sample, geo, time)`` KPI no channel explains (intercept + controls), model units."""
    intercept = post["intercept"]
    if GEO in intercept.coords:
        intercept = intercept.sel({GEO: curves.geos})
    base = np.asarray(intercept.transpose(SAMPLE, GEO).values, dtype=np.float64)
    cube = np.broadcast_to(base[:, :, None], (*base.shape, curves.times.size)).copy()
    if not design.empty:
        gamma = post["gamma_control"].sel(control_feature=design.features)
        g = np.asarray(gamma.transpose(SAMPLE, "control_feature").values, dtype=np.float64)
        cube = cube + np.einsum("gtf,sf->sgt", design.values, g)
    return xr.DataArray(
        cube,
        dims=[SAMPLE, GEO, TIME],
        coords={SAMPLE: np.arange(cube.shape[0]), GEO: curves.geos, TIME: curves.times},
    )


def contributions_dataset(
    curves: ChannelCurves,
    post: xr.Dataset,
    controls: Sequence[str],
    design: ControlDesign,
    to_original: ToOriginal | None,
) -> xr.Dataset:
    """Channel, baseline and control contributions over the fit window."""
    geo_coords = {SAMPLE: np.arange(curves.n_samples), GEO: curves.geos, TIME: curves.times}
    out: dict[str, xr.DataArray] = {}
    channel = _cube(curves.contribution(1.0), curves).transpose(SAMPLE, CHANNEL, GEO, TIME)
    out["channel_contribution"] = _convert(to_original, channel, "y")
    intercept = post["intercept"]
    if GEO in intercept.coords:
        intercept = intercept.sel({GEO: curves.geos})
    base = np.asarray(intercept.transpose(SAMPLE, GEO).values, dtype=np.float64)
    base_cube = np.broadcast_to(base[:, :, None], (*base.shape, curves.times.size)).copy()
    out[BASELINE] = _convert(
        to_original, xr.DataArray(base_cube, dims=[SAMPLE, GEO, TIME], coords=geo_coords), "y"
    )
    if not design.empty:
        gamma = post["gamma_control"].sel(control_feature=design.features)
        g = np.asarray(gamma.transpose(SAMPLE, "control_feature").values, dtype=np.float64)
        for name in controls:
            block = design.blocks[name]
            vals = np.einsum("gtf,sf->sgt", design.values[:, :, block], g[:, block])
            label = name if name not in out else f"{name}_contribution"
            out[label] = _convert(
                to_original, xr.DataArray(vals, dims=[SAMPLE, GEO, TIME], coords=geo_coords), "y"
            )
    ds = xr.Dataset(out)
    ds.attrs = {"description": "Additive contributions over the fit window"}
    return ds


def _spend_at(curves: ChannelCurves, multiplier: float) -> xr.DataArray:
    lag0 = curves.x[..., 0] * multiplier  # lag 0 of the tensor is the period's own spend
    return xr.DataArray(
        lag0,
        dims=[GEO, CHANNEL, TIME],
        coords={GEO: curves.geos, CHANNEL: curves.channels, TIME: curves.times},
    )


def _scales(curves: ChannelCurves, to_original: ToOriginal | None) -> tuple[xr.DataArray, ...]:
    """Original units per model unit: target ``(geo,)`` and spend ``(geo, channel)``."""
    ones_y = xr.DataArray(
        np.ones((curves.geos.size, curves.times.size)),
        dims=[GEO, TIME],
        coords={GEO: curves.geos, TIME: curves.times},
    )
    ones_x = xr.DataArray(
        np.ones((curves.geos.size, curves.channels.size, curves.times.size)),
        dims=[GEO, CHANNEL, TIME],
        coords={GEO: curves.geos, CHANNEL: curves.channels, TIME: curves.times},
    )
    ty = _convert(to_original, ones_y, "y")
    tx = _convert(to_original, ones_x, "spend")
    target = ty.mean(TIME)
    target.attrs = {**ty.attrs, "description": "original target units per model unit"}
    spend = tx.mean(TIME)
    spend.attrs = {**tx.attrs, "description": "original spend units per model unit"}
    return target, spend


def response_curves_dataset(
    curves: ChannelCurves,
    grid: GridSettings,
    to_original: ToOriginal | None,
    *,
    baseline: xr.DataArray | None = None,
) -> xr.Dataset:
    """Per-draw response grids (spend multiplier x channel x geo) over the fit window;
    ``baseline`` (``(sample, geo, time)``, model units) adds the unexplained KPI."""
    mults = np.asarray(grid.multipliers(), dtype=np.float64)
    resp: list[np.ndarray] = []
    spend: list[np.ndarray] = []
    attrs_y: dict[str, Any] = {}
    attrs_x: dict[str, Any] = {}
    for m in mults:
        c = _convert(to_original, _cube(curves.contribution(float(m)), curves), "y")
        attrs_y = dict(c.attrs)
        resp.append(np.asarray(c.sum(TIME).transpose(SAMPLE, CHANNEL, GEO).values))
        sp = _convert(to_original, _spend_at(curves, float(m)), "spend")
        attrs_x = dict(sp.attrs)
        spend.append(np.asarray(sp.sum(TIME).transpose(GEO, CHANNEL).values))
    contribution = xr.DataArray(
        np.stack(resp, axis=2),
        dims=[SAMPLE, CHANNEL, "multiplier", GEO],
        coords={
            SAMPLE: np.arange(curves.n_samples),
            CHANNEL: curves.channels,
            "multiplier": mults,
            GEO: curves.geos,
        },
        attrs={**attrs_y, "description": "total contribution over the fit window"},
    )
    by_geo = xr.DataArray(
        np.stack(spend, axis=-1),
        dims=[GEO, CHANNEL, "multiplier"],
        coords={GEO: curves.geos, CHANNEL: curves.channels, "multiplier": mults},
        attrs={**attrs_x, "description": "total spend over the fit window"},
    )
    total = by_geo.sum(GEO)
    total.attrs = dict(by_geo.attrs)
    current = by_geo.sel(multiplier=1.0, method="nearest").sum(GEO).drop_vars("multiplier")
    current.attrs = dict(by_geo.attrs)
    target_scale, spend_scale = _scales(curves, to_original)
    variables: dict[str, xr.DataArray] = {
        "contribution": contribution,
        "spend": total,
        "spend_by_geo": by_geo,
        "current_spend": current,
        "target_scale": target_scale,
        "spend_scale": spend_scale,
    }
    if baseline is not None:
        base = _convert(to_original, baseline, "y").sum(TIME)
        base_attrs = dict(base.attrs)
        variables["baseline_by_geo"] = base.transpose(SAMPLE, GEO).assign_attrs(
            {**base_attrs, "description": "KPI over the fit window that no channel explains"}
        )
        variables["baseline"] = base.sum(GEO).assign_attrs(
            {**base_attrs, "description": "KPI over the fit window that no channel explains"}
        )
    ds = xr.Dataset(variables)
    ds.attrs = {
        "periods": int(curves.times.size),
        "description": "Response of the target to scaling each channel's spend over the "
        "fit window, per thinned posterior draw",
    }
    return ds


def marginal_cost_dataset(
    curves: ChannelCurves, grid: GridSettings, to_original: ToOriginal | None
) -> xr.Dataset:
    """Marginal cost and ROI at current spend (central difference of ``marginal_step``)."""
    h = grid.marginal_step
    lo, hi = 1.0 - h, 1.0 + h
    resp_lo = _convert(to_original, _cube(curves.contribution(lo), curves), "y").sum(TIME)
    resp_hi = _convert(to_original, _cube(curves.contribution(hi), curves), "y").sum(TIME)
    sp_lo = _convert(to_original, _spend_at(curves, lo), "spend").sum(TIME)
    sp_hi = _convert(to_original, _spend_at(curves, hi), "spend").sum(TIME)
    d_out = (resp_hi - resp_lo).transpose(SAMPLE, CHANNEL, GEO)
    d_in = (sp_hi - sp_lo).transpose(CHANNEL, GEO)
    with np.errstate(divide="ignore", invalid="ignore"):
        cost = xr.where(d_out > 0, d_in / d_out, np.nan)
        roi = xr.where(d_in > 0, d_out / d_in, np.nan)
        total_out = d_out.sum(GEO)
        total_in = d_in.sum(GEO)
        cost_total = xr.where(total_out > 0, total_in / total_out, np.nan)
        roi_total = xr.where(total_in > 0, total_out / total_in, np.nan)
    ds = xr.Dataset(
        {
            "marginal_cost": cost.transpose(SAMPLE, CHANNEL, GEO),
            "marginal_roi": roi.transpose(SAMPLE, CHANNEL, GEO),
            "marginal_cost_total": cost_total.transpose(SAMPLE, CHANNEL),
            "marginal_roi_total": roi_total.transpose(SAMPLE, CHANNEL),
        }
    )
    for v in ds.data_vars:
        ds[v].attrs = {"output_space": resp_lo.attrs.get("output_space", "model")}
    ds["marginal_cost"].attrs["description"] = "extra spend per extra unit of the target"
    ds["marginal_roi"].attrs["description"] = "extra target per extra unit of spend"
    ds.attrs = {"step": float(h), "description": "Marginal cost and ROI at current spend"}
    return ds
