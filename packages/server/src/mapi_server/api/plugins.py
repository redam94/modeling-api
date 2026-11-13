"""Uploaded code: ``/v1/plugins`` and the tenant's plugin settings (v0.19 I-5).

Plugins are tenant resources reached from a project (``X-Mapi-Project``). Every route first
refuses a deployment with uploads turned off and a public-tier tenant (whatever
``MAPI_PUBLIC_CODE_UPLOAD_ENABLED`` says). Viewers read the approved versions; the uploader
and the tenant admins also read the private ones. Claims, tickets and ``:complete`` need a
tenant admin or an editor a tenant admin attested (D2). Approve and reject need a second
tenant admin with a recent web session; yank needs the uploader or a tenant admin, never an
API token. Clients reach none of these routes.

The settings routes (``/v1/tenants/{tenant_id}/plugin-settings`` and ``…/plugin-uploaders``)
are for tenant admins: private use on or off, the tenant code, and attested uploaders.

Bodies are at most 8 KiB (``app.V019_BODY_LIMITS``). Uploaded strings are returned as data;
the web renders them as text.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Response, status
from fastapi.responses import JSONResponse
from mapi_plots.sanitize import SanitizeError, sanitize_plot
from mapi_spec.api import Page
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.plots import PlotSpec
from mapi_spec.plugins import (
    PLUGIN_ID_RE,
    PLUGIN_UPLOAD_ID_RE,
    PLUGIN_VERSION_ID_RE,
    Plugin,
    PluginClaim,
    PluginDecision,
    PluginSettings,
    PluginSettingsPatch,
    PluginUploadComplete,
    PluginUploadTicket,
    PluginUploadTicketRequest,
    PluginUsage,
    PluginVersion,
    PluginVersionState,
    PluginVersionSummary,
    PluginYank,
    VerifiedUploader,
)

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Limit,
    Session,
    decode_cursor,
    encode_cursor,
)
from mapi_server.api.problems import responses_for
from mapi_server.auth.request import RequestAuth
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.plugins import review, store, uploaders, usage
from mapi_server.services.plugins.gate import describe_for

__all__ = ["V019_BODY_MAX_BYTES", "router", "tenant_router"]

V019_BODY_MAX_BYTES = 8 * 1024
"""Every plugin and plugin-settings request body (plan I-5)."""

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])
tenant_router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["tenants"])

PluginIdP = Annotated[str, Path(pattern=PLUGIN_ID_RE)]
VersionIdP = Annotated[str, Path(pattern=PLUGIN_VERSION_ID_RE)]
UploadIdP = Annotated[str, Path(pattern=PLUGIN_UPLOAD_ID_RE)]
PlotIdP = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.\-]{1,128}$")]
TenantIdP = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")]
UserIdP = Annotated[str, Path(pattern=r"^(?:usr|sa)_[0-9a-z]{26}$")]
IfNoneMatch = Annotated[str | None, Header(alias="If-None-Match", max_length=130)]


def _op(operation_id: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id)}


async def _visible_plugin(
    session: Session, ctx: Ctx, auth: RequestAuth, plugin_id: str
) -> m.Plugin:
    plugin = await store.load_plugin(session, ctx.scope.tenant_id, plugin_id)
    if uploaders.is_tenant_admin(auth) or plugin.created_by == auth.user_id:
        return plugin
    view = await store.plugin_view(session, ctx, auth, plugin)
    if view.versions == 0:
        raise NotFoundError("plugin", plugin_id)
    return plugin


async def _visible_version(
    session: Session, ctx: Ctx, auth: RequestAuth, plugin_id: str, version_id: str
) -> m.PluginVersion:
    version = await store.load_version(session, ctx.scope.tenant_id, plugin_id, version_id)
    if not store.version_visible(version, auth):
        raise NotFoundError("plugin version", version_id)
    return version


def _etag(dto: PluginVersion) -> str:
    raw = json.dumps(dto.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return '"' + hashlib.sha256(raw.encode()).hexdigest()[:32] + '"'


async def _version_out(
    session: Session, ctx: Ctx, version: m.PluginVersion, response: Response
) -> PluginVersion:
    dto = await store.version_dto(session, ctx, version)
    response.headers["ETag"] = _etag(dto)
    return dto


# ===================================================================================== plugins


@router.get("", response_model=Page[Plugin], **_op("list_plugins"))
async def list_plugins(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    state: Annotated[PluginVersionState | None, Query()] = None,
    mine: bool = False,
) -> Page[Plugin]:
    """The tenant's uploaded plugins by namespace. ``state`` keeps plugins with a version in
    that state, ``mine`` the plugins you claimed. Versions that are private to other people
    are not counted or shown."""
    uploaders.require_plugins_available(ctx, auth)
    after = decode_cursor(cursor)
    items, last = await store.list_plugins(
        session,
        ctx,
        auth,
        state=state,
        mine=mine,
        after=str(after) if after is not None else None,
        limit=limit,
    )
    return Page[Plugin](items=items, next_cursor=encode_cursor(last) if last else None)


@router.post("", response_model=Plugin, status_code=status.HTTP_201_CREATED, **_op("claim_plugin"))
async def claim_plugin(ctx: Ctx, auth: Auth, session: Session, body: PluginClaim) -> Plugin:
    """Claim a short name for uploaded code. The namespace is ``t_<tenant code>_<short
    name>``; the workspace gets its tenant code on its first claim."""
    plugin = await store.claim_plugin(session, ctx, auth, body)
    return store.plugin_dto(plugin, None, 0)


@router.get("/{plugin_id}", response_model=Plugin, **_op("get_plugin"))
async def get_plugin(ctx: Ctx, auth: Auth, session: Session, plugin_id: PluginIdP) -> Plugin:
    """One plugin and its newest version that you can see."""
    uploaders.require_plugins_available(ctx, auth)
    plugin = await _visible_plugin(session, ctx, auth, plugin_id)
    return await store.plugin_view(session, ctx, auth, plugin)


@router.get(
    "/{plugin_id}/versions",
    response_model=Page[PluginVersionSummary],
    **_op("list_plugin_versions"),
)
async def list_plugin_versions(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    plugin_id: PluginIdP,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[PluginVersionSummary]:
    """The versions of a plugin, newest first (private versions only for their uploader and
    the tenant admins)."""
    uploaders.require_plugins_available(ctx, auth)
    plugin = await _visible_plugin(session, ctx, auth, plugin_id)
    after = decode_cursor(cursor)
    items, last = await store.list_versions(
        session,
        ctx,
        auth,
        plugin,
        after=[str(a) for a in after] if isinstance(after, list) else None,
        limit=limit,
    )
    return Page[PluginVersionSummary](
        items=items, next_cursor=encode_cursor(last) if last else None
    )


@router.get(
    "/{plugin_id}/versions/{version_id}",
    response_model=PluginVersion,
    **_op("get_plugin_version"),
)
async def get_plugin_version(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    plugin_id: PluginIdP,
    version_id: VersionIdP,
    response: Response,
    if_none_match: IfNoneMatch = None,
) -> Any:
    """One version: inspect facts, SBOM, kit report, exports, issues and decisions
    (``ETag``; ``If-None-Match`` answers ``304`` when nothing changed)."""
    uploaders.require_plugins_available(ctx, auth)
    version = await _visible_version(session, ctx, auth, plugin_id, version_id)
    dto = await _version_out(session, ctx, version, response)
    if if_none_match is not None and if_none_match == response.headers["ETag"]:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": if_none_match})
    return dto


# ===================================================================================== uploads


@router.post(
    "/{plugin_id}/uploads",
    response_model=PluginUploadTicket,
    status_code=status.HTTP_201_CREATED,
    **_op("create_plugin_upload"),
)
async def create_plugin_upload(
    ctx: Ctx, auth: Auth, session: Session, plugin_id: PluginIdP, body: PluginUploadTicketRequest
) -> PluginUploadTicket:
    """An upload ticket: POST the wheel (``{dist}-{version}-py3-none-any.whl``) as
    ``multipart/form-data`` to ``post.url`` with every field of ``post.fields`` and the file
    last, then call ``…/uploads/{upload_id}:complete``. At most 20 tickets per person a day."""
    return await store.create_ticket(session, ctx, auth, plugin_id, body)


@router.post(
    "/{plugin_id}/uploads/{upload_id}:complete",
    response_model=PluginVersion,
    status_code=status.HTTP_202_ACCEPTED,
    **_op("complete_plugin_upload"),
)
async def complete_plugin_upload(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    plugin_id: PluginIdP,
    upload_id: UploadIdP,
    body: PluginUploadComplete,
    response: Response,
) -> PluginVersion:
    """The wheel is uploaded: the server hashes it and starts the checks (state
    ``inspecting``). ``project`` pays for the check runs; ``request_approval`` sends the
    version for review as soon as it passes its tests."""
    version = await store.complete_upload(session, ctx, auth, plugin_id, upload_id, body)
    return await _version_out(session, ctx, version, response)


# ====================================================================================== review


@router.post(
    "/{plugin_id}/versions/{version_id}:request-approval",
    response_model=PluginVersion,
    **_op("request_plugin_approval"),
)
async def request_plugin_approval(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    plugin_id: PluginIdP,
    version_id: VersionIdP,
    response: Response,
) -> PluginVersion:
    """Ask the tenant admins to review a version that passed its tests (its uploader or a
    tenant admin)."""
    uploaders.require_plugins_available(ctx, auth)
    version = await review.request_approval(session, ctx, auth, version_id, plugin_id=plugin_id)
    return await _version_out(session, ctx, version, response)


async def _decide(
    ctx: Ctx,
    auth: RequestAuth,
    session: Session,
    plugin_id: str,
    version_id: str,
    body: PluginDecision,
    response: Response,
    decision: str,
) -> PluginVersion:
    uploaders.require_plugins_available(ctx, auth)
    version = await review.decide(
        session,
        ctx,
        auth,
        version_id,
        body,
        decision="approve" if decision == "approve" else "reject",
        plugin_id=plugin_id,
    )
    return await _version_out(session, ctx, version, response)


@router.post(
    "/{plugin_id}/versions/{version_id}:approve",
    response_model=PluginVersion,
    **_op("approve_plugin_version"),
)
async def approve_plugin_version(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    plugin_id: PluginIdP,
    version_id: VersionIdP,
    body: PluginDecision,
    response: Response,
) -> PluginVersion:
    """Approve a version for the whole workspace: a tenant admin who is not the uploader,
    signed in to the web app in the last 15 minutes. The body repeats the digests the page
    showed."""
    return await _decide(ctx, auth, session, plugin_id, version_id, body, response, "approve")


@router.post(
    "/{plugin_id}/versions/{version_id}:reject",
    response_model=PluginVersion,
    **_op("reject_plugin_version"),
)
async def reject_plugin_version(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    plugin_id: PluginIdP,
    version_id: VersionIdP,
    body: PluginDecision,
    response: Response,
) -> PluginVersion:
    """Reject a version (the same rules as approve; ``note`` is required)."""
    return await _decide(ctx, auth, session, plugin_id, version_id, body, response, "reject")


@router.post(
    "/{plugin_id}/versions/{version_id}:yank",
    response_model=PluginVersion,
    **_op("yank_plugin_version"),
)
async def yank_plugin_version(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    plugin_id: PluginIdP,
    version_id: VersionIdP,
    body: PluginYank,
    response: Response,
) -> PluginVersion:
    """Withdraw a version: it leaves every catalog at once and runs that need it cannot
    start. ``security`` also cancels the queued and running runs that use it."""
    uploaders.require_plugins_available(ctx, auth)
    version = await review.yank(session, ctx, auth, version_id, body, plugin_id=plugin_id)
    return await _version_out(session, ctx, version, response)


@router.get(
    "/{plugin_id}/versions/{version_id}/usage",
    response_model=PluginUsage,
    **_op("get_plugin_usage"),
)
async def get_plugin_usage(
    ctx: Ctx, auth: Auth, session: Session, plugin_id: PluginIdP, version_id: VersionIdP
) -> PluginUsage:
    """What uses a version: runs in the last 30 days, model versions, endpoints and
    dashboards (each list at most 50 ids)."""
    uploaders.require_plugins_available(ctx, auth)
    version = await _visible_version(session, ctx, auth, plugin_id, version_id)
    return await usage.plugin_usage(session, version)


@router.get(
    "/{plugin_id}/versions/{version_id}/examples/{plot_id}",
    response_model=PlotSpec,
    **_op("get_plugin_example"),
)
async def get_plugin_example(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    plugin_id: PluginIdP,
    version_id: VersionIdP,
    plot_id: PlotIdP,
) -> JSONResponse:
    """The example of an uploaded plot recipe: a PlotSpec, sanitized again on the way out.
    Render it only in the plot-sandbox origin."""
    uploaders.require_plugins_available(ctx, auth)
    version = await _visible_version(session, ctx, auth, plugin_id, version_id)
    describe = await describe_for(session, version.id, ctx.catalogs.image_digest)
    raw = (describe.plot_examples or {}).get(plot_id) if describe is not None else None
    if not isinstance(raw, dict):
        raise NotFoundError("example", plot_id)
    try:
        clean = sanitize_plot(raw).plot
    except SanitizeError:
        raise NotFoundError("example", plot_id) from None
    except MapiError as exc:  # an envelope the sanitizer refuses: never served
        raise NotFoundError("example", plot_id) from exc
    return JSONResponse(clean.model_dump(mode="json", by_alias=True, exclude_none=True))


# ============================================================================ tenant settings


def _admin_tenant(ctx: Ctx, auth: RequestAuth, tenant_id: str) -> None:
    uploaders.require_plugins_available(ctx, auth)
    if auth.tenant_id != tenant_id:  # pragma: no cover - authentication bound the tenant
        raise MapiError("this request belongs to another tenant", code=ErrorCode.FORBIDDEN)


@tenant_router.get("/plugin-settings", response_model=PluginSettings, **_op("get_plugin_settings"))
async def get_plugin_settings(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: TenantIdP
) -> PluginSettings:
    """The workspace's uploaded-code settings, its tenant code (after the first claim) and
    whether uploads are allowed here (and why not)."""
    _admin_tenant(ctx, auth, tenant_id)
    return await uploaders.plugin_settings(session, ctx, tenant_id)


@tenant_router.patch(
    "/plugin-settings", response_model=PluginSettings, **_op("update_plugin_settings")
)
async def update_plugin_settings(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: TenantIdP, body: PluginSettingsPatch
) -> PluginSettings:
    """Turn private use on or off: whether uploaders may use their own versions that passed
    their tests before a tenant admin approves them."""
    _admin_tenant(ctx, auth, tenant_id)
    return await uploaders.update_settings(
        session, ctx, auth, tenant_id, private_use=body.private_use
    )


@tenant_router.get(
    "/plugin-uploaders",
    response_model=list[VerifiedUploader],
    **_op("list_plugin_uploaders"),
)
async def list_plugin_uploaders(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: TenantIdP
) -> list[VerifiedUploader]:
    """The editors a tenant admin attested: they may upload code (tenant admins always may)."""
    _admin_tenant(ctx, auth, tenant_id)
    return await uploaders.list_uploaders(session, tenant_id)


@tenant_router.put(
    "/plugin-uploaders/{user_id}",
    response_model=VerifiedUploader,
    **_op("attest_plugin_uploader"),
)
async def attest_plugin_uploader(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: TenantIdP, user_id: UserIdP
) -> VerifiedUploader:
    """Confirm that you know who this member is: they may upload code. Not yourself."""
    _admin_tenant(ctx, auth, tenant_id)
    return await uploaders.attest_uploader(session, ctx, auth, tenant_id, user_id)


@tenant_router.delete(
    "/plugin-uploaders/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    **_op("revoke_plugin_uploader"),
)
async def revoke_plugin_uploader(
    ctx: Ctx, auth: Auth, session: Session, tenant_id: TenantIdP, user_id: UserIdP
) -> Response:
    """Revoke an attestation. Versions the person uploaded keep their state."""
    _admin_tenant(ctx, auth, tenant_id)
    await uploaders.revoke_uploader(session, ctx, auth, tenant_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
