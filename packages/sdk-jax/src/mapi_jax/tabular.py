"""Tables as fixed-size batches for ``dl.mlp``.

Rows are the cells of the target's dims (stacked in the target's dim order). Feature
variables are broadcast over the row dims; dims beyond the row dims are flattened into
columns (``price[store=a]``), at most :data:`MAX_COLUMNS`. Columns are standardised with the
training mean and sd (:class:`Standardizer`); missing feature values become the training mean
(0 after standardising).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "MAX_COLUMNS",
    "Standardizer",
    "TabularBatch",
    "TabularLoader",
    "design_matrix",
    "feature_vars",
]

MAX_COLUMNS = 4096
_MIN_SD = 1e-8


@dataclass(frozen=True, slots=True)
class TabularBatch:
    x: np.ndarray  # (B, F) float32
    y: np.ndarray | None  # (B,) float32 (regression) or int32 codes (classification)
    index: np.ndarray  # (B,) int64 positions in the loader


def feature_vars(ds: xr.Dataset, target: str, features: Sequence[str] | None) -> list[str]:
    """The feature variables: ``features`` when given, else every numeric data variable
    other than the target that shares at least one of its dims."""
    if features is not None:
        missing = [f for f in features if f not in ds.data_vars]
        if missing:
            raise MapiError(
                f"the data has no feature variable(s) {missing[:10]}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/model/settings/features",
            )
        return list(features)
    rows = set(ds[target].dims)
    out = [
        str(v)
        for v in ds.data_vars
        if v != target and ds[v].dtype.kind in "biuf" and rows & set(ds[v].dims)
    ]
    if not out:
        raise MapiError(
            f"the data has no numeric feature variables next to the target {target!r}",
            code=ErrorCode.SCHEMA_INVALID,
        )
    return sorted(out)


def design_matrix(
    ds: xr.Dataset, rows: Sequence[str], features: Sequence[str]
) -> tuple[np.ndarray, list[str]]:
    """``(n_rows, F)`` float64 features and their column names (row order = C order of
    ``rows``)."""
    like = xr.DataArray(
        np.zeros([ds.sizes[d] for d in rows]),
        dims=list(rows),
        coords={d: ds[d].values for d in rows if d in ds.coords},
    )
    cols: list[np.ndarray] = []
    names: list[str] = []
    for f in features:
        da = ds[f]
        if da.dtype.kind not in "biuf":
            raise MapiError(
                f"feature {f!r} must hold numbers, not {da.dtype}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{f}",
            )
        da, _ = xr.broadcast(da.astype(float), like)
        extra = [str(d) for d in da.dims if d not in rows]
        da = da.transpose(*rows, *extra)
        n = int(np.prod([ds.sizes[d] for d in rows]))
        values = np.asarray(da.values, dtype=float).reshape(n, -1)
        if extra:
            labels = _extra_labels(da, extra)
            names.extend(f"{f}[{lab}]" for lab in labels)
        else:
            names.append(f)
        cols.append(values)
        if sum(c.shape[1] for c in cols) > MAX_COLUMNS:
            raise MapiError(
                f"the features give more than {MAX_COLUMNS} columns",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/model/settings/features",
            )
    return np.concatenate(cols, axis=1), names


def _extra_labels(da: xr.DataArray, extra: Sequence[str]) -> list[str]:
    grids = np.meshgrid(
        *[np.asarray(da[d].values) if d in da.coords else np.arange(da.sizes[d]) for d in extra],
        indexing="ij",
    )
    flat = [g.reshape(-1) for g in grids]
    return [",".join(f"{d}={flat[k][i]}" for k, d in enumerate(extra)) for i in range(flat[0].size)]


@dataclass(frozen=True, slots=True)
class Standardizer:
    mean: np.ndarray
    std: np.ndarray

    @classmethod
    def fit(cls, x: np.ndarray) -> Standardizer:
        with np.errstate(invalid="ignore"):
            mean = np.nanmean(x, axis=0) if x.size else np.zeros(x.shape[1])
            std = np.nanstd(x, axis=0) if x.size else np.ones(x.shape[1])
        mean = np.where(np.isfinite(mean), mean, 0.0)
        std = np.where(np.isfinite(std) & (std > _MIN_SD), std, 1.0)
        return cls(mean, std)

    def apply(self, x: np.ndarray) -> np.ndarray:
        z = (x - self.mean) / self.std
        return np.where(np.isfinite(z), z, 0.0).astype(np.float32)

    def to_json(self) -> dict[str, Any]:
        return {"mean": [float(v) for v in self.mean], "std": [float(v) for v in self.std]}

    @classmethod
    def from_json(cls, doc: dict[str, Any]) -> Standardizer:
        return cls(np.asarray(doc["mean"], dtype=float), np.asarray(doc["std"], dtype=float))


class TabularLoader:
    """Standardised rows ``x`` (and targets ``y``) in fixed-size batches.

    ``strata`` (classification codes) drives the stratified :meth:`split`.
    """

    def __init__(
        self, x: np.ndarray, y: np.ndarray | None = None, *, strata: np.ndarray | None = None
    ) -> None:
        self.x = np.asarray(x, dtype=np.float32)
        self.y = None if y is None else np.asarray(y)
        self.strata = None if strata is None else np.asarray(strata)
        if self.y is not None and self.y.shape[0] != self.x.shape[0]:
            raise ValueError("x and y must have the same number of rows")

    @property
    def n(self) -> int:
        return int(self.x.shape[0])

    @property
    def n_features(self) -> int:
        return int(self.x.shape[1])

    def subset(self, which: np.ndarray | Sequence[int]) -> TabularLoader:
        idx = np.asarray(which)
        if idx.dtype == bool:
            idx = np.flatnonzero(idx)
        return TabularLoader(
            self.x[idx],
            None if self.y is None else self.y[idx],
            strata=None if self.strata is None else self.strata[idx],
        )

    def split(
        self, fraction: float, *, seed: int, stratify: bool = True
    ) -> tuple[TabularLoader, TabularLoader]:
        """``(train, val)`` with about ``fraction`` of the rows (of each stratum) in val."""
        rng = np.random.default_rng(seed)
        groups = (
            self.strata
            if (stratify and self.strata is not None)
            else np.zeros(self.n, dtype=np.int64)
        )
        val = np.zeros(self.n, dtype=bool)
        for g in np.unique(groups):
            pos = np.flatnonzero(groups == g)
            k = round(fraction * pos.size)
            k = min(max(k, 1), pos.size - 1) if pos.size >= 2 else 0
            val[rng.permutation(pos)[:k]] = True
        return self.subset(~val), self.subset(val)

    def batches(
        self,
        batch_size: int,
        *,
        epoch: int = 0,
        shuffle: bool = False,
        seed: int = 0,
        augment: Any = None,
        drop_last: bool = False,
        shard: tuple[int, int] = (0, 1),
        prefetch: int = 2,
    ) -> Iterator[TabularBatch]:
        order = (
            np.random.default_rng([seed, epoch]).permutation(self.n)
            if shuffle
            else np.arange(self.n)
        )
        i, k = shard
        order = order[i::k]
        stop = (order.size // batch_size) * batch_size if drop_last else order.size
        for a in range(0, stop, batch_size):
            idx = order[a : a + batch_size]
            yield TabularBatch(
                self.x[idx], None if self.y is None else self.y[idx], idx.astype(np.int64)
            )
