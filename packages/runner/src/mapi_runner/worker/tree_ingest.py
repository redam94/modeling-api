"""Tree datasets in ingest (M4): NetCDF groups, zarr trees, raw HDF5 and multi-file versions.

A dataset version is an ``xr.DataTree`` (layout ``tree``) when its upload has groups - a
NetCDF-4/HDF5 file or a ``*.zarr.zip`` with nested groups, a raw HDF5 file whose mapping
puts variables below the root - or when an :class:`~mapi_spec.ingest.IngestAssembly` puts
sources in nodes other than the root. Everything else stays a plain dataset exactly as
before M4 (a NetCDF file with only a root group included). No group is ever dropped
silently: a group that holds nothing still becomes a (pure) node of the tree.

This module turns parsed uploads into trees and combines the sources of an assembly per
node (``node`` mount, ``concat`` along a new dim, ``merge`` of variables); the ingest run
(:mod:`mapi_runner.worker.ingest`) writes the outputs.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from mapi_sdk.errors import PayloadLimitError, SchemaValidationError, WireFormatError
from mapi_sdk.tree import (
    TREE_LIMITS,
    TREE_SCHEMA_ATTR,
    infer_tree_schema,
    read_tree_zip,
    stored_tree_schema,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.guided import ColumnProfile
from mapi_spec.ingest import AssemblySource, IngestAssembly, IngestMapping
from mapi_spec.schema import DataSchema, DimSpec
from mapi_spec.tree import (
    MAX_TREE_NODES,
    NODE_PATH_RE,
    ROOT,
    TreeJoin,
    TreeSchema,
    node_ancestors,
    node_depth,
    node_join,
)
from pydantic import ValidationError

from mapi_runner.worker.tabular import IngestLimits, problem

__all__ = [
    "TREE_SCHEMA_ATTR",
    "Upload",
    "assemble",
    "explore_slug",
    "map_tree",
    "node_parts",
    "read_netcdf_tree",
    "read_zarr_tree",
    "schema_for_nodes",
    "tree_from_nodes",
    "zarr_has_groups",
]

VarMeta = Callable[[str, str], Mapping[str, Any] | None]
"""``(node, variable) ->`` role/units/missing/description a mapping declares, or None."""
DimMeta = Callable[[str, str], DimSpec | None]
"""``(node, dim) ->`` the DimSpec a mapping declares for that dim there, or None."""


def _none_var(_node: str, _name: str) -> Mapping[str, Any] | None:
    return None


def _none_dim(_node: str, _dim: str) -> DimSpec | None:
    return None


@dataclass(slots=True)
class Upload:
    """One parsed upload (or a combined assembly): a dataset or a tree and its schema."""

    data: xr.Dataset | xr.DataTree
    schema: DataSchema | TreeSchema
    columns: list[ColumnProfile] | None = None
    warnings: list[tuple[str, str | None]] = field(default_factory=list)
    """``(message, untrusted text)`` reported as run warnings."""
    notes: list[dict[str, str]] = field(default_factory=list)
    """``{pointer, message}`` reported as info issues."""
    size: int = 0
    """Bytes of the uploaded file(s)."""

    @property
    def is_tree(self) -> bool:
        return isinstance(self.data, xr.DataTree)


def _invalid(
    message: str, pointer: str = "", *, action: str | None = None, untrusted: str | None = None
) -> MapiError:
    return problem(
        ErrorCode.SCHEMA_INVALID, message, pointer=pointer, action=action, untrusted=untrusted
    )


def _too_large(message: str, pointer: str = "", action: str | None = None) -> MapiError:
    return problem(ErrorCode.PAYLOAD_TOO_LARGE, message, pointer=pointer, action=action)


MAX_EXPLORE_SLUG = 64
"""Longest readable slug; a longer node path gets a hashed one (the server serves explorer
views whose whole object name has at most 256 characters)."""


def explore_slug(path: str) -> str:
    """The directory of a node's explorer views under ``ui/explore/nodes/`` (``~root`` for
    the root, else the path with ``~`` for ``/``: node names never contain ``~``). A path
    whose slug would be longer than :data:`MAX_EXPLORE_SLUG` gets ``~h`` + a hash of it
    (node names never start with ``~``, so no readable slug collides with it)."""
    if path == ROOT:
        return "~root"
    slug = path[1:].replace("/", "~")
    if len(slug) <= MAX_EXPLORE_SLUG:
        return slug
    return "~h" + hashlib.sha256(path.encode()).hexdigest()[:40]


# ------------------------------------------------------------------------------- limits


def check_tree_limits(tree: xr.DataTree, limits: IngestLimits, budget: int) -> None:
    """Nodes (count, path syntax, depth), variables per node and decoded bytes of ``tree``
    (lazily opened: only metadata is read)."""
    nodes = list(tree.subtree)
    cap = min(limits.max_nodes, MAX_TREE_NODES)
    if len(nodes) > cap:
        raise _too_large(
            f"the upload has {len(nodes):,} groups; a tree dataset has at most {cap:,} nodes",
            action="Merge groups, or split the upload into several datasets.",
        )
    total = 0
    for node in nodes:
        path = node.path
        if not re.fullmatch(NODE_PATH_RE, path):
            raise _invalid(
                "a group name of the upload cannot be a node name",
                action="Name groups with letters, digits, '_', '.' or '-' (starting with a "
                "letter or '_', at most 64 characters) and nest them at most 8 levels deep.",
                untrusted=path[:2000],
            )
        own = node.to_dataset(inherit=False)
        if len(own.data_vars) > limits.max_columns:
            raise _too_large(
                f"a group has {len(own.data_vars):,} variables (the maximum is "
                f"{limits.max_columns:,})",
                action="Drop the variables the model does not need.",
            )
        total += int(own.nbytes)
        if total > budget:
            raise _too_large(
                f"the upload decodes to more than {budget:,} bytes (the maximum is "
                f"{limits.max_decompression_ratio:g} times its size and "
                f"{limits.max_uncompressed_bytes:,} bytes)",
                action="Split the dataset, or write it with less compression.",
            )


# ------------------------------------------------------------------------------ readers


def zarr_has_groups(path: Path) -> bool:
    """True when a ``*.zarr.zip`` holds groups below its root (a DataTree store)."""
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                name = info.filename
                if not name.endswith("/zarr.json") or info.file_size > 1024 * 1024:
                    continue
                try:
                    meta = json.loads(zf.read(info))
                except (ValueError, UnicodeDecodeError, RecursionError):
                    continue
                if isinstance(meta, dict) and meta.get("node_type") == "group":
                    return True
    except (zipfile.BadZipFile, OSError):
        return False
    return False


def read_zarr_tree(path: Path, limits: IngestLimits) -> xr.DataTree:
    """A zarr zip with nested groups, under the tree limits (checked before decoding)."""
    wire = replace(
        TREE_LIMITS,
        max_uncompressed_bytes=limits.max_uncompressed_bytes,
        max_string_chars=limits.max_cell_chars,
        max_variables=limits.max_columns,
        max_tree_nodes=min(limits.max_nodes, MAX_TREE_NODES),
    )
    try:
        tree = read_tree_zip(path, limits=wire)
    except PayloadLimitError as exc:
        raise problem(
            ErrorCode.PAYLOAD_TOO_LARGE,
            "the zarr zip is larger than the upload limits (size, nodes or depth)",
            action="Split the dataset, or use a larger resource class.",
            untrusted=exc.detail,
        ) from None
    except WireFormatError as exc:
        raise _invalid(
            "the upload is not a readable zarr tree",
            action="Write it with mapi_sdk.wire.write_datatree_zip (zarr v3, blosc or zstd "
            "codecs; group names of letters, digits, '_', '.' and '-').",
            untrusted=exc.detail,
        ) from None
    check_tree_limits(tree, limits, limits.max_uncompressed_bytes)
    return tree


def read_netcdf_tree(path: Path, limits: IngestLimits) -> xr.DataTree:
    """A NetCDF-4 / HDF5 file with groups as a tree (call it after the HDF5 guard)."""
    budget = limits.expansion_budget(path.stat().st_size)
    try:
        opened = xr.open_datatree(path, engine="h5netcdf")
    except (ImportError, ModuleNotFoundError) as exc:
        raise MapiError(
            "netCDF-4 (HDF5) uploads need h5netcdf in the runner image",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            untrusted_text=str(exc)[:2000],
        ) from None
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise _invalid(
            "the groups of the netCDF file cannot be read as a tree",
            action="A group sees the dims of the groups above it: a dim with the same name must "
            "have the same length (and labels) there. Rename the dim in one of the groups.",
            untrusted=str(exc)[:2000],
        ) from None
    try:
        check_tree_limits(opened, limits, budget)
        return opened.load()
    except (ValueError, OSError) as exc:
        raise _invalid(
            "the upload is not a readable netCDF file", untrusted=str(exc)[:2000]
        ) from None
    finally:
        opened.close()


# ----------------------------------------------------------------------------- structure


def tree_from_nodes(nodes: Mapping[str, xr.Dataset], pointer: str = "") -> xr.DataTree:
    """Build a tree from node datasets (ancestors are created as needed). An indexed coord
    a node repeats from an ancestor with the same values is left to inheritance; any other
    coord stays on the node (xarray only inherits indexed coords)."""
    cleaned: dict[str, xr.Dataset] = {}
    for path in sorted(nodes, key=node_depth):
        ds = nodes[path]
        drop: list[str] = []
        for name in [str(c) for c in ds.coords]:
            if name not in ds.xindexes:
                continue
            for anc in node_ancestors(path):
                above = cleaned.get(anc)
                if (
                    above is not None
                    and name in above.xindexes
                    and above[name].equals(ds[name])
                ):
                    drop.append(name)
                    break
        cleaned[path] = ds.drop_vars(drop) if drop else ds
    try:
        return xr.DataTree.from_dict(cleaned)
    except (ValueError, KeyError, TypeError) as exc:
        raise _invalid(
            "the nodes of the dataset do not fit together",
            pointer,
            action="A node sees the dims and coords of the nodes above it: a dim with the same "
            "name must have the same length and labels there. Rename the dim in one node.",
            untrusted=str(exc)[:2000],
        ) from None


def node_parts(data: xr.Dataset | xr.DataTree, *, inherit: bool = False) -> dict[str, xr.Dataset]:
    """Every node's own dataset (``{"/": ds}`` for a plain dataset); ``inherit`` adds the
    indexed coords it inherits, so its dims are labelled (an assembly aligns nodes by
    label, never by position)."""
    if isinstance(data, xr.Dataset):
        return {ROOT: data}
    return {node.path: node.to_dataset(inherit=inherit) for node in data.subtree}


def map_tree(
    tree: xr.DataTree, mapping: IngestMapping | None
) -> tuple[xr.DataTree, VarMeta, DimMeta]:
    """Apply a NetCDF/zarr mapping's ``dims``/``vars`` to every node by name (rename, and
    keep only the mapped variables when ``vars`` is given), as for a plain dataset."""
    if mapping is None or not (mapping.dims or mapping.vars):
        return tree, _none_var, _none_dim
    rename = {dm.column: n for n, dm in mapping.dims.items() if dm.column != n}
    rename |= {vm.column: n for n, vm in mapping.vars.items() if vm.column != n}
    parts = node_parts(tree)
    found: set[str] = set()
    for ds in parts.values():
        found |= {str(v) for v in ds.variables} | {str(d) for d in ds.dims}
    missing = [c for c in rename if c not in found]
    if missing:
        raise _invalid(
            f"the upload has no variable or dim {missing[0][:64]!r} in any group",
            "/mapping",
            action=f"It has {', '.join(repr(v[:40]) for v in sorted(found)[:8])}.",
        )
    out: dict[str, xr.Dataset] = {}
    kept: set[str] = set()
    for path, ds in parts.items():
        local = {k: v for k, v in rename.items() if k in ds.variables or k in ds.dims}
        renamed = ds.rename(local) if local else ds
        if mapping.vars:
            names = [v for v in mapping.vars if v in renamed.data_vars]
            kept |= set(names)
            renamed = renamed[names] if names else renamed.drop_vars(list(renamed.data_vars))
        out[path] = renamed
    absent = [v for v in mapping.vars if v not in kept]
    if absent:
        raise _invalid(
            f"no group of the upload has the variable {absent[0][:64]!r}", "/mapping/vars"
        )

    def var_meta(_node: str, name: str) -> Mapping[str, Any] | None:
        vm = mapping.vars.get(name)
        if vm is None:
            return None
        meta: dict[str, Any] = {
            "role": vm.role,
            "missing": vm.missing,
            "description": vm.description,
        }
        if vm.units:
            meta["units"] = vm.units
        return meta

    def dim_meta(_node: str, dim: str) -> DimSpec | None:
        dm = mapping.dims.get(dim)
        return None if dm is None else dm.spec

    return tree_from_nodes(out, "/mapping"), var_meta, dim_meta


def schema_for_nodes(
    tree: xr.DataTree,
    *,
    var_meta: VarMeta = _none_var,
    dim_meta: DimMeta = _none_dim,
    layout: str = "dense",
    allow_missing: Sequence[str] = (),
    use_stored: bool = True,
    layouts: Mapping[str, str] | None = None,
) -> TreeSchema:
    """The TreeSchema of ``tree``: the stored one (a ``data.tree.zip`` written by the
    platform), else inferred per node and overlaid with what the mapping declares.
    ``allow_missing`` lists nodes whose values an outer join may have left missing;
    ``layouts`` gives nodes their own layout (else ``layout``)."""
    if use_stored and (stored := stored_tree_schema(tree)) is not None:
        return stored
    try:
        base = infer_tree_schema(tree)
    except SchemaValidationError as exc:
        raise _invalid(
            "the upload is not a valid tree dataset",
            action="A variable may not have the name of a dim of a group above it; rename it.",
            untrusted=exc.detail,
        ) from None
    nodes: dict[str, DataSchema] = {}
    for path, s in base.nodes.items():
        dims: dict[str, DimSpec] = {}
        for d, spec in s.dims.items():
            declared = next(
                (x for x in (dim_meta(p, d) for p in [path, *node_ancestors(path)]) if x),
                None,
            )
            dims[d] = declared or spec
        vars_ = {}
        for n, vs in s.vars.items():
            meta = dict(var_meta(path, n) or {})
            if (
                meta.get("missing") == "forbid"
                and path in allow_missing
                and node_dataset_has_null(tree, path, n)
            ):
                meta["missing"] = "allow"
            vars_[n] = vs.model_copy(update=meta) if meta else vs
        try:
            own = (layouts or {}).get(path, layout)
            nodes[path] = DataSchema(
                layout=own if vars_ else "dense", dims=dims, vars=vars_, ragged=s.ragged
            )
        except ValidationError as exc:
            msg = "; ".join(str(e["msg"]) for e in exc.errors()[:3])
            raise _invalid(f"node {path!r} has an invalid schema: {msg}") from None
    try:
        return TreeSchema(nodes=nodes)
    except ValidationError as exc:
        msg = "; ".join(str(e["msg"]) for e in exc.errors()[:3])
        raise _invalid(
            "the declared dims do not fit the tree",
            "/mapping",
            action="A dim shared by a node and a node above it needs the same spec in both.",
            untrusted=msg[:2000],
        ) from None


def node_dataset_has_null(tree: xr.DataTree, path: str, name: str) -> bool:
    node = tree if path == ROOT else tree[path.lstrip("/")]
    assert isinstance(node, xr.DataTree)
    da = node.to_dataset(inherit=False)[name]
    return da.dtype.kind in "fcmM" and bool(da.isnull().any())


# ------------------------------------------------------------------------------ assembly


def _label_array(labels: Sequence[Any]) -> np.ndarray:
    if all(isinstance(v, str) for v in labels):
        return np.array([str(v) for v in labels], dtype=str)
    if all(isinstance(v, int) for v in labels):
        return np.array(labels, dtype="int64")
    return np.array([float(v) for v in labels], dtype="float64")


def _signature(ds: xr.Dataset) -> dict[str, tuple[str, ...]]:
    return {str(n): tuple(str(d) for d in ds[n].dims) for n in ds.data_vars}


def _concat(
    path: str,
    parts: list[tuple[int, xr.Dataset]],
    sources: Sequence[AssemblySource],
) -> xr.Dataset:
    first = sources[parts[0][0]]
    dim = first.concat_dim
    assert dim is not None
    sig0: dict[str, tuple[str, ...]] | None = None
    prepared: list[xr.Dataset] = []
    for i, ds in parts:
        pointer = f"/assembly/sources/{i}"
        if dim in ds.dims or dim in ds.variables:
            raise _invalid(
                f"source {i} already has a dim or variable {dim!r}, the concat dim of node "
                f"{path!r}",
                f"{pointer}/concat_dim",
                action="Choose a new name for the dim the files are stacked along.",
            )
        sig = _signature(ds)
        if sig0 is None:
            sig0 = sig
        elif sig != sig0:
            diff = sorted(set(sig) ^ set(sig0)) or sorted(k for k in sig if sig[k] != sig0[k])
            raise _invalid(
                f"the files concatenated into node {path!r} need the same variables over the "
                f"same dims; source {i} differs in {', '.join(repr(d) for d in diff[:5])}",
                pointer,
                action="Concatenate only files with the same variables, or merge them instead.",
            )
        label = sources[i].concat_label
        prepared.append(ds.expand_dims({dim: _label_array([label])}))
    try:
        out = xr.concat(
            prepared,
            dim=dim,
            data_vars="all",
            coords="minimal",
            compat="equals",
            join=first.join,
            combine_attrs="drop_conflicts",
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise _invalid(
            f"the files of node {path!r} cannot be concatenated along {dim!r}",
            f"/assembly/sources/{parts[0][0]}",
            action="Their other dims must have the same labels (or set join to 'inner' or "
            "'outer'), and their coordinates must agree.",
            untrusted=str(exc)[:2000],
        ) from None
    labels = _label_array([sources[i].concat_label for i, _ in parts])
    return out.assign_coords({dim: labels})


def _merge(path: str, parts: list[tuple[int, xr.Dataset]], join: TreeJoin) -> xr.Dataset:
    try:
        return xr.merge(
            [ds for _, ds in parts],
            join=join,
            compat="no_conflicts",
            combine_attrs="drop_conflicts",
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise _invalid(
            f"the files merged into node {path!r} conflict",
            f"/assembly/sources/{parts[0][0]}",
            action="Variables with the same name must hold the same values where they overlap, "
            "and coords must have the same labels (or set join to 'inner' or 'outer').",
            untrusted=str(exc)[:2000],
        ) from None


def _source_meta(
    uploads: Sequence[Upload], contributors: Mapping[str, list[tuple[int, str]]]
) -> tuple[VarMeta, DimMeta]:
    """What the sources' own schemas say about each final node's variables and dims."""

    def schemas(path: str) -> list[DataSchema]:
        out: list[DataSchema] = []
        for i, sub in contributors.get(path, []):
            schema = uploads[i].schema
            if isinstance(schema, TreeSchema):
                node = schema.nodes.get(sub)
                if node is not None:
                    out.append(node)
            else:
                out.append(schema)
        return out

    def var_meta(path: str, name: str) -> Mapping[str, Any] | None:
        for s in schemas(path):
            vs = s.vars.get(name)
            if vs is not None:
                return {
                    "role": vs.role,
                    "units": vs.units,
                    "missing": vs.missing,
                    "description": vs.description,
                }
        return None

    def dim_meta(path: str, dim: str) -> DimSpec | None:
        for s in schemas(path):
            if dim in s.dims:
                return s.dims[dim]
        return None

    return var_meta, dim_meta


def _node_layouts(
    uploads: Sequence[Upload], contributors: Mapping[str, list[tuple[int, str]]]
) -> dict[str, str]:
    """Each final node's layout: ``long`` when every source node it combines (that holds
    variables) is long, else ``dense`` - per node, so a long table next to a dense one keeps
    its layout (and a selection mixing them is refused)."""
    out: dict[str, str] = {}
    for path, contrib in contributors.items():
        seen: set[str] = set()
        for i, sub in contrib:
            schema = uploads[i].schema
            node = schema.nodes.get(sub) if isinstance(schema, TreeSchema) else schema
            if node is not None and node.vars:
                seen.add(node.layout)
        out[path] = "long" if seen == {"long"} else "dense"
    return out


def _joined_size(indexes: list[Any], join: str) -> int:
    out = indexes[0]
    for idx in indexes[1:]:
        if join == "outer":
            out = out.union(idx)
        elif join == "inner":
            out = out.intersection(idx)
    return len(out)


def _combined_bytes(parts: list[tuple[int, xr.Dataset]], src: AssemblySource) -> int:
    """The bytes ``_concat``/``_merge`` of ``parts`` would produce, from their labels."""
    if src.combine == "node" or (src.combine == "merge" and len(parts) == 1):
        return sum(int(ds.nbytes) for _, ds in parts)
    sizes: dict[str, int] = {}
    dims = {str(d) for _, ds in parts for d in ds.dims}
    for dim in dims:
        having = [ds for _, ds in parts if dim in ds.dims]
        if all(dim in ds.indexes for ds in having):
            sizes[dim] = _joined_size([ds.indexes[dim] for ds in having], src.join)
        else:
            sizes[dim] = max(int(ds.sizes[dim]) for ds in having)
    stacked = src.combine == "concat"
    per_var: dict[str, int] = {}
    for _, ds in parts:
        for name, var in ds.variables.items():
            n = 1
            for d in var.dims:
                n *= sizes.get(str(d), int(ds.sizes[d]))
            if stacked and name in ds.data_vars:
                n *= len(parts)
            per_var[str(name)] = max(per_var.get(str(name), 0), n * int(var.dtype.itemsize))
    return sum(per_var.values())


def assemble(assembly: IngestAssembly, uploads: Sequence[Upload], limits: IngestLimits) -> Upload:
    """Combine the parsed sources of ``assembly`` (``uploads[i]`` is source ``i``) per node.

    The result is a plain dataset when everything lands in the root node, else a tree.
    """
    sources = assembly.sources
    contributions: dict[str, list[tuple[int, xr.Dataset]]] = {}
    contributors: dict[str, list[tuple[int, str]]] = {}
    owner: dict[str, str] = {}
    subpaths: dict[int, set[str]] = {}
    for i, (src, up) in enumerate(zip(sources, uploads, strict=True)):
        # inherited coords included: a child node's dims carry its file's labels, so the
        # sources align by label (a file with the same labels in another order must not
        # put the child's values on the wrong labels)
        parts = node_parts(up.data, inherit=True)
        subpaths[i] = set(parts)
        for sub, ds in parts.items():
            try:
                final = node_join(src.node, sub)
            except ValueError as exc:
                raise _invalid(
                    "a group of the file cannot be mounted at this node",
                    f"/assembly/sources/{i}/node",
                    action="Mount the file higher up (nodes are at most 8 levels deep).",
                    untrusted=str(exc)[:2000],
                ) from None
            if owner.setdefault(final, src.node) != src.node:
                raise _invalid(
                    f"node {final!r} would get data from sources of node {owner[final]!r} and "
                    f"of node {src.node!r}",
                    f"/assembly/sources/{i}/node",
                    action="Mount the sources at different nodes, or combine them in one node.",
                )
            contributions.setdefault(final, []).append((i, ds))
            contributors.setdefault(final, []).append((i, sub))
    for node, idx in assembly.nodes().items():
        if sources[idx[0]].combine == "concat":
            shapes = {frozenset(subpaths[i]) for i in idx}
            if len(shapes) > 1:
                raise _invalid(
                    f"the files concatenated into node {node!r} have different groups",
                    f"/assembly/sources/{idx[0]}",
                    action="Concatenate files with the same groups.",
                )
    # the combined size from the coordinate labels, before any array is allocated (an outer
    # join of sources with disjoint labels grows multiplicatively)
    budget = limits.expansion_budget(sum(u.size for u in uploads))
    estimate = sum(
        _combined_bytes(contrib, sources[contrib[0][0]]) for contrib in contributions.values()
    )
    if estimate > budget:
        raise _too_large(
            f"the combined dataset would hold about {estimate:,} bytes; the maximum is "
            f"{budget:,}",
            "/assembly",
            action="Combine fewer files, align their labels (join 'inner'), or split the "
            "dataset.",
        )
    nodes: dict[str, xr.Dataset] = {}
    outer: list[str] = []
    for final, contrib in contributions.items():
        src = sources[contrib[0][0]]
        if src.combine == "concat":
            nodes[final] = _concat(final, contrib, sources)
        elif src.combine == "merge" and len(contrib) > 1:
            nodes[final] = _merge(final, contrib, src.join)
        else:
            nodes[final] = contrib[0][1]
        if src.join == "outer" and len(contrib) > 1:
            outer.append(final)
    total = 0
    for path, ds in nodes.items():
        if len(ds.data_vars) > limits.max_columns:
            raise _too_large(
                f"node {path!r} has {len(ds.data_vars):,} variables (the maximum is "
                f"{limits.max_columns:,})",
                "/assembly",
            )
        total += int(ds.nbytes)
    if total > budget:
        raise _too_large(
            f"the combined dataset holds {total:,} bytes; the maximum is {budget:,}",
            "/assembly",
            action="Combine fewer files, or split the dataset.",
        )
    tree = tree_from_nodes(nodes, "/assembly")
    cap = min(limits.max_nodes, MAX_TREE_NODES)
    count = sum(1 for _ in tree.subtree)
    if count > cap:
        raise _too_large(
            f"the sources make {count:,} nodes; a tree dataset has at most {cap:,}",
            "/assembly",
        )
    var_meta, dim_meta = _source_meta(uploads, contributors)
    schema = schema_for_nodes(
        tree,
        var_meta=var_meta,
        dim_meta=dim_meta,
        layouts=_node_layouts(uploads, contributors),
        allow_missing=outer,
        use_stored=False,
    )
    columns: list[ColumnProfile] = []
    seen: set[str] = set()
    for u in uploads:
        for c in u.columns or []:
            if c.name not in seen:
                seen.add(c.name)
                columns.append(c)
    warnings = [w for u in uploads for w in u.warnings]
    notes = [n for u in uploads for n in u.notes]
    size = sum(u.size for u in uploads)
    if set(nodes) == {ROOT}:
        root = tree.to_dataset(inherit=False)
        return Upload(root, schema.nodes[ROOT], columns or None, warnings, notes, size)
    return Upload(tree, schema, columns or None, warnings, notes, size)


def prefix_error(exc: MapiError, prefix: str) -> MapiError:
    """``exc`` with its pointers moved under ``prefix`` (a source of an assembly)."""
    exc.pointer = prefix + (exc.pointer if exc.pointer.startswith("/") else "")
    exc.issues = [
        Issue.model_validate(
            {**i.model_dump(), "pointer": prefix + (i.pointer if i.pointer.startswith("/") else "")}
        )
        for i in exc.issues
    ]
    return exc
