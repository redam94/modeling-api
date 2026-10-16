"""The trust lifecycle over HTTP: sign-up, verification, tenant deletion and GDPR export.

* ``POST /auth/signup``, ``POST /auth/verify-email`` (public, rate-limited);
* ``GET /v1/tenants/{id}/verification``; ``PUT /v1/admin/tenants/{id}/verification``
  (operators);
* ``POST|GET|DELETE /v1/tenants/{id}/deletion``; ``POST /v1/admin/tenants/{id}/deletion/
  retry`` (operators);
* ``POST /v1/tenants/{id}/exports``, ``GET /v1/tenants/{id}/exports/{export_id}``;
* ``GET /v1/admin/signups``, ``POST /v1/admin/signups/{id}/review`` (operators: the Sybil
  review queue);
* ``GET /dev/outbox`` (``dev`` profile with the outbox mailer only): the newest e-mails, so
  end-to-end tests can follow a verification link.

Every operation documents its RFC 7807 problems (from :data:`mapi_spec.api.ALL_ENDPOINTS`).
Tenant routes run in the tenant's row-level-security session; operator routes and the
public sign-up routes in the gateway role's session.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any, Final

from fastapi import APIRouter, FastAPI, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from mapi_spec.api import ALL_ENDPOINTS, Page
from mapi_spec.errors import ERROR_INFO, Problem
from mapi_spec.trust import (
    EmailVerify,
    GdprExport,
    GdprExportRequest,
    SignupAccepted,
    SignupRequest,
    SignupReview,
    SignupReviewDecision,
    SignupReviewState,
    TenantDeletion,
    TenantDeletionRequest,
    VerificationResult,
    VerificationStatus,
    VerificationUpdate,
)

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Limit,
    Session,
    SystemSession,
    decode_cursor,
    encode_cursor,
)
from mapi_server.errors import NotFoundError
from mapi_server.services import deletion as deletion_svc
from mapi_server.services import gdpr_export, signup, verification
from mapi_server.services.mail import read_outbox
from mapi_server.settings import MailerKind, Profile, Settings

__all__ = [
    "ROUTERS",
    "admin_router",
    "auth_router",
    "mount_dev_outbox",
    "problem_responses",
    "router",
]

router = APIRouter(tags=["tenants"])
admin_router = APIRouter(prefix="/v1/admin", tags=["admin"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])

Slug = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")]
ExportId = Annotated[str, Path(pattern=r"^exp_[0-9a-z]{26}$")]
SignupId = Annotated[str, Path(pattern=r"^sgn_[0-9a-z]{26}$")]
DEVICE_COOKIE_MAX_AGE: Final = 365 * 86400

_DECLS = {e.operation_id: e for e in ALL_ENDPOINTS}


def problem_responses(operation_id: str) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` for an operation's documented problems (RFC 7807)."""
    decl = _DECLS[operation_id]
    by_status: dict[int, list[str]] = {}
    for code in decl.errors:
        by_status.setdefault(ERROR_INFO[code].status, []).append(code.value)
    out: dict[int | str, dict[str, Any]] = {}
    for http_status, codes in sorted(by_status.items()):
        out[http_status] = {
            "model": Problem,
            "description": "Problem document (`application/problem+json`); code "
            + ", ".join(f"`{c}`" for c in codes),
        }
    return out


# ================================================================================= sign-up


@auth_router.post(
    "/signup",
    response_model=SignupAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="signup",
    responses=problem_responses("signup"),
)
async def signup_route(
    request: Request, ctx: Ctx, auth: Auth, body: SignupRequest, response: Response
) -> SignupAccepted:
    """Self-service sign-up. The answer is the same for new and known addresses; the link
    in the e-mail (valid 24 hours, single use) creates a ``public``-tier workspace."""
    device = request.cookies.get(signup.DEVICE_COOKIE)
    if device is None or len(device) > 64:
        device = secrets.token_urlsafe(24)
        response.set_cookie(
            signup.DEVICE_COOKIE,
            device,
            max_age=DEVICE_COOKIE_MAX_AGE,
            httponly=True,
            secure=ctx.settings.secure_cookies,
            samesite="lax",
            path="/auth",
        )
    await signup.request_signup(ctx, auth, body, device_id=device)
    response.headers["Cache-Control"] = "no-store"
    return SignupAccepted()


