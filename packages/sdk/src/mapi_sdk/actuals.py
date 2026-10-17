"""Forecasts conditioned on actuals, sandbox side (the rules: :mod:`mapi_spec.actuals`).

:func:`~mapi_sdk.harness.score_bundle` runs this for every ``forecast`` request:

1. at the entry stage, :func:`observed_mask` finds the actuals (cells of the target that hold
   a value); a model whose *live* capabilities lack ``conditions_on_actuals`` is refused
   (``ACTUALS_NOT_SUPPORTED``) before anything runs;
2. :func:`append_horizon` adds ``horizon.periods`` periods after a payload that ends with an
   actual, and :func:`~mapi_sdk.future.fill_future_rows` fills empty drivers on the periods to
   forecast by the ``future_covariates`` policy, from the fitted history and the actuals;
3. after the pipeline, at the model stage, :func:`model_actuals` checks the actuals per series
   (gaps, drivers) and builds the :class:`ForecastActuals` handed to the plugin's
   ``forecast_with_actuals`` hook together with the
   :class:`~mapi_spec.actuals.ForecastConditioning` record the result carries.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.actuals import (
    ForecastConditioning,
    HorizonSource,
    SeriesConditioning,
    build_conditioning,
)
from mapi_spec.errors import ActualsInvalid, CoordPlanError, ErrorCode, Issue
from mapi_spec.schema import DataSchema, Role

from mapi_sdk._util import py_scalar
from mapi_sdk.coords import next_label

__all__ = [
    "ForecastActuals",
    "append_horizon",
    "drivers_along",
    "entry_fit_end",
    "future_mask",
    "label_text",
    "model_actuals",
    "observed_mask",
    "required_drivers",
]


@dataclass(frozen=True, slots=True)
class ForecastActuals:
    """The actuals of a forecast, as a plugin's ``forecast_with_actuals`` receives them.

    The dataset passed with it is the canonicalised model-stage payload: every requested
    period along :attr:`dim`, in order - the actuals (target values present), any periods
    inside the fitted range the caller sent, and the periods to forecast (target NaN), with
    the drivers (empty future drivers already filled by the ``future_covariates`` policy).

    * :attr:`observed` - bool over the target's dims: the target holds a supplied actual.
      Inside the fitted range (``label <= fit_end``) an actual *replaces* the fitted value.
    * :attr:`future` - bool, same dims: a period to forecast (after the cell's last actual
      and after the fit). Predictions there are the forecast conditioned on everything
      before; on the other periods they are one-step-ahead predictions, as in ``score``.
    * :attr:`gap` - periods between the fit and the last actual without a value: only
      present when the model's input schema allows missing targets (filter them as missing
      observations); otherwise the harness refused the request (``ACTUALS_INVALID``).

    Parameters are never re-estimated. :attr:`conditioning` is what the result reports.
    """

    dim: str
    fit_end: Any
    targets: tuple[str, ...]
    observed: xr.DataArray
    future: xr.DataArray
    conditioning: ForecastConditioning

    @property
    def after_fit(self) -> xr.DataArray:
        """bool over :attr:`dim`: the period lies after the fitted history."""
        return self.observed[self.dim] > np.asarray(self.fit_end)

    @property
    def revised(self) -> xr.DataArray:
        """Actuals inside the fitted range (they replace the fitted values)."""
        return self.observed & ~self.after_fit

    @property
    def gap(self) -> xr.DataArray:
        """Periods after the fit, before the cell's last actual, without a value."""
        return ~self.observed & ~self.future & self.after_fit

    def last_actual(self) -> xr.DataArray:
        """Per cell, the label of the last actual (NaT/NaN for cells without one)."""
        labels = xr.DataArray(np.asarray(self.observed[self.dim].values), dims=[self.dim])
        stamped = labels.broadcast_like(self.observed).where(self.observed)
        return stamped.max(self.dim, skipna=True).assign_coords(
            {
                d: self.observed[d]
                for d in stamped.dims
                if d != self.dim and d in self.observed.coords
            }
        )


def label_text(value: Any) -> str | int | float | bool:
    """A coordinate label as JSON (ISO text for dates)."""
    if isinstance(value, np.datetime64 | pd.Timestamp):
        ts = pd.Timestamp(value)
        return ts.isoformat()
    out = py_scalar(value)
    return out if isinstance(out, str | int | float | bool) else str(out)


def observed_mask(ds: xr.Dataset, targets: Sequence[str], dim: str) -> xr.DataArray | None:
    """bool over the targets' dims: where a target of ``ds`` holds a value along ``dim``;
    ``None`` when ``ds`` has no target running along ``dim``."""
    masks = [ds[t].notnull() for t in targets if t in ds and dim in ds[t].dims]
    if not masks:
        return None
    out = masks[0]
    for m in masks[1:]:
        out = out | m
    return out


