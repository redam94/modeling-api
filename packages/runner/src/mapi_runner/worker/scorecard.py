"""The sanity scorecard of a fit, computed in the sandbox (plan §11 Guided task 5; M3).

:func:`fit_scorecard` measures every check of :data:`mapi_spec.scorecard.SCORECARD_CHECKS`
that the fit can answer and rolls the items up (``scorecard.json``, stored on the model
version by the finalizer and read by the promotion gate). A guided spec
(``metadata.labels.guided: <template>``) runs its template's checks with its thresholds;
any other spec runs the whole catalog. A check that cannot be measured is listed in
``skipped`` with the reason, never guessed.

========================  ====================================================================
metric                    measured on
========================  ====================================================================
``r_hat_max``             the production posterior's free parameters (the plugin's
``ess_bulk_min``          convergence diagnostics, else the posterior itself)
``divergence_rate``       ``sample_stats.diverging`` (share of all draws)
``mape``, ``coverage_90`` the holdout, through the score path (``metrics.json``)
``accuracy_lift``         the holdout hit rate of a choice model minus that of always guessing
                          the most common label (``metrics.json``)
``negative_roi_share``    the ``response_curves`` grid at multiplier 1: per channel, the share of
``roi_max``               draws with a negative return, and the largest posterior-mean ROI
``max_channel_share``     channel contribution / (baseline + every channel), posterior mean
``baseline_share``        baseline / (baseline + every channel), posterior mean
``prior_overlap_max``     overlap of prior and posterior draws (needs a ``prior`` group:
                          ``fit.prior_predictive`` or ``fit.keep.prior``)
``missing_fraction``      the raw dataset's numeric variables that hold observations: not the
                          roles ``constant``, ``index`` and ``mask`` (geometry, lookups,
                          masks), and not the model's ``structural_missing`` variables
``history_years``         the model stage's time dim: periods / periods per year (from the
                          dim's frequency, else the one its labels follow)
``spend_cv_min``          the raw spend per channel over time (summed over other dims)
========================  ====================================================================
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Mapping
from typing import Final

import numpy as np
import xarray as xr
from mapi_plots.recipes._stats import ess_bulk, rhat
from mapi_sdk.bundle import Bundle
from mapi_sdk.future import season_length
from mapi_sdk.schema import infer_freq
from mapi_spec.capabilities import Diagnostic
from mapi_spec.guided import GUIDED_TEMPLATES
from mapi_spec.schema import Role
from mapi_spec.scorecard import (
    SCORECARD_CHECKS,
    CheckThresholds,
    Scorecard,
    ScorecardItem,
    evaluate_check,
    rollup,
)

__all__ = [
    "GUIDED_LABEL",
    "fit_scorecard",
    "history_years",
    "missing_count",
    "model_structural_missing",
    "scorecard_metrics",
]

GUIDED_LABEL: Final = "guided"
"""``metadata.labels`` key naming the guided template a spec was drafted from."""
_DRAW_DIMS: Final = ("chain", "draw")
_MAX_OVERLAP_CELLS: Final = 256
_OVERLAP_BINS: Final = 40
_NOT_OBSERVED: Final = frozenset({Role.CONSTANT, Role.INDEX, Role.MASK})
"""Roles whose values are not observations: their NaN are layout (padding of an outline, a
lookup table), not gaps in the data."""


Measured = tuple[float, str | None]
"""A metric value and its expert message."""


def _diag(diagnostics: list[Diagnostic], code: str) -> Diagnostic | None:
    return next((d for d in diagnostics if d.code == code and d.value is not None), None)


def _posterior(bundle: Bundle) -> xr.Dataset | None:
    post = bundle.model.groups.get("posterior")
    if post is None or not set(_DRAW_DIMS) <= set(map(str, post.dims)):
        return None
    return post


def _convergence(bundle: Bundle, out: dict[str, Measured], why: dict[str, str]) -> None:
    post = _posterior(bundle)
    if post is None:
        reason = "the model has no posterior draws (the fit did not use MCMC)"
        for m in ("r_hat_max", "ess_bulk_min", "divergence_rate"):
            why[m] = reason
        return
    chains = int(post.sizes["chain"])
    rh = _diag(bundle.diagnostics, "RHAT")
    ess = _diag(bundle.diagnostics, "ESS_BULK")
    if rh is not None and rh.value is not None:
        out["r_hat_max"] = (rh.value, rh.message_expert)
    if ess is not None and ess.value is not None:
        out["ess_bulk_min"] = (ess.value, ess.message_expert)
    if (rh is None or ess is None) and chains >= 2:
        worst_r, worst_e = _scan_posterior(post)
        if rh is None and worst_r is not None:
            out["r_hat_max"] = worst_r
        if ess is None and worst_e is not None:
            out["ess_bulk_min"] = worst_e
    if chains < 2:
        why.setdefault("r_hat_max", "R-hat needs at least two chains")
    why.setdefault("ess_bulk_min", "the posterior is too small to measure")
    why.setdefault("r_hat_max", "the posterior is too small to measure")
    stats = bundle.model.groups.get("sample_stats")
    if stats is not None and "diverging" in stats:
        div = np.asarray(stats["diverging"].values).astype(bool)
        if div.size:
            n = int(div.sum())
            out["divergence_rate"] = (
                float(n / div.size),
                f"{n} divergent transitions in {div.size} draws",
            )
    why.setdefault("divergence_rate", "the sampler reports no divergences (not NUTS)")


def _scan_posterior(post: xr.Dataset) -> tuple[Measured | None, Measured | None]:
    """Worst R-hat and bulk ESS over the posterior's (small) variables."""
    worst_r: Measured | None = None
    worst_e: Measured | None = None
    for name in sorted(map(str, post.data_vars)):
        da = post[name].transpose("chain", "draw", ...)
        values = np.asarray(da.values, dtype=float)
        cells = int(np.prod(values.shape[2:])) if values.ndim > 2 else 1
        if cells > 1024:
            continue
        flat = values.reshape(values.shape[0], values.shape[1], cells)
        for i in range(cells):
            x = flat[:, :, i]
            if not np.isfinite(x).all():
                continue
            r, e = rhat(x), ess_bulk(x)
            if math.isfinite(r) and (worst_r is None or r > worst_r[0]):
                worst_r = (r, f"max split R-hat {r:.3f} for {name!r}")
            if math.isfinite(e) and (worst_e is None or e < worst_e[0]):
                worst_e = (e, f"min bulk ESS {e:.0f} for {name!r}")
    return worst_r, worst_e


