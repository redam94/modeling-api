"""RESHAPE steps: change the structure of the data (dims, coords, layout).

``resample`` (``full_periods_only``), ``count_events``, ``filter_coords``, ``to_long``,
``to_padded``, ``stack`` and ``unstack``. Reshapes are stateless and are never inverted:
model outputs map back through VALUE steps only as far as the last reshape (plan §4.2).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, Self

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.steps import Step, StepContext, StepSettings, StepState
from mapi_spec.coords import parse_freq
from mapi_spec.effects import StepKind
from mapi_spec.errors import CoordPlanError, ErrorCode
from mapi_spec.schema import DimPolicy
from pydantic import Field, JsonValue, field_validator, model_validator

from mapi_steps._common import (
    Name,
    NameList,
    present,
    require_dim,
    require_vars,
    schema_error,
)
from mapi_steps._time import coord_freq, full_period_labels

__all__ = [
    "CountEventsStep",
    "FilterCoordsStep",
    "ResampleStep",
    "StackStep",
    "ToLongStep",
    "ToPaddedStep",
    "UnstackStep",
]

Scalar = str | int | float | bool
How = Literal["sum", "mean", "max", "min", "first", "last"]


def _freq(v: str) -> str:
    parse_freq(v)
    return v


# ============================================================================= resample


class ResampleSettings(StepSettings):
    dim: Name = "time"
    freq: str = Field(..., description="Target frequency, e.g. 'W-SUN' or 'MS'")
    how: How = "sum"
    how_vars: dict[Name, How] = Field(
        default_factory=dict, description="Per-variable aggregation overriding `how`"
    )
    full_periods_only: bool = Field(
        True, description="Drop periods that are not fully covered by the source data"
    )
    skipna: bool = Field(
        False, description="Ignore missing values (a period with only missing values stays NaN)"
    )

    @field_validator("freq")
    @classmethod
    def _f(cls, v: str) -> str:
        return _freq(v)


class ResampleStep(Step[ResampleSettings, StepState]):
    """Aggregate a datetime dim to a coarser frequency (pandas labelling conventions).

    With ``full_periods_only`` a period is kept only when it contains every label the
    source frequency puts in it (the source frequency comes from the input schema or is
    inferred), so partial first and last weeks never look like low-spend weeks.
    """

    id = "core.resample"
    version = "1.0.0"
    kind = StepKind.RESHAPE
    Settings = ResampleSettings
    dim_params = ("dim",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["dim", "freq"],
        "transforms_dims": {"{dim}": {"kind": "time", "policy": "extend", "freq": "{freq}"}},
    }
    summary = "Aggregate a time dim to a coarser frequency"
    summary_plain = "Turns daily data into weekly or monthly totals"

    def transform(
        self, ds: xr.Dataset, s: ResampleSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        require_dim(ds, s.dim, ctx.step_id)
        if s.dim not in ds.coords or ds[s.dim].dtype.kind != "M":
            raise schema_error(ctx.step_id, f"resample needs datetime labels on {s.dim!r}")
        users = [str(v) for v in ds.data_vars if s.dim in ds[v].dims]
        static = ds.drop_dims(s.dim)
        parts: list[xr.Dataset] = []
        if users:
            groups: dict[str, list[str]] = {}
            for v in users:
                how = s.how_vars.get(v, s.how)
                if ds[v].dtype.kind not in "fiub" and how not in ("first", "last"):
                    raise schema_error(
                        ctx.step_id, f"variable {v!r} is not numeric; use how 'first' or 'last'"
                    )
                groups.setdefault(how, []).append(v)
            for agg_how, names in groups.items():
                parts.append(self._agg(ds[names], s, agg_how))
        else:  # coordinate-only frames (e.g. forecast horizons) still get resampled labels
            ones = xr.Dataset({"__ones__": xr.ones_like(ds[s.dim], dtype=float)})
            parts.append(ones.resample({s.dim: s.freq}).sum().drop_vars("__ones__"))
        agg = xr.merge(parts, combine_attrs="override", join="outer")
        if s.full_periods_only:
            agg = agg.isel({s.dim: self._full(ds, s, ctx, agg)})
        for v in users:
            agg[v].attrs = dict(ds[v].attrs)
        out = xr.merge([agg, static], combine_attrs="override")
        out.attrs = dict(ds.attrs)
        return out

    @staticmethod
    def _agg(ds: xr.Dataset, s: ResampleSettings, how: str) -> xr.Dataset:
        res = ds.resample({s.dim: s.freq})
        match how:
            case "sum":
                return res.sum(skipna=s.skipna, min_count=1) if s.skipna else res.sum(skipna=False)
            case "mean":
                return res.mean(skipna=s.skipna)
            case "max":
                return res.max(skipna=s.skipna)
            case "min":
                return res.min(skipna=s.skipna)
            case "first":
                return res.first(skipna=s.skipna)
            case _:
                return res.last(skipna=s.skipna)

    @staticmethod
    def _full(ds: xr.Dataset, s: ResampleSettings, ctx: StepContext, agg: xr.Dataset) -> np.ndarray:
        values = np.asarray(ds[s.dim].values)
        if values.size == 0:
            return np.arange(0)
        src = coord_freq(values, ctx, s.dim)
        if src is None:
            raise schema_error(
                ctx.step_id,
                f"full_periods_only needs the frequency of {s.dim!r}; declare freq on the dim",
            )
        bins, full = full_period_labels(pd.DatetimeIndex(values), s.freq, src)
        complete = np.asarray(bins[full].values)
        return np.flatnonzero(np.isin(np.asarray(agg[s.dim].values), complete))


# ========================================================================= count_events


class CountEventsSettings(StepSettings):
    along: Name = Field("event", description="Row dim of the event table")
    time_var: Name = Field("timestamp", description="Datetime variable along the rows")
    freq: str = Field(..., description="Period length of the counts, e.g. 'D' or 'W-SUN'")
    dim: Name = Field("time", description="Name of the new time dim")
    by: NameList = Field(default_factory=list, description="Row variables that become dims")
    sum_vars: NameList = Field(
        default_factory=list, description="Numeric row variables summed per period"
    )
    out: Name = "count"
    levels: dict[Name, list[str]] = Field(
        default_factory=dict, description="Pinned levels per `by` variable (default: observed)"
    )
    start: str | None = Field(
        None, description="Earliest event time kept (ISO); the grid starts at its period"
    )
    end: str | None = Field(
        None, description="Latest event time kept (ISO); the grid ends at its period"
    )

    @field_validator("freq")
    @classmethod
    def _f(cls, v: str) -> str:
        return _freq(v)

    @model_validator(mode="after")
    def _check(self) -> Self:
        unknown = sorted(set(self.levels) - set(self.by))
        if unknown:
            raise ValueError(f"levels given for variables not in 'by': {unknown}")
        for bound in (self.start, self.end):
            if bound is not None:
                pd.Timestamp(bound)
        return self


class CountEventsStep(Step[CountEventsSettings, StepState]):
    """Count rows of a long event table per period (and per ``by`` level) on a dense grid.

    Every variable along ``along`` must be ``time_var``, a ``by`` variable or a
    ``sum_vars`` entry; empty periods count 0. The output grid spans the observed events
    unless ``start``/``end`` pin it.
    """

    id = "core.count_events"
    version = "1.0.0"
    kind = StepKind.RESHAPE
    Settings = CountEventsSettings
    var_params = ("time_var", "by", "sum_vars")
    dim_params = ("along",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["along", "time_var", "by", "sum_vars", "dim", "freq", "out"],
        "drops": ["{time_var}", "{*by}", "{*sum_vars}", "{along}"],
        "transforms_dims": {
            "{dim}": {"kind": "time", "policy": "extend", "freq": "{freq}"},
            "{*by as b}": {"kind": "categorical", "policy": "fixed"},
        },
        "adds": {
            "{out}": {"dims": ["{*by}", "{dim}"], "dtype": "float64", "units": "1"},
            "{*sum_vars as v}": {"dims": ["{*by}", "{dim}"], "dtype": "float64"},
        },
    }
    summary = "Count events per period on a dense grid"
    summary_plain = "Turns a list of events into counts per day, week or month"

    def transform(
        self, ds: xr.Dataset, s: CountEventsSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        require_dim(ds, s.along, ctx.step_id)
        require_vars(ds, [s.time_var, *s.by], ctx.step_id)
        sums = present(ds, s.sum_vars)
        used = [s.time_var, *s.by, *sums]
        others = [str(v) for v in ds.data_vars if s.along in ds[v].dims and v not in used]
        if others:
            raise schema_error(
                ctx.step_id, f"variables {others} along {s.along!r} are neither counted nor summed"
            )
        times = pd.DatetimeIndex(np.asarray(ds[s.time_var].values))
        frame = pd.DataFrame({"__t": times, "__n": 1.0})
        for b in s.by:
            raw = pd.Series(np.asarray(ds[b].values))
            if raw.isna().any():
                raise schema_error(ctx.step_id, f"'by' variable {b!r} has missing values")
            frame[b] = raw.astype(str).to_numpy()
        for v in sums:
            frame[v] = np.asarray(ds[v].values, dtype=np.float64)
        frame = frame[frame["__t"].notna()]
        if s.start is not None:
            frame = frame[frame["__t"] >= pd.Timestamp(s.start)]
        if s.end is not None:
            frame = frame[frame["__t"] <= pd.Timestamp(s.end)]
        labels = self._labels(frame["__t"], s)
        levels = []
        for b in s.by:
            observed = sorted(set(frame[b].tolist()))
            pinned = s.levels.get(b)
            if pinned is not None:
                unknown = sorted(set(observed) - set(pinned))
                if unknown:
                    raise CoordPlanError(
                        f"step {ctx.step_id!r}: {len(unknown)} level(s) of {b!r} are not pinned",
                        code=ErrorCode.UNSEEN_LEVELS,
                        untrusted_text=", ".join(unknown[:10]),
                    )
                observed = list(pinned)
            levels.append(observed)
        value_cols = ["__n", *sums]
        grouped = frame.groupby([*s.by, pd.Grouper(key="__t", freq=s.freq)])[value_cols].sum()
        grid = pd.MultiIndex.from_product([*levels, labels], names=[*s.by, "__t"])
        full = grouped.reindex(grid, fill_value=0.0)
        shape = [len(lv) for lv in levels] + [len(labels)]
        dims = [*s.by, s.dim]
        coords: dict[str, np.ndarray] = {
            b: np.asarray(lv) for b, lv in zip(s.by, levels, strict=True)
        }
        coords[s.dim] = np.asarray(labels.values, dtype="datetime64[ns]")
        out_vars: dict[str, xr.DataArray] = {
            s.out: xr.DataArray(full["__n"].to_numpy().reshape(shape), dims=dims, coords=coords)
        }
        for v in sums:
            da = xr.DataArray(full[v].to_numpy().reshape(shape), dims=dims, coords=coords)
            da.attrs = dict(ds[v].attrs)
            out_vars[v] = da
        rest = ds.drop_vars(used).drop_dims(s.along)
        out = rest.assign(out_vars)
        out.attrs = dict(ds.attrs)
        return out

    @staticmethod
    def _labels(times: pd.Series, s: CountEventsSettings) -> pd.DatetimeIndex:
        points = list(times)
        if s.start is not None:
            points.append(pd.Timestamp(s.start))
        if s.end is not None:
            points.append(pd.Timestamp(s.end))
        if not points:
            return pd.DatetimeIndex([], dtype="datetime64[ns]")
        idx = pd.DatetimeIndex(points)
        return pd.DatetimeIndex(
            pd.Series(np.ones(len(idx)), index=idx).resample(s.freq).sum().index
        ).as_unit("ns")


# ======================================================================== filter_coords


class FilterCoordsSettings(StepSettings):
    dim: Name
    keep: list[Scalar] | None = Field(None, max_length=100_000)
    drop: list[Scalar] | None = Field(None, max_length=100_000)
    start: Scalar | None = Field(None, description="Inclusive lower bound (ordered dims)")
    end: Scalar | None = Field(None, description="Inclusive upper bound; also applies at score")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.keep is not None and self.drop is not None:
            raise ValueError("give keep or drop, not both")
        if all(x is None for x in (self.keep, self.drop, self.start, self.end)):
            raise ValueError("give keep, drop, start and/or end")
        return self


def _coerce(values: list[Scalar], like: np.ndarray) -> np.ndarray:
    if like.dtype.kind == "M":
        return np.asarray([pd.Timestamp(str(v)).to_datetime64() for v in values], "datetime64[ns]")
    if like.dtype.kind in "iuf":
        return np.asarray(values, dtype=np.float64)
    return np.asarray([str(v) for v in values])


class FilterCoordsStep(Step[FilterCoordsSettings, StepState]):
    """Keep or drop labels of one dim (lists and/or an inclusive ``start``/``end`` range)."""

    id = "core.filter_coords"
    version = "1.0.0"
    kind = StepKind.RESHAPE
    Settings = FilterCoordsSettings
    dim_params = ("dim",)
    effect_template: ClassVar[dict[str, JsonValue]] = {}
    summary = "Select labels of a dim"
    summary_plain = "Keeps only some markets, products or dates"

    def transform(
        self, ds: xr.Dataset, s: FilterCoordsSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        require_dim(ds, s.dim, ctx.step_id)
        if s.dim not in ds.coords:
            raise schema_error(ctx.step_id, f"dim {s.dim!r} has no labels to filter")
        labels = np.asarray(ds[s.dim].values)
        keep = np.ones(labels.size, dtype=bool)
        if s.keep is not None:
            keep &= np.isin(labels, _coerce(s.keep, labels))
        if s.drop is not None:
            keep &= ~np.isin(labels, _coerce(s.drop, labels))
        for bound, upper in ((s.start, False), (s.end, True)):
            if bound is None:
                continue
            if labels.dtype.kind not in "Miuf":
                raise schema_error(ctx.step_id, f"start/end need an ordered dim, {s.dim!r} is not")
            b = _coerce([bound], labels)[0]
            keep &= (labels <= b) if upper else (labels >= b)
        return ds.isel({s.dim: np.flatnonzero(keep)})


# ============================================================================ ragged forms


class ToLongSettings(StepSettings):
    group_dim: Name = Field(..., description="Outer ragged dim (e.g. household)")
    within_dim: Name = Field(..., description="Inner padded dim (e.g. trip position)")
    row_dim: Name = "obs"
    vars: NameList = Field(..., min_length=1, description="Every variable on the padded grid")
    mask: Name | None = Field(
        None, description="Boolean padding mask (one of vars); rows where it is False go"
    )
    drop_missing: Literal["all", "none"] = Field(
        "all", description="Drop rows where every listed variable is missing"
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.mask is not None and self.mask not in self.vars:
            raise ValueError("mask must be one of vars")
        if len({self.group_dim, self.within_dim, self.row_dim}) != 3:
            raise ValueError("group_dim, within_dim and row_dim must differ")
        return self


class ToLongStep(Step[ToLongSettings, StepState]):
    """Padded ``(group, within, ...)`` → long ``(obs, ...)`` plus ``*_idx`` index variables.

    ``{group_dim}_idx(obs)`` and ``{within_dim}_idx(obs)`` index the coordinate positions of
    the group and within dims, which stay in the data as label-only dims.
    """

    id = "core.to_long"
    version = "1.0.0"
    kind = StepKind.RESHAPE
    Settings = ToLongSettings
    var_params = ("vars",)
    dim_params = ("group_dim", "within_dim")
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["group_dim", "within_dim", "row_dim", "vars"],
        "renames": {"{within_dim}": "{row_dim}"},
        "transforms_dims": {
            "{row_dim}": {"kind": "integer", "policy": "extend"},
            "{within_dim}": {"kind": "integer", "policy": "fixed"},
        },
        "updates": {"{*vars as v}": {"remove_dims": ["{group_dim}"], "missing": "allow"}},
        "adds": {
            "{group_dim}_idx": {
                "dims": ["{row_dim}"],
                "dtype": "int64",
                "role": "index",
                "index_of": "{group_dim}",
            },
            "{within_dim}_idx": {
                "dims": ["{row_dim}"],
                "dtype": "int64",
                "role": "index",
                "index_of": "{within_dim}",
            },
        },
    }
    summary = "Padded ragged data to long form"
    summary_plain = "Turns a padded table into one row per observation"

    def transform(
        self, ds: xr.Dataset, s: ToLongSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        g, w = s.group_dim, s.within_dim
        require_dim(ds, g, ctx.step_id)
        require_dim(ds, w, ctx.step_id)
        names = present(ds, s.vars)
        if s.mask is not None:
            require_vars(ds, [s.mask], ctx.step_id)
        stray = [
            str(v) for v in ds.data_vars if v not in s.vars and ({g, w} & set(map(str, ds[v].dims)))
        ]
        if stray:
            raise schema_error(ctx.step_id, f"variables {stray} use {g!r}/{w!r}; list them in vars")
        n_g, n_w = ds.sizes[g], ds.sizes[w]
        flat: dict[str, tuple[np.ndarray, list[str]]] = {}
        for v in names:
            da = ds[v]
            if not {g, w} <= set(map(str, da.dims)):
                raise schema_error(ctx.step_id, f"variable {v!r} must use both {g!r} and {w!r}")
            rest = [str(d) for d in da.dims if d not in (g, w)]
            arr = np.asarray(da.transpose(g, w, *rest).values)
            flat[v] = (arr.reshape(n_g * n_w, *arr.shape[2:]), rest)
        valid = np.ones(n_g * n_w, dtype=bool)
        if s.mask is not None:
            m = flat[s.mask][0]
            if m.dtype.kind != "b" or m.ndim != 1:
                raise schema_error(ctx.step_id, f"mask {s.mask!r} must be boolean over {g}, {w}")
            valid &= m
        if s.drop_missing == "all":
            any_value = np.zeros(n_g * n_w, dtype=bool)
            for v, (arr, _) in flat.items():
                if v == s.mask:
                    continue
                notnull = ~pd.isna(arr) if arr.dtype.kind in "fMO" else np.ones(arr.shape, bool)
                any_value |= notnull.reshape(n_g * n_w, -1).any(axis=1)
            valid &= any_value
        rows = np.flatnonzero(valid)
        gi, wi = np.divmod(rows, n_w)
        new: dict[str, xr.DataArray] = {}
        for v, (arr, rest) in flat.items():
            coords = {d: ds[d].values for d in rest if d in ds.coords}
            new[v] = xr.DataArray(
                arr[rows], dims=[s.row_dim, *rest], coords=coords, attrs=dict(ds[v].attrs)
            )
        new[f"{g}_idx"] = xr.DataArray(gi.astype(np.int64), dims=[s.row_dim])
        new[f"{w}_idx"] = xr.DataArray(wi.astype(np.int64), dims=[s.row_dim])
        out = ds.drop_vars(names).assign(new)
        out = out.assign_coords({s.row_dim: np.arange(rows.size, dtype=np.int64)})
        out.attrs = dict(ds.attrs)
        return out


class ToPaddedSettings(StepSettings):
    row_dim: Name = "obs"
    group: Name = Field(..., description="Variable along the rows naming each row's group")
    group_dim: Name = Field(..., description="Name of the outer dim (existing or new)")
    within_dim: Name = Field(..., description="Name of the new position dim")
    vars: NameList = Field(..., min_length=1, description="Every other variable on the rows")
    mask_var: Name = Field("mask", description="Name of the boolean validity mask to add")
    group_policy: DimPolicy = DimPolicy.FIXED

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.group in self.vars:
            raise ValueError("group must not be listed in vars")
        if len({self.group_dim, self.within_dim, self.row_dim}) != 3:
            raise ValueError("group_dim, within_dim and row_dim must differ")
        return self


class ToPaddedStep(Step[ToPaddedSettings, StepState]):
    """Long ``(obs, ...)`` → padded ``(group, within, ...)`` with a boolean mask (row order).

    ``group`` holds either labels or, when ``group_dim`` already exists, integer positions
    into it (an ``index_of`` variable). Padded cells are NaN and ``mask_var`` is False there.
    """

    id = "core.to_padded"
    version = "1.0.0"
    kind = StepKind.RESHAPE
    Settings = ToPaddedSettings
    var_params = ("vars", "group")
    dim_params = ("row_dim",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": [
            "row_dim",
            "group",
            "group_dim",
            "within_dim",
            "vars",
            "mask_var",
            "group_policy",
        ],
        "renames": {"{row_dim}": "{within_dim}"},
        "drops": ["{group}"],
        "transforms_dims": {
            "{within_dim}": {"kind": "integer", "policy": "extend"},
            "{group_dim}": {"kind": "categorical", "policy": "{group_policy}"},
        },
        "updates": {
            "{*vars as v}": {
                "append_dims": ["{group_dim}"],
                "missing": "allow",
                "dtype": "float64",
            }
        },
        "adds": {
            "{mask_var}": {"dims": ["{group_dim}", "{within_dim}"], "dtype": "bool", "role": "mask"}
        },
    }
    summary = "Long ragged data to padded form with a mask"
    summary_plain = "Lines up each group's rows side by side, padding short groups"

    def transform(
        self, ds: xr.Dataset, s: ToPaddedSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        require_dim(ds, s.row_dim, ctx.step_id)
        require_vars(ds, [s.group], ctx.step_id)
        names = present(ds, s.vars)
        if ds[s.group].dims != (s.row_dim,):
            raise schema_error(ctx.step_id, f"group {s.group!r} must be 1-D along {s.row_dim!r}")
        stray = [
            str(v)
            for v in ds.data_vars
            if v not in s.vars and v != s.group and s.row_dim in ds[v].dims
        ]
        if stray:
            raise schema_error(ctx.step_id, f"variables {stray} use {s.row_dim!r}; list them")
        groups = np.asarray(ds[s.group].values)
        codes, levels = self._codes(ds, s, groups, ctx)
        pos = pd.Series(codes).groupby(codes).cumcount().to_numpy()
        n_g = len(levels)
        width = int(pos.max()) + 1 if pos.size else 0
        new: dict[str, xr.DataArray] = {}
        for v in names:
            da = ds[v]
            rest = [str(d) for d in da.dims if d != s.row_dim]
            arr = np.asarray(da.transpose(s.row_dim, *rest).values, dtype=np.float64)
            grid = np.full((n_g, width, *arr.shape[1:]), np.nan)
            grid[codes, pos] = arr
            coords = {d: ds[d].values for d in rest if d in ds.coords}
            new[v] = xr.DataArray(
                grid, dims=[s.group_dim, s.within_dim, *rest], coords=coords, attrs=da.attrs
            )
        mask = np.zeros((n_g, width), dtype=bool)
        mask[codes, pos] = True
        new[s.mask_var] = xr.DataArray(mask, dims=[s.group_dim, s.within_dim])
        rest_ds = ds.drop_vars([s.group, *names])
        if s.row_dim in rest_ds.dims:
            rest_ds = rest_ds.drop_dims(s.row_dim)
        if s.group_dim in rest_ds.dims:
            rest_ds = rest_ds.drop_dims(s.group_dim)
        out = rest_ds.assign(new).assign_coords(
            {s.group_dim: levels, s.within_dim: np.arange(width, dtype=np.int64)}
        )
        out.attrs = dict(ds.attrs)
        return out

    @staticmethod
    def _codes(
        ds: xr.Dataset, s: ToPaddedSettings, groups: np.ndarray, ctx: StepContext
    ) -> tuple[np.ndarray, np.ndarray]:
        if s.group_dim in ds.coords:
            levels = np.asarray(ds[s.group_dim].values)
            if groups.dtype.kind in "iu" and levels.dtype.kind not in "iu":
                if groups.size and (groups.min() < 0 or groups.max() >= levels.size):
                    raise schema_error(ctx.step_id, f"{s.group!r} indexes outside {s.group_dim!r}")
                return groups.astype(np.int64), levels
            lookup = {v: i for i, v in enumerate(levels.tolist())}
            missing = [g for g in groups.tolist() if g not in lookup]
            if missing:
                raise schema_error(
                    ctx.step_id,
                    f"{len(missing)} group label(s) are not levels of {s.group_dim!r}",
                    untrusted_text=", ".join(str(m) for m in missing[:10]),
                )
            return np.asarray([lookup[g] for g in groups.tolist()], dtype=np.int64), levels
        if pd.isna(pd.Series(groups)).any():
            raise schema_error(ctx.step_id, f"group {s.group!r} has missing values")
        levels = np.unique(groups)
        return np.searchsorted(levels, groups).astype(np.int64), levels


# ========================================================================= stack/unstack


class StackSettings(StepSettings):
    dims: NameList = Field(..., min_length=2, max_length=8)
    new_dim: Name


class StackStep(Step[StackSettings, StepState]):
    """Stack several dims into one MultiIndex dim (unstack restores them)."""

    id = "core.stack"
    version = "1.0.0"
    kind = StepKind.RESHAPE
    Settings = StackSettings
    dim_params = ("dims",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["dims", "new_dim"],
        "stacks": {"{new_dim}": "{dims}"},
    }
    summary = "Stack dims into one"
    summary_plain = "Combines several dimensions into a single list of cells"

    def transform(
        self, ds: xr.Dataset, s: StackSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        for d in s.dims:
            require_dim(ds, d, ctx.step_id)
        if s.new_dim in ds.variables or s.new_dim in ds.dims:
            raise schema_error(ctx.step_id, f"{s.new_dim!r} already exists")
        for v in ds.data_vars:
            have = [d for d in s.dims if d in ds[v].dims]
            if have and len(have) != len(s.dims):
                raise schema_error(ctx.step_id, f"variable {v!r} has only some of {s.dims}")
        return ds.stack({s.new_dim: list(s.dims)})


class UnstackSettings(StepSettings):
    dim: Name
    into: NameList = Field(..., min_length=2, max_length=8, description="The stacked levels")


class UnstackStep(Step[UnstackSettings, StepState]):
    """Unstack a stacked dim back into its component dims (missing cells become NaN)."""

    id = "core.unstack"
    version = "1.0.0"
    kind = StepKind.RESHAPE
    Settings = UnstackSettings
    dim_params = ("dim",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["dim", "into"],
        "unstacks": {"{dim}": "{into}"},
    }
    summary = "Unstack a dim into several"
    summary_plain = "Splits a combined dimension back into its parts"

    def transform(
        self, ds: xr.Dataset, s: UnstackSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        require_dim(ds, s.dim, ctx.step_id)
        index = ds.indexes.get(s.dim)
        if isinstance(index, pd.MultiIndex):
            if [str(n) for n in index.names] != list(s.into):
                raise schema_error(
                    ctx.step_id, f"{s.dim!r} stacks {list(index.names)}, not {list(s.into)}"
                )
            return ds.unstack(s.dim)
        levels = [c for c in s.into if c in ds.coords and ds[c].dims == (s.dim,)]
        if levels != list(s.into):
            raise schema_error(
                ctx.step_id, f"{s.dim!r} has no level coordinates {list(s.into)} to unstack"
            )
        if s.dim in ds.coords:
            ds = ds.drop_vars(s.dim)
        index_spec: dict[Any, Any] = {s.dim: list(s.into)}
        return ds.set_index(index_spec).unstack(s.dim)
