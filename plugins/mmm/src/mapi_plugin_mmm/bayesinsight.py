"""``mmm.bayesinsight``: a BayesInsight ``model_def.json`` as a built-in model (Q8, M3).

BayesInsight models are defined variable by variable in ``model_def.json``; this plugin
fits exactly that model on the platform. The semantics - likelihoods, coefficient priors
in KPI units, curves and carry-over, sign constraints, random effects per row id,
seasonality and local spline trends - are documented on
:mod:`mapi_spec.bayesinsight_model`, which also holds the settings; the curves and design
matrices live in :mod:`mapi_plugin_mmm.bayesinsight_math`.

Model-stage data (``import_bayesinsight(..., target="mmm.bayesinsight")`` writes the
spec)::

    y(*panel, time)                  the KPI, as stored (BayesInsight does not rescale it)
    <media>(*panel, time, lag)       raw media as lag tensors (core.lag_tensor, pad: nan)
    <control>(*panel, time)          raw controls

``panel`` are the model's row ids besides ``Period`` (``geo``, ``product``, ...). Missing
lags (before a series starts, or the history of a level new at score time) carry no media,
which is BayesInsight's zero-padded convolution.

Outputs: ``y_hat(*panel, time)`` - ``expected`` is the KPI's mean (``eta`` for a Normal
likelihood, ``exp(eta + sigma^2 / 2)`` for a log-normal, ``exp(eta)`` times the
zero-inflation share for counts), ``predictive`` draws the likelihood with common random
numbers where the family allows - and ``contributions(component, *panel, time)``: the
media, ``baseline`` (the intercepts), each control, season and trend. They sum to the
expected KPI: for a Normal likelihood they are the additive terms; for log-link and
log-normal models they are the log-mean Divisia split of ``E[y] - baseline`` (each term
gets its share of the log change, weighted by the logarithmic mean of ``E[y]`` and the
baseline), which is exact and stable for terms of either sign.

Fit-time statistics (media indices and maxima, standardisation moments, the number of fit
periods) are stored in ``learned["bayesinsight"]`` and reused at score time, so a scenario
is scored with the fitted transforms. Lift tests (``calibration.lift_tests``) observe the
incremental KPI ``sum_cells E[y | x + dx] - E[y | x]`` over the test window with the extra
spend carried over; a test's ``geo`` is a label of the first panel dim.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Final

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.engines import ENGINES
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import FitContext, FitOutput, Fitted, ScoreContext
from mapi_spec.bayesinsight_model import (
    BayesInsightSettings,
    BIControl,
    BILocalTrend,
    BIMedia,
    BISeason,
    CoeffPrior,
    HillPrior,
    SShapedPrior,
    WeibullAdstockPrior,
)
from mapi_spec.calibration import LiftTest
from mapi_spec.capabilities import Capabilities, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig, ScoreRequest

from mapi_plugin_mmm.bayesinsight_math import (
    adstock_weights,
    bspline_basis,
    deterministic,
    fourier_features,
    fourier_labels,
    media_curve,
    nonzero_index,
)
from mapi_plugin_mmm.settings import mcmc_fit_schema

__all__ = ["SCHEMA_TEMPLATE", "BayesInsightMMM"]

TARGET: Final = "y"
TIME: Final = "time"
BASELINE: Final = "baseline"
LIFT_VAR: Final = "lift_tests"
LIFT_DIM: Final = "lift_test"
_CLIP: Final = 20.0
_STATS_KEY: Final = "bayesinsight"

SCHEMA_TEMPLATE: dict[str, Any] = {
    "params": {
        "panel": {"type": "string_list", "default": []},
        "media": {"type": "string_list", "default": []},
        "controls": {"type": "string_list", "default": []},
        "lag_dim": {"type": "string", "default": "lag"},
    },
    "template": {
        "dims": {
            TIME: {"kind": "time", "policy": "extend"},
            "{*panel as d}": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
            },
            "{lag_dim}": {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            TARGET: {"dims": ["{*panel}", TIME], "dtype": "float64", "role": "target",
                     "missing": "allow"},
            "{*media as v}": {"dims": ["{*panel}", TIME, "{lag_dim}"], "dtype": "float64",
                              "role": "spend", "missing": "allow"},
            "{*controls as v}": {"dims": ["{*panel}", TIME], "dtype": "float64",
                                 "role": "control", "missing": "allow"},
        },
    },
}  # fmt: skip

_MODES = frozenset({"predict", "forecast", "counterfactual", "evaluate", "simulate"})


def _schema_error(message: str, pointer: str = "") -> MapiError:
    return MapiError(message, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


def _settings_error(message: str, pointer: str = "/model/settings/model_def") -> MapiError:
    return MapiError(message, code=ErrorCode.SETTINGS_INVALID, pointer=pointer)


# ================================================================================ fit stats


def _moments(values: np.ndarray, mean: float | None, std: float | None) -> tuple[float, float]:
    """BayesInsight's ``Global Standardize``: the stored moments, else the mean and sample sd
    over every finite cell."""
    v = values[np.isfinite(values)]
    m = float(v.mean()) if mean is None and v.size else float(mean or 0.0)
    if std is not None:
        return m, float(std)
    sd = float(v.std(ddof=1)) if v.size > 1 else 0.0
    return m, sd if sd > 0 else 1.0


def _cell_lookup(
    fit_values: Any, fit_labels: Mapping[str, Sequence[str]], cur: Mapping[str, np.ndarray]
) -> np.ndarray:
    """A per-cell fit statistic (nested lists over the fit panel) on the current panel;
    levels unseen at fit get the median over the fitted cells."""
    arr = np.asarray(fit_values, dtype=np.float64)
    dims = list(fit_labels)
    if not dims:
        return arr
    idx = []
    for d in dims:
        pos = {str(v): i for i, v in enumerate(fit_labels[d])}
        idx.append(np.asarray([pos.get(str(v), -1) for v in cur[d].tolist()], dtype=np.int64))
    grid = np.ix_(*[np.where(i < 0, 0, i) for i in idx])
    out = arr[grid]
    unseen = np.zeros(out.shape, dtype=bool)
    for axis, i in enumerate(idx):
        shape = [1] * len(idx)
        shape[axis] = len(i)
        unseen |= (i < 0).reshape(shape)
    if unseen.any():
        out = np.where(unseen, float(np.nanmedian(arr)), out)
    return out


# ================================================================================ the panel


class _Panel:
    """Panel dims of one build: positions, new levels and broadcasting helpers."""

    def __init__(self, ctx: BuildContext, s: BayesInsightSettings) -> None:
        self.dims = list(s.panel)
        self.row_dims: dict[str, str] = {str(k): v for k, v in s.row_dims.items()}
        self.labels = {d: ctx.labels(d) for d in self.dims}
        self.new = {d: int(ctx.new_levels(d).size) for d in self.dims}
        self.shape = tuple(len(self.labels[d]) for d in self.dims)

    def of(self, row_ids: Sequence[str] | None) -> list[str]:
        """The panel dims of BayesInsight row ids, in panel order."""
        want = {self.row_dims[r] for r in row_ids or ()}
        return [d for d in self.dims if d in want]

    def rv_dims(self, dims: Sequence[str]) -> list[str]:
        """Dims of a parameter indexed by panel dims (fit levels when some are new)."""
        return [f"{d}__fit" if self.new[d] else d for d in dims]

    def extend(
        self,
        t: Any,
        dims: Sequence[str],
        *,
        offset: int = 0,
        fill: str | Callable[[Any, int], Any],
        what: str,
    ) -> Any:
        """``t`` (axes ``[*lead, *dims, ...]``) over the current levels: new levels of each
        dim get ``fill`` - ``zero``, ``mean`` (of the fitted levels), ``error`` or a callable
        ``(t, axis) -> pad`` of one level."""
        for i, d in enumerate(dims):
            n_new = self.new[d]
            if not n_new:
                continue
            axis = offset + i
            if fill == "error":
                raise MapiError(
                    f"{what} is estimated separately per {d!r} (fixed_ind_coeff_dims), so the "
                    f"{n_new} {d} level(s) unseen at fit have none; score fitted levels, or "
                    "pool it with random_coeff_dims",
                    code=ErrorCode.UNSEEN_LEVELS,
                )
            if fill == "zero":
                first = pt.take(t, pt.arange(1), axis=axis)
                one = pt.zeros_like(first)
            elif fill == "mean":
                one = t.mean(axis=axis, keepdims=True)
            elif callable(fill):
                one = fill(t, axis)
            else:  # pragma: no cover - internal
                raise ValueError(fill)
            t = pt.concatenate([t, pt.repeat(one, n_new, axis=axis)], axis=axis)
        return t

    def broadcast(self, t: Any, dims: Sequence[str], *, lead: int = 0, trailing: int = 0) -> Any:
        """``t`` with axes ``[*lead, *dims, *trailing]`` as ``[*lead, *panel, *trailing]``
        (size-1 axes for the panel dims it does not vary over)."""
        pattern: list[Any] = list(range(lead))
        for d in self.dims:
            pattern.append(lead + list(dims).index(d) if d in dims else "x")
        pattern += [lead + len(dims) + j for j in range(trailing)]
        return pt.as_tensor_variable(t).dimshuffle(*pattern)


# ================================================================================ lift tests


@dataclass(frozen=True, slots=True)
class _LiftDesign:
    """One row per cell of a lift test (a panel cell and a period in its window)."""

    ids: list[str]
    test: np.ndarray  # (row,)
    media: np.ndarray  # (row,) index into the media list
    cell: tuple[np.ndarray, ...]  # panel positions per panel dim, (row,) each
    time: np.ndarray  # (row,)
    z0: np.ndarray  # (row, lag) curve input at the actual spend
    z1: np.ndarray  # (row, lag) curve input with the extra spend
    valid: np.ndarray  # (row, lag)
    target_scale: np.ndarray  # (row,) original KPI units per model unit
    observed: np.ndarray  # (test,)
    sigma: np.ndarray  # (test,)
    skipped: list[str] = field(default_factory=list)

    def segments(self) -> np.ndarray:
        m = np.zeros((len(self.ids), self.test.size))
        m[self.test, np.arange(self.test.size)] = 1.0
        return m


def _day(t: date) -> np.datetime64:
    return np.datetime64(t.isoformat(), "D")


def _linear_scale(
    to_original: Callable[[xr.DataArray, str], xr.DataArray] | None,
    ones: xr.DataArray,
    src: str,
) -> np.ndarray:
    """Original units per model unit of ``src`` over ``ones``' cells (must be linear)."""
    if to_original is None:
        return np.ones(ones.shape)
    a = np.asarray(to_original(ones, src).transpose(*ones.dims).values, dtype=np.float64)
    b = np.asarray(to_original(ones * 2.0, src).transpose(*ones.dims).values, dtype=np.float64)
    if not (np.isfinite(a).all() and np.allclose(b, 2 * a, rtol=1e-6) and (a > 0).all()):
        raise MapiError(
            f"lift tests need the pipeline to scale {src!r} linearly (a log or standardising "
            "step makes amounts in business units untranslatable)",
            code=ErrorCode.CALIBRATION_UNSUPPORTED,
            pointer="/calibration/lift_tests",
        )
    return a


