"""Data builders for server tests (rows, stored objects, simulated runner outputs)."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runspec import EMPTY_CATALOG_DIGEST
from mapi_server.storage import MemoryObjectStore
from mapi_spec.api import new_id
from mapi_spec.executor import RunKind
from mapi_spec.run import (
    CompletePayload,
    InputDigest,
    ManifestObject,
    OutputManifest,
    RunSpec,
    make_event,
    sign_output_manifest,
)
from mapi_spec.storage import (
    crc32c,
    crc32c_to_base64,
    dataset_prefix,
    md5_base64,
    run_prefix,
)

from .fake_runner import outputs_for

EXEC_TOKEN = "exec-token-for-tests"
CATALOG = Path(__file__).resolve().parent.parent / "fixtures" / "catalog.json"

TREND_SCHEMA: dict[str, Any] = {
    "dims": {"t": {"kind": "time", "policy": "extend", "freq": "MS"}},
    "vars": {"y": {"dims": ["t"], "dtype": "float64", "role": "target"}},
}
TREND_SUMMARY: dict[str, Any] = {
    "vars": ["y"],
    "coords": {
        "t": {
            "kind": "time",
            "size": 48,
            "start": "2020-01-01",
            "end": "2023-12-01",
            "freq": "MS",
            "contiguous": True,
        }
    },
}

TREND_SPEC = """\
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: trend, project: default}
data:
  dataset: dataset://default/co2@latest
  split: {kind: time_holdout, dim: t, last: 4}
pipeline:
  - {id: scale_y, use: core.standardize@^1, with: {vars: [y], over: [t]}}
model:
  use: ts.hsgp_trend@^1
  fit: {seed: 7}
