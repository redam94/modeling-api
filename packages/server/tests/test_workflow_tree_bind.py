"""A ``model.fit`` node over a tree dataset completes its binding from the merged nodes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from mapi_sdk.testing.toy import tree_dataset
from mapi_sdk.tree import infer_tree_schema
from mapi_server.services.workflows.orchestrate import _bind_schema

NODES = {"sales": {"path": "/sales"}, "media": {"path": "/media"}}


def _dv(**kw: Any) -> Any:
    return SimpleNamespace(**{"data_schema": {"dims": {}, "vars": {}}, "tree_schema": None, **kw})


def test_bind_schema_is_the_merged_nodes_schema() -> None:
    tree_schema = infer_tree_schema(tree_dataset()).model_dump(mode="json")
    dv = _dv(tree_schema=tree_schema)
    merged = _bind_schema(dv, {"nodes": NODES})
    assert merged is not None and set(merged["vars"]) == {"revenue", "spend"}
    assert _bind_schema(dv, {}) == dv.data_schema  # no nodes: the root's schema
    assert _bind_schema(dv, {"nodes": {"x": {"path": "/nope"}}}) is None
    plain = _dv()
    assert _bind_schema(plain, {"nodes": NODES}) == plain.data_schema
