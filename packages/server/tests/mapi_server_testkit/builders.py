"""Data builders for server tests (rows, stored objects, simulated runner outputs)."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
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
) -> tuple[OutputManifest, CompletePayload]:
    """Write a run's outputs + signed manifest straight into the memory store and record the
    ``complete`` payload on the attempt (what the supervisor and events service would do)."""
    store = ctx.store
    assert isinstance(store, MemoryObjectStore)
    prefix = run_prefix(run.tenant_id, run.id, attempt.attempt)
    fake_rs = RunSpec.model_construct(kind=RunKind(run.kind))
    objects: dict[str, ManifestObject] = {}
    for name, (data, ctype) in outputs_for(fake_rs, mode).items():
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
