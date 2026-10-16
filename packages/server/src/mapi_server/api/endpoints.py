"""Endpoints and aliases: promotion gated on the scorecard, history and rollback, scoring,
the input schema with an example payload, static validation and code snippets."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from mapi_spec.api import (
    PREFER_HEADER,
    PREFERENCE_APPLIED_HEADER,
    Page,
    ScoreCallResult,
    parse_prefer_wait,
)
from mapi_spec.audit import AuditAction
from mapi_spec.endpoints import (
    Endpoint,
    EndpointPromote,
    EndpointRevision,
    EndpointRollback,
    EndpointSchema,
    EndpointSchemaSnapshot,
    EndpointScoreCall,
    EndpointValidation,
    example_score_call,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity, ValidationFailed
from mapi_spec.executor import RunKind
from mapi_spec.refs import DatasetRef
from mapi_spec.snippets import SnippetContext, SnippetLanguage, SnippetSet, render_snippets
from mapi_spec.tenancy import Action, ResourceKind
from mapi_spec.xrjson import MAX_XRJSON_BYTES
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Ctx,
    Idempotency,
    IdempotencyKeyHeader,
    Limit,
    Session,
    decode_cursor,
    encode_cursor,
)
from mapi_server.api.models import score_result
from mapi_server.api.problems import responses_for
from mapi_server.api.runs import wait_terminal
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.observability import metrics as obs_metrics
from mapi_server.services.audit import write_audit
from mapi_server.services.endpoints import (
    endpoint_dto,
    endpoint_schema_issues,
    load_endpoint,
    promote,
    revision_dto,
    rollback,
    score_request_for,
    store_inline_inputs,
)
from mapi_server.services.specs import check_score_request, resolve_dataset
from mapi_server.services.submit import fit_dataset_version, submit_model_run

__all__ = ["SCORE_BODY_MAX_BYTES", "SCORE_BODY_PATHS", "router"]

router = APIRouter(prefix="/v1/endpoints", tags=["endpoints"])

Alias = Annotated[
    str,
    Path(
        pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$",
        description="The endpoint alias (a slug)",
        examples=["geo-mmm-prod"],
    ),
]
PreferHeader = Annotated[
    str | None,
    Header(alias=PREFER_HEADER, description="wait=N (N <= 60): wait up to N s for the result"),
]

SCORE_BODY_MAX_BYTES = MAX_XRJSON_BYTES * 2 + 256 * 1024
"""Body cap of score and validate calls (two inline payloads of 5 MB plus the call)."""
SCORE_BODY_PATHS = (r"/v1/endpoints/[^/]+/score", r"/v1/endpoints/[^/]+/validate")


# --------------------------------------------------------------------------------- reads


@router.get(
    "",
    response_model=Page[Endpoint],
    operation_id="list_endpoints",
    summary="Endpoints of the project",
    responses=responses_for("list_endpoints"),
)
async def list_endpoints(
    ctx: Ctx, session: Session, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[Endpoint]:
    """Endpoints of the project, by alias."""
    stmt = select(m.Endpoint).where(
        m.Endpoint.tenant_id == ctx.scope.tenant_id, m.Endpoint.project_id == ctx.scope.project_id
    )
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.Endpoint.alias > str(after))
    rows = list((await session.execute(stmt.order_by(m.Endpoint.alias).limit(limit + 1))).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[Endpoint](
        items=[await endpoint_dto(session, r) for r in rows],
        next_cursor=encode_cursor(rows[-1].alias) if more and rows else None,
    )


@router.get(
    "/{alias}",
    response_model=Endpoint,
    operation_id="get_endpoint",
    summary="An endpoint and the version it serves",
    responses=responses_for("get_endpoint"),
)
async def get_endpoint(ctx: Ctx, session: Session, alias: Alias) -> Endpoint:
    return await endpoint_dto(session, await load_endpoint(session, ctx.scope, alias))


@router.get(
    "/{alias}/history",
    response_model=Page[EndpointRevision],
    operation_id="list_endpoint_history",
    summary="Promotions and rollbacks, newest first",
    responses=responses_for("list_endpoint_history"),
)
async def list_endpoint_history(
    ctx: Ctx,
    session: Session,
    alias: Alias,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[EndpointRevision]:
    row = await load_endpoint(session, ctx.scope, alias)
    stmt = select(m.EndpointRevision).where(m.EndpointRevision.endpoint_id == row.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.EndpointRevision.revision < int(after))
    revs = list(
        (
            await session.execute(
                stmt.order_by(m.EndpointRevision.revision.desc()).limit(limit + 1)
            )
        ).scalars()
    )
    more = len(revs) > limit
    revs = revs[:limit]
    return Page[EndpointRevision](
        items=[revision_dto(r) for r in revs],
        next_cursor=encode_cursor(revs[-1].revision) if more and revs else None,
    )


def _example(snapshot: EndpointSchemaSnapshot) -> dict[str, Any]:
    return example_score_call(snapshot).model_dump(
        mode="json", by_alias=True, exclude_defaults=True
    )


@router.get(
    "/{alias}/schema",
    response_model=EndpointSchema,
    operation_id="get_endpoint_schema",
    summary="Input schema of the current version and an example payload",
    responses=responses_for("get_endpoint_schema"),
)
async def get_endpoint_schema(ctx: Ctx, session: Session, alias: Alias) -> EndpointSchema:
    """What to send: the input contract frozen at promotion and an example call body that
    validates against it."""
    row = await load_endpoint(session, ctx.scope, alias)
    dto = await endpoint_dto(session, row)
    snapshot = dto.schema_snapshot
    return EndpointSchema(
        alias=alias,
        ref=dto.ref,
        model_ref=dto.model_ref,
        revision=dto.revision,
        snapshot=snapshot,
        example=_example(snapshot),
        max_inline_bytes=MAX_XRJSON_BYTES,
    )


@router.get(
    "/{alias}/snippets",
    response_model=SnippetSet,
    operation_id="list_endpoint_snippets",
    summary="Python, TypeScript, curl, R, Apps Script, Atlas and notebook code for the endpoint",
    responses=responses_for("list_endpoint_snippets"),
)
async def list_endpoint_snippets(
    ctx: Ctx,
    session: Session,
    alias: Alias,
    language: Annotated[
        list[SnippetLanguage] | None, Query(description="Only these languages (repeatable)")
    ] = None,
    base_url: Annotated[
        str | None,
        Query(max_length=500, description="The API base URL to embed (default: this server's)"),
    ] = None,
) -> SnippetSet:
    """Ready-to-run code that scores the endpoint with its example payload. The API token is
    read from ``MAPI_TOKEN`` (an Apps Script property): snippets never contain a secret."""
    row = await load_endpoint(session, ctx.scope, alias)
    snapshot = EndpointSchemaSnapshot.model_validate(row.schema_snapshot)
    try:
        sctx = SnippetContext(
            base_url=base_url or ctx.settings.api_url,
            project=f"{ctx.scope.tenant_id}/{ctx.scope.project_id}",
            alias=alias,
            payload=_example(snapshot),
        )
    except ValueError as exc:
        raise MapiError(
            str(exc).splitlines()[0][:2000] if str(exc) else "invalid snippet context",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/query/base_url",
        ) from None
    return render_snippets(sctx, language)


# ----------------------------------------------------------------------------- mutations


@router.put(
    "/{alias}",
    response_model=Endpoint,
    operation_id="promote_endpoint",
    summary="Create or promote (gated on the scorecard; override = admin + reason, audited)",
    responses=responses_for("promote_endpoint"),
)
async def promote_endpoint(
    ctx: Ctx, session: Session, alias: Alias, body: EndpointPromote
) -> Endpoint:
    """Point the alias at a model version (creating the alias if needed).

    The promotion gate decides: a version whose scorecard passes, that was fitted with
    ``evaluate_then_refit`` (unless the endpoint's policy allows ``train_only``) and whose
    input contract does not break the endpoint's callers is promoted. A ``warn`` scorecard
    (or no scorecard) needs ``override=true`` with a ``reason`` from a signed-in project admin
    (``409 OVERRIDE_REQUIRED`` otherwise; API tokens can never override); a ``fail``
    scorecard, an interrupted fit or a ``train_only`` fit is ``409 PROMOTION_BLOCKED``.
    The problem's ``errors`` list the gate's reasons.
    """
    row, _ = await promote(session, ctx, alias, body)
    return await endpoint_dto(session, row)


@router.post(
    "/{alias}/rollback",
    response_model=Endpoint,
    operation_id="rollback_endpoint",
    summary="Point the alias back at an earlier revision's version (audited)",
    responses=responses_for("rollback_endpoint"),
)
async def rollback_endpoint(
    ctx: Ctx, session: Session, alias: Alias, body: EndpointRollback
) -> Endpoint:
    row = await rollback(session, ctx, alias, body)
    return await endpoint_dto(session, row)


@router.delete(
    "/{alias}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_endpoint",
    summary="Delete the alias (model versions stay)",
    responses=responses_for("delete_endpoint"),
)
async def delete_endpoint(ctx: Ctx, session: Session, alias: Alias) -> Response:
    row = await load_endpoint(session, ctx.scope, alias, for_update=True)
    if ctx.request is not None:
        await write_audit(
            ctx.db,
            [
                ctx.request.audit(
                    ctx,
                    AuditAction.ENDPOINT_DELETE,
                    ResourceKind.ENDPOINT,
                    resource_id=row.id,
                    details={
                        "alias": alias,
                        "revision": row.revision,
                        "model_version_id": row.model_version_id,
                    },
                )
            ],
            session=session,
        )
    await session.delete(row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------- scoring


async def _static_checks(
    session: AsyncSession,
    ctx: ServerContext,
    row: m.Endpoint,
    body: EndpointScoreCall,
) -> tuple[list[Issue], Any, Any]:
    """Every static check of a call: the endpoint's input contract, then CoordPlan and
    dataset resolution against the current version. Returns (issues, request, summary)."""
    snapshot = EndpointSchemaSnapshot.model_validate(row.schema_snapshot)
    issues, summary = endpoint_schema_issues(body, snapshot)
    request = score_request_for(body, row.project_id, row.alias)
    mv = await session.get(m.ModelVersion, row.model_version_id)
    if mv is None or mv.status != "ready":
        raise MapiError(
            f"the version endpoint {row.alias!r} serves is no longer available",
            code=ErrorCode.CONFLICT,
        )
    await _check_dataset_refs(session, ctx, mv, body)
    await check_score_request(session, ctx.scope, request, mv, issues, inline=summary)
    return issues, request, summary


async def _check_dataset_refs(
    session: AsyncSession, ctx: ServerContext, mv: m.ModelVersion, body: EndpointScoreCall
) -> None:
    """A caller without dataset read access (a ``score:<alias>`` token) sends inline data or
    names the endpoint's own fit dataset: it may neither score other datasets of the project
    nor learn which exist. The refusal is the same for a missing and a forbidden dataset."""
    if ctx.request is None or ctx.request.allows(Action.READ, ResourceKind.DATASET):
        return
    refs = [r for r in (body.data, body.scenario) if isinstance(r, DatasetRef)]
    refs.extend(body.side_tables.values())
    if not refs:
        return
    try:
        fit = await fit_dataset_version(session, mv)
    except MapiError:
        fit = None
    for ref in refs:
        dv, _ = await resolve_dataset(session, ctx.scope, ref)
        if (
            fit is None
            or dv is None
            or (dv.dataset_id, dv.version)
            != (
                fit.dataset_id,
                fit.version,
            )
        ):
            raise MapiError(
                "this caller cannot read datasets: send the data inline, or name the "
                "dataset the endpoint's model was fitted on",
                code=ErrorCode.FORBIDDEN,
            )


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen: set[tuple[str, str, str]] = set()
    out = []
    for i in issues:
        key = (i.code.value, i.pointer, i.message)
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out


@router.post(
    "/{alias}/validate",
    response_model=EndpointValidation,
    operation_id="validate_endpoint_request",
    summary="Static checks of a score call without running it",
    responses=responses_for("validate_endpoint_request"),
)
async def validate_endpoint_request(
    ctx: Ctx, session: Session, alias: Alias, body: EndpointScoreCall
) -> EndpointValidation:
    """The checks ``…/score`` runs before it submits anything: the inline payload against the
    endpoint's input contract (variables, dims, coordinates), CoordPlan (modes, horizon,
    unseen levels, future covariates) and dataset references. ``200`` even when invalid."""
    row = await load_endpoint(session, ctx.scope, alias)
    issues, request, _ = await _static_checks(session, ctx, row, body)
    issues = _dedupe(issues)
    valid = not any(i.severity is Severity.ERROR for i in issues)
    return EndpointValidation(valid=valid, issues=issues, request=request)


@router.post(
    "/{alias}/score",
    response_model=ScoreCallResult,
    operation_id="score_endpoint",
    summary="Score the current version (Prefer: wait=N -> 200 if done, else 202 with the run)",
    responses=responses_for(
        "score_endpoint",
        extra={202: {"model": ScoreCallResult, "description": "Still running: the run"}},
    ),
)
async def score_endpoint(
    ctx: Ctx,
    request: Request,
    alias: Alias,
    body: EndpointScoreCall,
    prefer: PreferHeader = None,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Score the version the alias serves now, with replayed preprocessing.

    ``data``/``scenario`` are ``dataset://`` references or inline XrJSON (up to 5 MB each).
    ``Prefer: wait=N`` (N <= 60) waits up to N seconds: ``200`` with links to the outputs if
    the run finished, else ``202`` with the run. Repeat the call with the same
    ``Idempotency-Key`` (and ``Prefer: wait``) to collect the result later: tokens scoped to
    ``score:<alias>`` need nothing else. Per-token rate limits and vCPU budgets apply.
    """
    raw = await request.json()
    inline = bool(body.inline_inputs())
    async with ctx.data_session() as session:
        row = await load_endpoint(session, ctx.scope, alias)
        idem = Idempotency(session, ctx.scope, idempotency_key)
        replay = await idem.begin(f"POST {request.url.path}", raw)
        if replay is not None:
            run_id = replay.resource_id
            obs_metrics.record_endpoint_score("replayed", inline=inline)
        else:
            try:
                issues, score_req, summary = await _static_checks(session, ctx, row, body)
                if any(i.severity is Severity.ERROR for i in issues):
                    raise ValidationFailed(_dedupe(issues))
            except MapiError:
                obs_metrics.record_endpoint_score("invalid", inline=inline)
                raise
            inputs = await store_inline_inputs(ctx, ctx.scope, body)
            run = await submit_model_run(
                session,
                ctx,
                kind=RunKind.SCORE,
                model_version_id=row.model_version_id,
                request=score_req,
                resource_class=body.resource_class,
                timeout=body.timeout,
                labels=dict(body.labels),
                extra_inputs=inputs,
                inline_summary=summary,
            )
            session.add(
                m.EndpointRun(
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    project_id=run.project_id,
                    endpoint_id=row.id,
                    alias=row.alias,
                    revision=row.revision,
                )
            )
            run_id = run.id
            await idem.complete("run", run.id, 202)
            obs_metrics.record_endpoint_score("accepted", inline=inline)
    wait = parse_prefer_wait(prefer)
    headers: dict[str, str] = {"Location": f"/v1/runs/{run_id}"}
    if wait:
        await wait_terminal(ctx, run_id, float(wait), disconnected=request.is_disconnected)
        headers[PREFERENCE_APPLIED_HEADER] = f"wait={wait}"
    result = await score_result(ctx, run_id)
    return JSONResponse(
        result.model_dump(mode="json"), status_code=200 if result.done else 202, headers=headers
    )
