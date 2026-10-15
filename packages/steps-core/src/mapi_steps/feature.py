"""FEATURE steps: add new variables computed from coordinates, side tables or other variables.

``fourier`` (state: origin and frequency), ``trend``, ``calendar``, ``holidays`` (side
table), ``lag``, ``lag_tensor``, ``one_hot``, ``encode_levels`` and ``missing_indicator``.
``core.formula`` lives in :mod:`mapi_steps.formula`.

Group-local steps (``lag``, ``lag_tensor``) take ``along``/``within``, work on dense and long
layouts, respect an optional boolean ``mask`` variable and declare a per-group
:class:`~mapi_spec.effects.ContextNeed` so score-time replay sees the fitted history.
"""

from __future__ import annotations

import functools
from typing import ClassVar, Literal, Self

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.steps import Step, StepContext, StepSettings, StepState, UnseenPolicy
from mapi_spec.coords import parse_freq
from mapi_spec.effects import StepKind
from mapi_spec.errors import CoordPlanError, ErrorCode, MapiError
from pydantic import Field, JsonValue, field_validator, model_validator

from mapi_steps._common import (
    Name,
    NameList,
    apply_along,
    assign_vars,
    first_valid,
    present,
    require_dim,
    require_vars,
    schema_error,
    shift_last,
)
from mapi_steps._time import Unit, coord_freq, full_period_labels, origin_json, positions

__all__ = [
    "CalendarStep",
    "EncodeLevelsStep",
    "FourierStep",
    "HolidaysStep",
    "LagStep",
    "LagTensorStep",
    "MissingIndicatorStep",
    "OneHotStep",
    "TrendStep",
]


def _check_freq(v: str | None) -> str | None:
    if v is not None:
        parse_freq(v)
    return v


class _TimeState(StepState):
    origin: JsonValue = None
    freq: str | None = None


def _fit_time_state(ds: xr.Dataset, along: str, unit: Unit, ctx: StepContext) -> _TimeState:
    require_dim(ds, along, ctx.step_id)
    if along not in ds.coords or ds.sizes[along] == 0:
        raise schema_error(ctx.step_id, f"dim {along!r} needs labelled, non-empty coordinates")
    values = np.asarray(ds[along].values)
    try:
        origin = origin_json(values.min())
    except TypeError:
        raise schema_error(ctx.step_id, f"dim {along!r} must be datetime or numeric") from None
    freq = coord_freq(values, ctx, along) if unit == "step" else None
    if values.dtype.kind == "M" and unit == "step" and freq is None:
        raise schema_error(
            ctx.step_id,
            f"cannot tell the frequency of {along!r}; declare freq on the dim or use unit 'days'",
        )
    return _TimeState(origin=origin, freq=freq)


# ============================================================================== fourier


class FourierSettings(StepSettings):
    along: Name = "time"
    period: float = Field(..., gt=0, description="Season length in units of `unit`")
    order: int = Field(2, ge=1, le=50)
    out: Name = "fourier"
    unit: Unit = Field(
        "step", description="'step': periods of the dim's frequency; 'days': calendar days"
    )


class FourierState(_TimeState):
    pass


