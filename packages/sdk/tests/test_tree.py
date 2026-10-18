"""mapi_sdk.tree: node selection, tree schemas and data.tree.zip IO (M4)."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk import wire
from mapi_sdk.errors import PayloadLimitError, SchemaValidationError, WireFormatError
from mapi_sdk.testing import toy
from mapi_sdk.tree import (
    TREE_LIMITS,
    check_tree,
    infer_tree_schema,
    node_dataset,
    read_tree_zip,
    select_nodes,
    selection_error,
    selection_schema,
    validate_tree,
    write_tree_zip,
)
from mapi_spec.errors import ErrorCode
from mapi_spec.tree import MAX_TREE_NODES, NodeSelect, NodeSelectionError, TreeSchema

TIME = pd.date_range("2024-01-07", periods=6, freq="W-SUN").as_unit("ns")


def _tree(**extra: xr.Dataset) -> xr.DataTree:
    """Root coords (geo, time) inherited by /a and /b; /a/deep adds a dim of its own."""
    nodes = {
        "/": xr.Dataset(coords={"geo": ["x", "y"], "time": TIME}, attrs={"source": "test"}),
        "/a": xr.Dataset(
            {
                "y": (("geo", "time"), np.arange(12.0).reshape(2, 6), {"units": "EUR"}),
                "w": (("geo",), [1.0, 2.0]),
            }
        ),
        "/b": xr.Dataset({"y": (("geo", "time"), np.ones((2, 6))), "z": (("time",), np.arange(6))}),
        "/a/deep": xr.Dataset({"k": (("lag",), [0.5, 0.25, 0.125])}, coords={"lag": [1, 2, 3]}),
    }
    nodes.update(extra)
    return xr.DataTree.from_dict(nodes)


def sel(**entries: dict[str, Any]) -> dict[str, NodeSelect]:
    return {k: NodeSelect.model_validate(v) for k, v in entries.items()}


# ------------------------------------------------------------------------ node datasets


def test_node_dataset_inherits_only_the_coords_of_its_own_dims() -> None:
    t = _tree()
    a = node_dataset(t, "/a")
    assert set(a.data_vars) == {"y", "w"}
    np.testing.assert_array_equal(a["time"].values, TIME.values)  # inherited from the root
    assert list(a["geo"].values) == ["x", "y"]
    deep = node_dataset(t, "/a/deep")
    assert set(deep.dims) == {"lag"}  # the root's geo/time are not this node's dims
    assert "time" not in deep.coords
    with pytest.raises(NodeSelectionError):
        node_dataset(t, "/nope")


# -------------------------------------------------------------------------------- schema


def test_infer_tree_schema_gives_children_the_ancestors_dim_specs() -> None:
    schema = infer_tree_schema(_tree())
    assert set(schema.nodes) == {"/", "/a", "/a/deep", "/b"}
    assert schema.nodes["/"].vars == {}
    assert schema.nodes["/a"].dims["time"] == schema.nodes["/"].dims["time"]
    assert schema.nodes["/a"].dims["time"].kind == "time"
    assert schema.nodes["/a"].dims["time"].freq == "W-SUN"
    assert set(schema.nodes["/a/deep"].dims) == {"lag"}
    assert schema.default_node() == "/a"
    with_target = infer_tree_schema(_tree(), targets=["y"])
    assert with_target.nodes["/a"].vars["y"].role.value == "target"


def test_infer_tree_schema_refuses_bad_trees() -> None:
    badname = xr.DataTree.from_dict({"/": xr.Dataset(), "/9bad": xr.Dataset({"v": ("q", [1])})})
    with pytest.raises(SchemaValidationError):
        infer_tree_schema(badname)
    deep: dict[str, xr.Dataset] = {"/": xr.Dataset()}
    path = ""
    for i in range(9):
        path += f"/l{i}"
        deep[path] = xr.Dataset()
    with pytest.raises(SchemaValidationError):
        infer_tree_schema(xr.DataTree.from_dict(deep))
    odd = xr.DataTree.from_dict({"/": xr.Dataset({"c": (("q",), np.array([1j]))})})
    with pytest.raises(SchemaValidationError) as err:
        infer_tree_schema(odd)
    assert "node '/'" in str(err.value)


def test_validate_and_check_tree() -> None:
    t = _tree()
    schema = infer_tree_schema(t)
    assert [i for i in validate_tree(t, schema) if i.severity.value == "error"] == []
    check_tree(t, schema)
    # a node the schema does not know, a node the tree lacks, a missing variable
    extra = _tree(**{"/c": xr.Dataset({"v": (("time",), np.zeros(6))})})
    issues = validate_tree(extra, schema)
    assert any("'/c' is not declared" in i.message for i in issues)
    t2 = t.copy()
    del t2["b"]
    issues = validate_tree(t2, schema)
    assert any("has no node '/b'" in i.message for i in issues)
    t3 = t.copy()
    t3["a"].dataset = t3["a"].to_dataset(inherit=False).drop_vars("w")
    issues = validate_tree(t3, schema)
    msg = next(i.message for i in issues if i.severity.value == "error")
    assert msg.startswith("node '/a': ")
    assert next(i.context for i in issues if i.severity.value == "error")["node"] == "/a"
    with pytest.raises(SchemaValidationError) as err:
        check_tree(t3, schema)
    assert err.value.issues


# ----------------------------------------------------------------------------- selection


def test_select_one_node_with_inherited_coords() -> None:
    flat = select_nodes(_tree(), sel(a={"path": "/a"}))
    assert set(flat.data_vars) == {"y", "w"}
    np.testing.assert_array_equal(flat["time"].values, TIME.values)
    assert flat["y"].attrs["units"] == "EUR"
    assert flat.attrs == {}  # the tree's attrs (and any stored schema) do not leak


def test_select_with_prefixes_and_var_subsets() -> None:
    flat = select_nodes(
        _tree(),
        sel(a={"path": "/a", "vars": ["y"], "prefix": "a_"}, b={"path": "/b", "prefix": "b_"}),
    )
    assert set(flat.data_vars) == {"a_y", "b_y", "b_z"}
    assert "w" not in flat
    np.testing.assert_array_equal(flat["b_y"].values, np.ones((2, 6)))
    # a selection that only takes 'z' (time) does not bring in the geo dim
    only_z = select_nodes(_tree(), sel(b={"path": "/b", "vars": ["z"]}))
    assert set(only_z.dims) == {"time"}


def test_select_deep_node_brings_its_own_dims() -> None:
    flat = select_nodes(_tree(), {"a": {"path": "/a"}, "deep": {"path": "/a/deep"}})
    assert set(flat.dims) == {"geo", "time", "lag"}
    assert list(flat["lag"].values) == [1, 2, 3]


def test_default_selection_is_the_root() -> None:
    plain = xr.DataTree.from_dict(
        {"/": xr.Dataset({"v": (("t",), [1.0, 2.0])}, coords={"t": [0, 1]})}
    )
    assert list(select_nodes(plain)["v"].values) == [1.0, 2.0]
    with pytest.raises(NodeSelectionError) as err:
        select_nodes(_tree())
    assert "pick nodes in data.nodes" in str(err.value)
    assert "'/a'" in str(err.value)
    assert selection_error(err.value).pointer == "/data"


@pytest.mark.parametrize(
    ("entries", "key", "needle"),
    [
        ({"a": {"path": "/a"}, "b": {"path": "/b"}}, "b", "give one of them a prefix"),
        ({"a": {"path": "/nope"}}, "a", "has no node '/nope'"),
        ({"a": {"path": "/a", "vars": ["nope"]}}, "a", "no variable(s) ['nope']"),
    ],
)
def test_selection_conflicts_raise_like_the_static_rules(
    entries: dict[str, Any], key: str, needle: str
) -> None:
    t = _tree()
    schema = infer_tree_schema(t)
    with pytest.raises(NodeSelectionError) as static:
        schema.flat(sel(**entries))
    with pytest.raises(NodeSelectionError) as runtime:
        select_nodes(t, entries)
    assert str(runtime.value) == str(static.value)
    assert runtime.value.key == key and needle in str(runtime.value)
    err = selection_error(runtime.value)
    assert err.code is ErrorCode.SCHEMA_INVALID
    assert err.pointer == f"/data/nodes/{key}"


def test_dims_with_different_specs_conflict() -> None:
    t = _tree(
        **{
            "/c": xr.Dataset(
                {"q": (("week",), [1.0, 2.0])},
                coords={"week": pd.to_datetime(["2024-01-07", "2024-01-14"])},
            ),
            "/d": xr.Dataset({"r": (("week",), [1.0, 2.0])}, coords={"week": [1, 2]}),
        }
    )
    with pytest.raises(NodeSelectionError) as err:
        select_nodes(t, {"c": {"path": "/c"}, "d": {"path": "/d"}})
    assert err.value.key == "d" and err.value.name == "week"


def _shifted() -> xr.DataTree:
    """Two sibling nodes over the same dim with partly different labels."""
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(),
            "/early": xr.Dataset({"a": (("t",), [1, 2, 3])}, coords={"t": [0, 1, 2]}),
            "/late": xr.Dataset({"b": (("t",), [10.0, 20.0, 30.0])}, coords={"t": [1, 2, 3]}),
        }
    )


def test_join_modes_align_values() -> None:
    nodes = {"e": {"path": "/early"}, "l": {"path": "/late"}}
    with pytest.raises(NodeSelectionError) as err:
        select_nodes(_shifted(), nodes)
    assert err.value.name == "t" and err.value.key == "l"
    assert "data.join" in str(err.value)
    inner = select_nodes(_shifted(), nodes, "inner")
    assert list(inner["t"].values) == [1, 2]
    assert list(inner["a"].values) == [2, 3] and inner["a"].dtype.kind == "i"
    outer = select_nodes(_shifted(), nodes, "outer")
    assert list(outer["t"].values) == [0, 1, 2, 3]
    assert np.isnan(outer["b"].values[0]) and np.isnan(outer["a"].values[-1])
    with pytest.raises(NodeSelectionError):
        select_nodes(_shifted(), nodes, "left")  # type: ignore[arg-type]


def test_exact_join_without_labels_checks_sizes() -> None:
    t = xr.DataTree.from_dict(
        {
            "/": xr.Dataset(),
            "/p": xr.Dataset({"a": (("n",), [1.0, 2.0])}),
            "/q": xr.Dataset({"b": (("n",), [1.0, 2.0, 3.0])}),
        }
    )
    with pytest.raises(NodeSelectionError):
        select_nodes(t, {"p": {"path": "/p"}, "q": {"path": "/q"}})


def test_conflicting_non_index_coords_refuse_to_merge() -> None:
    t = xr.DataTree.from_dict(
        {
            "/": xr.Dataset(coords={"t": [0, 1]}),
            "/p": xr.Dataset({"a": (("t",), [1.0, 2.0])}, coords={"label": (("t",), ["u", "v"])}),
            "/q": xr.Dataset({"b": (("t",), [1.0, 2.0])}, coords={"label": (("t",), ["u", "w"])}),
        }
    )
    with pytest.raises(NodeSelectionError) as err:
        select_nodes(t, {"p": {"path": "/p"}, "q": {"path": "/q"}})
    assert "cannot be merged" in str(err.value)


def test_selection_schema_matches_the_flat_dataset() -> None:
    t = _tree()
    schema = infer_tree_schema(t)
    nodes = {"a": {"path": "/a", "prefix": "a_"}, "b": {"path": "/b", "vars": ["z"]}}
    flat = select_nodes(t, nodes, schema=schema)
    static = selection_schema(schema, nodes)
    assert set(static.vars) == set(flat.data_vars)
    assert set(static.dims) == {str(d) for d in flat.dims}
    with pytest.raises(NodeSelectionError):
        selection_schema(schema)  # the root holds no variables


def test_selection_schema_outer_join_follows_the_values() -> None:
    t = _shifted()
    schema = infer_tree_schema(t)
    nodes = {"e": {"path": "/early"}, "l": {"path": "/late"}}
    assert schema.nodes["/early"].vars["a"].dtype == "int64"
    outer = select_nodes(t, nodes, "outer", schema=schema)
    got = selection_schema(schema, nodes, data=outer, join="outer")
    assert got.vars["a"].dtype == "float64" and got.vars["a"].missing == "allow"
    assert got.vars["b"].missing == "allow"
    inner = select_nodes(t, nodes, "inner", schema=schema)
    assert selection_schema(schema, nodes, data=inner, join="inner") == schema.flat(sel(**nodes))


def test_given_schema_drives_the_rules() -> None:
    t = _tree()
    schema = infer_tree_schema(t)
    doc = schema.model_dump()
    del doc["nodes"]["/b"]
    smaller = TreeSchema.model_validate(doc)
    with pytest.raises(NodeSelectionError):
        select_nodes(t, {"b": {"path": "/b"}}, schema=smaller)


def test_invalid_selection_entries() -> None:
    with pytest.raises(NodeSelectionError) as err:
        select_nodes(_tree(), {"a": {"path": "no-slash"}})
    assert err.value.key == "a"


# ------------------------------------------------------------------------------------ IO


def test_tree_zip_roundtrip_keeps_nodes_values_and_attrs() -> None:
    t = toy.tree_dataset()
    raw = write_tree_zip(t)
    assert raw is not None
    back = read_tree_zip(raw)
    assert sorted(n.path for n in back.subtree) == sorted(n.path for n in t.subtree)
    for node in t.subtree:
        xr.testing.assert_identical(
            back[node.path].to_dataset(inherit=False), node.to_dataset(inherit=False)
        )
    assert back.attrs["title"] == "toy tree dataset"
    flat = select_nodes(back, {"s": {"path": "/sales"}, "m": {"path": "/media"}})
    xr.testing.assert_identical(
        flat, select_nodes(t, {"s": {"path": "/sales"}, "m": {"path": "/media"}})
    )
    assert write_tree_zip(back) == raw  # deterministic bytes


def _store(groups: list[str]) -> bytes:
    """A minimal zarr v3 zip declaring ``groups`` (no arrays)."""
    buf = io.BytesIO()
    meta = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {}}).encode()
    with zipfile.ZipFile(buf, "w") as zf:
        for g in groups:
            zf.writestr("zarr.json" if g == "/" else g.lstrip("/") + "/zarr.json", meta)
    return buf.getvalue()


def test_read_tree_zip_checks_node_limits_before_decoding() -> None:
    too_many = ["/"] + [f"/n{i}" for i in range(MAX_TREE_NODES)]
    with pytest.raises(PayloadLimitError):
        read_tree_zip(_store(too_many))
    too_deep = ["/"] + ["/" + "/".join(f"l{j}" for j in range(i + 1)) for i in range(9)]
    with pytest.raises(PayloadLimitError):
        read_tree_zip(_store(too_deep))
    with pytest.raises(WireFormatError):
        read_tree_zip(_store(["/", "/9bad"]))
    # the general reader (bundles) keeps its generous defaults and accepts these
    tree = wire.read_datatree_zip(_store(["/", "/9bad"]))
    assert "9bad" in tree.children
    assert TREE_LIMITS.max_tree_nodes == MAX_TREE_NODES < wire.DEFAULT_LIMITS.max_tree_nodes


def test_read_datatree_zip_has_tree_caps() -> None:
    limits = wire.WireLimits(max_tree_nodes=2)
    with pytest.raises(PayloadLimitError):
        wire.read_datatree_zip(_store(["/", "/a", "/b"]), limits=limits)
    with pytest.raises(PayloadLimitError):
        wire.read_datatree_zip(
            _store(["/", "/a", "/a/b"]), limits=wire.WireLimits(max_tree_depth=1)
        )


def test_write_tree_zip_refuses_bad_node_paths() -> None:
    deep: dict[str, xr.Dataset] = {"/": xr.Dataset()}
    path = ""
    for i in range(9):
        path += f"/l{i}"
        deep[path] = xr.Dataset()
    with pytest.raises(WireFormatError):
        write_tree_zip(xr.DataTree.from_dict(deep))
    with pytest.raises(WireFormatError):
        write_tree_zip(xr.DataTree.from_dict({"/": xr.Dataset(), "/9bad": xr.Dataset()}))
