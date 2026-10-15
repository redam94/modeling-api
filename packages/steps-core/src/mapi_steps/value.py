"""VALUE steps: change variable values in place without changing the data's structure.

``fx_convert`` (side table), ``mean_scale``, ``max_abs_scale``, ``standardize``, ``log1p``,
``logit``, ``impute``, ``replace_sentinel`` and ``clip``.

Reductions name the dims they reduce **over**; the learned state stays labelled by the
remaining dims (``mean_scale(over=[time])`` on ``spend(geo, channel, time)`` stores
``scale(geo, channel)``). Unseen levels at score time go through
:meth:`~mapi_sdk.steps.StepContext.align_state` with the step's ``unseen_levels`` policy.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Literal, Self

import numpy as np
import xarray as xr
from mapi_sdk.steps import Step, StepContext, StepSettings, StepState, UnseenPolicy
from mapi_spec.effects import StepKind
from mapi_spec.errors import CoordPlanError, ErrorCode, MapiError
from pydantic import Field, JsonValue, model_validator

from mapi_steps._common import (
    Name,
    NameList,
    align,
    apply_along,
    float_values,
    present,
    require_vars,
    schema_error,
)

__all__ = [
    "ClipStep",
    "FxConvertStep",
    "ImputeStep",
    "Log1pStep",
    "LogitStep",
    "MaxAbsScaleStep",
    "MeanScaleStep",
    "ReplaceSentinelStep",
    "StandardizeStep",
]

ISO_CURRENCY = r"^[A-Z]{3}$"
_FLOAT_UPDATE: dict[str, JsonValue] = {
    "template_params": ["vars"],
    "updates": {"{*vars as v}": {"dtype": "float64"}},
}


def _as_float(da: xr.DataArray) -> xr.DataArray:
    out = da.astype(np.float64) if da.dtype != np.float64 else da
    out.attrs = dict(da.attrs)
    return out


# =========================================================================== fx_convert


class FxConvertSettings(StepSettings):
    """Convert local-currency variables with per-period exchange rates from a side table."""

    vars: NameList = Field(..., min_length=1)
    table: Name = Field(..., description="Name of the side table in data.side_tables")
    to: str = Field("EUR", pattern=ISO_CURRENCY, description="Target currency (ISO 4217)")
    along: Name = Field("time", description="Time dim shared by the data and the table")
    table_along: Name | None = Field(None, description="Time dim name in the table, if different")
    rate_var: Name = Field("rate", description="Rate variable: units of `to` per local unit")
    table_currency_dim: Name = Field("currency", description="Currency dim of the rate table")
    currency_dim: Name | None = Field(
        None, description="Data dim whose levels have different local currencies (e.g. geo)"
    )
    currencies: dict[str, str] = Field(
        default={}, description="Local currency per level of currency_dim"
    )
    default_currency: str | None = Field(
        None, pattern=ISO_CURRENCY, description="Currency of levels missing from currencies"
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.currencies or self.default_currency) and self.currency_dim is None:
            raise ValueError("currencies/default_currency need currency_dim")
        bad = [c for c in self.currencies.values() if len(c) != 3 or not c.isupper()]
        if bad:
            raise ValueError(f"currencies must be ISO 4217 codes, got {bad}")
        return self


class FxConvertStep(Step[FxConvertSettings, StepState]):
    """Multiply ``vars`` by an exchange rate per period (and per local currency).

    The rate table is ``rate(table_along[, currency])`` quoted in units of ``to`` per unit of
    local currency. With a currency dim, each level of ``currency_dim`` maps to its
    currency through ``currencies``/``default_currency``; levels already in ``to`` use 1.
    Missing coverage raises ``SIDE_TABLE_COVERAGE``. Stateless, so it may precede the split.
    """

    id = "core.fx_convert"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = FxConvertSettings
    invertible = True
    var_params = ("vars",)
    dim_params = ("along", "currency_dim")
    side_table_params = ("table",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars", "to"],
        "updates": {"{*vars as v}": {"units": "{to}", "dtype": "float64"}},
    }
    summary = "Currency conversion with per-period rates from a side table"
    summary_plain = "Converts money columns into one currency using daily exchange rates"

    def touched_vars(self, s: FxConvertSettings) -> set[str]:
        return set(s.vars)

    def _rates(self, ds: xr.Dataset, s: FxConvertSettings, ctx: StepContext) -> xr.DataArray:
        table = ctx.side_table(s.table)
        if s.rate_var not in table:
            raise MapiError(
                f"side table {s.table!r} has no variable {s.rate_var!r}",
                code=ErrorCode.SIDE_TABLE_COVERAGE,
            )
        rate = table[s.rate_var]
        tdim = s.table_along or s.along
        if tdim not in rate.dims:
            raise MapiError(
                f"rate variable of side table {s.table!r} has no dim {tdim!r}",
                code=ErrorCode.SIDE_TABLE_COVERAGE,
            )
        if s.along not in ds.dims or s.along not in ds.coords:
            raise schema_error(ctx.step_id, f"dim {s.along!r} needs labelled coordinates")
        want = np.asarray(ds[s.along].values)
        known = np.asarray(rate[tdim].values)
        missing = want[~np.isin(want, known)]
        if missing.size:
            raise MapiError(
                f"side table {s.table!r} does not cover {missing.size} {s.along!r} label(s)",
                code=ErrorCode.SIDE_TABLE_COVERAGE,
                untrusted_text=", ".join(str(v) for v in missing[:5]),
            )
        rate = rate.sel({tdim: want})
        has_cur = s.table_currency_dim in rate.dims
        extra = [d for d in rate.dims if d not in (tdim, s.table_currency_dim)]
        if extra:
            raise MapiError(
                f"rate variable of side table {s.table!r} has unexpected dims {extra}",
                code=ErrorCode.SIDE_TABLE_COVERAGE,
            )
        if s.currency_dim is None:
            if has_cur:
                raise MapiError(
                    f"step {ctx.step_id!r}: the rate table has a {s.table_currency_dim!r} dim; "
                    "set currency_dim and currencies to say which currency each level uses",
                    code=ErrorCode.SETTINGS_INVALID,
                )
            values = float_values(rate.transpose(tdim))
            out = xr.DataArray(values, dims=[s.along], coords={s.along: want})
        else:
            out = self._per_level(ds, s, ctx, rate, tdim, want, has_cur)
        if not bool(np.isfinite(out.values).all()) or bool((out.values <= 0).any()):
            raise MapiError(
                f"side table {s.table!r} has missing or non-positive rates in the needed range",
                code=ErrorCode.SIDE_TABLE_COVERAGE,
            )
        return out

    def _per_level(
        self,
        ds: xr.Dataset,
        s: FxConvertSettings,
        ctx: StepContext,
        rate: xr.DataArray,
        tdim: str,
        want: np.ndarray,
        has_cur: bool,
    ) -> xr.DataArray:
        dim = s.currency_dim
        assert dim is not None
        if dim not in ds.dims or dim not in ds.coords:
            raise schema_error(ctx.step_id, f"currency_dim {dim!r} is not a labelled dim")
        levels = np.asarray(ds[dim].values)
        codes: list[str] = []
        unmapped: list[str] = []
        for lvl in levels.tolist():
            code = s.currencies.get(str(lvl), s.default_currency)
            if code is None:
                unmapped.append(str(lvl))
            codes.append(code or "")
        if unmapped:
            raise CoordPlanError(
                f"step {ctx.step_id!r}: {len(unmapped)} level(s) of {dim!r} have no currency; "
                "add them to currencies or set default_currency",
                code=ErrorCode.UNSEEN_LEVELS,
                untrusted_text=", ".join(unmapped[:10]),
            )
        known = (
            {str(c) for c in np.asarray(rate[s.table_currency_dim].values).tolist()}
            if has_cur
            else set()
        )
        cols = []
        for code in codes:
            if code == s.to:
                cols.append(np.ones(want.size))
            elif has_cur:
                if code not in known:
                    raise MapiError(
                        f"side table {s.table!r} has no rates for currency {code!r}",
                        code=ErrorCode.SIDE_TABLE_COVERAGE,
                    )
                sel = rate.sel({s.table_currency_dim: code}).transpose(tdim)
                cols.append(float_values(sel))
            else:
                cols.append(float_values(rate.transpose(tdim)))
        values = np.stack(cols, axis=0) if cols else np.zeros((0, want.size))
        return xr.DataArray(values, dims=[dim, s.along], coords={dim: levels, s.along: want})

    def _apply(
        self,
        ds: xr.Dataset,
        s: FxConvertSettings,
        ctx: StepContext,
        op: Callable[[xr.DataArray, xr.DataArray], xr.DataArray],
        units: str | None,
    ) -> xr.Dataset:
        names = present(ds, s.vars)
        if not names:
            return ds
        rates = self._rates(ds, s, ctx)
        out: dict[str, xr.DataArray] = {}
        for v in names:
            da = ds[v]
            lacking = [d for d in rates.dims if d not in da.dims]
            if lacking:
                raise schema_error(ctx.step_id, f"variable {v!r} lacks dims {lacking}")
            res = op(_as_float(da), rates).transpose(*da.dims)
            res.attrs = dict(da.attrs)
            if units is not None:
                res.attrs["units"] = units
            else:
                res.attrs.pop("units", None)
            out[v] = res
        return ds.assign(out)

    def transform(
        self, ds: xr.Dataset, s: FxConvertSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        return self._apply(ds, s, ctx, lambda a, r: a * r, s.to)

    def inverse(
        self, ds: xr.Dataset, s: FxConvertSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        return self._apply(ds, s, ctx, lambda a, r: a / r, None)


# ============================================================================ scaling


class ScaleSettings(StepSettings):
    vars: NameList = Field(..., min_length=1)
    over: NameList = Field(..., min_length=1, description="Dims the statistic reduces over")
    unseen_levels: UnseenPolicy = Field(
        "error", description="Unseen levels at score time: error, pooled (mean) or fill"
    )
    fill_value: float = Field(1.0, gt=0, description="Scale used for unseen levels with 'fill'")


class _ScaleStep(Step[ScaleSettings, StepState]):
    """Divide each variable by a positive statistic over ``over`` (labelled by the rest)."""

    kind = StepKind.VALUE
    Settings = ScaleSettings
    State = StepState
    invertible = True
    var_params = ("vars",)
    dim_params = ("over",)
    unseen_levels_param = "unseen_levels"
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars"],
        "updates": {"{*vars as v}": {"units": "1", "dtype": "float64"}},
    }
    statistic: ClassVar[str] = ""

    @staticmethod
    def _stat(da: xr.DataArray, dims: list[str]) -> xr.DataArray:
        raise NotImplementedError

    def fit(self, ds: xr.Dataset, s: ScaleSettings, ctx: StepContext) -> StepState:
        require_vars(ds, s.vars, ctx.step_id)
        arrays: dict[str, xr.DataArray] = {}
        for v in s.vars:
            dims = [d for d in s.over if d in ds[v].dims]
            if not dims:
                raise schema_error(ctx.step_id, f"variable {v!r} has none of the dims {s.over}")
            stat = self._stat(abs(_as_float(ds[v])), dims)
            stat = stat.where(np.isfinite(stat) & (stat > 0), 1.0)
            stat.attrs = {}
            arrays[f"scale__{v}"] = stat.drop_vars([c for c in stat.coords if c not in stat.dims])
        return StepState(arrays=xr.Dataset(arrays))

    def _scale(
        self, v: str, ds: xr.Dataset, s: ScaleSettings, st: StepState, ctx: StepContext
    ) -> xr.DataArray:
        return align(ctx, st.array(f"scale__{v}"), ds, s.unseen_levels, s.fill_value)

    def transform(
        self, ds: xr.Dataset, s: ScaleSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            res = (_as_float(ds[v]) / self._scale(v, ds, s, st, ctx)).transpose(*ds[v].dims)
            res.attrs = {**ds[v].attrs, "units": "1"}
            out[v] = res
        return ds.assign(out)

    def inverse(
        self, ds: xr.Dataset, s: ScaleSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            res = (_as_float(ds[v]) * self._scale(v, ds, s, st, ctx)).transpose(*ds[v].dims)
            res.attrs = {k: a for k, a in ds[v].attrs.items() if k != "units"}
            out[v] = res
        return ds.assign(out)


class MeanScaleStep(_ScaleStep):
    """Divide by the mean absolute value over ``over`` (the MMM "mean scaling")."""

    id = "core.mean_scale"
    version = "1.0.0"
    summary = "Divide by the mean absolute value over the named dims"
    summary_plain = "Puts each series on a comparable scale (average becomes 1)"

    @staticmethod
    def _stat(da: xr.DataArray, dims: list[str]) -> xr.DataArray:
        return da.mean(dims, skipna=True)


class MaxAbsScaleStep(_ScaleStep):
    """Divide by the maximum absolute value over ``over``."""

    id = "core.max_abs_scale"
    version = "1.0.0"
    summary = "Divide by the maximum absolute value over the named dims"
    summary_plain = "Puts each series between -1 and 1"

    @staticmethod
    def _stat(da: xr.DataArray, dims: list[str]) -> xr.DataArray:
        return da.max(dims, skipna=True)


class StandardizeSettings(StepSettings):
    vars: NameList = Field(..., min_length=1)
    over: NameList = Field(..., min_length=1, description="Dims the moments reduce over")
    with_mean: bool = True
    with_sd: bool = True
    ddof: int = Field(0, ge=0, le=1)
    unseen_levels: UnseenPolicy = "error"
    fill_mean: float = Field(0.0, description="Mean used for unseen levels with 'fill'")
    fill_sd: float = Field(1.0, gt=0, description="SD used for unseen levels with 'fill'")


class StandardizeStep(Step[StandardizeSettings, StepState]):
    """``(x - mean) / sd`` with moments over ``over`` (labelled by the remaining dims)."""

    id = "core.standardize"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = StandardizeSettings
    State = StepState
    invertible = True
    var_params = ("vars",)
    dim_params = ("over",)
    unseen_levels_param = "unseen_levels"
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars"],
        "updates": {"{*vars as v}": {"units": "1", "dtype": "float64"}},
    }
    summary = "Standardise to zero mean and unit variance over the named dims"
    summary_plain = "Centres each series and scales it to unit spread"

    def fit(self, ds: xr.Dataset, s: StandardizeSettings, ctx: StepContext) -> StepState:
        require_vars(ds, s.vars, ctx.step_id)
        arrays: dict[str, xr.DataArray] = {}
        for v in s.vars:
            dims = [d for d in s.over if d in ds[v].dims]
            if not dims:
                raise schema_error(ctx.step_id, f"variable {v!r} has none of the dims {s.over}")
            x = _as_float(ds[v])
            mean = x.mean(dims, skipna=True)
            if not s.with_mean:
                mean = xr.zeros_like(mean)
            sd = x.std(dims, skipna=True, ddof=s.ddof) if s.with_sd else xr.ones_like(mean)
            mean = mean.fillna(0.0)
            sd = sd.where(np.isfinite(sd) & (sd > 0), 1.0)
            for name, arr in (("mean", mean), ("sd", sd)):
                arr.attrs = {}
                arrays[f"{name}__{v}"] = arr.drop_vars([c for c in arr.coords if c not in arr.dims])
        return StepState(arrays=xr.Dataset(arrays))

    def _moments(
        self, v: str, ds: xr.Dataset, s: StandardizeSettings, st: StepState, ctx: StepContext
    ) -> tuple[xr.DataArray, xr.DataArray]:
        mean = align(ctx, st.array(f"mean__{v}"), ds, s.unseen_levels, s.fill_mean)
        sd = align(ctx, st.array(f"sd__{v}"), ds, s.unseen_levels, s.fill_sd)
        return mean, sd

    def transform(
        self, ds: xr.Dataset, s: StandardizeSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            mean, sd = self._moments(v, ds, s, st, ctx)
            res = ((_as_float(ds[v]) - mean) / sd).transpose(*ds[v].dims)
            res.attrs = {**ds[v].attrs, "units": "1"}
            out[v] = res
        return ds.assign(out)

    def inverse(
        self, ds: xr.Dataset, s: StandardizeSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            mean, sd = self._moments(v, ds, s, st, ctx)
            res = (_as_float(ds[v]) * sd + mean).transpose(*ds[v].dims)
            res.attrs = {k: a for k, a in ds[v].attrs.items() if k != "units"}
            out[v] = res
        return ds.assign(out)


# ===================================================================== pointwise transforms


class VarsSettings(StepSettings):
    vars: NameList = Field(..., min_length=1)


class Log1pStep(Step[VarsSettings, StepState]):
    """``log1p`` of ``vars``; values must be > -1. Non-linear: point outputs are retransformed."""

    id = "core.log1p"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = VarsSettings
    invertible = True
    linear = False
    var_params = ("vars",)
    effect_template: ClassVar[dict[str, JsonValue]] = _FLOAT_UPDATE
    summary = "log(1 + x) of the named variables"
    summary_plain = "Compresses large values with a logarithm"

    def transform(
        self, ds: xr.Dataset, s: VarsSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            x = _as_float(ds[v])
            if bool((x <= -1).any()):
                raise schema_error(ctx.step_id, f"log1p needs values > -1 in {v!r}")
            res = xr.apply_ufunc(np.log1p, x, keep_attrs=True)
            out[v] = res
        return ds.assign(out)

    def inverse(
        self, ds: xr.Dataset, s: VarsSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        return ds.assign(
            {
                v: xr.apply_ufunc(np.expm1, _as_float(ds[v]), keep_attrs=True)
                for v in present(ds, s.vars)
            }
        )


class LogitSettings(StepSettings):
    vars: NameList = Field(..., min_length=1)
    eps: float = Field(1e-6, gt=0, lt=0.5, description="Values are clipped to [eps, 1 - eps]")


class LogitStep(Step[LogitSettings, StepState]):
    """``log(p / (1 - p))`` of proportions in [0, 1] (clipped to ``[eps, 1 - eps]``)."""

    id = "core.logit"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = LogitSettings
    invertible = True
    linear = False
    var_params = ("vars",)
    effect_template: ClassVar[dict[str, JsonValue]] = _FLOAT_UPDATE
    summary = "Logit of proportions"
    summary_plain = "Maps shares between 0 and 1 onto an unbounded scale"

    def transform(
        self, ds: xr.Dataset, s: LogitSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            x = _as_float(ds[v])
            if bool(((x < 0) | (x > 1)).any()):
                raise schema_error(ctx.step_id, f"logit needs proportions in [0, 1] in {v!r}")
            p = x.clip(s.eps, 1 - s.eps)
            res = xr.apply_ufunc(_logit, p, keep_attrs=True)
            out[v] = res
        return ds.assign(out)

    def inverse(
        self, ds: xr.Dataset, s: LogitSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            x = _as_float(ds[v])
            out[v] = xr.apply_ufunc(_expit, x, keep_attrs=True)
        return ds.assign(out)


def _logit(p: np.ndarray) -> np.ndarray:
    return np.asarray(np.log(p) - np.log1p(-p))


def _expit(x: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):
        return np.asarray(np.where(x >= 0, 1 / (1 + np.exp(-x)), np.exp(x) / (1 + np.exp(x))))


# ============================================================================== impute


class ImputeSettings(StepSettings):
    vars: NameList = Field(..., min_length=1)
    method: Literal["mean", "median", "constant", "ffill"] = "mean"
    over: NameList = Field(default=[], description="mean/median: dims the statistic reduces over")
    value: float = Field(0.0, description="constant: the fill value")
    along: Name = Field("time", description="ffill: ordered dim to carry values along")
    within: NameList = Field(
        default=[], description="ffill: group dims (dense) or row group vars (long)"
    )
    limit: int | None = Field(
        None, ge=1, le=100_000, description="ffill: max consecutive missing values filled"
    )
    unseen_levels: UnseenPolicy = "error"
    fill_value: float = Field(0.0, description="mean/median: statistic for unseen levels")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.method in ("mean", "median") and not self.over:
            raise ValueError(f"method {self.method!r} needs 'over' (dims to reduce over)")
        return self


class ImputeState(StepState):
    carry_label: JsonValue = None
    """ffill: the last ``along`` label seen at fit (rows after it continue the fit series)."""


class ImputeStep(Step[ImputeSettings, ImputeState]):
    """Fill missing values: a statistic learned at fit, a constant, or forward fill.

    ``ffill`` carries the last observed value along ``along`` within groups. For data that
    continues after the fitted range (forecasts, holdout) the last fitted value per cell is
    carried over from the state, so replay is exact for any gap length (``limit`` counts
    the missing rows at the end of the fit data). Long layouts carry within the data only.
    """

    id = "core.impute"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = ImputeSettings
    State = ImputeState
    var_params = ("vars",)
    dim_params = ("over",)
    unseen_levels_param = "unseen_levels"
    effect_template: ClassVar[dict[str, JsonValue]] = _FLOAT_UPDATE
    summary = "Fill missing values (mean, median, constant or forward fill)"
    summary_plain = "Fills gaps in the data"

    def fit(self, ds: xr.Dataset, s: ImputeSettings, ctx: StepContext) -> ImputeState:
        require_vars(ds, s.vars, ctx.step_id)
        arrays: dict[str, xr.DataArray] = {}
        label: JsonValue = None
        if s.method in ("mean", "median"):
            for v in s.vars:
                dims = [d for d in s.over if d in ds[v].dims]
                x = _as_float(ds[v])
                stat = x.mean(dims, skipna=True) if s.method == "mean" else x.median(dims)
                stat = stat.fillna(s.fill_value)
                stat.attrs = {}
                arrays[f"value__{v}"] = stat.drop_vars(
                    [c for c in stat.coords if c not in stat.dims]
                )
        elif s.method == "ffill" and self._carries(ds, s):
            for v in s.vars:
                if s.along not in ds[v].dims:
                    raise schema_error(ctx.step_id, f"variable {v!r} has no dim {s.along!r}")
                last, gap = _last_valid(_as_float(ds[v]), s.along)
                arrays[f"carry__{v}"] = last
                arrays[f"gap__{v}"] = gap
            label = _label_json(ds[s.along].values.max()) if ds.sizes[s.along] else None
        return ImputeState(arrays=xr.Dataset(arrays) if arrays else None, carry_label=label)

    @staticmethod
    def _carries(ds: xr.Dataset, s: ImputeSettings) -> bool:
        return (
            s.along in ds.dims
            and s.along in ds.coords
            and ds[s.along].dtype.kind in "Mifu"
            and all(w in ds.dims for w in s.within)
        )

    def transform(
        self, ds: xr.Dataset, s: ImputeSettings, st: ImputeState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            x = _as_float(ds[v])
            match s.method:
                case "constant":
                    res = x.fillna(s.value)
                case "mean" | "median":
                    stat = align(ctx, st.array(f"value__{v}"), ds, s.unseen_levels, s.fill_value)
                    res = x.fillna(stat).transpose(*x.dims)
                case _:
                    res = self._ffill(x, ds, s, st, ctx, v)
            res.attrs = dict(ds[v].attrs)
            out[v] = res
        return ds.assign(out)

    def _ffill(
        self,
        x: xr.DataArray,
        ds: xr.Dataset,
        s: ImputeSettings,
        st: ImputeState,
        ctx: StepContext,
        v: str,
    ) -> xr.DataArray:
        if s.along not in x.dims:
            raise schema_error(ctx.step_id, f"variable {v!r} has no dim {s.along!r}")
        filled = apply_along(x, ds, s.along, s.within, lambda a: _ffill_last(a, s.limit))
        if st.carry_label is None or st.arrays is None or f"carry__{v}" not in st.arrays:
            return filled
        # new groups: 'error' raises, 'pooled' carries the mean last value, 'fill' fill_value
        carry = align(ctx, st.array(f"carry__{v}"), ds, s.unseen_levels, s.fill_value)
        coords = np.asarray(ds[s.along].values)
        after = xr.DataArray(
            coords > _label_value(st.carry_label, coords), dims=[s.along], coords={s.along: coords}
        )
        if not bool(after.any()):
            return filled
        # rows after the fitted range with no observation at or before them in this data
        # continue the fitted series: they take its last value (within ``limit``)
        ok = (x.notnull().cumsum(s.along) == 0) & after
        if s.limit is not None:
            gap = align(ctx, st.array(f"gap__{v}"), ds, "fill", 0.0)
            ok = ok & ((gap + after.cumsum(s.along)) <= s.limit)
        return filled.where(~ok, carry).transpose(*x.dims)


def _label_json(value: Any) -> JsonValue:
    if isinstance(value, np.datetime64):
        return str(np.datetime_as_string(value.astype("datetime64[ns]"), unit="ns"))
    item = value.item() if hasattr(value, "item") else value
    if isinstance(item, int | float):
        return item
    return str(item)


def _label_value(label: JsonValue, coords: np.ndarray) -> Any:
    if coords.dtype.kind == "M":
        return np.datetime64(str(label), "ns")
    return label


def _ffill_last(a: np.ndarray, limit: int | None) -> np.ndarray:
    n = a.shape[-1]
    idx = np.where(~np.isnan(a), np.arange(n), -1)
    idx = np.maximum.accumulate(idx, axis=-1)
    out = np.take_along_axis(a, np.clip(idx, 0, None), axis=-1)
    bad = idx < 0
    if limit is not None:
        bad |= (np.arange(n) - idx) > limit
    out[bad] = np.nan
    return out


def _last_valid(x: xr.DataArray, along: str) -> tuple[xr.DataArray, xr.DataArray]:
    """Last non-missing value along ``along`` per cell and the count of missing rows after it."""
    other = [d for d in x.dims if d != along]
    a = float_values(x.transpose(*other, along))
    n = a.shape[-1]
    valid = ~np.isnan(a)
    has = valid.any(axis=-1)
    last_idx = n - 1 - np.argmax(valid[..., ::-1], axis=-1)
    last = np.where(has, np.take_along_axis(a, last_idx[..., None], axis=-1)[..., 0], np.nan)
    gap = np.where(has, n - 1 - last_idx, n).astype(np.float64)
    coords = {d: x[d].values for d in other if d in x.coords}
    return (
        xr.DataArray(last, dims=other, coords=coords),
        xr.DataArray(gap, dims=other, coords=coords),
    )


# ===================================================================== sentinels and clip


class ReplaceSentinelSettings(StepSettings):
    vars: NameList = Field(..., min_length=1)
    sentinels: list[float] = Field(
        default=[], max_length=64, description="Exact codes that mean 'missing'"
    )
    below: float | None = Field(None, description="Values strictly below this are missing")
    above: float | None = Field(None, description="Values strictly above this are missing")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.sentinels and self.below is None and self.above is None:
            raise ValueError("give sentinels, below or above")
        return self


class ReplaceSentinelStep(Step[ReplaceSentinelSettings, StepState]):
    """Replace sentinel codes (e.g. ``-99``, negative codes) by NaN."""

    id = "core.replace_sentinel"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = ReplaceSentinelSettings
    var_params = ("vars",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars"],
        "updates": {"{*vars as v}": {"dtype": "float64", "missing": "allow"}},
    }
    summary = "Turn sentinel codes into missing values"
    summary_plain = "Marks placeholder numbers such as -99 as missing"

    def transform(
        self, ds: xr.Dataset, s: ReplaceSentinelSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            x = _as_float(ds[v])
            bad = x.isin(s.sentinels) if s.sentinels else xr.zeros_like(x, dtype=bool)
            if s.below is not None:
                bad = bad | (x < s.below)
            if s.above is not None:
                bad = bad | (x > s.above)
            res = x.where(~bad)
            res.attrs = dict(ds[v].attrs)
            out[v] = res
        return ds.assign(out)


class ClipSettings(StepSettings):
    vars: NameList = Field(..., min_length=1)
    lower: float | None = None
    upper: float | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.lower is None and self.upper is None:
            raise ValueError("give lower and/or upper")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("lower must be <= upper")
        return self


class ClipStep(Step[ClipSettings, StepState]):
    """Clip values to ``[lower, upper]`` (not invertible)."""

    id = "core.clip"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = ClipSettings
    var_params = ("vars",)
    effect_template: ClassVar[dict[str, JsonValue]] = _FLOAT_UPDATE
    summary = "Clip values to bounds"
    summary_plain = "Caps values that are too small or too large"

    def transform(
        self, ds: xr.Dataset, s: ClipSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            res = _as_float(ds[v]).clip(s.lower, s.upper)
            res.attrs = dict(ds[v].attrs)
            out[v] = res
        return ds.assign(out)
