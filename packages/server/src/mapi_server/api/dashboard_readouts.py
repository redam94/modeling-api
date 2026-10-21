"""Dashboard inputs, readouts, refresh and refit requests (v0.16).

Stub registered by WP7 so that every route of :data:`mapi_spec.api.V016_ENDPOINTS` is served
(``check_route_access``, the OpenAPI document); the inputs and readouts work package (WP8)
replaces the bodies. Until then every route answers ``501``.
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, HTTPException, Path
from mapi_spec.api import Page
from mapi_spec.dashboards import (
    ActualsSubmit,
    ActualsTemplate,
    DashboardGrid,
    DashboardRefitRequest,
    Readout,
    ReadoutHeader,
    ReadoutList,
    RefitRequestDecision,
    ScenarioSave,
    WhatIfRequest,
    WhatIfResult,
)

from mapi_server.api.dashboards import DashboardId
from mapi_server.api.problems import responses_for

__all__ = ["router"]

router = APIRouter(prefix="/v1/dashboards", tags=["dashboards"])

Seq = Annotated[int, Path(ge=1)]
RequestId = Annotated[str, Path(pattern=r"^drr_[0-9a-z]{26}$")]


def _op(operation_id: str, **extra: Any) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id), **extra}


def _wp8() -> NoReturn:
    raise HTTPException(status_code=501, detail="not available yet")


@router.get("/{dashboard_id}/grid", response_model=DashboardGrid, **_op("get_dashboard_grid"))
async def get_dashboard_grid(dashboard_id: DashboardId) -> DashboardGrid:
    """The response grid of the pinned version, clipped to the dashboard's channels."""
    _wp8()


@router.post(
    "/{dashboard_id}/whatif:evaluate",
    response_model=WhatIfResult,
    **_op("evaluate_dashboard_whatif"),
)
async def evaluate_dashboard_whatif(dashboard_id: DashboardId, body: WhatIfRequest) -> WhatIfResult:
    """Evaluate a spend plan on the grid (no run, nothing saved)."""
    _wp8()


@router.post(
    "/{dashboard_id}/scenarios",
    response_model=Readout | ReadoutHeader,
    **_op("save_dashboard_scenario"),
)
async def save_dashboard_scenario(
    dashboard_id: DashboardId, body: ScenarioSave
) -> Readout | ReadoutHeader:
    """Save a scenario as a private readout."""
    _wp8()


@router.get(
    "/{dashboard_id}/actuals/template",
    response_model=ActualsTemplate,
    **_op("get_dashboard_actuals_template"),
)
async def get_dashboard_actuals_template(dashboard_id: DashboardId) -> ActualsTemplate:
    """The table an actuals upload fills in."""
    _wp8()


@router.post(
    "/{dashboard_id}/actuals",
    response_model=ReadoutHeader,
    status_code=202,
    **_op("submit_dashboard_actuals"),
)
async def submit_dashboard_actuals(dashboard_id: DashboardId, body: ActualsSubmit) -> ReadoutHeader:
    """Send new actuals."""
    _wp8()


@router.get(
    "/{dashboard_id}/readouts", response_model=ReadoutList, **_op("list_dashboard_readouts")
)
async def list_dashboard_readouts(dashboard_id: DashboardId) -> ReadoutList:
    """Readouts newest first."""
    _wp8()


@router.get(
    "/{dashboard_id}/readouts/{seq}", response_model=Readout, **_op("get_dashboard_readout")
)
async def get_dashboard_readout(dashboard_id: DashboardId, seq: Seq) -> Readout:
    """One readout with its snapshot."""
    _wp8()


@router.post(
    "/{dashboard_id}/readouts/{seq}:publish",
    response_model=ReadoutHeader,
    **_op("publish_dashboard_readout"),
)
async def publish_dashboard_readout(dashboard_id: DashboardId, seq: Seq) -> ReadoutHeader:
    """Publish a readout that waits for review."""
    _wp8()


@router.post(
    "/{dashboard_id}/readouts/{seq}:hide",
    response_model=ReadoutHeader,
    **_op("hide_dashboard_readout"),
)
async def hide_dashboard_readout(dashboard_id: DashboardId, seq: Seq) -> ReadoutHeader:
    """Hide a readout from clients."""
    _wp8()


@router.post(
    "/{dashboard_id}:refresh",
    response_model=ReadoutHeader,
    status_code=202,
    **_op("refresh_dashboard"),
)
async def refresh_dashboard(dashboard_id: DashboardId) -> ReadoutHeader:
    """Queue a refresh readout of the published revision."""
    _wp8()


@router.get(
    "/{dashboard_id}/refit-requests",
    response_model=Page[DashboardRefitRequest],
    **_op("list_dashboard_refit_requests"),
)
async def list_dashboard_refit_requests(dashboard_id: DashboardId) -> Page[DashboardRefitRequest]:
    """Refit requests, newest first."""
    _wp8()


@router.post(
    "/{dashboard_id}/refit-requests/{request_id}:approve",
    response_model=DashboardRefitRequest,
    **_op("approve_dashboard_refit_request"),
)
async def approve_dashboard_refit_request(
    dashboard_id: DashboardId, request_id: RequestId, body: RefitRequestDecision
) -> DashboardRefitRequest:
    """Approve a refit request."""
    _wp8()


@router.post(
    "/{dashboard_id}/refit-requests/{request_id}:reject",
    response_model=DashboardRefitRequest,
    **_op("reject_dashboard_refit_request"),
)
async def reject_dashboard_refit_request(
    dashboard_id: DashboardId, request_id: RequestId, body: RefitRequestDecision
) -> DashboardRefitRequest:
    """Reject a refit request."""
    _wp8()
