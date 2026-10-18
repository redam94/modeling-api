"""Tree datasets (M4): select, describe, check and persist ``xr.DataTree`` versions.

A tree dataset version is an ``xr.DataTree`` whose nodes each hold variables over their
own dims and the coords they inherit from their ancestors. Its static description is a
:class:`~mapi_spec.tree.TreeSchema`; a spec reads it through ``data.nodes``
(:class:`~mapi_spec.tree.NodeSelect` entries) merged into ONE flat ``xr.Dataset`` before
the pipeline, aligned per ``data.join``:

* :func:`select_nodes` - that flat merge, with exactly the rules and errors of
  :meth:`TreeSchema.flat <mapi_spec.tree.TreeSchema.flat>` (it runs them first) plus the
  value alignment of ``join`` (``exact`` refuses differing coords, ``inner`` keeps the
  shared labels, ``outer`` the union with missing values);
* :func:`selection_schema` - the schema of that flat dataset (the static merge, adjusted
  for what an ``outer`` join does to the values);
* :func:`infer_tree_schema` / :func:`validate_tree` / :func:`check_tree` - draft and check
  a tree's schema;
* :func:`read_tree_zip` / :func:`write_tree_zip` - ``data.tree.zip`` IO through
  :func:`~mapi_sdk.wire.read_datatree_zip` with the tree limits
  (:data:`~mapi_spec.tree.MAX_TREE_NODES`, :data:`~mapi_spec.tree.MAX_TREE_DEPTH`, node path
  syntax) checked before any array is decoded.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import IO, Any, Literal

import pandas as pd
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError, json_pointer
from mapi_spec.schema import DataSchema, Role
from mapi_spec.tree import (
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    NODE_PATH_RE,
    ROOT,
    NodeSelect,
    NodeSelectionError,
    TreeJoin,
    TreeSchema,
)
from pydantic import ValidationError

from mapi_sdk._util import dtype_family, normalise_dataset
from mapi_sdk.errors import SchemaValidationError, WireFormatError
from mapi_sdk.schema import SchemaIssue, infer_schema, validate_dataset
from mapi_sdk.wire import DEFAULT_LIMITS, WireLimits, read_datatree_zip, write_datatree_zip

__all__ = [
    "TREE_LIMITS",
    "TREE_SCHEMA_ATTR",
    "NodesArg",
    "check_tree",
    "infer_tree_schema",
    "node_dataset",
    "read_tree_zip",
    "select_nodes",
    "selection_error",
    "selection_schema",
    "stored_tree_schema",
    "validate_tree",
    "write_tree_zip",
]

TREE_LIMITS = dataclasses.replace(
    DEFAULT_LIMITS, max_tree_nodes=MAX_TREE_NODES, max_tree_depth=MAX_TREE_DEPTH
)
"""Wire limits for tree dataset versions (``data.tree.zip``)."""

NodesArg = Mapping[str, NodeSelect | Mapping[str, Any]] | None
"""``data.nodes``: ``{key: NodeSelect}`` (plain mappings are validated), or ``None`` for
the default (the root node, when it has variables)."""

_JOINS: frozenset[str] = frozenset({"exact", "inner", "outer"})

TREE_SCHEMA_ATTR = "mapi_tree_schema"
"""Root attribute of a tree dataset version (``data.tree.zip``) holding its TreeSchema
(JSON), as ``mapi_schema`` holds a plain dataset's."""


# ------------------------------------------------------------------------------ nodes


def _rel_path(tree: xr.DataTree, node: xr.DataTree) -> str:
    """``node``'s path relative to ``tree`` as a node path (``/`` for ``tree`` itself)."""
    if node is tree:
        return ROOT
    rel = node.relative_to(tree)
    return ROOT if rel in (".", "") else "/" + rel.lstrip("/")


def _nodes(tree: xr.DataTree) -> dict[str, xr.DataTree]:
    return {_rel_path(tree, node): node for node in tree.subtree}