@auth_router.post(
    "/verify-email",
    response_model=VerificationResult,
    operation_id="verify_email",
    responses=problem_responses("verify_email"),
)
async def verify_email_route(
    ctx: Ctx, auth: Auth, body: EmailVerify, response: Response
) -> VerificationResult:
    """Verify the address with the e-mailed token: creates the workspace (sign in next)."""
    response.headers["Cache-Control"] = "no-store"
    return await signup.verify_email(ctx, auth, body.token)


# =========================================================================== verification


@router.get(
    "/v1/tenants/{tenant_id}/verification",
    response_model=VerificationStatus,
    operation_id="get_tenant_verification",
    responses=problem_responses("get_tenant_verification"),
)
async def get_tenant_verification(
    ctx: Ctx, session: Session, tenant_id: Slug
) -> VerificationStatus:
    """E-mail and identity verification, and whether the workspace may upload code."""
    return await verification.verification_status(session, ctx, tenant_id)


@admin_router.put(
    "/tenants/{tenant_id}/verification",
    response_model=VerificationStatus,
    operation_id="admin_put_tenant_verification",
    responses=problem_responses("admin_put_tenant_verification"),
)
async def admin_put_tenant_verification(
    ctx: Ctx, auth: Auth, system: SystemSession, tenant_id: Slug, body: VerificationUpdate
) -> VerificationStatus:
    """Record (or revoke) identity verification of a tenant; audited with the reason."""
    return await verification.put_verification(system, ctx, auth, tenant_id, body)


# ================================================================================ deletion


@router.post(
    "/v1/tenants/{tenant_id}/deletion",
    response_model=TenantDeletion,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="request_tenant_deletion",
    responses=problem_responses("request_tenant_deletion"),
)
async def request_tenant_deletion(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: Slug, body: TenantDeletionRequest
) -> TenantDeletion:
    """Schedule the deletion of the whole tenant. ``confirm`` repeats the tenant id. The
    tenant is suspended at once and its unfinished runs are cancelled; the purge starts
    when the grace period ends and can be cancelled until then."""
    row = await deletion_svc.request_deletion(session, ctx, auth, tenant_id, body)
    return deletion_svc.deletion_dto(row)


@router.get(
    "/v1/tenants/{tenant_id}/deletion",
    response_model=TenantDeletion,
    operation_id="get_tenant_deletion",
    responses=problem_responses("get_tenant_deletion"),
)
async def get_tenant_deletion(session: Session, tenant_id: Slug) -> TenantDeletion:
    """The latest deletion of the tenant and its progress per store."""
    row = await deletion_svc.latest_deletion(session, tenant_id)
    if row is None:
        raise NotFoundError("deletion of tenant", tenant_id)
    return deletion_svc.deletion_dto(row)


@router.delete(
    "/v1/tenants/{tenant_id}/deletion",
    response_model=TenantDeletion,
    operation_id="cancel_tenant_deletion",
    responses=problem_responses("cancel_tenant_deletion"),
)
async def cancel_tenant_deletion(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: Slug
) -> TenantDeletion:
    """Cancel a scheduled deletion (``CONFLICT`` once the purge has started)."""
    row = await deletion_svc.cancel_deletion(session, ctx, auth, tenant_id)
    return deletion_svc.deletion_dto(row)


@admin_router.post(
    "/tenants/{tenant_id}/deletion/retry",
    response_model=TenantDeletion,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="admin_retry_tenant_deletion",
    responses=problem_responses("admin_retry_tenant_deletion"),
)
async def admin_retry_tenant_deletion(
    ctx: Ctx, auth: Auth, system: SystemSession, tenant_id: Slug
) -> TenantDeletion:
    """Restart a failed purge (the orchestrator picks it up on its next pass)."""
    row = await deletion_svc.retry_deletion(system, ctx, auth, tenant_id)
    return deletion_svc.deletion_dto(row)


# ================================================================================= exports


