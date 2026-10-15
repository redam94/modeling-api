"""Per-feature contributions to a prediction.

``exact`` (linear estimators)
    ``intercept`` plus ``coef_j * x_j`` for every variable column; one-hot/code columns of a
    coordinate feature are summed into one component named after the dim. The components
    add up to the prediction exactly (in model space).
``zeroing`` (any estimator)
    Counterfactual zeroing: ``baseline`` is the prediction with every feature at its zero
    value (0, no level, missing category); each feature group's contribution is the drop in
    the prediction when only that group is zeroed; ``interaction`` is the remainder, so the
    components still add up to the prediction. These are *not* Shapley values: with
    interactions the split depends on the zero reference, which is why the remainder is
    reported separately and the output is labelled.

SHAP values are out of scope for v1.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
from mapi_spec.errors import ErrorCode, MapiError

from mapi_sklearn.design import Layout

__all__ = ["MAX_ZEROING_GROUPS", "exact_contributions", "zero_values", "zeroing_contributions"]

MAX_ZEROING_GROUPS = 128
PredictFn = Callable[[np.ndarray], np.ndarray]


def exact_contributions(est: Any, X: np.ndarray, layout: Layout) -> tuple[list[str], np.ndarray]:
    """``(names, values[component, rows])`` for a fitted linear estimator."""
    coef = np.asarray(est.coef_, dtype=float).reshape(-1)
    if coef.size != X.shape[1]:
        raise MapiError(
            "exact contributions need one coefficient per column (binary/single-output models)",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    intercept = float(np.asarray(getattr(est, "intercept_", 0.0), dtype=float).reshape(-1)[0])
    names = ["intercept"]
    parts = [np.full(X.shape[0], intercept)]
    for name, cols in layout.groups():
        names.append(name)
        parts.append(X[:, cols] @ coef[cols])
    return names, np.stack(parts)


def zero_values(layout: Layout, *, native_nan: bool) -> np.ndarray:
    """The "zero" value of every column (missing for natively categorical codes)."""
    zero = np.zeros(len(layout.columns))
    for j in layout.categorical:
        zero[j] = np.nan if native_nan else 0.0
    return zero


def zeroing_contributions(
    predict: PredictFn, X: np.ndarray, layout: Layout, *, native_nan: bool
) -> tuple[list[str], np.ndarray]:
    """``(names, values[component, rows])`` by counterfactual zeroing (see module doc)."""
    groups = layout.groups()
    if len(groups) > MAX_ZEROING_GROUPS:
        raise MapiError(
            f"zeroing contributions are limited to {MAX_ZEROING_GROUPS} feature groups; "
            f"this model has {len(groups)}",
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/settings/contributions",
        )
    zero = zero_values(layout, native_nan=native_nan)
    full = predict(X)
    base_x = np.broadcast_to(zero, X.shape).copy()
    baseline = predict(base_x)
    names = ["baseline"]
    parts = [baseline]
    for name, cols in groups:
        xz = X.copy()
        xz[:, cols] = zero[cols]
        names.append(name)
        parts.append(full - predict(xz))
    names.append("interaction")
    parts.append(full - baseline - np.sum(parts[1:], axis=0))
    return names, np.stack(parts)
