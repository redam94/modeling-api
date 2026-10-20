"""Shared pieces of the statsmodels plugins: canonical names, settings, schema templates,
per-series cells, quantiles, parameter tables and residual tests.

Canonical inputs (bind your dataset's names to these in ``data.bind``)::

    dims  t         time (or integer) index, policy extend
          variable  multivariate models only: one label per series modelled jointly
          <series>  optional panel dims named in ``settings.series_dims`` (geo, product)
    vars  y(<series...>, [variable], t)   target
          <exog...>(<series...>, t)       drivers named in ``settings.exog`` (future required)

Univariate models with ``series_dims`` fit one model per series cell (a per-series loop);
multivariate models fit one joint model per cell over the ``variable`` dim.
"""

from __future__ import annotations

import math
import re
import warnings
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.models import ModelSettings
from mapi_spec.capabilities import Diagnostic
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import Field, field_validator
from scipy import stats

__all__ = [
    "DEFAULT_QUANTILES",
    "NOT_A_PART",
    "PARAM",
    "QUANTILE",
    "TARGET",
    "TIME",
    "VARIABLE",
    "Cell",
    "CellFit",
    "ExogMixin",
    "StatsmodelsSettings",
    "data_error",
    "normal_quantiles",
    "param_rows",
    "quiet",
    "residual_tests",
    "schema_template",
    "settings_error",
    "stack_cells",
]

TIME: Final = "t"
TARGET: Final = "y"
VARIABLE: Final = "variable"
QUANTILE: Final = "quantile"
PARAM: Final = "param"
TEST: Final = "test"
RESERVED_DIMS: Final = frozenset({TIME, VARIABLE, QUANTILE, PARAM, TEST, "chain", "draw"})
NOT_A_PART: Final = {"decomposition": "exclude"}
"""Attrs of a ``components`` var that is not an additive part of the series (a slope)."""

DEFAULT_QUANTILES: Final[tuple[float, ...]] = (0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.975)
"""Prediction-interval quantiles: 50%, 80%, 90% and 95% bands around the median."""

RESIDUAL_TESTS: Final[tuple[str, ...]] = ("test.ljung_box", "test.jarque_bera", "test.arch_lm")
"""Residual checks stored with every fit (``residual_tests`` group)."""


# ======================================================================================= errors


