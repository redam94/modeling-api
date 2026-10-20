"""Tree datasets (M4): the schema of an ``xr.DataTree`` and how a spec selects its nodes.

A dataset version is stored either as one ``xr.Dataset`` (layout ``dataset``, every version
before M4) or as an ``xr.DataTree`` (layout ``tree``): nodes addressed by a
:data:`NodePath` (``/`` is the root, ``/instrument/psf`` a child of ``/instrument``), each
holding a :class:`~mapi_spec.schema.DataSchema` (an empty one for a pure group).

A :class:`TreeSchema` describes the whole tree without any arrays. A node sees the coords
of its ancestors (xarray aligns children with inherited coords), so a dim declared by a
node and one of its ancestors must have an equal :class:`~mapi_spec.schema.DimSpec`.

By default a spec does not see a tree: ``data.nodes`` (:class:`NodeSelect` per entry)
picks nodes whose variables are merged into ONE flat dataset before the pipeline, and
:meth:`TreeSchema.flat` computes that merged schema statically (the SDK's
``select_nodes`` applies the same rules to arrays).

This module is pure Pydantic (no numpy/xarray): the control plane imports it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from mapi_spec._types import NAME_RE, Name, StrictModel
from mapi_spec.schema import DataSchema, DimSpec, RaggedSpec, VarSpec

__all__ = [
    "MAX_TREE_DEPTH",
    "MAX_TREE_NODES",
    "NODE_PATH_RE",
    "ROOT",
    "DataLayout",
    "NodePath",
    "NodeSelect",
    "NodeSelectionError",
    "TreeJoin",
    "TreeSchema",
    "is_ancestor",
    "node_ancestors",
    "node_depth",
    "node_join",
    "node_name",
    "node_parent",
]

MAX_TREE_NODES: Final = 256
"""Nodes of one tree dataset (the root and every group, empty or not)."""
MAX_TREE_DEPTH: Final = 8
"""Levels below the root (``/a/b/c/d/e/f/g/h`` is the deepest node path)."""

_SEGMENT = r"[A-Za-z_][A-Za-z0-9_.\-]{0,63}"
NODE_PATH_RE: Final = rf"^/(?:{_SEGMENT}(?:/{_SEGMENT}){{0,{MAX_TREE_DEPTH - 1}}})?$"
"""``/`` (the root) or ``/a/b`` with 1-8 segments of up to 64 characters each."""

NodePath = Annotated[str, StringConstraints(pattern=NODE_PATH_RE)]
"""A node of a tree dataset: ``/`` is the root, ``/instrument/psf`` a nested group."""

ROOT: Final = "/"

DataLayout = Literal["dataset", "tree", "collection"]
"""How a dataset version is stored: one ``xr.Dataset`` (``data.zarr.zip``), an
``xr.DataTree`` (``data.tree.zip`` with a ``tree_schema.json``) or, from v0.15, an image
collection (``manifest.parquet`` + ``collection.json`` + packs; :mod:`mapi_spec.collection`)."""

TreeJoin = Literal["exact", "inner", "outer"]
"""How the coords of merged nodes (or sources) align (``xr.merge``'s ``join``)."""


# ------------------------------------------------------------------------------ helpers


def node_parent(path: str) -> str | None:
    """The parent of ``path`` (``None`` for the root)."""
    if path == ROOT:
        return None
    head = path.rsplit("/", 1)[0]
    return head or ROOT


def node_name(path: str) -> str:
    """The last segment of ``path`` (``""`` for the root)."""
    return path.rsplit("/", 1)[1]


def node_depth(path: str) -> int:
    """Levels below the root (0 for ``/``)."""
    return 0 if path == ROOT else path.count("/")


def node_ancestors(path: str) -> list[str]:
    """The ancestors of ``path``, nearest first, ending with the root (``[]`` for the root)."""
    out: list[str] = []
    parent = node_parent(path)
    while parent is not None:
        out.append(parent)
        parent = node_parent(parent)
    return out


def is_ancestor(ancestor: str, path: str) -> bool:
    """True if ``ancestor`` is a strict ancestor of ``path`` (a node is not its own)."""
    if ancestor == path:
        return False
    return ancestor == ROOT or path.startswith(ancestor + "/")


def node_join(base: str, sub: str) -> str:
    """Mount ``sub`` (a path inside a file) under ``base``: ``("/instrument", "/psf")`` gives
    ``/instrument/psf``. Raises ``ValueError`` when either is not a :data:`NodePath` or the
    result is deeper than :data:`MAX_TREE_DEPTH`."""
    for p in (base, sub):
        if not p.startswith(ROOT) or (p != ROOT and p.endswith("/")):
            raise ValueError(f"{p!r} is not a node path (absolute, no trailing '/')")
    joined = base if sub == ROOT else sub if base == ROOT else base + sub
    if node_depth(joined) > MAX_TREE_DEPTH:
        raise ValueError(f"node {joined!r} is deeper than {MAX_TREE_DEPTH} levels")
    if not re.fullmatch(NODE_PATH_RE, joined):
        raise ValueError(f"{joined!r} is not a valid node path")
    return joined


# --------------------------------------------------------------------------- selection


class NodeSelect(StrictModel):
    """One node a spec reads from a tree dataset (an entry of ``data.nodes``).

    Its variables (all, or ``vars``) are merged into the flat pipeline input, renamed
    ``prefix + name`` when ``prefix`` is given (so two nodes with the same variable names can
    both be read). Dims are never renamed: a dim shared by several selected nodes must be
    the same dim (equal :class:`~mapi_spec.schema.DimSpec`).
    """

    path: NodePath
    vars: list[Name] | None = Field(
        None, min_length=1, max_length=4096, description="Default: every variable of the node"
    )
    prefix: str | None = Field(
        None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,31}$",
        description="Prepended to the node's variable names in the flat dataset",
    )

    @model_validator(mode="after")
    def _unique(self) -> NodeSelect:
        if self.vars is not None and len(set(self.vars)) != len(self.vars):
            raise ValueError("vars must be unique")
        return self


