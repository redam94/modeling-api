"""Split nodes (``split.*``): resolve a :data:`~mapi_spec.splitplan.SplitPlan` on the data.

The plan's folds are computed by :func:`mapi_spec.splitplan.plan_folds` (ordered plans) and
:func:`~mapi_spec.splitplan.plan_group_folds` (geo holdout) from the length of the time dim
and the group levels, so the same folds can be shown before anything runs. A ``holdout``
plan wraps one existing ``data.split`` (``time_holdout``, ``as_of``, ``leave_group_out``);
its fold is resolved here the way :func:`mapi_sdk.split.split_dataset` cuts it.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any, Final

import numpy as np
import pandas as pd
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.splitplan import (
    Fold,
    GeoHoldoutPlan,
    HoldoutPlan,
    plan_folds,
    plan_group_folds,
)
from mapi_workflows.payloads import SplitPlanPayload
from mapi_workflows.runtime.data import data_error, label_of, schema_of, time_dim
from mapi_workflows.runtime.values import NodeContext, NodeOutcome
from pydantic import JsonValue, TypeAdapter, ValidationError

__all__ = ["SPLIT_NODES", "holdout_fold", "resolve_plan"]

_KINDS: Final = {
    "split.holdout": "holdout",
    "split.rolling_origin": "rolling_origin",
    "split.expanding_window": "expanding_window",
    "split.blocked_kfold": "blocked_kfold",
    "split.purged_kfold": "purged_kfold",
    "split.geo_holdout": "geo_holdout",
    "split.nested": "nested",
}


def _plan_adapter() -> TypeAdapter[Any]:
    from mapi_spec.splitplan import SplitPlan

    return TypeAdapter(SplitPlan)


def holdout_fold(plan: HoldoutPlan, labels: np.ndarray, n: int, levels: list[Any]) -> Fold | None:
    """The single fold of a holdout plan (``None`` for splits that are not windows or
    groups, such as ``mask_block``: those are only used by ``model.fit``)."""
    split = plan.split
    kind = split.kind
    if kind == "time_holdout":
        if split.last is not None:  # type: ignore[union-attr]
            k = int(split.last)  # type: ignore[union-attr]
        elif split.fraction is not None:  # type: ignore[union-attr]
            k = max(1, round(n * float(split.fraction)))  # type: ignore[union-attr]
        else:
            after = split.after  # type: ignore[union-attr]
            k = n - _index_after(labels, after)
        if not 0 < k < n:
            raise data_error(f"the holdout leaves {n - k} training and {k} test periods")
        return Fold(
            index=0,
            train_start=0,
            train_stop=n - k,
            test_start=n - k,
            test_stop=n,
            horizons=list(range(1, k + 1)),
        )
    if kind == "as_of":
        cut = _index_after(labels, split.as_of)  # type: ignore[union-attr]
        gap = int(split.gap)  # type: ignore[union-attr]
        if cut <= 0 or cut + gap >= n:
            raise data_error("the as_of date leaves no training or no test periods")
        return Fold(
            index=0,
            train_start=0,
            train_stop=cut,
            test_start=cut + gap,
            test_stop=n,
            excluded=[(cut, cut + gap)] if gap else [],
            horizons=list(range(1, n - cut - gap + 1)),
        )
    if kind == "leave_group_out":
        groups = split.groups  # type: ignore[union-attr]
        if groups is None:
            rng = random.Random(split.seed or 0)  # type: ignore[union-attr]
            groups = [levels[i] for i in sorted(rng.sample(range(len(levels)), int(split.n)))]  # type: ignore[union-attr,arg-type]
        held = [label_of(g) for g in groups]
        return Fold(index=0, train_groups=[g for g in levels if g not in held], test_groups=held)
    return None


def _index_after(labels: np.ndarray, value: Any) -> int:
    """Number of labels ``<= value`` (training periods of an ``as_of``/``after`` cut)."""
    if np.issubdtype(labels.dtype, np.datetime64):
        v = np.datetime64(pd.Timestamp(str(value)).to_datetime64())
    else:
        v = value
    return int(np.searchsorted(labels, v, side="right"))


def _fold_json(f: Fold, labels: np.ndarray) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = f.model_dump(mode="json")

    def lab(i: int | None) -> JsonValue:
        if i is None or not 0 <= i < len(labels):
            return None
        return label_of(labels[i])

    if f.train_start is not None and f.train_stop is not None and f.train_stop > 0:
        out["train_labels"] = [lab(f.train_start), lab(f.train_stop - 1)]
    if f.test_start is not None and f.test_stop is not None and f.test_stop > 0:
        out["test_labels"] = [lab(f.test_start), lab(f.test_stop - 1)]
    return out


def resolve_plan(plan_doc: dict[str, Any], ctx: NodeContext) -> SplitPlanPayload:
    """Validate a plan document and compute its folds on the node's dataset."""
    try:
        plan = _plan_adapter().validate_python(plan_doc)
    except ValidationError as exc:
        raise MapiError(
            "the split plan is not valid",
            code=ErrorCode.SPLIT_INVALID,
            untrusted_text=str(exc)[:4000],
        ) from None
    dv = ctx.dataset("data")
    assert dv is not None
    ds = dv.data
    schema = schema_of(dv)
    if isinstance(plan, GeoHoldoutPlan):
        if plan.dim not in ds.dims:
            raise data_error(f"the data has no group dimension {plan.dim!r}")
        levels: list[str | int | float | bool] = (
            [label_of(v) for v in ds[plan.dim].values]
            if plan.dim in ds.coords
            else list(range(ds.sizes[plan.dim]))
        )
        n_time = ds.sizes[plan.time_dim] if plan.time_dim and plan.time_dim in ds.dims else None
        folds = plan_group_folds(plan, levels, n_time=n_time)
        tdim = plan.time_dim
        labels = np.asarray(ds[tdim].values) if tdim and tdim in ds.coords else np.asarray([])
        return SplitPlanPayload(
            plan=plan.model_dump(mode="json"),
            dim=plan.dim,
            n=len(levels),
            labels=[levels[0], levels[-1]] if levels else [],
            folds=[_fold_json(f, labels) for f in folds],
        )
    dim_name = (
        plan.split.split_dims()[0]
        if isinstance(plan, HoldoutPlan)
        else (getattr(plan, "dim", None) or getattr(getattr(plan, "outer", None), "dim", None))
    )
    if not isinstance(dim_name, str) or dim_name not in ds.dims:
        dim_name = time_dim(ds, schema)
    n = ds.sizes[dim_name]
    labels = np.asarray(ds[dim_name].values) if dim_name in ds.coords else np.arange(n)
    if isinstance(plan, HoldoutPlan):
        levels = [label_of(v) for v in labels]
        fold = holdout_fold(plan, labels, n, levels)
        folds = [fold] if fold is not None else []
    else:
        folds = plan_folds(plan, n)
    return SplitPlanPayload(
        plan=plan.model_dump(mode="json"),
        dim=dim_name,
        n=n,
        labels=[label_of(labels[0]), label_of(labels[-1])] if n else [],
        folds=[_fold_json(f, labels) for f in folds],
    )


def _split_node(kind: str) -> Callable[[NodeContext], NodeOutcome]:
    def run(ctx: NodeContext) -> NodeOutcome:
        doc = {"kind": kind, **{k: v for k, v in ctx.params.items() if v is not None}}
        payload = resolve_plan(doc, ctx)
        body = payload.model_dump(mode="json")
        result: dict[str, JsonValue] = {
            "n_folds": len(payload.folds),
            "folds": [
                {
                    k: f.get(k)
                    for k in ("index", "train_labels", "test_labels", "test_groups")
                    if f.get(k) is not None
                }
                for f in payload.folds
            ],
            "dim": payload.dim,
            "n": payload.n,
        }
        return NodeOutcome(result=result, outputs={"plan": body})

    return run


SPLIT_NODES: Final[dict[str, Callable[[NodeContext], NodeOutcome]]] = {
    impl: _split_node(kind) for impl, kind in _KINDS.items()
}
