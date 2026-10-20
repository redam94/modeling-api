"""``causal.impact``: CausalImpact / interrupted time series on the ``ts.bsts`` core.

Canonical inputs::

    dims  time: time, extend
    vars  y(time): target, float64, missing allowed
          {*controls}(time): float64 covariates known for every period (future required)

The Bayesian structural time series of :mod:`mapi_plugin_ts.bsts` (trend, seasonality and a
static regression on the control series) is fitted to the **pre-period only** (the periods
before ``intervention_start``). The counterfactual - what ``y`` would have been without the
intervention - is the one-step-ahead prediction inside the pre-period and a forecast from
the filtered state at the end of the pre-period, driven by the observed controls, after it
(Brodersen et al. 2015). Both are predictive draws with common random numbers seeded by
``(fit seed, "counterfactual")``.

Bundle groups besides the posterior: ``impact`` (actual, counterfactual, pointwise and
cumulative effect, the window mask), ``components`` and ``one_step`` over the pre-period and
the ``terminal_state`` at its end.

**Score** (outputs ``y_hat(chain, draw, time)`` and ``effect(chain, draw, time)``):
``predict`` gives the counterfactual over the payload periods (one-step-ahead before the
start, the forecast with the payload's controls from the start on) and the effect
``y - y_hat`` when the payload has ``y`` (left out when it has none); ``forecast`` does
the same past the data.

The counterfactual is only as good as the controls: they must not be affected by the
intervention. Pointwise LOO is not offered (the Kalman likelihood is a sum over the series).
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

import numpy as np
import pandas as pd
import pymc as pm
import xarray as xr
from mapi_plugin_ts.bsts import (
    BSTSFit,
    StructureSettings,
    Terminal,
    check_regular,
    components_group,
    fit_bsts,
    fit_metrics,
    forecast,
    one_step_group,
    read_terminal,
    terminal_group,
)
from mapi_plugin_ts.ss_math import one_step_draws
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_sdk.models import (
    DIFFERENCE_ATTR,
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ScoreContext,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, field_validator

from mapi_plugin_causal._common import (
    IMPACT,
    SAMPLE,
    TARGET,
    TIME,
    CausalBase,
    CausalSettings,
    crn_normal,
    effect_summary,
    impact_group,
    log_effect_summary,
    parse_window,
    time_label,
)

__all__ = ["CONTROLS", "ONE_STEP", "TERMINAL_STATE", "CausalImpactModel", "ImpactSettings"]

ONE_STEP: Final = "one_step"
COMPONENTS: Final = "components"
TERMINAL_STATE: Final = "terminal_state"
CONTROLS: Final = "controls"
CF_NAME: Final = "counterfactual"
_RESERVED = frozenset({TARGET, TIME, SAMPLE, "chain", "draw"})


class ImpactSettings(StructureSettings, CausalSettings):
    """Settings of ``causal.impact``: the BSTS structure, the window and the controls."""

    controls: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="Control series that the intervention did not affect (other regions, "
        "competitors, search interest); their values are needed for every period",
        json_schema_extra={"x-ui-level": "basic"},
    )

    @field_validator("controls")
    @classmethod
    def _controls(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("controls must not repeat a variable")
        bad = sorted({x for x in v if x in _RESERVED or not x.isidentifier()})
        if bad:
            raise ValueError(f"controls cannot use the names {bad}")
        return v


_TEMPLATE: dict[str, Any] = {
    "params": {CONTROLS: {"type": "string_list", "default": []}},
    "template": {
        "dims": {TIME: {"kind": "time", "policy": "extend"}},
        "vars": {
            TARGET: {"dims": [TIME], "dtype": "float64", "role": "target", "missing": "allow"},
            "{*controls as x}": {
                "dims": [TIME],
                "dtype": "float64",
                "role": "covariate",
                "future_required": True,
            },
        },
    },
}


class CausalImpactModel(CausalBase, CustomModel[ImpactSettings]):
    """CausalImpact: a BSTS fitted before the intervention predicts what happened after it."""

    id = "causal.impact"
    version = "1.0.0"
    Settings = ImpactSettings
    description = (
        "CausalImpact / interrupted time series: a Bayesian structural time series fitted on "
        "the periods before an intervention, with control series as regressors, predicts the "
        "series without the intervention; the effect is actual minus that counterfactual."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast"}),
        loo_valid=False,
        loo_invalid_reason="the Kalman likelihood is a sum over the whole series; steps depend "
        "on each other, so pointwise leave-one-out is not defined",
        owns=frozenset({"scaling"}),
        deterministic_given_seed=False,
        checkpointable=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [TIME],
            "units_of": TARGET,
            "description": "What y would be without the intervention",
        },
        "effect": {
            "dims": [TIME],
            "units_of": TARGET,
            "description": "The effect of the intervention: y minus what y would be without it",
        },
    }
    plots = (
        "causal.impact",
        "causal.pre_fit",
        "core.decomposition",
        "core.convergence",
        "core.posterior_forest",
    )
    pad_extend = False
    default_engine: ClassVar[str] = "nutpie"

    # ================================================================== fit
    def fit(self, ds: xr.Dataset, s: ImpactSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        times = np.asarray(ds[TIME].values)
        self.check_time(times)
        check_regular(times)  # the filter and the forecast count periods by position
        window = parse_window(times, s.intervention_start, s.intervention_end)
        start = window.start
        y = np.asarray(ds[TARGET].values, dtype=np.float64)
        X = _controls(ds, s.controls)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        pre = ds.isel({TIME: slice(0, start)})
        fit = fit_bsts(
            y[:start],
            None if X is None else X[:start],
            s.controls,
            s,
            cfg,
            ctx,
            model_id=self.id,
            version=self.version,
            data=pre,
        )
        ctx.check_cancelled()
        return self.assemble(ds, s, fit, seed=seed, ctx=ctx)

    def assemble(
        self, ds: xr.Dataset, s: ImpactSettings, fit: BSTSFit, *, seed: int, ctx: FitContext
    ) -> FitOutput:
        """Everything after the pre-period fit: counterfactual, groups, tracking, learned.

        Tests call it with a :class:`~mapi_plugin_ts.bsts.BSTSFit` built from prior draws.
        """
        times = np.asarray(ds[TIME].values)
        window = parse_window(times, s.intervention_start, s.intervention_end)
        n, start = times.size, window.start
        y = np.asarray(ds[TARGET].values, dtype=np.float64)
        X = _controls(ds, s.controls)
        ctx.progress(0.9, "computing the counterfactual")
        last_pre = time_label(times[start - 1])
        terminal = terminal_group(fit, last_pre)
        term = read_terminal(terminal)
        cf_pre = _unscale(_pre_draws(fit, seed), term)
        _, cf_post = forecast(
            term, None if X is None else X[start:], n - start, seed=seed, name=CF_NAME
        )
        cf = np.concatenate([cf_pre, cf_post], axis=1)  # (S, n) model-stage target units
        actual, cf = _to_target(ctx, y, cf, times, fit)
        impact = impact_group(
            times,
            actual,
            cf,
            window.post_mask(n),
            units=None,
            window=window,
            model_ref=f"{self.id}@{self.version}",
            units_label=_units(ctx),
        )
        engine = fit.result.engine if fit.result is not None else "prior"
        groups: dict[str, xr.Dataset] = {"posterior": _clean(fit.posterior, engine)}
        if fit.sample_stats is not None:
            stats = fit.sample_stats
            keep = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[keep], engine)
        coords = {TIME: times}
        groups["observed_data"] = xr.Dataset({TARGET: ((TIME,), y)}, coords=coords)
        if X is not None:
            groups["constant_data"] = xr.Dataset(
                {name: ((TIME,), X[:, j]) for j, name in enumerate(s.controls)}, coords=coords
            )
        groups[IMPACT] = impact
        groups[COMPONENTS] = _components_to_target(components_group(fit, times[:start], TIME), ctx)
        groups[ONE_STEP] = one_step_group(fit, times[:start], TIME)
        summary = effect_summary(impact)
        log_effect_summary(ctx, impact, params=_bsts_params(s, fit, start, y[:start]))
        if ctx.tracking:
            ctx.log_metrics(fit_metrics(fit, y[:start]))
        ctx.progress(1.0, "posterior ready")
        learned: dict[str, Any] = {
            **fit.learned,
            "controls": list(s.controls),
            "intervention_start": window.start_label,
            "intervention_end": window.end_label,
            "n_pre": start,
            "n_data": n,
            "first_label": time_label(times[0]),
            "last_label": time_label(times[-1]),
            "last_pre_label": last_pre,
            "longest_season": max((x.period for x in s.seasonality), default=None),
            "effect": _json_summary(summary),
        }
        return FitOutput(groups=groups, learned=learned, terminal_state=terminal)

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        post = fitted.groups.get("posterior")
        if post is not None and {"chain", "draw"} <= set(post.dims):
            free = [str(v) for v in post.data_vars if TIME not in post[v].dims]
            out.extend(
                convergence_diagnostics(post, fitted.groups.get("sample_stats"), var_names=free)
            )
        season = fitted.learned.get("longest_season")
        n_pre = fitted.learned.get("n_pre")
        if isinstance(season, (int, float)) and isinstance(n_pre, int) and n_pre < 2 * season:
            out.append(
                Diagnostic(
                    code="SHORT_PRE_PERIOD",
                    severity="warning",
                    metric="n_pre",
                    value=float(n_pre),
                    threshold=float(2 * season),
                    message_expert=f"{n_pre} periods precede the intervention, fewer than two "
                    f"cycles of the longest seasonal period ({season:g}); the seasonal pattern "
                    "and the counterfactual are poorly identified",
                    message_plain="The data before the start is short. The model cannot learn "
                    "the seasonal pattern well, so the estimated effect is less certain.",
                    suggested_action="Use more data before the start, or remove the "
                    "seasonality or use fewer harmonics.",
                )
            )
        return out

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        if fitted.terminal_state is None or ONE_STEP not in fitted.groups:
            raise MapiError(
                "the fitted model has no terminal state; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        s = ImpactSettings.model_validate(fitted.settings)
        term = read_terminal(fitted.terminal_state)
        fit_times = _fit_times(fitted)
        start = int(_learned(fitted, "n_pre"))
        times = np.asarray(ds[TIME].values)
        pos = _positions(fit_times, times)
        ctx.check_cancelled()
        S = term.chain.size * term.draw.size
        y_hat = np.empty((S, times.size))
        before = pos < start
        if before.any():
            y_hat[:, before] = self._one_step(fitted, term, pos[before], ctx)
        after = ~before
        if after.any():
            last = int(pos[after].max())
            h = last - start + 1
            X = self._future_controls(fitted, ds, s.controls, pos, start, h)
            expected, predictive = forecast(term, X, h, seed=ctx.seed, name=CF_NAME)
            fc = predictive if ctx.kind == "predictive" else expected
            y_hat[:, after] = fc[:, pos[after] - start]
        shape = (term.chain.size, term.draw.size, times.size)
        dims = ("chain", "draw", TIME)
        out = {"y_hat": xr.DataArray(y_hat.reshape(shape), dims=dims, attrs={"kind": ctx.kind})}
        if TARGET in ds:  # the effect needs the actual values (absent in forecasts)
            effect = np.asarray(ds[TARGET].values, dtype=np.float64)[None, :] - y_hat
            # a difference, not a level of y: the harness inverts y and y_hat through the
            # target's transforms and subtracts them in the output space
            out["effect"] = xr.DataArray(
                effect.reshape(shape),
                dims=dims,
                attrs={"kind": ctx.kind, DIFFERENCE_ATTR: "y_hat"},
            )
        predictions = xr.Dataset(out, coords={"chain": term.chain, "draw": term.draw, TIME: times})
        return xr.DataTree.from_dict({PREDICTIONS: predictions})

    def _one_step(
        self, fitted: Fitted, term: Terminal, pos: np.ndarray, ctx: ScoreContext
    ) -> np.ndarray:
        """Pre-period one-step-ahead counterfactual ``(S, len(pos))`` (model-stage units)."""
        g = fitted.groups[ONE_STEP]
        mean = np.asarray(g["mean"].transpose("chain", "draw", TIME).values)
        var = np.asarray(g["var"].transpose("chain", "draw", TIME).values)
        mean = mean.reshape(-1, mean.shape[-1])[:, pos]
        var = var.reshape(-1, var.shape[-1])[:, pos]
        if ctx.kind == "predictive":
            # the normals of one_step_draws: with the fit seed this repeats the fit's draws
            z = crn_normal(ctx.seed, CF_NAME, (mean.shape[0], g.sizes[TIME]))
            mean = mean + np.sqrt(np.maximum(var, 0.0)) * z[:, pos]
        return _unscale(mean, term)

    def _future_controls(
        self,
        fitted: Fitted,
        ds: xr.Dataset,
        names: list[str],
        pos: np.ndarray,
        start: int,
        h: int,
    ) -> np.ndarray | None:
        """Controls ``(h, p)`` for the positions ``start .. start + h - 1``.

        Payload values win; the fitted controls fill the periods inside the data the payload
        leaves out. A period past the data without a payload value is an error.
        """
        if not names:
            return None
        X = np.full((h, len(names)), np.nan)
        stored = fitted.groups.get("constant_data")
        if stored is not None:
            n_data = stored.sizes[TIME]
            keep = min(n_data - start, h)
            if keep > 0:
                X[:keep] = np.stack(
                    [np.asarray(stored[c].values[start : start + keep]) for c in names], axis=1
                )
        rows = pos - start
        sel = rows >= 0
        for j, name in enumerate(names):
            if name in ds:
                X[rows[sel], j] = np.asarray(ds[name].values, dtype=np.float64)[sel]
        if not np.isfinite(X).all():
            missing = [names[j] for j in range(len(names)) if not np.isfinite(X[:, j]).all()]
            raise MapiError(
                f"the controls {missing} need a value for every period after the start of "
                "the intervention",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer="/data",
            )
        return X


# ====================================================================== helpers


def _controls(ds: xr.Dataset, names: list[str]) -> np.ndarray | None:
    """The control series ``(n, p)``; every value must be finite."""
    if not names:
        return None
    missing = [c for c in names if c not in ds]
    if missing:
        raise MapiError(
            f"the controls {missing} are not in the data",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/model/settings/controls",
        )
    X = np.stack([np.asarray(ds[c].values, dtype=np.float64) for c in names], axis=1)
    if not np.isfinite(X).all():
        bad = [names[j] for j in range(X.shape[1]) if not np.isfinite(X[:, j]).all()]
        raise MapiError(
            f"the controls {bad} have missing values; controls need a value for every period",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/data",
        )
    return X


def _pre_draws(fit: BSTSFit, seed: int) -> np.ndarray:
    """Predictive one-step-ahead draws ``(S, n_pre)`` (standardised)."""
    return one_step_draws(fit.filt, fit.d, seed=seed, name=CF_NAME)


def _unscale(v: np.ndarray, term: Terminal) -> np.ndarray:
    return term.scaling.y_loc + term.scaling.y_scale * v


def _clean(ds: xr.Dataset, engine: str) -> xr.Dataset:
    """Drop the engine's attrs; record the inference library (like ``ts.bsts``)."""
    out = ds.copy()
    for v in out.variables:
        out[v].attrs = {}
    out.attrs = {
        "inference_library": "pymc",
        "inference_library_version": pm.__version__,
        "engine": engine,
    }
    return out


