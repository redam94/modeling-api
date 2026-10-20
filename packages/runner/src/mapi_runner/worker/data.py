"""Reading the run's inputs inside the worker: datasets, schemas, side tables, bundles.

M4: a dataset input is a plain ``*.zarr.zip`` or a tree (``data.tree.zip``, nested zarr
groups). :func:`load_dataset` returns an ``xr.Dataset`` or an ``xr.DataTree`` accordingly;
:func:`flatten` selects a tree's nodes per a spec's ``data.nodes`` (the flat pipeline
input) and :func:`load_tree_schema` reads its :class:`~mapi_spec.tree.TreeSchema`.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.binding import BindingMap
from mapi_sdk.bundle import BUNDLE_FILE, Bundle, BundleManifest, MetricsReport, file_digest
from mapi_sdk.errors import BundleError, SchemaValidationError, WireFormatError
from mapi_sdk.tree import (
    infer_tree_schema,
    node_dataset,
    read_tree_zip,
    select_nodes,
    selection_error,
    stored_tree_schema,
)
from mapi_sdk.wire import DEFAULT_LIMITS, loads_xrjson, read_datatree_zip, read_zarr_zip
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.jobs import DATASET_SCHEMA_ATTR, INPUT_METRICS_SUFFIX
from mapi_spec.run import INPUT_BUNDLE, INPUT_DATASET
from mapi_spec.schema import DataSchema
from mapi_spec.spec import DataBlock, ModelSpec, ScorePolicy
from mapi_spec.tree import ROOT, NodeSelectionError, TreeSchema
from mapi_spec.xrjson import MAX_XRJSON_BYTES
from pydantic import ValidationError

from mapi_runner.jobs import INPUT_BUNDLE_MANIFEST, INPUT_SCHEMA, SIDE_TABLE_PREFIX
from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.tree_ingest import zarr_has_groups

__all__ = [
    "INPUT_TREE_SCHEMA",
    "SCHEMA_ATTR",
    "flatten",
    "load_bundle",
    "load_data_and_tree",
    "load_dataset",
    "load_flat_dataset",
    "load_inline",
    "load_raw_schema",
    "load_side_tables",
    "load_tree_schema",
    "side_table_dataset",
    "supplied_side_tables",
]

SCHEMA_ATTR = DATASET_SCHEMA_ATTR
INPUT_TREE_SCHEMA: Final = "tree_schema"
"""M4: optional input with the TreeSchema of a tree ``dataset`` input (``tree_schema.json``
of its version); without it the schema stored in the tree, else an inferred one, is used."""
_MAX_JSON_INPUT = 32 * 1024 * 1024


def load_dataset(ctx: WorkerContext, name: str = INPUT_DATASET) -> xr.Dataset | xr.DataTree:
    """Download and decode a dataset input: a ``*.zarr.zip`` (codec/size checks of the wire
    format) as an ``xr.Dataset``, or (M4) a tree dataset (nested groups, ``data.tree.zip``)
    as an ``xr.DataTree`` under the tree limits."""
    path = ctx.download(name)
    assert path is not None
    try:
        if zarr_has_groups(path):
            return read_tree_zip(path)
        return read_zarr_zip(path)
    except WireFormatError as exc:
        raise MapiError(
            f"input {name!r} is not a readable dataset",
            code=exc.code,
            pointer=f"/inputs/{name}",
            untrusted_text=exc.detail,
        ) from None


def load_tree_schema(ctx: WorkerContext, data: xr.Dataset | xr.DataTree) -> TreeSchema | None:
    """The TreeSchema of a tree input (``None`` for a plain dataset): the ``tree_schema``
    input, else the one stored in the tree's root attrs, else inferred."""
    if not isinstance(data, xr.DataTree):
        return None
    paths = {node.path for node in data.subtree}
    if ctx.has_input(INPUT_TREE_SCHEMA):
        path = ctx.download(INPUT_TREE_SCHEMA)
        assert path is not None
        raw = path.read_bytes()
        if len(raw) > _MAX_JSON_INPUT:
            raise MapiError("the tree schema input is too large", code=ErrorCode.PAYLOAD_TOO_LARGE)
        try:
            given = TreeSchema.model_validate_json(raw)
        except ValidationError as exc:
            raise MapiError(
                "the tree schema input is not a valid TreeSchema",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/inputs/{INPUT_TREE_SCHEMA}",
                untrusted_text=str(exc)[:8000],
            ) from None
        if set(given.nodes) == paths:
            return given
    stored = stored_tree_schema(data)
    if stored is not None:
        return stored
    try:
        return infer_tree_schema(data)
    except SchemaValidationError as exc:
        raise MapiError(
            "the tree dataset cannot be described",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/data",
            untrusted_text=exc.detail,
        ) from None