class NodeSelectionError(ValueError):
    """A node selection cannot be merged into one flat schema.

    ``key`` is the ``data.nodes`` entry at fault (``None`` when the selection as a whole is),
    ``name`` the variable or dim involved, if any.
    """

    def __init__(self, message: str, *, key: str | None = None, name: str | None = None):
        super().__init__(message)
        self.key = key
        self.name = name


# ------------------------------------------------------------------------------ schema


class TreeSchema(StrictModel):
    """Schema of an ``xr.DataTree``: one :class:`~mapi_spec.schema.DataSchema` per node.

    Every node's ancestors are present (an empty schema stands for a pure group), a
    dim a node shares with an ancestor has an equal spec (children align with inherited
    coords), and no variable has the name of an ancestor's dim (the inherited coord would
    hide it).
    """

    nodes: dict[NodePath, DataSchema] = Field(..., min_length=1, max_length=MAX_TREE_NODES)

    @model_validator(mode="after")
    def _structure(self) -> TreeSchema:
        problems: list[str] = []
        for path, schema in self.nodes.items():
            missing = [a for a in node_ancestors(path) if a not in self.nodes]
            if missing:
                problems.append(f"node {path!r} misses its ancestor(s) {sorted(missing)}")
                continue
            for anc in node_ancestors(path):
                parent_dims = self.nodes[anc].dims
                for dim, spec in schema.dims.items():
                    if dim in parent_dims and parent_dims[dim] != spec:
                        problems.append(
                            f"dim {dim!r} of node {path!r} differs from the same dim of "
                            f"its ancestor {anc!r}"
                        )
                if hidden := sorted(set(schema.vars) & set(parent_dims)):
                    problems.append(
                        f"variable(s) {hidden} of node {path!r} have the name of a dim of its "
                        f"ancestor {anc!r} (the inherited coord would hide them)"
                    )
        if problems:
            raise ValueError("; ".join(problems[:20]))
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def root(self) -> DataSchema:
        """The root node's schema (empty when the root is a pure group)."""
        return self.nodes.get(ROOT) or DataSchema()

    def children(self, path: str) -> list[str]:
        """The direct children of ``path`` (declaration order)."""
        return [p for p in self.nodes if node_parent(p) == path]

    def default_node(self) -> str:
        """The node a viewer shows first: the root if it has variables, else the first node
        (declaration order) that has some, else the root."""
        if self.root.vars:
            return ROOT
        return next((p for p, s in self.nodes.items() if s.vars), ROOT)

    def flat(self, selection: Mapping[str, NodeSelect]) -> DataSchema:
        """The schema of the flat dataset ``selection`` (``data.nodes``) merges.

        Each entry contributes the variables of its node (all, or ``vars``), renamed
        ``prefix + name``, and the dims those variables use (all the node's dims when every
        variable is taken). Raises :class:`NodeSelectionError` for an unknown node or
        variable, a variable name produced twice, a dim whose spec differs between nodes,
        nodes of different layouts, or a selection without any variable.
        """
        if not selection:
            raise NodeSelectionError("select at least one node")
        dims: dict[str, DimSpec] = {}
        dim_from: dict[str, str] = {}
        variables: dict[str, VarSpec] = {}
        var_from: dict[str, str] = {}
        ragged: list[RaggedSpec] = []
        layouts: dict[Literal["dense", "long"], str] = {}
        for key, sel in selection.items():
            node = self.nodes.get(sel.path)
            if node is None:
                raise NodeSelectionError(
                    f"the dataset has no node {sel.path!r} (nodes: "
                    f"{', '.join(list(self.nodes)[:20])})",
                    key=key,
                )
            names = list(node.vars) if sel.vars is None else sel.vars
            unknown = [n for n in names if n not in node.vars]
            if unknown:
                raise NodeSelectionError(
                    f"node {sel.path!r} has no variable(s) {unknown}", key=key, name=unknown[0]
                )
            if names:
                layouts.setdefault(node.layout, key)
            rename = {n: f"{sel.prefix or ''}{n}" for n in names}
            used: set[str] = set(node.dims) if sel.vars is None else set()
            for n in names:
                used.update(node.vars[n].dims)
                if idx := node.vars[n].index_of:
                    used.add(idx)
            for dim in [d for d in node.dims if d in used]:
                spec = node.dims[dim]
                if dim in variables:
                    raise NodeSelectionError(
                        f"dim {dim!r} of node {sel.path!r} has the name of a variable selected "
                        f"by {var_from[dim]!r}",
                        key=key,
                        name=dim,
                    )
                if dim in dims and dims[dim] != spec:
                    raise NodeSelectionError(
                        f"dim {dim!r} of node {sel.path!r} differs from the same dim selected "
                        f"by {dim_from[dim]!r}",
                        key=key,
                        name=dim,
                    )
                dims.setdefault(dim, spec)
                dim_from.setdefault(dim, key)
            for n in names:
                new = rename[n]
                if not re.fullmatch(NAME_RE, new):
                    raise NodeSelectionError(
                        f"variable {n!r} of node {sel.path!r} renamed {new!r} is not a valid "
                        "name (at most 128 characters): use a shorter prefix",
                        key=key,
                        name=n,
                    )
                if new in variables or new in dims:
                    other = var_from.get(new) or dim_from.get(new)
                    raise NodeSelectionError(
                        f"variable {new!r} (from node {sel.path!r}) is also selected by "
                        f"{other!r}: give one of them a prefix",
                        key=key,
                        name=new,
                    )
                variables[new] = node.vars[n]
                var_from[new] = key
            for r in node.ragged:
                if r.group_dim not in used or r.within_dim not in used:
                    continue
                if r.mask_var is not None:
                    if r.mask_var not in rename:
                        continue
                    r = r.model_copy(update={"mask_var": rename[r.mask_var]})
                if r not in ragged:
                    ragged.append(r)
        if len(layouts) > 1:
            raise NodeSelectionError(
                "the selected nodes mix dense and long layouts", key=next(reversed(layouts))
            )
        if not variables:
            raise NodeSelectionError("the selected nodes hold no variables")
        try:
            return DataSchema(
                layout=next(iter(layouts), "dense"),
                dims=dims,
                vars=variables,
                ragged=ragged,
            )
        except ValidationError as exc:
            msg = "; ".join(str(e["msg"]) for e in exc.errors()[:5])
            raise NodeSelectionError(f"the merged schema is invalid: {msg}") from None
