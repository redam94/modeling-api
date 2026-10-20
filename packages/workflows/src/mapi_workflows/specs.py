"""The ``ModelSpec`` a model node fits (pure JSON; shared by the runtime and the control plane).

A ``model.fit`` / ``model.backtest`` / ``search.grid`` node names a catalog model and its
settings; :func:`model_spec_document` turns its parameters into a full spec over a dataset
reference, with the replayable steps of upstream transforms first in the pipeline. The
in-process runtime fits it with the SDK harness; the control plane stores it as the spec
revision of a ``fit`` child run (``wf-<workflow>-<node>``), so the fitted model version is
an ordinary, scoreable version of the model of that name.

For a tree dataset the node's optional ``nodes`` and ``join`` parameters become
``data.nodes`` and ``data.join`` (the nodes merged into the flat pipeline input); without
them the spec reads the root node.

A non-empty ``calibration`` parameter becomes the spec's ``calibration`` block (business
priors and lift tests, e.g. ``[nodes.lift.lift_test]`` from a ``model.lift_test`` node).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import JsonValue

__all__ = ["MODEL_LABEL", "model_spec_document", "node_model_name"]

MODEL_LABEL: Final = "workflow-node"
_SLUG = re.compile(r"[^a-z0-9_-]+")


def node_model_name(workflow: str, node_id: str) -> str:
    """The spec (and model) name of a workflow node's fits: ``wf-<workflow>-<node>``."""
    text = _SLUG.sub("-", f"wf-{workflow}-{node_id}".lower()).strip("-_")
    return text[:120].rstrip("-_") or "wf-model"


def model_spec_document(
    params: Mapping[str, JsonValue],
    *,
    name: str,
    project: str,
    dataset: str,
    pipeline: Sequence[Mapping[str, Any]] = (),
    split: Mapping[str, Any] | None = None,
    settings: Mapping[str, Any] | None = None,
    bind: Mapping[str, Any] | None = None,
    mode: str | None = None,
    labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The spec document of a model node (validated by the caller)."""
    model = params.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("the node needs a catalog model ('model')")
    raw_fit = params.get("fit")
    fit: dict[str, Any] = dict(raw_fit) if isinstance(raw_fit, dict) else {}
    if mode is not None:
        fit["mode"] = mode
    raw_settings = params.get("settings")
    use_settings = (
        dict(settings)
        if settings is not None
        else dict(raw_settings)
        if isinstance(raw_settings, dict)
        else {}
    )
    raw_pipeline = params.get("pipeline")
    steps = [dict(s) for s in pipeline]
    if isinstance(raw_pipeline, list):
        steps += [dict(s) for s in raw_pipeline if isinstance(s, dict)]
    raw_bind = params.get("bind")
    use_bind = (
        dict(bind) if bind is not None else dict(raw_bind) if isinstance(raw_bind, dict) else {}
    )
    doc: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "ModelSpec",
        "metadata": {"name": name, "project": project, "labels": dict(labels or {})},
        "data": {"dataset": dataset, "bind": use_bind},
        "pipeline": steps,
        "model": {
            "use": model if "@" in model else f"{model}@*",
            "settings": use_settings,
            "fit": fit,
        },
    }
    if split:
        doc["data"]["split"] = dict(split)
    raw_nodes = params.get("nodes")
    raw_join = params.get("join")
    join = raw_join if isinstance(raw_join, str) and raw_join else "exact"
    if isinstance(raw_nodes, dict) and raw_nodes:
        doc["data"]["nodes"] = {
            str(k): dict(v) if isinstance(v, dict) else v for k, v in raw_nodes.items()
        }
        if join != "exact":
            doc["data"]["join"] = join
    elif join != "exact":
        raise ValueError("the node's 'join' is only used with 'nodes' (a tree dataset)")
    ev = params.get("evaluate")
    if isinstance(ev, dict):
        doc["evaluate"] = dict(ev)
    cal = params.get("calibration")
    if isinstance(cal, dict) and cal:
        # business priors and lift tests (for example the lift_test of a model.lift_test node)
        doc["calibration"] = dict(cal)
    return doc
