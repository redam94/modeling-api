"""CoordPlan checks, coordinate canonicalisation and horizon coordinates (plan §4.2-4.3).

* :class:`CoordPlan` runs the static checks of :func:`mapi_spec.validate.validate_score_request`
  against the bundle's fit coordinates and the actual score payload, raising typed errors
  (``UNSEEN_LEVELS``, ``COORD_NOT_ALLOWED``, ``HORIZON_EXCEEDS_FIT``,
  ``MISSING_FUTURE_COVARIATE``, ``NON_CONTIGUOUS_EXTENSION``, ...).
* :func:`canonicalise_coords` reorders model-stage score data to the fit order, pads requested
  subsets to the full fit set, appends unseen levels (flagged by ``<dim>__is_new``) and
  returns a :class:`CanonicalPlan` that selects the requested coords back afterwards.
* :func:`horizon_coords` / :func:`horizon_frame` generate future coordinates from a dim's
  frequency (or integer step).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.actuals import DataSpace
from mapi_spec.capabilities import Capabilities
from mapi_spec.coords import CoordSummary
from mapi_spec.errors import CoordPlanError, ErrorCode, Issue, Severity
from mapi_spec.schema import DataSchema, DimPolicy, Role
from mapi_spec.spec import ScoreRequest
from mapi_spec.validate import ResolvedSpec, ScoreMetadata, validate_score_request

from mapi_sdk._util import py_scalar
from mapi_sdk.context import ContextMark
from mapi_sdk.errors import raise_for_issues
from mapi_sdk.schema import data_summary, infer_freq, summarize_coords

__all__ = [
    "CanonicalPlan",
    "CoordPlan",
    "DimCanon",
    "canonicalise_coords",
    "horizon_coords",
    "horizon_frame",
    "next_label",
]


def _preview(values: Sequence[Any] | np.ndarray, n: int = 10) -> str:
    vals = list(values)
    s = ", ".join(str(py_scalar(v)) for v in vals[:n])
    return s + (f", ... (+{len(vals) - n})" if len(vals) > n else "")


# ===================================================================================== CoordPlan


class CoordPlan:
    """Typed early checks for a :class:`~mapi_spec.spec.ScoreRequest`.

    ``fit_coords`` maps each stage name to a coordinates-only dataset of the fitted data at
    that stage (stored in the bundle). Raises the most specific error for the first problem
    and returns the remaining warnings/infos.
    """

    def __init__(
        self,
        request: ScoreRequest,
        resolved: ResolvedSpec,
        fit_coords: Mapping[str, xr.Dataset],
        *,
        max_draws: int | None = None,
        live_capabilities: Capabilities | None = None,
        data_space: DataSpace | None = None,
    ) -> None:
        self.request = request
        self.resolved = resolved
        self.fit_coords = dict(fit_coords)
        self.max_draws = max_draws
        self.live_capabilities = live_capabilities
        self.data_space: DataSpace | None = data_space

    def metadata(self) -> ScoreMetadata:
        summaries: dict[str, dict[str, CoordSummary]] = {}
        for stage, coords in self.fit_coords.items():
            summaries[stage] = summarize_coords(coords, self.resolved.stages.get(stage))
        return ScoreMetadata(
            resolved=self.resolved, fit_coords=summaries, data_space=self.data_space
        )

    def check(self, inputs: xr.Dataset | None = None) -> list[Issue]:
        """Run the checks; ``inputs`` is the payload at ``inputs_stage`` (canonical names)."""
        schema = self.resolved.stages.get(self.request.inputs_stage)
        summary = data_summary(inputs, schema) if inputs is not None else None
        issues = validate_score_request(
            self.request,
            self.metadata(),
            summary,
            max_draws=self.max_draws,
            live_capabilities=self.live_capabilities,
        )
        raise_for_issues(issues)
        return issues


# ============================================================================== canonicalisation


@dataclass(slots=True)
class DimCanon:
    """How one dim was canonicalised."""

    dim: str
    policy: DimPolicy
    requested: np.ndarray
    order: np.ndarray
    new: np.ndarray
    padded: bool


@dataclass(slots=True)
class CanonicalPlan:
    """Records canonicalisation so outputs can be mapped back to the requested coords."""

    dims: dict[str, DimCanon] = field(default_factory=dict)

    def new_levels(self, dim: str) -> list[Any]:
        d = self.dims.get(dim)
        return [] if d is None else [py_scalar(v) for v in d.new]

    def restore(self, obj: xr.Dataset) -> xr.Dataset:
        """Select the requested labels (request order) and drop helper coordinates."""
        out = obj
        for name, d in self.dims.items():
            if name in out.dims and name in out.coords:
                out = out.sel({name: d.requested})
        helpers = [
            c
            for c in out.coords
            if str(c).endswith(("__is_new", "__requested")) and str(c).split("__")[0] in self.dims
        ]
        return out.drop_vars(helpers)


def next_label(last: Any, *, freq: str | None = None, step: Any = 1, k: int = 1) -> Any:
    """The label ``k`` periods after ``last`` (datetimes use ``freq``)."""
    if isinstance(last, np.datetime64 | pd.Timestamp):
        if freq is None:
            raise CoordPlanError(
                "cannot generate future dates without a frequency",
                code=ErrorCode.COORD_NOT_ALLOWED,
            )
        offset = pd.tseries.frequencies.to_offset(freq)
        return np.datetime64(pd.Timestamp(last) + k * offset, "ns")
    return last + k * step


def _step_of(values: np.ndarray) -> Any:
    """Smallest positive spacing of numeric labels (datetimes step by frequency instead)."""
    if values.size < 2 or values.dtype.kind not in "iuf":
        return 1
    diffs = np.diff(np.sort(values))
    pos = diffs[diffs > 0]
    return pos.min() if pos.size else 1


def _freq_of(values: np.ndarray, schema: DataSchema | None, dim: str) -> str | None:
    if schema is not None and dim in schema.dims and schema.dims[dim].freq:
        return schema.dims[dim].freq
    if values.dtype.kind == "M":
        return infer_freq(values)
    return None


def _context_labels(marks: Collection[ContextMark], dim: str) -> np.ndarray:
    parts = [m.labels for m in marks if m.dim == dim]
    return np.concatenate(parts) if parts else np.array([])


def canonicalise_coords(
    ds: xr.Dataset,
    fit_coords: xr.Dataset,
    schema: DataSchema | None,
    *,
    pad_extend: bool = True,
    marks: Collection[ContextMark] = (),
    pad_fill: float = 0.0,
) -> tuple[xr.Dataset, CanonicalPlan]:
    """Reindex model-stage score data to the fit coordinate order.

    * ``fixed`` dims: labels must be fit labels (``UNSEEN_LEVELS``); data is reindexed to
      the full fit set (padding unrequested labels);
    * ``new_levels`` dims: fit labels in fit order, then unseen labels appended (the
      ``<dim>__is_new`` coordinate flags them; strategy ``error`` raises);
    * ``extend`` dims: labels may not start before the fit range (``COORD_NOT_ALLOWED``),
      must continue it without gaps (``NON_CONTIGUOUS_EXTENSION``) and may not exceed
      ``max_extension`` (``HORIZON_EXCEEDS_FIT``); with ``pad_extend`` the full fit range is
      included so positional models see a contiguous axis.

    Padded cells of numeric non-target variables are filled with ``pad_fill`` and target
    variables with ``NaN``; ``<dim>__requested`` marks real labels.
    """
    plan = CanonicalPlan()
    targets = set(schema.targets) if schema is not None else set()
    out = ds
    for d in list(ds.dims):
        dim = str(d)
        if schema is None or dim not in schema.dims or dim not in fit_coords.coords:
            continue
        if dim not in ds.coords:
            continue
        spec = schema.dims[dim]
        fit = np.asarray(fit_coords[dim].values)
        cur = np.asarray(ds[dim].values)
        ctx = _context_labels(marks, dim)
        requested = cur[~np.isin(cur, ctx)] if ctx.size else cur
        if ctx.size:
            # long-layout context rows carry synthetic labels outside the fit set
            # (``-k..-1``); they are dropped after scoring and must not be judged (or
            # ordered) as score coordinates. Dense context labels are fit labels and stay.
            synthetic = np.isin(cur, ctx) & ~np.isin(cur, fit)
            cur = cur[~synthetic]
        unseen = cur[~np.isin(cur, fit)]
        new = np.array([], dtype=cur.dtype)
        match spec.policy:
            case DimPolicy.FIXED:
                if unseen.size:
                    raise CoordPlanError(
                        f"{unseen.size} level(s) of {dim!r} were not seen at fit and the dim's "
                        "policy is 'fixed'",
                        code=ErrorCode.UNSEEN_LEVELS,
                        untrusted_text=_preview(unseen),
                    )
                order = fit
            case DimPolicy.NEW_LEVELS:
                if unseen.size and spec.new_level_strategy == "error":
                    raise CoordPlanError(
                        f"{unseen.size} level(s) of {dim!r} were not seen at fit and its "
                        "new-level strategy is 'error'",
                        code=ErrorCode.UNSEEN_LEVELS,
                        untrusted_text=_preview(unseen),
                    )
                new = unseen
                order = np.concatenate([fit, unseen]) if unseen.size else fit
            case _:
                order = _extend_order(dim, spec.max_extension, fit, cur, schema, pad_extend)
        labels = np.asarray(ds[dim].values)
        padded = not np.array_equal(order, labels)
        if padded:
            out = _reindex(out, dim, order, targets, pad_fill)
            out = _remap_index_vars(out, ds, dim, labels, order, schema)
        flags = {f"{dim}__requested": (dim, np.isin(order, requested))}
        if spec.policy is DimPolicy.NEW_LEVELS:
            flags[f"{dim}__is_new"] = (dim, np.isin(order, new))
        out = out.assign_coords(flags)
        plan.dims[dim] = DimCanon(dim, spec.policy, requested, order, new, padded)
    return out, plan


def _extend_order(
    dim: str,
    max_extension: int | None,
    fit: np.ndarray,
    cur: np.ndarray,
    schema: DataSchema | None,
    pad_extend: bool,
) -> np.ndarray:
    if cur.size == 0:
        return fit if pad_extend else cur
    if fit.size == 0:
        return np.sort(cur)
    lo, hi = fit.min(), fit.max()
    if cur.min() < lo:
        raise CoordPlanError(
            f"score coords of {dim!r} start before the fitted range",
            code=ErrorCode.COORD_NOT_ALLOWED,
            untrusted_text=f"{py_scalar(cur.min())} < {py_scalar(lo)}",
        )
    beyond = np.sort(cur[cur > hi])
    if beyond.size:
        freq = _freq_of(fit, schema, dim)
        step = _step_of(fit)
        if fit.dtype.kind == "M" and freq is None:
            raise CoordPlanError(
                f"cannot check the extension of {dim!r}: its frequency is unknown",
                code=ErrorCode.NON_CONTIGUOUS_EXTENSION,
            )
        expected = np.array(
            [next_label(hi, freq=freq, step=step, k=k + 1) for k in range(beyond.size)],
            dtype=fit.dtype,
        )
        if not np.array_equal(beyond, expected):
            first = expected[0]
            gap = "a gap" if beyond[0] != first else "gaps"
            raise CoordPlanError(
                f"score coords of {dim!r} extend the fitted range with {gap}; they must start "
                f"right after it and be contiguous",
                code=ErrorCode.NON_CONTIGUOUS_EXTENSION,
                untrusted_text=f"expected {py_scalar(first)}, got {py_scalar(beyond[0])}",
            )
        if max_extension is not None and beyond.size > max_extension:
            raise CoordPlanError(
                f"score coords extend {beyond.size} period(s) past the fit along {dim!r}; at most "
                f"{max_extension} are supported without a refit",
                code=ErrorCode.HORIZON_EXCEEDS_FIT,
            )
    if pad_extend:
        return np.union1d(fit, cur)
    return np.sort(cur)


def _remap_index_vars(
    out: xr.Dataset,
    ds: xr.Dataset,
    dim: str,
    cur: np.ndarray,
    order: np.ndarray,
    schema: DataSchema,
) -> xr.Dataset:
    """Long layout: index variables hold positions into ``dim``; re-point them at ``order``.

    ``county_idx(obs)`` indexes the *payload's* ``county`` labels. After reindexing the
    labels to the fit order (plus appended new levels) the positions must follow, otherwise
    every observation would silently point at the wrong level. Out-of-range positions are
    left untouched (array-level validation reports them).
    """
    names = [n for n, v in schema.vars.items() if v.index_of == dim and n in out.data_vars]
    if not names or not any(n in ds.data_vars for n in names):
        return out
    mapping = pd.Index(order).get_indexer(cur)
    updates: dict[str, xr.DataArray] = {}
    for name in names:
        da = out[name]
        if da.dtype.kind not in "iu":
            continue
        idx = np.asarray(da.values)
        valid = (idx >= 0) & (idx < cur.size)
        moved = np.where(valid, mapping[np.clip(idx, 0, max(cur.size - 1, 0))], idx)
        updates[name] = da.copy(data=moved.astype(idx.dtype))
    return out.assign(updates)


def _reindex(
    ds: xr.Dataset, dim: str, order: np.ndarray, targets: set[str], pad_fill: float
) -> xr.Dataset:
    present = np.isin(order, np.asarray(ds[dim].values))
    out = ds.reindex({dim: order})
    if present.all():
        return out
    fills: dict[Any, xr.DataArray] = {}
    pad = xr.DataArray(~present, dims=[dim], coords={dim: order})
    for v in out.data_vars:
        if dim not in out[v].dims or str(v) in targets:
            continue
        orig = ds[v]
        if orig.dtype.kind in "fiu":
            filled = out[v].where(~pad, pad_fill)
            fills[v] = filled.astype(orig.dtype) if orig.dtype.kind in "iu" else filled
        elif orig.dtype.kind == "b":
            fills[v] = out[v].where(~pad, False).astype(bool)
    return out.assign(fills) if fills else out


# ========================================================================================= horizon


def horizon_coords(
    fit: np.ndarray | Sequence[Any], periods: int, *, freq: str | None = None
) -> np.ndarray:
    """``periods`` labels continuing ``fit`` (dates by ``freq``, integers by their step)."""
    values = np.asarray(fit)
    if values.size == 0:
        raise CoordPlanError("cannot extend an empty coordinate", code=ErrorCode.COORD_NOT_ALLOWED)
    last = values.max()
    if values.dtype.kind == "M":
        f = freq or infer_freq(values)
        return np.array(
            [next_label(last, freq=f, k=k) for k in range(1, periods + 1)], dtype="datetime64[ns]"
        )
    step = _step_of(values)
    return np.array([last + k * step for k in range(1, periods + 1)], dtype=values.dtype)


def horizon_frame(
    fit_coords: xr.Dataset,
    dim: str,
    *,
    periods: int | None = None,
    until: Any = None,
    freq: str | None = None,
    drop: Collection[str] = (),
) -> xr.Dataset:
    """A coordinates-only frame covering the future along ``dim``.

    Every other labelled dim keeps its fit labels. Give either ``periods`` or ``until``
    (the last label to include, e.g. computed at a coarser model stage).
    """
    if dim not in fit_coords.coords:
        raise CoordPlanError(
            f"{dim!r} has no fitted coordinates to extend", code=ErrorCode.COORD_NOT_ALLOWED
        )
    fit = np.asarray(fit_coords[dim].values)
    f = freq or (infer_freq(fit) if fit.dtype.kind == "M" else None)
    if periods is not None:
        labels = horizon_coords(fit, periods, freq=f)
    elif until is not None:
        labels_list: list[Any] = []
        k = 1
        stop = np.datetime64(pd.Timestamp(until), "ns") if fit.dtype.kind == "M" else until
        while True:
            nxt = next_label(fit.max(), freq=f, step=_step_of(fit), k=k)
            if nxt > stop or k > 1_000_000:
                break
            labels_list.append(nxt)
            k += 1
        labels = np.array(labels_list, dtype=fit.dtype)
    else:
        raise ValueError("give periods or until")
    coords: dict[str, Any] = {
        str(c): np.asarray(fit_coords[c].values)
        for c in fit_coords.coords
        if c in fit_coords.dims and c != dim and c not in drop
    }
    coords[dim] = labels
    return xr.Dataset(coords=coords)


def payload_issues(issues: Sequence[Issue]) -> list[Issue]:
    """Non-error issues worth reporting back with a score result."""
    return [i for i in issues if i.severity is not Severity.ERROR]


def required_future_vars(schema: DataSchema | None) -> list[str]:
    """Variables that must be supplied over a forecast horizon (excluding targets)."""
    if schema is None:
        return []
    return [n for n, v in schema.vars.items() if v.future_required and v.role is not Role.TARGET]