def flatten(
    data: xr.Dataset | xr.DataTree,
    block: DataBlock | None = None,
    schema: TreeSchema | None = None,
) -> xr.Dataset:
    """The flat dataset a spec's pipeline reads: ``data`` itself, or (a tree) the merge of
    the nodes ``block.nodes`` selects aligned per ``block.join`` (the root node without
    ``nodes``). Selection problems are ``SCHEMA_INVALID`` at ``/data/nodes/<key>``."""
    if isinstance(data, xr.Dataset):
        return data
    nodes = block.nodes if block is not None else None
    join = block.join if block is not None else "exact"
    try:
        return select_nodes(data, nodes, join, schema=schema)
    except NodeSelectionError as exc:
        raise selection_error(exc) from None


def load_flat_dataset(
    ctx: WorkerContext, block: DataBlock | None = None, name: str = INPUT_DATASET
) -> xr.Dataset:
    """:func:`load_dataset` + :func:`flatten` (for runs that only read the flat data)."""
    data = load_dataset(ctx, name)
    return flatten(data, block, load_tree_schema(ctx, data))


def load_data_and_tree(
    ctx: WorkerContext, block: DataBlock | None = None, name: str = INPUT_DATASET
) -> tuple[xr.Dataset, xr.DataTree | None]:
    """:func:`load_flat_dataset`, plus the tree it was selected from (``None`` for a plain
    dataset): runs that score a tree-input model hand it to the model (M4)."""
    data = load_dataset(ctx, name)
    flat = flatten(data, block, load_tree_schema(ctx, data))
    return flat, data if isinstance(data, xr.DataTree) else None


_INLINE_LIMITS = replace(DEFAULT_LIMITS, max_json_bytes=MAX_XRJSON_BYTES + 64 * 1024)
"""An endpoint call's inline payload: the API caps the call at ``MAX_XRJSON_BYTES``; the
stored document adds only the envelope's own keys."""


def load_inline(
    ctx: WorkerContext,
    name: str,
    schema: DataSchema | None,
    binding: BindingMap | None = None,
) -> xr.Dataset:
    """Decode an inline XrJSON score payload (``inline.data`` / ``inline.scenario``, stored by
    the API from an endpoint call) with the wire format's caps, then give its dim coordinates
    the types of ``schema``'s dims (JSON carries dates as ISO strings and every number as a
    float). Strict JSON only: no NaN or Infinity constants; nulls become missing values.

    An endpoint's input contract (``EndpointSchemaSnapshot.input_schema``, which the API
    validates the call against) names dims and variables canonically (``time``, ``y``), while
    ``schema`` is the bundle's raw schema under the dataset's names (``week``,
    ``units_sold``): with ``binding`` the payload's canonical names are renamed to the dataset
    names first, so it is typed and scored like the same rows uploaded as a dataset."""
    path = ctx.download(name)
    assert path is not None
    pointer = "/" + name.removeprefix("inline.")
    try:
        ds = loads_xrjson(path.read_bytes(), limits=_INLINE_LIMITS)
    except WireFormatError as exc:
        raise MapiError(
            f"the inline {pointer[1:]} payload is not a readable dataset",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=pointer,
            untrusted_text=exc.detail,
        ) from None
    if binding is not None:
        ds = binding.reverse(ds)
    if schema is None:
        return ds
    coords: dict[str, Any] = {}
    for dim, spec in schema.dims.items():
        if dim not in ds.coords or ds[dim].ndim != 1:
            continue
        values = ds[dim].values
        try:
            if spec.kind == "time":
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    coords[dim] = pd.to_datetime(values).values.astype("datetime64[ns]")
            elif spec.kind in ("integer", "index"):
                num = np.asarray(values, dtype="float64")
                if not np.all(np.isfinite(num)) or not np.all(num % 1 == 0):
                    raise ValueError("not whole numbers")
                coords[dim] = num.astype("int64")
            elif spec.kind == "float":
                coords[dim] = np.asarray(values, dtype="float64")
            else:
                coords[dim] = np.asarray([str(v) for v in values.tolist()], dtype=object)
        except (ValueError, TypeError, OverflowError) as exc:
            raise MapiError(
                f"coordinate {dim!r} of the inline {pointer[1:]} payload cannot be read as a "
                f"{spec.kind} dim",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"{pointer}/coords/{dim}",
                untrusted_text=str(exc)[:2000],
            ) from None
    return ds.assign_coords(coords) if coords else ds


