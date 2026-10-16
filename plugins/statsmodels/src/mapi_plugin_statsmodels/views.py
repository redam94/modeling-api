"""Model Explorer sections for statsmodels fits (``mapi_spec.views`` DTOs).

:func:`explorer_views` reads only the bundle groups the plugins write (``params``,
``fit_stats``, ``residuals``, ``residual_tests``, ``irf``, ``fevd``, ``granger``,
``cointegration``, ``ecm``, ``components``, ``regimes``) and returns the sections it can
fill: ``params``, ``diagnostics``, ``residuals``, ``irf``, ``fevd`` and ``decomposition``.
An explore run writes each to ``ui/explore/<section>.json``. Panels are flattened into
facets; the IRF/FEVD sections show the first series cell (with a note) because those views
hold one system.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.models import Fitted
from mapi_spec.stattests import TEST_DEFINITIONS, StatTestResult, make_test_result
from mapi_spec.views import (
    MAX_EXPLORE_LAGS,
    MAX_EXPLORE_POINTS,
    MAX_EXPLORE_SERIES,
    DecompositionView,
    FevdView,
    Histogram,
    IrfView,
    LagProfile,
    ModelDiagnosticsView,
    ParamRow,
    ParamTableView,
    QQPoints,
    ResidualDiagnosticsView,
    SeriesView,
)
from pydantic import BaseModel
from scipy import stats

from mapi_plugin_statsmodels._common import PARAM, RESIDUAL_TESTS, TEST, TIME, VARIABLE

__all__ = ["explorer_views", "johansen_result", "residual_test_results"]

MAX_PARAM_ROWS = 5000


def _scalar(v: Any) -> str | int | float | bool | None:
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) > 10**15:
        return pd.Timestamp(v).isoformat()  # datetime64[ns] as int
    if isinstance(v, float) and not np.isfinite(v):
        return None
    if isinstance(v, str | int | float | bool) or v is None:
        return v
    return str(v)


def _coords(values: np.ndarray) -> list[str | int | float | bool | None]:
    arr = np.asarray(values)
    if arr.dtype.kind == "M":
        return [pd.Timestamp(v).isoformat() for v in arr]
    return [_scalar(v) for v in arr]


def _num(v: Any) -> float | None:
    f = float(v)
    return f if np.isfinite(f) else None


def _series_dims(fitted: Fitted, ds: xr.Dataset) -> list[str]:
    """The panel dims (``settings.series_dims``) present in ``ds``."""
    dims = fitted.settings.get("series_dims") or []
    return [str(d) for d in dims if d in ds.dims]


def _cells(ds: xr.Dataset, dims: list[str]) -> Iterator[dict[str, Any]]:
    if not dims:
        yield {}
        return
    labels = [np.asarray(ds[d].values) for d in dims]
    for idx in np.ndindex(*(len(x) for x in labels)):
        yield {d: labels[i][j].item() for i, (d, j) in enumerate(zip(dims, idx, strict=True))}


def _facet(key: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    return {k: v for k, v in ((k, _scalar(v)) for k, v in key.items()) if v is not None}


# ==================================================================================== params


def param_view(fitted: Fitted) -> ParamTableView:
    ds = fitted.group("params")
    dims = _series_dims(fitted, ds)
    rows: list[ParamRow] = []
    total = 0
    for key in _cells(ds, dims):
        cell = ds.sel(key) if key else ds
        for p in np.asarray(cell[PARAM].values):
            row = cell.sel({PARAM: p})
            if not np.isfinite(float(row["estimate"])):
                continue
            total += 1
            if len(rows) >= MAX_PARAM_ROWS:
                continue
            pv = _num(row["p_value"])
            rows.append(
                ParamRow(
                    name=str(p),
                    coords=_facet(key),
                    mean=_num(row["estimate"]),
                    sd=_num(row["std_err"]),
                    lower=_num(row["ci_lower"]),
                    upper=_num(row["ci_upper"]),
                    p_value=None if pv is None else min(max(pv, 0.0), 1.0),
                )
            )
    return ParamTableView(
        interval="ci_95",
        rows=rows,
        omitted=max(0, total - len(rows)),
        caption_plain="Estimated effects with their 95% confidence intervals; a small p-value "
        "means the effect is unlikely to be zero.",
    )


# =============================================================================== diagnostics


def residual_test_results(fitted: Fitted, alpha: float = 0.05) -> list[StatTestResult]:
    """One :class:`StatTestResult` per residual test, with every series in ``per_series``."""
    ds = fitted.group("residual_tests")
    dims = _series_dims(fitted, ds) + ([VARIABLE] if VARIABLE in ds.dims else [])
    out: list[StatTestResult] = []
    for test in RESIDUAL_TESTS:
        if test not in ds[TEST].values:
            continue
        rows = []
        for key in _cells(ds, dims):
            cell = ds.sel({**key, TEST: test})
            st, p = float(cell["statistic"]), float(cell["p_value"])
            if not np.isfinite(p):
                continue
            r = make_test_result(test, statistic=st, p_value=min(max(p, 0.0), 1.0), alpha=alpha)
            rows.append((key, r, int(cell["lags"])))
        if not rows:
            continue
        ps = np.array([r.p_value for _, r, _ in rows if r.p_value is not None])
        share = float(np.mean([r.reject for _, r, _ in rows]))
        head = make_test_result(
            test,
            statistic=float(
                np.median([r.statistic for _, r, _ in rows if r.statistic is not None])
            ),
            p_value=float(np.median(ps)),
            alpha=alpha,
            var="resid",
        )
        reject = share >= 0.5
        d = TEST_DEFINITIONS[test]
        out.append(
            head.model_copy(
                update={
                    "reject": reject,
                    "decision": d.reject_decision if reject else d.keep_decision,
                    "verdict_plain": d.reject_plain if reject else d.keep_plain,
                    "lags": rows[0][2] or None,
                    "n_obs": None,
                    "reject_share": share,
                    "per_series": [
                        {
                            "coords": _facet(k),
                            "statistic": r.statistic,
                            "p_value": r.p_value,
                            "reject": r.reject,
                            "decision": r.decision,
                        }
                        for k, r, _ in rows
                    ][:10_000],
                }
            )
        )
    return out


def johansen_result(fitted: Fitted) -> StatTestResult | None:
    """The Johansen rank test of a VECM fit (first series cell) as a test result."""
    if "cointegration" not in fitted.groups:
        return None
    ds = fitted.group("cointegration")
    dims = _series_dims(fitted, ds)
    key = next(_cells(ds, dims))
    cell = ds.sel(key) if key else ds
    rank = int(cell["johansen_rank"])
    d = TEST_DEFINITIONS["test.johansen"]
    reject = rank > 0
    return StatTestResult(
        test="test.johansen",
        statistic=_num(cell["statistic"].isel(rank_tested=0)),
        p_value=None,
        alpha=float(ds.attrs.get("signif", 0.05)),
        reject=reject,
        decision=d.reject_decision if reject else d.keep_decision,
        verdict_plain=d.reject_plain if reject else d.keep_plain,
        null_plain=d.null_plain,
        critical_values={
            f"r<={int(r)}": float(cell["critical_value"].sel(rank_tested=r))
            for r in np.asarray(cell["rank_tested"].values)
        },
        details={
            "rank": rank,
            "method": str(ds.attrs.get("method", "trace")),
            "statistics": [_num(v) for v in np.asarray(cell["statistic"].values)],
            "coords": _facet(key),
        },
    )


def granger_results(fitted: Fitted, alpha: float = 0.05) -> list[StatTestResult]:
    if "granger" not in fitted.groups:
        return []
    ds = fitted.group("granger")
    dims = _series_dims(fitted, ds)
    out: list[StatTestResult] = []
    for key in _cells(ds, dims):
        cell = ds.sel(key) if key else ds
        for caused in np.asarray(cell["caused"].values):
            for causing in np.asarray(cell["causing"].values):
                p = float(cell["p_value"].sel(caused=caused, causing=causing))
                if caused == causing or not np.isfinite(p):
                    continue
                st = float(cell["statistic"].sel(caused=caused, causing=causing))
                out.append(
                    make_test_result(
                        "test.granger",
                        statistic=st,
                        p_value=min(max(p, 0.0), 1.0),
                        alpha=alpha,
                        var=f"{causing}->{caused}",
                        details={
                            "caused": str(caused),
                            "causing": str(causing),
                            "coords": _facet(key),
                        },
                    )
                )
    return out


def bounds_results(fitted: Fitted) -> list[dict[str, Any]]:
    if "ecm" not in fitted.groups:
        return []
    ds = fitted.group("ecm")
    dims = _series_dims(fitted, ds)
    out = []
    for key in _cells(ds, dims):
        cell = ds.sel(key) if key else ds
        out.append(
            {
                "kind": "test.pss_bounds",
                "coords": _facet(key),
                "statistic": _num(cell["bounds_statistic"]),
                "p_value_i0": _num(cell["bounds_p_lower"]),
                "p_value_i1": _num(cell["bounds_p_upper"]),
                "case": int(cell["bounds_case"]),
                "speed_of_adjustment": _num(cell["speed_of_adjustment"]),
                "half_life": _num(cell["half_life"]),
            }
        )
    return out


def diagnostics_view(fitted: Fitted) -> ModelDiagnosticsView:
    ds = fitted.group("fit_stats")
    stats_: dict[str, float | None] = {}
    for name in ("aic", "bic", "hqic", "llf", "nobs", "sigma2"):
        if name in ds:
            vals = np.asarray(ds[name].values, dtype=float).reshape(-1)
            stats_[name if vals.size == 1 else f"{name}_total"] = (
                _num(vals.sum()) if vals.size else None
            )
    if "converged" in ds:
        stats_["converged_share"] = _num(np.asarray(ds["converged"].values, float).mean())
    items: list[dict[str, Any]] = []
    dims = _series_dims(fitted, ds)
    for key in _cells(ds, dims):
        cell = ds.sel(key) if key else ds
        items.append(
            {
                "kind": "fit_stats",
                "coords": _facet(key),
                **{n: _num(cell[n]) for n in ds.data_vars},
            }
        )
    johansen = johansen_result(fitted)
    if johansen is not None:
        items.append({"kind": "test", **johansen.model_dump(mode="json")})
    for g in granger_results(fitted):
        items.append({"kind": "test", **g.model_dump(mode="json")})
    items.extend(bounds_results(fitted))
    return ModelDiagnosticsView(
        inference=fitted.manifest.capabilities.inference,
        stats=stats_,
        items=items[:200],
        caption_plain="How well the model fits (lower AIC/BIC is better when comparing models "
        "on the same data) and the model's own tests.",
    )


# ================================================================================ residuals


def _acf(r: np.ndarray, lags: int) -> np.ndarray:
    from statsmodels.tsa.stattools import acf

    return np.asarray(acf(r, nlags=lags, fft=True), dtype=float)


def residuals_view(fitted: Fitted) -> ResidualDiagnosticsView:
    ds = fitted.group("residuals")
    dims = _series_dims(fitted, ds) + ([VARIABLE] if VARIABLE in ds.dims else [])
    series: list[SeriesView] = []
    acfs: list[LagProfile] = []
    pooled: list[np.ndarray] = []
    for key in _cells(ds, dims):
        if len(series) >= MAX_EXPLORE_SERIES:
            break
        da = ds["resid"].sel(key) if key else ds["resid"]
        values = np.asarray(da.values, dtype=float)
        coords = _coords(da[TIME].values)
        step = max(1, int(np.ceil(values.size / MAX_EXPLORE_POINTS)))
        series.append(
            SeriesView(
                var="resid",
                facet=_facet(key),
                coords=coords[::step],
                values=[_num(v) for v in values[::step]],
                downsampled=step > 1,
            )
        )
        finite = values[np.isfinite(values)]
        pooled.append(finite)
        if finite.size > 4:
            nl = int(min(MAX_EXPLORE_LAGS, max(1, finite.size // 4), 40))
            a = _acf(finite, nl)
            bound = float(1.96 / np.sqrt(finite.size))
            sig = [int(i) for i in range(1, a.size) if abs(a[i]) > bound]
            acfs.append(
                LagProfile(
                    var="resid",
                    facet=_facet(key),
                    lags=list(range(a.size)),
                    values=[_num(v) for v in a],
                    bound=bound,
                    significant_lags=sig,
                    peak_lag=int(np.argmax(np.abs(a[1:])) + 1) if a.size > 1 else None,
                )
            )
    allr = np.concatenate(pooled) if pooled else np.array([])
    hist = qq = None
    if allr.size >= 5:
        counts, edges = np.histogram(allr, bins=min(40, max(5, allr.size // 5)))
        hist = Histogram(edges=[float(e) for e in edges], counts=[int(c) for c in counts])
        sample = np.sort(allr)
        if sample.size > 2000:
            sample = sample[np.linspace(0, sample.size - 1, 2000).round().astype(int)]
        probs = (np.arange(1, sample.size + 1) - 0.5) / sample.size
        z = (sample - sample.mean()) / (sample.std() or 1.0)
        qq = QQPoints(
            theoretical=[float(v) for v in stats.norm.ppf(probs)], sample=[float(v) for v in z]
        )
    return ResidualDiagnosticsView(
        series=series,
        acf=acfs,
        tests=residual_test_results(fitted),
        histogram=hist,
        qq=qq,
        caption_plain="The errors the model makes one period ahead. Good models leave errors "
        "without patterns over time and with a steady spread.",
    )


# ====================================================================================== irf


def _first_cell(fitted: Fitted, ds: xr.Dataset) -> tuple[xr.Dataset, dict[str, Any], int]:
    dims = _series_dims(fitted, ds)
    keys = list(_cells(ds, dims))
    key = keys[0]
    return (ds.sel(key) if key else ds), key, len(keys)


def irf_view(fitted: Fitted) -> IrfView | None:
    if "irf" not in fitted.groups:
        return None
    cell, key, n = _first_cell(fitted, fitted.group("irf"))
    variables = [str(v) for v in cell["response"].values]
    order = ("response", "shock", "horizon")

    def arr(name: str) -> list[list[list[float]]] | None:
        if name not in cell:
            return None
        return np.asarray(cell[name].transpose(*order).values, dtype=float).tolist()  # type: ignore[no-any-return]

    values = arr("irf")
    assert values is not None
    return IrfView(
        variables=variables,
        horizons=int(cell.sizes["horizon"]) - 1,
        orthogonalized=True,
        values=values,
        lower=arr("irf_lower"),
        upper=arr("irf_upper"),
        caption_plain="How a one-off shock to one series (one standard deviation) moves each "
        "series over the following periods.",
        warnings_plain=[f"Showing {_facet(key)} of {n} series groups."] if n > 1 else [],
    )


def fevd_view(fitted: Fitted) -> FevdView | None:
    if "fevd" not in fitted.groups:
        return None
    cell, key, n = _first_cell(fitted, fitted.group("fevd"))
    shares = np.asarray(cell["share"].transpose(VARIABLE, "horizon", "shock").values, dtype=float)
    shares = shares / shares.sum(axis=2, keepdims=True)
    return FevdView(
        variables=[str(v) for v in cell[VARIABLE].values],
        horizons=int(cell.sizes["horizon"]),
        shares=shares.tolist(),
        caption_plain="Which series' surprises explain the uncertainty of each forecast, by "
        "horizon.",
        warnings_plain=[f"Showing {_facet(key)} of {n} series groups."] if n > 1 else [],
    )


def decomposition_view(fitted: Fitted) -> DecompositionView | None:
    if "components" not in fitted.groups:
        return None
    cell, key, n = _first_cell(fitted, fitted.group("components"))
    comps = {
        str(v): [_num(x) for x in np.asarray(cell[v].values, dtype=float)] for v in cell.data_vars
    }
    absmean = {
        k: float(np.nanmean(np.abs([x for x in v if x is not None] or [0.0])))
        for k, v in comps.items()
    }
    total = sum(absmean.values()) or 1.0
    return DecompositionView(
        dim=TIME,
        coords=_coords(cell[TIME].values),
        components=comps,
        shares={k: v / total for k, v in absmean.items()},
        caption_plain="The series split into its smoothed level/trend, season and other parts.",
        warnings_plain=[f"Showing {_facet(key)} of {n} series."] if n > 1 else [],
    )


def explorer_views(fitted: Fitted) -> dict[str, BaseModel]:
    """Every Model Explorer section a statsmodels fit can fill, keyed by section name."""
    out: dict[str, BaseModel] = {
        "params": param_view(fitted),
        "diagnostics": diagnostics_view(fitted),
        "residuals": residuals_view(fitted),
    }
    for name, view in (
        ("irf", irf_view(fitted)),
        ("fevd", fevd_view(fitted)),
        ("decomposition", decomposition_view(fitted)),
    ):
        if view is not None:
            out[name] = view
    return out
