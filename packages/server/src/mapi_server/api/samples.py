"""Sample projects: ``/v1/tenants/{tenant_id}/samples`` (v0.18 D20).

A one-click sample project per tenant, fitted for real (:mod:`mapi_server.services.samples`
has the life cycle). Tenant routes for signed-in members with at least the viewer role
somewhere in the tenant; API tokens are refused and clients reach none of them
(:data:`mapi_spec.api.CLIENT_OPERATIONS`).

* ``GET`` lists the samples as the caller sees them (``[]`` when samples are turned off);
* ``POST …/{sample_id}:install`` installs a sample (``202``; an editor somewhere in the
  tenant, since it spends their compute) or joins the one installed (``200``);
* ``POST …:retry`` starts a failed sample again (``202``; the installer or a tenant admin);
* ``POST …:retire`` hides it for good and removes its grants (``200``; tenant admins).

The actions are audited (``sample.*``, and ``member.add``/``member.remove`` for the grants a
sample makes or removes). Bodies are empty (at most 4 KiB, the app's v0.18 body limits).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, status
from fastapi.responses import JSONResponse
from mapi_spec.samples import SampleInfo

from mapi_server.api.common import Auth, Ctx, Session
from mapi_server.api.problems import responses_for
from mapi_server.services import samples as svc

__all__ = ["router"]

router = APIRouter(prefix="/v1/tenants/{tenant_id}/samples", tags=["samples"])

TenantId = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")]
SampleId = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?$")]


def _op(operation_id: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id)}


def _json(info: SampleInfo, status_code: int) -> JSONResponse:
    return JSONResponse(info.model_dump(mode="json"), status_code=status_code)


@router.get("", response_model=list[SampleInfo], **_op("list_samples"))
async def list_samples(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: TenantId
) -> list[SampleInfo]:
    """The sample projects of the tenant: what each is, whether it is installed and how far
    it got, and what you may do with it. Empty when samples are turned off."""
    return await svc.list_samples(ctx, session, auth, tenant_id)


@router.post(
    "/{sample_id}:install",
    response_model=SampleInfo,
    status_code=status.HTTP_202_ACCEPTED,
    **_op("install_sample"),
)
async def install_sample(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: TenantId, sample_id: SampleId
) -> JSONResponse:
    """Install the sample (``202``: its project is made and the fit starts as you), or join
    the one installed (``200``: a membership of its project, at most ``runner``)."""
    info, installed = await svc.install(ctx, session, auth, tenant_id, sample_id)
    return _json(info, status.HTTP_202_ACCEPTED if installed else status.HTTP_200_OK)


@router.post(
    "/{sample_id}:retry",
    response_model=SampleInfo,
    status_code=status.HTTP_202_ACCEPTED,
    **_op("retry_sample"),
)
async def retry_sample(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: TenantId, sample_id: SampleId
) -> JSONResponse:
    """Start a failed sample again; its next runs are yours. ``409`` when it is not failed
    or used its attempts."""
    info = await svc.retry(ctx, session, auth, tenant_id, sample_id)
    return _json(info, status.HTTP_202_ACCEPTED)


@router.post("/{sample_id}:retire", response_model=SampleInfo, **_op("retire_sample"))
async def retire_sample(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: TenantId, sample_id: SampleId
) -> SampleInfo:
    """Retire the sample: it is hidden for everyone and the grants it made are removed. Its
    project, data and models stay."""
    return await svc.retire(ctx, session, auth, tenant_id, sample_id)
