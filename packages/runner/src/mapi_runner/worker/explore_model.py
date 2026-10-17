"""The Model Explorer (M3.5): one :mod:`mapi_spec.views` document per section, computed in
the sandbox from a fitted bundle and the data it was fitted on, for every plugin family.

==================== ========================================================================
section              source
==================== ========================================================================
``params``           :func:`~mapi_runner.worker.modelview.param_rows` (posterior, ``params``/
                     ``coefficients`` group or ``learned["params"]``)
``diagnostics``      convergence (R-hat, ESS, divergences, optimiser status), information
                     criteria, holdout metrics and the plugin's diagnostics
``fit``              in-sample predictions through the score path, with a 90% band and the
                     split line at the first held-out period
``residuals``        actual - fitted over time, their ACF, Ljung-Box, Jarque-Bera,
                     Breusch-Pagan and ARCH-LM tests, histogram and normal QQ points
``decomposition``    the model's ``component`` output (contributions) summed over panel dims
``response_curves``  the fit's ``response_curves`` grid (mean and 90% band per channel)
``irf`` / ``fevd``   a plugin's ``irf``/``fevd`` group or derived quantity (VAR/VECM)
``forecast``         a ``forecast`` score over ``horizon`` periods (covariates carried on)
``backtest``         rolling-origin / expanding-window refits with naive benchmarks
``comparison``       holdout metrics of several versions, a ranking and Diebold-Mariano tests
==================== ========================================================================

:data:`FIT_SECTIONS` are written by every fit (``ui/explore/``); the rest on demand. A
plugin that computes its own views (``explorer_views(fitted)`` on the model class, such as
the statsmodels plugin) supplies the sections of :data:`PLUGIN_SECTIONS` it returns; the
generic implementation is the fallback.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import score_bundle
from mapi_sdk.metrics import point_estimate
from mapi_sdk.registry import Registry
from mapi_spec.calibration import FanChart
from mapi_spec.errors import MapiError
from mapi_spec.explore import ModelExploreRequest
from mapi_spec.spec import ScoreRequest
from mapi_spec.splitplan import ExpandingWindowPlan, RollingOriginPlan, plan_folds
from mapi_spec.stattests import StatTestResult
from mapi_spec.views import (
    MAX_EXPLORE_POINTS,
    MAX_EXPLORE_SERIES,
    BacktestFoldResult,
    BacktestView,
    ComparisonView,
    DecompositionView,
    FevdView,
    FitVsActualView,
    ForecastView,
    Histogram,
    IrfView,
    LagProfile,
    ModelDiagnosticsView,
    ModelForecastView,
    ParamTableView,
    QQPoints,
    ResidualDiagnosticsView,
    ResponseCurvesView,
    SeriesView,
)
from pydantic import BaseModel

from mapi_runner.worker import stats
from mapi_runner.worker.explore_data import SectionSkipped, explore_period, jsonable
from mapi_runner.worker.modelview import (
    Prediction,
    bundle_kind,
    convergence_stats,
    holdout_mask,
    param_rows,
    predict,
    supplied_tables,
    target_vars,
    time_dim_of,
)
from mapi_runner.worker.refit import Refitter

__all__ = [
    "ERROR_METRICS",
    "FIT_SECTIONS",
    "MODEL_SECTION_FUNCS",
    "ModelContext",
    "error_metrics",
]

FIT_SECTIONS: Final = (
    "params",
    "diagnostics",
    "fit",
    "residuals",
    "decomposition",
    "response_curves",
    "irf",
    "fevd",
)
"""Sections every fit writes (they read the bundle and one in-sample score)."""
ERROR_METRICS: Final = ("rmse", "mae", "mape", "smape", "wape", "mase", "crps")
"""Metrics where lower is better (rankings sort them ascending)."""
MAX_BACKTEST_FOLDS: Final = 10


def _fv(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


@dataclass
class ModelContext:
    """A bundle prepared for exploration."""

    bundle: Bundle
    data: xr.Dataset
    registry: Registry
    req: ModelExploreRequest = field(default_factory=ModelExploreRequest)
    ref: str = "model://runner/model@latest"
    side_tables: dict[str, xr.Dataset] = field(default_factory=dict)
    compare: list[tuple[str, Bundle]] = field(default_factory=list)
    evaluation: Bundle | None = None
    refitter: Refitter | None = None
    _pred: list[Prediction] | None = None
    _plugin: dict[str, BaseModel] | None = None
    plugin_error: str | None = None

    def plugin_views(self) -> dict[str, BaseModel]:
        """The plugin's own Model Explorer sections (``explorer_views(fitted)`` on the model
        class, e.g. the statsmodels plugin's parameter, test, IRF and FEVD views); empty
        when the plugin has none or they fail (the generic sections are used then)."""
        if self._plugin is None:
            self._plugin = {}
            man = self.bundle.model.manifest
            try:
                cls = self.registry.model(f"{man.id}@{man.version}")
                hook = getattr(cls(), "explorer_views", None)
                if callable(hook):
                    views = hook(self.bundle.model)
                    if isinstance(views, dict):
                        self._plugin = {
                            str(k): v for k, v in views.items() if isinstance(v, BaseModel)
                        }
            except Exception as exc:
                self.plugin_error = f"{type(exc).__name__}: {exc}"[:2000]
        return self._plugin

    @property
    def time_dim(self) -> str | None:
        return time_dim_of(self.bundle, self.data)

    def predictions(self) -> list[Prediction]:
        if self._pred is None:
            self._pred = predict(
                self.bundle, self.data, self.registry, side_tables=self.side_tables
            )
            if not self._pred:
                raise SectionSkipped("the model's predictions do not line up with its target")
        return self._pred


def _need_time(c: ModelContext) -> str:
    t = c.time_dim
    if t is None:
        raise SectionSkipped("this view needs a time dimension")
    return t


def _series(da: xr.DataArray, t: str) -> list[tuple[dict[str, Any], np.ndarray]]:
    others = [str(d) for d in da.dims if d != t]
    arr = np.asarray(da.transpose(t, *others).values, dtype=float)
    if not others:
        return [({}, arr)]
    labels = [
        [jsonable(v) for v in np.asarray(da[d].values)]
        if d in da.coords
        else list(range(da.sizes[d]))
        for d in others
    ]
    out: list[tuple[dict[str, Any], np.ndarray]] = []
    for idx in itertools.product(*[range(len(lv)) for lv in labels]):
        facet = {d: lv[i] for d, lv, i in zip(others, labels, idx, strict=True)}
        sel: tuple[Any, ...] = (slice(None), *idx)
        out.append((facet, arr[sel]))
    return out


def _down(values: Sequence[Any], n: int) -> list[int]:
    if n <= MAX_EXPLORE_POINTS:
        return list(range(n))
    return [int(i) for i in np.linspace(0, n - 1, MAX_EXPLORE_POINTS).round()]


def _coords(da: xr.DataArray, t: str) -> list[Any]:
    return (
        [jsonable(v) for v in np.asarray(da[t].values)]
        if t in da.coords
        else list(range(da.sizes[t]))
    )


# --------------------------------------------------------------------------- sections


def params(c: ModelContext) -> ParamTableView:
    rows, omitted, label = param_rows(
        c.bundle, interval=c.req.interval, max_params=c.req.max_params
    )
    if not rows:
        raise SectionSkipped(
            "this model stores no parameter estimates (machine-learning models "
            "have no interpretable parameters)"
        )
    kind = bundle_kind(c.bundle)
    cap = {
        "bayesian": "Each estimate's posterior mean with its highest-density interval.",
        "frequentist": "Each estimate with its confidence interval and p-value.",
        "ml": "The fitted coefficients or importances (no uncertainty).",
    }[kind]
    return ParamTableView(interval=label, rows=rows, omitted=omitted, caption_plain=cap)


def diagnostics(c: ModelContext) -> ModelDiagnosticsView:
    b = c.bundle
    stats_: dict[str, float | None] = dict(convergence_stats(b))
    for k, v in b.metrics.values.items():
        stats_[f"holdout_{k}"] = _fv(v)
    items = [d.model_dump(mode="json", exclude_none=True) for d in b.diagnostics[:200]]
    warn: list[str] = []
    r = stats_.get("r_hat_max")
    if r is not None and r > 1.01:
        warn.append(f"The chains disagree (R-hat {r:.3f} > 1.01): run longer or reparameterise.")
    div = stats_.get("divergences")
    if div:
        warn.append(f"{int(div)} divergent transitions: estimates may be biased.")
    if stats_.get("converged") == 0.0:
        warn.append("The optimiser did not converge: estimates are unreliable.")
    return ModelDiagnosticsView(
        inference=b.model.manifest.capabilities.inference,
        stats=stats_,
        items=items,
        warnings_plain=warn,
        caption_plain="Whether the fit finished properly, and how well it does on held-out data.",
    )


def fit(c: ModelContext) -> FitVsActualView:
    t = _need_time(c)
    mask = holdout_mask(c.bundle, c.data, c.evaluation)
    split_at = None
    if mask is not None:
        dim = str(mask.dims[0])
        held = np.asarray(mask[dim].values)[np.asarray(mask.values)]
        split_at = jsonable(held.min()) if held.size else None
    series: list[SeriesView] = []
    fitted: list[FanChart] = []
    facets: list[dict[str, Any]] = []
    for p in c.predictions():
        obs = _series(p.observed, t)
        pts = _series(p.point, t)
        los = _series(p.lower, t) if p.lower is not None else None
        his = _series(p.upper, t) if p.upper is not None else None
        coords = _coords(p.observed, t)
        idx = _down(coords, len(coords))
        cs = [coords[i] for i in idx]
        for k, ((facet, o), (_f2, pt)) in enumerate(zip(obs, pts, strict=True)):
            if len(series) >= MAX_EXPLORE_SERIES:
                break
            series.append(
                SeriesView(
                    var=p.target,
                    facet=facet,
                    coords=cs,
                    values=[_fv(o[i]) for i in idx],
                    downsampled=len(idx) < len(coords),
                )
            )
            if los is not None and his is not None:
                q = [0.05, 0.5, 0.95]
                vals = [
                    [_fv(los[k][1][i]) for i in idx],
                    [_fv(pt[i]) for i in idx],
                    [_fv(his[k][1][i]) for i in idx],
                ]
            else:
                q, vals = [0.5], [[_fv(pt[i]) for i in idx]]
            fitted.append(FanChart(dim=t, coords=cs, quantiles=q, values=vals))
            facets.append(facet)
    return FitVsActualView(
        dim=t,
        split_at=split_at,
        series=series,
        fitted=fitted,
        facets=facets,
        caption_plain="Actual values against the model's fit; the shaded band is where the "
        "model expects 90% of values. Periods right of the line were held out.",
    )


def residual_tests(
    resid: np.ndarray, fitted: np.ndarray, var: str, *, lags: int = 10, model_df: int = 0
) -> list[StatTestResult]:
    r = np.asarray(resid, dtype=float)
    out = [
        stats.ljung_box_test(r, lags=lags, model_df=model_df, var=var),
        stats.jarque_bera_test(r, var=var),
        stats.breusch_pagan_test(r, fitted, var=var),
        stats.arch_lm_test(r, var=var),
    ]
    return out


def residuals(c: ModelContext) -> ResidualDiagnosticsView:
    t = _need_time(c)
    series: list[SeriesView] = []
    acfs: list[LagProfile] = []
    tests: list[StatTestResult] = []
    pooled: list[np.ndarray] = []
    for p in c.predictions():
        resid = _series(p.resid, t)
        fitted = _series(p.point, t)
        coords = _coords(p.observed, t)
        idx = _down(coords, len(coords))
        for (facet, r), (_f2, f) in zip(resid, fitted, strict=True):
            if len(series) >= MAX_EXPLORE_SERIES:
                break
            label = p.target if not facet else f"{p.target}[{','.join(map(str, facet.values()))}]"
            series.append(
                SeriesView(
                    var=p.target,
                    facet=facet,
                    coords=[coords[i] for i in idx],
                    values=[_fv(r[i]) for i in idx],
                )
            )
            prof = stats.acf_profile(stats.finite(r), min(c.req.horizon or 24, 40))
            acfs.append(
                LagProfile(
                    var=p.target,
                    facet=facet,
                    lags=prof.lags,
                    values=prof.values,
                    bound=prof.bound,
                    peak_lag=prof.peak_lag,
                    significant_lags=prof.significant,
                )
            )
            if len(tests) < 40:
                tests.extend(residual_tests(r, f, label[:256]))
            pooled.append(stats.finite(r))
    allr = np.concatenate(pooled) if pooled else np.array([])
    hist = None
    if allr.size >= 3 and np.ptp(allr) > 0:
        counts, edges = np.histogram(allr, bins=30)
        hist = Histogram(edges=[float(e) for e in edges], counts=[int(k) for k in counts])
    theo, samp = stats.qq_points(allr)
    warn = [
        f"{x.var}: {x.verdict_plain}"
        for x in tests
        if x.status == "ok" and x.reject and x.test == "test.ljung_box"
    ][:10]
    return ResidualDiagnosticsView(
        series=series,
        acf=acfs,
        tests=tests,
        histogram=hist,
        qq=QQPoints(theoretical=theo, sample=samp) if theo else None,
        warnings_plain=warn,
        caption_plain="What the model got wrong over time. Good residuals look like noise: "
        "no pattern, no growing spread.",
    )


def decomposition(c: ModelContext) -> DecompositionView:
    t = _need_time(c)
    preds = c.predictions()
    p = next((x for x in preds if x.contributions is not None), None)
    if p is None or p.contributions is None:
        raise SectionSkipped("this model does not split its predictions into parts")
    da = p.contributions
    if "component" not in da.dims or t not in da.dims:
        raise SectionSkipped("the model's contributions have no time dimension")
    other = [d for d in da.dims if d not in ("component", t)]
    total = da.sum(other) if other else da
    total = total.transpose("component", t)
    coords = _coords(total, t)
    idx = _down(coords, len(coords))
    comps: dict[str, list[float | None]] = {}
    shares: dict[str, float | None] = {}
    vals = np.asarray(total.values, dtype=float)
    grand = float(np.nansum(np.abs(vals)))
    for i, name in enumerate(np.asarray(total["component"].values)[:128]):
        key = str(name)[:128]
        comps[key] = [_fv(vals[i, j]) for j in idx]
        shares[key] = _fv(np.nansum(vals[i]) / grand) if grand > 0 else None
    scale = c.bundle.model.manifest.capabilities.contribution_scale
    return DecompositionView(
        dim=t,
        coords=[coords[j] for j in idx],
        components=comps,
        shares=shares,
        scale="multiplicative" if scale == "multiplicative" else "additive",
        caption_plain="How much each part of the model adds to the prediction over time.",
    )


def response_curves(c: ModelContext) -> ResponseCurvesView:
    from mapi_runner.worker.guided import response_grid_view

    grid = response_grid_view(c.bundle)
    if grid is None:
        raise SectionSkipped(
            "this model has no response curves (marketing mix models keep them when the "
            "spec asks for the derived response_curves)"
        )
    contrib = np.asarray(grid.contribution, dtype=float)  # draw, channel, multiplier
    mean = contrib.mean(axis=0)
    lo = np.quantile(contrib, 0.05, axis=0) if grid.draws > 1 else None
    hi = np.quantile(contrib, 0.95, axis=0) if grid.draws > 1 else None
    return ResponseCurvesView(
        channels=list(grid.channels),
        spend=[[float(v) for v in row] for row in grid.spend],
        mean=[[float(v) for v in row] for row in mean],
        lower=[[float(v) for v in row] for row in lo] if lo is not None else None,
        upper=[[float(v) for v in row] for row in hi] if hi is not None else None,
        current_spend=list(grid.current_spend),
        caption_plain="What each channel adds at different spend levels (diminishing returns "
        "flatten the curve).",
    )


def _group(c: ModelContext, name: str) -> xr.Dataset | None:
    return c.bundle.model.groups.get(name) or c.bundle.model.derived.get(name)


def irf(c: ModelContext) -> IrfView:
    g = _group(c, "irf")
    if g is None or "irf" not in g:
        raise SectionSkipped("only vector models (VAR, VECM) have impulse responses")
    da = g["irf"]
    need = ("response", "shock", "horizon")
    if not set(need) <= set(map(str, da.dims)):
        raise SectionSkipped("the model's impulse responses are not (response, shock, horizon)")
    da = da.transpose(*need)
    names = (
        [str(v) for v in np.asarray(da["response"].values)]
        if "response" in da.coords
        else [str(i) for i in range(da.sizes["response"])]
    )
    h = int(da.sizes["horizon"]) - 1

    def arr(x: xr.DataArray) -> list[list[list[float]]]:
        a = np.nan_to_num(np.asarray(x.transpose(*need).values, dtype=float))
        return [[[float(v) for v in cell] for cell in row] for row in a]

    lower = arr(g["lower"]) if "lower" in g else None
    upper = arr(g["upper"]) if "upper" in g else None
    return IrfView(
        variables=names[:20],
        horizons=max(1, h),
        orthogonalized=bool(da.attrs.get("orthogonalized", True)),
        values=arr(da),
        lower=lower,
        upper=upper,
        caption_plain="How each series reacts over the following periods to a one-off shock "
        "in another.",
    )


def fevd(c: ModelContext) -> FevdView:
    g = _group(c, "fevd")
    if g is None or "fevd" not in g:
        raise SectionSkipped("only vector models (VAR, VECM) have a variance decomposition")
    da = g["fevd"]
    need = ("variable", "horizon", "shock")
    if not set(need) <= set(map(str, da.dims)):
        raise SectionSkipped("the model's variance decomposition is not (variable, horizon, shock)")
    a = np.asarray(da.transpose(*need).values, dtype=float)
    a = a / np.where(a.sum(axis=2, keepdims=True) > 0, a.sum(axis=2, keepdims=True), 1.0)
    names = (
        [str(v) for v in np.asarray(da["variable"].values)]
        if "variable" in da.coords
        else [str(i) for i in range(a.shape[0])]
    )
    return FevdView(
        variables=names[:20],
        horizons=int(a.shape[1]),
        shares=[[[float(v) for v in row] for row in per_h] for per_h in a],
        caption_plain="Which shocks explain each series' forecast errors at each horizon.",
    )


def forecast(c: ModelContext) -> ModelForecastView:
    t = _need_time(c)
    caps = c.bundle.model.manifest.capabilities
    if "forecast" not in caps.score_modes:
        raise SectionSkipped("this model does not forecast")
    canon_t = c.bundle.binding.to_canonical(t)
    model_schema = c.bundle.resolved.model.input_schema
    freq = (
        model_schema.dims[canon_t].freq if model_schema and canon_t in model_schema.dims else None
    )
    horizon = c.req.horizon or explore_period(freq) or 12
    request = ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": f"model://{c.bundle.spec.metadata.project}/{c.bundle.spec.metadata.name}@latest",
            "mode": "forecast",
            "horizon": {"dim": canon_t, "periods": horizon},
            "future_covariates": "carry_last",
            "output": {"kind": "expected"},
            "side_tables": supplied_tables(c.bundle, c.side_tables),
        }
    )
    tree = score_bundle(
        c.bundle,
        request,
        None,
        side_tables=c.side_tables,
        registry=c.registry,
        return_full=True,
        history=c.data,
    )
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    pred = node.to_dataset(inherit=False)
    out: list[ForecastView] = []
    rep = caps.representation
    for var in target_vars(c.bundle):
        name = next(
            (n for n in pred.data_vars if str(n) in (var, f"{var}_hat") or str(n).endswith("_hat")),
            None,
        )
        if name is None or t not in pred[name].dims:
            continue
        da = pred[name]
        if not np.issubdtype(da.dtype, np.number):
            continue  # predicted categories (choice, classification) have no fan
        other = [d for d in da.dims if d not in (t, "chain", "draw", "quantile", "bound", "moment")]
        from mapi_runner.worker.modelview import _band

        point = point_estimate(da, rep)
        band = _band(da, rep, 0.9)
        if other:
            point = point.sum(other)
            band = (
                (band[0].sum(other), band[1].sum(other)) if band and rep.value == "draws" else None
            )
        hist = c.data.get(var)
        if hist is not None and not np.issubdtype(hist.dtype, np.number):
            continue  # a categorical target (choice models) has no history line
        hist_vals: list[float | None] = []
        hist_coords: list[Any] = []
        if hist is not None and t in hist.dims:
            ho = [d for d in hist.dims if d != t]
            h = hist.sum(ho, min_count=1) if ho else hist
            h = h.isel({t: slice(-3 * horizon, None)})
            hist_coords = _coords(h, t)
            hist_vals = [_fv(v) for v in np.asarray(h.values, dtype=float)]
        fc_coords = _coords(point, t)
        n_h = len(hist_coords)
        coords = hist_coords + fc_coords
        empty: list[float | None] = [None] * n_h
        rows: list[list[float | None]] = [
            empty + [_fv(v) for v in np.asarray(point.values, dtype=float)]
        ]
        q = [0.5]
        if band is not None:
            q = [0.05, 0.5, 0.95]
            rows = [
                empty + [_fv(v) for v in np.asarray(band[0].values, dtype=float)],
                rows[0],
                empty + [_fv(v) for v in np.asarray(band[1].values, dtype=float)],
            ]
        fan = FanChart(
            dim=t,
            coords=coords,
            quantiles=q,
            values=rows,
            observed=hist_vals + [None] * len(fc_coords),
        )
        out.append(
            ForecastView(
                kpi=var[:128],
                dim=t,
                history=n_h,
                split_at=fc_coords[0] if fc_coords else None,
                summed_over=[str(d) for d in other][:32],
                fan=fan,
            )
        )
    if not out:
        raise SectionSkipped("the forecast has no target series over time")
    return ModelForecastView(
        forecasts=out,
        caption_plain=f"The next {horizon} periods, with drivers held at their last values.",
    )


def error_metrics(
    observed: np.ndarray, point: np.ndarray, *, scale: float | None = None
) -> dict[str, float | None]:
    """``rmse``, ``mae``, ``mape`` (fraction, over non-zero actuals), ``smape`` and, with
    an in-sample ``scale`` (the naive or seasonal-naive MAE), ``mase``."""
    o = np.asarray(observed, dtype=float).reshape(-1)
    p = np.asarray(point, dtype=float).reshape(-1)
    ok = np.isfinite(o) & np.isfinite(p)
    o, p = o[ok], p[ok]
    if o.size == 0:
        return {"rmse": None, "mae": None, "mape": None, "smape": None}
    e = o - p
    nz = o != 0
    denom = np.abs(o) + np.abs(p)
    out = {
        "rmse": _fv(np.sqrt(np.mean(e**2))),
        "mae": _fv(np.mean(np.abs(e))),
        "mape": _fv(np.mean(np.abs(e[nz] / o[nz]))) if nz.any() else None,
        "smape": _fv(
            np.mean(np.where(denom > 0, 2 * np.abs(e) / np.where(denom > 0, denom, 1), 0.0))
        ),
    }
    if scale is not None and scale > 0:
        out["mase"] = _fv(np.mean(np.abs(e)) / scale)
    return out


def _benchmark_scale(history: np.ndarray, season: int | None) -> float | None:
    h = stats.finite(history)
    m = season if season and h.size > season else 1
    if h.size <= m:
        return None
    return _fv(np.mean(np.abs(h[m:] - h[:-m])))


def backtest(c: ModelContext) -> BacktestView:
    t = _need_time(c)
    plan = c.req.backtest
    if plan is None:
        raise SectionSkipped("pass a split plan (backtest) to run a backtest")
    if not isinstance(plan, RollingOriginPlan | ExpandingWindowPlan):
        raise SectionSkipped(
            "backtests refit on contiguous windows: use rolling_origin or expanding_window"
        )
    if c.refitter is None:
        raise SectionSkipped("refits are not available in this run")
    dim = plan.dim if plan.dim in c.data.dims else c.bundle.binding.to_dataset(plan.dim)
    if dim not in c.data.dims:
        dim = t  # window plans run along time: the default dim name means the data's time
    if dim != t:
        raise SectionSkipped(f"the plan's dim {plan.dim!r} is not the data's time dimension")
    n = int(c.data.sizes[t])
    try:
        folds = plan_folds(plan, n)[-MAX_BACKTEST_FOLDS:]
    except MapiError as exc:
        raise SectionSkipped(f"the history is too short for this plan ({exc.detail})") from None
    targets = target_vars(c.bundle)
    if not targets:
        raise SectionSkipped("the model has no target to score")
    y = targets[0]
    canon_t = c.bundle.binding.to_canonical(t)
    schema = c.bundle.resolved.model.input_schema
    freq = schema.dims[canon_t].freq if schema and canon_t in schema.dims else None
    season = explore_period(freq)
    spec = c.refitter.spec(split=None)
    results: list[BacktestFoldResult] = []
    bench_acc: dict[str, list[float]] = {}
    model_acc: list[float] = []
    warn: list[str] = []
    for f in folds:
        assert f.train_start is not None and f.train_stop is not None
        assert f.test_start is not None and f.test_stop is not None
        train = c.data.isel({t: slice(f.train_start, f.train_stop)})
        test = c.data.isel({t: slice(f.train_start, f.test_stop)})
        try:
            b = c.refitter.fit(train, spec)
            preds = predict(b, test, c.registry, side_tables=c.side_tables)
        except Exception as exc:
            warn.append(f"fold {f.index}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        p = next((x for x in preds if x.target == y), None)
        if p is None:
            continue
        sel = {t: slice(f.test_start - f.train_start, f.test_stop - f.train_start)}
        obs_tot = _tot(p.observed.isel(sel), t)
        pt_tot = _tot(p.point.isel(sel), t)
        hist_tot = _tot(c.data[y].isel({t: slice(f.train_start, f.train_stop)}), t)
        scale = _benchmark_scale(hist_tot, season)
        m = error_metrics(obs_tot, pt_tot, scale=scale)
        by_h: dict[str, dict[str, float | None]] = {}
        for h in f.horizons or plan.horizons:
            if 1 <= h <= obs_tot.size:
                by_h[str(h)] = error_metrics(obs_tot[h - 1 : h], pt_tot[h - 1 : h])
        bench = stats.naive_forecasts(
            hist_tot, obs_tot.size, season=season, methods=("naive", "seasonal_naive")
        )
        for name, fc in bench.items():
            mae = error_metrics(obs_tot, fc)["mae"]
            if mae is not None:
                bench_acc.setdefault(name, []).append(mae)
        if m["mae"] is not None:
            model_acc.append(m["mae"])
        results.append(BacktestFoldResult(fold=f, metrics=m, by_horizon=by_h))
    if not results:
        raise SectionSkipped("no fold could be refitted: " + "; ".join(warn[:3]))
    summary: dict[str, float | None] = {}
    for key in ("rmse", "mae", "mape", "smape", "mase"):
        vals = [r.metrics.get(key) for r in results]
        vs = [v for v in vals if v is not None]
        summary[key] = _fv(np.mean(vs)) if vs else None
    benchmarks = {k: {"mae": _fv(np.mean(v))} for k, v in bench_acc.items()}
    if benchmarks and summary.get("mae") is not None:
        best = min(v["mae"] for v in benchmarks.values() if v["mae"] is not None)
        mae = summary["mae"]
        summary["skill_vs_best_benchmark"] = (
            _fv(1 - mae / best) if best and mae is not None else None
        )
    return BacktestView(
        plan=plan.model_dump(mode="json"),
        folds=results,
        summary=summary,
        benchmarks=benchmarks,
        warnings_plain=warn[:50],
        caption_plain="The model refitted on past data and scored on the periods after, "
        "compared with 'same as last period' and 'same as last season'.",
    )


def _tot(da: xr.DataArray, t: str) -> np.ndarray:
    other = [d for d in da.dims if d != t]
    x = da.sum(other, min_count=1) if other else da
    return np.asarray(x.transpose(t).values, dtype=float)


def comparison(c: ModelContext) -> ComparisonView:
    if not c.compare:
        raise SectionSkipped("pass compare_with to compare model versions")
    models = [(c.ref, c.bundle), *c.compare]
    table: dict[str, dict[str, float | None]] = {}
    names: set[str] = set()
    for ref, b in models:
        row = {k: _fv(v) for k, v in b.metrics.values.items()}
        row["n_params"] = float(len(param_rows(b, max_params=5000)[0]))
        table[ref] = row
        names |= set(row)
    metrics = sorted(names)
    ranking: dict[str, list[str]] = {}
    for m in metrics:
        if m not in ERROR_METRICS:
            continue
        have = [(table[r].get(m), r) for r, _b in models if table[r].get(m) is not None]
        ranking[m] = [r for _v, r in sorted(have, key=lambda x: (x[0], x[1]))]
    tests: list[StatTestResult] = []
    warn: list[str] = []
    mask = holdout_mask(c.bundle, c.data, c.evaluation)
    path = [r for r, b in models if forecasts_from_one_origin(b)]
    if mask is not None and path:
        # 1..H-step errors from one origin are a single, strongly dependent path (their
        # loss difference wanders with the series): no test of equal accuracy is valid
        # on it, and an h = 1 Diebold-Mariano test rejects most of the time
        warn.append(
            f"No significance test: {', '.join(path[:3])} forecast the held-out periods from "
            "one forecast origin, so their errors form a single path. Compare the models' "
            "backtests (rolling-origin errors at a fixed horizon), e.g. with a "
            "compare.diebold_mariano workflow node."
        )
    elif mask is not None:
        errs: dict[str, np.ndarray] = {}
        dim = str(mask.dims[0])
        for ref, b in models:
            trained = b.evaluation or b
            try:
                preds = predict(trained, c.data, c.registry, side_tables=c.side_tables)
            except Exception as exc:
                warn.append(f"{ref}: could not score the shared holdout ({type(exc).__name__})")
                continue
            if preds:
                p = preds[0]
                pmask = holdout_mask(c.bundle, p.observed, c.evaluation)
                e = (p.observed - p.point).where(pmask if pmask is not None else mask, drop=True)
                errs[ref] = _tot(e, dim)
        refs = list(errs)
        for ra, rb in itertools.combinations(refs, 2):
            n_pairs = int(min(errs[ra].size, errs[rb].size))
            # errors of models that predict from the observed drivers are still serially
            # correlated: a Newey-West bandwidth of autocovariances (h = lags + 1)
            lags = int(4 * (max(n_pairs, 1) / 100) ** (2 / 9))
            dm = stats.diebold_mariano(errs[ra], errs[rb], h=lags + 1)
            if dm is None:
                continue
            stat, pval = dm
            tests.append(_dm_result(ra, rb, stat, pval, n=n_pairs, lags=lags))
    else:
        warn.append("No shared held-out period: Diebold-Mariano tests need a time holdout.")
    return ComparisonView(
        models=[r for r, _b in models][:20],
        metrics=metrics[:32],
        table=table,
        ranking=ranking,
        tests=tests[:50],
        warnings_plain=warn[:50],
        caption_plain="Several model versions side by side on held-out data (lower errors "
        "are better).",
    )


_FORECAST_FAMILIES: Final = frozenset({"forecast", "ts_multivariate", "regime", "ts"})


def forecasts_from_one_origin(b: Bundle) -> bool:
    """Whether the model's held-out predictions are a multi-step forecast from the end of
    training (statsmodels and other time-series forecasters), not predictions from the
    observed drivers of each period (regressions, marketing mix, tabular models)."""
    m = b.model.manifest
    return m.capabilities.inference == "kalman" or m.model_family in _FORECAST_FAMILIES


def _dm_result(
    a: str, b: str, stat: float, p: float, *, n: int, lags: int = 0, alpha: float = 0.05
) -> StatTestResult:
    better = a if stat < 0 else b
    reject = p < alpha
    verdict = (
        f"{better} is more accurate than the other (significant at {alpha:g})."
        if reject
        else "The two models are about equally accurate on the held-out data."
    )
    return StatTestResult(
        test="test.diebold_mariano",
        var=f"{a} vs {b}"[:256],
        statistic=stat,
        p_value=p,
        alpha=alpha,
        reject=reject,
        decision="different" if reject else "equal",
        verdict_plain=verdict[:500],
        null_plain="Both models forecast the held-out periods equally well.",
        n_obs=n,
        details={
            "model_a": a,
            "model_b": b,
            "loss": "squared error",
            "hln_corrected": True,
            "variance_lags": lags,
        },
    )


PLUGIN_SECTIONS: Final = ("params", "diagnostics", "residuals", "decomposition", "irf", "fevd")
"""Sections a plugin may compute itself (``explorer_views``); its view wins over the generic
one, which stays the fallback."""


def _plugin_first(
    section: str, generic: Callable[[ModelContext], BaseModel]
) -> Callable[[ModelContext], BaseModel]:
    def run(c: ModelContext) -> BaseModel:
        view = c.plugin_views().get(section)
        if view is not None:
            return view
        return generic(c)

    run.__name__ = generic.__name__
    run.__doc__ = generic.__doc__
    return run


MODEL_SECTION_FUNCS: Final[dict[str, Callable[[ModelContext], BaseModel]]] = {
    "params": _plugin_first("params", params),
    "diagnostics": _plugin_first("diagnostics", diagnostics),
    "fit": fit,
    "residuals": _plugin_first("residuals", residuals),
    "decomposition": _plugin_first("decomposition", decomposition),
    "response_curves": response_curves,
    "irf": _plugin_first("irf", irf),
    "fevd": _plugin_first("fevd", fevd),
    "forecast": forecast,
    "backtest": backtest,
    "comparison": comparison,
}
"""Every Model Explorer section's implementation (keys = :data:`mapi_spec.views.MODEL_SECTIONS`)."""