def future_mask(observed: xr.DataArray, dim: str, fit_end: Any) -> xr.DataArray:
    """bool, like ``observed``: periods after the fit and after the cell's last actual."""
    labels = observed[dim]
    after_fit = labels > np.asarray(fit_end)
    seen = observed.astype(bool)
    # True from the cell's last actual backwards: a reversed cumulative "any"
    rev = seen.isel({dim: slice(None, None, -1)}).cumsum(dim) > 0
    upto_last = rev.isel({dim: slice(None, None, -1)})
    return (after_fit & ~upto_last).transpose(*observed.dims)


def append_horizon(
    ds: xr.Dataset, dim: str, periods: int, *, freq: str | None, step: Any = 1
) -> xr.Dataset:
    """``ds`` with ``periods`` more periods after its last label along ``dim`` (every
    variable empty there)."""
    labels = np.asarray(ds[dim].values)
    last = labels.max()
    new = [next_label(last, freq=freq, step=step, k=k) for k in range(1, periods + 1)]
    extended = np.concatenate([labels, np.asarray(new, dtype=labels.dtype)])
    return ds.reindex({dim: extended})


def _requested(ds: xr.Dataset, da: xr.DataArray) -> xr.DataArray:
    """bool over ``da``'s dims: real (non-padding) cells of the canonicalised payload."""
    keep = xr.ones_like(da, dtype=bool)
    for d in da.dims:
        flag = f"{d}__requested"
        if flag in ds.coords:
            keep = keep & ds[flag].astype(bool)
    return keep


def _cells(da: xr.DataArray, dim: str) -> list[dict[str, Any]]:
    others = [str(d) for d in da.dims if d != dim]
    if not others:
        return [{}]
    grids = [np.asarray(da[d].values) if d in da.coords else np.arange(da.sizes[d]) for d in others]
    return [
        {d: g[i] for d, g, i in zip(others, grids, idx, strict=True)}
        for idx in np.ndindex(*(len(g) for g in grids))
    ]


def _empty_on(canon: xr.Dataset, names: Collection[str], rows: xr.DataArray) -> list[str]:
    """The variables of ``names`` that are absent or empty somewhere on ``rows``."""
    out: list[str] = []
    for name in names:
        if name not in canon:
            if bool(rows.any()):
                out.append(name)
            continue
        da = canon[name]
        extra = [d for d in rows.dims if d not in da.dims]
        sel = rows.any(extra) if extra else rows
        if bool((da.isnull() & sel).any()):
            out.append(name)
    return out


