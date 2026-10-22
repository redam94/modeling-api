"""Family accuracy of a layout 2 dashboard: errors, classification scores and causal effects
(v0.17, pure Python).

The server joins an actuals upload to the score summary of its readout and calls these
functions; the web only draws the numbers. They follow ``mapi_sdk.metrics.compute_metrics``
(the holdout metrics of a fit) so a dashboard and a fit report agree (a parity test in
``packages/sdk`` checks them to 1e-9):

* :func:`error_metrics_of` - ``mae``, ``rmse``, ``bias`` (mean of predicted − actual),
  ``mape`` (actuals of 0 left out), ``smape``, ``wape``, ``r2`` and ``band_hit_rate``; the
  percentages are in percent, as v0.16 :func:`~mapi_spec.dashboards.metrics_of`.
* :func:`classification_metrics_of` - a binary classifier: ``accuracy`` at the analyst's
  ``threshold``, ``brier`` ``mean((p - y)^2)``, ``log_score`` (mean log probability of the
  observed class, floored at 1e-12), ``auc`` (Mann-Whitney with average ranks: ties count one
  half; ``None`` with one class only), the predicted and observed share of the positive class,
  ten calibration bins and the confusion matrix.
* :func:`multiclass_metrics_of` - ``accuracy`` (the most probable class is the observed one),
  the multi-class Brier score ``sum_j (p_j - [j = y])^2``, ``log_score`` and the macro
  one-vs-rest ``auc`` over the classes that have both outcomes (``None`` below two).
* :func:`effect_of` - the updated effect of a causal model: Σ actual − the counterfactual
  total, with the range ``[Σa − cf_high, Σa − cf_low]`` (exact: the actuals are constants).

Inputs are checked, never repaired: NaN or infinite numbers, more than :data:`MAX_ROWS`
rows, more than :data:`MAX_CLASSES` labels, labels longer than :data:`MAX_LABEL_CHARS`
characters and probabilities outside ``[0, 1]`` raise :class:`ValueError`.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Final

from mapi_spec.calibration import DistributionSummary
from mapi_spec.dashboard_v2 import (
    CalibrationBin,
    ClassAccuracy,
    ConfusionMatrix,
    EffectAccuracy,
    ErrorAccuracy,
)

__all__ = [
    "CALIBRATION_BINS",
    "LOG_FLOOR",
    "MAX_CLASSES",
    "MAX_CONFUSION_LABELS",
    "MAX_LABEL_CHARS",
    "MAX_ROWS",
    "auc_of",
    "classification_metrics_of",
    "effect_of",
    "error_metrics_of",
    "match_label",
    "multiclass_metrics_of",
    "normalise_label",
]

MAX_ROWS: Final = 5000
"""The most rows (pairs or cases) one call takes (``MAX_ROW_ACTUALS``, ``MAX_BATCH_ROWS``)."""
MAX_CLASSES: Final = 50
MAX_LABEL_CHARS: Final = 64
MAX_CONFUSION_LABELS: Final = 20
"""A confusion matrix with more classes is left out (the snapshot block holds 20)."""
CALIBRATION_BINS: Final = 10
LOG_FLOOR: Final = 1e-12
_PROB_TOL: Final = 1e-9
_LOOSE: Final = re.compile(r"[^0-9a-z]+")
_INTEGRAL: Final = re.compile(r"^[+-]?\d+\.0*$")

# ===================================================================================== labels


def _label_text(value: object) -> str:
    """A label as text; integral numbers drop the fraction (``3.0`` and ``"3.0"`` -> ``"3"``)."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    text = unicodedata.normalize("NFC", str(value)).strip()
    if _INTEGRAL.match(text):
        return str(int(float(text)))
    return text


def normalise_label(s: str) -> str:
    """The loose form of a class label that uploads are matched on: NFC, trimmed, case-folded,
    integral numbers without the fraction (``"1.0"`` -> ``"1"``) and every run of
    non-alphanumerics one ``_`` (``"Coke Zero"`` -> ``"coke_zero"``), as the loose key of
    ``mapi_sdk.metrics._label_codes``."""
    return _LOOSE.sub("_", _label_text(s).casefold()).strip("_")