def data_error(message: str, *, pointer: str = "/data") -> MapiError:
    """A problem with the data (``SCHEMA_INVALID``, plain language)."""
    return MapiError(message, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


def settings_error(message: str, field_name: str | None = None) -> MapiError:
    """A setting that cannot work with this data (``SETTINGS_INVALID``)."""
    pointer = "/model/settings" + (f"/{field_name}" if field_name else "")
    return MapiError(message, code=ErrorCode.SETTINGS_INVALID, pointer=pointer)


@contextmanager
def quiet() -> Iterator[None]:
    """Silence statsmodels' convergence and frequency warnings (they become diagnostics)."""
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore")
        yield


# ===================================================================================== settings


class StatsmodelsSettings(ModelSettings):
    """Settings every statsmodels plugin shares."""

    series_dims: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Panel dims (geo, product, ...): one model per series cell. Empty for "
        "a single series.",
        json_schema_extra={"x-ui-level": "basic"},
    )
    quantiles: list[float] = Field(
        default_factory=lambda: list(DEFAULT_QUANTILES),
        min_length=3,
        max_length=16,
        description="Quantiles of the prediction intervals (must include 0.05, 0.5 and 0.95)",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    time_kind: Literal["time", "integer"] = Field(
        "time",
        description="Whether the time index holds dates ('time') or period numbers ('integer')",
        json_schema_extra={"x-ui-level": "advanced"},
    )

    @field_validator("series_dims")
    @classmethod
    def _series_dims(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("series_dims must not repeat a dim")
        bad = sorted(set(v) & RESERVED_DIMS)
        if bad:
            raise ValueError(f"series_dims cannot use the reserved names {bad}")
        for d in v:
            if not d.isidentifier():
                raise ValueError(f"{d!r} is not a valid dim name")
        return v

    @field_validator("quantiles")
    @classmethod
    def _quantiles(cls, v: list[float]) -> list[float]:
        if any(not 0.0 < q < 1.0 for q in v):
            raise ValueError("quantiles must lie strictly between 0 and 1")
        if sorted(set(v)) != v:
            raise ValueError("quantiles must be unique and ascending")
        for need in (0.05, 0.5, 0.95):
            if not any(math.isclose(q, need) for q in v):
                raise ValueError(f"quantiles must include {need} (median and the 90% band)")
        return v


class ExogMixin(ModelSettings):
    """Driver variables (exogenous regressors) known ahead for forecasts."""

    exog: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="Driver variables (prices, promotions, holidays) whose future values are "
        "supplied for forecasts",
        json_schema_extra={"x-ui-level": "basic"},
    )

    @field_validator("exog")
    @classmethod
    def _exog(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("exog must not repeat a variable")
        bad = sorted({x for x in v if x == TARGET or x in RESERVED_DIMS or not x.isidentifier()})
        if bad:
            raise ValueError(f"exog cannot use the names {bad}")
        return v


def schema_template(*, multivariate: bool, exog: bool, missing: str = "forbid") -> dict[str, Any]:
    """The DataSchemaTemplate of a statsmodels plugin (see the module docstring)."""
    y_dims: list[Any] = ["{*series_dims}", *([VARIABLE] if multivariate else []), TIME]
    dims: dict[str, Any] = {
        TIME: {"kind": "time", "policy": "extend"},
        "{*series_dims as d}": {"kind": "categorical", "policy": "fixed"},
    }
    if multivariate:
        dims[VARIABLE] = {"kind": "categorical", "policy": "fixed"}
    variables: dict[str, Any] = {
        TARGET: {"dims": y_dims, "dtype": "float64", "role": "target", "missing": missing}
    }
    params: dict[str, Any] = {
        "series_dims": {"type": "string_list", "default": []},
        "time_kind": {"type": "string", "enum": ["time", "integer"], "default": "time"},
    }
    if exog:
        params["exog"] = {"type": "string_list", "default": []}
        variables["{*exog as x}"] = {
            "dims": ["{*series_dims}", TIME],
            "dtype": "float64",
            "role": "covariate",
            "future_required": True,
        }
    return {
        "params": params,
        "template": {"dims": dims, "vars": variables},
        "cases": [
            {"when": {"time_kind": "integer"}, "merge": {"dims": {TIME: {"kind": "integer"}}}}
        ],
    }


# ======================================================================================= cells


@dataclass(slots=True)
class Cell:
    """One series (univariate) or one group of jointly modelled series (multivariate).

    ``y`` is ``(n,)`` or ``(n, k)`` over the fitted periods; ``x`` the drivers ``(n, m)``.
    ``key`` names the cell along the panel dims (empty for a single series); ``t`` holds
    the fitted periods' labels.
    """

    key: dict[str, Any]
    y: np.ndarray
    x: np.ndarray | None
    exog_names: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)
    t: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.int64))
    """The fitted periods' labels (dates or integers)."""

    @property
    def n(self) -> int:
        return int(self.y.shape[0])

    @property
    def label(self) -> str:
        if not self.key:
            return "the series"
        return ", ".join(f"{k}={v}" for k, v in self.key.items())

    def endog(self) -> pd.Series | pd.DataFrame:
        """``y`` as pandas over a 0..n-1 RangeIndex (statsmodels' integer index)."""
        if self.y.ndim == 1:
            return pd.Series(self.y, name=TARGET)
        return pd.DataFrame(self.y, columns=self.variables)

    def exog(self) -> pd.DataFrame | None:
        if self.x is None or not self.exog_names:
            return None
        return pd.DataFrame(self.x, columns=self.exog_names)


