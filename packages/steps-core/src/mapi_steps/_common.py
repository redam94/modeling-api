"""Shared helpers for the built-in steps: names, errors, group-local array ops, reductions.

Group-local operations (lags, forward fill, carry-over) run along one dim ``along`` within
groups ``within``. Two layouts are supported, matching :mod:`mapi_sdk.context`:

* **dense** - every ``within`` entry is a dim (or ``within`` is empty): the operation runs
  along the ``along`` axis of the array and groups are implicit;
* **long** - ``along`` is a row dim and ``within`` names 1-D variables or coords along it:
  rows are scattered into a padded ``(group, position)`` grid in row order, the operation
  runs along ``position`` and the result is gathered back.

Operations receive a float array with ``along`` as the **last** axis.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Annotated

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.steps import StepContext, UnseenPolicy
from mapi_spec.errors import CoordPlanError, ErrorCode, MapiError
from pydantic import AfterValidator, Field, StringConstraints

__all__ = [
    "NAME_RE",
    "Name",
    "NameList",
    "apply_along",
    "assign_vars",
    "first_valid",
    "float_values",
    "group_codes",
    "is_dense",
    "level_param",
    "present",
    "require_dim",
    "require_vars",
    "schema_error",
    "shift_last",
    "state_array",
]

NAME_RE = r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$"
"""Dim and variable names (same rule as the spec's names)."""


def _unique(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        dups = sorted({v for v in values if values.count(v) > 1})
        raise ValueError(f"duplicate names: {dups}")
    return values


Name = Annotated[str, StringConstraints(pattern=NAME_RE)]
NameList = Annotated[
    list[Name],
    Field(max_length=256, json_schema_extra={"uniqueItems": True}),
    AfterValidator(_unique),
]


# ----------------------------------------------------------------------------------- errors


def schema_error(step_id: str, message: str, *, untrusted_text: str | None = None) -> MapiError:
    """A ``SCHEMA_INVALID`` error attributed to a step."""
    return MapiError(
        f"step {step_id!r}: {message}", code=ErrorCode.SCHEMA_INVALID, untrusted_text=untrusted_text
    )


def require_vars(ds: xr.Dataset, names: Iterable[str], step_id: str) -> None:
    """Raise ``SCHEMA_INVALID`` unless every name is a data variable of ``ds``."""
    missing = [n for n in names if n not in ds.data_vars]
    if missing:
        raise schema_error(step_id, f"variables {missing} are missing from the data")


def require_dim(ds: xr.Dataset | xr.DataArray, dim: str, step_id: str) -> None:
    if dim not in ds.dims:
        raise schema_error(step_id, f"dim {dim!r} does not exist in the data")


def present(ds: xr.Dataset, names: Iterable[str]) -> list[str]:
    """The names that are data variables of ``ds`` (absent targets are tolerated)."""
    return [n for n in names if n in ds.data_vars]


def float_values(da: xr.DataArray) -> np.ndarray:
    """Values as float64 (booleans and integers converted; NaN stays NaN)."""
    if da.dtype.kind not in "fiub":
        raise TypeError(f"variable {da.name!r} is not numeric (dtype {da.dtype})")
    return np.asarray(da.values, dtype=np.float64)


# ------------------------------------------------------------------------- group-local ops


def is_dense(ds: xr.Dataset, along: str, within: Sequence[str]) -> bool:
    """True when every ``within`` entry is a dim of ``ds`` (dense layout)."""
    return all(w in ds.dims for w in within)


def group_codes(ds: xr.Dataset, along: str, within: Sequence[str]) -> tuple[np.ndarray, int]:
    """Long layout: integer group code per row and the number of groups."""
    for w in within:
        if w not in ds.variables or ds[w].dims != (along,):
            raise CoordPlanError(
                f"group {w!r} is neither a dim nor a variable along {along!r}",
                code=ErrorCode.COORD_NOT_ALLOWED,
            )
    if not within:
        return np.zeros(ds.sizes[along], dtype=np.int64), 1
    keys = pd.MultiIndex.from_arrays([np.asarray(ds[w].values) for w in within])
    codes, uniques = pd.factorize(keys, use_na_sentinel=False)
    return np.asarray(codes, dtype=np.int64), len(uniques)


def apply_along(
    da: xr.DataArray,
    ds: xr.Dataset,
    along: str,
    within: Sequence[str],
    fn: Callable[[np.ndarray], np.ndarray],
) -> xr.DataArray:
    """Apply ``fn`` along ``along`` within groups (see the module docstring)."""
    if along not in da.dims:
        raise CoordPlanError(
            f"variable {da.name!r} does not use the dim {along!r}", code=ErrorCode.COORD_NOT_ALLOWED
        )
    other = [d for d in da.dims if d != along]
    arr = float_values(da.transpose(*other, along))
    if is_dense(ds, along, within):
        out = fn(arr)
    else:
        codes, n_groups = group_codes(ds, along, within)
        pos = pd.Series(codes).groupby(codes).cumcount().to_numpy()
        width = int(pos.max()) + 1 if pos.size else 0
        grid = np.full((*arr.shape[:-1], n_groups, width), np.nan)
        grid[..., codes, pos] = arr
        out = fn(grid)[..., codes, pos]
    result = xr.DataArray(out, dims=(*other, along), coords=da.coords, attrs=da.attrs)
    return result.transpose(*da.dims)


def shift_last(a: np.ndarray, k: int, fill: float = np.nan) -> np.ndarray:
    """Shift ``a`` by ``k`` positions along its last axis (``k > 0`` looks back)."""
    if k == 0:
        return a.copy()
    out = np.full(a.shape, fill, dtype=np.float64)
    n = a.shape[-1]
    if abs(k) >= n:
        return out
    if k > 0:
        out[..., k:] = a[..., :-k]
    else:
        out[..., :k] = a[..., -k:]
    return out


def first_valid(a: np.ndarray) -> np.ndarray:
    """Index of the first non-NaN value along the last axis (``n`` when there is none)."""
    valid = ~np.isnan(a)
    n = a.shape[-1]
    return np.where(valid.any(axis=-1), np.argmax(valid, axis=-1), n)


# ---------------------------------------------------------------------- reductions & state


def assign_vars(ds: xr.Dataset, new: Mapping[str, xr.DataArray]) -> xr.Dataset:
    """``ds.assign(new)`` that is safe on stacked (MultiIndex) dims.

    Coordinates a result shares with ``ds`` (equal indexes, MultiIndex levels included) are
    dropped from the result first, so ``ds`` provides them and xarray never deletes a single
    MultiIndex level. Coordinates of new dims (``lag``, ``fourier_mode`` ...) are kept.
    """
    clean: dict[str, xr.DataArray] = {}
    for name, da in new.items():
        drop: set[str] = set()
        for d in map(str, da.dims):
            if d in da.indexes and d in ds.indexes and da.indexes[d].equals(ds.indexes[d]):
                drop |= {str(c) for c in da.xindexes.get_all_coords(d)}
        drop |= {str(c) for c in da.coords if c not in da.xindexes and c in ds.coords}
        clean[name] = da.drop_vars(sorted(drop)) if drop else da
    return ds.assign(clean)


def state_array(da: xr.DataArray) -> xr.DataArray:
    """A learned array ready to store: no attrs, only dim/index coordinates (MultiIndex
    levels of stacked dims are kept with their index)."""
    out = da.drop_vars([c for c in da.coords if c not in da.dims and c not in da.xindexes])
    out.attrs = {}
    return out


def align(
    ctx: StepContext,
    state: xr.DataArray,
    ds: xr.Dataset | xr.DataArray,
    policy: UnseenPolicy,
    fill_value: float,
) -> xr.DataArray:
    """Reindex a labelled state array to ``ds`` (``error``/``pooled``/``fill`` for new levels)."""
    return ctx.align_state(state, ds, policy, fill_value=fill_value)


def level_param(
    value: float | Mapping[str, float],
    by: str | None,
    like: xr.DataArray,
    *,
    name: str,
    step_id: str,
) -> xr.DataArray | float:
    """A scalar parameter, or a per-level parameter keyed by the levels of dim ``by``."""
    if not isinstance(value, Mapping):
        return float(value)
    if by is None:
        raise MapiError(
            f"step {step_id!r}: {name} is given per level but 'by' names no dim",
            code=ErrorCode.SETTINGS_INVALID,
        )
    if by not in like.dims or by not in like.coords:
        raise schema_error(step_id, f"'by' dim {by!r} is not a labelled dim of the data")
    levels = [str(v) for v in np.asarray(like[by].values).tolist()]
    unknown = [lvl for lvl in levels if lvl not in value]
    if unknown:
        raise CoordPlanError(
            f"step {step_id!r}: {len(unknown)} level(s) of {by!r} have no {name} value",
            code=ErrorCode.UNSEEN_LEVELS,
            untrusted_text=", ".join(unknown[:10]),
        )
    return xr.DataArray(
        np.array([float(value[lvl]) for lvl in levels]),
        dims=[by],
        coords={by: like[by].values},
    )
