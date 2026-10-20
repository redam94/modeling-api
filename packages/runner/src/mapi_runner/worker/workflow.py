"""``workflow_node`` runs: one data node of a workflow, in the sandbox (M3.5).

The control plane walks the workflow DAG; every node that needs data runs here as its own
run (:class:`~mapi_spec.workflow.WorkflowNodeJob`). The worker materialises the bound
inputs (``in.<port>.<k>`` datasets and model bundles, inline JSON payloads), runs the node
implementation from :mod:`mapi_workflows.runtime`, and writes ``node_result.json``
(:class:`~mapi_spec.workflow.NodeResultSidecar`) plus one object per dataset or large JSON
output (:func:`mapi_workflows.runtime.sandbox.output_files`). The finalizer verifies the
objects like any run's; the orchestrator reads the sidecar.

Model-version inputs come as three run inputs: ``in.<port>.<k>`` (the bundle),
``in.<port>.<k>.manifest`` (its SDK manifest) and ``in.<port>.<k>.data`` (the dataset it
was fitted on); its ``model://`` reference is in the job's reserved ``_refs`` parameter.
A tree dataset input (M4) is its root node with the tree kept, so a model node's ``nodes``
can select other nodes of it.
"""

from __future__ import annotations

from typing import Any

import xarray as xr
from mapi_sdk.bundle import BUNDLE_FILE, Bundle, BundleManifest, file_digest
from mapi_sdk.errors import BundleError, WireFormatError
from mapi_sdk.wire import read_datatree_zip
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import RunnerPhase
from mapi_spec.workflow import NODE_RESULT_OUTPUT, WorkflowNodeJob
from mapi_spec.workflow_nodes import PortType
from pydantic import ValidationError

from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import _manifest_from_tree, load_data_and_tree, load_dataset

__all__ = ["run_workflow_node"]


def _bundle(ctx: WorkerContext, name: str) -> Bundle:
    path = ctx.download(name)
    assert path is not None
    raw = path.read_bytes()
    manifest_name = name + ".manifest"
    try:
        tree = read_datatree_zip(raw)
    except WireFormatError as exc:
        raise MapiError(
            f"input {name!r} is not a readable bundle",
            code=ErrorCode.SCHEMA_INVALID,
            untrusted_text=exc.detail,
        ) from None
    manifest: BundleManifest | None = None
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
    if manifest is None:
        manifest = _manifest_from_tree(raw)
    try:
        return Bundle.from_tree(tree, ctx.registry, manifest=manifest)
    except BundleError as exc:
        raise MapiError(
            "the bundle cannot be read", code=ErrorCode.SCHEMA_INVALID, untrusted_text=exc.detail
        ) from None


def run_workflow_node(ctx: WorkerContext, job: WorkflowNodeJob) -> None:
    """Execute one workflow node (raises on failure; the caller reports the error)."""
    from mapi_workflows.runtime.sandbox import (
        dataset_value,
        execute_job,
        output_files,
        parse_input_name,
    )
    from mapi_workflows.runtime.values import DatasetValue, ModelValue, Value

    ctx.state(RunnerPhase.LOAD_INPUTS, f"loading the inputs of {job.node_id}")
    refs_raw = job.params.get("_refs")
    refs: dict[str, Any] = dict(refs_raw) if isinstance(refs_raw, dict) else {}
    found: dict[str, list[tuple[int, Value]]] = {}
    for name in ctx.input_names("in."):
        parsed = parse_input_name(name)
        if parsed is None:
            continue
        port, k = parsed
        kind = job.input_types.get(name)
        if kind is PortType.MODEL_VERSION:
            bundle = _bundle(ctx, name)
            data, tree = (
                load_data_and_tree(ctx, bundle.spec.data, name + ".data")
                if ctx.has_input(name + ".data")
                else (None, None)
            )
            ref = refs.get(name)
            value: Value = ModelValue(
                bundle=bundle,
                data=data,
                tree=tree,
                ref=str(ref) if ref else None,
                model=f"{bundle.model.manifest.id}@{bundle.model.manifest.version}",
                settings=dict(bundle.model.settings),
            )
        elif kind is PortType.DATASET or kind is None:
            ref = refs.get(name)
            raw = load_dataset(ctx, name)
            if isinstance(raw, xr.DataTree):  # M4: the root node, with the tree kept
                value = DatasetValue.from_tree(raw, ref=str(ref) if ref else None)
            else:
                value = dataset_value(raw, str(ref) if ref else None)
        else:
            raise MapiError(
                f"input {name!r} of type {kind.value} must be inline", code=ErrorCode.SCHEMA_INVALID
            )
        found.setdefault(port, []).append((k, value))
    for name, payload in job.inline_inputs.items():
        parsed = parse_input_name(name)
        if parsed is None or not isinstance(payload, dict):
            raise MapiError(
                f"inline input {name!r} is not a JSON object", code=ErrorCode.SCHEMA_INVALID
            )
        port, k = parsed
        found.setdefault(port, []).append((k, payload))
    inputs = {
        port: [v for _, v in sorted(vals, key=lambda kv: kv[0])] for port, vals in found.items()
    }
    ctx.state(RunnerPhase.EVALUATE, f"running {job.use}")
    outcome = execute_job(
        job,
        inputs,
        registry=ctx.registry,
        log=ctx.log,
        progress=ctx.progress,
        cancel=ctx.cancel,
        tracker=ctx.tracker,
    )
    ctx.state(RunnerPhase.UPLOAD, "writing the node's results")
    sidecar, files = output_files(job, outcome)
    for f in files:
        ctx.write_output(f.name, f.data)
    ctx.write_json(NODE_RESULT_OUTPUT, sidecar.model_dump(mode="json"))
    for w in outcome.warnings_plain[:20]:
        ctx.warning("NODE_WARNING", w)
