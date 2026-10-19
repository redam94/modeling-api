"""``model.score``: predictions, simulations, forecasts and scenarios of a fitted model.

The node runs the score path of a score run (:func:`mapi_sdk.harness.score_bundle`, full
representation) and outputs one group of the result as a dataset: ``predictions`` (draws,
quantiles, ...) or ``summary``, or the ``group`` the workflow names (for
example ``delta/predictions`` of a counterfactual). The root attributes of the result
(representation, mode, kind) are kept on the dataset, so a ``plot.render`` node reads it as
a score output. By default the dataset is the draws for a model that gives draws, else the
``summary`` (statistics along a ``stat`` dim), which the prediction plots read.
"""

from __future__ import annotations

from typing import Any, Final

import xarray as xr
from mapi_sdk.harness import score_bundle
from mapi_spec.errors import ErrorCode, MapiError
from mapi_workflows.runtime.models import _future_rows, _registry, _request
from mapi_workflows.runtime.values import (
    DatasetValue,
    ModelValue,
    NodeContext,
    NodeOutcome,
    score_payload,
    score_rows,
    scoring_tree,
)
from pydantic import JsonValue

__all__ = ["SCORE_NODES", "score_node"]

SCORE_MODES: Final = ("predict", "simulate", "forecast", "counterfactual")
_FIXED: Final = frozenset({"apiVersion", "kind", "model", "mode", "data", "scenario"})
_ATTRS: Final = ("representation", "mode", "kind", "units", "names", "bundle_id")


def _time_dim(mv: ModelValue) -> str:
    """The model's canonical time dim (forecast horizons run along it)."""
    schema = mv.bundle.model_schema
    return next((d for d, s in (schema.dims.items() if schema else []) if s.kind == "time"), "time")


def _group(tree: xr.DataTree, wanted: str | None) -> tuple[str, xr.Dataset]:
    """The group to output: ``wanted``, else the draws (``predictions`` of a draws model),
    else the ``summary`` (a ``stat`` dim plots read), else the full predictions."""
    draws = str(tree.attrs.get("representation", "")) == "draws"
    names = (
        [wanted]
        if wanted
        else (["predictions", "summary"] if draws else ["summary", "predictions"])
    )
    for name in names:
        try:
            node = tree[name]
        except KeyError:
            continue
        if isinstance(node, xr.DataTree) and node.has_data:
            return name, node.to_dataset(inherit=False)
    have = sorted(n.path.lstrip("/") for n in tree.subtree if n.has_data and n.path != "/")
    raise MapiError(
        f"the score result has no group {names[0]!r} (it has: {', '.join(have) or 'none'})",
        code=ErrorCode.SCHEMA_INVALID,
    )


def score_node(ctx: NodeContext) -> NodeOutcome:
    mv = ctx.model("model")
    assert mv is not None
    dv = ctx.dataset("data", required=False)
    mode = str(ctx.params.get("mode") or "predict")
    if mode not in SCORE_MODES:
        raise MapiError(f"unknown score mode {mode!r}", code=ErrorCode.WORKFLOW_INVALID)
    modes = mv.bundle.model.manifest.capabilities.score_modes
    if mode not in modes:
        raise MapiError(
            f"the model cannot {mode} (its score modes: {', '.join(modes)})",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    raw = ctx.params.get("request")
    extra: dict[str, Any] = (
        {k: v for k, v in raw.items() if k not in _FIXED} if isinstance(raw, dict) else {}
    )
    bundle = mv.bundle
    reg = _registry(ctx)
    history = None
    payload: xr.Dataset | xr.DataTree | None
    if mode == "forecast":
        tdim = _time_dim(mv)
        extra.setdefault("horizon", {"dim": tdim, "periods": 12})
        payload = _future_rows(bundle, score_rows(dv, bundle), tdim) if dv is not None else None
        history = mv.data
    else:
        payload = score_payload(dv) if dv is not None else mv.data
    try:
        req = _request(bundle.spec, mode, **extra)
    except ValueError as exc:
        raise MapiError(
            "the 'request' of the score node is not a valid score request. Correct it.",
            code=ErrorCode.WORKFLOW_INVALID,
            untrusted_text=str(exc)[:4000],
        ) from None
    ctx.info(f"scoring ({mode})")
    tree = score_bundle(
        bundle,
        req,
        data=payload,
        registry=reg,
        history=history,
        return_full=True,
        tree=scoring_tree(mv, dv),
    )
    wanted = ctx.params.get("group")
    name, ds = _group(tree, wanted if isinstance(wanted, str) and wanted else None)
    attrs = {k: tree.attrs[k] for k in _ATTRS if isinstance(tree.attrs.get(k), str | int | float)}
    ds = ds.assign_attrs({**{k: v for k, v in ds.attrs.items() if k != "mapi_schema"}, **attrs})
    result: dict[str, JsonValue] = {
        "mode": mode,
        "n_vars": len(ds.data_vars),
        "group": name,
        "representation": str(tree.attrs.get("representation", "point")),
        "vars": [str(v) for v in ds.data_vars],
        "model": mv.ref,
    }
    return NodeOutcome(result=result, outputs={"scores": DatasetValue(data=ds)})


SCORE_NODES: Final = {"model.score": score_node}
