"""Extend a dataset version with new periods (v0.16, a dashboard refit request).

An approved refit request ingests the client's uploaded actuals as a new version of the
dataset the model was fitted on. The new version must be that dataset *plus* the uploaded
periods, not the uploaded periods alone (an MMM refit on eight weeks is not a refit).

:func:`extend_dataset` combines them along the time dim: the base keeps every period the
upload does not have, an uploaded period replaces the base's, and variables the upload does
not carry are missing (NaN) for its periods. The result keeps the base's schema and is
validated with it, so the variables' own missing policies apply (a model whose spend may
not be missing needs the spend in the upload).

The base is the earlier version's ``data.zarr.zip`` (written by an ingest run, read with the
wire-format checks); nothing here unpickles anything.
"""

from __future__ import annotations

import warnings

import numpy as np
import xarray as xr
from mapi_spec.errors import ErrorCode

from mapi_runner.worker.tabular import IngestLimits, problem

__all__ = ["extend_dataset"]

_POINTER = "/extend_dim"


def _unknown(labels: xr.DataArray, known: xr.DataArray) -> list[str]:
    """The labels of ``labels`` that ``known`` lacks (as text, for the message)."""
    missing = ~labels.isin(known.values)
    return sorted(str(x) for x in np.asarray(labels.values[missing.values]).tolist())


def _collapse_broadcast(new: xr.Dataset, base: xr.Dataset) -> xr.Dataset:
    """Undo the long table's broadcast: an upload arrives as one CSV row per label
    combination, so a variable of fewer dims (``price(week)`` beside ``spend(channel,
    week)``) comes back with every dim. A numeric variable that is the same along the dims
    the dataset's variable lacks (missing cells aside) is reduced to the dataset's dims; any
    other is left for the dims check to refuse."""
    out = new
    for name, var in new.data_vars.items():
        want = set(base[name].dims)
        extra = [d for d in var.dims if d not in want]
        if not extra or not want <= set(var.dims) or not np.issubdtype(var.dtype, np.number):
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-missing slices
            hi = var.max(extra, skipna=True)
            lo = var.min(extra, skipna=True)
        if bool(((hi == lo) | hi.isnull()).all()):
            out = out.assign({name: hi.drop_vars(extra, errors="ignore")})
    return out


def extend_dataset(base: xr.Dataset, new: xr.Dataset, dim: str, limits: IngestLimits) -> xr.Dataset:
    """``base`` with the labels of ``new`` along ``dim`` added (or replaced), sorted along
    ``dim``. ``new`` may carry fewer variables (the rest are NaN for its labels) and fewer
    labels of the other dims (NaN too), never variables, dims or labels ``base`` lacks."""
    if dim not in base.dims:
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            f"the dataset has no {dim!r} dimension to add periods to",
            pointer=_POINTER,
        )
    if dim not in new.dims:
        raise problem(
            ErrorCode.SCHEMA_INVALID, f"the upload has no {dim!r} dimension", pointer=_POINTER
        )
    unknown = sorted(str(v) for v in new.data_vars if v not in base.data_vars)
    if unknown:
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            f"the upload has variables the dataset does not have: {', '.join(unknown[:5])}",
            pointer=_POINTER,
            action="Send only the variables of the template.",
        )
    for d in new.dims:
        if d == dim:
            continue
        if d not in base.dims:
            raise problem(
                ErrorCode.SCHEMA_INVALID,
                f"the upload has a dimension the dataset does not have: {d!r}",
                pointer=_POINTER,
            )
        extra = _unknown(new[d], base[d])
        if extra:
            shown = ", ".join(extra[:5])
            raise problem(
                ErrorCode.SCHEMA_INVALID,
                f"the upload has labels of {d!r} the dataset does not have: {shown}",
                pointer=_POINTER,
                action="Use the labels of the template.",
            )
    new = _collapse_broadcast(new, base)
    for name, var in new.data_vars.items():
        if set(var.dims) != set(base[name].dims):
            raise problem(
                ErrorCode.SCHEMA_INVALID,
                f"variable {name!r} has dims {list(var.dims)} in the upload but "
                f"{list(base[name].dims)} in the dataset",
                pointer=_POINTER,
            )
    try:
        periods = new[dim].astype(base[dim].dtype)
    except (TypeError, ValueError):
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            f"the upload's {dim!r} labels do not match the dataset's",
            pointer=_POINTER,
        ) from None
    new = new.assign_coords({dim: periods})
    others = {d: base[d] for d in base.dims if d != dim}
    new = new.reindex({d: c for d, c in others.items() if d in new.dims})
    keep = base.isel({dim: ~base[dim].isin(new[dim].values).values})
    sizes = {**{d: base.sizes[d] for d in others}, dim: new.sizes[dim]}
    block = xr.Dataset(coords={dim: new[dim], **others})
    for name, var in base.data_vars.items():
        if dim not in var.dims:
            continue
        if name in new.data_vars:
            block[name] = new[name].transpose(*var.dims)
            continue
        dtype = var.dtype if np.issubdtype(var.dtype, np.floating) else np.float64
        block[name] = xr.DataArray(
            np.full([sizes[d] for d in var.dims], np.nan, dtype=dtype), dims=var.dims
        )
    timed = [n for n, v in base.data_vars.items() if dim in v.dims]
    combined = xr.concat(
        [keep[timed].reset_coords(drop=True), block[timed]],
        dim=dim,
        data_vars="all",
        coords="minimal",
        compat="override",
        join="outer",
    ).sortby(dim)
    for name, var in base.data_vars.items():
        if dim not in var.dims:
            combined[name] = var
    combined = combined[list(base.data_vars)]
    combined.attrs = dict(base.attrs)
    for name in combined.data_vars:
        combined[name].attrs = dict(base[name].attrs)
    if int(combined.nbytes) > limits.max_uncompressed_bytes:
        raise problem(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"the extended dataset decodes to more than {limits.max_uncompressed_bytes:,} bytes",
            pointer=_POINTER,
        )
    return combined
