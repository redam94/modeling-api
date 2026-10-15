"""Models: registry, versions (signed manifest, diagnostics, scorecard, lineage), artifact
links, ``ui/`` views, scoring (``Prefer: wait``), evaluate, and plots."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Path, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from mapi_plots.sanitize import sanitize_plot
from mapi_spec.api import (
    PREFER_HEADER,
    PREFERENCE_APPLIED_HEADER,
    ArtifactLink,
    Lineage,
    Model,
    ModelVersion,
    ObjectRef,
    Page,
    PlotListItem,
    PlotValidateResponse,
    Run,
    ScoreCall,
    ScoreCallResult,
    Scorecard,
    parse_prefer_wait,
)
from mapi_spec.capabilities import Capabilities, Diagnostic
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.plots import PlotSpec
from mapi_spec.refs import ModelVersionRef
from mapi_spec.run import RunState
from pydantic import BaseModel, Field
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
from mapi_server.api.runs import render_run, run_view, wait_terminal
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.bundles import (
    artifact_link,
    verified_bundle_object,
    verified_manifest,
)
from mapi_server.services.runs import load_run
from mapi_server.services.submit import submit_model_run

__all__ = ["EvaluateCall", "plots_router", "router"]

router = APIRouter(prefix="/v1/models", tags=["models"])
plots_router = APIRouter(prefix="/v1/plots", tags=["plots"])

ModelId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
VersionNo = Annotated[int, Path(ge=1)]


def model_dto(model: m.Model) -> Model:
    return Model(
        id=model.id,
        tenant_id=model.tenant_id,
        project_id=model.project_id,
        name=model.name,
        ref=f"model://{model.project_id}/{model.name}",
        latest_version=model.latest_version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def version_dto(model: m.Model, mv: m.ModelVersion) -> ModelVersion:
    return ModelVersion(
        id=mv.id,
        model_id=mv.model_id,
        tenant_id=mv.tenant_id,
        project_id=mv.project_id,
        version=mv.version,
        ref=f"model://{model.project_id}/{model.name}@v{mv.version}",
        status=mv.status,
        run_id=mv.run_id,
        spec_hash=mv.spec_hash,
        manifest=mv.manifest,
        capabilities=Capabilities.model_validate(mv.capabilities) if mv.capabilities else None,
        diagnostics=[Diagnostic.model_validate(d) for d in mv.diagnostics],
        scorecard=Scorecard.model_validate(mv.scorecard) if mv.scorecard else None,
        lineage=Lineage.model_validate(mv.lineage),
        metrics=mv.metrics,
        artifacts=[ObjectRef.model_validate(a) for a in mv.artifacts],
        created_at=mv.created_at,
    )


async def _model(ctx: ServerContext, session: AsyncSession, model_id: str) -> m.Model:
    model = await session.get(m.Model, model_id)
    if (
        model is None
        or model.tenant_id != ctx.scope.tenant_id
        or model.project_id != ctx.scope.project_id
    ):
        raise NotFoundError("model", model_id)
    return model


async def _mv(
    ctx: ServerContext, session: AsyncSession, model_id: str, version: int
) -> tuple[m.Model, m.ModelVersion]:
    model = await _model(ctx, session, model_id)
    mv = (
        await session.execute(
            select(m.ModelVersion).where(
                m.ModelVersion.model_id == model.id, m.ModelVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if mv is None:
        raise NotFoundError("model version", f"{model_id}@v{version}")
    return model, mv


@router.get("", response_model=Page[Model], operation_id="list_models")
async def list_models(
    ctx: Ctx, session: Session, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[Model]:
    stmt = select(m.Model).where(
        m.Model.tenant_id == ctx.scope.tenant_id, m.Model.project_id == ctx.scope.project_id
    )
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.Model.id < str(after))
    rows = list(
        (await session.execute(stmt.order_by(m.Model.id.desc()).limit(limit + 1))).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[Model](
        items=[model_dto(x) for x in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.get("/{model_id}", response_model=Model, operation_id="get_model")
async def get_model(ctx: Ctx, session: Session, model_id: ModelId) -> Model:
    return model_dto(await _model(ctx, session, model_id))


@router.get(
    "/{model_id}/versions", response_model=Page[ModelVersion], operation_id="list_model_versions"
)
async def list_model_versions(
    ctx: Ctx,
    session: Session,
    model_id: ModelId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[ModelVersion]:
    model = await _model(ctx, session, model_id)
    stmt = select(m.ModelVersion).where(m.ModelVersion.model_id == model.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.ModelVersion.version < int(after))
    rows = list(
        (
            await session.execute(stmt.order_by(m.ModelVersion.version.desc()).limit(limit + 1))
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[ModelVersion](
        items=[version_dto(model, v) for v in rows],
        next_cursor=encode_cursor(rows[-1].version) if more and rows else None,
    )


@router.get(
    "/{model_id}/versions/{version}", response_model=ModelVersion, operation_id="get_model_version"
)
async def get_model_version(
    ctx: Ctx, session: Session, model_id: ModelId, version: VersionNo
) -> ModelVersion:
    model, mv = await _mv(ctx, session, model_id, version)
    return version_dto(model, mv)


async def _bundle_link(
    ctx: ServerContext, session: AsyncSession, model_id: str, version: int, name: str
) -> ArtifactLink:
    _, mv = await _mv(ctx, session, model_id, version)
    obj = await verified_bundle_object(ctx.store, ctx.signer, mv, name)
    return await artifact_link(
        ctx.store, name=name, bucket=obj.bucket, key=obj.key, generation=obj.generation
    )


@router.get(
    "/{model_id}/versions/{version}/artifacts/{name:path}",
    response_model=ArtifactLink,
    operation_id="get_model_artifact",
)
async def get_model_artifact(
    ctx: Ctx, session: Session, model_id: ModelId, version: VersionNo, name: str
) -> ArtifactLink:
    """Signed URL for a bundle object or ``ui/…`` view, minted only after the signed bundle
    manifest verifies (``INTEGRITY_FAILURE`` otherwise)."""
    return await _bundle_link(ctx, session, model_id, version, name)


@router.get(
    "/{model_id}/versions/{version}/ui/{path:path}",
    response_class=RedirectResponse,
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    operation_id="redirect_model_ui",
    responses={307: {"description": "Redirect to a signed URL of the precomputed view"}},
)
async def redirect_model_ui(
    ctx: Ctx, session: Session, model_id: ModelId, version: VersionNo, path: str
) -> RedirectResponse:
    """Precomputed ``ui/`` views (summaries, thin draws, grids): 307 to a signed URL."""
    link = await _bundle_link(ctx, session, model_id, version, f"ui/{path}")
    return RedirectResponse(link.url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


async def _score_result(ctx: ServerContext, run_id: str) -> ScoreCallResult:
    async with ctx.db.session() as session:
        run = await load_run(session, None, run_id)
        view = await run_view(session, run)
        outputs: list[ArtifactLink] = []
        done = RunState(run.state).terminal
        if run.state == RunState.SUCCEEDED.value:
            att = await session.get(m.RunAttempt, (run.id, run.attempt))
            for name, meta in sorted((att.outputs if att else {}).items()):
                outputs.append(
                    await artifact_link(
                        ctx.store,
                        name=name,
                        bucket=meta["bucket"],
                        key=meta["name"],
                        generation=int(meta["generation"]),
                    )
                )
    return ScoreCallResult(done=done, run=view, outputs=outputs)


@router.post(
    "/{model_id}/versions/{version}:score",
    response_model=ScoreCallResult,
    operation_id="score_model",
    responses={202: {"model": ScoreCallResult, "description": "Still running"}},
)
async def score_model(
    ctx: Ctx,
    request: Request,
    model_id: ModelId,
    version: VersionNo,
    body: ScoreCall,
    prefer: Annotated[str | None, Header(alias=PREFER_HEADER)] = None,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Score with replayed preprocessing. ``Prefer: wait=N`` (≤ 60) waits up to N s:
    200 with output links if the run finished, else 202 with the run."""
    raw = await request.json()
    async with ctx.db.session() as session:
        model, mv = await _mv(ctx, session, model_id, version)
        target = body.request.model
        expected = ModelVersionRef(model.project_id, model.name, f"v{mv.version}")
        if not isinstance(target, ModelVersionRef) or (
            target.project,
            target.name,
            target.version.removeprefix("v"),
        ) not in (
            (expected.project, expected.name, str(mv.version)),
            (expected.project, expected.name, "latest"),
        ):
            raise MapiError(
                f"request.model must reference {expected} (the version in the URL)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/body/request/model",
            )
        idem = Idempotency(session, ctx.scope, idempotency_key)
        replay = await idem.begin(f"POST {request.url.path}", raw)
        if replay is not None:
            run_id = replay.resource_id
        else:
            run = await submit_model_run(
                session,
                ctx,
                kind=RunKind.SCORE,
                model_version_id=mv.id,
                request=body.request,
                resource_class=body.resource_class,
                timeout=body.timeout,
                labels=dict(body.labels),
            )
            run_id = run.id
            await idem.complete("run", run.id, 202)
    wait = parse_prefer_wait(prefer)
    headers: dict[str, str] = {}
    if wait:
        await wait_terminal(ctx, run_id, float(wait), disconnected=request.is_disconnected)
        headers[PREFERENCE_APPLIED_HEADER] = f"wait={wait}"
    result = await _score_result(ctx, run_id)
    return JSONResponse(
        result.model_dump(mode="json"),
        status_code=200 if result.done else 202,
        headers=headers,
    )


