"""Toy reference steps and models.

Small but real implementations of the contracts, used to test the runtime end to end
without ``mapi_steps`` or PyMC, and as worked examples for plugin authors:

Steps
    ``toy.fx`` (VALUE, side table), ``toy.resample`` (RESHAPE), ``toy.scale`` (VALUE,
    stateful, unseen-level policy), ``toy.log1p`` (VALUE, non-linear inverse),
    ``toy.lag`` (FEATURE, per-group context, dense and long layouts), ``toy.fourier``
    (FEATURE, stateful origin).

Models (panel models over ``group_dim × time_dim``)
    ``toy.linear`` (point; ridge least squares with group intercepts and contributions),
    ``toy.bayes_linear`` (draws; conjugate normal-inverse-gamma posterior, log likelihood),
    ``toy.quantile`` (quantiles; least squares plus empirical residual quantiles).

:func:`toy_registry` returns a registry with all of them and :func:`panel_dataset` makes a
daily E33-like dataset (markets × channels × days) with a known linear signal.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Literal, cast

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.effects import StepKind
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, JsonValue

from mapi_sdk._util import py_scalar
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
    new_level_mask,
)
from mapi_sdk.registry import Registry
from mapi_sdk.steps import Step, StepContext, StepSettings, StepState, UnseenPolicy

__all__ = [
    "BayesLinearModel",
    "FourierStep",
    "FxStep",
    "LagStep",
    "LinearModel",
    "Log1pStep",
    "QuantileModel",
    "ResampleStep",
    "ScaleStep",
    "panel_dataset",
    "toy_registry",
]

# ============================================================================ steps


def _with_units(res: xr.DataArray, src: xr.DataArray, units: str | None) -> xr.DataArray:
    """``res`` with ``src``'s attrs and ``units`` (dropped when None: the pipeline restores
    the stage's units on inverted outputs)."""
    res.attrs = {k: a for k, a in src.attrs.items() if k != "units"}
    if units is not None:
        res.attrs["units"] = units
    return res


class FxSettings(StepSettings):
    vars: list[str]
    table: str
    to: str = Field("EUR", pattern=r"^[A-Z]{3}$")
    along: str = "time"
    table_along: str | None = None
    rate_var: str = "rate"


