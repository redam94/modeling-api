"""Workflow test helpers: real datasets in the store, and child runs executed in-process.

:func:`drive` advances a workflow run like the orchestrator's loop and plays the runner for
its children: a ``workflow_node`` run executes the real node code
(:func:`mapi_workflows.runtime.sandbox.execute_job`) on the run's stored inputs, a ``fit``
run fits the stored spec with the SDK harness; their outputs are written through
:func:`~.builders.simulate_outputs` and finalized by the real finalizer.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import xarray as xr
from mapi_sdk.bundle import Bundle, BundleManifest
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import Registry
from mapi_sdk.schema import data_summary
from mapi_sdk.wire import read_datatree_zip, read_zarr_zip, write_zarr_zip
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run
from mapi_server.services.workflows import WorkflowOrchestrator
from mapi_spec.api import new_id
from mapi_spec.executor import RunKind
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ModelSpec
from mapi_spec.storage import dataset_prefix
from mapi_spec.workflow import WorkflowNodeJob, WorkflowRunStatus
from mapi_workflows.runtime.data import schema_of, with_schema
from mapi_workflows.runtime.sandbox import (
    dataset_value,
    execute_job,
    output_files,
    parse_input_name,
)
from mapi_workflows.runtime.values import ModelValue, Value
from mapi_workflows.testing import test_registry
from sqlalchemy import select

from .builders import simulate_outputs
from .fake_executor import FakeExecutor

__all__ = ["catalog_file", "drive", "execute_children", "put_dataset"]

_REGISTRY: Registry | None = None


def registry() -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = test_registry()
    return _REGISTRY


def catalog_file(tmp: Path) -> Path:
    """The test registry's catalog (toy models, the statsmodels models, the steps)."""
    path = tmp / "catalog.json"
    path.write_text(registry().catalog().model_dump_json())
    return path