def _node(tree: xr.DataTree, path: str) -> xr.DataTree | None:
    if path == ROOT:
        return tree
    try:
        node = tree[path.lstrip("/")]
    except KeyError:
        return None
    return node if isinstance(node, xr.DataTree) else None


def _own_dims(node: xr.DataTree) -> set[str]:
    return {str(d) for d in node.to_dataset(inherit=False).dims}


def _restrict(full: xr.Dataset, names: list[str], dims: Collection[str]) -> xr.Dataset:
    """``names`` of ``full`` plus every coord whose dims all lie in ``dims``."""
    keep = set(dims)
    out = full[names] if names else xr.Dataset(attrs=full.attrs)
    extra = {
        k: v
        for k, v in full.coords.items()
        if k not in out.coords and {str(d) for d in v.dims} <= keep
    }
    if extra:
        out = out.assign_coords(extra)
    return out


def node_dataset(tree: xr.DataTree, path: str = ROOT) -> xr.Dataset:
    """The dataset of node ``path``: its own variables over its own dims, with the coords
    of those dims whether the node defines them or inherits them from an ancestor.

    Coords of ancestor dims the node does not use are left out (they belong to the
    ancestor). Raises :class:`~mapi_spec.tree.NodeSelectionError` for an unknown node.
    """
    node = _node(tree, path)
    if node is None:
        raise NodeSelectionError(f"the dataset has no node {path!r}")
    full = node.to_dataset(inherit=True)
    return _restrict(full, [str(v) for v in full.data_vars], _own_dims(node))


# ----------------------------------------------------------------------------- schema


def stored_tree_schema(tree: xr.DataTree) -> TreeSchema | None:
    """The TreeSchema stored in ``tree``'s root attrs (:data:`TREE_SCHEMA_ATTR`, written by
    the ingest), or ``None`` when there is none, it is invalid or its nodes are not the
    tree's. The stored schema keeps what the upload's mapping declared (dim specs a
    per-node inference cannot recover), so every reader prefers it to inferring one."""
    raw = tree.attrs.get(TREE_SCHEMA_ATTR)
    if not isinstance(raw, str):
        return None
    try:
        schema = TreeSchema.model_validate_json(raw)
    except ValueError:
        return None
    return schema if set(schema.nodes) == set(_nodes(tree)) else None


def infer_tree_schema(
    tree: xr.DataTree,
    *,
    targets: Collection[str] = (),
    roles: Mapping[str, Role | str] | None = None,
) -> TreeSchema:
    """Draft a :class:`~mapi_spec.tree.TreeSchema` from ``tree``: one
    :func:`~mapi_sdk.schema.infer_schema` per node over :func:`node_dataset` (inherited
    coords included, so a node's dims get the same spec as its ancestor's).

    ``targets`` / ``roles`` name variables (in any node) as for ``infer_schema``. Raises
    :class:`~mapi_sdk.errors.SchemaValidationError` when the tree breaks the tree rules
    (node names, node count or depth, a variable named like an ancestor's dim, ...).
    """
    nodes: dict[str, DataSchema] = {}
    for path in _nodes(tree):
        try:
            ds = node_dataset(tree, path)
            nodes[path] = infer_schema(ds, targets=targets, roles=roles)
        except (ValueError, ValidationError) as exc:
            raise SchemaValidationError(
                f"node {path[:200]!r} cannot be described: {str(exc)[:1000]}"
            ) from None
    try:
        return TreeSchema(nodes=nodes)
    except ValidationError as exc:
        msg = "; ".join(str(e["msg"]) for e in exc.errors()[:5])
        raise SchemaValidationError(f"the tree is not a valid tree dataset: {msg}") from None