evaluate: {metrics: [rmse]}
"""

LEAKY_SPEC = TREND_SPEC.replace(
    "split: {kind: time_holdout, dim: t, last: 4}",
    "split: {kind: time_holdout, dim: t, last: 4, after_step: scale_y}",
)


async def ready_dataset(
    ctx: ServerContext, name: str = "co2", *, version: int = 1
) -> tuple[m.Dataset, m.DatasetVersion]:
    """A dataset with one ``ready`` version whose ``data.zarr.zip`` exists in the store."""
    data = os.urandom(1024)
    sha = hashlib.sha256(data).hexdigest()
    prefix = dataset_prefix(ctx.scope.tenant_id, sha)
    meta = await ctx.store.put(ctx.buckets.data, prefix + "data.zarr.zip", data)
    async with ctx.db.session() as s:
        ds = m.Dataset(
            id=new_id("ds"),
            tenant_id=ctx.scope.tenant_id,
            project_id=ctx.scope.project_id,
            name=name,
            labels={},
            latest_version=version,
            next_version=version + 1,
        )
        s.add(ds)
        await s.flush()
        dv = m.DatasetVersion(
            dataset_id=ds.id,
            version=version,
            tenant_id=ds.tenant_id,
            project_id=ds.project_id,
            status="ready",
            sha256=f"sha256:{sha}",
            size_bytes=len(data),
            data_schema=TREND_SCHEMA,
            summary=TREND_SUMMARY,
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
            ready_at=datetime.now(UTC),
        )
        s.add(dv)
    return ds, dv


async def trend_spec(ctx: ServerContext, *, yaml: str = TREND_SPEC) -> str:
    """Store a ready dataset and a valid spec revision; returns the spec id."""
    from mapi_server.services.specs import validate_document

    await ready_dataset(ctx)
    async with ctx.db.session() as s:
        v = await validate_document(s, ctx.scope, ctx.catalogs, text=yaml)
        spec = m.Spec(
            id=new_id("spc"),
            tenant_id=ctx.scope.tenant_id,
            project_id=ctx.scope.project_id,
            name="trend",
            kind="ModelSpec",
            latest_revision=1,
        )
        s.add(spec)
        await s.flush()
        s.add(
            m.SpecRevision(
                spec_id=spec.id,
                revision=1,
                tenant_id=spec.tenant_id,
                project_id=spec.project_id,
                kind="ModelSpec",
                hash=v.hash or "",
                yaml=yaml,
                document=v.document,
                valid=v.valid,
                issues=[],
                resolved=v.resolved.model_dump(mode="json") if v.resolved else None,
            )
        )
        return spec.id


async def submit_fit(
    ctx: ServerContext,
    spec_id: str,
    *,
    labels: dict[str, str] | None = None,
    timeout_s: int | None = None,
    checkpointable: bool | None = None,
) -> str:
    """Submit a fit of revision 1 of ``spec_id`` (QUEUED), optionally overriding the row."""
    from mapi_server.services.submit import submit_spec_run
    from mapi_spec._types import Duration

    async with ctx.db.session() as s:
        run = await submit_spec_run(
            s,
            ctx,
            kind=RunKind.FIT,
            spec_id=spec_id,
            revision=1,
            labels=labels,
            timeout=Duration(timeout_s) if timeout_s is not None else None,
        )
        if checkpointable is not None:
            run.checkpointable = checkpointable
        return run.id


async def queued_fit(ctx: ServerContext, *, yaml: str = TREND_SPEC) -> str:
    """Store a ready dataset + a valid spec revision and submit a fit run (QUEUED)."""
    return await submit_fit(ctx, await trend_spec(ctx, yaml=yaml))


async def runspec_from_bootstrap(ctx: ServerContext, nonce: str) -> RunSpec:
    raw = await ctx.store.get(ctx.buckets.bootstrap, nonce)
    return RunSpec.model_validate_json(raw)


async def simulate_outputs(
    ctx: ServerContext,
    run: m.Run,
    attempt: m.RunAttempt,
    *,
    mode: str = "ok",
    extra: dict[str, bytes] | None = None,
    corrupt: str | None = None,
    sign_key: str | None = None,
    replace: dict[str, bytes] | None = None,
    add: dict[str, tuple[bytes, str]] | None = None,
) -> tuple[OutputManifest, CompletePayload]:
    """Write a run's outputs + signed manifest straight into the memory store and record the
    ``complete`` payload on the attempt (what the supervisor and events service would do).
    ``add`` adds outputs (in the manifest) to the fake runner's set for the run kind."""
    store = ctx.store
    assert isinstance(store, MemoryObjectStore)
    prefix = run_prefix(run.tenant_id, run.id, attempt.attempt)
    fake_rs = RunSpec.model_construct(kind=RunKind(run.kind))
    objects: dict[str, ManifestObject] = {}
    for name, (data, ctype) in {**outputs_for(fake_rs, mode), **(add or {})}.items():
        data = (replace or {}).get(name, data)
        await store.put(ctx.buckets.runs, prefix + name, data, content_type=ctype)
        claimed = data + b"!" if name == corrupt else data
        objects[name] = ManifestObject(
            key=prefix + name,
            size=len(claimed),
            crc32c=crc32c_to_base64(crc32c(claimed)),
            md5=md5_base64(claimed),
            content_type=ctype,
        )
    for name, data in (extra or {}).items():
        await store.put(ctx.buckets.runs, prefix + name, data)
    inputs = {
        n: InputDigest(generation=int(r["generation"]), size=1, crc32c="AAAAAA==")
        for n, r in run.inputs.items()
    }
    manifest = OutputManifest(
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        run_id=run.id,
        attempt=attempt.attempt,
        kind=RunKind(run.kind),
        status="partial" if mode == "partial" else "complete",
        prefix=prefix,
        spec_hash=run.spec_hash or "sha256:" + "0" * 64,
        catalog_digest=run.catalog_digest or EMPTY_CATALOG_DIGEST,
        inputs=inputs,
        objects=objects,
        runner_version="fake-0",
        created_at=datetime.now(UTC),
    )
    signed = sign_output_manifest(manifest, sign_key or attempt.hmac_key)
    body = signed.model_dump_json().encode()
    await store.put(
        ctx.buckets.runs, prefix + "manifest.json", body, content_type="application/json"
    )
    complete = CompletePayload(
        status=manifest.status,
        manifest_name=prefix + "manifest.json",
        manifest_crc32c=crc32c_to_base64(crc32c(body)),
        objects=len(objects),
        total_bytes=sum(o.size for o in objects.values()),
    )
    async with ctx.db.session() as s:
        row = await s.get(m.RunAttempt, (run.id, attempt.attempt))
        assert row is not None
        row.complete_payload = complete.model_dump(mode="json")
    return signed, complete


def signed_event(attempt: m.RunAttempt, seq: int, payload: Any, *, key: str | None = None) -> Any:
    return make_event(attempt.run_id, attempt.attempt, seq, payload, key=key or attempt.hmac_key)


def dumps(obj: Any) -> bytes:
    return json.dumps(obj).encode()