_HOLDOUT_METRICS: Final = tuple(
    dict.fromkeys(c.metric for c in SCORECARD_CHECKS.values() if c.category == "holdout")
)
"""Every metric a holdout check reads from ``metrics.json``."""

_PERCENT_METRICS: Final = frozenset({"mape", "smape", "wape"})
"""Metrics ``mapi_sdk.metrics`` reports in percent; the scorecard's thresholds and its ``pct``
messages are fractions (``holdout.mape`` warns above 0.10), so these are divided by 100."""


def _holdout(bundle: Bundle, out: dict[str, Measured], why: dict[str, str]) -> None:
    report = bundle.metrics
    n = report.n_holdout
    for metric in _HOLDOUT_METRICS:
        value = report.values.get(metric)
        if value is not None and math.isfinite(value):
            expert = f"{metric} {value:.4g} over {n} held-out values"
            if metric == "accuracy_lift":
                acc = report.values.get("accuracy")
                naive = report.values.get("accuracy_naive")
                if acc is not None and naive is not None:
                    expert = (
                        f"holdout hit rate {acc:.3f} vs {naive:.3f} for always guessing the "
                        f"holdout's most common label ({n} held-out values)"
                    )
            if metric in _PERCENT_METRICS:
                expert = f"{metric} {value:.4g}% over {n} held-out values"
                value = value / 100.0
            out[metric] = (float(value), expert)
        elif metric == "accuracy_lift" and metric not in report.skipped:
            why[metric] = (
                "only models of a categorical target (choice models) report a hit rate. "
                "Evaluate accuracy_lift on one of these models"
            )
        else:
            why[metric] = report.skipped.get(
                metric,
                "the spec does not evaluate it (evaluate.metrics) or has no holdout split",
            )


def _channel_totals(rc: xr.Dataset) -> tuple[xr.DataArray, xr.DataArray] | None:
    """``(contribution(sample, channel), spend(channel))`` at the actual spend (multiplier
    exactly 1; ``None`` for a grid without it, never the nearest point)."""
    if "contribution" not in rc or "current_spend" not in rc:
        return None
    da = rc["contribution"]
    if "channel" not in da.dims or "multiplier" not in da.dims or "sample" not in da.dims:
        return None
    if not np.any(np.asarray(da["multiplier"].values, dtype=float) == 1.0):
        return None
    at_one = da.sel(multiplier=1.0)
    other = [d for d in at_one.dims if d not in ("sample", "channel")]
    if other:
        at_one = at_one.sum(other)
    return at_one.transpose("sample", "channel"), rc["current_spend"]