def _components_to_target(ds: xr.Dataset, ctx: FitContext) -> xr.Dataset:
    """Components in target units: running sums mapped back, then differenced (the parts
    still add up when the pipeline's inverse is not linear)."""
    if ctx.to_original is None:
        return ds
    out: dict[str, xr.Variable] = {}
    running: xr.DataArray | None = None
    prev: xr.DataArray | None = None
    for name in ds.data_vars:
        running = ds[name] if running is None else running + ds[name]
        mapped = ctx.to_original(running, TARGET)
        part = mapped if prev is None else mapped - prev
        out[str(name)] = part.variable.copy()
        out[str(name)].attrs = {}
        prev = mapped
    return xr.Dataset(out, coords=ds.coords)


def _to_target(
    ctx: FitContext, y: np.ndarray, cf: np.ndarray, times: np.ndarray, fit: BSTSFit
) -> tuple[np.ndarray, np.ndarray]:
    """Actual and counterfactual in the units of the target (``ctx.to_original``)."""
    if ctx.to_original is None:
        return y, cf
    grid = (fit.chain.size, fit.draw.size, times.size)
    da = xr.DataArray(
        cf.reshape(grid),
        dims=("chain", "draw", TIME),
        coords={"chain": fit.chain, "draw": fit.draw, TIME: times},
    )
    cf_out = ctx.to_original(da, TARGET).transpose("chain", "draw", TIME).values
    actual = ctx.to_original(xr.DataArray(y, dims=(TIME,), coords={TIME: times}), TARGET)
    return np.asarray(actual.values, dtype=np.float64), np.asarray(cf_out).reshape(cf.shape)


