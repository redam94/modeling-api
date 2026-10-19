"""Evaluation metrics computed from score-path predictions (plan §4.2, §13).

Point metrics (``rmse``, ``mae``, ``mape``, ``smape``, ``wape``, ``bias``, ``r2``) use the
representation's point estimate; ``coverage_<p>``/``interval_width_<p>`` use central
intervals (ETIs for draws and quantiles, the stored interval, normal intervals for
moments); ``crps`` needs draws. ``elpd_loo``/``elpd_waic`` come from ArviZ on the fitted
model's ``log_likelihood`` group when ArviZ is installed; otherwise they are skipped with a
reason. Unknown or unsupported metrics are skipped, never silently zero.

**Categorical targets** (M3, choice models): when the observed target holds labels (text)
and the prediction carries one probability per category along a dim the target lacks
(``y_hat(hh, trip, product)`` for ``y(hh, trip)``), the metrics are computed on the
predicted probabilities (the mean over draws), per held-out cell with a label:

==================  =========================================================================
``accuracy``        hit rate: the most probable category is the observed one
``accuracy_naive``  the hit rate of always guessing the holdout's most common label (a strict
                    baseline: it knows the holdout's own frequencies)
``accuracy_lift``   ``accuracy - accuracy_naive`` (positive: the model beats the guess)
``log_score``       mean log probability of the observed label (higher is better;
                    probabilities are floored at 1e-12)
``brier``           mean multi-class Brier score ``sum_j (p_j - [j = y])^2`` (0 is perfect)
==================  =========================================================================

Labels are matched to categories exactly, else after lower-casing and collapsing every run
of non-alphanumerics to ``_`` (``Coke Zero`` matches ``coke_zero``); a label matching no
category counts as a miss with probability 0. Cells whose probabilities are all zero (no
choice set, such as a trip that never happened) and empty labels do not count. Numeric
metrics (``rmse``, ``mape``, ...) are skipped for categorical targets.

**Circular outputs** (M5, ``OutputDecl.circular``): angles that wrap at ``period`` (one
turn). The point is the circular mean of the draws, errors are wrapped to half a turn,
intervals are central intervals of the draws around the circular mean, ``crps`` uses the arc
distance, and ``mape``/``smape``/``wape``/``r2`` are skipped (:func:`_circular_metrics`).
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

__all__ = [
    "CATEGORICAL_METRICS",
    "MAX_POINTWISE_ELPD",
    "MetricResult",
    "categorical_dim",
    "compute_metrics",
    "elpd",
    "point_estimate",
]

_COVERAGE = re.compile(r"^(coverage|interval_width)_([1-9][0-9]?)$")
MetricResult = tuple[dict[str, float | None], dict[str, str]]
PARETO_K_THRESHOLD = 0.7
MAX_POINTWISE_ELPD = 10_000
"""The most pointwise elpd values :func:`elpd` keeps in ``detail`` (larger data: none)."""
CATEGORICAL_METRICS: tuple[str, ...] = (
    "accuracy",
    "accuracy_naive",
    "accuracy_lift",
    "log_score",
    "brier",
)
"""Holdout metrics of categorical targets (see the module doc)."""
_SAMPLE_DIMS = frozenset({"chain", "draw", "sample", "quantile", "bound", "moment"})
_LOG_FLOOR = 1e-12
_KEY = re.compile(r"[^a-z0-9]+")


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


def _is_text(da: xr.DataArray) -> bool:
    return da.dtype.kind in "USO"


def categorical_dim(pred: xr.DataArray, observed: xr.DataArray) -> str | None:
    """The category dim of a probability prediction for a text target, else ``None``.

    ``pred`` must carry every dim of ``observed`` plus exactly one more (besides sample
    dims such as ``chain`` and ``draw``), and ``observed`` must hold labels.
    """
    if not _is_text(observed):
        return None
    extra = [str(d) for d in pred.dims if d not in observed.dims and str(d) not in _SAMPLE_DIMS]
    if len(extra) != 1 or not set(map(str, observed.dims)) <= set(map(str, pred.dims)):
        return None
    return extra[0]


def _label_key(label: Any) -> str:
    return _KEY.sub("_", str(label).strip().lower()).strip("_")


def _label_codes(obs: np.ndarray, categories: Sequence[Any]) -> np.ndarray:
    """Category index per observed label (``-1`` when it matches none)."""
    exact = {str(c): j for j, c in enumerate(categories)}
    loose: dict[str, int] = {}
    for j, c in enumerate(categories):
        loose.setdefault(_label_key(c), j)
    flat = obs.reshape(-1)
    codes = np.full(flat.shape, -1, dtype=np.int64)
    for i, value in enumerate(flat.tolist()):
        text = str(value)
        codes[i] = exact.get(text, loose.get(_label_key(text), -1))
    return codes.reshape(obs.shape)


def _categorical_metrics(
    pred: xr.DataArray,
    observed: xr.DataArray,
    rep: Representation,
    names: Sequence[str],
    cat: str,
    mask: xr.DataArray | None,
) -> MetricResult:
    values: dict[str, float | None] = {}
    skipped: dict[str, str] = {}
    wanted = [n for n in dict.fromkeys(names) if not n.startswith("elpd_")]
    if rep not in (Representation.DRAWS, Representation.POINT):
        reason = f"categorical metrics need probabilities, not {rep.value}"
        return values, dict.fromkeys(wanted, reason)
    prob = point_estimate(pred, rep)
    valid = observed.notnull() & (observed.astype(str) != "")
    if mask is not None:
        valid = valid & mask.broadcast_like(observed).astype(bool)
    prob, obs, valid = xr.align(prob, observed, valid, join="inner", exclude=[cat])
    dims = [str(d) for d in obs.dims]
    raw = np.asarray(prob.transpose(*dims, cat).values, dtype=np.float64)
    p = np.where(np.isfinite(raw), np.clip(raw, 0.0, None), 0.0).reshape(-1, prob.sizes[cat])
    total = p.sum(axis=1)
    keep = np.asarray(valid.transpose(*dims).values, dtype=bool).reshape(-1) & (total > 0)
    codes = _label_codes(np.asarray(obs.transpose(*dims).values), list(prob[cat].values))
    codes = codes.reshape(-1)[keep]
    p = p[keep] / total[keep, None]
    n = int(keep.sum())
    rows = np.arange(n)
    hit = codes >= 0
    p_obs = np.where(hit, p[rows, np.where(hit, codes, 0)], 0.0)
    for name in wanted:
        if name not in CATEGORICAL_METRICS:
            skipped[name] = "not defined for a categorical target (use accuracy, log_score, brier)"
            continue
        if n == 0:
            skipped[name] = "no holdout observations"
            continue
        accuracy = float(np.mean(np.argmax(p, axis=1) == codes))
        counts = np.bincount(codes[hit], minlength=p.shape[1])
        naive = float(counts.max() / n) if counts.size else 0.0
        match name:
            case "accuracy":
                values[name] = accuracy
            case "accuracy_naive":
                values[name] = naive
            case "accuracy_lift":
                values[name] = accuracy - naive
            case "log_score":
                values[name] = float(np.mean(np.log(np.maximum(p_obs, _LOG_FLOOR))))
            case _:  # brier
                onehot = np.zeros_like(p)
                onehot[rows[hit], codes[hit]] = 1.0
                values[name] = float(np.mean(((p - onehot) ** 2).sum(axis=1)))
    return values, skipped


def compute_metrics(
    pred: xr.DataArray,
    observed: xr.DataArray,
    rep: Representation,
    names: Sequence[str],
    *,
    mask: xr.DataArray | None = None,
    period: float | None = None,
) -> MetricResult:
    """Holdout metrics of ``pred`` (representation-shaped) against ``observed``.

    ``mask`` (bool, broadcastable to ``observed``) restricts the cells that count.
    Returns ``(values, skipped)`` where ``skipped`` maps metric -> reason. A probability
    prediction for a categorical target (:func:`categorical_dim`) gets the categorical
    metrics (see the module doc). ``period`` (one turn, for a circular output such as a
    direction) switches to the circular metrics (:func:`_circular_metrics`).
    """
    if period is not None:
        return _circular_metrics(pred, observed, rep, names, float(period), mask)
    cat = categorical_dim(pred, observed)
    if cat is not None:
        return _categorical_metrics(pred, observed, rep, names, cat, mask)
    if _is_text(observed):
        reason = "the target holds labels but the prediction has no category dim"
        return {}, {n: reason for n in names if not n.startswith("elpd_")}
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
                case _ if name in CATEGORICAL_METRICS:
                    skipped[name] = "defined for categorical targets only"
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


def _wrap(x: Any, period: float) -> Any:
    """Signed difference wrapped to ``[-period/2, period/2)``."""
    return (x + period / 2) % period - period / 2


def _circular_mean(da: xr.DataArray, period: float) -> xr.DataArray:
    """Circular mean over ``chain``/``draw``, in ``[0, period)``."""
    t = da * (2 * np.pi / period)
    c = xr.DataArray(np.cos(t)).mean(["chain", "draw"])
    s = xr.DataArray(np.sin(t)).mean(["chain", "draw"])
    out: xr.DataArray = (xr.apply_ufunc(np.arctan2, s, c) * (period / (2 * np.pi))) % period
    return out


def _circular_metrics(
    pred: xr.DataArray,
    observed: xr.DataArray,
    rep: Representation,
    names: Sequence[str],
    period: float,
    mask: xr.DataArray | None,
) -> MetricResult:
    """Metrics of a circular output (angles that wrap at ``period``).

    The point estimate is the circular mean of the draws; errors are wrapped to
    ``[-period/2, period/2)`` (359 degrees against 1 degree is an error of -2, not 358);
    intervals are central intervals of the draws' wrapped deviations from the circular
    mean, so an interval may straddle the seam; ``crps`` uses the arc distance. Metrics
    relative to the target's size (``mape``, ``smape``, ``wape``, ``r2``) have no meaning on
    a circle and are skipped.
    """
    values: dict[str, float | None] = {}
    skipped: dict[str, str] = {}
    wanted = [n for n in dict.fromkeys(names) if not n.startswith("elpd_")]
    if not (math.isfinite(period) and period > 0):
        return values, dict.fromkeys(wanted, f"the circular output's period {period} is invalid")
    if rep is not Representation.DRAWS:
        reason = f"circular metrics need posterior draws, not {rep.value}"
        return values, dict.fromkeys(wanted, reason)
    valid = observed.notnull()
    if mask is not None:
        valid = valid & mask.broadcast_like(observed).astype(bool)
    point = _circular_mean(pred, period)
    point, obs_a, valid_a = xr.align(point, observed, valid, join="inner")
    err = _wrap(point - obs_a, period).where(valid_a)
    n = int(valid_a.sum())
    for name in wanted:
        if n == 0:
            skipped[name] = "no holdout observations"
            continue
        m = _COVERAGE.match(name)
        match name:
            case "rmse":
                values[name] = float(np.sqrt((err**2).mean()))
            case "mae":
                values[name] = float(abs(err).mean())
            case "bias":
                values[name] = float(err.mean())
            case "crps":
                values[name] = _crps_circular(pred, obs_a.where(valid_a), period)
            case _ if m is not None:
                level = int(m.group(2)) / 100
                a = (1 - level) / 2
                dev = _wrap(pred - point, period)
                q = dev.quantile([a, 1 - a], dim=["chain", "draw"])
                lo, hi = q.isel(quantile=0, drop=True), q.isel(quantile=1, drop=True)
                lo, hi, oe, vv = xr.align(lo, hi, _wrap(obs_a - point, period), valid_a)
                if m.group(1) == "coverage":
                    values[name] = float(((oe >= lo) & (oe <= hi)).where(vv).mean())
                else:
                    values[name] = float((hi - lo).broadcast_like(oe).where(vv).mean())
            case _:
                skipped[name] = "not defined for a circular output (use mae, rmse, coverage_90)"
    for k, v in list(values.items()):
        if v is not None and not math.isfinite(v):
            values[k] = None
    return values, skipped


def _crps_circular(da: xr.DataArray, obs: xr.DataArray, period: float) -> float:
    """CRPS with the arc distance: ``E d(X, y) - E d(X, X') / 2`` (Grimit et al. 2006)."""
    aligned, ob = xr.align(da, obs, join="inner")
    samples = aligned.stack(__s__=["chain", "draw"]).transpose(..., "__s__")
    x = samples.values
    y = ob.broadcast_like(samples.isel(__s__=0, drop=True)).values[..., None]
    term1 = np.abs(_wrap(x - y, period)).mean(axis=-1)
    m = x.shape[-1]
    # E d(X, X') from the draws paired with shifted copies of themselves (memory O(m))
    shifts = range(1, min(m, 33))
    term2 = (
        np.mean(
            [np.abs(_wrap(x - np.roll(x, k, axis=-1), period)).mean(axis=-1) for k in shifts],
            axis=0,
        )
        if m > 1
        else np.zeros(term1.shape)
    )
    return float(np.nanmean(np.where(np.isnan(y[..., 0]), np.nan, term1 - term2 / 2)))


def _finite(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _elpd_detail(res: Any) -> dict[str, Any]:
    """The fields of an ``ElpdDetail`` from an ArviZ ELPD result."""
    raw = getattr(res, "elpd_i", None)
    if raw is None:
        raw = getattr(res, "loo_i", getattr(res, "waic_i", None))
    pw: list[float] | None = None
    n = int(getattr(res, "n_data_points", 0) or 0)
    if raw is not None:
        flat = np.asarray(getattr(raw, "values", raw), dtype=float).ravel()
        n = n or int(flat.size)
        if flat.size <= MAX_POINTWISE_ELPD and bool(np.all(np.isfinite(flat))):
            pw = [float(f"{v:.6g}") for v in flat.tolist()]
    return {
        "elpd": float(res.elpd),
        "se": _finite(getattr(res, "se", None)),
        "p": _finite(getattr(res, "p", None)),
        "n": n,
        "pointwise": pw,
    }


def elpd(
    groups: Mapping[str, xr.Dataset],
    names: Sequence[str],
    *,
    loo_valid: bool = True,
    reason: str | None = None,
    detail: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, float | None], dict[str, str], list[Diagnostic]]:
    """``elpd_loo``/``elpd_waic`` from a ``log_likelihood`` group (needs ArviZ).

    Returns ``(values, skipped, diagnostics)``; high Pareto k values become a
    ``PARETO_K_HIGH`` diagnostic instead of a warning on stderr. When ``detail`` is given,
    it receives ``{name: {elpd, se, p, n, pointwise}}`` for each computed estimate (the
    fields of :class:`mapi_sdk.bundle.ElpdDetail`; ``pointwise`` is ``None`` above
    :data:`MAX_POINTWISE_ELPD` points).
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
                res = fn(tree, pointwise=True)
        except Exception as exc:  # report, never fail the run on a diagnostic
            skipped[n] = f"ArviZ could not compute it: {type(exc).__name__}"
            continue
        values[n] = _finite(res.elpd)
        if detail is not None and values[n] is not None:
            # a -inf log-likelihood gives NaN: no detail (a null elpd would not load)
            detail[n] = _elpd_detail(res)
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
                        message_plain="Some data points have a strong effect on the model, so "
                        "the leave-one-out score is only approximate.",
                        suggested_action="Look at the observations that have a strong effect, "
                        "or use K-fold cross-validation.",
                    )
                )
    return values, skipped, diagnostics