class FxStep(Step[FxSettings, StepState]):
    """Multiply ``vars`` by a per-period exchange rate from a side table."""

    id = "toy.fx"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = FxSettings
    invertible = True
    var_params = ("vars",)
    side_table_params = ("table",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars", "to"],
        "updates": {"{*vars as v}": {"units": "{to}"}},
    }

    def _rates(self, ds: xr.Dataset, s: FxSettings, ctx: StepContext) -> xr.DataArray:
        table = ctx.side_table(s.table)
        rates = table[s.rate_var]
        tdim = s.table_along or s.along
        if tdim != s.along:
            rates = rates.rename({tdim: s.along})
        want = ds[s.along].values
        missing = want[~np.isin(want, rates[s.along].values)]
        if missing.size:
            raise MapiError(
                f"side table {s.table!r} does not cover {missing.size} {s.along!r} label(s)",
                code=ErrorCode.SIDE_TABLE_COVERAGE,
                untrusted_text=", ".join(str(py_scalar(v)) for v in missing[:5]),
            )
        return rates.sel({s.along: want})

    def transform(
        self, ds: xr.Dataset, s: FxSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        present = [v for v in s.vars if v in ds]
        if not present:
            return ds
        rates = self._rates(ds, s, ctx)
        return ds.assign({v: _with_units(ds[v] * rates, ds[v], s.to) for v in present})

    def inverse(self, ds: xr.Dataset, s: FxSettings, st: StepState, ctx: StepContext) -> xr.Dataset:
        present = [v for v in s.vars if v in ds]
        if not present:
            return ds
        rates = self._rates(ds, s, ctx)
        return ds.assign({v: _with_units(ds[v] / rates, ds[v], None) for v in present})


class ResampleSettings(StepSettings):
    dim: str = "time"
    freq: str = "W-SUN"
    how: Literal["sum", "mean"] = "sum"
    full_periods_only: bool = True


class ResampleStep(Step[ResampleSettings, StepState]):
    """Aggregate a time dim to a coarser frequency (drops partial periods by default)."""

    id = "toy.resample"
    version = "1.0.0"
    kind = StepKind.RESHAPE
    Settings = ResampleSettings
    dim_params = ("dim",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["dim", "freq"],
        "transforms_dims": {"{dim}": {"kind": "time", "policy": "extend", "freq": "{freq}"}},
    }

    def transform(
        self, ds: xr.Dataset, s: ResampleSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        users = [v for v in ds.data_vars if s.dim in ds[v].dims]
        static = ds.drop_dims(s.dim)
        # coordinate-only frames (e.g. forecast horizons) still need resampled labels
        body = (
            ds[users] if users else xr.Dataset({"__ones__": xr.ones_like(ds[s.dim], dtype=float)})
        )
        res = body.resample({s.dim: s.freq})
        agg = res.sum(skipna=False) if s.how == "sum" else res.mean(skipna=False)
        if s.full_periods_only:
            counts = xr.ones_like(ds[s.dim], dtype=int).resample({s.dim: s.freq}).sum()
            full = counts == counts.max()
            agg = agg.isel({s.dim: np.flatnonzero(full.values)})
        for v in users:
            agg[v].attrs = ds[v].attrs
        if not users:
            agg = agg.drop_vars("__ones__")
        return xr.merge([agg, static], combine_attrs="override")


class ScaleSettings(StepSettings):
    vars: list[str]
    over: list[str] = Field(default=[])
    unseen_levels: UnseenPolicy = "error"
    fill: float = 1.0


class ScaleState(StepState):
    pass


class ScaleStep(Step[ScaleSettings, ScaleState]):
    """Divide ``vars`` by their mean absolute value over ``over`` (labelled by the rest)."""

    id = "toy.scale"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = ScaleSettings
    State = ScaleState
    invertible = True
    var_params = ("vars",)
    dim_params = ("over",)
    unseen_levels_param = "unseen_levels"
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars"],
        "updates": {"{*vars as v}": {"units": "1"}},
    }

    def fit(self, ds: xr.Dataset, s: ScaleSettings, ctx: StepContext) -> ScaleState:
        arrays = {}
        for v in s.vars:
            if v not in ds:
                raise MapiError(
                    f"toy.scale: variable {v!r} is missing", code=ErrorCode.SCHEMA_INVALID
                )
            over = [d for d in s.over if d in ds[v].dims]
            scale = abs(ds[v]).mean(over, skipna=True) if over else abs(ds[v])
            arrays[f"scale__{v}"] = scale.where(scale > 0, 1.0).fillna(1.0)
        return ScaleState(arrays=xr.Dataset(arrays))

    def _scale(
        self, v: str, ds: xr.Dataset, s: ScaleSettings, st: ScaleState, ctx: StepContext
    ) -> xr.DataArray:
        return ctx.align_state(st.array(f"scale__{v}"), ds, s.unseen_levels, fill_value=s.fill)

    def transform(
        self, ds: xr.Dataset, s: ScaleSettings, st: ScaleState, ctx: StepContext
    ) -> xr.Dataset:
        return ds.assign(
            {
                v: _with_units(ds[v] / self._scale(v, ds, s, st, ctx), ds[v], "1")
                for v in s.vars
                if v in ds
            }
        )

    def inverse(
        self, ds: xr.Dataset, s: ScaleSettings, st: ScaleState, ctx: StepContext
    ) -> xr.Dataset:
        return ds.assign(
            {
                v: _with_units(ds[v] * self._scale(v, ds, s, st, ctx), ds[v], None)
                for v in s.vars
                if v in ds
            }
        )


class VarsSettings(StepSettings):
    vars: list[str]


class Log1pStep(Step[VarsSettings, StepState]):
    """``log1p`` of ``vars`` (non-linear inverse: point outputs need retransformation)."""

    id = "toy.log1p"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = VarsSettings
    invertible = True
    linear = False
    var_params = ("vars",)
    effect_template: ClassVar[dict[str, JsonValue]] = {}

    def transform(
        self, ds: xr.Dataset, s: VarsSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        return ds.assign({v: np.log1p(ds[v]) for v in s.vars if v in ds})

    def inverse(
        self, ds: xr.Dataset, s: VarsSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        return ds.assign({v: np.expm1(ds[v]) for v in s.vars if v in ds})


class LagSettings(StepSettings):
    vars: list[str]
    along: str = "time"
    within: list[str] = Field(default=[])
    lags: list[int] = Field(default=[1], min_length=1)
    fill: float | None = None


class LagStep(Step[LagSettings, StepState]):
    """Lagged copies of ``vars`` along ``along`` within groups, as a new ``lag`` dim."""

    id = "toy.lag"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = LagSettings
    depends_on_target = True
    target_vars_param = "vars"
    var_params = ("vars",)
    dim_params = ("along",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars"],
        "transforms_dims": {"lag": {"kind": "integer", "policy": "fixed"}},
        "derives": {
            "{*vars as v}_lag": {"from": "{v}", "append_dims": ["lag"], "missing": "allow"}
        },
    }
    context_template: ClassVar[dict[str, JsonValue]] = {
        "along": "{along}",
        "n": "{max:lags}",
        "within": "{within}",
        "min_lag": "{min:lags}",
    }

    def touched_vars(self, s: LagSettings) -> set[str]:
        return set()  # adds lagged copies; the source variables are unchanged

    def transform(
        self, ds: xr.Dataset, s: LagSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        long = bool(s.within) and not all(w in ds.dims for w in s.within)
        for v in s.vars:
            if v not in ds:
                continue
            da = ds[v]
            if long:
                shifted = [self._group_shift(ds, da, s, k) for k in s.lags]
            else:
                shifted = [da.shift({s.along: k}) for k in s.lags]
            lagged = xr.concat(shifted, dim=pd.Index(s.lags, name="lag"))
            if s.fill is not None:
                lagged = lagged.fillna(s.fill)
            out[f"{v}_lag"] = lagged.transpose(*da.dims, "lag")
        return ds.assign(out)

    @staticmethod
    def _group_shift(ds: xr.Dataset, da: xr.DataArray, s: LagSettings, k: int) -> xr.DataArray:
        """Shift rows by ``k`` within groups (long layout: ``along`` is the row dim)."""
        keys = pd.MultiIndex.from_arrays([np.asarray(ds[w].values) for w in s.within])
        codes = pd.factorize(keys)[0]
        rows = pd.Series(np.arange(len(codes), dtype=float))
        src = rows.groupby(codes).shift(k).to_numpy()
        ok = ~np.isnan(src)
        axis = da.dims.index(s.along)
        vals = np.full(da.shape, np.nan)
        target = [slice(None)] * da.ndim
        target[axis] = np.flatnonzero(ok)  # type: ignore[call-overload]
        vals[tuple(target)] = np.take(da.values.astype(float), src[ok].astype(int), axis=axis)
        return xr.DataArray(vals, dims=da.dims, coords=da.coords)


class FourierSettings(StepSettings):
    along: str = "time"
    period: float = Field(52.18, gt=0)
    order: int = Field(2, ge=1, le=20)
    out: str = "fourier"


class FourierState(StepState):
    origin: str = ""
    step_ns: int = 1


class FourierStep(Step[FourierSettings, FourierState]):
    """Seasonal Fourier features; the time origin and step are learned at fit."""

    id = "toy.fourier"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = FourierSettings
    State = FourierState
    dim_params = ("along",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["out", "along"],
        "transforms_dims": {"fourier_mode": {"kind": "categorical", "policy": "fixed"}},
        "adds": {"{out}": {"dims": ["{along}", "fourier_mode"], "dtype": "float64"}},
    }

    def fit(self, ds: xr.Dataset, s: FourierSettings, ctx: StepContext) -> FourierState:
        t = pd.DatetimeIndex(ds[s.along].values)
        step = int(np.median(np.diff(t.asi8))) if len(t) > 1 else 86_400 * 10**9
        return FourierState(origin=t[0].isoformat(), step_ns=step)

    def transform(
        self, ds: xr.Dataset, s: FourierSettings, st: FourierState, ctx: StepContext
    ) -> xr.Dataset:
        t = pd.DatetimeIndex(ds[s.along].values)
        pos = (t.asi8 - pd.Timestamp(st.origin).value) / st.step_ns
        modes, cols = [], []
        for k in range(1, s.order + 1):
            ang = 2 * np.pi * k * pos / s.period
            modes += [f"sin_{k}", f"cos_{k}"]
            cols += [np.sin(ang), np.cos(ang)]
        feats = xr.DataArray(
            np.stack(cols, axis=-1),
            dims=[s.along, "fourier_mode"],
            coords={s.along: ds[s.along].values, "fourier_mode": modes},
        )
        return ds.assign({s.out: feats})


# ============================================================================ models


def _learned_columns(fitted: Fitted) -> list[dict[str, Any]]:
    cols = fitted.learned.get("columns")
    if not isinstance(cols, list):
        raise MapiError("fitted model has no feature columns", code=ErrorCode.INTEGRITY_FAILURE)
    return cast(list[dict[str, Any]], cols)


class PanelSettings(ModelSettings):
    target: str = "y"
    group_dim: str = "geo"
    time_dim: str = "time"
    features: list[str] | None = None
    ridge: float = Field(1e-6, ge=0)
    retransform: Literal["median", "smearing"] = "median"
    quantiles: list[float] = Field(default=[0.05, 0.1, 0.5, 0.9, 0.95])


_PANEL_TEMPLATE: dict[str, Any] = {
    "params": {
        "target": {"type": "string", "default": "y"},
        "group_dim": {"type": "string", "default": "geo"},
        "time_dim": {"type": "string", "default": "time"},
    },
    "template": {
        "dims": {
            "{group_dim}": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
            },
            "{time_dim}": {"kind": "time", "policy": "extend"},
        },
        "vars": {
            "{target}": {
                "dims": ["{group_dim}", "{time_dim}"],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
            }
        },
    },
}


def _columns(ds: xr.Dataset, s: PanelSettings) -> list[dict[str, Any]]:
    """Feature columns: every numeric non-target variable, flattened over extra dims."""
    names = (
        s.features
        if s.features is not None
        else [str(v) for v in ds.data_vars if v != s.target and ds[v].dtype.kind in "fiub"]
    )
    cols: list[dict[str, Any]] = []
    panel = {s.group_dim, s.time_dim}
    for name in names:
        if name not in ds:
            raise MapiError(f"feature {name!r} is missing", code=ErrorCode.SCHEMA_INVALID)
        extra = [str(d) for d in ds[name].dims if d not in panel]
        if not set(ds[name].dims) & panel:
            continue
        if not extra:
            cols.append({"var": name, "sel": {}})
            continue
        grid = pd.MultiIndex.from_product([ds[d].values for d in extra], names=extra)
        for combo in grid:
            vals = combo if isinstance(combo, tuple) else (combo,)
            cols.append(
                {"var": name, "sel": {d: py_scalar(v) for d, v in zip(extra, vals, strict=True)}}
            )
    return cols


def _design(ds: xr.Dataset, cols: Sequence[dict[str, Any]], s: PanelSettings) -> np.ndarray:
    frame = xr.DataArray(
        np.zeros((ds.sizes[s.group_dim], ds.sizes[s.time_dim])),
        dims=[s.group_dim, s.time_dim],
        coords={s.group_dim: ds[s.group_dim].values, s.time_dim: ds[s.time_dim].values},
    )
    out = np.zeros((*frame.shape, len(cols)))
    for j, c in enumerate(cols):
        if c["var"] not in ds:
            raise MapiError(
                f"input variable {c['var']!r} is missing at score time",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
            )
        da = ds[c["var"]].sel(c["sel"]) if c["sel"] else ds[c["var"]]
        out[..., j] = da.broadcast_like(frame).transpose(*frame.dims).values
    return np.nan_to_num(out, nan=0.0)


def _align_groups(
    ds: xr.Dataset, alpha: np.ndarray, levels: np.ndarray, s: PanelSettings
) -> tuple[np.ndarray, np.ndarray]:
    """Gather ``alpha[..., group]`` for the score groups; also return the new-level mask.

    New levels get ``NaN`` here; callers fill them according to the dim's strategy.
    """
    groups = ds[s.group_dim].values
    is_new = np.asarray(new_level_mask(ds, s.group_dim).values, dtype=bool)
    is_new |= ~np.isin(groups, levels)
    lookup = {lvl: i for i, lvl in enumerate(levels.tolist())}
    idx = np.array([lookup.get(g, 0) for g in groups.tolist()], dtype=int)
    out = np.take(alpha, idx, axis=-1).astype(float)
    out[..., is_new] = np.nan
    return out, is_new


def _alpha_for(
    ds: xr.Dataset, alpha: xr.DataArray, s: PanelSettings, ctx: ScoreContext
) -> xr.DataArray:
    """Point intercepts for the score groups; new levels pooled (mean) or zero."""
    vals, is_new = _align_groups(ds, alpha.values, alpha[s.group_dim].values, s)
    vals[is_new] = 0.0 if ctx.strategy(s.group_dim) == "zero" else float(alpha.mean())
    return xr.DataArray(vals, dims=[s.group_dim], coords={s.group_dim: ds[s.group_dim].values})


class _PanelModel(CustomModel[PanelSettings]):
    Settings = PanelSettings
    schema_template = _PANEL_TEMPLATE

    def _fit_ls(
        self, ds: xr.Dataset, s: PanelSettings
    ) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]], np.ndarray, np.ndarray]:
        cols = _columns(ds, s)
        x = _design(ds, cols, s)
        y = ds[s.target].transpose(s.group_dim, s.time_dim).values.astype(float)
        g, t, p = x.shape
        onehot = np.broadcast_to(np.eye(g)[:, None, :], (g, t, g))
        full = np.concatenate([onehot, x], axis=-1).reshape(g * t, g + p)
        yy = y.reshape(-1)
        ok = np.isfinite(yy)
        a = full[ok]
        coef = np.linalg.solve(a.T @ a + s.ridge * np.eye(g + p), a.T @ yy[ok])
        return coef, full, cols, yy, ok


