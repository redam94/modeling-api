"""Budget optimisation (M3.5): submit a sandboxed ``optimize`` run and read its plan.

``POST /v1/models/{model_id}/versions/{version}:optimize`` checks the request statically -
the model version is ready and its model declares the ``decision`` capability (the
mmm-framework MMMs) - pins the verified bundle and queues an ``optimize`` run. The run's
only inputs are the bundle and its manifest; it writes ``optimize.json``, which the
control plane reads as size-capped JSON (never arrays) and validates as an
:class:`~mapi_spec.optimize.OptimizeResult`. Feasibility against the current spend per
channel needs the fitted data, so the sandbox checks it (``OPTIMIZE_INFEASIBLE``).
"""

from __future__ import annotations

from typing import Any

from mapi_spec.capabilities import Capabilities
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.jobs import BUNDLE_MANIFEST_OUTPUT, FIT_BUNDLE, INPUT_BUNDLE, INPUT_BUNDLE_MANIFEST
from mapi_spec.loader import spec_hash
from mapi_spec.optimize import OPTIMIZE_OUTPUT, OptimizeJob, OptimizeRequest, OptimizeResult
from mapi_spec.resources import ResourceClass
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.jsonlimits import JsonTooDeep, loads_limited
from mapi_server.services.bundles import verified_bundle_object
from mapi_server.services.runs import create_run
from mapi_server.services.runspec import StoredRef
from mapi_server.services.submit import _accept

__all__ = [
    "OPTIMIZE_TIMEOUT_S",
    "load_optimize_result",
    "parse_optimize_result",
    "submit_optimize",
]

OPTIMIZE_TIMEOUT_S = 900


def _capabilities(mv: m.ModelVersion) -> Capabilities | None:
    if not mv.capabilities:
        return None
    try:
        return Capabilities.model_validate(mv.capabilities)
    except ValidationError:
        return None


async def submit_optimize(
    session: AsyncSession,
    ctx: ServerContext,
    *,
    model: m.Model,
    mv: m.ModelVersion,
    request: OptimizeRequest,
    resource_class: ResourceClass | None = None,
    labels: dict[str, str] | None = None,
) -> m.Run:
    """Validate and queue an ``optimize`` run against a ready model version."""
    if mv.status != "ready":
        raise Conflict(f"model version {mv.id} is {mv.status}; only ready versions can be used")
    caps = _capabilities(mv)
    if caps is None or not caps.decision:
        raise MapiError(
            "this model does not optimise budgets (it declares no decision capability); "
            "budget optimisation needs a marketing-mix model such as mmm.bayesian",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    ref = f"model://{mv.project_id}/{model.name}@v{mv.version}"
    job = OptimizeJob(request=request, model=ref)
    payload = job.model_dump(mode="json", by_alias=True, exclude_none=True)
    bundle = await verified_bundle_object(ctx.store, ctx.signer, mv, FIT_BUNDLE)
    inputs: dict[str, Any] = {INPUT_BUNDLE: StoredRef(bundle.bucket, bundle.key, bundle.generation)}
    if BUNDLE_MANIFEST_OUTPUT in (mv.manifest.get("objects") or {}):
        man = await verified_bundle_object(ctx.store, ctx.signer, mv, BUNDLE_MANIFEST_OUTPUT)
        inputs[INPUT_BUNDLE_MANIFEST] = StoredRef(man.bucket, man.key, man.generation)
    run = await create_run(
        session,
        ctx.scope,
        kind=RunKind.OPTIMIZE,
        resource_class=resource_class or ResourceClass.S,
        timeout_s=OPTIMIZE_TIMEOUT_S,
        payload=payload,
        inputs=inputs,
        labels=labels,
        spec_hash=spec_hash(payload),
        model_version_id=mv.id,
        catalog_digest=mv.manifest.get("catalog_digest"),
    )
    return await _accept(session, ctx, run, untrusted=bool((mv.lineage or {}).get("overlay_hash")))


def parse_optimize_result(raw: bytes) -> OptimizeResult:
    """Validate ``optimize.json`` bytes (``INTEGRITY_FAILURE`` if they are not a plan)."""
    try:
        return OptimizeResult.model_validate(loads_limited(raw))
    except (JsonTooDeep, ValueError, ValidationError) as exc:
        raise MapiError(
            f"{OPTIMIZE_OUTPUT} is not a valid budget plan",
            code=ErrorCode.INTEGRITY_FAILURE,
            untrusted_text=str(exc)[:8000],
        ) from None


async def load_optimize_result(
    session: AsyncSession, ctx: ServerContext, run: m.Run
) -> OptimizeResult | None:
    """The verified ``optimize.json`` of a succeeded run (``None`` before)."""
    if run.kind != RunKind.OPTIMIZE.value:
        raise NotFoundError("optimization", run.id)
    if run.state != "succeeded":
        return None
    att = await session.get(m.RunAttempt, (run.id, run.attempt))
    meta = (att.outputs or {}).get(OPTIMIZE_OUTPUT) if att is not None else None
    if not isinstance(meta, dict):
        raise MapiError(
            f"the optimize run has no {OPTIMIZE_OUTPUT}", code=ErrorCode.INTEGRITY_FAILURE
        )
    cap = ctx.settings.max_sidecar_bytes
    if int(meta.get("size", 0)) > cap:
        raise MapiError(
            f"{OPTIMIZE_OUTPUT} exceeds the {cap}-byte sidecar limit",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    raw = await ctx.store.get(
        str(meta["bucket"]), str(meta["name"]), generation=int(meta["generation"]), max_bytes=cap
    )
    return parse_optimize_result(raw)