async def put_dataset(
    ctx: ServerContext,
    name: str,
    ds: xr.Dataset,
    *,
    project: str | None = None,
    tenant: str | None = None,
) -> tuple[m.Dataset, m.DatasetVersion]:
    """A ready dataset version holding ``ds`` (a real zarr zip, schema and summary)."""
    import hashlib

    tenant_id = tenant or ctx.scope.tenant_id
    project_id = project or ctx.scope.project_id
    schema = schema_of(ds)
    payload = write_zarr_zip(with_schema(ds, schema))
    assert payload is not None
    sha = hashlib.sha256(payload).hexdigest()
    prefix = dataset_prefix(tenant_id, sha)
    meta = await ctx.store.put(ctx.buckets.data, prefix + "data.zarr.zip", payload)
    async with ctx.db.session() as s:
        row = (
            await s.execute(
                select(m.Dataset).where(
                    m.Dataset.tenant_id == tenant_id,
                    m.Dataset.project_id == project_id,
                    m.Dataset.name == name,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = m.Dataset(
                id=new_id("ds"),
                tenant_id=tenant_id,
                project_id=project_id,
                name=name,
                labels={},
                next_version=1,
            )
            s.add(row)
            await s.flush()
        version = row.next_version
        row.next_version += 1
        row.latest_version = version
        dv = m.DatasetVersion(
            dataset_id=row.id,
            version=version,
            tenant_id=tenant_id,
            project_id=project_id,
            status="ready",
            sha256=f"sha256:{sha}",
            size_bytes=len(payload),
            data_schema=schema.model_dump(mode="json"),
            summary=data_summary(ds, schema).model_dump(mode="json"),
            objects={
                "data.zarr.zip": {
                    "bucket": meta.bucket,
                    "name": meta.name,
                    "generation": meta.generation,
                    "size": meta.size,
                    "crc32c": meta.crc32c,
                    "md5": meta.md5_hash,
                    "content_type": None,
                }
            },
            issues=[],
        )
        s.add(dv)
    return row, dv


async def _bytes(ctx: ServerContext, ref: dict[str, Any]) -> bytes:
    return await ctx.store.get(
        str(ref["bucket"]), str(ref["name"]), generation=int(ref["generation"])
    )


async def _node_outputs(ctx: ServerContext, run: m.Run) -> dict[str, tuple[bytes, str]]:
    job = WorkflowNodeJob.model_validate(run.payload)
    refs = job.params.get("_refs") if isinstance(job.params.get("_refs"), dict) else {}
    assert isinstance(refs, dict)
    found: dict[str, list[tuple[int, Value]]] = {}
    for name, stored in run.inputs.items():
        parsed = parse_input_name(name)
        if parsed is None:
            continue
        port, k = parsed
        raw = await _bytes(ctx, stored)
        if job.input_types.get(name) is not None and job.input_types[name].value == "model_version":
            man_raw = await _bytes(ctx, run.inputs[name + ".manifest"])
            manifest = BundleManifest.model_validate_json(man_raw)
            bundle = Bundle.from_tree(read_datatree_zip(raw), registry(), manifest=manifest)
            data = (
                read_zarr_zip(await _bytes(ctx, run.inputs[name + ".data"]))
                if name + ".data" in run.inputs
                else None
            )
            value: Value = ModelValue(
                bundle=bundle,
                data=data,
                ref=str(refs.get(name)),
                settings=dict(bundle.model.settings),
            )
        else:
            value = dataset_value(read_zarr_zip(raw), str(refs.get(name)))
        found.setdefault(port, []).append((k, value))
    for name, payload in job.inline_inputs.items():
        parsed = parse_input_name(name)
        assert parsed is not None and isinstance(payload, dict)
        found.setdefault(parsed[0], []).append((parsed[1], payload))
    inputs = {p: [v for _, v in sorted(vals, key=lambda kv: kv[0])] for p, vals in found.items()}
    outcome = execute_job(job, inputs, registry=registry())
    sidecar, files = output_files(job, outcome)
    out = {f.name: (f.data, "application/octet-stream") for f in files}
    out["node_result.json"] = (sidecar.model_dump_json().encode(), "application/json")
    return out


async def _fit_outputs(
    ctx: ServerContext, run: m.Run
) -> tuple[dict[str, bytes], dict[str, tuple[bytes, str]]]:
    spec = ModelSpec.model_validate(run.payload)
    data = read_zarr_zip(await _bytes(ctx, run.inputs["dataset"]))
    stored = data.attrs.get("mapi_schema")
    raw_schema = DataSchema.model_validate_json(stored) if isinstance(stored, str) else None
    bundle = fit_spec(spec, data, registry=registry(), raw_schema=raw_schema)
    with tempfile.TemporaryDirectory() as tmp:
        bundle.write(tmp)
        root = Path(tmp)
        replace = {
            "bundle.zarr.zip": (root / "bundle.zarr.zip").read_bytes(),
            "metrics.json": (root / "metrics.json").read_bytes(),
            "diagnostics.json": (root / "diagnostics.json").read_bytes(),
            "plots.json": b"[]",
        }
        add = {"bundle.manifest.json": ((root / "manifest.json").read_bytes(), "application/json")}
    return replace, add


async def execute_children(
    ctx: ServerContext, *, fail: Callable[[m.Run], bool] | None = None
) -> int:
    """Run every queued ``workflow_node``/``fit`` run to completion; returns how many."""
    async with ctx.db.session() as s:
        queued = list(
            (
                await s.execute(
                    select(m.Run.id).where(
                        m.Run.state == RunState.QUEUED.value,
                        m.Run.kind.in_([RunKind.WORKFLOW_NODE.value, RunKind.FIT.value]),
                    )
                )
            ).scalars()
        )
    if not queued:
        return 0
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    done = 0
    for run_id in queued:
        async with ctx.db.session() as s:
            run = await load_run(s, None, run_id, for_update=True)
            if run.state != RunState.PROVISIONING.value:
                continue
            await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
            att = await s.get(m.RunAttempt, (run.id, run.attempt))
            assert att is not None
            s.expunge_all()
        if fail is not None and fail(run):
            async with ctx.db.session() as s:
                row = await load_run(s, None, run_id, for_update=True)
                await apply_transition(s, row, RunTrigger.FAIL_USER, Actor.RECONCILER)
            continue
        if run.kind == RunKind.FIT.value:
            replace, add = await _fit_outputs(ctx, run)
            extra: dict[str, Any] = {"replace": replace, "add": add}
        else:
            extra = {"add": await _node_outputs(ctx, run)}
        async with ctx.db.session() as s:
            row = await load_run(s, None, run_id, for_update=True)
            await apply_transition(s, row, RunTrigger.COMPLETE, Actor.RECONCILER)
        await simulate_outputs(ctx, run, att, **extra)
        await Finalizer(ctx).finalize(run.id, run.attempt)
        done += 1
    return done


async def drive(
    ctx: ServerContext,
    run_id: str,
    *,
    max_rounds: int = 60,
    fail: Callable[[m.Run], bool] | None = None,
) -> WorkflowRunStatus:
    """Advance a workflow run until it is finished (children run in-process)."""
    orch = WorkflowOrchestrator(ctx)
    for _ in range(max_rounds):
        await orch.tick_all()
        async with ctx.db.session() as s:
            wr = await s.get(m.WorkflowRunRow, run_id)
            assert wr is not None
            status = WorkflowRunStatus(wr.status)
        if status.terminal:
            return status
        await execute_children(ctx, fail=fail)
    raise AssertionError(f"workflow run {run_id} did not finish: {status}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)