@router.post(
    "/v1/tenants/{tenant_id}/exports",
    response_model=GdprExport,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_tenant_export",
    responses=problem_responses("create_tenant_export"),
)
async def create_tenant_export(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: Slug, body: GdprExportRequest
) -> GdprExport:
    """Queue a GDPR export of everything the tenant stored; poll it for the link."""
    row = await gdpr_export.create_export(session, ctx, auth, tenant_id, body)
    return gdpr_export.export_dto(row)


@router.get(
    "/v1/tenants/{tenant_id}/exports",
    response_model=Page[GdprExport],
    operation_id="list_tenant_exports",
    responses=problem_responses("list_tenant_exports"),
)
async def list_tenant_exports(
    session: Session,
    tenant_id: Slug,
    response: Response,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[GdprExport]:
    """The tenant's exports, newest first: what is being built and what can be downloaded
    (``GET`` one export for its download link)."""
    response.headers["Cache-Control"] = "no-store"
    after = decode_cursor(cursor)
    items, more = await gdpr_export.list_exports(
        session, tenant_id, limit=limit, after=str(after) if after is not None else None
    )
    return Page[GdprExport](
        items=items, next_cursor=encode_cursor(items[-1].id) if more and items else None
    )


@router.get(
    "/v1/tenants/{tenant_id}/exports/{export_id}",
    response_model=GdprExport,
    operation_id="get_tenant_export",
    responses=problem_responses("get_tenant_export"),
)
async def get_tenant_export(
    ctx: Ctx, session: Session, tenant_id: Slug, export_id: ExportId, response: Response
) -> GdprExport:
    """An export; once ``ready``, ``download_url`` is a fresh short-lived signed link."""
    response.headers["Cache-Control"] = "no-store"
    return await gdpr_export.get_export(session, ctx, tenant_id, export_id)


# ================================================================================= review


@admin_router.get(
    "/signups",
    response_model=Page[SignupReview],
    operation_id="admin_list_signups",
    responses=problem_responses("admin_list_signups"),
)
async def admin_list_signups(
    system: SystemSession,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    review: SignupReviewState | None = None,
    tenant_id: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[SignupReview]:
    """Sign-ups, newest first; ``review=flagged`` is the Sybil review queue."""
    after = decode_cursor(cursor)
    items, more = await signup.list_signups(
        system,
        review=review,
        tenant_id=tenant_id,
        limit=limit,
        after=str(after) if after is not None else None,
    )
    return Page[SignupReview](
        items=items, next_cursor=encode_cursor(items[-1].id) if more and items else None
    )


@admin_router.post(
    "/signups/{signup_id}/review",
    response_model=SignupReview,
    operation_id="admin_review_signup",
    responses=problem_responses("admin_review_signup"),
)
async def admin_review_signup(
    ctx: Ctx, auth: Auth, system: SystemSession, signup_id: SignupId, body: SignupReviewDecision
) -> SignupReview:
    """Approve or reject a sign-up; a rejection suspends the workspace it created."""
    return await signup.review_signup(system, ctx, auth, signup_id, body)


ROUTERS: Final = (auth_router, router, admin_router)
"""Every router of this module (the app includes each)."""


# ================================================================================ dev only


def mount_dev_outbox(app: FastAPI, settings: Settings) -> None:
    """``GET /dev/outbox?to=…``: the newest outbox e-mails (``dev`` profile only)."""
    if settings.profile is not Profile.DEV or settings.effective_mailer is not MailerKind.OUTBOX:
        raise RuntimeError(
            "the dev outbox is only served in the dev profile with MAPI_MAILER=outbox"
        )
    directory = settings.resolved_outbox_dir()

    async def dev_outbox(
        to: Annotated[str | None, Query(max_length=320)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> JSONResponse:
        """Development only: messages the outbox mailer wrote, newest first."""
        return JSONResponse({"items": read_outbox(directory, to=to, limit=limit)})

    app.add_api_route(
        "/dev/outbox",
        dev_outbox,
        methods=["GET"],
        operation_id="dev_outbox",
        tags=["dev"],
        include_in_schema=False,
    )
