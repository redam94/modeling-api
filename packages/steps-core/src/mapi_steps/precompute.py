"""Precompute steps for point models: carry-over (``adstock``) and diminishing returns
(``saturation``) with fixed, user-given parameters.

Bayesian models learn these transforms inside the model; point models (sklearn, ridge) get
them as preprocessing. The formulas follow pymc-marketing's conventions (implemented here in
numpy; pymc-marketing is not a dependency):

adstock (weights over lags ``l = 0 .. l_max-1``, optional ``normalize`` to sum 1)
    ``geometric``  ``w_l = alpha ** l``
    ``delayed``    ``w_l = alpha ** ((l - theta) ** 2)``
    ``weibull_pdf`` Weibull(k, lam) density at ``t = l + 1``, min-max scaled to [0, 1]
    ``weibull_cdf`` ``w_0 = 1``, ``w_l = prod_{t=1..l} exp(-(t / lam) ** k)``
saturation
    ``logistic``          ``(1 - exp(-lam x)) / (1 + exp(-lam x))``
    ``hill``              ``x ** slope / (half ** slope + x ** slope)``
    ``michaelis_menten``  ``alpha x / (lam + x)``
    ``tanh``              ``alpha tanh(x / (alpha c))``

Every parameter is a number or a mapping from the levels of the ``by`` dim (e.g. channel)
to numbers.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from typing import Any, ClassVar, Literal, Self, cast

import numpy as np
import xarray as xr
from mapi_sdk.steps import Step, StepContext, StepSettings, StepState
from mapi_spec.effects import StepKind
from pydantic import Field, JsonValue, model_validator

from mapi_steps._common import (
    Name,
    NameList,
    apply_along,
    first_valid,
    level_param,
    present,
    schema_error,
    shift_last,
)

__all__ = [
    "AdstockStep",
    "SaturationStep",
    "adstock_weights",
    "saturate",
]

Param = float | dict[str, float]


def _values(p: Param | None) -> list[float]:
    if p is None:
        return []
    return list(p.values()) if isinstance(p, Mapping) else [float(p)]


def _check(name: str, p: Param | None, lo: float, hi: float | None, *, lo_open: bool) -> None:
    for v in _values(p):
        bad = (v <= lo if lo_open else v < lo) or (hi is not None and v >= hi)
        if bad or not np.isfinite(v):
            rng = f"({lo}, {hi})" if lo_open else f"[{lo}, {hi})"
            raise ValueError(f"{name} must lie in {rng.replace('None', 'inf')}, got {v}")


# ============================================================================== adstock

AdstockKind = Literal["geometric", "delayed", "weibull_pdf", "weibull_cdf"]


def adstock_weights(
    kind: AdstockKind,
    l_max: int,
    *,
    alpha: np.ndarray | float | None = None,
    theta: np.ndarray | float | None = None,
    lam: np.ndarray | float | None = None,
    k: np.ndarray | float | None = None,
    normalize: bool = False,
) -> np.ndarray:
    """Carry-over weights with a trailing lag axis; parameters broadcast (e.g. per channel)."""
    lags = np.arange(l_max, dtype=np.float64)

    def p(x: np.ndarray | float | None) -> np.ndarray:
        assert x is not None
        return np.asarray(x, dtype=np.float64)[..., None]

    match kind:
        case "geometric":
            w = p(alpha) ** lags
        case "delayed":
            w = p(alpha) ** ((lags - p(theta)) ** 2)
        case "weibull_pdf":
            t = lags + 1
            kk, ll = p(k), p(lam)
            pdf = (kk / ll) * (t / ll) ** (kk - 1) * np.exp(-((t / ll) ** kk))
            lo, hi = pdf.min(axis=-1, keepdims=True), pdf.max(axis=-1, keepdims=True)
            span = np.where(hi > lo, hi - lo, 1.0)
            w = np.where(hi > lo, (pdf - lo) / span, 1.0)
        case _:
            t = lags[1:]
            surv = np.exp(-((t / p(lam)) ** p(k)))
            ones = np.ones((*surv.shape[:-1], 1))
            w = np.cumprod(np.concatenate([ones, surv], axis=-1), axis=-1)
    if normalize:
        total = w.sum(axis=-1, keepdims=True)
        w = w / np.where(total > 0, total, 1.0)
    return np.asarray(w, dtype=np.float64)


class AdstockSettings(StepSettings):
    vars: NameList = Field(..., min_length=1)
    along: Name = "time"
    within: NameList = Field(default_factory=list)
    kind: AdstockKind = "geometric"
    l_max: int = Field(12, ge=1, le=520)
    alpha: Param | None = Field(None, description="geometric/delayed decay in [0, 1)")
    theta: Param | None = Field(None, description="delayed: lag of the peak effect (>= 0)")
    lam: Param | None = Field(None, description="weibull: scale (> 0)")
    k: Param | None = Field(None, description="weibull: shape (> 0)")
    by: Name | None = Field(None, description="Dim whose levels key per-level parameters")
    normalize: bool = Field(False, description="Scale the weights to sum to 1")
    mask: Name | None = None

    @model_validator(mode="after")
    def _params(self) -> Self:
        need = {
            "geometric": ("alpha",),
            "delayed": ("alpha", "theta"),
            "weibull_pdf": ("lam", "k"),
            "weibull_cdf": ("lam", "k"),
        }[self.kind]
        missing = [n for n in need if getattr(self, n) is None]
        if missing:
            raise ValueError(f"adstock kind {self.kind!r} needs {missing}")
        _check("alpha", self.alpha, 0.0, 1.0, lo_open=False)
        _check("theta", self.theta, 0.0, None, lo_open=False)
        _check("lam", self.lam, 0.0, None, lo_open=True)
        _check("k", self.k, 0.0, None, lo_open=True)
        dict_params = [
            n for n in ("alpha", "theta", "lam", "k") if isinstance(getattr(self, n), dict)
        ]
        if dict_params and self.by is None:
            raise ValueError(f"per-level {dict_params} need 'by'")
        return self


def _carry_shift(a: np.ndarray, *, k: int) -> np.ndarray:
    return shift_last(_zero_leading(a), k, 0.0)


def _zero_leading(a: np.ndarray) -> np.ndarray:
    """Missing rows before a series' first observation are 'no history' (zero)."""
    fv = first_valid(a)[..., None]
    return np.where(np.arange(a.shape[-1]) < fv, 0.0, a)


