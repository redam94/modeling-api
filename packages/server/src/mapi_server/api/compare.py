"""Run comparison route (v0.14): ``GET /v1/run-compare``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from mapi_spec.api import RunCompare

from mapi_server.api.common import Ctx, Session
from mapi_server.services.compare import compare_runs as compare

__all__ = ["router"]

router = APIRouter(tags=["runs"])


@router.get("/v1/run-compare", response_model=RunCompare, operation_id="compare_runs")
async def compare_runs(
    ctx: Ctx,
    session: Session,
    run: Annotated[list[str], Query(max_length=64)],
    key: Annotated[list[str] | None, Query(max_length=64)] = None,
    media_key: Annotated[list[str] | None, Query(max_length=64)] = None,
    max_points: Annotated[int, Query(ge=10, le=2000)] = 300,
    params: bool = True,
) -> RunCompare:
    """Compare 2 to 10 runs (``run``, repeatable, in the order shown): every metric series
    summary, the histories of up to 12 metric keys (``key``; default the keys most of the
    runs have), the params with the ones that differ (``params=false`` leaves them out) and
    up to 4 media keys (``media_key``) with signed links. A run of another project is 404."""
    return await compare(
        session,
        ctx,
        ctx.scope,
        run,
        keys=key or (),
        media_keys=media_key or (),
        max_points=max_points,
        params=params,
    )
