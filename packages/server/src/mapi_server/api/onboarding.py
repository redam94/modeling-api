"""Onboarding progress: ``/v1/tenants/{tenant_id}/me/onboarding`` (v0.18 D19).

The tour and checklist progress of the signed-in member in one tenant. Tenant routes for
members with at least the viewer role somewhere in the tenant; API tokens are refused (the
tenant scope already refuses them; the handler checks again that the caller is a signed-in
person), and clients reach none of them (:data:`mapi_spec.api.CLIENT_OPERATIONS`).

``GET`` returns the state with ``ETag`` = its version; ``PATCH`` merges (completed steps
never revert; ``If-Match`` optional, ``412`` when stale; body at most 2 KiB, else ``413``);
``POST …:reset`` starts again. Nothing here is audited: progress is UX state.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Response
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.onboarding import OnboardingPatch, OnboardingState
from mapi_spec.tenancy import ProjectRole

from mapi_server.api.common import Auth, Session
from mapi_server.api.problems import responses_for
from mapi_server.auth.request import Credential, RequestAuth
from mapi_server.services import onboarding as svc

__all__ = ["router"]

router = APIRouter(prefix="/v1/tenants/{tenant_id}/me/onboarding", tags=["onboarding"])

TenantId = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")]
IfMatch = Annotated[str | None, Header(alias="If-Match", max_length=130)]

_PEOPLE = (Credential.SESSION, Credential.LOCAL)
"""Credentials of a signed-in person (``LOCAL``: the single-tenant administrator)."""


def _op(operation_id: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id)}


def _member(auth: RequestAuth, tenant_id: str) -> str:
    """The caller's principal id, after checking they are a person with at least the viewer
    role somewhere in the tenant (an operator acting without a membership is allowed: the
    progress is their own)."""
    if auth.credential not in _PEOPLE or auth.token is not None:
        raise MapiError(
            "onboarding progress belongs to a signed-in person; API tokens cannot read or "
            "change it",
            code=ErrorCode.FORBIDDEN,
        )
    member = any(
        mm.tenant_id == tenant_id and mm.role >= ProjectRole.VIEWER for mm in auth.memberships
    )
    principal_id = auth.principal_id
    if principal_id is None or not (member or auth.operator):
        raise MapiError(
            "you need at least the viewer role in this tenant", code=ErrorCode.FORBIDDEN
        )
    return principal_id


def _with_etag(response: Response, state: OnboardingState) -> OnboardingState:
    response.headers["ETag"] = svc.etag(state)
    return state


@router.get("", response_model=OnboardingState, **_op("get_onboarding"))
async def get_onboarding(
    auth: Auth, session: Session, tenant_id: TenantId, response: Response
) -> OnboardingState:
    """Your checklist and tour progress in this tenant (``ETag``: its version). Before the
    first change it is empty, at version 0."""
    principal_id = _member(auth, tenant_id)
    return _with_etag(response, await svc.get_state(session, tenant_id, principal_id))


@router.patch(
    "",
    response_model=OnboardingState,
    **_op("update_onboarding"),
)
async def update_onboarding(
    auth: Auth,
    session: Session,
    tenant_id: TenantId,
    body: OnboardingPatch,
    response: Response,
    if_match: IfMatch = None,
) -> OnboardingState:
    """Merge progress: steps completed, tours moved, the checklist dismissed, tips off,
    samples hidden. A completed step never reverts (only ``:reset`` starts again). With
    ``If-Match`` the change applies only to that version (``412`` when it moved)."""
    principal_id = _member(auth, tenant_id)
    state = await svc.apply_patch(session, tenant_id, principal_id, body, if_match=if_match)
    return _with_etag(response, state)


@router.post(":reset", response_model=OnboardingState, **_op("reset_onboarding"))
async def reset_onboarding(
    auth: Auth, session: Session, tenant_id: TenantId, response: Response
) -> OnboardingState:
    """Start the tours and the checklist again and turn tips back on. The samples you hid
    stay hidden, and the welcome dialog does not show again."""
    principal_id = _member(auth, tenant_id)
    return _with_etag(response, await svc.reset(session, tenant_id, principal_id))