def match_label(value: object, classes: Sequence[str]) -> int:
    """The index of the class ``value`` names (``-1`` when none): the exact text first, then
    :func:`normalise_label` (the first class with that loose form wins), as
    ``mapi_sdk.metrics._label_codes``."""
    text = _label_text(value)
    for j, c in enumerate(classes):
        if str(c) == text:
            return j
    key = normalise_label(text)
    for j, c in enumerate(classes):
        if normalise_label(str(c)) == key:
            return j
    return -1


def _check_label(label: str, what: str) -> None:
    if not isinstance(label, str):
        raise ValueError(f"{what} must be text")
    if len(label) > MAX_LABEL_CHARS:
        raise ValueError(f"{what} {label[:20]!r}… is longer than {MAX_LABEL_CHARS} characters")


def _check_rows(n: int) -> None:
    if n > MAX_ROWS:
        raise ValueError(f"{n} rows is more than the {MAX_ROWS} allowed")


def _finite(x: object, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, int | float):
        raise ValueError(f"{what} must be a number")
    v = float(x)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number (NaN and infinity are refused)")
    return v


def _probability(x: object, what: str) -> float:
    p = _finite(x, what)
    if p < -_PROB_TOL or p > 1 + _PROB_TOL:
        raise ValueError(f"{what} must be a probability in [0, 1]")
    return min(max(p, 0.0), 1.0)


def _clean(v: float | None) -> float | None:
    return v if v is not None and math.isfinite(v) else None


# ===================================================================================== errors


def error_metrics_of(
    pairs: Sequence[tuple[float, float]],
    bands: Sequence[tuple[float | None, float | None]] | None = None,
) -> ErrorAccuracy:
    """Errors of numeric predictions. ``pairs``: ``(predicted, actual)`` per period or case;
    ``bands``: the ``(low, high)`` range of each prediction (same length; a ``None`` bound
    leaves that row out of ``band_hit_rate``).

    ``mape`` leaves out actuals of 0 (``None`` when all are 0); ``smape`` rows where both are
    0; ``wape`` and ``r2`` are ``None`` when the actuals sum to 0 or do not vary."""
    _check_rows(len(pairs))
    if bands is not None and len(bands) != len(pairs):
        raise ValueError("bands must have one (low, high) per pair")
    ps: list[float] = []
    actual: list[float] = []
    for i, pair in enumerate(pairs):
        if len(pair) != 2:
            raise ValueError(f"pair {i} must be (predicted, actual)")
        ps.append(_finite(pair[0], f"predicted value {i}"))
        actual.append(_finite(pair[1], f"actual value {i}"))
    n = len(ps)
    hit_rate: float | None = None
    if bands is not None:
        hits = banded = 0
        for i, (lo, hi) in enumerate(bands):
            if lo is None or hi is None:
                continue
            flo, fhi = _finite(lo, f"low bound {i}"), _finite(hi, f"high bound {i}")
            banded += 1
            hits += flo <= actual[i] <= fhi
        hit_rate = hits / banded * 100 if banded else None
    if not n:
        return ErrorAccuracy(n=0, band_hit_rate=hit_rate)
    err = [p - a for p, a in zip(ps, actual, strict=True)]
    abs_err = [abs(e) for e in err]
    pct = [abs(e) / abs(a) for e, a in zip(err, actual, strict=True) if a != 0]
    sm = [
        abs(e) / ((abs(p) + abs(a)) / 2)
        for e, p, a in zip(err, ps, actual, strict=True)
        if (abs(p) + abs(a)) != 0
    ]
    sum_abs_actual = sum(abs(a) for a in actual)
    mean_a = sum(actual) / n
    ss_tot = sum((a - mean_a) ** 2 for a in actual)
    ss_res = sum(e * e for e in err)
    return ErrorAccuracy(
        n=n,
        mae=_clean(sum(abs_err) / n),
        rmse=_clean(math.sqrt(ss_res / n)),
        bias=_clean(sum(err) / n),
        mape=_clean(sum(pct) / len(pct) * 100) if pct else None,
        smape=_clean(sum(sm) / len(sm) * 100) if sm else None,
        wape=_clean(sum(abs_err) / sum_abs_actual * 100) if sum_abs_actual else None,
        r2=_clean(1 - ss_res / ss_tot) if ss_tot else None,
        band_hit_rate=hit_rate,
    )


# ============================================================================= classification


