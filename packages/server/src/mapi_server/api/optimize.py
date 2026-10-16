"""Budget optimisation (M3.5): ``POST /v1/models/{model_id}/versions/{version}:optimize`` and
``GET /v1/optimizations/{run_id}``.

``POST`` checks the request statically (a ready model version whose model declares the
``decision`` capability), pins the verified bundle and queues a sandboxed ``optimize`` run;
``Prefer: wait=N`` (at most 60 s) waits for it: ``200`` with the plan when it finished,
else ``202`` with the run (poll ``GET /v1/optimizations/{run_id}``). An
``Idempotency-Key`` replays the same run. The plan (``optimize.json``) is read as
size-capped JSON and validated; its plot is the run's ``mmm.budget_plans``
(``GET /v1/runs/{run_id}/plots``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Request
from fastapi.responses import JSONResponse
from mapi_spec.api import (
    PREFER_HEADER,
    PREFERENCE_APPLIED_HEADER,
    Optimization,
    parse_prefer_wait,
)
from mapi_spec.executor import RunKind
from mapi_spec.optimize import OptimizeJob, OptimizeRequest
from mapi_spec.run import RunState
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import Ctx, Idempotency, IdempotencyKeyHeader, Session
from mapi_server.api.models import ModelId, VersionNo, _mv
from mapi_server.api.prior_translations import problem_responses
from mapi_server.api.runs import run_view, wait_terminal
from mapi_server.context import ServerContext
from mapi_server.errors import NotFoundError
from mapi_server.services.optimize import load_optimize_result, submit_optimize
from mapi_server.services.runs import load_run

__all__ = ["optimizations_router", "router"]

router = APIRouter(prefix="/v1/models", tags=["models"])
optimizations_router = APIRouter(prefix="/v1/optimizations", tags=["models"])

RunId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]


async def _view(ctx: ServerContext, session: AsyncSession, run_id: str) -> Optimization:
    run = await load_run(session, ctx.scope, run_id)
    if run.kind != RunKind.OPTIMIZE.value:
        raise NotFoundError("optimization", run_id)
    job = OptimizeJob.model_validate(run.payload)
    return Optimization(
        run=await run_view(session, run),
        done=RunState(run.state).terminal,
        request=job.request,
        result=await load_optimize_result(session, ctx, run),
    )


@router.post(
    "/{model_id}/versions/{version}:optimize",
    response_model=Optimization,
    operation_id="optimize_model_version",
    summary="Optimise a budget across channels (Prefer: wait=N -> 200 if done, else 202)",
    responses={
        202: {"model": Optimization, "description": "Still running (poll the optimization)"},
        **problem_responses("optimize_model_version"),
    },
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        f"example{i}": {"value": ex}
                        for i, ex in enumerate(
                            OptimizeRequest.model_json_schema().get("examples", [])
                        )
                    }
                }
            }
        }
    },
)
async def optimize_model_version(
    ctx: Ctx,
    request: Request,
    model_id: ModelId,
    version: VersionNo,
    body: OptimizeRequest,
    prefer: Annotated[str | None, Header(alias=PREFER_HEADER)] = None,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """How should the budget be split across channels (and geos)? The plan comes from the
    model's posterior: the allocation with its expected KPI and interval, the uplift over the
    current plan and the chance it is positive, the expected regret of committing to one
    plan, the marginal return of every channel and, on request, the efficient frontier.
    Static problems (a model that cannot optimise, a version that is not ready) are refused
    before any run starts; bounds the data cannot meet fail the run with
    ``OPTIMIZE_INFEASIBLE``."""
    raw = await request.json()
    async with ctx.data_session() as session:
        model, mv = await _mv(ctx, session, model_id, version)
        idem = Idempotency(session, ctx.scope, idempotency_key)
        replay = await idem.begin(f"POST {request.url.path}", raw)
        if replay is not None:
            run_id = replay.resource_id
        else:
            run = await submit_optimize(session, ctx, model=model, mv=mv, request=body)
            run_id = run.id
            await idem.complete("run", run.id, 202)
    wait = parse_prefer_wait(prefer)
    headers: dict[str, str] = {}
    if wait:
        await wait_terminal(ctx, run_id, float(wait), disconnected=request.is_disconnected)
        headers[PREFERENCE_APPLIED_HEADER] = f"wait={wait}"
    async with ctx.data_session() as session:
        view = await _view(ctx, session, run_id)
    return JSONResponse(
        view.model_dump(mode="json"), status_code=200 if view.done else 202, headers=headers
    )


@optimizations_router.get(
    "/{run_id}",
    response_model=Optimization,
    operation_id="get_optimization",
    summary="An optimize run and, once done, its plan",
    responses=problem_responses("get_optimization"),
)
async def get_optimization(ctx: Ctx, session: Session, run_id: RunId) -> Optimization:
    """``done`` once the run is terminal; ``result`` only when it succeeded (a failed run's
    problem, e.g. ``OPTIMIZE_INFEASIBLE``, is on ``run.problem``)."""
    return await _view(ctx, session, run_id)
