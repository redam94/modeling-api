"""Raw HDF5 uploads (format ``hdf5``, M4): mapped arrays become the nodes of a tree dataset.

A raw HDF5 file has no dim names or coordinates, so an
:class:`~mapi_spec.ingest.Hdf5Mapping` says which arrays become which variables of which
node (``dims`` names each axis) and where each dim's coordinate comes from: a 1-D array of
the file (``path``), a range (``start`` + ``step``: numbers, or a date and a pandas frequency)
or an integer index. A dim without any :class:`~mapi_spec.ingest.Hdf5Coord` stays without a
coordinate (an ``index`` dim).

Only the mapped arrays are read, after :func:`~mapi_runner.worker.hdf5_guard.walk_hdf5`
accepted the file; the others are listed in one warning.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import Hdf5Coord, Hdf5Mapping, Hdf5Var
from mapi_spec.schema import DimSpec, VarSpec
from mapi_spec.tree import is_ancestor, node_ancestors

from mapi_runner.worker.hdf5_guard import Hdf5Walk
from mapi_runner.worker.tabular import IngestLimits, problem

__all__ = ["MAX_UNMAPPED_NAMES", "Hdf5Read", "apply_var_meta", "read_hdf5_mapped"]

MAX_UNMAPPED_NAMES = 20
"""Unmapped array paths named in the warning (the rest are counted)."""


@dataclass(slots=True)
class Hdf5Read:
    """The mapped arrays as node datasets plus the schema facts the mapping declares."""

    nodes: dict[str, xr.Dataset]
    var_specs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    """``(node, name)`` -> role/units/missing/description of the variable."""
    dim_specs: dict[tuple[str, str], DimSpec] = field(default_factory=dict)
    """``(node, dim)`` -> declared DimSpec (applies to that node and the nodes below it)."""
    warnings: list[tuple[str, str | None]] = field(default_factory=list)


def _bad(
    message: str, pointer: str, *, action: str | None = None, untrusted: str | None = None
) -> MapiError:
    return problem(
        ErrorCode.SCHEMA_INVALID, message, pointer=pointer, action=action, untrusted=untrusted
    )


def _open_dataset(f: Any, path: str, pointer: str) -> Any:
    import h5py

    obj = f.get(path)
    if obj is None:
        raise _bad(
            "the mapped array is not in the file",
            pointer,
            action="Inspect the upload and use one of the array paths it lists.",
            untrusted=path,
        )
    if not isinstance(obj, h5py.Dataset):
        raise _bad("the mapped path is a group, not an array", pointer, untrusted=path)
    return obj


def _values(obj: Any, limits: IngestLimits, pointer: str) -> np.ndarray:
    import h5py

    if h5py.check_string_dtype(obj.dtype) is not None:
        raw = np.asarray(obj.asstr(errors="replace")[()], dtype=object)
        longest = max((len(s) for s in raw.reshape(-1)), default=0)
        if longest > limits.max_cell_chars:
            raise problem(
                ErrorCode.PAYLOAD_TOO_LARGE,
                f"an array holds text of {longest:,} characters (the maximum is "
                f"{limits.max_cell_chars:,})",
                pointer=pointer,
            )
        return raw.astype(str) if raw.size else np.array([], dtype=str).reshape(raw.shape)
    values = np.asarray(obj[()])
    if values.dtype.kind not in "biuf":
        raise _bad(
            f"the mapped array has data type {str(values.dtype)[:40]!r}, which is not numbers, "
            "booleans or text",
            pointer,
        )
    return values


def _addr(obj: Any) -> int:
    from h5py import h5o

    return int(h5o.get_info(obj.id).addr)


def _range(coord: Hdf5Coord, n: int, pointer: str) -> np.ndarray:
    start, step = coord.start, coord.step
    if isinstance(start, str):
        if not isinstance(step, str):
            raise _bad(
                "a date range needs a frequency as its step (such as 'D', 'W-MON' or 'MS')",
                pointer,
            )
        try:
            dates = pd.date_range(start=start, periods=n, freq=step)
            return np.asarray(dates, dtype="datetime64[ns]")
        except (ValueError, TypeError, OverflowError) as exc:
            raise _bad(
                "the date range cannot be built from this start and frequency",
                pointer,
                action="Use an ISO date such as 2024-01-01 and a pandas frequency such as 'W-MON'.",
                untrusted=f"{start!r} {step!r}: {exc}"[:2000],
            ) from None
    step = 1 if step is None else step
    assert start is not None and not isinstance(step, str)
    if isinstance(start, int) and isinstance(step, int):
        return start + step * np.arange(n, dtype="int64")
    return float(start) + float(step) * np.arange(n, dtype="float64")


def _coord_for(mapping: Hdf5Mapping, node: str, dim: str) -> tuple[int, Hdf5Coord] | None:
    """The coord declared for ``dim`` nearest to ``node`` (itself, else an ancestor)."""
    for where in [node, *node_ancestors(node)]:
        for i, c in enumerate(mapping.coords):
            if c.dim == dim and c.node == where:
                return i, c
    return None


def read_hdf5_mapped(
    path: Path, mapping: Hdf5Mapping, limits: IngestLimits, walk: Hdf5Walk
) -> Hdf5Read:
    """Read the arrays ``mapping`` names (``walk`` is the guard's walk of ``path``)."""
    import h5py

    budget = limits.expansion_budget(path.stat().st_size)
    used = 0
    out = Hdf5Read(nodes={})
    read_addrs: set[int] = set()
    # node -> name -> (dims, values)
    per_node: dict[str, dict[str, tuple[list[str], np.ndarray]]] = {}
    sizes: dict[str, dict[str, tuple[int, str]]] = {}
    with h5py.File(path, "r", locking=False) as f:
        for i, var in enumerate(mapping.vars):
            pointer = f"/mapping/hdf5/vars/{i}"
            obj = _open_dataset(f, var.path, f"{pointer}/path")
            if len(var.dims) != obj.ndim:
                raise _bad(
                    f"variable {var.name!r} names {len(var.dims)} dims, but its array has "
                    f"{obj.ndim} (shape {tuple(obj.shape)})",
                    f"{pointer}/dims",
                    action="Name one dim for each axis of the array, in order.",
                )
            used += int(obj.size) * max(int(obj.dtype.itemsize), 8)
            if used > budget:
                raise problem(
                    ErrorCode.PAYLOAD_TOO_LARGE,
                    f"the mapped arrays hold more than {budget:,} bytes",
                    pointer=pointer,
                    action="Map fewer arrays, or split the file.",
                )
            values = _values(obj, limits, f"{pointer}/path")
            read_addrs.add(_addr(obj))
            per_node.setdefault(var.node, {})[var.name] = (list(var.dims), values)
            for dim, n in zip(var.dims, values.shape, strict=True):
                have = sizes.setdefault(var.node, {}).get(dim)
                if have is not None and have[0] != n:
                    raise _bad(
                        f"dim {dim!r} of node {var.node!r} has {have[0]} labels in "
                        f"{have[1]!r} but {n} in {var.name!r}",
                        f"{pointer}/dims",
                        action="Map arrays of the same length to the same dim, or give the "
                        "dims different names.",
                    )
                sizes[var.node][dim] = (n, var.name)
            out.var_specs[(var.node, var.name)] = _var_meta(var)
        # a dim shared with an ancestor node must have the ancestor's length
        for node, dims in sizes.items():
            for anc in node_ancestors(node):
                for dim, (n, name) in dims.items():
                    other = sizes.get(anc, {}).get(dim)
                    if other is not None and other[0] != n:
                        raise _bad(
                            f"dim {dim!r} has {n} labels in node {node!r} ({name!r}) but "
                            f"{other[0]} in its ancestor {anc!r} ({other[1]!r})",
                            "/mapping/hdf5/vars",
                            action="A node sees the dims of its ancestors: give the dims "
                            "different names, or map arrays of the same length.",
                        )
        coords: dict[str, dict[str, np.ndarray]] = {}
        for node, dims in sizes.items():
            for dim, (n, _name) in dims.items():
                found = _coord_for(mapping, node, dim)
                if found is None:
                    continue
                i, coord = found
                pointer = f"/mapping/hdf5/coords/{i}"
                # users at or below the coord's node must agree on the length
                for other_node, other_dims in sizes.items():
                    if other_node != coord.node and not is_ancestor(coord.node, other_node):
                        continue
                    other = other_dims.get(dim)
                    if other is not None and other[0] != n:
                        raise _bad(
                            f"dim {dim!r} has {n} labels in node {node!r} but {other[0]} in "
                            f"node {other_node!r}, which share the coord of {coord.node!r}",
                            pointer,
                        )
                if dim in coords.get(coord.node, {}):
                    continue
                coords.setdefault(coord.node, {})[dim] = _coord_values(
                    f, coord, n, limits, pointer, read_addrs
                )
                if coord.spec is not None:
                    out.dim_specs[(coord.node, dim)] = coord.spec
                elif coord.path is None and coord.start is None:
                    out.dim_specs[(coord.node, dim)] = DimSpec(kind="index")
    for node in {*per_node, *coords}:
        data_vars = {n: (dims, values) for n, (dims, values) in per_node.get(node, {}).items()}
        try:
            out.nodes[node] = xr.Dataset(data_vars, coords=coords.get(node, {}))
        except ValueError as exc:
            raise _bad(
                f"node {node!r} cannot be built from the mapped arrays",
                "/mapping/hdf5",
                untrusted=str(exc)[:2000],
            ) from None
    unmapped = [a.path for a in walk.arrays if a.addr not in read_addrs]
    if unmapped:
        shown = unmapped[:MAX_UNMAPPED_NAMES]
        more = len(unmapped) - len(shown)
        out.warnings.append(
            (
                f"{len(unmapped)} array(s) of the HDF5 file are not mapped and were not read"
                + (f" ({more} not listed)" if more else ""),
                ", ".join(shown),
            )
        )
    return out


def _coord_values(
    f: Any,
    coord: Hdf5Coord,
    n: int,
    limits: IngestLimits,
    pointer: str,
    read_addrs: set[int],
) -> np.ndarray:
    if coord.path is not None:
        obj = _open_dataset(f, coord.path, f"{pointer}/path")
        if obj.ndim != 1 or obj.shape[0] != n:
            raise _bad(
                f"the coord of dim {coord.dim!r} has shape {tuple(obj.shape)}, but the dim has "
                f"{n} labels",
                f"{pointer}/path",
                action="Use a 1-D array with one label per position along the dim.",
            )
        values = _values(obj, limits, f"{pointer}/path")
        read_addrs.add(_addr(obj))
        if coord.spec is not None and coord.spec.kind == "time":
            try:
                return np.asarray(pd.to_datetime(values)).astype("datetime64[ns]")
            except (ValueError, TypeError, OverflowError) as exc:
                raise _bad(
                    f"the labels of dim {coord.dim!r} cannot be read as dates",
                    f"{pointer}/path",
                    untrusted=str(exc)[:2000],
                ) from None
        return values
    if coord.start is None:
        return np.arange(n, dtype="int64")
    return _range(coord, n, pointer)


def _var_meta(var: Hdf5Var) -> dict[str, Any]:
    meta: dict[str, Any] = {"role": var.role, "missing": var.missing}
    if var.units:
        meta["units"] = var.units
    if var.description:
        meta["description"] = var.description
    return meta


def apply_var_meta(spec: VarSpec, meta: Mapping[str, Any]) -> VarSpec:
    """``spec`` with the role/units/missing/description a mapping declares."""
    return spec.model_copy(update=dict(meta))