def load_raw_schema(ctx: WorkerContext, data: xr.Dataset) -> DataSchema | None:
    """The dataset's raw schema: ``schema`` input, else the ``mapi_schema`` attr, else None."""
    if ctx.has_input(INPUT_SCHEMA):
        path = ctx.download(INPUT_SCHEMA)
        assert path is not None
        raw = path.read_bytes()
        if len(raw) > _MAX_JSON_INPUT:
            raise MapiError("the schema input is too large", code=ErrorCode.PAYLOAD_TOO_LARGE)
        try:
            return DataSchema.model_validate_json(raw)
        except ValidationError as exc:
            raise MapiError(
                "the schema input is not a valid DataSchema",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/inputs/{INPUT_SCHEMA}",
                untrusted_text=str(exc)[:8000],
            ) from None
    stored = data.attrs.get(SCHEMA_ATTR)
    try:
        if isinstance(stored, str):
            return DataSchema.model_validate_json(stored)
        if isinstance(stored, dict):
            return DataSchema.model_validate(stored)
    except ValidationError as exc:
        raise MapiError(
            f"the dataset's {SCHEMA_ATTR!r} attribute is not a valid DataSchema",
            code=ErrorCode.SCHEMA_INVALID,
            untrusted_text=str(exc)[:8000],
        ) from None
    return None


def side_table_dataset(
    data: xr.Dataset | xr.DataTree, key: str, spec: ModelSpec | None = None
) -> xr.Dataset:
    """Side table ``key`` as a dataset: a tree side table reads the ONE node its
    ``data.side_tables.<key>.node`` names (M4)."""
    table = spec.data.side_tables.get(key) if spec is not None else None
    node = table.node if table is not None else None
    pointer = f"/data/side_tables/{key}/node"
    if isinstance(data, xr.Dataset):
        if node not in (None, ROOT):
            raise MapiError(
                f"side table {key!r} is a plain dataset: it has no node {node!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=pointer,
            )
        return data
    if node is None:
        raise MapiError(
            f"side table {key!r} is a tree dataset: name the node to read in "
            f"data.side_tables.{key}.node",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=pointer,
        )
    try:
        return node_dataset(data, node)
    except NodeSelectionError as exc:
        raise MapiError(str(exc)[:2000], code=ErrorCode.SCHEMA_INVALID, pointer=pointer) from None


def load_side_tables(ctx: WorkerContext, spec: ModelSpec | None = None) -> dict[str, xr.Dataset]:
    """Every ``side.<name>`` input, keyed by ``<name>`` (a tree side table: the node
    ``spec`` names for it)."""
    out: dict[str, xr.Dataset] = {}
    for name in ctx.input_names(SIDE_TABLE_PREFIX):
        key = name.removeprefix(SIDE_TABLE_PREFIX)
        out[key] = side_table_dataset(load_dataset(ctx, name), key, spec)
    return out