class EvaluateCall(BaseModel):
    """Body of ``…:evaluate`` (holdout metrics through the score path)."""

    model_config = {"extra": "forbid"}

    params: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict, max_length=64)


@router.post(
    "/{model_id}/versions/{version}:evaluate",
    response_model=Run,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="evaluate_model",
    responses={
        202: {"model": Run, "description": "The evaluate run"},
        200: {"model": Run, "description": "Idempotent replay"},
    },
)
async def evaluate_model(
    ctx: Ctx,
    session: Session,
    request: Request,
    model_id: ModelId,
    version: VersionNo,
    body: EvaluateCall,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    _, mv = await _mv(ctx, session, model_id, version)
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin(f"POST {request.url.path}", await request.json())
    if replay is not None:
        return await render_run(session, replay.resource_id, 200)
    run = await submit_model_run(
        session,
        ctx,
        kind=RunKind.EVALUATE,
        model_version_id=mv.id,
        params=body.params,
        labels=body.labels,
    )
    await idem.complete("run", run.id, 202)
    return await render_run(session, run.id, 202)


# ----------------------------------------------------------------------------------- plots


@router.get(
    "/{model_id}/versions/{version}/plots",
    response_model=Page[PlotListItem],
    tags=["plots"],
    operation_id="list_plots",
)
async def list_plots(
    ctx: Ctx,
    session: Session,
    model_id: ModelId,
    version: VersionNo,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[PlotListItem]:
    _, mv = await _mv(ctx, session, model_id, version)
    stmt = select(m.Plot).where(m.Plot.model_version_id == mv.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.Plot.plot_id > str(after))
    rows = list((await session.execute(stmt.order_by(m.Plot.plot_id).limit(limit + 1))).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    items = [
        PlotListItem(
            id=p.plot_id,
            title=p.title,
            kind=p.kind,
            persona=p.persona,
            recipe=p.recipe,
            caption_plain=p.caption_plain,
            href=f"/v1/models/{model_id}/versions/{version}/plots/{p.plot_id}",
        )
        for p in rows
    ]
    return Page[PlotListItem](
        items=items, next_cursor=encode_cursor(rows[-1].plot_id) if more and rows else None
    )


@router.get(
    "/{model_id}/versions/{version}/plots/{plot_id}",
    response_model=PlotSpec,
    tags=["plots"],
    operation_id="get_plot",
)
async def get_plot(
    ctx: Ctx,
    session: Session,
    model_id: ModelId,
    version: VersionNo,
    plot_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_.\-]{1,128}$")],
) -> JSONResponse:
    """A sanitized PlotSpec — render it only in the plot-sandbox origin. The manifest is
    verified first, and the stored spec is re-sanitized on the way out."""
    _, mv = await _mv(ctx, session, model_id, version)
    verified_manifest(ctx.signer, mv)
    plot = await session.get(m.Plot, (mv.id, plot_id))
    if plot is None:
        raise NotFoundError("plot", plot_id)
    clean = sanitize_plot(plot.spec).plot
    return JSONResponse(clean.model_dump(mode="json", by_alias=True, exclude_none=True))


@plots_router.post(
    ":validate",
    response_model=PlotValidateResponse,
    operation_id="validate_plot",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PlotSpec"}}},
        }
    },
)
async def validate_plot(
    body: Annotated[dict[str, Any], Body(description="A PlotSpec document")],
) -> PlotValidateResponse:
    """Run the server-side plot sanitizer on a user-authored PlotSpec (200 even when it is
    unsafe: the verdict is ``valid`` + ``issues``)."""
    try:
        result = sanitize_plot(body)
    except MapiError as exc:  # SanitizeError and envelope errors
        return PlotValidateResponse(valid=False, issues=exc.issues or [exc.to_issue()])
    return PlotValidateResponse(valid=True, issues=result.changes, sanitized=result.plot)
