"""Evaluation metrics computed from score-path predictions (plan §4.2, §13).

Point metrics (``rmse``, ``mae``, ``mape``, ``smape``, ``wape``, ``bias``, ``r2``) use the
representation's point estimate; ``coverage_<p>``/``interval_width_<p>`` use central
intervals (ETIs for draws and quantiles, the stored interval, normal intervals for
moments); ``crps`` needs draws. ``elpd_loo``/``elpd_waic`` come from ArviZ on the fitted
model's ``log_likelihood`` group when ArviZ is installed; otherwise they are skipped with a
reason. Unknown or unsupported metrics are skipped, never silently zero.
"""

from __future__ import annotations

import math
import re
import warnings
from collections.abc import Mapping, Sequence
from statistics import NormalDist
from typing import Any

import numpy as np
import xarray as xr
from mapi_spec.capabilities import Diagnostic, Representation
from mapi_spec.errors import CapabilityError

from mapi_sdk.summarise import interp_quantile

__all__ = ["MetricResult", "compute_metrics", "elpd", "point_estimate"]

_COVERAGE = re.compile(r"^(coverage|interval_width)_([1-9][0-9]?)$")
MetricResult = tuple[dict[str, float | None], dict[str, str]]
PARETO_K_THRESHOLD = 0.7


def point_estimate(da: xr.DataArray, rep: Representation) -> xr.DataArray:
    """The representation's point estimate (mean of draws, median of quantiles, ...)."""
    match rep:
        case Representation.DRAWS:
            return da.mean(["chain", "draw"])
        case Representation.QUANTILES:
            return interp_quantile(da, 0.5, "median")
        case Representation.INTERVAL:
            if "point" in [str(b) for b in da["bound"].values]:
                return da.sel(bound="point", drop=True)
            return (da.sel(bound="lower", drop=True) + da.sel(bound="upper", drop=True)) / 2
        case Representation.MOMENTS:
            return da.sel(moment="mean", drop=True)
    return da


def _interval(
    da: xr.DataArray, rep: Representation, level: float
) -> tuple[xr.DataArray, xr.DataArray] | str:
    a = (1 - level) / 2
    match rep:
        case Representation.DRAWS:
            q = da.quantile([a, 1 - a], dim=["chain", "draw"])
            return q.isel(quantile=0, drop=True), q.isel(quantile=1, drop=True)
        case Representation.QUANTILES:
            try:
                return interp_quantile(da, a), interp_quantile(da, 1 - a)
            except CapabilityError:
                return f"the stored quantiles do not cover a {level:.0%} interval"
        case Representation.INTERVAL:
            alpha = da.attrs.get("alpha")
            if alpha is None or not math.isclose(1 - float(alpha), level, abs_tol=1e-6):
                return f"the model's interval is not a {level:.0%} interval"
            return da.sel(bound="lower", drop=True), da.sel(bound="upper", drop=True)
        case Representation.MOMENTS:
            z = NormalDist().inv_cdf(0.5 + level / 2)
            mean, sd = da.sel(moment="mean", drop=True), da.sel(moment="sd", drop=True)
            return mean - z * sd, mean + z * sd
    return "point predictions carry no uncertainty"


def _crps_draws(da: xr.DataArray, obs: xr.DataArray) -> float:
    samples = da.stack(__s__=["chain", "draw"]).transpose(..., "__s__")
    x = np.sort(samples.values, axis=-1)
    y = obs.broadcast_like(samples.isel(__s__=0, drop=True)).values[..., None]
    m = x.shape[-1]
    term1 = np.abs(x - y).mean(axis=-1)
    weights = (2 * np.arange(1, m + 1) - m - 1) / (m * m)
    term2 = (x * weights).sum(axis=-1)
    vals = term1 - term2
    return float(np.nanmean(vals))