def validate_tree(
    tree: xr.DataTree, schema: TreeSchema, *, check_values: bool = True
) -> list[SchemaIssue]:
    """Check ``tree`` against ``schema`` node by node (:func:`~mapi_sdk.schema.
    validate_dataset` on :func:`node_dataset`); describe every problem.

    Nodes the schema declares but the tree lacks, and nodes the tree has but the schema
    does not declare, are errors. Every message names its node.
    """
    issues: list[SchemaIssue] = []
    present = _nodes(tree)
    for path in schema.nodes:
        if path not in present:
            issues.append(
                SchemaIssue(message=f"the tree has no node {path!r}", context={"node": path[:200]})
            )
    for path in present:
        if path not in schema.nodes:
            issues.append(
                SchemaIssue(
                    message=f"node {path[:200]!r} is not declared in the tree schema",
                    context={"node": path[:200]},
                )
            )
    for path, node_schema in schema.nodes.items():
        if path not in present:
            continue
        ds = node_dataset(tree, path)
        for issue in validate_dataset(node_schema, ds, check_values=check_values):
            prefix = "" if path == ROOT else f"node {path!r}: "
            issues.append(
                issue.model_copy(
                    update={
                        "message": (prefix + issue.message)[:2000],
                        "context": {**issue.context, "node": path},
                    }
                )
            )
    return issues


def check_tree(tree: xr.DataTree, schema: TreeSchema, **kwargs: Any) -> list[SchemaIssue]:
    """:func:`validate_tree`, raising :class:`~mapi_sdk.errors.SchemaValidationError` on
    any error (warnings are returned)."""
    issues = validate_tree(tree, schema, **kwargs)
    errors = [i for i in issues if i.severity.value == "error"]
    if errors:
        more = f" (and {len(errors) - 1} more problem(s))" if len(errors) > 1 else ""
        raise SchemaValidationError(
            f"{errors[0].message}{more}"[:2000], issues=[i.to_issue() for i in issues]
        )
    return issues


# -------------------------------------------------------------------------- selection


def _selection(nodes: NodesArg) -> dict[str, NodeSelect] | None:
    if nodes is None:
        return None
    out: dict[str, NodeSelect] = {}
    for key, sel in nodes.items():
        try:
            out[key] = sel if isinstance(sel, NodeSelect) else NodeSelect.model_validate(sel)
        except ValidationError as exc:
            msg = "; ".join(str(e["msg"]) for e in exc.errors()[:3])
            raise NodeSelectionError(f"node selection {key!r} is invalid: {msg}", key=key) from None
    return out


def _default_selection(schema: TreeSchema) -> dict[str, NodeSelect]:
    if not schema.root.vars:
        raise NodeSelectionError(
            "this dataset is a tree: pick nodes in data.nodes (e.g. node "
            f"{schema.default_node()!r})"
        )
    return {"root": NodeSelect(path=ROOT)}


def _used_dims(schema: DataSchema, names: list[str], every: bool) -> set[str]:
    used: set[str] = set(schema.dims) if every else set()
    for n in names:
        used.update(schema.vars[n].dims)
        if idx := schema.vars[n].index_of:
            used.add(idx)
    return used


def _check_exact(parts: list[tuple[str, xr.Dataset]]) -> None:
    """``join="exact"``: every dim shared by several selections has the same labels."""
    seen: dict[str, tuple[str, pd.Index | int]] = {}
    for key, ds in parts:
        for dim, size in ds.sizes.items():
            name = str(dim)
            labels: pd.Index | int = ds.indexes[name] if name in ds.indexes else int(size)
            if name not in seen:
                seen[name] = (key, labels)
                continue
            other_key, other = seen[name]
            same = (
                other.equals(labels)
                if isinstance(other, pd.Index) and isinstance(labels, pd.Index)
                else other == labels
            )
            if not same:
                raise NodeSelectionError(
                    f"the labels of dim {name!r} differ between the nodes selected by "
                    f"{other_key!r} and {key!r}: set data.join to 'inner' (shared labels) "
                    "or 'outer' (all labels)",
                    key=key,
                    name=name,
                )