def auc_of(scores: Sequence[float], positive: Sequence[bool]) -> float | None:
    """ROC AUC of ``scores`` for the cases marked ``positive`` (Mann-Whitney U with average
    ranks: a tie between a positive and a negative counts one half). ``None`` when either
    class is missing."""
    if len(scores) != len(positive):
        raise ValueError("scores and positive must have the same length")
    n1 = sum(1 for b in positive if b)
    n0 = len(positive) - n1
    if n1 == 0 or n0 == 0:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    u = sum(r for r, b in zip(ranks, positive, strict=True) if b) - n1 * (n1 + 1) / 2
    return u / (n1 * n0)


def _bins(p: Sequence[float], hit: Sequence[bool]) -> list[CalibrationBin]:
    """Ten equal-width bins over ``[0, 1]`` (empty bins left out): the mean predicted
    probability against the observed share."""
    acc: list[list[float]] = [[0.0, 0.0, 0.0] for _ in range(CALIBRATION_BINS)]
    for x, h in zip(p, hit, strict=True):
        b = min(int(x * CALIBRATION_BINS), CALIBRATION_BINS - 1)
        acc[b][0] += x
        acc[b][1] += 1.0 if h else 0.0
        acc[b][2] += 1
    return [
        CalibrationBin(
            p_mean=min(max(s / c, 0.0), 1.0), observed=min(max(o / c, 0.0), 1.0), n=int(c)
        )
        for s, o, c in acc
        if c
    ]


