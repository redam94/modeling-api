"""Build the bootstrap :class:`~mapi_spec.run.RunSpec` for one attempt (plan §8.1).

The RunSpec is the *only* channel through which an execution receives URLs and its HMAC
key. Inputs are generation-pinned signed GETs, the manifest is a write-once signed PUT and
every other output goes through a *create-only* V4 POST policy on the attempt prefix (it
cannot replace the manifest or an output once written; only the checkpoint policy may
overwrite, since each segment rewrites the same checkpoint names). v0.14: every attempt also
gets a create-only policy on its ``media/`` prefix (one logged image or table per object, at
most ``TrackingLimits.media_item_bytes``). All keys are built server-side from validated ids
(:mod:`mapi_spec.storage` helpers).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from mapi_spec.catalog import Catalog
from mapi_spec.collection import COLLECTION_MANIFEST
from mapi_spec.executor import RunKind
from mapi_spec.jobs import DATATREE_OUTPUT
from mapi_spec.loader import spec_hash
from mapi_spec.resources import ResourceClass
from mapi_spec.run import (
    MANIFEST_OBJECT_NAME,
    OUTPUT_ARTIFACTS,
    OUTPUT_CHECKPOINTS,
    OUTPUT_MANIFEST,
    OUTPUT_MEDIA,
    RunLimits,
    RunSpec,
    catalog_digest,
)
from mapi_spec.storage import (
    MAX_SIGNED_URL_TTL,
    ObjectStore,
    PostPolicy,
    SignedGet,
    SignedPut,
    run_checkpoint_prefix,
    run_media_prefix,
    run_prefix,
)

from mapi_server.db import models as m
from mapi_server.services.runs import now
from mapi_server.storage import list_all

__all__ = [
    "EMPTY_CATALOG_DIGEST",
    "MANIFEST_MAX_BYTES",
    "StoredRef",
    "build_runspec",
    "input_layouts",
    "signed_links_ttl",
]

MANIFEST_MAX_BYTES = 4 * 1024 * 1024
EMPTY_CATALOG_DIGEST = catalog_digest(Catalog())
_URL_MARGIN = timedelta(minutes=30)
_IO_NAME = re.compile(r"[^a-z0-9_.\-]")


def StoredRef(bucket: str, name: str, generation: int) -> dict[str, Any]:
    """The JSON shape of ``runs.inputs`` entries (a generation-pinned object)."""
    return {"bucket": bucket, "name": name, "generation": generation}


def input_layouts(inputs: dict[str, Any]) -> dict[str, Any]:
    """M4: ``{input name: "tree"}`` for every input that is a tree version's
    ``data.tree.zip`` (the runner reads those as an ``xr.DataTree``); v0.15:
    ``"collection"`` for every input that is a collection version's ``manifest.parquet``
    (its ``<name>.info`` and ``<name>.pack.<i>`` inputs come with it)."""
    out: dict[str, Any] = {}
    for name, ref in sorted(inputs.items()):
        if not isinstance(ref, dict):
            continue
        obj = str(ref.get("name", ""))
        if obj.endswith("/" + DATATREE_OUTPUT):
            out[name] = "tree"
        elif obj.endswith("/" + COLLECTION_MANIFEST):
            out[name] = "collection"
    return out


def _ttl(timeout_s: int) -> timedelta:
    return min(timedelta(seconds=timeout_s) + _URL_MARGIN, MAX_SIGNED_URL_TTL)


def signed_links_ttl(timeout_s: int) -> timedelta:
    """How long the signed links (inputs, output and checkpoint upload policies) of an
    attempt of a run with ``timeout_s`` stay valid after its RunSpec is built."""
    return _ttl(timeout_s)


def _io_name(rel: str, i: int) -> str:
    name = _IO_NAME.sub("-", rel.lower()).strip("-.") or f"ckpt-{i:04d}"
    if not name[0].isalpha():
        name = f"c{name}"
    return name[:64]


async def build_runspec(
    store: ObjectStore,
    *,
    run: m.Run,
    attempt: m.RunAttempt,
    runs_bucket: str,
) -> RunSpec:
    ttl = _ttl(run.timeout_s)
    rc = ResourceClass(run.resource_class)
    limits = RunLimits.for_class(rc)
    prefix = run_prefix(run.tenant_id, run.id, attempt.attempt)

    inputs: dict[str, SignedGet] = {}
    for name, ref in sorted(run.inputs.items()):
        inputs[name] = await store.sign_get(
            ref["bucket"], ref["name"], expires_in=ttl, generation=int(ref["generation"])
        )

    outputs: dict[str, SignedPut | PostPolicy] = {
        OUTPUT_MANIFEST: await store.sign_put(
            runs_bucket,
            prefix + MANIFEST_OBJECT_NAME,
            expires_in=ttl,
            content_type="application/json",
            max_bytes=MANIFEST_MAX_BYTES,
            if_generation_match=0,
        ),
        OUTPUT_ARTIFACTS: await store.post_policy(
            runs_bucket,
            prefix,
            expires_in=ttl,
            max_bytes=limits.max_output_bytes,
            create_only=True,
        ),
    }
    checkpoint_prefix: str | None = None
    if run.checkpointable:
        checkpoint_prefix = run_checkpoint_prefix(run.tenant_id, run.id, attempt.attempt)
        outputs[OUTPUT_CHECKPOINTS] = await store.post_policy(
            runs_bucket, checkpoint_prefix, expires_in=ttl, max_bytes=limits.max_output_bytes
        )

    media_prefix = run_media_prefix(run.tenant_id, run.id, attempt.attempt)
    outputs[OUTPUT_MEDIA] = await store.post_policy(
        runs_bucket,
        media_prefix,
        expires_in=ttl,
        max_bytes=limits.tracking.media_item_bytes,
        create_only=True,
    )

    resume: dict[str, SignedGet] = {}
    prev, found = "", []
    if run.checkpointable:
        # the newest earlier attempt that checkpointed: an attempt that died before its
        # first segment (or never started) must not throw away the progress before it
        for earlier in range(attempt.attempt - 1, 0, -1):
            prev = run_checkpoint_prefix(run.tenant_id, run.id, earlier)
            found = await list_all(store, runs_bucket, prev)
            if found:
                break
    if found:
        for i, meta in enumerate(found):
            if len(resume) >= 1024:
                break
            key = _io_name(meta.name[len(prev) :], i)
            while key in resume:
                key = f"ckpt-{i:04d}"
            resume[key] = await store.sign_get(
                runs_bucket, meta.name, expires_in=ttl, generation=meta.generation
            )

    created = now()
    return RunSpec(
        run_id=run.id,
        attempt=attempt.attempt,
        nonce=attempt.nonce,
        kind=RunKind(run.kind),
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        spec=run.payload,
        spec_hash=run.spec_hash or spec_hash(run.payload),
        catalog_digest=run.catalog_digest or EMPTY_CATALOG_DIGEST,
        inputs=inputs,
        outputs=outputs,
        resume=resume,
        checkpoint_prefix=checkpoint_prefix,
        media_prefix=media_prefix,
        hmac_key=attempt.hmac_key,
        limits=limits,
        resource_class=rc,
        deadline=max(attempt.deadline, created + timedelta(seconds=1)),
        created_at=created,
        labels={k: str(v) for k, v in run.labels.items()},
        input_layouts=input_layouts(run.inputs),
    )