class FourierStep(Step[FourierSettings, FourierState]):
    """Seasonal Fourier features ``sin/cos(2πk·t/period)``; the origin is learned at fit."""

    id = "core.fourier"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = FourierSettings
    State = FourierState
    dim_params = ("along",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["out", "along"],
        "transforms_dims": {"{out}_mode": {"kind": "categorical", "policy": "fixed"}},
        "adds": {"{out}": {"dims": ["{along}", "{out}_mode"], "dtype": "float64"}},
    }
    summary = "Fourier seasonality features"
    summary_plain = "Adds smooth repeating patterns (for example yearly seasonality)"

    def fit(self, ds: xr.Dataset, s: FourierSettings, ctx: StepContext) -> FourierState:
        base = _fit_time_state(ds, s.along, s.unit, ctx)
        return FourierState(origin=base.origin, freq=base.freq)

    def transform(
        self, ds: xr.Dataset, s: FourierSettings, st: FourierState, ctx: StepContext
    ) -> xr.Dataset:
        require_dim(ds, s.along, ctx.step_id)
        values = np.asarray(ds[s.along].values)
        pos = positions(
            values, st.origin, freq=st.freq, unit=s.unit, step_id=ctx.step_id, dim=s.along
        )
        modes: list[str] = []
        cols: list[np.ndarray] = []
        for k in range(1, s.order + 1):
            angle = 2 * np.pi * k * pos / s.period
            modes += [f"sin_{k}", f"cos_{k}"]
            cols += [np.sin(angle), np.cos(angle)]
        mode_dim = f"{s.out}_mode"
        feats = xr.DataArray(
            np.stack(cols, axis=-1) if cols else np.zeros((values.size, 0)),
            dims=[s.along, mode_dim],
            coords={s.along: values, mode_dim: modes},
        )
        return assign_vars(ds, {s.out: feats})


# ================================================================================ trend


class TrendSettings(StepSettings):
    along: Name = "time"
    out: Name = "trend"
    unit: Unit = "step"
    power: int = Field(1, ge=1, le=3, description="Polynomial power of the scaled trend")
    scale_to_fit: bool = Field(
        True, description="Scale so the fitted range maps to [0, 1] (forecasts continue past 1)"
    )


class TrendState(_TimeState):
    span: float = 1.0


class TrendStep(Step[TrendSettings, TrendState]):
    """A deterministic trend ``((t - origin) / span) ** power`` learned on the fit range."""

    id = "core.trend"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = TrendSettings
    State = TrendState
    dim_params = ("along",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["out", "along"],
        "adds": {"{out}": {"dims": ["{along}"], "dtype": "float64", "role": "control"}},
    }
    summary = "Polynomial time trend"
    summary_plain = "Adds a steady upward or downward drift over time"

    def fit(self, ds: xr.Dataset, s: TrendSettings, ctx: StepContext) -> TrendState:
        base = _fit_time_state(ds, s.along, s.unit, ctx)
        span = 1.0
        if s.scale_to_fit:
            pos = positions(
                np.asarray(ds[s.along].values),
                base.origin,
                freq=base.freq,
                unit=s.unit,
                step_id=ctx.step_id,
                dim=s.along,
            )
            span = float(pos.max()) if pos.size and pos.max() > 0 else 1.0
        return TrendState(origin=base.origin, freq=base.freq, span=span)

    def transform(
        self, ds: xr.Dataset, s: TrendSettings, st: TrendState, ctx: StepContext
    ) -> xr.Dataset:
        require_dim(ds, s.along, ctx.step_id)
        values = np.asarray(ds[s.along].values)
        pos = positions(
            values, st.origin, freq=st.freq, unit=s.unit, step_id=ctx.step_id, dim=s.along
        )
        trend = (pos / st.span) ** s.power
        return assign_vars(
            ds, {s.out: xr.DataArray(trend, dims=[s.along], coords={s.along: values})}
        )


# ============================================================================= calendar

CalendarFeature = Literal[
    "day_of_week",
    "day_of_month",
    "day_of_year",
    "week_of_year",
    "month",
    "quarter",
    "year",
    "is_weekend",
    "is_month_start",
    "is_month_end",
]


def _calendar_value(idx: pd.DatetimeIndex, feature: str) -> np.ndarray:
    match feature:
        case "day_of_week":
            out = idx.dayofweek
        case "day_of_month":
            out = idx.day
        case "day_of_year":
            out = idx.dayofyear
        case "week_of_year":
            out = idx.isocalendar().week.to_numpy()
        case "month":
            out = idx.month
        case "quarter":
            out = idx.quarter
        case "year":
            out = idx.year
        case "is_weekend":
            out = idx.dayofweek >= 5
        case "is_month_start":
            out = idx.is_month_start
        case _:
            out = idx.is_month_end
    return np.asarray(out, dtype=np.float64)