class LinearModel(_PanelModel):
    """Ridge least squares with group intercepts (point predictions + contributions)."""

    id = "toy.linear"
    version = "1.0.0"
    capabilities = Capabilities(
        inference="point",
        representation=Representation.POINT,
        score_modes=frozenset({"predict", "forecast", "counterfactual", "evaluate"}),
        contribution_scale="additive",
    )
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {"dims": ["geo", "time"], "units_of": "y"},
        "contributions": {"dims": ["component", "geo", "time"], "units_of": "y"},
    }
    pad_extend = False

    def fit(self, ds: xr.Dataset, s: PanelSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        coef, full, cols, yy, ok = self._fit_ls(ds, s)
        g = ds.sizes[s.group_dim]
        resid = yy[ok] - full[ok] @ coef
        params = xr.Dataset(
            {
                "alpha": ((s.group_dim,), coef[:g]),
                "beta": (("feature",), coef[g:]),
                "sigma": ((), float(np.std(resid)) if resid.size else 0.0),
            },
            coords={s.group_dim: ds[s.group_dim].values, "feature": np.arange(len(cols))},
        )
        return FitOutput(
            groups={"params": params}, learned={"columns": cast(list[JsonValue], cols)}
        )

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        params = fitted.group("params")
        cols = _learned_columns(fitted)
        x = _design(ds, cols, s)
        alpha = _alpha_for(ds, params["alpha"], s, ctx)
        contrib_x = x * params["beta"].values
        dims = [s.group_dim, s.time_dim]
        coords = {s.group_dim: ds[s.group_dim].values, s.time_dim: ds[s.time_dim].values}
        intercept = np.broadcast_to(alpha.values[:, None], x.shape[:2])
        comps = [intercept] + [contrib_x[..., j] for j in range(x.shape[-1])]
        names = ["intercept"] + [
            c["var"] + "".join(f"[{v}]" for v in c["sel"].values()) for c in cols
        ]
        contributions = xr.DataArray(
            np.stack(comps), dims=["component", *dims], coords={**coords, "component": names}
        )
        y_hat = contributions.sum("component")
        pred = xr.Dataset({"y_hat": y_hat, "contributions": contributions})
        return xr.DataTree.from_dict({PREDICTIONS: pred})


class BayesLinearModel(_PanelModel):
    """Conjugate Bayesian linear regression (normal-inverse-gamma) giving joint draws."""

    id = "toy.bayes_linear"
    version = "1.0.0"
    capabilities = Capabilities(
        inference="custom",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "counterfactual", "evaluate", "simulate"}),
        contribution_scale="additive",
    )
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": ["geo", "time"], "units_of": "y"}}

    def fit(self, ds: xr.Dataset, s: PanelSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        cols = _columns(ds, s)
        x = _design(ds, cols, s)
        y = ds[s.target].transpose(s.group_dim, s.time_dim).values.astype(float)
        g, t, p = x.shape
        onehot = np.broadcast_to(np.eye(g)[:, None, :], (g, t, g))
        full = np.concatenate([onehot, x], axis=-1).reshape(g * t, g + p)
        yy = y.reshape(-1)
        ok = np.isfinite(yy)
        a, b = full[ok], yy[ok]
        k = g + p
        prec0 = 1e-2
        vn = np.linalg.inv(a.T @ a + prec0 * np.eye(k))
        mn = vn @ a.T @ b
        an = 1.0 + ok.sum() / 2
        bn = 1.0 + 0.5 * float(b @ b - mn @ np.linalg.solve(vn, mn))
        chains, draws = cfg.chains or 2, cfg.draws or 200
        rng = np.random.default_rng(cfg.seed if cfg.seed is not None else ctx.seed)
        sigma2 = 1.0 / rng.gamma(an, 1.0 / max(bn, 1e-12), size=(chains, draws))
        chol = np.linalg.cholesky(vn + 1e-12 * np.eye(k))
        z = rng.standard_normal((chains, draws, k))
        coef = mn + np.sqrt(sigma2)[..., None] * (z @ chol.T)
        sigma = np.sqrt(sigma2)
        mu = coef @ full.T  # (chain, draw, cells)
        resid = yy[None, None, ok] - mu[..., ok]
        ll = -0.5 * np.log(2 * np.pi * sigma2)[..., None] - 0.5 * resid**2 / sigma2[..., None]
        cd = {"chain": np.arange(chains), "draw": np.arange(draws)}
        posterior = xr.Dataset(
            {
                "alpha": (("chain", "draw", s.group_dim), coef[..., :g]),
                "beta": (("chain", "draw", "feature"), coef[..., g:]),
                "sigma": (("chain", "draw"), sigma),
            },
            coords={**cd, s.group_dim: ds[s.group_dim].values, "feature": np.arange(len(cols))},
        )
        log_lik = xr.Dataset({s.target: (("chain", "draw", "obs"), ll)}, coords=cd)
        observed = xr.Dataset({s.target: (("obs",), yy[ok])})
        return FitOutput(
            groups={"posterior": posterior, "log_likelihood": log_lik, "observed_data": observed},
            learned={"columns": cast(list[JsonValue], cols)},
        )

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        post = fitted.posterior
        cols = _learned_columns(fitted)
        x = _design(ds, cols, s)
        dims = [s.group_dim, s.time_dim]
        coords = {s.group_dim: ds[s.group_dim].values, s.time_dim: ds[s.time_dim].values}
        alpha = post["alpha"].transpose("chain", "draw", s.group_dim)
        a_known, is_new = _align_groups(ds, alpha.values, alpha[s.group_dim].values, s)
        strategy = ctx.strategy(s.group_dim)
        if is_new.any():
            n_new = int(is_new.sum())
            mean_a = alpha.mean(s.group_dim).values[..., None]
            sd_a = alpha.std(s.group_dim).values[..., None]
            if strategy == "hyperprior":
                rng = np.random.default_rng(ctx.seed)
                eps = rng.standard_normal((*mean_a.shape[:2], n_new))
                a_known[..., is_new] = mean_a + sd_a * eps
            elif strategy == "zero":
                a_known[..., is_new] = 0.0
            else:
                a_known[..., is_new] = np.broadcast_to(mean_a, (*mean_a.shape[:2], n_new))
        beta = post["beta"].transpose("chain", "draw", "feature").values
        mu = a_known[..., :, None] + np.einsum("gtp,cdp->cdgt", x, beta)
        if ctx.kind == "predictive":
            rng = np.random.default_rng(ctx.seed + 1)
            sigma = post["sigma"].values[..., None, None]
            mu = mu + sigma * rng.standard_normal(mu.shape)
        y_hat = xr.DataArray(
            mu,
            dims=["chain", "draw", *dims],
            coords={"chain": post["chain"].values, "draw": post["draw"].values, **coords},
        )
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": y_hat})})


class QuantileModel(_PanelModel):
    """Least squares plus empirical residual quantiles (marginal per cell)."""

    id = "toy.quantile"
    version = "1.0.0"
    capabilities = Capabilities(
        inference="point",
        representation=Representation.QUANTILES,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
    )
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": ["geo", "time"], "units_of": "y"}}
    pad_extend = False

    def fit(self, ds: xr.Dataset, s: PanelSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        coef, full, cols, yy, ok = self._fit_ls(ds, s)
        g = ds.sizes[s.group_dim]
        resid = yy[ok] - full[ok] @ coef
        qs = np.quantile(resid, s.quantiles) if resid.size else np.zeros(len(s.quantiles))
        params = xr.Dataset(
            {
                "alpha": ((s.group_dim,), coef[:g]),
                "beta": (("feature",), coef[g:]),
                "offset": (("quantile",), qs),
            },
            coords={
                s.group_dim: ds[s.group_dim].values,
                "feature": np.arange(len(cols)),
                "quantile": s.quantiles,
            },
        )
        return FitOutput(
            groups={"params": params}, learned={"columns": cast(list[JsonValue], cols)}
        )

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        params = fitted.group("params")
        cols = _learned_columns(fitted)
        x = _design(ds, cols, s)
        alpha = _alpha_for(ds, params["alpha"], s, ctx)
        point = alpha.values[:, None] + x @ params["beta"].values
        dims = [s.group_dim, s.time_dim]
        coords = {s.group_dim: ds[s.group_dim].values, s.time_dim: ds[s.time_dim].values}
        q = params["offset"].values[:, None, None] + point[None]
        y_hat = xr.DataArray(
            q, dims=["quantile", *dims], coords={**coords, "quantile": params["quantile"].values}
        )
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": y_hat})})


# ============================================================================ helpers


def toy_registry() -> Registry:
    """A registry with every toy step and model."""
    reg = Registry()
    reg.register(
        [
            FxStep,
            ResampleStep,
            ScaleStep,
            Log1pStep,
            LagStep,
            FourierStep,
            LinearModel,
            BayesLinearModel,
            QuantileModel,
        ]
    )
    return reg


def panel_dataset(
    *,
    markets: Sequence[str] = ("NL", "DE", "FR"),
    channels: Sequence[str] = ("tv", "search"),
    days: int = 7 * 30,
    start: str = "2024-01-01",
    seed: int = 0,
    noise: float = 0.05,
) -> tuple[xr.Dataset, xr.Dataset]:
    """A daily E33-like dataset and its FX side table.

    Returns ``(data, fx)`` where ``data`` has ``revenue_local(market, date)`` and
    ``spend_local(market, channel, date)`` in local currency, and ``fx`` has
    ``rate(date)`` covering the data plus a year of future dates. Starts on a Monday so
    weekly (W-SUN) resampling yields full weeks.
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=days, freq="D").as_unit("ns")
    m, c = len(markets), len(channels)
    spend = rng.gamma(2.0, 50.0, size=(m, c, days))
    beta = np.linspace(0.8, 1.6, c)
    base = rng.uniform(200, 400, size=m)
    revenue = base[:, None] + np.einsum("mcd,c->md", spend, beta)
    revenue *= 1 + noise * rng.standard_normal(revenue.shape)
    data = xr.Dataset(
        {
            "revenue_local": (("market", "date"), revenue, {"units": "LCU"}),
            "spend_local": (("market", "channel", "date"), spend, {"units": "LCU"}),
        },
        coords={"market": list(markets), "channel": list(channels), "date": dates},
    )
    fx_dates = pd.date_range(start, periods=days + 400, freq="D").as_unit("ns")
    fx = xr.Dataset(
        {"rate": (("date",), 0.9 + 0.05 * np.sin(np.arange(len(fx_dates)) / 30.0))},
        coords={"date": fx_dates},
    )
    return data, fx
