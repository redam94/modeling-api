"""Reading the run's inputs inside the worker: datasets, schemas, side tables, bundles."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import xarray as xr
from mapi_sdk.bundle import BUNDLE_FILE, Bundle, BundleManifest, file_digest
from mapi_sdk.errors import BundleError, WireFormatError
from mapi_sdk.wire import read_datatree_zip, read_zarr_zip
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import INPUT_BUNDLE, INPUT_DATASET
from mapi_spec.schema import DataSchema
from pydantic import ValidationError

from mapi_runner.jobs import INPUT_BUNDLE_MANIFEST, INPUT_SCHEMA, SIDE_TABLE_PREFIX
from mapi_runner.worker.context import WorkerContext

__all__ = [
    "SCHEMA_ATTR",
    "load_bundle",
    "load_dataset",
    "load_raw_schema",
    "load_side_tables",
]

SCHEMA_ATTR = "mapi_schema"
_MAX_JSON_INPUT = 32 * 1024 * 1024


def load_dataset(ctx: WorkerContext, name: str = INPUT_DATASET) -> xr.Dataset:
    """Download and decode a ``*.zarr.zip`` input (codec/size checks of the wire format)."""
    path = ctx.download(name)
    assert path is not None
    try:
        return read_zarr_zip(path)
    except WireFormatError as exc:
        raise MapiError(
            f"input {name!r} is not a readable dataset",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/inputs/{name}",
            untrusted_text=exc.detail,
        ) from None


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


def load_side_tables(ctx: WorkerContext) -> dict[str, xr.Dataset]:
    """Every ``side.<name>`` input, keyed by ``<name>``."""
    return {
        name.removeprefix(SIDE_TABLE_PREFIX): load_dataset(ctx, name)
        for name in ctx.input_names(SIDE_TABLE_PREFIX)
    }


def load_bundle(ctx: WorkerContext) -> Bundle:
    """Rebuild a fitted :class:`Bundle` from the ``bundle`` (+ ``bundle.manifest``) inputs.

    With a manifest, the archive must match its recorded size and hashes.
    """
    path = ctx.download(INPUT_BUNDLE)
    assert path is not None
    raw = path.read_bytes()
    manifest: BundleManifest
    if ctx.has_input(INPUT_BUNDLE_MANIFEST):
        mpath = ctx.download(INPUT_BUNDLE_MANIFEST)
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
        return Bundle.from_tree(tree, ctx.registry, manifest=manifest)
    except (WireFormatError, BundleError) as exc:
        raise MapiError(
            "the bundle cannot be read", code=ErrorCode.SCHEMA_INVALID, untrusted_text=exc.detail
        ) from None


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