def _plausibility(bundle: Bundle, out: dict[str, Measured], why: dict[str, str]) -> None:
    metrics = ("negative_roi_share", "roi_max", "max_channel_share", "baseline_share")
    rc = bundle.model.derived.get("response_curves")
    totals = _channel_totals(rc) if rc is not None else None
    if rc is None or totals is None:
        reason = (
            "the model has no response curves (outputs.derived: response_curves)"
            if rc is None or "contribution" not in rc
            else "the response curves have no point at today's spend (multiplier 1)"
        )
        for m in metrics:
            why[m] = reason
        return
    contrib, spend = totals
    channels = [str(c) for c in contrib["channel"].values]
    c = np.asarray(contrib.values, dtype=float)  # (sample, channel)
    s = np.asarray(spend.transpose("channel").values, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        roi = np.where(s[None, :] > 0, c / s[None, :], np.nan)
    has_spend = np.isfinite(roi).any(axis=0)
    if has_spend.any():
        neg = np.where(has_spend, np.nanmean(roi < 0, axis=0), -1.0)
        k = int(np.argmax(neg))
        out["negative_roi_share"] = (
            float(neg[k]),
            f"P(ROI < 0) = {neg[k]:.2f} for {channels[k]!r}",
        )
        mean_roi = np.where(has_spend, np.nanmean(roi, axis=0), -np.inf)
        k = int(np.argmax(mean_roi))
        out["roi_max"] = (
            float(mean_roi[k]),
            f"posterior-mean ROI {mean_roi[k]:.3g} for {channels[k]!r}",
        )
    else:
        why["negative_roi_share"] = why["roi_max"] = "no channel has spend in the fit window"
    if "baseline" not in rc:
        why["max_channel_share"] = why["baseline_share"] = (
            "the response curves have no baseline (fit again with a current plugin version)"
        )
        return
    base = np.asarray(rc["baseline"].transpose("sample").values, dtype=float)
    total = base + c.sum(axis=1)
    ok = np.isfinite(total) & (np.abs(total) > 0)
    if not ok.any():
        why["max_channel_share"] = why["baseline_share"] = "the expected KPI is zero"
        return
    shares = c[ok] / total[ok, None]
    mean_share = shares.mean(axis=0)
    k = int(np.argmax(mean_share))
    out["max_channel_share"] = (
        float(mean_share[k]),
        f"{channels[k]!r} explains {mean_share[k]:.1%} of the expected KPI",
    )
    b = float((base[ok] / total[ok]).mean())
    out["baseline_share"] = (b, f"baseline (intercepts and controls) {b:.1%} of the expected KPI")


def _overlap(prior: np.ndarray, post: np.ndarray) -> float:
    """Overlap coefficient of two samples (shared histogram bins)."""
    lo = float(min(prior.min(), post.min()))
    hi = float(max(prior.max(), post.max()))
    if not hi > lo:
        return 1.0
    edges = np.linspace(lo, hi, _OVERLAP_BINS + 1)
    p, _ = np.histogram(prior, edges)
    q, _ = np.histogram(post, edges)
    return float(np.minimum(p / p.sum(), q / q.sum()).sum())


def _prior_overlap(bundle: Bundle, out: dict[str, Measured], why: dict[str, str]) -> None:
    prior = bundle.model.groups.get("prior")
    post = _posterior(bundle)
    if prior is None or post is None:
        why["prior_overlap_max"] = (
            "the fit kept no prior draws (set fit.prior_predictive or fit.keep.prior)"
        )
        return
    worst: Measured | None = None
    for name in sorted(set(map(str, prior.data_vars)) & set(map(str, post.data_vars))):
        a, b = prior[name], post[name]
        if not set(_DRAW_DIMS) <= set(map(str, a.dims)) or a.dims[2:] != b.dims[2:]:
            continue
        av = np.asarray(a.transpose("chain", "draw", ...).values, dtype=float)
        bv = np.asarray(b.transpose("chain", "draw", ...).values, dtype=float)
        cells = int(np.prod(av.shape[2:])) if av.ndim > 2 else 1
        if cells > _MAX_OVERLAP_CELLS or av.shape[2:] != bv.shape[2:]:
            continue
        af = av.reshape(-1, cells)
        bf = bv.reshape(-1, cells)
        for i in range(cells):
            x, y = af[:, i], bf[:, i]
            x, y = x[np.isfinite(x)], y[np.isfinite(y)]
            if x.size < 10 or y.size < 10:
                continue
            v = _overlap(x, y)
            if worst is None or v > worst[0]:
                label = name if cells == 1 else f"{name}[{i}]"
                worst = (v, f"prior/posterior overlap {v:.2f} for {label!r}")
    if worst is None:
        why["prior_overlap_max"] = "no parameter has both prior and posterior draws"
    else:
        out["prior_overlap_max"] = worst


def _numeric_vars(data: xr.Dataset) -> list[str]:
    return [str(v) for v in data.data_vars if data[v].dtype.kind in "fiub"]


def model_structural_missing(registry: object, bundle: Bundle) -> frozenset[str]:
    """The canonical variables the fitted model declares with NaN by design
    (``CustomModel.structural_missing``); empty when the model or its plugin is not known."""
    try:
        plugin = registry.model(bundle.resolved.model.resolved)  # type: ignore[attr-defined]
    except Exception:  # a missing plugin only means no declaration
        return frozenset()
    return frozenset(str(n) for n in getattr(plugin, "structural_missing", ()) or ())


def _excluded(bundle: Bundle, structural: frozenset[str]) -> dict[str, str]:
    """``{dataset variable: reason}`` for the variables the missing check does not count."""
    out: dict[str, str] = {}
    for name, var in bundle.raw_schema.vars.items():
        if var.role in _NOT_OBSERVED:
            out[name] = f"role {var.role.value}"
    model = bundle.model_schema
    binding = bundle.binding
    if model is not None:
        for name, var in model.vars.items():
            if var.role in _NOT_OBSERVED:
                out.setdefault(binding.to_dataset(name), f"role {var.role.value}")
    for name in sorted(structural):
        out.setdefault(binding.to_dataset(name), "missing by design of the model")
    return out


def missing_count(
    bundle: Bundle, data: xr.Dataset, structural: frozenset[str] = frozenset()
) -> tuple[int, int, dict[str, str]]:
    """``(missing, cells, not counted)`` over the numeric variables of the raw data that hold
    observations (see :data:`_NOT_OBSERVED` and ``structural``, canonical names)."""
    excluded = _excluded(bundle, structural)
    names = [n for n in _numeric_vars(data) if n not in excluded]
    cells = sum(int(data[v].size) for v in names)
    missing = sum(int(data[v].isnull().sum()) for v in names if data[v].dtype.kind == "f")
    return missing, cells, {n: r for n, r in excluded.items() if n in data}


def _data_checks(
    bundle: Bundle,
    data: xr.Dataset | None,
    out: dict[str, Measured],
    why: dict[str, str],
    structural: frozenset[str] = frozenset(),
) -> None:
    if data is None:
        why["missing_fraction"] = why["spend_cv_min"] = "the raw data was not available"
    else:
        missing, cells, skipped = missing_count(bundle, data, structural)
        note = (
            " (not counted: " + ", ".join(f"{n} ({r})" for n, r in sorted(skipped.items())) + ")"
            if skipped
            else ""
        )
        if cells:
            out["missing_fraction"] = (
                missing / cells,
                f"{missing} of {cells} values missing{note}",
            )
        elif _numeric_vars(data):
            why["missing_fraction"] = f"no numeric variable holds observations{note}"
        else:
            why["missing_fraction"] = "the dataset has no numeric variables"
        cv = _spend_cv(bundle, data)
        if cv is None:
            why["spend_cv_min"] = "the dataset has no spend variable along time"
        else:
            out["spend_cv_min"] = cv
    rs = bundle.resolved
    schema = rs.model.input_schema or rs.stages.get(rs.model_stage)
    coords = bundle.fit_coords.get(rs.model_stage)
    time_dims = (
        [d for d, spec in schema.dims.items() if spec.kind == "time"] if schema is not None else []
    )
    if coords is None or not time_dims or time_dims[0] not in coords.coords:
        why["history_years"] = "the model has no time dim"
        return
    t = time_dims[0]
    assert schema is not None
    got = history_years(np.asarray(coords[t].values), schema.dims[t].freq)
    if got is None:
        why["history_years"] = (
            f"the time dim {t!r} has no regular frequency, so the history in years is not known"
        )
        return
    years, expert = got
    out["history_years"] = (years, f"{expert} along {t!r} at the model stage")


def history_years(labels: np.ndarray, freq: str | None) -> Measured | None:
    """Years covered by the time labels: their count over the periods in a year at ``freq``
    (or at the frequency the labels follow); ``None`` when neither is known."""
    n = len(labels)
    per_year = season_length(freq)
    if per_year is None and np.asarray(labels).dtype.kind == "M":
        freq = infer_freq(labels)
        per_year = season_length(freq)
    if per_year is None:
        return None
    return n / per_year, f"{n} periods at {freq} ({per_year} per year)"


def _spend_cv(bundle: Bundle, data: xr.Dataset) -> Measured | None:
    schema = bundle.raw_schema
    binding = bundle.binding
    spend_vars = [n for n, v in schema.vars.items() if v.role is Role.SPEND and n in data]
    if not spend_vars and binding.to_dataset("spend") in data:
        spend_vars = [binding.to_dataset("spend")]
    time_dims = [d for d, spec in schema.dims.items() if spec.kind == "time"]
    for name in spend_vars:
        da = data[name]
        t = next((d for d in time_dims if d in da.dims), None)
        if t is None:
            continue
        channel = binding.to_dataset("channel")
        keep = [t] + ([channel] if channel in da.dims else [])
        other = [d for d in da.dims if d not in keep]
        series = da.sum(other, min_count=1) if other else da
        values = np.asarray(series.transpose(*keep).values, dtype=float)
        values = values.reshape(values.shape[0], -1)
        labels = [str(c) for c in series[channel].values] if channel in series.dims else [name]
        with np.errstate(divide="ignore", invalid="ignore"):
            mean = np.nanmean(values, axis=0)
            cv = np.where(mean > 0, np.nanstd(values, axis=0) / mean, np.nan)
        if not np.isfinite(cv).any():
            continue
        k = int(np.nanargmin(cv))
        return float(cv[k]), f"spend of {labels[k]!r} varies by {cv[k]:.1%} over {t!r}"
    return None


def scorecard_metrics(
    bundle: Bundle, data: xr.Dataset | None, structural_missing: frozenset[str] = frozenset()
) -> tuple[dict[str, Measured], dict[str, str]]:
    """Every scorecard metric the fit can measure, and why the others cannot be
    (``structural_missing``: canonical variables whose NaN the missing check skips)."""
    out: dict[str, Measured] = {}
    why: dict[str, str] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _convergence(bundle, out, why)
        _holdout(bundle, out, why)
        _plausibility(bundle, out, why)
        _prior_overlap(bundle, out, why)
        _data_checks(bundle, data, out, why, structural_missing)
    return out, why


def _plan(labels: Mapping[str, str]) -> tuple[list[str], dict[str, CheckThresholds]]:
    template = GUIDED_TEMPLATES.get(labels.get(GUIDED_LABEL, ""))
    if template is None:
        return list(SCORECARD_CHECKS), {}
    return list(template.scorecard_checks), dict(template.scorecard_thresholds)


def fit_scorecard(
    bundle: Bundle,
    data: xr.Dataset | None = None,
    *,
    structural_missing: frozenset[str] = frozenset(),
) -> Scorecard:
    """The scorecard of a fitted bundle (``data``: the raw dataset, for the data checks;
    ``structural_missing``: the model's canonical variables with NaN by design, see
    :func:`model_structural_missing`)."""
    checks, thresholds = _plan(bundle.spec.metadata.labels)
    measured, why = scorecard_metrics(bundle, data, structural_missing)
    items: list[ScorecardItem] = []
    skipped: dict[str, str] = {}
    for check_id in checks:
        check = SCORECARD_CHECKS[check_id]
        got = measured.get(check.metric)
        if got is None:
            skipped[check_id] = why.get(check.metric, "not measured for this model")[:500]
            continue
        value, expert = got
        if not math.isfinite(value):
            skipped[check_id] = "the metric is not a finite number"
            continue
        items.append(
            evaluate_check(
                check,
                value,
                thresholds=thresholds.get(check_id),
                message_expert=None if expert is None else expert[:2000],
            )
        )
    return rollup(items, skipped)
