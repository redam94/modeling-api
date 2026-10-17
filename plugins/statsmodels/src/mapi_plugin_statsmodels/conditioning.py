"""Forecasts conditioned on actuals (:mod:`mapi_spec.actuals`): the extended history of a cell.

A forecast request may carry actuals - target values for periods after the fitted history,
or revising its last periods - and the periods to forecast (target empty). Per series cell,
:func:`extend_cell` turns the stored training data and the payload into the history the
model is run over with its **fitted** parameters:

* an actual inside the fitted range replaces the stored value (a revision); the drivers the
  payload sends with it replace the stored ones too (it is a revised observation);
* actuals after the fit extend the history up to the cell's last actual; periods between
  them without a value stay missing (only models whose input schema allows missing targets
  get here - the harness refuses the others with ``ACTUALS_INVALID``);
* the payload's periods after the last actual are forecast, multi-step, from it.

Each plugin then applies its fitted parameters to the extended history without
re-estimating them (:meth:`~mapi_plugin_statsmodels.base.StatsmodelsModel.predict_conditioned`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from mapi_spec.errors import ActualsInvalid, ErrorCode, Issue, MapiError

from mapi_plugin_statsmodels._common import Cell

__all__ = ["Extension", "extend_cell"]


@dataclass(frozen=True, slots=True)
class Extension:
    """A cell's history extended with the actuals, and where each payload row comes from.

    ``cell`` holds the fitted periods followed by the periods up to the last actual;
    ``insample`` the positions in ``cell`` of the payload rows that are not forecast (in
    payload order: one-step-ahead predictions there); ``steps`` the 1-based forecast steps
    of the payload rows after the last actual; ``x_future`` the drivers over those steps.
    """

    cell: Cell
    insample: np.ndarray
    steps: np.ndarray
    x_future: np.ndarray | None

    @property
    def horizon(self) -> int:
        """Steps to forecast after the last actual (0 when nothing is forecast)."""
        return int(self.steps.max()) if self.steps.size else 0


def _label(value: object) -> str:
    text = str(value)
    return text[:10] if isinstance(value, np.datetime64) and text[10:19] == "T00:00:00" else text


def _invalid(message: str, action: str) -> ActualsInvalid:
    return ActualsInvalid(
        message,
        pointer="/data",
        issues=[
            Issue(
                code=ErrorCode.ACTUALS_INVALID,
                pointer="/data",
                message=message,
                suggested_action=action,
            )
        ],
    )


def extend_cell(
    fit: Cell,
    *,
    labels: np.ndarray,
    y_rows: np.ndarray,
    observed: np.ndarray,
    x_rows: np.ndarray | None,
    allow_missing: bool,
) -> Extension:
    """Extend the fitted cell ``fit`` with a payload's actuals.

    ``labels`` are the payload's periods in ascending order, every one either a fitted
    period or after the fit; ``y_rows`` (``(L,)`` or ``(L, k)``) the payload's target values
    and ``observed`` where they are actuals; ``x_rows`` (``(L, m)``) the payload's drivers in
    ``fit.exog_names`` order (NaN where absent). Raises ``ACTUALS_INVALID`` for jointly
    modelled series observed on different periods after the fit, gaps a model cannot filter
    and actuals or gaps without their drivers; ``MISSING_FUTURE_COVARIATE`` for periods to
    forecast without drivers.
    """
    hist = np.asarray(fit.t)
    n = fit.n
    rows = labels.size
    obs = np.asarray(observed, dtype=bool).reshape(rows, -1)
    obs_any = np.asarray(obs.any(axis=1), dtype=bool)
    after = np.asarray(labels > hist.max(), dtype=bool)
    in_fit = ~after
    if fit.variables:
        partial = after & obs_any & ~obs.all(axis=1)
        if partial.any():
            first = int(np.flatnonzero(partial)[0])
            lacking = [v for v, seen in zip(fit.variables, obs[first], strict=True) if not seen]
            raise _invalid(
                f"the series {fit.variables} are modelled jointly, so an actual after the "
                f"fitted history needs a value for every one of them: {lacking} have none "
                f"on {_label(labels[first])} ({fit.label})",
                "Send the values of every series for the periods with actuals, or leave "
                "the period empty for all of them to forecast it.",
            )
    after_rows = np.flatnonzero(after)
    seen_after = np.flatnonzero(obs_any[after_rows])
    k = int(seen_after[-1]) + 1 if seen_after.size else 0
    new_rows, future_rows = after_rows[:k], after_rows[k:]

    y = np.asarray(fit.y, dtype=float)
    pad = np.full((k, *y.shape[1:]), np.nan)
    y_ext = np.concatenate([y.copy(), pad], axis=0)
    vals = np.asarray(y_rows, dtype=float).reshape(rows, *y.shape[1:])
    obs_shaped = obs.reshape(vals.shape)
    fit_pos = np.searchsorted(hist, labels[in_fit])
    for row, pos in zip(np.flatnonzero(in_fit), fit_pos, strict=True):
        y_ext[pos] = np.where(obs_shaped[row], vals[row], y_ext[pos])
    for j, row in enumerate(new_rows):
        y_ext[n + j] = np.where(obs_shaped[row], vals[row], np.nan)
    width = int(np.prod(y.shape[1:], dtype=np.int64))
    gaps = ~np.isfinite(y_ext[n:].reshape(k, width)).all(axis=1)
    if gaps.any() and not allow_missing:
        raise _invalid(
            f"{fit.label}: the target is empty on {_label(labels[new_rows][gaps][0])}, between "
            "the fitted history and the last actual, and the model needs a complete history",
            "Fill the empty periods, or send only the actuals up to the first empty period.",
        )

    x_ext = None
    x_future = None
    if fit.x is not None:
        xr_ = np.asarray(x_rows, dtype=float) if x_rows is not None else None
        if xr_ is None:
            xr_ = np.full((rows, fit.x.shape[1]), np.nan)
        x_ext = np.concatenate([np.asarray(fit.x, dtype=float).copy(), xr_[new_rows]], axis=0)
        revised = np.flatnonzero(in_fit & obs_any)
        for row in revised:
            pos = int(np.searchsorted(hist, labels[row]))
            x_ext[pos] = np.where(np.isfinite(xr_[row]), xr_[row], x_ext[pos])
        _require(fit.exog_names, x_ext[n:], labels[new_rows], fit.label, future=False)
        x_future = xr_[future_rows]
        _require(fit.exog_names, x_future, labels[future_rows], fit.label, future=True)

    ext = Cell(
        key=dict(fit.key),
        y=y_ext,
        x=x_ext,
        exog_names=list(fit.exog_names),
        variables=list(fit.variables),
        t=np.concatenate([hist, labels[new_rows]]),
    )
    keep = np.ones(rows, dtype=bool)
    keep[future_rows] = False
    position = np.empty(rows, dtype=np.int64)
    position[in_fit] = fit_pos
    position[new_rows] = n + np.arange(k)
    return Extension(
        cell=ext,
        insample=position[keep],
        steps=np.arange(1, future_rows.size + 1, dtype=np.int64),
        x_future=x_future,
    )


def _require(
    names: Sequence[str], x: np.ndarray, labels: np.ndarray, where: str, *, future: bool
) -> None:
    """Every driver must have a value on the extended (or forecast) periods."""
    missing = ~np.isfinite(x)
    if not missing.any():
        return
    lacking = [str(n) for j, n in enumerate(names) if missing[:, j].any()]
    first = _label(labels[int(np.flatnonzero(missing.any(axis=1))[0])])
    if future:
        raise MapiError(
            f"{where}: the forecast needs future values of the drivers {lacking} (first "
            f"missing on {first})",
            code=ErrorCode.MISSING_FUTURE_COVARIATE,
            pointer="/data",
        )
    raise _invalid(
        f"{where}: the drivers {lacking} are needed on every period up to the last actual "
        f"(first missing on {first})",
        "Send the drivers for the periods with actuals and for any empty period between them.",
    )
