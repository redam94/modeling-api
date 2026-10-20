"""Explorers (M3.5): queue ``explore`` runs for datasets and model versions, compare model
versions, and read an exploration's index and view links.

The control plane never decodes the views: it pins the inputs, starts the sandboxed run,
reads ``ui/explore/index.json`` as size-capped JSON (validated as an
:class:`~mapi_spec.views.ExplorerIndex`) and mints signed links to the view objects the run
wrote. Asking again with the same request is answered from the last successful run of the
same target (``cached``), so explorers are instant once computed.
"""

from __future__ import annotations

from typing import Any, Final

from mapi_spec.api import ArtifactLink
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.explore import DataExploreRequest, ExploreJob, ModelExploreRequest
from mapi_spec.jobs import (
    BUNDLE_MANIFEST_OUTPUT,
    FIT_BUNDLE,
    INPUT_BUNDLE,
    INPUT_BUNDLE_MANIFEST,
    INPUT_COMPARE_PREFIX,
    INPUT_DATASET,
    INPUT_EVALUATION,
    INPUT_EVALUATION_MANIFEST,
    INPUT_METRICS_SUFFIX,
)
from mapi_spec.loader import spec_hash
from mapi_spec.refs import ModelVersionRef
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from mapi_spec.views import EXPLORE_INDEX, EXPLORE_PREFIX, ExplorerIndex
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.jsonlimits import JsonTooDeep, loads_limited
from mapi_server.services.bundles import artifact_link, verified_bundle_object
from mapi_server.services.governance import EVALUATION_BUNDLE, EVALUATION_MANIFEST
from mapi_server.services.runs import Scope, create_run
from mapi_server.services.runspec import StoredRef

__all__ = [
    "EXPLORER_VERSION",
    "EXPLORE_TIMEOUT_S",
    "INDEX_MAX_BYTES",
    "exploration_parts",
    "load_exploration",
    "resolve_model_ref",
    "submit_dataset_explore",
    "submit_model_explore",
]

EXPLORE_TIMEOUT_S: Final = 1800
EXPLORER_VERSION: Final = "0.10-charts.1"
"""The version of the explorer views, hashed with each request: an explore run from before
a change of the views (v0.10: the sections for scientific data; the comparison with the
fits' metrics and its elpd plot) is not reused. Change it when the runner changes what a
view means."""
INDEX_MAX_BYTES: Final = 256 * 1024
MAX_VIEW_LINKS: Final = 200
FIT_METRICS: Final = "metrics.json"
"""The fit sidecar pinned next to each bundle input (``<input>.metrics``)."""


def _explore_hash(payload: dict[str, Any]) -> str:
    """The cache key of an explore request: the job and :data:`EXPLORER_VERSION`."""
    return spec_hash({**payload, "explorer_version": EXPLORER_VERSION})