def compute_metrics(
    pred: xr.DataArray,
    observed: xr.DataArray,
    rep: Representation,
    names: Sequence[str],
    *,
    mask: xr.DataArray | None = None,
) -> MetricResult:
    """Holdout metrics of ``pred`` (representation-shaped) against ``observed``.

    ``mask`` (bool, broadcastable to ``observed``) restricts the cells that count.
    Returns ``(values, skipped)`` where ``skipped`` maps metric -> reason.
    """
    values: dict[str, float | None] = {}
    skipped: dict[str, str] = {}
    obs = observed
    valid = obs.notnull()
    if mask is not None:
        valid = valid & mask.broadcast_like(obs).astype(bool)
    point = point_estimate(pred, rep)
    point, obs_a, valid_a = xr.align(point, obs, valid, join="inner")
    err = (point - obs_a).where(valid_a)
    o = obs_a.where(valid_a)
    n = int(valid_a.sum())
    for name in names:
        if name in values or name in skipped:
            continue
        if n == 0:
            skipped[name] = "no holdout observations"
            continue
        if name.startswith("elpd_"):
            continue  # computed from the fitted model, not the holdout
        m = _COVERAGE.match(name)
        with np.errstate(divide="ignore", invalid="ignore"):
            match name:
                case "rmse":
                    values[name] = float(np.sqrt((err**2).mean()))
                case "mae":
                    values[name] = float(abs(err).mean())
                case "bias":
                    values[name] = float(err.mean())
                case "mape":
                    nz = o.where(o != 0)
                    values[name] = float((abs(err) / abs(nz)).mean() * 100)
                case "smape":
                    denom = (abs(point) + abs(o)) / 2
                    values[name] = float((abs(err) / denom.where(denom != 0)).mean() * 100)
                case "wape":
                    values[name] = float(abs(err).sum() / abs(o).sum() * 100)
                case "r2":
                    ss_res = float((err**2).sum())
                    ss_tot = float(((o - o.mean()) ** 2).sum())
                    values[name] = None if ss_tot == 0 else 1 - ss_res / ss_tot
                case "crps":
                    if rep is not Representation.DRAWS:
                        skipped[name] = "CRPS needs posterior draws"
                    else:
                        aligned, ob = xr.align(pred, o, join="inner")
                        values[name] = _crps_draws(aligned, ob)
                case _ if m is not None:
                    level = int(m.group(2)) / 100
                    got = _interval(pred, rep, level)
                    if isinstance(got, str):
                        skipped[name] = got
                        continue
                    lo, hi, oo, vv = xr.align(got[0], got[1], o, valid_a, join="inner")
                    if m.group(1) == "coverage":
                        inside = ((oo >= lo) & (oo <= hi)).where(vv)
                        values[name] = float(inside.mean())
                    else:
                        values[name] = float((hi - lo).broadcast_like(oo).where(vv).mean())
                case _:
                    skipped[name] = "not computed by the SDK harness"
    for k, v in list(values.items()):
        if v is not None and not math.isfinite(v):
            values[k] = None
    return values, skipped


def elpd(
    groups: Mapping[str, xr.Dataset],
    names: Sequence[str],
    *,
    loo_valid: bool = True,
    reason: str | None = None,
) -> tuple[dict[str, float | None], dict[str, str], list[Diagnostic]]:
    """``elpd_loo``/``elpd_waic`` from a ``log_likelihood`` group (needs ArviZ).

    Returns ``(values, skipped, diagnostics)``; high Pareto k values become a
    ``PARETO_K_HIGH`` diagnostic instead of a warning on stderr.
    """
    values: dict[str, float | None] = {}
    skipped: dict[str, str] = {}
    diagnostics: list[Diagnostic] = []
    wanted = [n for n in names if n in ("elpd_loo", "elpd_waic")]
    if not wanted:
        return values, skipped, diagnostics
    if not loo_valid:
        for n in wanted:
            skipped[n] = reason or "LOO is not valid for this model"
        return values, skipped, diagnostics
    if "log_likelihood" not in groups or "posterior" not in groups:
        for n in wanted:
            skipped[n] = "the model did not store a log_likelihood group"
        return values, skipped, diagnostics
    try:
        import arviz as az
    except ImportError:  # pragma: no cover - arviz is installed in the dev environment
        for n in wanted:
            skipped[n] = "ArviZ is not installed"
        return values, skipped, diagnostics
    tree = xr.DataTree.from_dict(
        {"posterior": groups["posterior"], "log_likelihood": groups["log_likelihood"]}
    )
    for n in wanted:
        fn: Any = az.loo if n == "elpd_loo" else getattr(az, "waic", None)
        if fn is None:
            skipped[n] = "this ArviZ version does not provide WAIC"
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                res = fn(tree)
        except Exception as exc:  # report, never fail the run on a diagnostic
            skipped[n] = f"ArviZ could not compute it: {type(exc).__name__}"
            continue
        values[n] = float(res.elpd)
        k = getattr(res, "pareto_k", None)
        if n == "elpd_loo" and k is not None:
            kv = np.asarray(getattr(k, "values", k), dtype=float)
            bad = int(np.sum(kv > PARETO_K_THRESHOLD))
            if bad:
                diagnostics.append(
                    Diagnostic(
                        code="PARETO_K_HIGH",
                        severity="warning",
                        metric="elpd_loo",
                        value=float(bad),
                        threshold=PARETO_K_THRESHOLD,
                        message_expert=f"{bad} of {kv.size} observations have Pareto k > "
                        f"{PARETO_K_THRESHOLD}; PSIS-LOO may be unreliable",
                        message_plain="Some data points strongly influence the model, so the "
                        "leave-one-out score is only approximate.",
                        suggested_action="Inspect influential observations or use K-fold "
                        "cross-validation.",
                    )
                )
    return values, skipped, diagnostics
