"""Classification recipes (Vega-Lite): ROC and precision-recall curves, confusion matrix.

============================  ==========================================================
``core.roc_pr``               ROC and precision-recall curves, AUC with a draw interval
``core.confusion``            2 x 2 confusion matrix at a threshold (fixed or max Youden J),
                              or K x K on the most probable class (multiclass)
============================  ==========================================================

Binary inputs read the probability of the positive class through :func:`binary_scores`,
which also backs the point path of ``core.calibration``: predictive draws
(``posterior_predictive``, ``predictions``), a point probability without sample dims (a
scikit-learn classifier's ``y_hat``), or a score ``summary`` (``stat`` = mean, point or
median), matched to the 0/1 observations of ``observed_data``.

Multiclass inputs (v0.14) hold one probability per class along a category dim the observed
target lacks: ``y_hat(*rows, class)`` with ``meaning = "class_probability"`` (draws add
``chain``/``draw`` in front). :func:`class_scores` reads them; the observed target holds the
class labels (text, or numbers compared as text). Both recipes switch to the multiclass
layout for such inputs: one-vs-rest curves per class and a K x K confusion matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.capabilities import Representation
from pydantic import Field, field_validator

from mapi_plots.palette import BAND, EMPTY, ESTIMATE, OBSERVED, REFERENCE, SURFACE
from mapi_plots.recipes import _stats
from mapi_plots.recipes._base import (
    Figure,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    named,
    recipe,
)
from mapi_plots.recipes._util import SAMPLE, observed_name, pct, sample_dims, stack_samples
from mapi_plots.recipes.diagnostics import _matched

__all__ = [
    "BinaryScores",
    "ClassScores",
    "ConfusionSettings",
    "RocPrSettings",
    "binary_auc",
    "binary_scores",
    "class_scores",
    "confusion",
    "roc_pr",
]

_DRAW_GROUPS = ("posterior_predictive", "predictions", "draws")
_SUMMARY_STATS = ("mean", "point", "median")
_TOL = 1e-9
_MAX_AUC_DRAWS = 500
_MAX_MACRO_AUC_DRAWS = 200
_MAX_CURVE_CLASSES = 12
_MAX_MATRIX_CLASSES = 30
_OTHER = "(other)"
_CLASS_PROBABILITY = "class_probability"


@dataclass(slots=True)
class BinaryScores:
    """Predicted probabilities of the positive class matched to 0/1 observations."""

    p: np.ndarray  # (N,) probability of the positive class
    y: np.ndarray  # (N,) 0/1
    draws: np.ndarray | None  # (S, N) when the input had draws
    var: str
    observed: str
    positive: float
    source: Literal["draws", "point", "summary"]


def _numeric(da: xr.DataArray) -> bool:
    return bool(np.issubdtype(da.dtype, np.number) or da.dtype == bool)


def _in_unit(values: np.ndarray) -> bool:
    v = values[np.isfinite(values)]
    return bool(v.size) and float(v.min()) >= -_TOL and float(v.max()) <= 1 + _TOL


def _candidates(inp: RecipeInput, var: str | None) -> tuple[xr.DataArray, str]:
    """``(prediction array, source)``: draws first, then a point probability, then a summary."""
    seen: list[str] = []
    datasets = [(g, inp.find(g)) for g in _DRAW_GROUPS]
    for _, ds in datasets:  # draws
        if ds is None:
            continue
        for v in ds.data_vars:
            da = ds[v]
            if not _numeric(da) or (var is not None and str(v) != var):
                continue
            seen.append(str(v))
            if sample_dims(da):
                return da, "draws"
    for _, ds in datasets:  # point probabilities
        if ds is None:
            continue
        for v in ds.data_vars:
            da = ds[v]
            if not _numeric(da) or sample_dims(da) or (var is not None and str(v) != var):
                continue
            if var is not None or _in_unit(np.asarray(da.values, dtype=float)):
                return da, "point"
    summary = inp.find("summary")
    if summary is not None:
        for v in summary.data_vars:
            da = summary[v]
            if not _numeric(da) or "stat" not in da.dims or (var is not None and str(v) != var):
                continue
            stats = [str(x) for x in da["stat"].values] if "stat" in da.coords else []
            for name in _SUMMARY_STATS:
                if name in stats:
                    return da.sel(stat=name, drop=True), "summary"
            if var is not None:
                raise RecipeDataError(
                    f"the summary of {var!r} has no {', '.join(_SUMMARY_STATS)} statistic"
                )
    if var is not None:
        raise RecipeDataError(
            f"no predicted variable {var!r} in the groups {', '.join(_DRAW_GROUPS)} or summary"
        )
    raise RecipeDataError(
        "no predicted probabilities: expected draws, a point prediction in [0, 1] or a "
        f"summary with a mean in the groups {', '.join(_DRAW_GROUPS)} or summary"
    )


def binary_scores(
    inp: RecipeInput,
    var: str | None,
    observed_var: str | None,
    positive: float | None,
    *,
    not_binary: str | None = None,
) -> BinaryScores:
    """The probability of the positive class per observation, and the 0/1 observations.

    ``p`` is the mean over draws when the prediction has them. Observed values must have
    exactly two classes (or one class that is 0 or 1); ``positive`` (default: the larger
    value) maps to 1. ``not_binary`` replaces the error text for a non-binary target.
    """
    da, source = _candidates(inp, var)
    name = str(da.name)
    obs = inp.find("observed_data", "observed")
    st = da if source == "draws" else da.expand_dims({SAMPLE: 1})
    draws, y = _matched(xr.Dataset({name: st}), obs, name, observed_var)
    obs_name = observed_name(obs, name, observed_var) or name
    classes = np.unique(y)
    if classes.size > 2 or (classes.size == 1 and classes[0] not in (0.0, 1.0)):
        raise RecipeDataError(
            not_binary
            or f"observed {obs_name!r} is not binary: it has {classes.size} distinct values "
            "(a binary classification plot needs two classes)"
        )
    if positive is None:
        positive = float(classes.max()) if classes.size == 2 else 1.0
    elif classes.size == 2 and positive not in classes:
        raise RecipeDataError(
            f"positive={positive:g} is not a value of {obs_name!r} "
            f"({', '.join(f'{c:g}' for c in classes)})"
        )
    if not _in_unit(draws):
        raise RecipeDataError(
            not_binary or f"the predictions of {name!r} are not probabilities in [0, 1]"
        )
    draws = np.clip(draws, 0.0, 1.0)
    return BinaryScores(
        p=draws.mean(axis=0),
        y=(y == positive).astype(float),
        draws=draws if source == "draws" else None,
        var=name,
        observed=obs_name,
        positive=float(positive),
        source=source,  # type: ignore[arg-type]
    )


@dataclass(slots=True)
class ClassScores:
    """Predicted class probabilities matched to observed class labels (multiclass)."""

    p: np.ndarray  # (N, K) probabilities (mean over draws), rows sum to 1
    codes: np.ndarray  # (N,) index of the observed class in ``classes``
    classes: list[str]  # K class labels (the category coord as text)
    draws: np.ndarray | None  # (S, N, K) when the input had draws
    var: str
    observed: str
    source: Literal["draws", "point", "summary"]
    category: str  # the category dim of the prediction
    unmatched: int = 0  # observations whose label is not a class (left out)


def _label_text(value: Any) -> str | None:
    """A class label as text; ``None`` for missing or empty. ``3.0`` becomes ``"3"``."""
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if not np.isfinite(value):
            return None
        return str(int(value)) if value.is_integer() else f"{value:g}"
    text = str(value)
    return text if text.strip() else None


def _category_dim(da: xr.DataArray, obs: xr.DataArray) -> str | None:
    """The one dim of a prediction that the observed target lacks (besides sample dims)."""
    skip = {*sample_dims(da), "stat"}
    extra = [str(d) for d in da.dims if d not in obs.dims and d not in skip]
    if len(extra) != 1 or not set(map(str, obs.dims)) <= set(map(str, da.dims)):
        return None
    return extra[0]


def _class_candidate(
    inp: RecipeInput, var: str | None, observed_var: str | None
) -> tuple[xr.DataArray, str, str, str] | None:
    """``(prediction, source, observed name, category dim)`` of a multiclass input, or None.

    A prediction is multiclass when it says so (``meaning: class_probability``) and has one
    dim more than its observed target; the draw groups come first, then a score summary.
    """
    obs = inp.find("observed_data", "observed")
    if obs is None:
        return None
    found: list[tuple[xr.DataArray, str]] = []
    for g in _DRAW_GROUPS:
        ds = inp.find(g)
        if ds is None:
            continue
        for v in ds.data_vars:
            if var is None or str(v) == var:
                da = ds[v]
                found.append((da, "draws" if sample_dims(da) else "point"))
    summary = inp.find("summary")
    if summary is not None:
        for v in summary.data_vars:
            da = summary[v]
            if (var is None or str(v) == var) and "stat" in da.dims and "stat" in da.coords:
                stats = [str(x) for x in da["stat"].values]
                stat = next((n for n in _SUMMARY_STATS if n in stats), None)
                if stat is not None:
                    found.append((da.sel(stat=stat, drop=True), "summary"))
    for da, source in found:
        if not _numeric(da) or da.attrs.get("meaning") != _CLASS_PROBABILITY:
            continue
        name = observed_name(obs, str(da.name), observed_var)
        if name is None or name not in obs.data_vars:
            continue
        cat = _category_dim(da, obs[name])
        if cat is not None:
            return da, source, name, cat
    return None


def class_scores(
    inp: RecipeInput, var: str | None = None, observed_var: str | None = None
) -> ClassScores | None:
    """Class probabilities per observation and the observed class codes, or ``None`` when
    the input is not a multiclass prediction (see the module doc).

    ``p`` is the mean over draws, clipped to ``[0, 1]`` and normalised per row. Observations
    without a label are left out; labels that are not a class are left out and counted.
    """
    hit = _class_candidate(inp, var, observed_var)
    if hit is None:
        return None
    da, source, obs_name, cat = hit
    obs = inp.find("observed_data", "observed")
    assert obs is not None
    o = obs[obs_name]
    st = stack_samples(da) if source == "draws" else da.expand_dims({SAMPLE: 1})
    st, o = xr.align(st, o, join="inner", exclude=[SAMPLE, cat])
    dims = [str(d) for d in o.dims]
    st = st.transpose(SAMPLE, *dims, cat)
    classes = (
        [str(_label_text(c)) for c in np.asarray(da[cat].values).tolist()]
        if (cat in da.coords)
        else [str(i) for i in range(da.sizes[cat])]
    )
    k = len(classes)
    draws = np.asarray(st.values, dtype=float).reshape(st.sizes[SAMPLE], -1, k)
    labels = [_label_text(v) for v in np.asarray(o.transpose(*dims).values).reshape(-1).tolist()]
    index = {c: j for j, c in enumerate(classes)}
    codes = np.array([-1 if t is None else index.get(t, -2) for t in labels], dtype=np.int64)
    finite = np.all(np.isfinite(draws), axis=(0, 2))
    keep = (codes >= 0) & finite
    unmatched = int(np.sum((codes == -2) & finite))
    if not keep.any():
        raise RecipeDataError(
            f"no observed labels of {obs_name!r} match the classes of {da.name!r} "
            f"({', '.join(classes[:5])})"
        )
    if not _in_unit(draws[:, keep]):
        raise RecipeDataError(f"the predictions of {da.name!r} are not probabilities in [0, 1]")
    draws = np.clip(draws[:, keep], 0.0, 1.0)
    p = draws.mean(axis=0)
    total = p.sum(axis=1, keepdims=True)
    p = np.where(total > 0, p / np.where(total > 0, total, 1.0), 1.0 / k)
    return ClassScores(
        p=p,
        codes=codes[keep],
        classes=classes,
        draws=draws if source == "draws" else None,
        var=str(da.name),
        observed=obs_name,
        source=source,  # type: ignore[arg-type]
        category=cat,
        unmatched=unmatched,
    )


def _macro_auc(p: np.ndarray, codes: np.ndarray) -> tuple[float | None, dict[int, float]]:
    """Macro one-vs-rest AUC over the classes with both outcomes, and the per-class AUCs."""
    per = {
        j: a
        for j in range(p.shape[1])
        if (a := binary_auc(p[:, j], (codes == j).astype(float))) is not None
    }
    return (float(np.mean(list(per.values()))) if per else None), per


def binary_auc(p: np.ndarray, y: np.ndarray) -> float | None:
    """ROC AUC by Mann-Whitney with average ranks; ``None`` when a class is missing."""
    pos = y == 1
    n1, n0 = int(pos.sum()), int((~pos).sum())
    if not n1 or not n0:
        return None
    ranks = _stats._rankdata(np.asarray(p, dtype=float))
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _curve(p: np.ndarray, y: np.ndarray) -> pd.DataFrame:
    """True and false positives at each distinct threshold, from the highest score down."""
    order = np.argsort(-p, kind="mergesort")
    ps, ys = p[order], y[order]
    last = np.r_[np.flatnonzero(ps[1:] != ps[:-1]), ps.size - 1]  # end of each tie group
    tp = np.cumsum(ys)[last]
    fp = (last + 1) - tp
    return pd.DataFrame({"threshold": ps[last], "tp": tp, "fp": fp})


def _class_label(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _youden(curve: pd.DataFrame, n1: int, n0: int) -> float:
    j = curve["tp"] / n1 - curve["fp"] / n0
    return float(curve["threshold"].iloc[int(np.argmax(j.to_numpy()))])


class _BinarySettings(RecipeSettings):
    var: str | None = None
    observed_var: str | None = None
    positive: float | None = Field(
        None, description="Observed value of the positive class (default: the larger value)"
    )


# ----------------------------------------------------------------------------------- roc_pr


class RocPrSettings(_BinarySettings):
    max_points: int = Field(500, ge=20, le=5000, description="Thresholds drawn per curve")
    classes: list[str] | None = Field(
        None,
        min_length=1,
        max_length=_MAX_CURVE_CLASSES,
        description="Multiclass: the classes to draw (default: the most frequent, at most 12)",
    )
    width: int = Field(320, ge=80, le=2000)
    height: int = Field(280, ge=60, le=2000)


def _verdict(auc: float) -> str:
    if auc >= 0.9:
        return "separates the classes very well"
    if auc >= 0.8:
        return "separates the classes well"
    if auc >= 0.7:
        return "separates the classes fairly well"
    return "does not separate the classes well"


@recipe(
    "core.roc_pr",
    title="ROC and precision-recall curves",
    settings=RocPrSettings,
    persona="both",
    requires_representation=[Representation.DRAWS, Representation.POINT],
    summary="How well the predicted probabilities put the positive cases above the negative "
    "cases, at every threshold (binary or multiclass: one curve per class).",
    tags=["diagnostic"],
)
def roc_pr(inp: RecipeInput, s: RocPrSettings) -> Figure:
    """ROC (FPR, TPR) and precision-recall curves with AUC and average precision."""
    mc = class_scores(inp, s.var, s.observed_var)
    if mc is not None:
        return _roc_pr_multiclass(mc, s)
    sc = binary_scores(inp, s.var, s.observed_var, s.positive)
    n1, n0 = int(sc.y.sum()), int(sc.y.size - sc.y.sum())
    if not n1 or not n0:
        raise RecipeDataError(
            f"the observed values of {sc.observed!r} have only one class; ROC and PR curves "
            "need both"
        )
    auc = binary_auc(sc.p, sc.y)
    assert auc is not None
    cur = _curve(sc.p, sc.y)
    tpr, fpr = cur["tp"].to_numpy() / n1, cur["fp"].to_numpy() / n0
    precision = cur["tp"].to_numpy() / (cur["tp"].to_numpy() + cur["fp"].to_numpy())
    ap = float(np.sum(np.diff(np.r_[0.0, tpr]) * precision))
    thr = cur["threshold"].to_numpy()
    keep = np.unique(np.r_[np.linspace(0, thr.size - 1, min(thr.size, s.max_points)).round(), 0])
    keep = keep.astype(int)
    roc = pd.DataFrame(
        {
            "fpr": np.r_[0.0, fpr[keep]],
            "tpr": np.r_[0.0, tpr[keep]],
            "threshold": np.r_[1.0, thr[keep]],
        }
    )
    pr = pd.DataFrame(
        {
            "recall": np.r_[0.0, tpr[keep]],
            "precision": np.r_[1.0, precision[keep]],
            "threshold": np.r_[1.0, thr[keep]],
        }
    )
    base_rate = n1 / sc.y.size
    unit = alt.Scale(domain=[0, 1], nice=False)
    diag = pd.DataFrame({"x": [0.0, 1.0], "base": [base_rate, base_rate]})
    tip = alt.Tooltip("threshold:Q", format=".3f")
    roc_chart = alt.layer(
        alt.Chart(named("reference"))
        .mark_line(color=REFERENCE, strokeDash=[4, 4])
        .encode(
            x=alt.X("x:Q", title="false positive rate", scale=unit),
            y=alt.Y("x:Q", title="true positive rate", scale=unit),
        ),
        alt.Chart(named("roc"))
        .mark_line(color=ESTIMATE, strokeWidth=2, interpolate="step-after")
        .encode(
            x="fpr:Q",
            y="tpr:Q",
            order="fpr:Q",
            tooltip=[alt.Tooltip("fpr:Q", format=".2f"), alt.Tooltip("tpr:Q", format=".2f"), tip],
        ),
    ).properties(width=s.width, height=s.height, title=f"ROC curve (AUC {auc:.3f})")
    pr_chart = alt.layer(
        alt.Chart(named("reference"))
        .mark_line(color=REFERENCE, strokeDash=[4, 4])
        .encode(
            x=alt.X("x:Q", title="recall", scale=unit),
            y=alt.Y("base:Q", title="precision", scale=unit),
        ),
        alt.Chart(named("pr"))
        .mark_line(color=BAND, strokeWidth=2)
        .encode(
            x="recall:Q",
            y="precision:Q",
            order="recall:Q",
            tooltip=[
                alt.Tooltip("recall:Q", format=".2f"),
                alt.Tooltip("precision:Q", format=".2f"),
                tip,
            ],
        ),
    ).properties(
        width=s.width, height=s.height, title=f"Precision-recall (average precision {ap:.3f})"
    )
    chart = alt.hconcat(roc_chart, pr_chart)
    pos = _class_label(sc.positive)
    caption = (
        f"The left plot shows the true positive rate against the false positive rate of "
        f"{sc.var} at each threshold. The right plot shows the precision against the recall. "
        f"The AUC is {auc:.3f}: the model {_verdict(auc)}. {pct(base_rate)} of the "
        f"{sc.y.size} observations are positive ({sc.observed} = {pos}), so a model without "
        f"skill has a precision of {base_rate:.2f} (the dashed line)."
    )
    if sc.draws is not None and not np.all((sc.draws == 0) | (sc.draws == 1)):
        idx = np.unique(np.linspace(0, sc.draws.shape[0] - 1, _MAX_AUC_DRAWS).round().astype(int))
        aucs = [binary_auc(sc.draws[i], sc.y) for i in idx]
        lo, hi = np.quantile([a for a in aucs if a is not None], [0.05, 0.95])
        caption += f" Over {idx.size} draws, the 90% interval of the AUC is {lo:.3f} to {hi:.3f}."
    if sc.y.size < 30:
        caption += f" With only {sc.y.size} observations the curves are not precise."
    return Figure(chart=chart, datasets={"roc": roc, "pr": pr, "reference": diag}, caption=caption)


# -------------------------------------------------------------------------------- confusion


class ConfusionSettings(_BinarySettings):
    threshold: float | Literal["auto"] = Field(
        0.5, description="Predicted probability at or above which a case is positive, or auto"
    )
    normalize: Literal["none", "true"] = Field(
        "none", description="none: counts; true: the share of each observed class"
    )
    width: int = Field(260, ge=80, le=2000)
    height: int = Field(240, ge=60, le=2000)

    @field_validator("threshold")
    @classmethod
    def _threshold(cls, v: Any) -> Any:
        if v != "auto" and not 0 <= float(v) <= 1:
            raise ValueError("threshold must be in [0, 1] or 'auto'")
        return v


@recipe(
    "core.confusion",
    title="Confusion matrix",
    settings=ConfusionSettings,
    persona="both",
    requires_representation=[Representation.DRAWS, Representation.POINT],
    summary="How many cases the model classifies correctly and wrongly: at a threshold on the "
    "predicted probability (binary), or by the most probable class (multiclass).",
    tags=["diagnostic"],
)
def confusion(inp: RecipeInput, s: ConfusionSettings) -> Figure:
    """2 x 2 confusion matrix (counts or row rates) with accuracy, precision, recall, F1."""
    mc = class_scores(inp, s.var, s.observed_var)
    if mc is not None:
        return _confusion_multiclass(mc, s)
    sc = binary_scores(inp, s.var, s.observed_var, s.positive)
    n1, n0 = int(sc.y.sum()), int(sc.y.size - sc.y.sum())
    auto = s.threshold == "auto"
    if auto:
        if not n1 or not n0:
            raise RecipeDataError(
                f"threshold=auto needs both classes in {sc.observed!r}; set a threshold"
            )
        thr = _youden(_curve(sc.p, sc.y), n1, n0)
    else:
        thr = float(s.threshold)
    pred = sc.p >= thr
    actual = sc.y == 1
    tp = int(np.sum(pred & actual))
    fp = int(np.sum(pred & ~actual))
    fn = int(np.sum(~pred & actual))
    tn = int(np.sum(~pred & ~actual))
    pos = _class_label(sc.positive)
    neg = f"not {pos}"
    rows = []
    for act, row_total, cells in (
        (pos, n1, ((pos, tp), (neg, fn))),
        (neg, n0, ((pos, fp), (neg, tn))),
    ):
        for predicted, count in cells:
            rate = count / row_total if row_total else float("nan")
            value = rate if s.normalize == "true" else float(count)
            rows.append(
                {
                    "observed": act,
                    "predicted": predicted,
                    "count": count,
                    "rate": rate,
                    "value": value,
                    "label": pct(rate) if s.normalize == "true" else str(count),
                    "correct": act == predicted,
                }
            )
    matrix = pd.DataFrame(rows)
    top = float(np.nanmax(matrix["value"].to_numpy())) if len(matrix) else 1.0
    matrix["dark"] = matrix["value"] > 0.6 * top
    order = [pos, neg]
    base = alt.Chart(named("matrix")).encode(
        x=alt.X("predicted:N", sort=order, title=f"predicted {sc.observed}"),
        y=alt.Y("observed:N", sort=order, title=f"observed {sc.observed}"),
    )
    title = "share of the observed class" if s.normalize == "true" else "observations"
    chart = alt.layer(
        base.mark_rect(stroke=SURFACE, strokeWidth=2).encode(
            color=alt.Color(
                "value:Q",
                title=title,
                scale=alt.Scale(domain=[0, max(top, 1e-12)], range=[EMPTY, ESTIMATE]),
            ),
            tooltip=[
                "observed:N",
                "predicted:N",
                "count:Q",
                alt.Tooltip("rate:Q", format=".1%", title="share of the row"),
            ],
        ),
        base.mark_text(fontSize=14).encode(
            text="label:N",
            color=alt.condition(alt.datum.dark, alt.value(SURFACE), alt.value(OBSERVED)),
        ),
    ).properties(width=s.width, height=s.height, title=f"Threshold {thr:.3g}")
    n = sc.y.size

    def ratio(a: int, b: int) -> float:
        return a / b if b else float("nan")

    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = ratio(2 * tp, 2 * tp + fp + fn)
    f1_text = f"{f1:.2f}" if np.isfinite(f1) else "n/a"
    caption = (
        f"The matrix counts the {n} observations of {sc.var} by the observed class (rows) and "
        f"the predicted class (columns). A case is positive ({sc.observed} = {pos}) when the "
        f"predicted probability is {thr:.3g} or more. Accuracy {pct(ratio(tp + tn, n))}, "
        f"precision {pct(precision)}, recall (sensitivity) {pct(recall)}, specificity "
        f"{pct(ratio(tn, tn + fp))}, F1 {f1_text}."
    )
    if auto:
        caption += (
            " The threshold gives the largest difference between the true positive rate and "
            "the false positive rate."
        )
    if not n1 or not n0:
        caption += " The observed values have only one class."
    return Figure(chart=chart, datasets={"matrix": matrix}, caption=caption)


# ------------------------------------------------------------------------------- multiclass


def _ignored_note(s: RecipeSettings, names: tuple[str, ...]) -> str:
    used = [n for n in names if n in s.model_fields_set]
    if not used:
        return ""
    return f" The {' and '.join(used)} settings apply to two classes only; the plot ignores them."


def _class_counts(mc: ClassScores) -> np.ndarray:
    return np.bincount(mc.codes, minlength=len(mc.classes))


def _roc_pr_multiclass(mc: ClassScores, s: RocPrSettings) -> Figure:
    """One-vs-rest ROC and precision-recall curves per class, coloured by class."""
    counts = _class_counts(mc)
    n = mc.codes.size
    macro, per = _macro_auc(mc.p, mc.codes)
    if macro is None or len(per) < 2:
        raise RecipeDataError(
            f"the observed values of {mc.observed!r} have fewer than two classes; ROC and PR "
            "curves need at least two"
        )
    if s.classes is not None:
        unknown = [c for c in s.classes if c not in mc.classes]
        if unknown:
            raise RecipeDataError(f"classes {', '.join(unknown[:5])} are not classes of {mc.var!r}")
        drawn = [mc.classes.index(c) for c in s.classes if mc.classes.index(c) in per]
    else:
        ranked = sorted(per, key=lambda j: (-int(counts[j]), mc.classes[j]))
        drawn = ranked[:_MAX_CURVE_CLASSES]
    if not drawn:
        raise RecipeDataError("none of the chosen classes has both outcomes in the observations")
    roc_parts, pr_parts = [], []
    for j in drawn:
        y = (mc.codes == j).astype(float)
        cur = _curve(mc.p[:, j], y)
        n1, n0 = int(y.sum()), int(y.size - y.sum())
        tp, fp = cur["tp"].to_numpy(), cur["fp"].to_numpy()
        tpr, fpr, prec = tp / n1, fp / n0, tp / (tp + fp)
        thr = cur["threshold"].to_numpy()
        keep = np.unique(
            np.r_[np.linspace(0, thr.size - 1, min(thr.size, s.max_points)).round(), 0]
        ).astype(int)
        label = mc.classes[j]
        roc_parts.append(
            pd.DataFrame(
                {
                    "class": label,
                    "fpr": np.r_[0.0, fpr[keep]],
                    "tpr": np.r_[0.0, tpr[keep]],
                    "threshold": np.r_[1.0, thr[keep]],
                }
            )
        )
        pr_parts.append(
            pd.DataFrame(
                {
                    "class": label,
                    "recall": np.r_[0.0, tpr[keep]],
                    "precision": np.r_[1.0, prec[keep]],
                    "threshold": np.r_[1.0, thr[keep]],
                }
            )
        )
    roc, pr = pd.concat(roc_parts, ignore_index=True), pd.concat(pr_parts, ignore_index=True)
    order = [mc.classes[j] for j in drawn]
    unit = alt.Scale(domain=[0, 1], nice=False)
    diag = pd.DataFrame({"x": [0.0, 1.0]})
    color = alt.Color("class:N", sort=order, title=mc.observed)
    tip = alt.Tooltip("threshold:Q", format=".3f")
    roc_chart = alt.layer(
        alt.Chart(named("reference"))
        .mark_line(color=REFERENCE, strokeDash=[4, 4])
        .encode(
            x=alt.X("x:Q", title="false positive rate", scale=unit),
            y=alt.Y("x:Q", title="true positive rate", scale=unit),
        ),
        alt.Chart(named("roc"))
        .mark_line(strokeWidth=2, interpolate="step-after")
        .encode(
            x="fpr:Q",
            y="tpr:Q",
            color=color,
            order="fpr:Q",
            tooltip=[
                "class:N",
                alt.Tooltip("fpr:Q", format=".2f"),
                alt.Tooltip("tpr:Q", format=".2f"),
                tip,
            ],
        ),
    ).properties(
        width=s.width,
        height=s.height,
        title=f"ROC curves, one class against the rest (macro AUC {macro:.3f})",
    )
    pr_chart = (
        alt.Chart(named("pr"))
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("recall:Q", title="recall", scale=unit),
            y=alt.Y("precision:Q", title="precision", scale=unit),
            color=color,
            order="recall:Q",
            tooltip=[
                "class:N",
                alt.Tooltip("recall:Q", format=".2f"),
                alt.Tooltip("precision:Q", format=".2f"),
                tip,
            ],
        )
        .properties(width=s.width, height=s.height, title="Precision-recall per class")
    )
    chart = alt.hconcat(roc_chart, pr_chart)
    k = len(mc.classes)
    best = max(per, key=lambda j: per[j])
    worst = min(per, key=lambda j: per[j])
    caption = (
        f"Each curve compares one class of {mc.observed} with all the other classes, from the "
        f"predicted probability of that class in {mc.var}. The left plot shows the true "
        f"positive rate against the false positive rate. The right plot shows the precision "
        f"against the recall. The macro AUC (the mean over {len(per)} classes) is {macro:.3f}: "
        f"the model {_verdict(macro)}. The class {mc.classes[best]} has the highest AUC "
        f"({per[best]:.3f}) and the class {mc.classes[worst]} the lowest ({per[worst]:.3f})."
    )
    if len(drawn) < k:
        caption += f" The plot shows {len(drawn)} of the {k} classes" + (
            " (the most frequent)." if s.classes is None else "."
        )
    if mc.draws is not None:
        idx = np.unique(
            np.linspace(0, mc.draws.shape[0] - 1, _MAX_MACRO_AUC_DRAWS).round().astype(int)
        )
        aucs = [a for i in idx if (a := _macro_auc(mc.draws[i], mc.codes)[0]) is not None]
        if aucs and not np.all((mc.draws == 0) | (mc.draws == 1)):
            lo, hi = np.quantile(aucs, [0.05, 0.95])
            caption += (
                f" Over {idx.size} draws, the 90% interval of the macro AUC is {lo:.3f} to "
                f"{hi:.3f}."
            )
    if mc.unmatched:
        caption += (
            f" {mc.unmatched} observations have a label that is not a class; the plot ignores them."
        )
    if n < 30:
        caption += f" With only {n} observations the curves are not precise."
    caption += _ignored_note(s, ("positive",))
    return Figure(chart=chart, datasets={"roc": roc, "pr": pr, "reference": diag}, caption=caption)


def _confusion_multiclass(mc: ClassScores, s: ConfusionSettings) -> Figure:
    """K x K confusion matrix on the most probable class, with accuracy and macro F1."""
    k = len(mc.classes)
    counts = _class_counts(mc)
    pred = np.argmax(mc.p, axis=1)
    n = mc.codes.size
    full = np.zeros((k, k), dtype=np.int64)
    np.add.at(full, (mc.codes, pred), 1)
    accuracy = float(np.trace(full) / n) if n else float("nan")
    f1s = []
    for j in range(k):
        tp = full[j, j]
        denom = full[j, :].sum() + full[:, j].sum()
        if full[j, :].sum() > 0:  # classes that occur in the observations
            f1s.append(2 * tp / denom if denom else 0.0)
    macro_f1 = float(np.mean(f1s)) if f1s else float("nan")
    if k > _MAX_MATRIX_CLASSES:
        top = sorted(range(k), key=lambda j: (-int(counts[j]), mc.classes[j]))
        shown = sorted(top[: _MAX_MATRIX_CLASSES - 1])
        group = np.full(k, len(shown), dtype=np.int64)
        group[shown] = np.arange(len(shown))
        labels = [mc.classes[j] for j in shown] + [_OTHER]
        mat = np.zeros((len(labels), len(labels)), dtype=np.int64)
        np.add.at(mat, (group[mc.codes], group[pred]), 1)
    else:
        labels = list(mc.classes)
        mat = full
    rows = []
    for i, act in enumerate(labels):
        row_total = int(mat[i].sum())
        for j, predicted in enumerate(labels):
            count = int(mat[i, j])
            rate = count / row_total if row_total else float("nan")
            value = rate if s.normalize == "true" else float(count)
            rows.append(
                {
                    "observed": act,
                    "predicted": predicted,
                    "count": count,
                    "rate": rate,
                    "value": value,
                    "label": pct(rate) if s.normalize == "true" else str(count),
                    "correct": act == predicted,
                }
            )
    matrix = pd.DataFrame(rows)
    top_value = float(np.nanmax(matrix["value"].to_numpy())) if len(matrix) else 1.0
    matrix["dark"] = matrix["value"] > 0.6 * top_value
    side = max(s.width, s.height, 22 * len(labels))
    base = alt.Chart(named("matrix")).encode(
        x=alt.X("predicted:N", sort=labels, title=f"predicted {mc.observed}"),
        y=alt.Y("observed:N", sort=labels, title=f"observed {mc.observed}"),
    )
    title = "share of the observed class" if s.normalize == "true" else "observations"
    layers = [
        base.mark_rect(stroke=SURFACE, strokeWidth=1).encode(
            color=alt.Color(
                "value:Q",
                title=title,
                scale=alt.Scale(domain=[0, max(top_value, 1e-12)], range=[EMPTY, ESTIMATE]),
            ),
            tooltip=[
                "observed:N",
                "predicted:N",
                "count:Q",
                alt.Tooltip("rate:Q", format=".1%", title="share of the row"),
            ],
        )
    ]
    if len(labels) <= 12:
        layers.append(
            base.mark_text(fontSize=12).encode(
                text="label:N",
                color=alt.condition(alt.datum.dark, alt.value(SURFACE), alt.value(OBSERVED)),
            )
        )
    chart = alt.layer(*layers).properties(
        width=side, height=side, title="Most probable class against the observed class"
    )
    f1_text = f"{macro_f1:.2f}" if np.isfinite(macro_f1) else "n/a"
    caption = (
        f"The matrix counts the {n} observations of {mc.var} by the observed class (rows) and "
        f"the most probable class (columns), for {k} classes. Accuracy {pct(accuracy)}, macro "
        f"F1 {f1_text} (the mean F1 of the observed classes)."
    )
    if k > _MAX_MATRIX_CLASSES:
        caption += (
            f" The matrix shows the {_MAX_MATRIX_CLASSES - 1} most frequent classes; "
            f"{_OTHER} holds the other {k - len(labels) + 1}."
        )
    if mc.unmatched:
        caption += (
            f" {mc.unmatched} observations have a label that is not a class; the matrix "
            "ignores them."
        )
    caption += _ignored_note(s, ("threshold", "positive"))
    return Figure(chart=chart, datasets={"matrix": matrix}, caption=caption)