@dataclass(slots=True)
class CellFit:
    """What fitting one cell produced (stored in the bundle, never pickled).

    ``params`` holds every estimated parameter in the order the model needs to rebuild it;
    ``table`` the parameter table rows ``name -> {estimate, std_err, statistic, p_value,
    ci_lower, ci_upper}``; ``resid`` residuals over the fitted periods (NaN where none);
    ``groups`` extra per-cell datasets (impulse responses, regime probabilities, ...).
    """

    params: dict[str, float]
    table: dict[str, dict[str, float]]
    stats: dict[str, float]
    resid: np.ndarray
    learned: dict[str, Any] = field(default_factory=dict)
    groups: dict[str, xr.Dataset] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    converged: bool = True


TABLE_FIELDS: Final = ("estimate", "std_err", "statistic", "p_value", "ci_lower", "ci_upper")


def _num(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return f


def param_rows(
    names: Sequence[str],
    estimate: Any,
    std_err: Any = None,
    statistic: Any = None,
    p_value: Any = None,
    conf_int: Any = None,
) -> dict[str, dict[str, float]]:
    """Parameter-table rows from statsmodels arrays (any of them may be ``None``)."""

    def arr(a: Any) -> np.ndarray:
        if a is None:
            return np.full(len(names), np.nan)
        return np.asarray(a, dtype=float).reshape(-1)

    est, se, st, pv = arr(estimate), arr(std_err), arr(statistic), arr(p_value)
    if conf_int is None:
        z = stats.norm.ppf(0.975)
        lo, hi = est - z * se, est + z * se
    else:
        ci = np.asarray(conf_int, dtype=float).reshape(len(names), 2)
        lo, hi = ci[:, 0], ci[:, 1]
    if statistic is None:
        with np.errstate(all="ignore"):
            st = est / se
    out: dict[str, dict[str, float]] = {}
    for i, name in enumerate(names):
        out[str(name)] = {
            "estimate": _num(est[i]),
            "std_err": _num(se[i]),
            "statistic": _num(st[i]),
            "p_value": _num(pv[i]),
            "ci_lower": _num(lo[i]),
            "ci_upper": _num(hi[i]),
        }
    return out


# =================================================================================== quantiles


def normal_quantiles(mean: np.ndarray, var: np.ndarray, quantiles: Sequence[float]) -> np.ndarray:
    """``(Q, *mean.shape)`` Gaussian quantiles from means and variances (NaN-safe)."""
    z = stats.norm.ppf(np.asarray(quantiles, dtype=float))
    sd = np.sqrt(np.clip(np.asarray(var, dtype=float), 0.0, None))
    mean = np.asarray(mean, dtype=float)
    return mean[None, ...] + z.reshape((-1,) + (1,) * mean.ndim) * sd[None, ...]


def sample_quantiles(samples: np.ndarray, quantiles: Sequence[float] | np.ndarray) -> np.ndarray:
    """``(Q, ...)`` quantiles over the first axis of simulated paths."""
    return np.quantile(np.asarray(samples, dtype=float), np.asarray(quantiles), axis=0)


# ================================================================================ residual tests


_ARMA_NAME: Final = re.compile(r"^(?:(?:ar|ma)\.(?:S\.)?L\d+|(?:D\.)?y\.L\d+)")


def arma_df(param_names: Iterable[str]) -> int:
    """The Ljung-Box degrees-of-freedom correction of a fit: the number of ARMA
    coefficients only (``ar.L*``, ``ma.L*``, seasonal ``ar.S.L*``/``ma.S.L*``, ARDL lags of
    the target ``y.L*`` and an error-correction model's lagged differences ``D.y.L*``;
    regime-specific copies ``ar.L1[0]``/``ar.L1[1]`` count once). Constants, drivers,
    variances and smoothing or initial-state parameters do not shrink the test's degrees of
    freedom (Ljung & Box 1978)."""
    seen = {m.group(0) for n in param_names if (m := _ARMA_NAME.match(str(n)))}
    return len(seen)


def residual_tests(resid: np.ndarray, *, model_df: int = 0) -> dict[str, tuple[float, float, int]]:
    """``test id -> (statistic, p_value, lags)`` for one residual series.

    Ljung-Box (lags ``min(10, n // 5)``, at least ``model_df + 3``; degrees of freedom
    reduced by ``model_df``, the number of ARMA terms, :func:`arma_df`), Jarque-Bera and
    Engle's ARCH-LM (``min(4, n // 10)`` lags).
    """
    from statsmodels.stats.diagnostic import acorr_ljungbox, het_arch
    from statsmodels.stats.stattools import jarque_bera

    r = np.asarray(resid, dtype=float)
    r = r[np.isfinite(r)]
    nan = (float("nan"), float("nan"), 0)
    out: dict[str, tuple[float, float, int]] = dict.fromkeys(RESIDUAL_TESTS, nan)
    if r.size < 8 or float(np.std(r)) == 0.0:
        return out
    with quiet():
        lags = max(1, min(10, r.size // 5))
        if model_df > 0 and lags <= model_df and model_df + 3 <= r.size // 2:
            lags = model_df + 3  # keep positive degrees of freedom
        dof = model_df if 0 < model_df < lags else 0
        lb = acorr_ljungbox(r, lags=[lags], model_df=dof)
        out["test.ljung_box"] = (float(lb["lb_stat"].iloc[0]), float(lb["lb_pvalue"].iloc[0]), lags)
        jb, jb_p, _, _ = jarque_bera(r)
        out["test.jarque_bera"] = (float(jb), float(jb_p), 0)
        arch_lags = max(1, min(4, r.size // 10))
        try:
            lm, lm_p, _, _ = het_arch(r, nlags=arch_lags)
            out["test.arch_lm"] = (float(lm), float(lm_p), arch_lags)
        except (ValueError, np.linalg.LinAlgError):
            pass
    return out


# ================================================================================ stacking cells


def stack_cells(
    keys: Sequence[Mapping[str, Any]],
    datasets: Sequence[xr.Dataset],
    order: Mapping[str, np.ndarray],
) -> xr.Dataset:
    """Combine per-cell datasets into one with the panel dims in front (fit order).

    Cells may have different coordinates along their own dims (parameter names of
    per-series lag selections): the union is taken and missing cells are NaN.
    """
    if not datasets:
        return xr.Dataset()
    if not order:
        return datasets[0]
    expanded = [
        ds.expand_dims({d: [key[d]] for d in order}) for key, ds in zip(keys, datasets, strict=True)
    ]
    with xr.set_options(use_new_combine_kwarg_defaults=True):
        merged = xr.merge(expanded, join="outer", compat="no_conflicts", combine_attrs="drop")
    merged = merged.reindex({d: order[d] for d in order})
    lead = list(order)
    out = {}
    for v in merged.data_vars:
        dims = lead + [d for d in merged[v].dims if d not in lead]
        out[v] = merged[v].transpose(*dims)
        out[v].attrs = next((dict(ds[v].attrs) for ds in datasets if v in ds), {})
    result = xr.Dataset(out, coords=merged.coords)
    result.attrs = dict(datasets[0].attrs)
    return result


def iter_cells(ds: xr.Dataset, series_dims: Sequence[str]) -> Iterator[dict[str, Any]]:
    """Every combination of the panel dims' labels, in dataset order."""
    if not series_dims:
        yield {}
        return
    labels = [np.asarray(ds[d].values) for d in series_dims]
    for idx in np.ndindex(*(len(x) for x in labels)):
        yield {
            d: labels[i][j].item() for i, (d, j) in enumerate(zip(series_dims, idx, strict=True))
        }