def select_nodes(
    tree: xr.DataTree,
    nodes: NodesArg = None,
    join: TreeJoin = "exact",
    *,
    schema: TreeSchema | None = None,
) -> xr.Dataset:
    """Merge the variables of the selected nodes into ONE flat dataset (``data.nodes``).

    Each entry contributes the variables of its node (all, or ``vars``), renamed
    ``prefix + name``, with the coords of the dims they use (inherited ones included).
    ``nodes=None`` reads the root node (an error when the root holds no variables).

    The rules and errors are those of :meth:`TreeSchema.flat
    <mapi_spec.tree.TreeSchema.flat>` (checked first on ``schema``; when omitted, the one
    stored in the tree (:func:`stored_tree_schema`), else one inferred from it): unknown
    nodes or variables, a variable produced twice, a variable named like a selected dim,
    dims whose spec differs between nodes, mixed layouts. Coordinate values then align
    per ``join`` (``exact``: equal labels, else an error; ``inner``: shared labels;
    ``outer``: all labels, missing values elsewhere). Every problem raises
    :class:`~mapi_spec.tree.NodeSelectionError` (``key`` names the entry).

    Non-index coords are not part of the schema, so they never make a selection fail: one
    named like a selected variable, or differing between nodes, is left out.
    """
    if join not in _JOINS:
        raise NodeSelectionError(f"join must be one of {sorted(_JOINS)}, not {join!r}")
    if schema is None:
        schema = stored_tree_schema(tree) or infer_tree_schema(tree)
    selection = _selection(nodes)
    if selection is None:
        selection = _default_selection(schema)
    schema.flat(selection)  # the static rules, with their errors
    parts: list[tuple[str, xr.Dataset]] = []
    for key, sel in selection.items():
        node = _node(tree, sel.path)
        node_schema = schema.nodes[sel.path]
        if node is None:
            raise NodeSelectionError(f"the dataset has no node {sel.path!r}", key=key)
        full = node.to_dataset(inherit=True)
        names = [str(v) for v in full.data_vars] if sel.vars is None else list(sel.vars)
        missing = [n for n in names if n not in full.data_vars]
        if missing:
            raise NodeSelectionError(
                f"node {sel.path!r} has no variable(s) {missing}", key=key, name=missing[0]
            )
        dims = _used_dims(
            node_schema, [n for n in names if n in node_schema.vars], sel.vars is None
        )
        if sel.vars is None:
            dims |= _own_dims(node)
        part = _restrict(full, names, dims)
        if sel.prefix:
            part = part.rename({n: f"{sel.prefix}{n}" for n in names})
        part.attrs = {}
        parts.append((key, part))
    if join == "exact":
        _check_exact(parts)
    cores: list[xr.Dataset] = []
    aux: list[xr.DataArray] = []
    for _, p in parts:
        extra = [str(n) for n in p.coords if n not in p.xindexes]
        cores.append(p.drop_vars(extra) if extra else p)
        aux.extend(p.coords[n] for n in extra)
    try:
        merged = xr.merge(
            cores,
            join=join,
            compat="no_conflicts",
            combine_attrs="drop_conflicts",
        )
    except (ValueError, KeyError) as exc:
        raise NodeSelectionError(f"the selected nodes cannot be merged: {str(exc)[:500]}") from None
    merged = _with_aux_coords(merged, aux)
    merged.attrs = {}
    if not merged.data_vars:
        raise NodeSelectionError("the selected nodes hold no variables")
    return normalise_dataset(merged)


