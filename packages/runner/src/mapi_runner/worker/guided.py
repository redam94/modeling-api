"""Guided-mode views of fits and score runs (plan §11 Guided tasks 5-7; M3).

* :func:`response_grid_view` - ``ui/grids/response_curves.json``
  (:class:`~mapi_spec.views.ResponseGridView`) from a fit's ``response_curves`` derived
  quantity: thinned per-draw contributions per channel and spend multiplier (and geo), the
  spend at every grid point, the actual spend and the baseline, in KPI units. The web app's
  sliders interpolate it (:func:`mapi_spec.whatif.evaluate_scenario`);
* :func:`forecast_view` - ``ui/guided/forecast.json`` (:class:`~mapi_spec.views.ForecastView`):
  the actual KPI over the fitted history, then the forecast fan after the split;
* :func:`counterfactual_view` - ``ui/guided/counterfactual.json``
  (:class:`~mapi_spec.views.CounterfactualView`): the change of the KPI in total and per
  period, from common-random-number draws;
* :func:`check_lift_tests` - lift tests against the raw dataset (channel, geo and dates
  exist) before a fit spends any compute.

KPI totals are sums over every dim but time *per posterior draw*, so their quantiles are
valid (draws are joint); outputs in other representations get no view.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_spec.calibration import DistributionSummary, FanChart
from mapi_spec.capabilities import Representation
from mapi_spec.errors import ErrorCode, Issue, ValidationFailed
from mapi_spec.spec import ModelSpec, ScoreRequest
from mapi_spec.views import (
    MAX_GRID_CELLS,
    MAX_GRID_DRAWS,
    CounterfactualView,
    ForecastView,
    ResponseGridView,
)

__all__ = [
    "LEVELS",
    "MAX_HISTORY",
    "check_lift_tests",
    "counterfactual_view",
    "forecast_tree",
    "forecast_view",
    "response_grid_view",
]

LEVELS: Final = (0.05, 0.25, 0.5, 0.75, 0.95)
MAX_HISTORY: Final = 520
"""Fitted periods shown before a forecast (the most recent ones)."""
_SIG: Final = 12


def _f(x: float) -> float:
    return float(f"{float(x):.{_SIG}g}")


def _units(da: xr.DataArray) -> str | None:
    u = da.attrs.get("units")
    return u[:64] if isinstance(u, str) and u else None


def _label(v: Any) -> str | int | float:
    if isinstance(v, np.datetime64):
        return str(np.datetime_as_string(v, unit="D"))
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int | float):
        return v
    return str(v)[:128]


# ============================================================================ response grid


def response_grid_view(bundle: Bundle, *, kpi: str | None = None) -> ResponseGridView | None:
    """The what-if grid of a fit, or ``None`` when it has no usable ``response_curves``."""
    rc = bundle.model.derived.get("response_curves")
    if rc is None or "contribution" not in rc or "spend" not in rc:
        return None
    da = rc["contribution"]
    need = {"sample", "channel", "multiplier"}
    if not need <= set(map(str, da.dims)) or set(map(str, da.dims)) - need - {"geo"}:
        return None
    per_geo = "geo" in da.dims
    contrib = da.transpose("sample", "channel", "multiplier", *(["geo"] if per_geo else []))
    values = np.asarray(contrib.values, dtype=np.float64)
    if not np.isfinite(values).all():
        return None
    n_draws, n_ch, n_m = values.shape[:3]
    n_geo = values.shape[3] if per_geo else 1
    draws = min(n_draws, MAX_GRID_DRAWS, max(1, MAX_GRID_CELLS // max(1, n_ch * n_m)))
    with_geo = per_geo and draws * n_ch * n_m * n_geo <= MAX_GRID_CELLS
    values = values[:draws]
    total = values.sum(axis=3) if per_geo else values
    spend = np.asarray(rc["spend"].transpose("channel", "multiplier").values, dtype=np.float64)
    current = (
        np.asarray(rc["current_spend"].transpose("channel").values, dtype=np.float64)
        if "current_spend" in rc
        else spend[:, int(np.argmin(np.abs(np.asarray(rc["multiplier"].values) - 1.0)))]
    )
    body: dict[str, Any] = {
        "kpi": (kpi or _target_name(bundle))[:128],
        "kpi_units": _units(da),
        "spend_units": _units(rc["spend"]),
        "periods": max(1, int(rc.attrs.get("periods", 1))),
        "channels": [str(c)[:128] for c in rc["channel"].values],
        "multipliers": [_f(m) for m in rc["multiplier"].values],
        "draws": draws,
        "contribution": [[[_f(v) for v in row] for row in per_c] for per_c in total],
        "spend": [[_f(v) for v in row] for row in spend],
        "current_spend": [_f(v) for v in current],
    }
    if "baseline" in rc and set(map(str, rc["baseline"].dims)) == {"sample"}:
        base = np.asarray(rc["baseline"].values, dtype=np.float64)[:draws]
        if np.isfinite(base).all():
            body["baseline"] = [_f(v) for v in base]
    if with_geo:
        body["geos"] = [str(g)[:128] for g in rc["geo"].values]
        body["contribution_by_geo"] = [
            [[[_f(v) for v in cell] for cell in row] for row in per_c] for per_c in values
        ]
        if "spend_by_geo" in rc:
            sg = rc["spend_by_geo"].transpose("geo", "channel", "multiplier").values
            body["spend_by_geo"] = [[[_f(v) for v in row] for row in per_c] for per_c in sg]
    return ResponseGridView.model_validate(body)


def _target_name(bundle: Bundle) -> str:
    """The dataset name of the model's (first) target."""
    schema = bundle.resolved.model.input_schema
    targets = schema.targets if schema is not None else []
    return bundle.binding.to_dataset(targets[0] if targets else "y")


