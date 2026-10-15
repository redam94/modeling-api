"""Levels of coordinate features that were not seen at fit.

Each dim's new-level strategy (from the model's input schema, i.e. the effective dim
policy) decides what a row with an unseen label becomes:

``pooled`` (and ``hyperprior``, which has no meaning for these estimators)
    The prediction is the training-frequency-weighted average of the predictions the row
    would get under each fitted level (the ``pooled_max_levels`` most frequent ones). For
    linear models with one-hot features this equals predicting with the average encoding;
    for quantiles and interval bounds it is quantile averaging (Vincentisation); for
    bootstrap draws the average is taken per draw, so joint draws stay joint.
``zero``
    The feature carries no level: all one-hot columns are 0 and category codes are missing
    (estimators with native missing-value support only).
``error``
    ``UNSEEN_LEVELS``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
from mapi_spec.errors import ErrorCode, MapiError

from mapi_sklearn.design import Design, Layout

__all__ = ["MAX_EXPANDED_CELLS", "Expansion", "expand_new_levels"]

MAX_EXPANDED_CELLS = 200_000_000


@dataclass(slots=True)
class Expansion:
    """Rows to predict (``X``) and how to fold their predictions back onto design rows."""

    X: np.ndarray
    weights: sp.csr_matrix | None = None
    pooled: tuple[str, ...] = ()

    def reduce(self, values: np.ndarray) -> np.ndarray:
        """Fold ``values[..., M]`` (one per expanded row) into ``[..., n]`` design rows."""
        if self.weights is None:
            return values
        lead = values.shape[:-1]
        flat = values.reshape(-1, values.shape[-1])
        out = np.asarray(self.weights @ flat.T).T
        return out.reshape(*lead, self.weights.shape[0])


def _top_levels(counts: list[int], k: int) -> tuple[np.ndarray, np.ndarray]:
    c = np.asarray(counts, dtype=float)
    order = np.argsort(-c, kind="stable")[: min(k, c.size)]
    order = order[c[order] > 0]
    if order.size == 0:  # pragma: no cover - every fitted level has rows
        raise MapiError("no fitted levels to pool over", code=ErrorCode.UNSEEN_LEVELS)
    p = c[order] / c[order].sum()
    return order, p


def expand_new_levels(
    design: Design,
    layout: Layout,
    strategy_for: Callable[[str], str],
    *,
    native_nan: bool,
    max_levels: int,
    log: Callable[[str], None] | None = None,
) -> Expansion:
    """Apply each dim's new-level strategy to the rows flagged in ``design.new_rows``."""
    pooled: list[str] = []
    for dim in sorted(design.new_rows):
        rows = design.new_rows[dim]
        if not rows.any():
            continue
        strategy = strategy_for(dim)
        enc = layout.encodings[dim]
        if strategy == "error":
            raise MapiError(
                f"{int(rows.sum())} row(s) have levels of {dim!r} that were not seen at fit and "
                "its new-level strategy is 'error'",
                code=ErrorCode.UNSEEN_LEVELS,
            )
        if strategy == "zero":
            if enc.encoding == "ordinal" and not native_nan:
                raise MapiError(
                    f"new levels of {dim!r} cannot be scored with strategy 'zero': its ordinal "
                    "code would be missing and this estimator does not accept missing values; "
                    "use the 'pooled' strategy or a one-hot encoding",
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                )
            continue
        if strategy == "hyperprior" and log is not None:
            log(f"{dim!r}: 'hyperprior' has no meaning for this estimator; pooling instead")
        pooled.append(dim)
    if not pooled:
        return Expansion(design.X)

    n = design.n
    orig = np.arange(n)
    weight = np.ones(n)
    assign: dict[str, np.ndarray] = {}
    for dim in pooled:
        flags = design.new_rows[dim][orig]
        levels, probs = _top_levels(layout.encodings[dim].counts, max_levels)
        k = levels.size
        stay = np.flatnonzero(~flags)
        grow = np.flatnonzero(flags)
        sel = np.concatenate([stay, np.repeat(grow, k)])
        ks = np.concatenate([np.full(stay.size, -1), np.tile(np.arange(k), grow.size)])
        if sel.size * max(design.X.shape[1], 1) > MAX_EXPANDED_CELLS:
            raise MapiError(
                "pooling new levels needs too many predictions; lower pooled_max_levels",
                code=ErrorCode.RESOURCE_EXHAUSTED,
            )
        safe = np.where(ks >= 0, ks, 0)
        weight = weight[sel] * np.where(ks >= 0, probs[safe], 1.0)
        orig = orig[sel]
        assign = {d: a[sel] for d, a in assign.items()}
        assign[dim] = np.where(ks >= 0, levels[safe], -1)
    X = design.X[orig].copy()
    for dim, level in assign.items():
        hit = level >= 0
        if not hit.any():
            continue
        cols = layout.columns_of(dim)
        enc = layout.encodings[dim]
        if enc.encoding == "onehot":
            block = np.zeros((int(hit.sum()), len(cols)))
            block[np.arange(block.shape[0]), level[hit]] = 1.0
            X[np.ix_(np.flatnonzero(hit), np.asarray(cols, dtype=np.intp))] = block
        else:
            X[hit, cols[0]] = level[hit].astype(float)
    m = orig.size
    weights = sp.csr_matrix((weight, (orig, np.arange(m))), shape=(n, m))
    return Expansion(X, weights, tuple(pooled))
