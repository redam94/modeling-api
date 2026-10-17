"""Prior translations (M3): submit a sandboxed ``translate_priors`` run and read its result.

Submission validates the spec exactly like a fit (catalog, dataset, data caps), then checks
the business priors statically - cross-field consistency
(:func:`mapi_spec.calibration.check_business_priors`) and channel labels against the dataset
version's coordinate summary - and pins the document. The run's only input is the dataset
(plus side tables); it writes ``priors.json``, which the control plane reads as size-capped
JSON (never arrays) and validates as a :class:`~mapi_spec.calibration.PriorTranslationResult`.
"""

from __future__ import annotations

import copy
from typing import Any

from mapi_spec.api import PriorTranslationRequest
from mapi_spec.calibration import (
    PRIOR_TRANSLATION_OUTPUT,
    BusinessPriors,
    LiftTest,
    PriorTranslationJob,
    PriorTranslationResult,
    check_business_priors,
)
from mapi_spec.coords import DataSummary
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity, ValidationFailed
from mapi_spec.executor import RunKind
from mapi_spec.jobs import INPUT_DATASET, side_table_input
from mapi_spec.loader import spec_hash
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import ModelSpec
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.jsonlimits import JsonTooDeep, loads_limited
from mapi_server.services.calibration import (
    channel_dim,
    check_lift_tests,
    summary_levels,
)
from mapi_server.services.runs import create_run
from mapi_server.services.specs import pin_document, validate_document
from mapi_server.services.submit import _accept, _overlay, data_ref, load_revision

__all__ = [
    "PRIOR_TRANSLATION_TIMEOUT_S",
    "check_channel_labels",
    "load_prior_translation_result",
    "submit_prior_translation",
]

PRIOR_TRANSLATION_TIMEOUT_S = 900


def check_channel_labels(
    priors: BusinessPriors, summary: DataSummary | None, dim: str, pointer: str
) -> list[Issue]:
    """Channels named in ``priors`` that the dataset does not have (``BUSINESS_PRIORS_INVALID``).

    Needs the dataset's categorical levels for ``dim``; without them nothing is reported
    (the sandbox checks again against the data itself)."""
    levels = summary_levels(summary, dim)
    if levels is None:
        return []
    known = {str(v) for v in levels}
    out: list[Issue] = []
    for label in priors.channels:
        if label not in known:
            out.append(
                Issue(
                    code=ErrorCode.BUSINESS_PRIORS_INVALID,
                    pointer=f"{pointer}/channels/{label.replace('~', '~0').replace('/', '~1')}",
                    message=f"the data has no channel {label!r}",
                    suggested_action="Use the channel names exactly as they are in the data.",
                )
            )
    return out


def _lift_issues(
    tests: list[LiftTest], spec: ModelSpec, summary: DataSummary | None
) -> list[Issue]:
    return check_lift_tests(tests, spec, summary, pointer="/lift_tests")


async def submit_prior_translation(
    session: AsyncSession, ctx: ServerContext, body: PriorTranslationRequest
) -> m.Run:
    """Validate and queue a ``translate_priors`` run (``POST /v1/guided/prior-translations``)."""
    scope = ctx.scope
    spec_id: str | None = None
    revision: int | None = None
    if body.spec_id is not None and body.revision is not None:
        _, rev = await load_revision(session, scope, body.spec_id, body.revision)
        document: dict[str, Any] = copy.deepcopy(rev.document)
        spec_id, revision = body.spec_id, body.revision
    else:
        assert body.document is not None
        document = copy.deepcopy(dict(body.document))
    v = await validate_document(
        session,
        scope,
        ctx.catalogs,
        document=document,
        dataset_override=body.dataset,
        resource_class=body.resource_class,
    )
    if not isinstance(v.typed, ModelSpec):
        raise MapiError(
            "prior translations need a ModelSpec", code=ErrorCode.SCHEMA_INVALID, pointer="/kind"
        )
    if not v.valid or v.resolved is None or v.dataset is None:
        raise ValidationFailed(v.issues)
    caps = v.resolved.model.capabilities
    if "business_priors" not in caps.calibration:
        raise MapiError(
            f"model {v.resolved.model.resolved!r} does not use business priors",
            code=ErrorCode.CALIBRATION_UNSUPPORTED,
            pointer="/business_priors",
        )
    summary = DataSummary.model_validate(v.dataset.summary) if v.dataset.summary else None
    issues = check_business_priors(body.business_priors, "/business_priors")
    issues += check_channel_labels(
        body.business_priors, summary, channel_dim(v.typed), "/business_priors"
    )
    issues += _lift_issues(list(body.lift_tests), v.typed, summary)
    if any(i.severity is Severity.ERROR for i in issues):
        raise ValidationFailed(issues)
    pinned = pin_document(document, v)
    job = PriorTranslationJob(
        spec=pinned,
        business_priors=body.business_priors,
        lift_tests=list(body.lift_tests),
        draws=body.draws,
        seed=body.seed,
    )
    payload = job.model_dump(mode="json", by_alias=True, exclude_none=True)
    inputs = {INPUT_DATASET: data_ref(v.dataset)}
    for name, tv in v.side_tables.items():
        inputs[side_table_input(name)] = data_ref(tv)
    run = await create_run(
        session,
        scope,
        kind=RunKind.TRANSLATE_PRIORS,
        resource_class=body.resource_class or ResourceClass.S,
        timeout_s=PRIOR_TRANSLATION_TIMEOUT_S,
        payload=payload,
        inputs=inputs,
        spec_id=spec_id,
        spec_revision=revision,
        spec_hash=spec_hash(payload),
        dataset_id=v.dataset.dataset_id,
        dataset_version=v.dataset.version,
        catalog_digest=v.catalog.digest if v.catalog else None,
    )
    return await _accept(
        session, ctx, run, untrusted=_overlay(v.catalog, v.resolved.model.resolved)
    )


async def load_prior_translation_result(
    session: AsyncSession, ctx: ServerContext, run: m.Run
) -> PriorTranslationResult | None:
    """The verified ``priors.json`` of a succeeded translation (``None`` before)."""
    if run.kind != RunKind.TRANSLATE_PRIORS.value:
        raise NotFoundError("prior translation", run.id)
    if run.state != "succeeded":
        return None
    att = await session.get(m.RunAttempt, (run.id, run.attempt))
    meta = (att.outputs or {}).get(PRIOR_TRANSLATION_OUTPUT) if att is not None else None
    if not isinstance(meta, dict):
        raise MapiError(
            f"the translation run has no {PRIOR_TRANSLATION_OUTPUT}",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    cap = ctx.settings.max_sidecar_bytes
    if int(meta.get("size", 0)) > cap:
        raise MapiError(
            f"{PRIOR_TRANSLATION_OUTPUT} exceeds the {cap}-byte sidecar limit",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    raw = await ctx.store.get(
        str(meta["bucket"]), str(meta["name"]), generation=int(meta["generation"]), max_bytes=cap
    )
    return parse_prior_translation(raw)


def parse_prior_translation(raw: bytes) -> PriorTranslationResult:
    """Validate ``priors.json`` bytes (``INTEGRITY_FAILURE`` if they are not a result)."""
    try:
        return PriorTranslationResult.model_validate(loads_limited(raw))
    except (JsonTooDeep, ValueError, ValidationError) as exc:
        raise MapiError(
            f"{PRIOR_TRANSLATION_OUTPUT} is not a valid prior translation",
            code=ErrorCode.INTEGRITY_FAILURE,
            untrusted_text=str(exc)[:8000],
        ) from None