# ============================================================================== score views


def _kpi_var(bundle: Bundle, ds: xr.Dataset) -> str | None:
    """The prediction variable holding the KPI (an output in target units, not a
    decomposition), by its name in ``ds``."""
    manifest = bundle.model.manifest
    targets = (
        bundle.resolved.model.input_schema.targets if bundle.resolved.model.input_schema else []
    )
    binding = bundle.binding
    for name, decl in manifest.outputs.items():
        if "component" in decl.dims:
            continue
        if decl.units_of is not None and decl.units_of not in targets:
            continue
        for candidate in (name, binding.to_dataset(name)):
            if candidate in ds:
                return candidate
    return None


def _samples(da: xr.DataArray, keep: Sequence[str]) -> tuple[np.ndarray, list[str]]:
    """``(sample, *keep)`` values summed over every other dim, and the dims summed."""
    other = [str(d) for d in da.dims if d not in ("chain", "draw", *keep)]
    total = da.sum(other) if other else da
    stacked = total.stack(sample=("chain", "draw")).transpose("sample", *keep)
    return np.asarray(stacked.values, dtype=np.float64), other


def _summary(values: np.ndarray, units: str | None) -> DistributionSummary:
    v = values[np.isfinite(values)]
    if not v.size:
        return DistributionSummary(mean=0.0, sd=0.0, quantiles={}, units=units)
    qs = np.quantile(v, LEVELS)
    return DistributionSummary(
        mean=_f(v.mean()),
        sd=_f(v.std()),
        quantiles={f"{q:g}": _f(x) for q, x in zip(LEVELS, qs, strict=True)},
        units=units,
    )


def _time_dim(da: xr.DataArray, preferred: str | None) -> str | None:
    if preferred is not None and preferred in da.dims:
        return preferred
    for d in da.dims:
        if d in da.coords and np.issubdtype(da[d].dtype, np.datetime64):
            return str(d)
    return None


def _draws_rep(bundle: Bundle) -> bool:
    return bundle.model.manifest.capabilities.representation is Representation.DRAWS


def _history(
    bundle: Bundle, registry: Any, tables: Mapping[str, xr.Dataset], var: str
) -> xr.DataArray | None:
    """The observed KPI over the fit window in the output space, dataset names (``None``
    when the bundle stores no usable observed data)."""
    obs = bundle.model.groups.get("observed_data")
    targets = (
        bundle.resolved.model.input_schema.targets if bundle.resolved.model.input_schema else []
    )
    if obs is None or not targets or targets[0] not in obs:
        return None
    target = targets[0]
    stage_coords = bundle.fit_coords.get(bundle.resolved.model_stage)
    da = obs[target]
    if stage_coords is None:
        return None
    for d in list(map(str, da.dims)):
        # a likelihood over the observed subset of a dim (``t__obs``, labelled by ``t``)
        base = d.removesuffix("__obs")
        if base != d and base in stage_coords.dims and d in da.coords:
            da = da.rename({d: base})
    if any(d not in stage_coords.dims for d in da.dims):
        return None
    ds = xr.Dataset({target: da})
    if "inverse_units" not in bundle.model.manifest.capabilities.owns:
        pipeline = bundle.pipeline(registry)
        inv = pipeline.inverse_outputs(
            ds,
            bundle.states,
            {target: target},
            representation=Representation.DRAWS,
            side_tables={**tables, **bundle.side_tables},
        )
        ds = inv.data
    out = bundle.binding.reverse(ds)
    name = bundle.binding.to_dataset(target)
    return out.get(name)