class CalendarSettings(StepSettings):
    along: Name = "time"
    features: list[CalendarFeature] = Field(..., min_length=1, max_length=10)
    out: Name = "calendar"

    @field_validator("features")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("calendar features must be unique")
        return v


class CalendarStep(Step[CalendarSettings, StepState]):
    """Calendar features of a datetime dim as ``{out}(along, {out}_feature)``."""

    id = "core.calendar"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = CalendarSettings
    dim_params = ("along",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["out", "along"],
        "transforms_dims": {"{out}_feature": {"kind": "categorical", "policy": "fixed"}},
        "adds": {"{out}": {"dims": ["{along}", "{out}_feature"], "dtype": "float64"}},
    }
    summary = "Calendar features (day of week, month, ISO week, ...)"
    summary_plain = "Adds calendar facts such as the month or whether it is a weekend"

    def transform(
        self, ds: xr.Dataset, s: CalendarSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        require_dim(ds, s.along, ctx.step_id)
        values = np.asarray(ds[s.along].values)
        if values.dtype.kind != "M":
            raise schema_error(ctx.step_id, f"calendar needs a datetime dim, {s.along!r} is not")
        idx = pd.DatetimeIndex(values)
        cols = [_calendar_value(idx, f) for f in s.features]
        fdim = f"{s.out}_feature"
        feats = xr.DataArray(
            np.stack(cols, axis=-1),
            dims=[s.along, fdim],
            coords={s.along: values, fdim: list(s.features)},
        )
        return assign_vars(ds, {s.out: feats})


# ============================================================================= holidays


class HolidaysSettings(StepSettings):
    table: Name = Field(..., description="Side table with a daily (or finer) holiday variable")
    along: Name = "time"
    table_along: Name | None = Field(None, description="Time dim of the table, if different")
    table_var: Name = Field("holiday", description="Indicator/weight variable in the table")
    out: Name = "holiday"
    by: NameList = Field(default=[], description="Dims shared by table and data (e.g. country/geo)")
    how: Literal["sum", "max"] = Field(
        "sum", description="Aggregate table rows inside each data period (count or any)"
    )
    freq: str | None = Field(None, description="Frequency of `along` (default: from the data)")
    missing_levels: Literal["error", "zero"] = Field(
        "error", description="Levels of `by` dims absent from the table: error or zero"
    )

    @field_validator("freq")
    @classmethod
    def _freq(cls, v: str | None) -> str | None:
        return _check_freq(v)


class HolidaysStep(Step[HolidaysSettings, StepState]):
    """Holiday counts per period from a side table ``{table_var}(table_along[, by...])``.

    Table rows falling into each period of ``along`` are summed (or maxed), so a daily
    table yields "number of holiday days per week" for weekly data. Every needed period must
    be fully covered by the table (``SIDE_TABLE_COVERAGE`` otherwise).
    """

    id = "core.holidays"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = HolidaysSettings
    dim_params = ("along", "by")
    side_table_params = ("table",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["out", "along", "by"],
        "adds": {"{out}": {"dims": ["{along}", "{*by}"], "dtype": "float64", "role": "control"}},
    }
    summary = "Holiday indicators per period from a side table"
    summary_plain = "Marks periods that contain public holidays"

    def transform(
        self, ds: xr.Dataset, s: HolidaysSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        require_dim(ds, s.along, ctx.step_id)
        table = ctx.side_table(s.table)
        tdim = s.table_along or s.along
        if s.table_var not in table or tdim not in table[s.table_var].dims:
            raise MapiError(
                f"side table {s.table!r} needs a variable {s.table_var!r} along {tdim!r}",
                code=ErrorCode.SIDE_TABLE_COVERAGE,
            )
        var = table[s.table_var]
        extra = sorted(set(map(str, var.dims)) - {tdim, *s.by})
        if extra:
            raise MapiError(
                f"holiday variable has dims {extra} that are not in 'by'",
                code=ErrorCode.SIDE_TABLE_COVERAGE,
            )
        if tdim != s.along:
            var = var.rename({tdim: s.along})
        labels = np.asarray(ds[s.along].values)
        if labels.dtype.kind != "M":
            raise schema_error(ctx.step_id, f"holidays needs a datetime dim, {s.along!r} is not")
        per_period = self._per_period(var, labels, s, ctx)
        per_period = self._align_by(per_period, ds, s, ctx)
        out = per_period.transpose(s.along, *s.by).astype(np.float64)
        out.attrs = {}
        return assign_vars(ds, {s.out: out})

    def _per_period(
        self, var: xr.DataArray, labels: np.ndarray, s: HolidaysSettings, ctx: StepContext
    ) -> xr.DataArray:
        tlabels = np.asarray(var[s.along].values)
        freq = s.freq or coord_freq(labels, ctx, s.along)
        tfreq = coord_freq(tlabels, StepContext(step_id=ctx.step_id), s.along) or "D"
        values = var.astype(np.float64)
        if freq is None or freq == tfreq:
            missing = labels[~np.isin(labels, tlabels)]
            if missing.size:
                raise self._coverage(s, missing)
            return values.sel({s.along: labels})
        agg = values.resample({s.along: freq})
        res = agg.sum(skipna=True) if s.how == "sum" else agg.max(skipna=True)
        bins, full = full_period_labels(pd.DatetimeIndex(tlabels), freq, tfreq)
        complete = np.asarray(bins[full].values)
        missing = labels[~np.isin(labels, complete)]
        if missing.size:
            raise self._coverage(s, missing)
        return res.sel({s.along: labels})

    @staticmethod
    def _coverage(s: HolidaysSettings, missing: np.ndarray) -> MapiError:
        return MapiError(
            f"side table {s.table!r} does not fully cover {missing.size} {s.along!r} period(s)",
            code=ErrorCode.SIDE_TABLE_COVERAGE,
            untrusted_text=", ".join(str(v) for v in missing[:5]),
        )

    def _align_by(
        self, var: xr.DataArray, ds: xr.Dataset, s: HolidaysSettings, ctx: StepContext
    ) -> xr.DataArray:
        for d in s.by:
            require_dim(ds, d, ctx.step_id)
            if d not in var.dims:
                var = var.expand_dims({d: ds[d].values})
                continue
            want = np.asarray(ds[d].values)
            unknown = want[~np.isin(want, np.asarray(var[d].values))]
            if unknown.size and s.missing_levels == "error":
                raise MapiError(
                    f"side table {s.table!r} has no rows for {unknown.size} level(s) of {d!r}",
                    code=ErrorCode.SIDE_TABLE_COVERAGE,
                    untrusted_text=", ".join(str(v) for v in unknown[:5]),
                )
            var = var.reindex({d: want}, fill_value=0.0)
        return var


# ================================================================================== lags


class _LagBase(StepSettings):
    vars: NameList = Field(..., min_length=1)
    along: Name = "time"
    within: NameList = Field(
        default=[], description="Group dims (dense) or row group variables (long)"
    )
    mask: Name | None = Field(
        None, description="Boolean variable; cells where it is False count as missing"
    )


def _masked(da: xr.DataArray, ds: xr.Dataset, mask: str | None, step_id: str) -> xr.DataArray:
    if mask is None:
        return da
    if mask not in ds:
        raise schema_error(step_id, f"mask variable {mask!r} is missing")
    m = ds[mask]
    if m.dtype.kind != "b":
        raise schema_error(step_id, f"mask variable {mask!r} must be boolean")
    if not set(m.dims) <= set(da.dims):
        raise schema_error(step_id, f"mask {mask!r} has dims that {da.name!r} lacks")
    return da.where(m)


def _restore_mask(out: xr.DataArray, ds: xr.Dataset, mask: str | None) -> xr.DataArray:
    return out if mask is None else out.where(ds[mask])


class LagSettings(_LagBase):
    lags: list[int] = Field(..., min_length=1, max_length=64)
    lag_dim: Name = "lag"
    fill: float | None = Field(None, description="Value for lags with no history (default NaN)")

    @field_validator("lags")
    @classmethod
    def _lags(cls, v: list[int]) -> list[int]:
        if any(k < 1 for k in v) or len(set(v)) != len(v):
            raise ValueError("lags must be unique integers >= 1")
        return v


class LagStep(Step[LagSettings, StepState]):
    """Lagged copies ``{v}_lagged(..., lag)`` of ``vars`` along ``along`` within groups."""

    id = "core.lag"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = LagSettings
    depends_on_target = True
    target_vars_param = "vars"
    var_params = ("vars", "mask")
    dim_params = ("along",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars", "lag_dim"],
        "transforms_dims": {"{lag_dim}": {"kind": "integer", "policy": "fixed"}},
        "derives": {
            "{*vars as v}_lagged": {
                "from": "{v}",
                "append_dims": ["{lag_dim}"],
                "dtype": "float64",
                "role": "covariate",
                "missing": "allow",
            }
        },
    }
    context_template: ClassVar[dict[str, JsonValue]] = {
        "along": "{along}",
        "n": "{max:lags}",
        "within": "{within}",
        "min_lag": "{min:lags}",
    }
    summary = "Lagged copies of variables within groups"
    summary_plain = "Adds last period's (or earlier) values as extra columns"

    def touched_vars(self, s: LagSettings) -> set[str]:
        return set()  # adds lagged copies; the sources are unchanged

    def transform(
        self, ds: xr.Dataset, s: LagSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            da = _masked(ds[v], ds, s.mask, ctx.step_id)
            shifted = []
            for k in s.lags:
                lagged = apply_along(da, ds, s.along, s.within, functools.partial(_shift, k=k))
                shifted.append(_restore_mask(lagged, ds, s.mask))
            res = xr.concat(shifted, dim=pd.Index(s.lags, name=s.lag_dim))
            if s.fill is not None:
                res = res.fillna(s.fill)
            res = res.transpose(*da.dims, s.lag_dim)
            res.attrs = dict(ds[v].attrs)
            out[f"{v}_lagged"] = res
        return assign_vars(ds, out)


class LagTensorSettings(_LagBase):
    l_max: int = Field(..., ge=1, le=520, description="Number of lags, 0 .. l_max - 1")
    pad: Literal["first", "zero", "nan"] = Field(
        "zero", description="History before a series starts: its first value, 0 or NaN"
    )
    dim: Name = Field("lag", description="Name of the new lag dim")


def _shift(a: np.ndarray, *, k: int) -> np.ndarray:
    return shift_last(a, k)


def _lag_padded(a: np.ndarray, *, k: int, pad: str) -> np.ndarray:
    """Lag ``k`` along the last axis; positions before the series start are padded.

    The series starts at its first non-missing value (leading missing rows, such as context
    rows of a group unseen at fit, are "before the start" and stay missing themselves).
    """
    n = a.shape[-1]
    fv = first_valid(a)[..., None]
    t = np.arange(n)
    vals = shift_last(a, k)
    before = (t - k) < fv
    if pad == "first":
        first = np.take_along_axis(a, np.clip(fv, 0, max(n - 1, 0)), axis=-1)
        padv = np.where(fv < n, first, np.nan)
    elif pad == "zero":
        padv = np.zeros_like(fv, dtype=np.float64)
    else:
        padv = np.full(fv.shape, np.nan)
    vals = np.where(before, padv, vals)
    return np.where(t < fv, np.nan, vals)


class LagTensorStep(Step[LagTensorSettings, StepState]):
    """Replace ``vars`` by their lag tensor ``v(..., along, lag)`` with lags ``0..l_max-1``.

    Used by carry-over (adstock) models that convolve inside the model. History before a
    series' start is padded (``pad``); at score time the per-group context provides it.
    """

    id = "core.lag_tensor"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = LagTensorSettings
    depends_on_target = True
    target_vars_param = "vars"
    var_params = ("vars", "mask")
    dim_params = ("along",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars", "dim"],
        "transforms_dims": {"{dim}": {"kind": "integer", "policy": "fixed"}},
        "updates": {"{*vars as v}": {"append_dims": ["{dim}"], "dtype": "float64"}},
    }
    context_template: ClassVar[dict[str, JsonValue]] = {
        "along": "{along}",
        "n": "{l_max}",
        "within": "{within}",
        "min_lag": 0,
    }
    summary = "Lag tensor (lags 0 .. l_max-1) for in-model carry-over"
    summary_plain = "Keeps recent history of each series for carry-over effects"

    def transform(
        self, ds: xr.Dataset, s: LagTensorSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        lags = list(range(s.l_max))
        for v in present(ds, s.vars):
            da = _masked(ds[v], ds, s.mask, ctx.step_id)
            parts = [
                _restore_mask(
                    apply_along(
                        da, ds, s.along, s.within, functools.partial(_lag_padded, k=k, pad=s.pad)
                    ),
                    ds,
                    s.mask,
                )
                for k in lags
            ]
            res = xr.concat(parts, dim=pd.Index(lags, name=s.dim)).transpose(*da.dims, s.dim)
            res.attrs = dict(ds[v].attrs)
            out[v] = res
        return assign_vars(ds, out)


# ======================================================================= categorical codes


def _level_strings(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """String form of categorical values and a mask of missing entries."""
    flat = values.ravel()
    missing = np.array(
        [v is None or (isinstance(v, float) and np.isnan(v)) or v == "" for v in flat.tolist()],
        dtype=bool,
    ).reshape(values.shape)
    if values.dtype.kind == "f":
        as_int = np.where(missing, 0, values)
        if np.all(np.equal(np.mod(as_int, 1), 0)):
            text = np.asarray(as_int.astype(np.int64).astype(str))
        else:
            text = np.asarray(values.astype(str))
    else:
        text = np.asarray(values.astype(str))
    return text, missing


def _learn_levels(da: xr.DataArray, max_levels: int, step_id: str) -> tuple[list[str], list[float]]:
    text, missing = _level_strings(np.asarray(da.values))
    vals, counts = np.unique(text[~missing], return_counts=True)
    if vals.size > max_levels:
        raise schema_error(step_id, f"{da.name!r} has {vals.size} levels (max_levels={max_levels})")
    total = counts.sum() if counts.size else 1
    return [str(v) for v in vals.tolist()], [float(c / total) for c in counts.tolist()]


def _unseen_error(step_id: str, name: str, unseen: np.ndarray) -> CoordPlanError:
    preview = ", ".join(str(v) for v in np.unique(unseen)[:10])
    return CoordPlanError(
        f"step {step_id!r}: {np.unique(unseen).size} level(s) of {name!r} were not seen at fit "
        "and its unseen-level policy is 'error'",
        code=ErrorCode.UNSEEN_LEVELS,
        untrusted_text=preview,
    )


class OneHotSettings(StepSettings):
    vars: NameList = Field(..., min_length=1, description="Categorical variables to encode")
    drop_first: bool = False
    max_levels: int = Field(256, ge=1, le=4096)
    unseen_levels: UnseenPolicy = Field(
        "error", description="error, fill (all zeros) or pooled (training level frequencies)"
    )


class OneHotStep(Step[OneHotSettings, StepState]):
    """Indicator columns ``{v}_onehot(..., {v}_level)`` for the levels seen at fit."""

    id = "core.one_hot"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = OneHotSettings
    State = StepState
    var_params = ("vars",)
    unseen_levels_param = "unseen_levels"
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars"],
        "transforms_dims": {"{*vars as v}_level": {"kind": "categorical", "policy": "fixed"}},
        "derives": {
            "{*vars as v}_onehot": {
                "from": "{v}",
                "append_dims": ["{v}_level"],
                "dtype": "float64",
                "role": "covariate",
                "missing": "forbid",
                "units": "1",
            }
        },
    }
    summary = "One-hot indicators of categorical variables"
    summary_plain = "Turns categories into yes/no columns"

    def touched_vars(self, s: OneHotSettings) -> set[str]:
        return set()

    def fit(self, ds: xr.Dataset, s: OneHotSettings, ctx: StepContext) -> StepState:
        require_vars(ds, s.vars, ctx.step_id)
        levels: dict[str, JsonValue] = {}
        freqs: dict[str, JsonValue] = {}
        for v in s.vars:
            lv, fq = _learn_levels(ds[v], s.max_levels, ctx.step_id)
            levels[v] = list(lv)
            freqs[v] = list(fq)
        return StepState(learned={"levels": levels, "frequencies": freqs})

    def transform(
        self, ds: xr.Dataset, s: OneHotSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        levels_all = st.learned.get("levels", {})
        freqs_all = st.learned.get("frequencies", {})
        assert isinstance(levels_all, dict) and isinstance(freqs_all, dict)
        for v in present(ds, s.vars):
            levels = [str(x) for x in levels_all.get(v, [])]  # type: ignore[union-attr]
            freqs = np.asarray(freqs_all.get(v, []), dtype=np.float64)
            da = ds[v]
            text, missing = _level_strings(np.asarray(da.values))
            known = np.isin(text, levels) | missing
            if not known.all() and s.unseen_levels == "error":
                raise _unseen_error(ctx.step_id, v, text[~known])
            onehot = (text[..., None] == np.asarray(levels)).astype(np.float64)
            if s.unseen_levels == "pooled" and not known.all():
                onehot[~known] = freqs
            keep = slice(1, None) if s.drop_first else slice(None)
            ldim = f"{v}_level"
            res = xr.DataArray(
                onehot[..., keep],
                dims=(*da.dims, ldim),
                coords={**{d: da[d].values for d in da.dims if d in da.coords}, ldim: levels[keep]},
            )
            out[f"{v}_onehot"] = res
        return assign_vars(ds, out)


class EncodeLevelsSettings(StepSettings):
    vars: NameList = Field(default=[], description="Categorical variables to encode")
    dims: NameList = Field(default=[], description="Dims whose labels to encode")
    role: Literal["covariate", "index"] = "covariate"
    max_levels: int = Field(100_000, ge=1, le=1_000_000)
    unseen_levels: UnseenPolicy = Field(
        "error", description="error, fill (fill_code) or pooled (one shared code n_levels)"
    )
    fill_code: int = Field(-1, description="Code for missing values and 'fill' unseen levels")

    @model_validator(mode="after")
    def _some(self) -> Self:
        if not self.vars and not self.dims:
            raise ValueError("give vars and/or dims to encode")
        return self


class EncodeLevelsStep(Step[EncodeLevelsSettings, StepState]):
    """Integer codes (sorted fit-time levels) as ``{v}_code`` / ``{d}_code(d)`` variables."""

    id = "core.encode_levels"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = EncodeLevelsSettings
    State = StepState
    var_params = ("vars",)
    dim_params = ("dims",)
    unseen_levels_param = "unseen_levels"
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars", "dims", "role"],
        "derives": {
            "{*vars as v}_code": {
                "from": "{v}",
                "dtype": "int64",
                "role": "{role}",
                "missing": "forbid",
                "units": "1",
            }
        },
        "adds": {"{*dims as d}_code": {"dims": ["{d}"], "dtype": "int64", "role": "{role}"}},
    }
    summary = "Integer codes for categorical levels"
    summary_plain = "Numbers each category"

    def touched_vars(self, s: EncodeLevelsSettings) -> set[str]:
        return set()

    def fit(self, ds: xr.Dataset, s: EncodeLevelsSettings, ctx: StepContext) -> StepState:
        require_vars(ds, s.vars, ctx.step_id)
        levels: dict[str, JsonValue] = {}
        for v in s.vars:
            levels[f"var:{v}"] = list(_learn_levels(ds[v], s.max_levels, ctx.step_id)[0])
        for d in s.dims:
            require_dim(ds, d, ctx.step_id)
            if d not in ds.coords:
                raise schema_error(ctx.step_id, f"dim {d!r} has no labels to encode")
            lv, _ = _learn_levels(ds[d], s.max_levels, ctx.step_id)
            levels[f"dim:{d}"] = list(lv)
        return StepState(learned={"levels": levels})

    def _codes(
        self, values: np.ndarray, levels: list[str], s: EncodeLevelsSettings, name: str, sid: str
    ) -> np.ndarray:
        text, missing = _level_strings(values)
        lookup = {lvl: i for i, lvl in enumerate(levels)}
        codes = np.array([lookup.get(t, -2) for t in text.ravel().tolist()], dtype=np.int64)
        codes = codes.reshape(text.shape)
        unseen = (codes == -2) & ~missing
        if unseen.any():
            if s.unseen_levels == "error":
                raise _unseen_error(sid, name, text[unseen])
            codes[unseen] = len(levels) if s.unseen_levels == "pooled" else s.fill_code
        codes[missing] = s.fill_code
        return codes

    def transform(
        self, ds: xr.Dataset, s: EncodeLevelsSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        all_levels = st.learned.get("levels", {})
        assert isinstance(all_levels, dict)
        out: dict[str, xr.DataArray] = {}

        def levels_of(key: str) -> list[str]:
            raw = all_levels.get(key, [])
            return [str(x) for x in raw] if isinstance(raw, list) else []

        for v in present(ds, s.vars):
            da = ds[v]
            codes = self._codes(np.asarray(da.values), levels_of(f"var:{v}"), s, v, ctx.step_id)
            out[f"{v}_code"] = xr.DataArray(codes, dims=da.dims, coords=da.coords)
        for d in s.dims:
            require_dim(ds, d, ctx.step_id)
            labels = np.asarray(ds[d].values)
            codes = self._codes(labels, levels_of(f"dim:{d}"), s, d, ctx.step_id)
            out[f"{d}_code"] = xr.DataArray(codes, dims=[d], coords={d: labels})
        return assign_vars(ds, out)


# ===================================================================== missing indicators


class MissingIndicatorSettings(StepSettings):
    vars: NameList = Field(..., min_length=1)


class MissingIndicatorStep(Step[MissingIndicatorSettings, StepState]):
    """``{v}_missing`` = 1.0 where ``v`` is missing (NaN), else 0.0."""

    id = "core.missing_indicator"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = MissingIndicatorSettings
    var_params = ("vars",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars"],
        "derives": {
            "{*vars as v}_missing": {
                "from": "{v}",
                "dtype": "float64",
                "role": "covariate",
                "missing": "forbid",
                "units": "1",
            }
        },
    }
    summary = "Indicators of missing values"
    summary_plain = "Flags where values were missing"

    def touched_vars(self, s: MissingIndicatorSettings) -> set[str]:
        return set()

    def transform(
        self, ds: xr.Dataset, s: MissingIndicatorSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            out[f"{v}_missing"] = ds[v].isnull().astype(np.float64)
        return assign_vars(ds, out)