def _units(ctx: FitContext) -> str | None:
    schema = ctx.schema
    if schema is None or TARGET not in schema.vars:
        return None
    return getattr(schema.vars[TARGET], "units", None)


def _bsts_params(
    s: ImpactSettings, fit: BSTSFit, n_pre: int, y_pre: np.ndarray
) -> dict[str, str | int]:
    """The I-10 ``bsts.*`` params of the pre-period fit."""
    return {
        "bsts.trend": s.trend,
        "bsts.seasonality": ",".join(x.label for x in s.seasonality),
        "bsts.regressors": ",".join(s.controls),
        "bsts.n_states": len(fit.state_names),
        "bsts.n_periods": n_pre,
        "bsts.missing_periods": int(np.isnan(y_pre).sum()),
    }


def _json_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """``effect_summary`` without ``by_unit``, NaN as ``None`` (JSON)."""

    def clean(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        if isinstance(v, float) and not np.isfinite(v):
            return None
        return v

    return clean({k: v for k, v in summary.items() if k != "by_unit"})


def _learned(fitted: Fitted, key: str) -> Any:
    value = fitted.learned.get(key)
    if value is None:
        raise MapiError(
            f"the fitted model has no {key!r}; refit the model", code=ErrorCode.INTEGRITY_FAILURE
        )
    return value


def _fit_times(fitted: Fitted) -> np.ndarray:
    """The time coords of the fitted data (pre- and post-period)."""
    obs = fitted.groups.get("observed_data")
    if obs is not None and TIME in obs.coords:
        return np.asarray(obs[TIME].values)
    if TIME in fitted.coords:
        return np.asarray(fitted.coords[TIME])
    raise MapiError(
        "the fitted model has no time coordinates; refit the model",
        code=ErrorCode.INTEGRITY_FAILURE,
    )


def _positions(fit_times: np.ndarray, times: np.ndarray) -> np.ndarray:
    """Positions of ``times`` on the fitted time axis, extended past its end."""
    n = fit_times.size
    extra = times.size + 1
    if np.issubdtype(fit_times.dtype, np.datetime64):
        idx = pd.DatetimeIndex(fit_times)
        freq = pd.infer_freq(idx) if n >= 3 else None
        if freq is not None:
            ext = pd.date_range(idx[-1], periods=extra + 1, freq=freq)[1:]
        else:
            step = idx[-1] - idx[-2] if n >= 2 else pd.Timedelta(days=1)
            ext = pd.DatetimeIndex([idx[-1] + step * (k + 1) for k in range(extra)])
        axis = np.concatenate([idx.values, ext.as_unit("ns").values]).astype("datetime64[ns]")
        want = np.asarray(times).astype("datetime64[ns]")
    else:
        step = fit_times[1] - fit_times[0] if n >= 2 else 1
        axis = np.concatenate([fit_times, fit_times[-1] + step * np.arange(1, extra + 1)])
        want = np.asarray(times)
    lookup = {v: i for i, v in enumerate(axis.tolist())}
    out = np.empty(want.size, dtype=np.int64)
    for i, v in enumerate(want.tolist()):
        p = lookup.get(v)
        if p is None:
            raise MapiError(
                f"the period {time_label(want[i])} is not on the time axis of the fit "
                "(before the data, or not a regular step after it)",
                code=ErrorCode.COORD_NOT_ALLOWED,
                pointer="/data",
            )
        out[i] = p
    return out