def forecast_view(
    bundle: Bundle,
    tree: xr.DataTree,
    req: ScoreRequest,
    *,
    registry: Any,
    tables: Mapping[str, xr.Dataset],
) -> tuple[ForecastView, xr.DataTree] | None:
    """The forecast view and the plot tree (history + forecast with the split line) of a
    ``forecast`` score run scored with ``return_full``; ``None`` without draws."""
    if "predictions" not in tree or not _draws_rep(bundle) or req.horizon is None:
        return None
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    pred = node.to_dataset(inherit=False)
    var = _kpi_var(bundle, pred)
    if var is None:
        return None
    da = pred[var]
    t = _time_dim(da, bundle.binding.to_dataset(req.horizon.dim))
    if t is None or not {"chain", "draw"} <= set(map(str, da.dims)):
        return None
    values, summed = _samples(da, [t])
    times = np.asarray(da[t].values)
    units = _units(da)
    try:
        hist = _history(bundle, registry, tables, var)
    except Exception:  # the history is decoration; the forecast itself stands
        hist = None
    hist_times = np.asarray([])
    hist_values = np.asarray([])
    if hist is not None and t in hist.dims:
        other = [d for d in hist.dims if d != t]
        h = hist.sum(other, min_count=1) if other else hist
        h = h.sortby(t)
        h = h.where(h[t] < times.min(), drop=True) if times.size else h
        h = h.isel({t: slice(-MAX_HISTORY, None)})
        hist_times = np.asarray(h[t].values)
        hist_values = np.asarray(h.values, dtype=np.float64)
    q = np.quantile(values, LEVELS, axis=0) if values.size else np.zeros((len(LEVELS), 0))
    n_hist = int(hist_times.size)
    coords = [_label(x) for x in hist_times] + [_label(x) for x in times]
    fan = FanChart(
        dim=t,
        coords=coords,
        quantiles=list(LEVELS),
        values=[[None] * n_hist + [_f(v) for v in row] for row in q],
        observed=[_f(v) if math.isfinite(v) else None for v in hist_values] + [None] * times.size,
        units=units,
    )
    view = ForecastView(
        kpi=var[:128],
        units=units,
        kind=req.output.kind,
        dim=t,
        history=n_hist,
        split_at=coords[n_hist] if times.size else None,
        summed_over=summed[:32],
        fan=fan,
        total=_summary(values.sum(axis=1), units) if values.size else None,
    )
    return view, _forecast_tree(da, t, hist, hist_times, summed, var)


def _forecast_tree(
    da: xr.DataArray,
    t: str,
    hist: xr.DataArray | None,
    hist_times: np.ndarray,
    summed: list[str],
    var: str,
) -> xr.DataTree:
    """History + forecast as one series for ``core.ppc_fan``: predictions are missing over
    the history and observations over the forecast, so the split line falls in between."""
    total = da.sum(summed) if summed else da
    total = total.transpose("chain", "draw", t)
    if hist is None or not hist_times.size:
        return forecast_tree(total, None, var)
    pad = xr.DataArray(
        np.full((total.sizes["chain"], total.sizes["draw"], hist_times.size), np.nan),
        dims=["chain", "draw", t],
        coords={t: hist_times},
    )
    full = xr.concat([pad, total.drop_vars([c for c in total.coords if c not in total.dims])], t)
    other = [d for d in hist.dims if d != t]
    observed = (hist.sum(other, min_count=1) if other else hist).reindex({t: full[t].values})
    return forecast_tree(full, observed, var)


def forecast_tree(pred: xr.DataArray, observed: xr.DataArray | None, var: str) -> xr.DataTree:
    """A ``core.ppc_fan`` input: ``/predictions/<var>(chain, draw, t)`` [+ ``/observed``]."""
    nodes: dict[str, xr.Dataset] = {
        "/": xr.Dataset(attrs={"mode": "forecast"}),
        "/predictions": xr.Dataset({var: pred.assign_attrs(dict(pred.attrs))}),
    }
    if observed is not None:
        nodes["/observed"] = xr.Dataset({var: observed})
    return xr.DataTree.from_dict(nodes)


