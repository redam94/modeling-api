"""Explorers and dataset About (M3.5).

* ``GET``/``PATCH /v1/datasets/{dataset_id}/about``: the owner's description plus a summary
  generated from the latest version's schema and coordinates (no data is read);
* ``POST …/versions/{version}/explore`` and ``POST /v1/models/{model_id}/versions/{version}/
  explore``: an ``explore`` run computing Data or Model Explorer sections in the sandbox
  (``202``; ``200`` when the same request was already answered for that target);
* ``POST /v1/models:compare``: several model versions side by side (an explore run on the
  first with the others to compare);
* ``GET /v1/explorations/{run_id}``: the run, and once done the index and signed links to
  every view (``ui/explore/<section>.json`` and its plots).

The cheap Data Explorer sections are also written by every ingest (fetch them with
``GET …/versions/{version}/artifacts/ui/explore/index.json``) and the Model Explorer's fit
sections by every fit (``GET …/models/{model_id}/versions/{version}/ui/explore/index.json``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request, status
from fastapi.responses import JSONResponse
from mapi_spec.about import DatasetAbout, DatasetAboutUpdate, describe_dataset
from mapi_spec.api import Exploration
from mapi_spec.audit import AuditAction
from mapi_spec.coords import DataSummary
from mapi_spec.explore import DataExploreRequest, ModelCompareRequest, ModelExploreRequest
from mapi_spec.run import RunState
from mapi_spec.schema import DataSchema
from mapi_spec.tenancy import ResourceKind
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import Ctx, Idempotency, IdempotencyKeyHeader, Session
from mapi_server.api.models import ModelId, VersionNo, _mv
from mapi_server.api.prior_translations import problem_responses
from mapi_server.api.runs import run_view
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.audit import write_audit
from mapi_server.services.explore import (
    exploration_parts,
    load_exploration,
    resolve_model_ref,
    submit_dataset_explore,
    submit_model_explore,
)
from mapi_server.services.runs import now

__all__ = ["router"]

router = APIRouter(tags=["explorers"])

DatasetId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
RunId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
DatasetVersionNo = Annotated[int, Path(ge=1)]


async def exploration_view(session: AsyncSession, ctx: ServerContext, run: m.Run) -> Exploration:
    target, ref, index, views = await exploration_parts(session, ctx, run)
    return Exploration(
        run=await run_view(session, run),
        done=RunState(run.state).terminal,
        target=target,
        ref=ref,
        index=index,
        views=views,
    )


async def _respond(
    session: AsyncSession, ctx: ServerContext, run: m.Run, cached: bool
) -> JSONResponse:
    view = await exploration_view(session, ctx, run)
    return JSONResponse(view.model_dump(mode="json"), status_code=200 if cached else 202)


def _responses(op: str) -> dict[int | str, dict[str, object]]:
    return {
        200: {"model": Exploration, "description": "Already computed (or an idempotent replay)"},
        **problem_responses(op),
    }


# =========================================================================== datasets


async def _dataset(
    ctx: Ctx, session: AsyncSession, dataset_id: str, *, lock: bool = False
) -> m.Dataset:
    ds = await session.get(m.Dataset, dataset_id, with_for_update=lock)
    if ds is None or (ds.tenant_id, ds.project_id) != (ctx.scope.tenant_id, ctx.scope.project_id):
        raise NotFoundError("dataset", dataset_id)
    return ds


async def _about(session: AsyncSession, ds: m.Dataset) -> DatasetAbout:
    dv = None
    if ds.latest_version is not None:
        dv = await session.get(m.DatasetVersion, (ds.id, ds.latest_version))
    schema = DataSchema.model_validate(dv.data_schema) if dv and dv.data_schema else None
    summary = DataSummary.model_validate(dv.summary) if dv and dv.summary else None
    warnings = [
        str(i.get("message"))[:300]
        for i in (dv.issues if dv else [])
        if isinstance(i, dict) and i.get("severity") in ("warning", "error") and i.get("message")
    ][:20]
    about = describe_dataset(schema, summary, description=ds.description, warnings=warnings)
    return about.model_copy(update={"generated_at": dv.ready_at if dv else None})


@router.get(
    "/v1/datasets/{dataset_id}/about",
    response_model=DatasetAbout,
    operation_id="get_dataset_about",
    summary="The owner's description and the generated summary of the latest version",
    responses=problem_responses("get_dataset_about"),
)
async def get_dataset_about(ctx: Ctx, session: Session, dataset_id: DatasetId) -> DatasetAbout:
    """A plain-language summary from the latest version's schema and coordinates: periods,
    markets, what each measure is."""
    return await _about(session, await _dataset(ctx, session, dataset_id))


@router.patch(
    "/v1/datasets/{dataset_id}/about",
    response_model=DatasetAbout,
    operation_id="update_dataset_about",
    summary="Change the owner's description",
    responses=problem_responses("update_dataset_about"),
)
async def update_dataset_about(
    ctx: Ctx, session: Session, dataset_id: DatasetId, body: DatasetAboutUpdate
) -> DatasetAbout:
    ds = await _dataset(ctx, session, dataset_id, lock=True)
    ds.description = body.description_user
    ds.updated_at = now()
    if ctx.request is not None:
        await write_audit(
            ctx.db,
            [
                ctx.request.audit(
                    ctx, AuditAction.DATASET_ABOUT_UPDATE, ResourceKind.DATASET, resource_id=ds.id
                )
            ],
            session=session,
        )
    await session.flush()
    return await _about(session, ds)


@router.post(
    "/v1/datasets/{dataset_id}/versions/{version}/explore",
    response_model=Exploration,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="explore_dataset_version",
    summary="Compute Data Explorer sections (an explore run)",
    responses=_responses("explore_dataset_version"),
)
async def explore_dataset_version(
    ctx: Ctx,
    session: Session,
    request: Request,
    dataset_id: DatasetId,
    version: DatasetVersionNo,
    body: DataExploreRequest,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """STL, stationarity, cross-correlations, VIF and every other section on demand."""
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin("POST explore dataset", await request.json())
    if replay is not None:
        return await _respond(
            session, ctx, await load_exploration(session, ctx.scope, replay.resource_id), True
        )
    ds = await _dataset(ctx, session, dataset_id)
    dv = await session.get(m.DatasetVersion, (ds.id, version))
    if dv is None:
        raise NotFoundError("dataset version", f"{dataset_id}@v{version}")
    run, cached = await submit_dataset_explore(session, ctx, ds, dv, body)
    await idem.complete("run", run.id, 200 if cached else 202)
    return await _respond(session, ctx, run, cached)


@router.get(
    "/v1/explorations/{run_id}",
    response_model=Exploration,
    operation_id="get_exploration",
    summary="An explore run and, once done, links to its views",
    responses=problem_responses("get_exploration"),
)
async def get_exploration(ctx: Ctx, session: Session, run_id: RunId) -> Exploration:
    """``done`` once the run is terminal; ``index`` and ``views`` only when it succeeded."""
    return await exploration_view(session, ctx, await load_exploration(session, ctx.scope, run_id))


# ============================================================================= models


@router.post(
    "/v1/models/{model_id}/versions/{version}/explore",
    response_model=Exploration,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="explore_model_version",
    summary="Compute Model Explorer sections (an explore run)",
    responses=_responses("explore_model_version"),
)
async def explore_model_version(
    ctx: Ctx,
    session: Session,
    request: Request,
    model_id: ModelId,
    version: VersionNo,
    body: ModelExploreRequest,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Forecasts, backtests (refits on a split plan) and comparisons on demand; the fit
    sections were already written by the fit."""
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin("POST explore model", await request.json())
    if replay is not None:
        return await _respond(
            session, ctx, await load_exploration(session, ctx.scope, replay.resource_id), True
        )
    _model, mv = await _mv(ctx, session, model_id, version)
    run, cached = await submit_model_explore(session, ctx, mv, body)
    await idem.complete("run", run.id, 200 if cached else 202)
    return await _respond(session, ctx, run, cached)


@router.post(
    "/v1/models:compare",
    response_model=Exploration,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="compare_model_versions",
    summary="Compare several model versions side by side",
    responses=_responses("compare_model_versions"),
)
async def compare_model_versions(
    ctx: Ctx,
    session: Session,
    request: Request,
    body: ModelCompareRequest,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Holdout metrics of every version, a ranking per metric and pairwise Diebold-Mariano
    tests on the first version's held-out periods."""
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin("POST /v1/models:compare", await request.json())
    if replay is not None:
        return await _respond(
            session, ctx, await load_exploration(session, ctx.scope, replay.resource_id), True
        )
    first = await resolve_model_ref(session, ctx.scope, body.models[0], "/models/0")
    run, cached = await submit_model_explore(session, ctx, first, body.explore_request())
    await idem.complete("run", run.id, 200 if cached else 202)
    return await _respond(session, ctx, run, cached)