def model_actuals(
    canon: xr.Dataset,
    *,
    dim: str,
    targets: Sequence[str],
    fit_end: Any,
    schema: DataSchema | None,
    horizon_source: HorizonSource,
    drivers: Collection[str] = (),
    skip: np.ndarray | None = None,
) -> tuple[ForecastActuals | None, ForecastConditioning]:
    """Check the actuals of the model-stage payload ``canon`` per series and describe them.

    ``skip`` (bool along ``dim``) marks rows that are not the caller's: context rows the
    pipeline prepended from the fitted history. Returns the :class:`ForecastActuals` for the
    plugin (``None`` when the payload has no actual) and the conditioning record (always).
    Raises ``ACTUALS_INVALID`` for gaps the model cannot filter and for actuals after the
    fit without their ``drivers``.
    """
    observed = observed_mask(canon, targets, dim)
    labels = np.asarray(canon[dim].values)
    if observed is None:  # no target in the payload: a plain forecast, per target cell
        dims = [dim]
        for t in targets:
            tdims = list(schema.vars[t].dims) if schema is not None and t in schema.vars else []
            if dim in tdims and all(d in canon.dims for d in tdims):
                dims = tdims
                break
        observed = xr.DataArray(
            np.zeros([canon.sizes[d] for d in dims], dtype=bool),
            dims=dims,
            coords={d: canon[d] for d in dims if d in canon.coords},
        )
    keep = _requested(canon, observed)
    if skip is not None:
        keep = keep & xr.DataArray(~np.asarray(skip, dtype=bool), dims=[dim])
    observed = observed & keep
    future = future_mask(observed, dim, fit_end) & keep
    after_fit = observed[dim] > np.asarray(fit_end)
    gap = ~observed & ~future & after_fit & keep
    any_actual = bool(observed.any())
    if any_actual and bool(gap.any()):
        missing_ok = schema is not None and any(
            schema.vars[t].missing == "allow" for t in targets if t in schema.vars
        )
        if not missing_ok:
            n = int(gap.sum())
            raise ActualsInvalid(
                f"the target is empty for {n} period(s) between the end of the fitted history "
                f"({label_text(fit_end)}) and the last actual, and the model needs a complete "
                "history",
                pointer="/data",
                issues=[
                    Issue(
                        code=ErrorCode.ACTUALS_INVALID,
                        pointer="/data",
                        message=f"the target is empty for {n} period(s) between the end of "
                        "the fitted history and the last actual",
                        suggested_action="Fill the empty periods, or send only the actuals "
                        "up to the first empty period.",
                    )
                ],
            )
    lacking = _empty_on(canon, drivers, observed & after_fit)
    if lacking:
        raise ActualsInvalid(
            f"the actuals after the fitted history need the drivers {sorted(lacking)} observed "
            "with them",
            pointer="/data",
            issues=[
                Issue(
                    code=ErrorCode.ACTUALS_INVALID,
                    pointer="/data",
                    message=f"the actuals after the fitted history need the drivers "
                    f"{sorted(lacking)} observed with them",
                    suggested_action="Send the drivers for the periods with actuals.",
                )
            ],
        )
    short = _empty_on(canon, drivers, future) if any_actual else []
    if short:
        raise CoordPlanError(
            f"the forecast needs future values of the drivers {sorted(short)} on the periods "
            "after the last actual",
            code=ErrorCode.MISSING_FUTURE_COVARIATE,
            pointer="/data",
            issues=[
                Issue(
                    code=ErrorCode.MISSING_FUTURE_COVARIATE,
                    pointer="/data",
                    message=f"the forecast needs future values of the drivers {sorted(short)} "
                    "on the periods after the last actual",
                    suggested_action="Fill them in the payload, or set future_covariates "
                    "(carry_last, seasonal_naive or zero).",
                )
            ],
        )
    records: list[SeriesConditioning] = []
    for cell in _cells(observed, dim):
        sel = {d: v for d, v in cell.items()}
        if not bool(keep.sel(sel).any()):
            continue  # a padding cell (not requested)
        obs = np.asarray(observed.sel(sel).values, dtype=bool)
        fut = np.asarray(future.sel(sel).values, dtype=bool)
        gp = np.asarray(gap.sel(sel).values, dtype=bool)
        seen = labels[obs]
        ahead = labels[fut]
        through = seen.max() if seen.size else None
        origin = fit_end if through is None or through < fit_end else through
        records.append(
            SeriesConditioning(
                series={d: label_text(v) for d, v in cell.items()},
                n_actuals=int(obs.sum()),
                first_actual=label_text(seen.min()) if seen.size else None,
                actuals_through=label_text(through) if through is not None else None,
                revised=int((obs & (labels <= fit_end)).sum()),
                missing=int(gp.sum()) if seen.size else 0,
                origin=label_text(origin),
                forecast_from=label_text(ahead.min()) if ahead.size else None,
                forecast_to=label_text(ahead.max()) if ahead.size else None,
                n_forecast=int(fut.sum()),
            )
        )
    record = build_conditioning(
        dim=dim,
        fit_end=label_text(fit_end),
        series=records,
        horizon_source=horizon_source,
    )
    if not any_actual:
        return None, record
    return (
        ForecastActuals(
            dim=dim,
            fit_end=fit_end,
            targets=tuple(t for t in targets if t in canon),
            observed=observed,
            future=future,
            conditioning=record,
        ),
        record,
    )


def required_drivers(*schemas: DataSchema | None) -> list[str]:
    """Non-target variables a forecast needs on its periods (``future_required``)."""
    out: set[str] = set()
    for schema in schemas:
        if schema is None:
            continue
        out |= {
            n for n, v in schema.vars.items() if v.future_required and v.role is not Role.TARGET
        }
    return sorted(out)


def drivers_along(schema: DataSchema | None, dim: str, targets: Collection[str]) -> list[str]:
    """Non-target variables of ``schema`` that run along ``dim``."""
    if schema is None:
        return []
    return [
        n
        for n, v in schema.vars.items()
        if dim in v.dims and n not in targets and v.role is not Role.TARGET
    ]


def entry_fit_end(fit_coords: Mapping[str, xr.Dataset], stage: str, dim: str) -> Any | None:
    """The last fitted label along ``dim`` at ``stage`` (``None`` when unknown)."""
    coords = fit_coords.get(stage)
    if coords is None or dim not in coords.coords:
        return None
    values = np.asarray(coords[dim].values)
    return values.max() if values.size else None