def counterfactual_view(
    bundle: Bundle, tree: xr.DataTree, req: ScoreRequest
) -> CounterfactualView | None:
    """The counterfactual view of a score run scored with ``return_full``."""
    if not _draws_rep(bundle) or "predictions" not in tree:
        return None
    try:
        base_node = tree["baseline/predictions"]
        delta_node = tree["delta/predictions"]
        scen_node = tree["predictions"]
    except KeyError:
        return None
    assert isinstance(base_node, xr.DataTree) and isinstance(delta_node, xr.DataTree)
    assert isinstance(scen_node, xr.DataTree)
    scen = scen_node.to_dataset(inherit=False)
    var = _kpi_var(bundle, scen)
    if var is None:
        return None
    s_da = scen[var]
    b_da = base_node.to_dataset(inherit=False)[var]
    d_da = delta_node.to_dataset(inherit=False)[var]
    if not {"chain", "draw"} <= set(map(str, s_da.dims)):
        return None
    s_tot, summed = _samples(s_da, [])
    b_tot, _ = _samples(b_da, [])
    d_tot, _ = _samples(d_da, [])
    units = _units(s_da)
    t = _time_dim(d_da, None)
    fan = None
    if t is not None:
        per_t, _ = _samples(d_da, [t])
        q = np.quantile(per_t, LEVELS, axis=0)
        fan = FanChart(
            dim=t,
            coords=[_label(x) for x in np.asarray(d_da[t].values)],
            quantiles=list(LEVELS),
            values=[[_f(v) for v in row] for row in q],
            units=units,
        )
    with np.errstate(divide="ignore", invalid="ignore"):
        share = np.where(np.abs(b_tot) > 0, d_tot / b_tot, np.nan)
    return CounterfactualView(
        kpi=var[:128],
        units=units,
        kind=req.output.kind,
        dim=t,
        summed_over=[d for d in summed if d != t][:32],
        interventions=[
            i.model_dump(mode="json", by_alias=True, exclude_none=True)
            for i in req.interventions[:64]
        ],
        scenario_total=_summary(s_tot, units),
        baseline_total=_summary(b_tot, units),
        delta_total=_summary(d_tot, units),
        delta_share=_summary(share, None) if np.isfinite(share).any() else None,
        delta_fan=fan,
    )


# ============================================================================== lift tests


def _labels(data: xr.Dataset, dim: str) -> set[str] | None:
    if dim not in data.coords:
        return None
    return {str(v) for v in np.asarray(data[dim].values).tolist()}


def check_lift_tests(spec: ModelSpec, data: xr.Dataset) -> None:
    """Lift tests whose channel, geo or dates are not in the raw dataset are refused
    (``SCHEMA_INVALID`` at their pointers) before anything is fitted."""
    cal = spec.calibration
    if cal is None or not cal.lift_tests:
        return
    dims = spec.data.bind.dims
    channel_dim = dims.get("channel", "channel")
    geo_dim = dims.get("geo", "geo")
    time_dim = dims.get("time", "time")
    channels = _labels(data, channel_dim)
    geos = _labels(data, geo_dim)
    first = last = None
    if time_dim in data.coords and np.issubdtype(data[time_dim].dtype, np.datetime64):
        days = np.asarray(data[time_dim].values).astype("datetime64[D]")
        if days.size:
            first = date.fromisoformat(str(days.min()))
            last = date.fromisoformat(str(days.max()))
    issues: list[Issue] = []
    for i, test in enumerate(cal.lift_tests):
        here = f"/calibration/lift_tests/{i}"
        if channels is not None and test.channel not in channels:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/channel",
                    message=f"the data has no channel {test.channel!r}",
                    suggested_action="Use the channel name exactly as it appears in the data.",
                )
            )
        if test.geo is not None and geos is not None and test.geo not in geos:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/geo",
                    message=f"the data has no geo {test.geo!r}",
                )
            )
        if first is not None and last is not None and (test.start < first or test.end > last):
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/start",
                    message=f"the test runs {test.start} to {test.end} but the data covers "
                    f"{first} to {last}",
                    suggested_action="Only enter tests that ran inside the data's dates.",
                )
            )
    if issues:
        raise ValidationFailed(issues)