def _labels_of(y: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for i, label in enumerate(y):
        _check_label(label, f"label {i}")
        if label not in seen:
            seen.add(label)
            out.append(label)
            if len(out) > MAX_CLASSES:
                raise ValueError(f"more than {MAX_CLASSES} different labels")
    return out


def classification_metrics_of(
    p: Sequence[float], y: Sequence[str], positive: str, threshold: float
) -> ClassAccuracy:
    """How right a binary classifier was. ``p``: the predicted probability of ``positive`` per
    case; ``y``: the observed label per case (a label that matches ``positive`` by
    :func:`match_label` is a positive, any other label a negative); ``threshold``: the
    probability from which the model says ``positive`` (0.5 gives the SDK's ``accuracy``)."""
    if len(p) != len(y):
        raise ValueError("p and y must have the same length")
    _check_rows(len(p))
    _check_label(positive, "the positive label")
    t = _finite(threshold, "threshold")
    if not 0 < t < 1:
        raise ValueError("threshold must be between 0 and 1")
    probs = [_probability(x, f"probability {i}") for i, x in enumerate(p)]
    labels = _labels_of(y)
    pos = [match_label(label, [positive]) == 0 for label in y]
    n = len(probs)
    if not n:
        return ClassAccuracy(n=0, positive=positive, threshold=t)
    said = [x >= t for x in probs]
    p_obs = [x if b else 1 - x for x, b in zip(probs, pos, strict=True)]
    negatives = [lab for lab in labels if match_label(lab, [positive]) != 0]
    negative = negatives[0] if len(negatives) == 1 else "other"
    negative = negative if negative != positive else "other"
    tn = sum(1 for s, b in zip(said, pos, strict=True) if not s and not b)
    fp = sum(1 for s, b in zip(said, pos, strict=True) if s and not b)
    fn = sum(1 for s, b in zip(said, pos, strict=True) if not s and b)
    tp = sum(1 for s, b in zip(said, pos, strict=True) if s and b)
    return ClassAccuracy(
        n=n,
        positive=positive,
        threshold=t,
        accuracy=(tp + tn) / n,
        brier=sum((x - (1.0 if b else 0.0)) ** 2 for x, b in zip(probs, pos, strict=True)) / n,
        log_score=sum(math.log(max(q, LOG_FLOOR)) for q in p_obs) / n,
        auc=auc_of(probs, pos),
        rate_pred=sum(said) / n,
        rate_obs=sum(pos) / n,
        bins=_bins(probs, pos),
        confusion=ConfusionMatrix(labels=[negative, positive], counts=[[tn, fp], [fn, tp]]),
    )


def _argmax(row: Sequence[float]) -> int:
    """The index of the largest value (the first one on a tie, as ``numpy.argmax``)."""
    best = 0
    for j in range(1, len(row)):
        if row[j] > row[best]:
            best = j
    return best


def multiclass_metrics_of(probs: Sequence[Mapping[str, float]], y: Sequence[str]) -> ClassAccuracy:
    """How right a classifier of several classes was. ``probs``: the predicted probability of
    each class per case (the classes are the keys, in first-seen order; a missing key is 0);
    ``y``: the observed label per case, matched to the classes by :func:`match_label`.

    As ``mapi_sdk.metrics`` (categorical targets): a case whose probabilities are all 0 or
    whose label is empty does not count; the probabilities are scaled to sum to 1; a label
    that matches no class is a miss with probability 0. ``bins`` hold the probability of the
    most likely class against how often it was right."""
    if len(probs) != len(y):
        raise ValueError("probs and y must have the same length")
    _check_rows(len(probs))
    classes: list[str] = []
    for i, row in enumerate(probs):
        for c in row:
            _check_label(c, f"class of case {i}")
            if c not in classes:
                classes.append(c)
                if len(classes) > MAX_CLASSES:
                    raise ValueError(f"more than {MAX_CLASSES} classes")
    _labels_of(y)
    k = len(classes)
    rows: list[list[float]] = []
    codes: list[int] = []
    for i, (row, label) in enumerate(zip(probs, y, strict=True)):
        vec = [_probability(row.get(c, 0.0), f"probability of {c!r} in case {i}") for c in classes]
        total = sum(vec)
        if total <= 0 or label == "":
            continue
        rows.append([v / total for v in vec])
        codes.append(match_label(label, classes))
    n = len(rows)
    if not n:
        return ClassAccuracy(n=0)
    top = [_argmax(r) for r in rows]
    right = [t == c for t, c in zip(top, codes, strict=True)]
    p_obs = [r[c] if c >= 0 else 0.0 for r, c in zip(rows, codes, strict=True)]
    brier = sum(
        sum((r[j] - (1.0 if j == c else 0.0)) ** 2 for j in range(k))
        for r, c in zip(rows, codes, strict=True)
    )
    aucs = [
        a
        for j in range(k)
        if (a := auc_of([r[j] for r in rows], [c == j for c in codes])) is not None
    ]
    confusion: ConfusionMatrix | None = None
    if k <= MAX_CONFUSION_LABELS:
        counts = [[0] * k for _ in range(k)]
        for t, code in zip(top, codes, strict=True):
            if code >= 0:
                counts[code][t] += 1
        confusion = ConfusionMatrix(labels=classes, counts=counts)
    return ClassAccuracy(
        n=n,
        accuracy=sum(right) / n,
        brier=brier / n,
        log_score=sum(math.log(max(q, LOG_FLOOR)) for q in p_obs) / n,
        auc=sum(aucs) / len(aucs) if len(aucs) >= 2 else None,
        bins=_bins([r[t] for r, t in zip(rows, top, strict=True)], right),
        confusion=confusion,
    )


# ===================================================================================== effect

_LOW_KEYS: Final = ("0.05", "0.050", "0.1")
_HIGH_KEYS: Final = ("0.95", "0.950", "0.9")


def _bound(quantiles: Mapping[str, float], low: bool) -> float | None:
    for key in _LOW_KEYS if low else _HIGH_KEYS:
        if key in quantiles:
            return quantiles[key]
    levels = sorted((float(k), v) for k, v in quantiles.items())
    side = [x for x in levels if (x[0] < 0.5 if low else x[0] > 0.5)]
    if not side:
        return None
    return side[0][1] if low else side[-1][1]


def effect_of(
    actual_total: float,
    cf_total: DistributionSummary,
    *,
    n_periods: int = 0,
    note: str | None = None,
) -> EffectAccuracy:
    """The updated effect of a causal model over the new periods: ``actual_total`` (Σ of
    the actuals) − the counterfactual total (the forecast without the intervention). The
    range is ``[Σa − cf_high, Σa − cf_low]`` from the counterfactual's 5 % and 95 % quantiles
    (else its outermost quantiles; no range without quantiles); ``relative`` is the effect
    over the counterfactual mean, in percent (``None`` when that mean is 0)."""
    a = _finite(actual_total, "actual_total")
    mean = _finite(cf_total.mean, "the counterfactual mean")
    lo_cf, hi_cf = _bound(cf_total.quantiles, True), _bound(cf_total.quantiles, False)
    total = a - mean
    return EffectAccuracy(
        n_periods=n_periods,
        total=_clean(total),
        low=_clean(a - hi_cf) if hi_cf is not None else None,
        high=_clean(a - lo_cf) if lo_cf is not None else None,
        relative=_clean(total / mean * 100) if mean else None,
        note=note,
    )
