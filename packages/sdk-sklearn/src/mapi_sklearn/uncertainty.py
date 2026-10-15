"""Uncertainty building blocks: quantile models, bootstrap resampling, split conformal.

These are pure functions over numpy arrays so they can be tested in isolation; the model
classes in :mod:`mapi_sklearn.model` wire them to estimators and the bundle.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from mapi_spec.errors import ErrorCode, MapiError

from mapi_sklearn.estimators import ESTIMATORS, AnyChoice, EstimatorEntry

__all__ = [
    "bootstrap_samples",
    "calibration_mask",
    "conformal_quantile",
    "managed",
    "quantile_overrides",
    "rearrange",
]


def managed(
    entry: EstimatorEntry, choice: AnyChoice, param: str, value: Any, why: str
) -> dict[str, Any]:
    """Override ``param`` with ``value`` unless the user set it to something else."""
    current = getattr(choice.params, param)
    default = entry.params.model_fields[param].default
    if current != default and current != value:
        raise MapiError(
            f"estimator params.{param} is managed by {why}; remove it or set it to {value!r}",
            code=ErrorCode.SETTINGS_INVALID,
            pointer=f"/model/settings/estimator/params/{param}",
        )
    return {param: value}


def quantile_overrides(
    entry: EstimatorEntry, choice: AnyChoice, quantiles: Sequence[float]
) -> list[dict[str, Any]]:
    """Platform-owned parameters for each quantile model.

    One element for a native multi-quantile estimator (xgboost), one per level otherwise.
    """
    why = "uncertainty kind 'quantile'"
    if entry.quantile is None:
        capable = sorted(k for k, e in ESTIMATORS.items() if e.quantile is not None)
        raise MapiError(
            f"estimator {entry.kind!r} cannot fit quantile models; use one of "
            + ", ".join(capable),
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/settings/estimator/kind",
        )
    base: dict[str, Any] = {}
    if entry.loss_param is not None and entry.quantile_loss is not None:
        base.update(managed(entry, choice, entry.loss_param, entry.quantile_loss, why))
    if entry.quantile == "native_multi":
        if getattr(choice.params, "quantile_alpha", None) is not None:
            raise MapiError(
                "estimator params.quantile_alpha is managed by uncertainty.quantiles",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/estimator/params/quantile_alpha",
            )
        return [{**base, "quantile_alpha": np.asarray(quantiles, dtype=float)}]
    assert entry.quantile_setter is not None
    return [entry.quantile_setter(dict(base), float(q)) for q in quantiles]


def rearrange(q: np.ndarray, axis: int = 0) -> tuple[np.ndarray, int]:
    """Sort quantile predictions along ``axis`` (monotone rearrangement).

    Returns the sorted array and the number of cells whose quantiles crossed.
    """
    srt = np.sort(q, axis=axis)
    crossed = ~np.all(np.isclose(srt, q, equal_nan=True), axis=axis)
    return srt, int(np.count_nonzero(crossed))


def conformal_quantile(scores: np.ndarray, alpha: float) -> float:
    """The split-conformal radius: the ``ceil((n+1)(1-alpha))``-th smallest score.

    With exchangeable calibration and test points, ``|y - point| <= radius`` holds with
    probability at least ``1 - alpha``. Raises ``SETTINGS_INVALID`` when the calibration set
    is too small for ``alpha`` (the radius would be infinite).
    """
    s = np.sort(np.asarray(scores, dtype=float)[np.isfinite(scores)])
    n = s.size
    k = math.ceil((n + 1) * (1 - alpha))
    if n == 0 or k > n:
        need = math.ceil(1 / alpha) - 1
        raise MapiError(
            f"the conformal calibration set has {n} rows; alpha={alpha} needs at least "
            f"{need}: increase uncertainty.calibration or alpha, or add data",
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/settings/uncertainty",
        )
    return float(s[k - 1])


def calibration_mask(
    n: int,
    fraction: float,
    *,
    by: str,
    seed: int,
    time_labels: np.ndarray | None = None,
) -> np.ndarray:
    """Which of ``n`` training rows form the conformal calibration set.

    ``by="last"`` takes every row whose time label is among the most recent
    ``ceil(fraction * n_periods)`` periods (so calibration mimics forecasting);
    ``"random"`` a seeded random ``fraction`` of rows. Both leave at least one row to fit.
    """
    if n < 2:
        raise MapiError(
            "conformal intervals need at least two training rows", code=ErrorCode.SCHEMA_INVALID
        )
    if by == "last":
        assert time_labels is not None
        periods = np.unique(time_labels)
        if periods.size < 2:
            raise MapiError(
                "calibration_by 'last' needs at least two time periods",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/uncertainty/calibration_by",
            )
        k = min(max(1, math.ceil(fraction * periods.size)), periods.size - 1)
        cut = periods[-k]
        return np.asarray(time_labels >= cut, dtype=bool)
    m = min(max(1, round(fraction * n)), n - 1)
    rng = np.random.default_rng(seed)
    mask = np.zeros(n, dtype=bool)
    mask[rng.choice(n, size=m, replace=False)] = True
    return mask


def bootstrap_samples(
    n: int, replicates: int, *, seed: int, groups: np.ndarray | None = None
) -> list[np.ndarray]:
    """Row indices of each bootstrap replicate (with replacement).

    With ``groups`` (one label per row) whole groups are resampled (cluster bootstrap), so
    within-group dependence is kept.
    """
    rng = np.random.default_rng(seed)
    if groups is None:
        return [rng.integers(0, n, size=n) for _ in range(replicates)]
    labels, inverse = np.unique(groups, return_inverse=True)
    members = [np.flatnonzero(inverse == g) for g in range(labels.size)]
    out = []
    for _ in range(replicates):
        picked = rng.integers(0, labels.size, size=labels.size)
        out.append(np.concatenate([members[g] for g in picked]))
    return out