def supplied_side_tables(spec: ModelSpec, available: Iterable[str]) -> dict[str, str]:
    """Refs of the spec's ``score_policy: supplied`` side tables that are available; a score
    request must name these to replay the steps that use them."""
    have = set(available)
    return {
        name: str(st.ref)
        for name, st in spec.data.side_tables.items()
        if st.score_policy is ScorePolicy.SUPPLIED and name in have
    }


def load_bundle(
    ctx: WorkerContext,
    name: str = INPUT_BUNDLE,
    manifest_name: str = INPUT_BUNDLE_MANIFEST,
) -> Bundle:
    """Rebuild a fitted :class:`Bundle` from the ``bundle`` (+ ``bundle.manifest``) inputs
    (M3.5: or any other pair, e.g. ``evaluation`` or ``compare.<k>``).

    With a manifest, the archive must match its recorded size and hashes. v0.10: with a
    ``<name>.metrics`` input (the fit's ``metrics.json``), the bundle has its metrics.
    """
    path = ctx.download(name)
    assert path is not None
    raw = path.read_bytes()
    manifest: BundleManifest
    if ctx.has_input(manifest_name):
        mpath = ctx.download(manifest_name)
        assert mpath is not None
        try:
            manifest = BundleManifest.model_validate_json(mpath.read_bytes())
        except ValidationError as exc:
            raise MapiError(
                "the bundle manifest is invalid",
                code=ErrorCode.INTEGRITY_FAILURE,
                untrusted_text=str(exc)[:8000],
            ) from None
        want = manifest.files.get(BUNDLE_FILE)
        if want is None or file_digest(raw) != want:
            raise MapiError(
                "the bundle archive does not match its manifest", code=ErrorCode.INTEGRITY_FAILURE
            )
    else:
        manifest = _manifest_from_tree(raw)
    try:
        tree = read_datatree_zip(raw)
        bundle = Bundle.from_tree(tree, ctx.registry, manifest=manifest)
    except (WireFormatError, BundleError) as exc:
        raise MapiError(
            "the bundle cannot be read", code=ErrorCode.SCHEMA_INVALID, untrusted_text=exc.detail
        ) from None
    metrics_name = f"{name}{INPUT_METRICS_SUFFIX}"
    if ctx.has_input(metrics_name):
        # metrics.json is a sidecar of the fit, not part of the archive
        mpath = ctx.download(metrics_name)
        assert mpath is not None
        try:
            bundle.metrics = MetricsReport.model_validate_json(mpath.read_bytes())
        except ValidationError as exc:
            raise MapiError(
                "the bundle's metrics.json is invalid",
                code=ErrorCode.SCHEMA_INVALID,
                untrusted_text=str(exc)[:8000],
            ) from None
    return bundle


def _manifest_from_tree(raw: bytes) -> BundleManifest:
    """A minimal manifest for a bundle that arrived without its sidecar."""
    try:
        tree = read_datatree_zip(raw)
    except WireFormatError as exc:
        raise MapiError(
            "the bundle cannot be read", code=ErrorCode.SCHEMA_INVALID, untrusted_text=exc.detail
        ) from None
    attrs: dict[str, Any] = {str(k): v for k, v in tree.attrs.items()}
    spec_node = tree.children.get("spec")
    spec_hash = ""
    if isinstance(spec_node, xr.DataTree):
        resolved = spec_node.attrs.get("resolved")
        if isinstance(resolved, str):
            spec_hash = str(json.loads(resolved).get("spec_hash", ""))
    role = attrs.get("role", "production")
    return BundleManifest(
        bundle_id=str(attrs.get("bundle_id", "unknown")),
        role=role if role in ("production", "evaluation") else "production",
        created_at=datetime.now(UTC).isoformat(),
        spec_hash=spec_hash,
        files={BUNDLE_FILE: file_digest(raw)},
    )