class AdstockStep(Step[AdstockSettings, StepState]):
    """Carry-over ``y_t = sum_l w_l x_{t-l}`` along ``along`` within groups (in place).

    History before a series starts counts as zero; at score time the per-group context
    supplies the last ``l_max`` fitted rows so forecasts continue the carry-over exactly.
    """

    id = "core.adstock"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = AdstockSettings
    var_params = ("vars", "mask")
    dim_params = ("along", "by")
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars"],
        "updates": {"{*vars as v}": {"dtype": "float64"}},
    }
    context_template: ClassVar[dict[str, JsonValue]] = {
        "along": "{along}",
        "n": "{l_max}",
        "within": "{within}",
    }
    summary = "Adstock (carry-over) with fixed parameters"
    summary_plain = "Spreads each period's media effect over the following periods"

    def _weights(self, da: xr.DataArray, s: AdstockSettings, ctx: StepContext) -> xr.DataArray:
        params = {
            n: level_param(getattr(s, n), s.by, da, name=n, step_id=ctx.step_id)
            for n in ("alpha", "theta", "lam", "k")
            if getattr(s, n) is not None
        }
        arrays = [p for p in params.values() if isinstance(p, xr.DataArray)]
        if arrays:
            ref = arrays[0]
            vals = {
                n: (np.asarray(p.values) if isinstance(p, xr.DataArray) else p)
                for n, p in params.items()
            }
            w = adstock_weights(s.kind, s.l_max, normalize=s.normalize, **vals)
            w = np.broadcast_to(w, (ref.size, s.l_max))
            return xr.DataArray(w, dims=[*ref.dims, "__lag__"], coords=ref.coords)
        w = adstock_weights(s.kind, s.l_max, normalize=s.normalize, **params)  # type: ignore[arg-type]
        return xr.DataArray(w, dims=["__lag__"])

    def transform(
        self, ds: xr.Dataset, s: AdstockSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            da = ds[v]
            if s.along not in da.dims:
                raise schema_error(ctx.step_id, f"variable {v!r} has no dim {s.along!r}")
            x = da.astype(np.float64)
            if s.mask is not None:
                x = x.where(ds[s.mask])
            w = self._weights(da, s, ctx)
            total: xr.DataArray | None = None
            for k in range(s.l_max):
                shifted = apply_along(
                    x, ds, s.along, s.within, functools.partial(_carry_shift, k=k)
                )
                term = shifted * w.isel(__lag__=k, drop=True)
                total = term if total is None else total + term
            assert total is not None
            res = total.where(x.notnull()).transpose(*da.dims)
            res.attrs = dict(da.attrs)
            out[v] = res
        return ds.assign(out)


# =========================================================================== saturation

SaturationKind = Literal["logistic", "hill", "michaelis_menten", "tanh"]


def saturate[ArrayT: (np.ndarray, xr.DataArray)](
    x: ArrayT,
    kind: SaturationKind,
    *,
    lam: Any = None,
    slope: Any = None,
    half: Any = None,
    alpha: Any = None,
    c: Any = None,
) -> ArrayT:
    """Apply a saturation curve (see the module docstring); parameters broadcast."""
    out: Any
    match kind:
        case "logistic":
            e = np.exp(-lam * x)
            out = (1 - e) / (1 + e)
        case "hill":
            xs = x**slope
            out = xs / (half**slope + xs)
        case "michaelis_menten":
            out = alpha * x / (lam + x)
        case _:
            out = alpha * np.tanh(x / (alpha * c))
    return cast(ArrayT, out)


class SaturationSettings(StepSettings):
    vars: NameList = Field(..., min_length=1)
    kind: SaturationKind = "logistic"
    lam: Param | None = Field(None, description="logistic: rate (> 0); michaelis_menten: K")
    slope: Param | None = Field(None, description="hill: slope (> 0)")
    half: Param | None = Field(None, description="hill: half-saturation point (> 0)")
    alpha: Param | None = Field(None, description="michaelis_menten / tanh: maximum (> 0)")
    c: Param | None = Field(None, description="tanh: initial cost per unit (> 0)")
    by: Name | None = Field(None, description="Dim whose levels key per-level parameters")

    @model_validator(mode="after")
    def _params(self) -> Self:
        need = {
            "logistic": ("lam",),
            "hill": ("slope", "half"),
            "michaelis_menten": ("alpha", "lam"),
            "tanh": ("alpha", "c"),
        }[self.kind]
        missing = [n for n in need if getattr(self, n) is None]
        if missing:
            raise ValueError(f"saturation kind {self.kind!r} needs {missing}")
        for n in ("lam", "slope", "half", "alpha", "c"):
            _check(n, getattr(self, n), 0.0, None, lo_open=True)
        dict_params = [
            n for n in ("lam", "slope", "half", "alpha", "c") if isinstance(getattr(self, n), dict)
        ]
        if dict_params and self.by is None:
            raise ValueError(f"per-level {dict_params} need 'by'")
        return self


class SaturationStep(Step[SaturationSettings, StepState]):
    """Diminishing returns ``f(x)`` applied pointwise (in place; inputs must be >= 0)."""

    id = "core.saturation"
    version = "1.0.0"
    kind = StepKind.VALUE
    Settings = SaturationSettings
    var_params = ("vars",)
    dim_params = ("by",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["vars"],
        "updates": {"{*vars as v}": {"dtype": "float64", "units": "1"}},
    }
    summary = "Saturation curve with fixed parameters"
    summary_plain = "Models diminishing returns of extra spend"

    def transform(
        self, ds: xr.Dataset, s: SaturationSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        out = {}
        for v in present(ds, s.vars):
            da = ds[v]
            x = da.astype(np.float64)
            if bool((x < 0).any()):
                raise schema_error(ctx.step_id, f"saturation needs non-negative values in {v!r}")
            params = {
                n: level_param(getattr(s, n), s.by, da, name=n, step_id=ctx.step_id)
                for n in ("lam", "slope", "half", "alpha", "c")
                if getattr(s, n) is not None
            }
            res = saturate(x, s.kind, **params)
            res = res.transpose(*da.dims)
            res.attrs = {**da.attrs, "units": "1"}
            out[v] = res
        return ds.assign(out)
