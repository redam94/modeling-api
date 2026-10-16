"""Guided mode: business priors to model-space priors (``/v1/guided/prior-translations``, M3).

``POST`` validates the spec and the beliefs statically and queues a sandboxed
``translate_priors`` run (``202`` with the run; ``Idempotency-Key`` replays answer ``200``);
``GET`` returns the run and, once it succeeded, its verified ``priors.json``: model-space
priors to merge at ``apply_at`` and the prior-predictive preview in business units. Plots of
the preview are the run's plots (``GET /v1/runs/{run_id}/plots``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, status
from fastapi.responses import JSONResponse
from mapi_spec.api import ALL_ENDPOINTS, PriorTranslation, PriorTranslationRequest
from mapi_spec.errors import ERROR_INFO, ErrorCode, Problem
from mapi_spec.executor import RunKind
from mapi_spec.run import RunState
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import Ctx, Idempotency, IdempotencyKeyHeader, Session
from mapi_server.api.runs import run_view
from mapi_server.context import ServerContext
from mapi_server.errors import NotFoundError
from mapi_server.services.prior_translations import (
    load_prior_translation_result,
    submit_prior_translation,
)
from mapi_server.services.runs import load_run

__all__ = ["problem_responses", "router"]

router = APIRouter(prefix="/v1/guided/prior-translations", tags=["guided"])

RunId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]


def problem_responses(operation_id: str) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` for the RFC 7807 problems an operation declares in
    :data:`mapi_spec.api.ALL_ENDPOINTS` (grouped by status, codes listed)."""
    decl = next(e for e in ALL_ENDPOINTS if e.operation_id == operation_id)
    by_status: dict[int, list[ErrorCode]] = {}
    for code in decl.errors:
        by_status.setdefault(ERROR_INFO[code].status, []).append(code)
    out: dict[int | str, dict[str, Any]] = {}
    for st, codes in sorted(by_status.items()):
        titles = "; ".join(f"`{c.value}`: {ERROR_INFO[c].title}" for c in codes)
        out[st] = {
            "model": Problem,
            "description": f"Problem document (application/problem+json) - {titles}",
        }
    return out


async def _view(ctx: ServerContext, session: AsyncSession, run_id: str) -> PriorTranslation:
    run = await load_run(session, ctx.scope, run_id)
    if run.kind != RunKind.TRANSLATE_PRIORS.value:
        raise NotFoundError("prior translation", run_id)
    result = await load_prior_translation_result(session, ctx, run)
    return PriorTranslation(
        run=await run_view(session, run),
        done=RunState(run.state).terminal,
        result=result,
    )


@router.post(
    "",
    response_model=PriorTranslation,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_prior_translation",
    summary="Translate business priors (sandboxed translate_priors run)",
    responses={
        200: {"model": PriorTranslation, "description": "Idempotent replay"},
        **problem_responses("create_prior_translation"),
    },
)
async def create_prior_translation(
    ctx: Ctx,
    session: Session,
    request: Request,
    body: PriorTranslationRequest,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Business priors (ROI or share ranges with a credibility, a baseline share) to
    model-space priors for the spec's model, with a prior-predictive preview in business
    units. Static problems (an unknown channel, shares adding up to more than the KPI, a
    model without ``business_priors`` calibration) are refused before any run starts."""
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin("POST /v1/guided/prior-translations", await request.json())
    if replay is not None:
        view = await _view(ctx, session, replay.resource_id)
        return JSONResponse(view.model_dump(mode="json"), status_code=200)
    run = await submit_prior_translation(session, ctx, body)
    await idem.complete("run", run.id, 202)
    view = PriorTranslation(run=await run_view(session, run), done=False)
    return JSONResponse(view.model_dump(mode="json"), status_code=202)


@router.get(
    "/{run_id}",
    response_model=PriorTranslation,
    operation_id="get_prior_translation",
    summary="A prior translation and, once done, its priors and preview",
    responses=problem_responses("get_prior_translation"),
)
async def get_prior_translation(ctx: Ctx, session: Session, run_id: RunId) -> PriorTranslation:
    """``done`` once the run is terminal; ``result`` only when it succeeded (a failed run's
    problem is on ``run.problem``)."""
    return await _view(ctx, session, run_id)
