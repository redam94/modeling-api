"""Reading tree inputs in the worker (M4): ``load_dataset`` returns a tree for a
``data.tree.zip``, ``flatten`` applies ``data.nodes``, ``load_tree_schema`` prefers the
given and stored schemas, and side tables read the node ``SideTable.node`` names."""

from __future__ import annotations

from pathlib import Path

import pytest
import xarray as xr
from mapi_runner.worker.data import (
    INPUT_TREE_SCHEMA,
    flatten,
    load_dataset,
    load_side_tables,
    load_tree_schema,
    side_table_dataset,
)
from mapi_runner.worker.tree_ingest import TREE_SCHEMA_ATTR
from mapi_sdk.testing import toy
from mapi_sdk.tree import infer_tree_schema, write_tree_zip
from mapi_sdk.wire import write_zarr_zip
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.loader import load_model_spec
from mapi_spec.spec import DataBlock
from tree_fixtures import IngestCtx, root_dataset

pytestmark = pytest.mark.unit


def _zip(tmp: Path, name: str, data: xr.Dataset | xr.DataTree) -> Path:
    path = tmp / name
    raw = write_tree_zip(data) if isinstance(data, xr.DataTree) else write_zarr_zip(data)
    path.write_bytes(raw or b"")
    return path


def test_load_dataset_returns_a_tree_or_a_dataset(tmp_path: Path) -> None:
    ctx = IngestCtx(
        tmp_path,
        {
            "dataset": _zip(tmp_path, "t.zip", toy.tree_dataset(weeks=6)),
            "plain": _zip(tmp_path, "p.zip", root_dataset()),
        },
    )
    tree = load_dataset(ctx, "dataset")  # type: ignore[arg-type]
    assert isinstance(tree, xr.DataTree) and "sales" in tree.children
    assert isinstance(load_dataset(ctx, "plain"), xr.Dataset)  # type: ignore[arg-type]


def test_flatten_selects_nodes_and_points_at_the_bad_entry() -> None:
    tree = toy.tree_dataset(weeks=6)
    block = load_model_spec(toy.tree_spec(split=False)).data
    flat = flatten(tree, block)
    assert {"revenue", "spend"} <= set(flat.data_vars)
    bad = DataBlock.model_validate(
        {"dataset": "dataset://g/t@latest", "nodes": {"x": {"path": "/nowhere"}}}
    )
    with pytest.raises(MapiError) as exc:
        flatten(tree, bad)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID and exc.value.pointer == "/data/nodes/x"
    with pytest.raises(MapiError, match="pick nodes"):
        flatten(tree, None)  # the root holds no variables
    plain = root_dataset()
    assert flatten(plain, block) is plain


def test_tree_schema_from_the_input_the_tree_or_inference(tmp_path: Path) -> None:
    tree = toy.tree_dataset(weeks=6)
    inferred = infer_tree_schema(tree)
    ctx = IngestCtx(tmp_path, {})
    assert load_tree_schema(ctx, root_dataset()) is None  # type: ignore[arg-type]
    assert load_tree_schema(ctx, tree) == inferred  # type: ignore[arg-type]
    stored = inferred.model_copy(
        update={
            "nodes": {
                **inferred.nodes,
                "/sales": inferred.nodes["/sales"].model_copy(update={"layout": "dense"}),
            }
        }
    )
    tree.attrs[TREE_SCHEMA_ATTR] = stored.model_dump_json()
    assert load_tree_schema(ctx, tree) == stored  # type: ignore[arg-type]
    given = tmp_path / "ts.json"
    given.write_text(inferred.model_dump_json())
    ctx = IngestCtx(tmp_path, {INPUT_TREE_SCHEMA: given})
    assert load_tree_schema(ctx, tree) == inferred  # type: ignore[arg-type]


def test_side_tables_read_the_named_node(tmp_path: Path) -> None:
    text = toy.tree_spec(split=False).replace(
        "  bind:",
        "  side_tables:\n    fx: {ref: 'dataset://growth/fx@latest', node: /adjust}\n"
        "    plain: {ref: 'dataset://growth/plain@latest'}\n  bind:",
    )
    spec = load_model_spec(text)
    tree = toy.tree_dataset(weeks=6)
    assert set(side_table_dataset(tree, "fx", spec).data_vars) == {"offset"}
    with pytest.raises(MapiError) as exc:
        side_table_dataset(tree, "plain", spec)
    assert exc.value.pointer == "/data/side_tables/plain/node"
    with pytest.raises(MapiError, match="plain dataset"):
        side_table_dataset(root_dataset(), "fx", spec)
    ctx = IngestCtx(
        tmp_path,
        {
            "side.fx": _zip(tmp_path, "fx.zip", tree),
            "side.plain": _zip(tmp_path, "plain.zip", root_dataset()),
        },
    )
    tables = load_side_tables(ctx, spec)  # type: ignore[arg-type]
    assert set(tables) == {"fx", "plain"} and "offset" in tables["fx"]
