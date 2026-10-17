"""Per-step, per-group context tails: recorded at fit time, replayed at score time.

A step that looks back along a dim (lags, rolling windows) declares a
:class:`~mapi_spec.effects.ContextNeed` ``{along, n, within}``. At fit time the pipeline
records the last ``n`` rows along ``along`` of the step's **input stage**, per group of
``within``. At score time those rows are prepended to the score data at the same stage
(after a contiguity check), the step transforms the combined data, and the context rows are
dropped again after the model has scored.

Two layouts are supported:

* **dense** - ``along`` is a dim shared by every group (``week`` in ``(market, week)``);
  the tail is the last ``n`` labels of that dim;
* **long** - ``along`` is a row dim (``obs``) and ``within`` names 1-D variables or coords
  along it (``hh``); the tail is the last ``n`` rows of each group.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.effects import ContextNeed
from mapi_spec.errors import CoordPlanError, ErrorCode, Issue, Severity

from mapi_sdk._util import py_scalar

__all__ = [
    "ContextMark",
    "context_layout",
    "drop_context",
    "extract_context",
    "prepend_context",
]


@dataclass(frozen=True, slots=True)
class ContextMark:
    """Labels of context rows prepended along ``dim`` (dropped after scoring)."""

    dim: str
    labels: np.ndarray
    score_start: Any = None
    step_id: str = ""


def context_layout(ds: xr.Dataset, need: ContextNeed) -> Literal["dense", "long"]:
    """Decide how ``need`` applies to ``ds`` (see the module docstring)."""
    if need.along not in ds.dims:
        raise CoordPlanError(
            f"context dim {need.along!r} does not exist in the data",
            code=ErrorCode.COORD_NOT_ALLOWED,
        )
    if all(w in ds.dims for w in need.within):
        return "dense"
    for w in need.within:
        if w not in ds.variables or ds[w].dims != (need.along,):
            raise CoordPlanError(
                f"context group {w!r} is neither a dim nor a variable along {need.along!r}",
                code=ErrorCode.COORD_NOT_ALLOWED,
            )
    return "long"


def _along_vars(ds: xr.Dataset, along: str) -> list[Hashable]:
    return [v for v in ds.data_vars if along in ds[v].dims]


def extract_context(ds: xr.Dataset, need: ContextNeed | None) -> xr.Dataset | None:
    """The last ``need.n`` rows along ``need.along`` (per group for long layouts)."""
    if need is None or need.n == 0 or need.along not in ds.dims:
        return None
    along = need.along
    keep = _along_vars(ds, along)
    if context_layout(ds, need) == "dense":
        n = min(need.n, int(ds.sizes[along]))
        return ds[keep].isel({along: slice(ds.sizes[along] - n, None)})
    frame = pd.DataFrame({w: np.asarray(ds[w].values) for w in need.within})
    rows = frame.groupby(list(need.within), sort=False, dropna=False).tail(need.n).index
    sub = ds.isel({along: np.sort(np.asarray(rows))})
    extra = [w for w in need.within if w not in keep and w in sub.data_vars]
    return sub[[*keep, *extra]]


def _ordered(values: np.ndarray) -> bool:
    return values.dtype.kind in "Miuf"


def _next_label(last: Any, freq: str | None, step: Any) -> Any:
    if isinstance(last, np.datetime64):
        if freq is None:
            return None
        return np.datetime64(pd.Timestamp(last) + pd.tseries.frequencies.to_offset(freq), "ns")
    return last + step


def _fmt(v: Any) -> str:
    return str(py_scalar(v))


def prepend_context(
    ds: xr.Dataset,
    context: xr.Dataset | None,
    need: ContextNeed | None,
    *,
    freq: str | None = None,
    step_id: str = "",
    replayed: np.ndarray | None = None,
    fit_start: Any = None,
) -> tuple[xr.Dataset, ContextMark | None, list[Issue]]:
    """Prepend stored context rows to score data (contiguity-checked).

    ``replayed`` lists labels along ``need.along`` that earlier steps prepended as their
    own context. Those rows were transformed without *their* look-back history, so where
    this step's stored context has the fit-time values for them, the stored values win
    (chained look-back steps such as adstock followed by a lag replay exactly).

    ``fit_start`` (the first fitted label along ``need.along``) tells a window that starts
    with the fitted series - whose first rows had no history at fit either - from one
    that starts inside it, which gets a WARNING because its first rows lose history.

    Returns the combined dataset, a :class:`ContextMark` of the prepended labels (or
    ``None``) and informational issues. Raises ``NON_CONTIGUOUS_EXTENSION`` when the score
    data starts after a gap following the fitted range.
    """
    if context is None or need is None or need.n == 0 or need.along not in ds.dims:
        return ds, None, []
    if context_layout(ds, need) == "long":
        return _prepend_long(ds, context, need, step_id)
    return _prepend_dense(ds, context, need, freq, step_id, replayed, fit_start)


def _overwrite_replayed(
    ds: xr.Dataset, context: xr.Dataset, along: str, labels: np.ndarray
) -> xr.Dataset:
    """Replace ``ds`` rows at ``labels`` (earlier steps' context rows) by stored values."""
    ctx = context.sel({along: labels})
    updates: dict[Hashable, xr.DataArray] = {}
    for v in _along_vars(ds, along):
        if v not in ctx or set(ctx[v].dims) != set(ds[v].dims):
            continue
        stored = ctx[v].reindex({along: ds[along].values})
        for d in stored.dims:
            if d != along and d in ds.coords and d in stored.coords:
                stored = stored.reindex({d: ds[d].values})
        hit = xr.DataArray(np.isin(ds[along].values, labels), dims=[along])
        merged = stored.where(hit, ds[v]).transpose(*ds[v].dims)
        merged.attrs = ds[v].attrs
        updates[v] = merged.astype(ds[v].dtype) if ds[v].dtype.kind == "f" else merged
    return ds.assign(updates) if updates else ds


def _prepend_dense(
    ds: xr.Dataset,
    context: xr.Dataset,
    need: ContextNeed,
    freq: str | None,
    step_id: str,
    replayed: np.ndarray | None = None,
    fit_start: Any = None,
) -> tuple[xr.Dataset, ContextMark | None, list[Issue]]:
    along = need.along
    issues: list[Issue] = []
    if along not in context.dims or context.sizes[along] == 0 or ds.sizes[along] == 0:
        return ds, None, issues
    score = np.asarray(ds[along].values) if along in ds.coords else None
    ctx_labels = np.asarray(context[along].values) if along in context.coords else None
    if score is None or ctx_labels is None:
        raise CoordPlanError(
            f"step {step_id!r}: context along {along!r} needs labelled coordinates",
            code=ErrorCode.COORD_NOT_ALLOWED,
        )
    if replayed is not None and replayed.size:
        overlap = ctx_labels[np.isin(ctx_labels, replayed) & np.isin(ctx_labels, score)]
        if overlap.size:
            ds = _overwrite_replayed(ds, context, along, overlap)
    start = score.min() if _ordered(score) else score[0]
    if _ordered(score) and _ordered(ctx_labels):
        before = ctx_labels < start
        last = ctx_labels.max()
        if start > last:
            step = np.diff(np.sort(ctx_labels)).min() if ctx_labels.size > 1 else 1
            expected = _next_label(last, freq, step)
            if expected is not None and start != expected:
                raise CoordPlanError(
                    f"step {step_id!r}: score data along {along!r} starts at {_fmt(start)} but "
                    f"the fitted data ends at {_fmt(last)}; the extension is not contiguous",
                    code=ErrorCode.NON_CONTIGUOUS_EXTENSION,
                    untrusted_text=f"expected {_fmt(expected)}",
                )
        elif start < ctx_labels.min() and (fit_start is None or start > fit_start):
            # the step only stores its last ``n`` rows: the window's first ``n`` rows are
            # computed as if the series started there (biased lags/carry-over)
            issues.append(
                Issue(
                    code=ErrorCode.NON_CONTIGUOUS_EXTENSION,
                    severity=Severity.WARNING,
                    message=f"step {step_id!r}: score data starts inside the fitted range "
                    f"before the stored context; its first {need.n} row(s) per group have "
                    "no look-back history and are biased (score from the start of the fit "
                    "or a window that continues it)",
                )
            )
    else:
        before = ~np.isin(ctx_labels, score)
    if not before.any():
        return ds, None, issues
    ctx = context.isel({along: np.flatnonzero(before)})
    for d in ctx.dims:
        if d != along and d in ds.dims and d in ds.coords and d in ctx.coords:
            ctx = ctx.reindex({d: ds[d].values})
    along_vars = _along_vars(ds, along)
    parts: dict[Hashable, xr.DataArray] = {}
    for v in along_vars:
        cur = ds[v]
        if v in ctx:
            head = ctx[v]
        else:
            head = xr.full_like(
                cur.isel({along: np.zeros(ctx.sizes[along], dtype=int)}), np.nan, dtype=float
            ).assign_coords({along: ctx[along].values})
        combined = xr.concat([head, cur], dim=along, join="outer", combine_attrs="override")
        combined.attrs = cur.attrs
        parts[v] = combined.transpose(*cur.dims)
    rest = ds.drop_dims(along)
    out = xr.Dataset(parts).merge(rest, join="outer", combine_attrs="override")
    if along not in out.dims:
        # coordinate-only frame (e.g. a forecast horizon with no payload): keep the axis
        out = out.assign_coords({along: np.concatenate([np.asarray(ctx[along].values), score])})
    out.attrs = ds.attrs
    labels = np.asarray(ctx[along].values).copy()
    mark = ContextMark(dim=along, labels=labels, score_start=start, step_id=step_id)
    return out, mark, issues


def _prepend_long(
    ds: xr.Dataset, context: xr.Dataset, need: ContextNeed, step_id: str
) -> tuple[xr.Dataset, ContextMark | None, list[Issue]]:
    along = need.along
    within = list(need.within)
    missing = [w for w in within if w not in context.variables]
    if missing or context.sizes.get(along, 0) == 0:
        return ds, None, []
    score_keys = pd.MultiIndex.from_arrays([np.asarray(ds[w].values) for w in within])
    ctx_keys = pd.MultiIndex.from_arrays([np.asarray(context[w].values) for w in within])
    rows = np.flatnonzero(ctx_keys.isin(score_keys))
    if rows.size == 0:
        return ds, None, []
    ctx = context.isel({along: rows})
    if along not in ds.coords:
        ds = ds.assign_coords({along: np.arange(ds.sizes[along])})
    score_labels = np.asarray(ds[along].values)
    k = rows.size
    if score_labels.dtype.kind in "iu":
        base = min(int(score_labels.min()) if score_labels.size else 0, 0)
        new_labels: np.ndarray = np.arange(base - k, base)
    elif score_labels.dtype.kind in ("U", "O", "T"):
        new_labels = np.array([f"__context__{i}" for i in range(k)])
    else:
        raise CoordPlanError(
            f"step {step_id!r}: long-layout context needs integer or string row labels",
            code=ErrorCode.COORD_NOT_ALLOWED,
        )
    ctx = ctx.assign_coords({along: new_labels})
    along_vars = _along_vars(ds, along)
    parts: dict[Hashable, xr.DataArray] = {}
    for v in along_vars:
        cur = ds[v]
        head = (
            ctx[v]
            if v in ctx
            else xr.full_like(
                cur.isel({along: np.zeros(k, dtype=int)}), np.nan, dtype=float
            ).assign_coords({along: new_labels})
        )
        combined = xr.concat([head, cur], dim=along, join="outer", combine_attrs="override")
        combined.attrs = cur.attrs
        parts[v] = combined.transpose(*cur.dims)
    coord_parts: dict[Hashable, Any] = {}
    for c in ds.coords:
        if c != along and ds[c].dims == (along,) and c in ctx.coords:
            coord_parts[c] = (along, np.concatenate([ctx[c].values, ds[c].values]))
    rest = ds.drop_dims(along)
    out = xr.Dataset(parts).merge(rest, join="outer", combine_attrs="override")
    if coord_parts:
        out = out.assign_coords(coord_parts)
    out.attrs = ds.attrs
    mark = ContextMark(dim=along, labels=new_labels, step_id=step_id)
    return out, mark, []


def drop_context(obj: xr.Dataset, marks: Sequence[ContextMark]) -> xr.Dataset:
    """Remove prepended context rows (by label; by position before ``score_start`` if the
    labels were transformed by a later step)."""
    out = obj
    for mark in marks:
        if mark.dim not in out.dims or mark.dim not in out.coords:
            continue
        current = np.asarray(out[mark.dim].values)
        present = np.isin(current, mark.labels)
        if present.any():
            out = out.isel({mark.dim: np.flatnonzero(~present)})
        elif mark.score_start is not None and _ordered(current):
            out = out.isel({mark.dim: np.flatnonzero(current >= mark.score_start)})
    return out
