"""Train/holdout splits applied at the split stage (plan §4.2).

Ordered splits (``time_holdout``, ``as_of``) and group splits (``leave_group_out``, and
``last_per_group`` on long data) cut the data along a dim, so train and holdout have
disjoint coordinates. Cell-level splits (``mask_block``, and ``last_per_group`` on dense
data) cannot be expressed by slicing: train keeps every coordinate with the *target*
masked (NaN) in held-out cells, the holdout is the full input data, and
``SplitResult.holdout_mask`` marks the cells that metrics are computed on. Models used
with these splits must accept missing target values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.spec import (
    AsOfSplit,
    LastPerGroupSplit,
    LeaveGroupOutSplit,
    MaskBlockSplit,
    Split,
    TimeHoldoutSplit,
)

__all__ = ["SplitResult", "split_dataset"]


@dataclass(slots=True)
class SplitResult:
    """Train and holdout data at the split stage."""

    train: xr.Dataset
    holdout: xr.Dataset
    holdout_mask: xr.DataArray | None = None
    kind: str = ""
    lead: int = 0
    """Leading holdout labels along the split dim that are only scored for continuity
    (an ``as_of`` ``gap``): their targets are masked and metrics never count them."""

    @property
    def is_masked(self) -> bool:
        return self.holdout_mask is not None


def _err(message: str) -> MapiError:
    return MapiError(message, code=ErrorCode.SPLIT_INVALID, pointer="/data/split")


def _require_dim(ds: xr.Dataset, dim: str) -> int:
    if dim not in ds.dims:
        raise _err(f"split dim {dim!r} does not exist at the split stage")
    return int(ds.sizes[dim])


def _coerce(value: Any, coords: np.ndarray) -> Any:
    if coords.dtype.kind == "M":
        return np.datetime64(pd.Timestamp(value).to_datetime64(), "ns")
    return value


def _cut(ds: xr.Dataset, dim: str, train_idx: np.ndarray, hold_idx: np.ndarray) -> SplitResult:
    if train_idx.size == 0:
        raise _err("the split leaves no training data")
    if hold_idx.size == 0:
        raise _err("the split leaves no holdout data")
    return SplitResult(ds.isel({dim: train_idx}), ds.isel({dim: hold_idx}))


def _mask_lead(ds: xr.Dataset, dim: str, k: int, schema: DataSchema | None) -> xr.Dataset:
    """Mask the target in the first ``k`` labels of ``dim``."""
    lead = xr.DataArray(np.arange(ds.sizes[dim]) < k, dims=[dim])
    out = ds.copy()
    for t in schema.targets if schema is not None else []:
        if t in out and dim in out[t].dims:
            out[t] = out[t].where(~lead)
    return out


def _targets(schema: DataSchema | None, ds: xr.Dataset) -> list[str]:
    names = [t for t in (schema.targets if schema is not None else []) if t in ds]
    if not names:
        raise _err("a cell-level split needs a target-role variable to mask")
    return names


def _masked(ds: xr.Dataset, mask: xr.DataArray, targets: list[str], kind: str) -> SplitResult:
    if not bool(mask.any()):
        raise _err("the split leaves no holdout data")
    train = ds.copy()
    for t in targets:
        m = mask.broadcast_like(ds[t]).transpose(*ds[t].dims)
        if bool(m.all()):
            raise _err("the split leaves no training data")
        train[t] = ds[t].where(~m)
    return SplitResult(train, ds, mask, kind)


def split_dataset(
    ds: xr.Dataset, split: Split, *, schema: DataSchema | None = None, seed: int = 0
) -> SplitResult:
    """Split ``ds`` (the data at the split stage) according to ``split``."""
    result: SplitResult
    match split:
        case TimeHoldoutSplit():
            n = _require_dim(ds, split.dim)
            coords = np.asarray(ds[split.dim].values) if split.dim in ds.coords else np.arange(n)
            if split.last is not None:
                k = split.last
                cut = n - k
            elif split.fraction is not None:
                cut = n - max(1, math.ceil(n * split.fraction))
            else:
                cut = int(np.sum(coords <= _coerce(split.after, coords)))
            idx = np.arange(n)
            result = _cut(ds, split.dim, idx[: max(cut, 0)], idx[max(cut, 0) :])
        case AsOfSplit():
            n = _require_dim(ds, split.dim)
            coords = np.asarray(ds[split.dim].values) if split.dim in ds.coords else np.arange(n)
            cut = int(np.sum(coords <= _coerce(split.as_of, coords)))
            idx = np.arange(n)
            if idx[cut + split.gap :].size == 0:
                raise _err("the split leaves no holdout data")
            # the gap periods stay in the holdout so scoring continues the fitted range
            # without a hole; their targets are masked and metrics skip them (``lead``)
            result = _cut(ds, split.dim, idx[:cut], idx[cut:])
            if split.gap:
                result.holdout = _mask_lead(result.holdout, split.dim, split.gap, schema)
                result.lead = split.gap
        case LeaveGroupOutSplit():
            n = _require_dim(ds, split.dim)
            levels = np.asarray(ds[split.dim].values) if split.dim in ds.coords else np.arange(n)
            if split.groups is not None:
                chosen = np.asarray(split.groups)
                unknown = chosen[~np.isin(chosen, levels)]
                if unknown.size:
                    raise _err(f"{unknown.size} held-out group(s) do not exist in the data")
            else:
                assert split.n is not None
                if split.n >= n:
                    raise _err("leave_group_out would hold out every group")
                rng = np.random.default_rng(split.seed if split.seed is not None else seed)
                chosen = levels[np.sort(rng.choice(n, size=split.n, replace=False))]
            hold = np.isin(levels, chosen)
            result = _cut(ds, split.dim, np.flatnonzero(~hold), np.flatnonzero(hold))
        case LastPerGroupSplit():
            result = _last_per_group(ds, split, schema)
        case MaskBlockSplit():
            result = _mask_block(ds, split, schema, seed)
        case _:  # pragma: no cover - exhaustive over the Split union
            raise _err(f"unsupported split kind {getattr(split, 'kind', '?')!r}")
    result.kind = split.kind
    return result


def _last_per_group(
    ds: xr.Dataset, split: LastPerGroupSplit, schema: DataSchema | None
) -> SplitResult:
    dim = split.dim
    _require_dim(ds, dim)
    if all(w in ds.dims for w in split.within):
        targets = _targets(schema, ds)
        valid = ds[targets[0]].notnull()
        for t in targets[1:]:
            valid = valid | ds[t].notnull()
        other = [d for d in valid.dims if d != dim and d not in split.within]
        if other:
            valid = valid.any(other)
        valid = valid.transpose(*split.within, dim)
        v = np.asarray(valid.values)
        # rank of each valid cell counted from the end of its group
        from_end = np.flip(np.cumsum(np.flip(v, axis=-1), axis=-1), axis=-1)
        n_valid = v.sum(axis=-1, keepdims=True)
        eligible = n_valid >= split.min_len + split.last
        hold = v & (from_end <= split.last) & eligible
        mask = xr.DataArray(hold, dims=valid.dims, coords=valid.coords)
        return _masked(ds, mask, targets, split.kind)
    for w in split.within:
        if w not in ds.variables or ds[w].dims != (dim,):
            raise _err(f"group {w!r} must be a dim or a variable along {dim!r}")
    frame = pd.DataFrame({w: np.asarray(ds[w].values) for w in split.within})
    frame["__pos__"] = np.arange(len(frame))
    hold_rows: list[int] = []
    for _, grp in frame.groupby(list(split.within), sort=False):
        if len(grp) >= split.min_len + split.last:
            hold_rows.extend(grp["__pos__"].to_numpy()[-split.last :].tolist())
    hold = np.zeros(len(frame), dtype=bool)
    hold[hold_rows] = True
    return _cut(ds, dim, np.flatnonzero(~hold), np.flatnonzero(hold))


def _mask_block(
    ds: xr.Dataset, split: MaskBlockSplit, schema: DataSchema | None, seed: int
) -> SplitResult:
    targets = _targets(schema, ds)
    sizes = [_require_dim(ds, d) for d in split.dims]
    blocks = [split.block.get(d, 1) for d in split.dims]
    grid = [math.ceil(n / b) for n, b in zip(sizes, blocks, strict=True)]
    total = math.prod(grid)
    k = max(1, round(total * split.fraction))
    if k >= total:
        raise _err("mask_block would hold out every block")
    rng = np.random.default_rng(split.seed if split.seed is not None else seed)
    picked = rng.choice(total, size=k, replace=False)
    block_mask = np.zeros(total, dtype=bool)
    block_mask[picked] = True
    block_mask = block_mask.reshape(grid)
    for axis, b in enumerate(blocks):
        block_mask = np.repeat(block_mask, b, axis=axis)
    cell_mask = block_mask[tuple(slice(0, n) for n in sizes)]
    coords = {d: ds[d] for d in split.dims if d in ds.coords}
    mask = xr.DataArray(cell_mask, dims=list(split.dims), coords=coords)
    return _masked(ds, mask, targets, split.kind)