def _with_aux_coords(merged: xr.Dataset, aux: list[xr.DataArray]) -> xr.Dataset:
    """``merged`` plus the selected nodes' non-index coords that fit it: aligned to its
    labels, and left out when one is named like a variable or differs between nodes."""
    added: dict[str, xr.Variable] = {}
    dropped: set[str] = set()
    for coord in aux:
        name = str(coord.name)
        if name in dropped or (name in merged.variables and name not in added):
            continue
        try:
            labels = {
                d: merged.indexes[d]
                for d in coord.dims
                if d in coord.indexes and d in merged.indexes
            }
            var = (coord.reindex(labels) if labels else coord).variable
            if any(merged.sizes.get(d, -1) != n for d, n in var.sizes.items()):
                raise ValueError("the coord does not fit the merged dims")
        except (ValueError, KeyError, TypeError):
            dropped.add(name)
            added.pop(name, None)
            continue
        if name in added:
            if not xr.DataArray(added[name]).equals(xr.DataArray(var)):
                dropped.add(name)
                del added[name]
            continue
        added[name] = var
    return merged.assign_coords(added) if added else merged


def selection_schema(
    schema: TreeSchema,
    nodes: NodesArg = None,
    *,
    data: xr.Dataset | None = None,
    join: TreeJoin = "exact",
) -> DataSchema:
    """The schema of the flat dataset ``nodes`` selects (:meth:`TreeSchema.flat`; the root
    node when ``nodes`` is ``None``).

    With ``join="outer"`` and the merged ``data`` at hand, variables the alignment left
    with missing values allow them, and integer/bool variables it turned into floats are
    ``float64`` - what the merge did, not what the nodes declare.
    """
    selection = _selection(nodes)
    flat = schema.flat(selection if selection is not None else _default_selection(schema))
    if data is None or join != "outer":
        return flat
    doc = flat.model_dump()
    changed = False
    for name, spec in doc["vars"].items():
        if name not in data:
            continue
        da = data[name]
        fam = dtype_family(da.dtype)
        if fam == "float" and spec["dtype"] not in ("float64", "float32"):
            spec["dtype"] = "float64"
            changed = True
        if spec["missing"] == "forbid" and fam in ("float", "datetime") and bool(da.isnull().any()):
            spec["missing"] = "allow"
            changed = True
    return DataSchema.model_validate(doc) if changed else flat


def selection_error(exc: NodeSelectionError) -> MapiError:
    """``exc`` as a ``SCHEMA_INVALID`` :class:`~mapi_spec.errors.MapiError` pointing at
    the ``data.nodes`` entry at fault (``/data`` when the selection as a whole is)."""
    if exc.key is not None:
        pointer = json_pointer("data", "nodes", exc.key)
    elif "data.nodes" in str(exc):
        pointer = json_pointer("data")
    else:
        pointer = json_pointer("data", "nodes")
    return MapiError(str(exc)[:2000], code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


# --------------------------------------------------------------------------------- IO


def write_tree_zip(
    tree: xr.DataTree,
    dest: str | Path | IO[bytes] | None = None,
    *,
    compressor: Literal["zstd", "blosc"] = "zstd",
) -> bytes | None:
    """Write a tree dataset (``data.tree.zip``) after checking its node paths, count and
    depth; returns the bytes when ``dest`` is ``None``."""
    paths = list(_nodes(tree))
    if len(paths) > MAX_TREE_NODES:
        raise WireFormatError(f"tree has {len(paths)} nodes; at most {MAX_TREE_NODES}")
    for path in paths:
        if not re.fullmatch(NODE_PATH_RE, path):
            raise WireFormatError(
                f"tree node {path[:200]!r} is not a valid node path (at most "
                f"{MAX_TREE_DEPTH} levels of names like 'psf' or 'year_2024')"
            )
    return write_datatree_zip(tree, dest, compressor=compressor)


def read_tree_zip(
    src: str | Path | bytes | IO[bytes], *, limits: WireLimits = TREE_LIMITS
) -> xr.DataTree:
    """Read an untrusted tree dataset (``data.tree.zip``): every node is checked as a zarr
    zip is, and node count, depth and path syntax before any array is decoded."""
    return read_datatree_zip(src, limits=limits, node_pattern=NODE_PATH_RE)