# ============================================================================== the plugin


class BayesInsightMMM(PyMCModel[BayesInsightSettings]):
    """A BayesInsight model (``model_def.json``) under the platform contract."""

    id = "mmm.bayesinsight"
    version = "1.0.0"
    Settings = BayesInsightSettings
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=_MODES,
        contribution_scale="additive",
        identified_quantities=["y_hat", "contributions"],
        deterministic_given_seed=False,
        checkpointable=True,
        calibration=frozenset({"lift_tests"}),
    )
    schema_template = SCHEMA_TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        # the panel dims (settings ``panel``) come from the KPI
        "y_hat": {
            "dims": [TIME],
            "dims_of": TARGET,
            "units_of": TARGET,
            "description": "Expected (or predictive) KPI over the panel dims and time",
        },
        "contributions": {
            "dims": ["component", TIME],
            "dims_of": TARGET,
            "units_of": TARGET,
            "description": "Media, baseline (intercepts), controls, seasons and trends in KPI "
            "units; they sum to the expected KPI (log-mean Divisia split for log links)",
        },
    }
    expected: ClassVar[dict[str, str]] = {"y_hat": "mu", "contributions": "contributions"}
    predictive: ClassVar[dict[str, str]] = {"y_hat": TARGET}
    plots: ClassVar[list[str]] = ["core.ppc_fan", "core.convergence", "core.posterior_forest"]
    resources = ResourceHint(
        min_class=ResourceClass.S, default_class=ResourceClass.M, est_fit_seconds=900
    )
    fit_schema: ClassVar[dict[str, Any]] = mcmc_fit_schema(
        engines=[e for e in ENGINES if e in ("nutpie", "pymc", "numpyro", "blackjax", "advi")],
        default_engine="nutpie",
    )
    description = (
        "A BayesInsight model_def.json as a built-in model: Normal, log-normal, Poisson, "
        "negative binomial and zero-inflated likelihoods, coefficient priors in KPI units, "
        "Hill, linear and S-shaped curves with delayed, geometric or Weibull carry-over, sign "
        "constraints, fixed and partially pooled effects per row id, Fourier seasons and "
        "local spline trends."
    )
    pad_extend = False
    default_engine = "nutpie"
    _stats: dict[str, Any] | None = None
    """Fit-time statistics of the build in progress (set by :meth:`fit` and :meth:`score`)."""
    _lift: _LiftDesign | None = None

    # ------------------------------------------------------------------ data
    @staticmethod
    def _array(ds: xr.Dataset, name: str, dims: Sequence[str]) -> np.ndarray:
        if name not in ds:
            raise MapiError(
                f"the data has no variable {name!r}",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer=f"/data/{name}",
            )
        da = ds[name]
        if set(map(str, da.dims)) != set(dims):
            raise _schema_error(
                f"{name!r} must have dims {sorted(dims)}, got {list(map(str, da.dims))}",
                f"/data/{name}",
            )
        return np.asarray(da.transpose(*dims).values, dtype=np.float64)

    def _fit_stats(self, ds: xr.Dataset, s: BayesInsightSettings) -> dict[str, Any]:
        """The statistics BayesInsight computes from the fit data (see the module doc)."""
        md = s.model_def
        panel = list(s.panel)
        out: dict[str, Any] = {
            "n_fit": int(ds.sizes[TIME]),
            "labels": {d: [str(v) for v in ds[d].values] for d in panel},
            "media": {},
            "controls": {},
            "seasons": {},
        }
        if out["n_fit"] < 2:
            raise _schema_error("the model needs at least two periods", "/data")
        for m in md.media:
            name = s.var(m.variable_name)
            x = self._array(ds, name, [*panel, TIME, s.lag_dim])
            series = x[..., 0]
            if not np.isfinite(series).all():
                raise _schema_error(
                    f"media {name!r} has {int((~np.isfinite(series)).sum())} missing "
                    "value(s); impute them in the pipeline (core.impute)",
                    f"/data/{name}",
                )
            if (series < 0).any():
                raise _schema_error(f"media {name!r} must be non-negative", f"/data/{name}")
            dt = m.deterministic_transform
            moved = deterministic(series, dt.functional_form, dt.params)
            mean, std = (None, None)
            if m.normalization == "Global Standardize":
                mean, std = _moments(moved, m.mean, m.std)
            peak = np.where(series.max(axis=-1) > 0, series.max(axis=-1), 1.0)
            out["media"][name] = {
                "index": nonzero_index(series, m.index_to).tolist(),
                "max": peak.tolist(),
                "mean": mean,
                "std": std,
            }
        for c in md.controls:
            name = s.var(c.variable_name)
            x = self._array(ds, name, [*panel, TIME])
            if not np.isfinite(x).all():
                raise _schema_error(
                    f"control {name!r} has {int((~np.isfinite(x)).sum())} missing value(s); "
                    "impute them in the pipeline (core.impute)",
                    f"/data/{name}",
                )
            dt = c.deterministic_transform
            mean, std = (None, None)
            if c.normalization == "Global Standardize":
                mean, std = _moments(deterministic(x, dt.functional_form, dt.params), c.mean, c.std)
            out["controls"][name] = {"mean": mean, "std": std}
        for sv in md.seasons:
            mean, std = (None, None)
            if sv.normalization == "Global Standardize":
                n = out["n_fit"]
                f = fourier_features(np.arange(n), n, sv.period, sv.n_fourier)
                mean, std = _moments(f, sv.mean, sv.std)
            out["seasons"][sv.variable_name] = {"mean": mean, "std": std}
        return out

    def _stats_or_raise(self) -> dict[str, Any]:
        if self._stats is None:
            raise MapiError(
                "the fitted model has no BayesInsight statistics; refit it",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        return self._stats

    def _media_input(
        self,
        x: np.ndarray,
        m: BIMedia,
        stat: Mapping[str, Any],
        fit_labels: Mapping[str, Sequence[str]],
        cur: Mapping[str, np.ndarray],
    ) -> tuple[np.ndarray, np.ndarray]:
        """``(z, valid)``: the curve input of a media lag tensor ``(*panel, time, lag)``
        (missing lags - padding or no history - are not valid)."""
        dt = m.deterministic_transform
        moved = deterministic(x, dt.functional_form, dt.params)
        if stat["std"] is not None:
            moved = (moved - stat["mean"]) / stat["std"]
        key = "max" if m.media_transform == "sshaped" else "index"
        ref = _cell_lookup(stat[key], fit_labels, cur)
        z = moved / ref.reshape((*ref.shape, 1, 1))
        valid = np.isfinite(z)
        z = np.where(valid, z, 0.0)
        if m.media_transform != "linear":
            z = np.maximum(z, 0.0)
        return z, valid

    def _control_input(self, x: np.ndarray, c: BIControl, stat: Mapping[str, Any]) -> np.ndarray:
        dt = c.deterministic_transform
        moved = deterministic(x, dt.functional_form, dt.params)
        if stat["std"] is not None:
            moved = (moved - stat["mean"]) / stat["std"]
        return np.where(np.isfinite(moved), moved, 0.0)

    # ------------------------------------------------------------------ priors
    @staticmethod
    def _dist(name: str, prior: CoeffPrior, dims: list[str], sign: str | None) -> Any:
        """A coefficient prior with its sign: truncated (real priors) or negated (positive
        priors under ``negative``)."""
        dist = prior.coeff_dist
        params = dict(prior.coeff_params)
        kw: dict[str, Any] = {"dims": dims} if dims else {}
        if sign is None:
            return getattr(pm, dist)(name, **params, **kw)
        if prior.positive:
            rv = getattr(pm, dist)(name, **params, **kw)
            return -rv if sign == "negative" else rv
        if dist == "Uniform":
            lo, hi = params["lower"], params["upper"]
            lo, hi = (max(lo, 0.0), hi) if sign == "positive" else (lo, min(hi, 0.0))
            return pm.Uniform(name, lower=lo, upper=hi, **kw)
        bounds = {"lower": 0.0} if sign == "positive" else {"upper": 0.0}
        return pm.Truncated(name, getattr(pm, dist).dist(**params), **bounds, **kw)

    def _coef(
        self,
        pan: _Panel,
        name: str,
        prior: CoeffPrior,
        fixed_ids: Sequence[str] | None,
        random_ids: Sequence[str] | None,
        pooling: float,
        sign: str | None,
        *,
        lead: str | None = None,
    ) -> Any:
        """A coefficient over the whole panel (``[lead, *panel]`` with a leading dim)."""
        fixed = pan.of(fixed_ids)
        random = pan.of(random_ids)
        lead_dims = [lead] if lead else []
        n_lead = len(lead_dims)
        coef = self._dist(f"{name}__coef", prior, [*lead_dims, *pan.rv_dims(fixed)], sign)
        coef = pan.extend(coef, fixed, offset=n_lead, fill="error", what=f"{name!r}'s coefficient")
        coef = pan.broadcast(coef, fixed, lead=n_lead)
        if random_ids is None or not random or pooling <= 0:
            return coef
        sd_kw: dict[str, Any] = {"dims": lead_dims} if lead_dims else {}
        sigma = pm.HalfNormal(f"{name}__coef_sigma", pooling, **sd_kw)
        z = pm.Normal(f"{name}__coef_z", 0.0, 1.0, dims=[*lead_dims, *pan.rv_dims(random)])
        z = pan.extend(z, random, offset=n_lead, fill="zero", what=name)
        z = pan.broadcast(z, random, lead=n_lead)
        sig = pt.as_tensor_variable(sigma)
        if lead_dims:
            sig = sig.dimshuffle(0, *(["x"] * len(pan.dims)))
        if sign is not None:
            return coef * pt.exp(sig * z)  # every level keeps the sign
        return coef + sig * z

    # ------------------------------------------------------------------ media
    @staticmethod
    def _curve_params(name: str, m: BIMedia) -> dict[str, Any]:
        prior = m.media_transform_prior
        if m.media_transform == "hill":
            hp = prior if isinstance(prior, HillPrior) else HillPrior()
            k = pm.LogNormal(f"{name}__K", math.log(hp.K_ave), math.log(hp.K_std / hp.K_ave + 1))
            n_ = pm.Exponential(f"{name}__n_", 1.0 / (hp.n_ave - 1.0))
            return {"K": k, "n": pm.Deterministic(f"{name}__n", n_ + 1.0)}
        if m.media_transform == "sshaped":
            sp = prior if isinstance(prior, SShapedPrior) else SShapedPrior()
            alpha = pm.Beta(f"{name}__s_alpha", mu=sp.alpha_ave, sigma=sp.alpha_std)
            mu_b, sd_b = sp.beta_moments()
            beta_ = pm.Beta(f"{name}__s_beta_", mu=mu_b, sigma=sd_b)
            return {"alpha": alpha, "beta": pm.Deterministic(f"{name}__s_beta", 10 ** (8 * beta_))}
        return {}

    @staticmethod
    def _adstock_params(name: str, m: BIMedia) -> dict[str, Any]:
        prior = m.adstock_prior
        if m.adstock in ("delayed", "geometric"):
            mean = getattr(prior, "retention_rate_mean", 0.2)
            nu = getattr(prior, "retention_rate_std", 10.0)
            out: dict[str, Any] = {"alpha": pm.Beta(f"{name}__retention", mu=mean, nu=nu)}
            if m.adstock == "delayed":
                out["theta"] = pm.Exponential(f"{name}__lag", getattr(prior, "lag_max", 3.0))
            return out
        if m.adstock == "weibull":
            wp = prior if isinstance(prior, WeibullAdstockPrior) else WeibullAdstockPrior()
            lm, ls, km, ks = wp.resolved()
            return {
                "lam": pm.Gamma(f"{name}__w_lam", mu=lm, sigma=ls),
                "k": pm.Gamma(f"{name}__w_k", mu=km, sigma=ks),
            }
        return {}

    # ------------------------------------------------------------------ build
    def build(self, ds: xr.Dataset, s: BayesInsightSettings, ctx: BuildContext) -> pm.Model:
        stats = self._stats_or_raise()
        md = s.model_def
        panel = list(s.panel)
        n_lag = int(ds.sizes.get(s.lag_dim, 0)) if md.media else s.l_max
        if md.media and n_lag != s.l_max:
            raise _schema_error(
                f"l_max is {s.l_max} but the media lag tensors have {n_lag} lags; make them "
                "equal (core.lag_tensor l_max)",
                "/model/settings/l_max",
            )
        fit_labels: dict[str, list[str]] = stats["labels"]
        cur = {d: np.asarray(ds[d].values) for d in panel}
        coords = ctx.coords()
        components = [s.var(m.variable_name) for m in md.media] + [BASELINE]
        components += [s.var(c.variable_name) for c in md.controls]
        components += [v.variable_name for v in md.seasons]
        components += [v.variable_name for v in md.trends]
        if len(set(components)) != len(components):
            raise _settings_error(f"component names clash: {components}")
        coords["component"] = components
        for sv in md.seasons:
            coords[f"{sv.variable_name}__mode"] = fourier_labels(sv.n_fourier)
        lift = self._lift if ctx.is_fit else None
        if lift is not None:
            coords[LIFT_DIM] = lift.ids
        n_time = int(ds.sizes[TIME])
        positions = ctx.position(TIME) if md.seasons else np.arange(n_time, dtype=float)
        with pm.Model(coords=coords) as model:
            pan = _Panel(ctx, s)
            parts: list[Any] = []
            handles: list[tuple[Any, Any, dict[str, Any], BIMedia]] = []
            for m in md.media:
                name = s.var(m.variable_name)
                x = self._array(ds, name, [*panel, TIME, s.lag_dim])
                if ctx.is_fit and not np.isfinite(x[..., 0]).all():
                    raise _schema_error(f"media {name!r} has missing values", f"/data/{name}")
                z, valid = self._media_input(x, m, stats["media"][name], fit_labels, cur)
                curve = self._curve_params(name, m)
                w = adstock_weights(
                    m.adstock,
                    n_lag,
                    self._adstock_params(name, m),
                    weibull=getattr(m.adstock_prior, "kind", "PDF"),
                    xp=pt,
                )
                f = media_curve(pt.as_tensor_variable(z), m.media_transform, curve, xp=pt)
                carried = (f * pt.as_tensor_variable(valid.astype(np.float64)) * w).sum(axis=-1)
                coef = self._coef(
                    pan, name, m.coeff_prior, m.fixed_ind_coeff_dims, m.random_coeff_dims,
                    m.partial_pooling_sigma, m.sign,
                )  # fmt: skip
                parts.append(coef[..., None] * carried)
                handles.append((pt.broadcast_to(coef, pan.shape), w, curve, m))
            kpi = md.kpi
            if kpi.intercept_prior is None:
                intercept = pt.zeros(pan.shape)
            else:
                intercept = self._coef(
                    pan, "intercept", kpi.intercept_prior, kpi.fixed_ind_coeff_dims,
                    kpi.random_coeff_dims, kpi.partial_pooling_sigma, None,
                )  # fmt: skip
            parts.append(intercept[..., None] * pt.ones((n_time,)))
            for c in md.controls:
                name = s.var(c.variable_name)
                x = self._array(ds, name, [*panel, TIME])
                if ctx.is_fit and not np.isfinite(x).all():
                    raise _schema_error(f"control {name!r} has missing values", f"/data/{name}")
                xc = self._control_input(x, c, stats["controls"][name])
                coef = self._coef(
                    pan, name, c.coeff_prior, c.fixed_ind_coeff_dims, c.random_coeff_dims,
                    c.partial_pooling_sigma, c.sign,
                )  # fmt: skip
                parts.append(coef[..., None] * pt.as_tensor_variable(xc))
            for sv in md.seasons:
                parts.append(self._season(pan, sv, stats, positions))
            for tv in md.trends:
                parts.append(self._trend(pan, tv, ctx))
            stacked = pt.stack([pt.broadcast_to(p, (*pan.shape, n_time)) for p in parts])
            eta = stacked.sum(axis=0)
            mean_of, obs_dist = self._likelihood(pan, kpi.likelihood, eta)
            mu = pm.Deterministic("mu", mean_of(eta), dims=[*panel, TIME])
            base = len(md.media)
            contributions = self._contributions(stacked, eta, mu, base, mean_of, kpi.likelihood)
            pm.Deterministic("contributions", contributions, dims=["component", *panel, TIME])
            if lift is not None:
                self._lift_likelihood(lift, handles, eta, mean_of)
            observed = ctx.observed(TARGET, dims=[*panel, TIME])
            if ctx.is_fit:
                self._check_observed(observed, kpi.likelihood.type)
            obs_dist(TARGET, observed, [*panel, TIME])
        return model

    def _season(
        self, pan: _Panel, sv: BISeason, stats: Mapping[str, Any], positions: np.ndarray
    ) -> Any:
        f = fourier_features(positions, int(stats["n_fit"]), sv.period, sv.n_fourier)
        st = stats["seasons"].get(sv.variable_name, {})
        if st.get("std") is not None:
            f = (f - st["mean"]) / st["std"]
        mode = f"{sv.variable_name}__mode"
        coef = self._coef(
            pan, _rv_name(sv.variable_name), sv.coeff_prior, sv.fixed_ind_coeff_dims,
            sv.random_coeff_dims, sv.partial_pooling_sigma, None, lead=mode,
        )  # fmt: skip
        # (mode, *panel) x (time, mode) -> (*panel, time)
        out = pt.tensordot(pt.as_tensor_variable(f), coef, axes=[[1], [0]])
        return out.dimshuffle(*range(1, 1 + len(pan.dims)), 0)

    def _trend(self, pan: _Panel, tv: BILocalTrend, ctx: BuildContext) -> Any:
        t = ctx.position(TIME, scale=True)
        basis = bspline_basis(t, tv.num_knots, tv.order)  # (time, spline)
        name = _rv_name(tv.variable_name)
        n_spl = basis.shape[1]
        spl = f"{name}__spline"
        model = pm.modelcontext(None)
        model.add_coord(spl, np.arange(n_spl))
        prior = tv.llt_prior
        init = pm.Normal.dist(0.0, prior.initial_dist_var)
        tau = pm.HalfNormal(f"{name}__tau", prior.variability)
        b = pt.as_tensor_variable(basis)
        if tv.random_coeff_dims is None:
            betas = pm.GaussianRandomWalk(
                f"{name}__betas", mu=0.0, sigma=tau, init_dist=init, dims=[spl]
            )
            return pan.broadcast(pt.dot(b, betas), [], trailing=1)  # (time,) -> panel
        rand = pan.of(tv.random_coeff_dims)
        mu = pm.GaussianRandomWalk(
            f"{name}__betas_mu", mu=0.0, sigma=tau, init_dist=init, dims=[spl]
        )
        sd = pm.HalfNormal(f"{name}__betas_sd", prior.group_variablility, dims=[spl])
        rv_dims = pan.rv_dims(rand)
        if tv.grouping_map is not None:
            (dim,) = rand
            groups = list(tv.grouping_map)
            group_dim = f"{name}__{tv.grouping_name}"
            model.add_coord(group_dim, groups)
            levels = [str(v) for v in ctx.fit_levels(dim)]
            gm = np.asarray(
                [[1.0 if lv in tv.grouping_map[g] else 0.0 for g in groups] for lv in levels]
            )
            if (gm.sum(axis=1) == 0).any():
                lost = [lv for lv, row in zip(levels, gm, strict=True) if not row.any()]
                raise _settings_error(
                    f"{tv.variable_name}: levels {lost[:5]} of {dim!r} are in no group of "
                    "grouping_map"
                )
            g_betas = pm.Normal(f"{name}__betas_group", mu=mu, sigma=sd, dims=[group_dim, spl])
            g_sd = pm.HalfNormal(f"{name}__betas_group_sd", prior.partial_pooling, dims=[group_dim])
            gmt = pt.as_tensor_variable(gm)
            betas = pm.Normal(
                f"{name}__betas",
                mu=pt.dot(gmt, g_betas),
                sigma=pt.dot(gmt, g_sd)[:, None],
                dims=[*rv_dims, spl],
            )
        else:
            betas = pm.Normal(f"{name}__betas", mu=mu, sigma=sd, dims=[*rv_dims, spl])

        def pooled(t_: Any, axis: int) -> Any:
            shape = [1] * (len(rand) + 1)
            shape[-1] = n_spl
            return pt.reshape(mu, shape) * pt.ones_like(pt.take(t_, pt.arange(1), axis=axis))

        betas = pan.extend(betas, rand, fill=pooled, what=tv.variable_name)
        out = pt.tensordot(betas, b, axes=[[len(rand)], [1]])  # (*rand, time)
        return pan.broadcast(out, rand, trailing=1)

    # ------------------------------------------------------------------ likelihood
    def _likelihood(
        self, pan: _Panel, lik: Any, eta: Any
    ) -> tuple[Callable[[Any], Any], Callable[[str, np.ndarray, list[str]], Any]]:
        """``(mean_of, observe)``: the KPI's expected value as a function of the linear
        predictor, and the observation model."""
        disp = lik.dispersion
        value: Any = None
        if disp is not None:
            pname, dist, params = disp
            ddims = pan.of(lik.dispersion_dims) if pname != "psi" else []
            kw: dict[str, Any] = {"dims": pan.rv_dims(ddims)} if ddims else {}
            value = getattr(pm, dist)(f"{TARGET}__{pname}", **params, **kw)
            if ddims:
                value = pan.extend(value, ddims, fill="mean", what=pname)
                value = pan.broadcast(value, ddims)[..., None]
        kind = lik.type
        psi: Any = None
        if kind == "ZeroInflatedNegativeBinomial":
            psi = pm.Beta(f"{TARGET}__psi", alpha=1.0, beta=1.0)
        elif kind == "ZeroInflatedPoisson":
            psi = value

        def rate(e: Any) -> Any:
            return pt.exp(pt.clip(e, -_CLIP, _CLIP))

        if kind == "Normal":
            return (lambda e: e), (
                lambda n, obs, dims: pm.Normal(n, mu=eta, sigma=value, observed=obs, dims=dims)
            )
        if kind == "LogNormal":
            return (lambda e: pt.exp(e + value**2 / 2)), (
                lambda n, obs, dims: pm.LogNormal(n, mu=eta, sigma=value, observed=obs, dims=dims)
            )
        if kind == "Poisson":
            return rate, (lambda n, obs, dims: pm.Poisson(n, mu=rate(eta), observed=obs, dims=dims))
        if kind == "NegativeBinomial":
            return rate, (
                lambda n, obs, dims: pm.NegativeBinomial(
                    n, mu=rate(eta), alpha=value, observed=obs, dims=dims
                )
            )
        if kind == "ZeroInflatedPoisson":
            return (lambda e: psi * rate(e)), (
                lambda n, obs, dims: pm.ZeroInflatedPoisson(
                    n, psi=psi, mu=rate(eta), observed=obs, dims=dims
                )
            )
        return (lambda e: psi * rate(e)), (
            lambda n, obs, dims: pm.ZeroInflatedNegativeBinomial(
                n, psi=psi, mu=rate(eta), alpha=value, observed=obs, dims=dims
            )
        )

    @staticmethod
    def _contributions(
        stacked: Any, eta: Any, mu: Any, base: int, mean_of: Callable[[Any], Any], lik: Any
    ) -> Any:
        """Contributions in KPI units that sum to ``mu`` (see the module doc)."""
        if lik.type == "Normal":
            return stacked
        eta0 = stacked[base]
        y0 = mean_of(eta0)
        change = eta - eta0
        safe = pt.switch(pt.abs(change) > 1e-9, change, 1.0)
        weight = pt.switch(pt.abs(change) > 1e-9, (mu - y0) / safe, mu)
        out = stacked * weight[None]
        return pt.set_subtensor(out[base], y0)

    @staticmethod
    def _check_observed(observed: np.ndarray, kind: str) -> None:
        bad = ~np.isfinite(observed)
        if bad.any():
            raise _schema_error(
                f"the KPI has {int(bad.sum())} missing value(s); impute or filter them in the "
                "pipeline",
                "/data/y",
            )
        if kind == "LogNormal" and (observed <= 0).any():
            raise _schema_error("a LogNormal likelihood needs a positive KPI", "/data/y")
        if kind not in ("Normal", "LogNormal") and (
            (observed < 0).any() or not np.allclose(observed, np.round(observed))
        ):
            raise _schema_error(
                f"a {kind} likelihood needs a KPI of non-negative whole numbers (counts)",
                "/data/y",
            )

    # ------------------------------------------------------------------ lift tests
    def _lift_likelihood(
        self,
        lift: _LiftDesign,
        handles: Sequence[tuple[Any, Any, dict[str, Any], BIMedia]],
        eta: Any,
        mean_of: Callable[[Any], Any],
    ) -> None:
        rows: list[Any] = []
        for j, (coef, w, curve, m) in enumerate(handles):
            sel = np.flatnonzero(lift.media == j)
            if not sel.size:
                continue
            z0 = pt.as_tensor_variable(lift.z0[sel])
            z1 = pt.as_tensor_variable(lift.z1[sel])
            valid = pt.as_tensor_variable(lift.valid[sel].astype(np.float64))
            gain = (
                (media_curve(z1, m.media_transform, curve, xp=pt)
                 - media_curve(z0, m.media_transform, curve, xp=pt)) * valid * w
            ).sum(axis=-1)  # fmt: skip
            cell = tuple(c[sel] for c in lift.cell)
            d_eta = (coef[cell] if cell else coef) * gain
            e0 = eta[(*cell, lift.time[sel])]
            delta = (mean_of(e0 + d_eta) - mean_of(e0)) * lift.target_scale[sel]
            rows.append((sel, delta))
        order = np.concatenate([sel for sel, _ in rows])
        cells = pt.concatenate([d for _, d in rows])
        seg = lift.segments()[:, order]
        modelled = pm.Deterministic(
            f"{LIFT_VAR}__model", pt.dot(pt.as_tensor_variable(seg), cells), dims=LIFT_DIM
        )
        pm.Normal(LIFT_VAR, mu=modelled, sigma=lift.sigma, observed=lift.observed, dims=LIFT_DIM)

    def _lift_design(
        self, tests: Sequence[LiftTest], ds: xr.Dataset, s: BayesInsightSettings, ctx: FitContext
    ) -> _LiftDesign | None:
        """The lift-test cells over the model-stage fit data (``None`` without a usable
        test); problems raise ``SCHEMA_INVALID`` at the test's pointer."""
        pointer = "/calibration/lift_tests"
        stats = self._stats_or_raise()
        panel = list(s.panel)
        times = np.asarray(ds[TIME].values)
        if not np.issubdtype(times.dtype, np.datetime64):
            raise MapiError(
                "lift tests need a date-valued time axis",
                code=ErrorCode.CALIBRATION_UNSUPPORTED,
                pointer=pointer,
            )
        days = times.astype("datetime64[D]")
        cur = {d: np.asarray(ds[d].values) for d in panel}
        shape = tuple(len(cur[d]) for d in panel)
        ones = xr.DataArray(
            np.ones((*shape, times.size)),
            dims=[*panel, TIME],
            coords={**cur, TIME: times},
        )
        target_scale = _linear_scale(ctx.to_original, ones, TARGET)
        media = {s.var(m.variable_name): (j, m) for j, m in enumerate(s.model_def.media)}
        by_analytic = {m.variable_name: s.var(m.variable_name) for m in s.model_def.media}
        ids: list[str] = []
        parts: dict[str, list[np.ndarray]] = {
            k: [] for k in ("test", "media", "cell", "time", "z0", "z1", "valid", "ts")
        }
        observed: list[float] = []
        sigma: list[float] = []
        skipped: list[str] = []
        for i, test in enumerate(tests):
            here = f"{pointer}/{i}"
            label = test.id or f"{test.channel}-{test.start.isoformat()}"
            name = by_analytic.get(test.channel, test.channel)
            if name not in media:
                raise _schema_error(
                    f"lift test {label!r}: no media variable {test.channel!r} (media: "
                    f"{', '.join(media)})",
                    here + "/channel",
                )
            j, m = media[name]
            mask = np.ones(shape, dtype=bool)
            if test.geo is not None:
                if not panel:
                    raise _schema_error(
                        f"lift test {label!r} names a geo but the model has no panel dims",
                        here + "/geo",
                    )
                hits = np.asarray([str(v) == test.geo for v in cur[panel[0]].tolist()])
                if not hits.any():
                    raise _schema_error(
                        f"lift test {label!r}: no {panel[0]} {test.geo!r}", here + "/geo"
                    )
                mask = mask & hits.reshape((-1,) + (1,) * (len(panel) - 1))
            window = np.flatnonzero((days >= _day(test.start)) & (days <= _day(test.end)))
            if not window.size:
                if _day(test.start) > days.max():
                    skipped.append(label)
                    ctx.info(f"lift test {label!r} lies after the fitted periods; not used here")
                    continue
                raise _schema_error(
                    f"lift test {label!r}: {test.start} to {test.end} covers no period of the "
                    "model's time axis",
                    here,
                )
            x = self._array(ds, name, [*panel, TIME, s.lag_dim])
            spend_scale = _linear_scale(ctx.to_original, ones, name)
            in_test = mask[..., None] & np.isin(np.arange(times.size), window)
            spend = np.where(in_test, x[..., 0] * spend_scale, 0.0)
            total = float(spend.sum())
            if test.spend_delta < 0 and -test.spend_delta > total * (1 + 1e-9):
                raise _schema_error(
                    f"lift test {label!r} removes {-test.spend_delta:.6g} of spend but the "
                    f"data shows only {total:.6g} on {name!r} in that window",
                    here + "/spend_delta",
                )
            share = spend / total if total > 0 else in_test / in_test.sum()
            dx = test.spend_delta * share / spend_scale  # model units, (*panel, time)
            n_lag = x.shape[-1]
            dx_lag = np.zeros(x.shape)
            for lag in range(n_lag):
                dx_lag[..., lag:, lag] = dx[..., : times.size - lag]
            z0, v0 = self._media_input(x, m, stats["media"][name], stats["labels"], cur)
            z1, v1 = self._media_input(x + dx_lag, m, stats["media"][name], stats["labels"], cur)
            cells = np.argwhere(in_test)  # (row, panel + time)
            at = tuple(cells.T)
            parts["test"].append(np.full(len(cells), len(ids), dtype=np.int64))
            parts["media"].append(np.full(len(cells), j, dtype=np.int64))
            parts["cell"].append(cells[:, :-1])
            parts["time"].append(cells[:, -1])
            parts["z0"].append(z0[at])
            parts["z1"].append(z1[at])
            parts["valid"].append(v0[at] & v1[at])
            parts["ts"].append(target_scale[at])
            ids.append(label)
            observed.append(test.kpi_delta)
            sigma.append(test.sigma)
        if not ids:
            return None
        if len(set(ids)) != len(ids):
            raise _schema_error(
                "lift tests on the same channel and start date need distinct ids", pointer
            )
        cell = np.concatenate(parts["cell"])
        return _LiftDesign(
            ids=ids,
            test=np.concatenate(parts["test"]),
            media=np.concatenate(parts["media"]),
            cell=tuple(cell[:, k] for k in range(len(panel))),
            time=np.concatenate(parts["time"]),
            z0=np.concatenate(parts["z0"]),
            z1=np.concatenate(parts["z1"]),
            valid=np.concatenate(parts["valid"]),
            target_scale=np.concatenate(parts["ts"]),
            observed=np.asarray(observed, dtype=np.float64),
            sigma=np.asarray(sigma, dtype=np.float64),
            skipped=skipped,
        )

    # ------------------------------------------------------------------ fit / score
    def fit(
        self, ds: xr.Dataset, s: BayesInsightSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        self._stats = self._fit_stats(ds, s)
        cal = ctx.calibration
        record: dict[str, Any] = {}
        try:
            if cal is not None and cal.lift_tests:
                self._lift = self._lift_design(cal.lift_tests, ds, s, ctx)
                record["lift_tests"] = {
                    "used": [] if self._lift is None else list(self._lift.ids),
                    "skipped": [] if self._lift is None else list(self._lift.skipped),
                }
            setattr(self, "prior_overlap_vars", _overlap_vars(s))  # noqa: B010 - per fit
            out = super().fit(ds, s, cfg, ctx)
        finally:
            stats, self._stats, self._lift = self._stats, None, None
        out.learned[_STATS_KEY] = stats
        if record:
            out.learned["calibration"] = record
            ll = out.groups.get("log_likelihood")
            if ll is not None and LIFT_VAR in ll:
                out.groups["log_likelihood"] = ll.drop_vars(LIFT_VAR)
        post = out.groups["posterior"]
        # time-indexed deterministics are recomputed at score time
        heavy = [str(v) for v in post.data_vars if TIME in post[v].dims]
        out.groups["posterior"] = post.drop_vars(heavy)
        out.learned["dropped_deterministics"] = list(sorted(heavy))
        return out

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        stats = fitted.learned.get(_STATS_KEY)
        if not isinstance(stats, dict):
            raise MapiError(
                "the fitted model has no BayesInsight statistics; refit it",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        self._stats = stats
        try:
            return super().score(fitted, ds, req, ctx)
        finally:
            self._stats = None


def _rv_name(label: str) -> str:
    """A parameter-name prefix for a season or trend (their names are free text)."""
    import re

    out = re.sub(r"[^A-Za-z0-9_]+", "_", label).strip("_") or "term"
    return out if out[0].isalpha() or out[0] == "_" else f"v_{out}"


def _overlap_vars(s: BayesInsightSettings) -> tuple[str, ...]:
    """The media parameters whose prior/posterior overlap the scorecard reports."""
    out: list[str] = []
    for m in s.model_def.media:
        name = s.var(m.variable_name)
        out.append(f"{name}__coef")
        if m.media_transform == "hill":
            out += [f"{name}__K", f"{name}__n_"]
        elif m.media_transform == "sshaped":
            out += [f"{name}__s_alpha", f"{name}__s_beta_"]
        if m.adstock in ("delayed", "geometric"):
            out.append(f"{name}__retention")
        if m.adstock == "delayed":
            out.append(f"{name}__lag")
        if m.adstock == "weibull":
            out += [f"{name}__w_lam", f"{name}__w_k"]
    return tuple(out)