async def _cached(
    session: AsyncSession,
    scope: Scope,
    payload_hash: str,
    *,
    dataset: tuple[str, int] | None,
    model_version_id: str | None,
) -> m.Run | None:
    stmt = select(m.Run).where(
        m.Run.tenant_id == scope.tenant_id,
        m.Run.project_id == scope.project_id,
        m.Run.kind == RunKind.EXPLORE.value,
        m.Run.spec_hash == payload_hash,
        m.Run.state.in_([RunState.SUCCEEDED.value, *(s.value for s in RunState if not s.terminal)]),
    )
    if dataset is not None:
        stmt = stmt.where(m.Run.dataset_id == dataset[0], m.Run.dataset_version == dataset[1])
    if model_version_id is not None:
        stmt = stmt.where(m.Run.model_version_id == model_version_id)
    stmt = stmt.order_by(m.Run.created_at.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


def _check_explore_node(dv: m.DatasetVersion, node: str | None) -> None:
    """M4: the Data Explorer's ``node`` must be a node of the version (a plain dataset has
    only the root) - checked before a run is spent on it."""
    from mapi_spec.tree import ROOT

    from mapi_server.services.specs import tree_schema_of

    if node is None:
        return
    tree = tree_schema_of(dv)
    if tree is None and node != ROOT:
        raise MapiError(
            f"dataset version v{dv.version} is not a tree: it has no node {node!r}",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/node",
        )
    if tree is not None and node not in tree.nodes:
        raise MapiError(
            f"dataset version v{dv.version} has no node {node!r} "
            f"(nodes: {', '.join(list(tree.nodes)[:20])})",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/node",
        )


async def submit_dataset_explore(
    session: AsyncSession,
    ctx: ServerContext,
    ds: m.Dataset,
    dv: m.DatasetVersion,
    body: DataExploreRequest,
) -> tuple[m.Run, bool]:
    """``(run, cached)``: an ``explore`` run of a ready dataset version (or the last one
    that answered the same request)."""
    from mapi_server.services.submit import _accept, data_ref

    if dv.status != "ready":
        raise MapiError(
            f"dataset version v{dv.version} is {dv.status}; only ready versions can be explored",
            code=ErrorCode.CONFLICT,
        )
    _check_explore_node(dv, body.node)
    ref = f"dataset://{ds.project_id}/{ds.name}@v{dv.version}"
    job = ExploreJob(target="dataset", ref=ref, data=body)
    payload = job.model_dump(mode="json", exclude_none=True)
    digest = _explore_hash(payload)
    scope = ctx.scope
    hit = await _cached(session, scope, digest, dataset=(ds.id, dv.version), model_version_id=None)
    if hit is not None:
        return hit, True
    run = await create_run(
        session,
        scope,
        kind=RunKind.EXPLORE,
        resource_class=ResourceClass.S,
        timeout_s=EXPLORE_TIMEOUT_S,
        payload=payload,
        inputs={INPUT_DATASET: data_ref(dv)},
        spec_hash=digest,
        dataset_id=ds.id,
        dataset_version=dv.version,
    )
    return await _accept(session, ctx, run), False


async def resolve_model_ref(
    session: AsyncSession, scope: Scope, ref: str, pointer: str
) -> m.ModelVersion:
    """The model version a ``model://project/name@vN`` ref names in ``scope`` (404 else)."""
    from mapi_server.services.specs import resolve_model_version

    try:
        parsed = ModelVersionRef.parse(ref)
    except ValueError as exc:
        raise MapiError(
            f"{ref!r} is not a model reference",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=pointer,
            untrusted_text=str(exc)[:2000],
        ) from None
    mv, why = await resolve_model_version(session, scope, parsed)
    if mv is None:
        raise NotFoundError("model version", f"{ref} ({why})")
    return mv


async def _model_ref(session: AsyncSession, mv: m.ModelVersion) -> str:
    model = await session.get(m.Model, mv.model_id)
    return f"model://{mv.project_id}/{model.name if model else mv.model_id}@v{mv.version}"


async def submit_model_explore(
    session: AsyncSession,
    ctx: ServerContext,
    mv: m.ModelVersion,
    body: ModelExploreRequest,
) -> tuple[m.Run, bool]:
    """``(run, cached)``: an ``explore`` run of a model version with its fit data, its
    evaluation bundle (when the fit kept one) and the bundles it is compared with."""
    from mapi_server.services.submit import _accept, data_ref, fit_dataset_version

    scope = ctx.scope
    if mv.status != "ready":
        raise MapiError(
            f"model version v{mv.version} is {mv.status}; only ready versions can be explored",
            code=ErrorCode.CONFLICT,
        )
    ref = await _model_ref(session, mv)
    job = ExploreJob(target="model", ref=ref, model=body)
    payload = job.model_dump(mode="json", exclude_none=True)
    digest = _explore_hash(payload)
    hit = await _cached(session, scope, digest, dataset=None, model_version_id=mv.id)
    if hit is not None:
        return hit, True
    dv = await fit_dataset_version(session, mv)
    inputs: dict[str, Any] = {INPUT_DATASET: data_ref(dv)}
    objects = (mv.manifest or {}).get("objects") or {}

    async def pin(target: m.ModelVersion, name: str, manifest_name: str) -> None:
        bundle = await verified_bundle_object(ctx.store, ctx.signer, target, FIT_BUNDLE)
        inputs[name] = StoredRef(bundle.bucket, bundle.key, bundle.generation)
        if BUNDLE_MANIFEST_OUTPUT in ((target.manifest or {}).get("objects") or {}):
            sdk = await verified_bundle_object(
                ctx.store, ctx.signer, target, BUNDLE_MANIFEST_OUTPUT
            )
            inputs[manifest_name] = StoredRef(sdk.bucket, sdk.key, sdk.generation)
        if FIT_METRICS in ((target.manifest or {}).get("objects") or {}):
            # the holdout metrics and elpd estimates are a sidecar, not in the archive
            mt = await verified_bundle_object(ctx.store, ctx.signer, target, FIT_METRICS)
            inputs[f"{name}{INPUT_METRICS_SUFFIX}"] = StoredRef(mt.bucket, mt.key, mt.generation)

    await pin(mv, INPUT_BUNDLE, INPUT_BUNDLE_MANIFEST)
    if EVALUATION_BUNDLE in objects and EVALUATION_MANIFEST in objects:
        ev = await verified_bundle_object(ctx.store, ctx.signer, mv, EVALUATION_BUNDLE)
        evm = await verified_bundle_object(ctx.store, ctx.signer, mv, EVALUATION_MANIFEST)
        inputs[INPUT_EVALUATION] = StoredRef(ev.bucket, ev.key, ev.generation)
        inputs[INPUT_EVALUATION_MANIFEST] = StoredRef(evm.bucket, evm.key, evm.generation)
    for k, other_ref in enumerate(body.compare_with):
        other = await resolve_model_ref(session, scope, other_ref, f"/compare_with/{k}")
        if other.status != "ready":
            raise MapiError(
                f"{other_ref} is {other.status}; only ready versions compare",
                code=ErrorCode.EXPLORE_INVALID,
                pointer=f"/compare_with/{k}",
            )
        name = f"{INPUT_COMPARE_PREFIX}{k}"
        await pin(other, name, f"{name}.manifest")
    run = await create_run(
        session,
        scope,
        kind=RunKind.EXPLORE,
        resource_class=ResourceClass.S,
        timeout_s=EXPLORE_TIMEOUT_S,
        payload=payload,
        inputs=inputs,
        spec_hash=digest,
        model_version_id=mv.id,
        dataset_id=dv.dataset_id,
        dataset_version=dv.version,
        catalog_digest=(mv.manifest or {}).get("catalog_digest"),
    )
    return await _accept(
        session, ctx, run, untrusted=bool((mv.lineage or {}).get("overlay_hash"))
    ), False


async def load_exploration(session: AsyncSession, scope: Scope, run_id: str) -> m.Run:
    from mapi_server.services.runs import load_run

    run = await load_run(session, scope, run_id)
    if run.kind != RunKind.EXPLORE.value:
        raise NotFoundError("exploration", run_id)
    return run


async def exploration_parts(
    session: AsyncSession, ctx: ServerContext, run: m.Run
) -> tuple[str, str, ExplorerIndex | None, list[ArtifactLink]]:
    """``(target, ref, index, view links)`` of an exploration; the index and links only
    once it succeeded."""
    try:
        job = ExploreJob.model_validate(run.payload)
        target, ref = job.target, job.ref
    except ValidationError:  # pragma: no cover - the server wrote the payload
        target, ref = "dataset", "dataset://unknown/unknown@latest"
    index: ExplorerIndex | None = None
    views: list[ArtifactLink] = []
    if run.state != RunState.SUCCEEDED.value:
        return target, ref, index, views
    att = await session.get(m.RunAttempt, (run.id, run.attempt))
    outputs = (att.outputs if att is not None else {}) or {}
    meta = outputs.get(EXPLORE_INDEX)
    if isinstance(meta, dict) and int(meta.get("size") or 0) <= INDEX_MAX_BYTES:
        try:
            raw = await ctx.store.get(
                str(meta["bucket"]),
                str(meta["name"]),
                generation=int(meta["generation"]),
                max_bytes=INDEX_MAX_BYTES,
            )
            index = ExplorerIndex.model_validate(loads_limited(raw))
        except (JsonTooDeep, ValueError, ValidationError, KeyError):
            index = None
    for name, obj in sorted(outputs.items()):
        if not name.startswith(EXPLORE_PREFIX) or not isinstance(obj, dict):
            continue
        if len(views) >= MAX_VIEW_LINKS:
            break
        views.append(
            await artifact_link(
                ctx.store,
                name=name,
                bucket=str(obj["bucket"]),
                key=str(obj["name"]),
                generation=int(obj["generation"]),
            )
        )
    return target, ref, index, views
