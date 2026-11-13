"""Client dashboards: drafts, publishing, revisions, grants and share links (v0.16).

Project routes in the ``X-Mapi-Project`` project. Viewers and up read every dashboard
(``Dashboard``; a draft needs the editor role); a ``client`` lists and reads only the
published dashboards granted to them (``ClientDashboard``: the published revision, no ids
of people) and gets ``404`` for anything else. Drafts, publishing, grants and share links
need an editor; publishing, and share links with inputs, also need the runner role (the
live inputs run as the publisher). Every change is audited.

The inputs and readouts of a dashboard are in :mod:`mapi_server.api.dashboard_readouts`,
the public share-link routes in :mod:`mapi_server.api.shares`.

v0.17 (layout 2, any model type): ``GET /v1/dashboards:presets`` suggests the presets of a
model version and compiles the first (nothing is saved), ``GET …/sources`` lists what the
draft's model version offers to panels and inputs, ``POST …:upgrade`` converts a layout 1
draft, and ``GET …/assets/{asset_id}`` serves a picture of the dashboard (PNG bytes).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Path, Query, Response, status
from mapi_spec.api import Page
from mapi_spec.capabilities import Capabilities
from mapi_spec.dashboard_presets import compile_preset, suggest_presets
from mapi_spec.dashboard_v2 import (
    ASSET_ID_RE,
    DashboardPresets,
    DashboardUpgrade,
    PresetSuggestion,
    SourceCatalog,
)
from mapi_spec.dashboards import (
    UPGRADE_NEEDS_REVIEW,
    ChannelDef,
    ClientDashboard,
    Dashboard,
    DashboardCreate,
    DashboardDraftUpdate,
    DashboardGrant,
    DashboardRevision,
    DashboardShare,
    DashboardShareCreate,
    DashboardShareCreated,
    DashboardSource,
    DashboardSummary,
    DashboardValidation,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.tenancy import Action, ResourceKind
from pydantic import ValidationError
from sqlalchemy import select

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
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.dashboards import crud
from mapi_server.services.dashboards.access import (
    actuals_mode,
    any_revision_spec,
    any_spec,
    caller_is_client,
    client_dashboard,
    dashboard_dto,
    effective_inputs,
    grant_dto,
    latest_readout,
    load_dashboard,
    load_revision,
    readout_header,
    revision_dto,
    share_dto,
    summary_dto,
)
from mapi_server.services.dashboards.assets import read_asset
from mapi_server.services.dashboards.shares import create_share, revoke_share
from mapi_server.services.dashboards.sources import version_sources
from mapi_server.services.dashboards.upgrade import review_issues
from mapi_server.services.runs import now
from mapi_server.services.sharing import dashboard_uses_uploaded_code

__all__ = ["DashboardId", "router"]

router = APIRouter(prefix="/v1/dashboards", tags=["dashboards"])

DashboardId = Annotated[str, Path(pattern=r"^dsh_[0-9a-z]{26}$")]
ShareId = Annotated[str, Path(pattern=r"^dss_[0-9a-z]{26}$")]
UserId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
AssetId = Annotated[str, Path(pattern=ASSET_ID_RE)]
IfMatch = Annotated[str | None, Header(alias="If-Match", max_length=130)]
StatusFilter = Literal["draft", "published", "archived"]


def _op(operation_id: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id)}


def _etag(response: Response, row: m.Dashboard) -> None:
    response.headers["ETag"] = row.draft_etag


async def _analyst_dto(
    session: Session, row: m.Dashboard, response: Response, ctx: Ctx | None = None
) -> Dashboard:
    """The analyst's DTO (with the ``ETag``). With ``ctx``, a dashboard flagged
    ``upgrade_needs_review`` also carries the issues that stop its upgrade."""
    _etag(response, row)
    issues: list[Issue] = []
    if ctx is not None and row.problem == UPGRADE_NEEDS_REVIEW:
        issues = await review_issues(ctx, session, row)
    return await dashboard_dto(session, row, upgrade_issues=issues)


# ================================================================================ dashboards


@router.get("", response_model=Page[DashboardSummary], **_op("list_dashboards"))
async def list_dashboards(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_: Annotated[StatusFilter | None, Query(alias="status")] = None,
) -> Page[DashboardSummary]:
    """Dashboards of the project, newest first. A client sees only the published ones
    granted to them, less those made with uploaded code (v0.19 D16: opening one answers
    ``403 UPLOADED_CODE_NOT_SHAREABLE``, so the list leaves it out); viewers do not see
    drafts (editors do)."""
    sc = ctx.scope
    client = caller_is_client(auth)
    stmt = select(m.Dashboard).where(
        m.Dashboard.tenant_id == sc.tenant_id, m.Dashboard.project_id == sc.project_id
    )
    if client:
        stmt = stmt.join(
            m.DashboardGrant,
            (m.DashboardGrant.dashboard_id == m.Dashboard.id)
            & (m.DashboardGrant.user_id == (auth.user_id or "")),
        ).where(m.Dashboard.status == "published")
    elif not auth.allows(Action.UPDATE, ResourceKind.DASHBOARD):
        stmt = stmt.where(m.Dashboard.status != "draft")
    if status_ is not None:
        stmt = stmt.where(m.Dashboard.status == status_)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.Dashboard.id < str(after))
    rows = list(
        (await session.execute(stmt.order_by(m.Dashboard.id.desc()).limit(limit + 1))).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    items: list[DashboardSummary] = []
    for row in rows:
        rev = await load_revision(session, row)
        if client:
            if rev is None:  # pragma: no cover - published rows have a revision
                continue
            if await dashboard_uses_uploaded_code(session, row):
                continue
            spec = any_revision_spec(rev)
        else:
            spec = any_spec(row.draft_spec)
        latest = await latest_readout(session, row.id, published_only=client)
        items.append(summary_dto(row, spec, readout_header(latest) if latest is not None else None))
    return Page[DashboardSummary](
        items=items, next_cursor=encode_cursor(rows[-1].id) if more and rows else None
    )


@router.post(
    "",
    response_model=Dashboard,
    status_code=status.HTTP_201_CREATED,
    **_op("create_dashboard"),
)
async def create_dashboard(
    ctx: Ctx, session: Session, body: DashboardCreate, response: Response
) -> Dashboard:
    """A new draft (``CONFLICT`` when the name is taken in the project). Audited."""
    row = await crud.create_dashboard(session, ctx, body)
    return await _analyst_dto(session, row, response)


@router.get(":presets", response_model=DashboardPresets, **_op("list_dashboard_presets"))
async def list_dashboard_presets(
    ctx: Ctx,
    session: Session,
    model_id: Annotated[str | None, Query(max_length=128)] = None,
    version: Annotated[int | None, Query(ge=1)] = None,
    alias: Annotated[str | None, Query(max_length=128)] = None,
) -> DashboardPresets:
    """The presets that fit a model version (``model_id`` and optional ``version``: the
    latest ready one) or the version an endpoint ``alias`` serves, best first, with plain
    reasons and the inputs each cannot offer; and the compiled layout 2 spec of the first
    (a starting draft: nothing is saved)."""
    if (model_id is None) == (alias is None):
        raise MapiError(
            "give model_id (and optionally version) or alias", code=ErrorCode.SCHEMA_INVALID
        )
    try:
        src = (
            DashboardSource(kind="endpoint", alias=alias)
            if alias is not None
            else DashboardSource(kind="model_version", model_id=model_id, version=version)
        )
    except ValidationError:
        raise MapiError("the model or endpoint name is not valid",
                        code=ErrorCode.SCHEMA_INVALID) from None  # fmt: skip
    sc = ctx.scope
    issues: list[Issue] = []
    binding = await crud.binding_of(session, sc.tenant_id, sc.project_id, src, issues)
    if binding is None:
        raise NotFoundError("model" if alias is None else "endpoint", (model_id or alias) or "")
    mv = binding.model_version
    found = await version_sources(ctx, session, mv)
    catalog = found.catalog
    if found.manifest is not None:
        caps: Capabilities = found.manifest.capabilities
        suggestions = suggest_presets(found.manifest, caps, catalog)
    else:
        suggestions = [PresetSuggestion(preset="generic", reasons=[
            "The platform does not list this model's plugin now: only the generic preset fits."
        ])]  # fmt: skip
    first = suggestions[0].preset if suggestions else "generic"
    channels: list[ChannelDef] | None = None
    if first == "mmm_planner":
        grid = await crud.read_grid(ctx, mv)
        if grid is not None:
            channels = [ChannelDef(name=c, label=c[:60]) for c in grid.channels[:40]]
    model = await session.get(m.Model, mv.model_id)
    title = (model.name if model is not None else "Model results")[:120]
    spec = None
    try:
        spec = compile_preset(first, catalog, title=title, source=src, channels=channels)
    except (ValueError, ValidationError) as exc:
        why = f"the {first} preset cannot be built: {str(exc)[:300]}"
        issues.append(Issue(code=ErrorCode.SCHEMA_INVALID, pointer="/preset", message=why))
    return DashboardPresets(suggestions=suggestions, spec=spec, issues=issues[:200])


@router.get("/{dashboard_id}", response_model=Dashboard | ClientDashboard, **_op("get_dashboard"))
async def get_dashboard(
    ctx: Ctx, auth: Auth, session: Session, dashboard_id: DashboardId, response: Response
) -> Dashboard | ClientDashboard:
    """Viewers and up: the analyst's view with the draft (``ETag`` = ``draft_etag``). A
    client: the published revision only (``ClientDashboard``)."""
    row = await load_dashboard(session, ctx, dashboard_id)
    if not caller_is_client(auth):
        return await _analyst_dto(session, row, response, ctx)
    rev = await load_revision(session, row)
    if rev is None:  # pragma: no cover - load_dashboard refuses unpublished ones
        raise NotFoundError("dashboard", dashboard_id)
    spec = any_revision_spec(rev)
    mode = await actuals_mode(session, ctx, rev.model_version_id) if spec.actuals.enabled else None
    inputs = effective_inputs(spec, mode, client=True)
    return await client_dashboard(session, row, rev, inputs)


@router.put("/{dashboard_id}/draft", response_model=Dashboard, **_op("update_dashboard_draft"))
async def update_dashboard_draft(
    ctx: Ctx,
    session: Session,
    dashboard_id: DashboardId,
    body: DashboardDraftUpdate,
    response: Response,
    if_match: IfMatch = None,
) -> Dashboard:
    """Replace the draft spec. ``If-Match`` must be the ``draft_etag`` you read
    (``DASHBOARD_SPEC_STALE`` when it changed since, ``PRECONDITION_FAILED`` without it)."""
    row = await crud.update_draft(session, ctx, dashboard_id, body, if_match)
    return await _analyst_dto(session, row, response)


@router.post(
    "/{dashboard_id}:validate", response_model=DashboardValidation, **_op("validate_dashboard")
)
async def validate_dashboard(
    ctx: Ctx, session: Session, dashboard_id: DashboardId
) -> DashboardValidation:
    """Whether the draft can be published now, and what to fix (JSON pointers into it)."""
    row = await load_dashboard(session, ctx, dashboard_id)
    result, _ = await crud.validate_draft(session, ctx, any_spec(row.draft_spec))
    return result


@router.post("/{dashboard_id}:publish", response_model=Dashboard, **_op("publish_dashboard"))
async def publish_dashboard(
    ctx: Ctx,
    session: Session,
    dashboard_id: DashboardId,
    response: Response,
    if_match: IfMatch = None,
) -> Dashboard:
    """Publish the draft as a new revision pinned to its model version. Needs the runner
    role: the live inputs run as you. Queues the baseline readout. Audited. ``If-Match``
    (the ``draft_etag`` you checked) publishes only that draft (``412`` when it moved)."""
    row = await crud.publish(session, ctx, dashboard_id, if_match)
    return await _analyst_dto(session, row, response)


@router.get("/{dashboard_id}/sources", response_model=SourceCatalog, **_op("get_dashboard_sources"))
async def get_dashboard_sources(
    ctx: Ctx, session: Session, dashboard_id: DashboardId
) -> SourceCatalog:
    """What the draft's model version offers to panels and inputs: outputs (summary names,
    dims, representation, what each allows), fit metrics, scorecard checks, pictures and
    inputs (cached per model version)."""
    row = await load_dashboard(session, ctx, dashboard_id)
    binding = await crud.resolve_binding(session, ctx, any_spec(row.draft_spec))
    return (await version_sources(ctx, session, binding.model_version)).catalog


@router.post("/{dashboard_id}:upgrade", response_model=DashboardUpgrade, **_op("upgrade_dashboard"))
async def upgrade_dashboard(
    ctx: Ctx,
    session: Session,
    dashboard_id: DashboardId,
    response: Response,
    if_match: IfMatch = None,
) -> DashboardUpgrade:
    """Convert the layout 1 draft to layout 2 and save it as the draft (``If-Match``: the
    ``draft_etag``). ``issues`` say what to fix before publishing, ``notes`` what changed.
    Publish it as usual. Audited."""
    result = await crud.upgrade_draft(session, ctx, dashboard_id, if_match)
    dto = await _analyst_dto(session, result.row, response)
    return DashboardUpgrade(dashboard=dto, issues=result.issues, notes=result.notes)


@router.get(
    "/{dashboard_id}/assets/{asset_id}",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}, **responses_for("get_dashboard_asset")},
    operation_id="get_dashboard_asset",
)
async def get_dashboard_asset(
    ctx: Ctx, session: Session, dashboard_id: DashboardId, asset_id: AssetId
) -> Response:
    """A picture of the dashboard (PNG, at most 512 KiB). Members (viewers and up) may
    read the pictures of any revision (History, the builder); a client only those of the
    published revision (as a share link), so a picture the analyst removed stays gone."""
    row = await load_dashboard(session, ctx, dashboard_id)
    narrow: list[str] | None = None
    if caller_is_client(ctx.request):
        narrow = [row.published_revision_id] if row.published_revision_id else []
    asset = await read_asset(ctx, session, row.id, asset_id, revision_ids=narrow)
    return Response(
        content=asset.data,
        media_type=asset.content_type,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"},
    )


@router.post("/{dashboard_id}:archive", response_model=Dashboard, **_op("archive_dashboard"))
async def archive_dashboard(
    ctx: Ctx, session: Session, dashboard_id: DashboardId, response: Response
) -> Dashboard:
    """Archive: clients and share links lose the dashboard; every share link is revoked.
    Idempotent. Audited."""
    row = await crud.archive(session, ctx, dashboard_id)
    return await _analyst_dto(session, row, response)


@router.get(
    "/{dashboard_id}/revisions",
    response_model=Page[DashboardRevision],
    **_op("list_dashboard_revisions"),
)
async def list_dashboard_revisions(
    ctx: Ctx,
    session: Session,
    dashboard_id: DashboardId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[DashboardRevision]:
    """Published revisions, newest first."""
    row = await load_dashboard(session, ctx, dashboard_id)
    stmt = select(m.DashboardRevision).where(m.DashboardRevision.dashboard_id == row.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.DashboardRevision.seq < int(after))
    rows = list(
        (
            await session.execute(stmt.order_by(m.DashboardRevision.seq.desc()).limit(limit + 1))
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[DashboardRevision](
        items=[revision_dto(r) for r in rows],
        next_cursor=encode_cursor(rows[-1].seq) if more and rows else None,
    )


# ==================================================================================== grants


@router.get(
    "/{dashboard_id}/grants", response_model=Page[DashboardGrant], **_op("list_dashboard_grants")
)
async def list_dashboard_grants(
    ctx: Ctx,
    session: Session,
    dashboard_id: DashboardId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[DashboardGrant]:
    """The users the dashboard is granted to (with the e-mail of their membership)."""
    row = await load_dashboard(session, ctx, dashboard_id)
    stmt = select(m.DashboardGrant).where(m.DashboardGrant.dashboard_id == row.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.DashboardGrant.user_id > str(after))
    rows = list(
        (await session.execute(stmt.order_by(m.DashboardGrant.user_id).limit(limit + 1))).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    members: dict[str, m.Membership] = {}
    if rows:
        for mm in (
            await session.execute(
                select(m.Membership).where(
                    m.Membership.tenant_id == row.tenant_id,
                    m.Membership.principal_id.in_([r.user_id for r in rows]),
                )
            )
        ).scalars():
            if mm.project_id in (None, row.project_id):
                members.setdefault(mm.principal_id, mm)
    return Page[DashboardGrant](
        items=[grant_dto(r, members.get(r.user_id)) for r in rows],
        next_cursor=encode_cursor(rows[-1].user_id) if more and rows else None,
    )


@router.put(
    "/{dashboard_id}/grants/{user_id}",
    response_model=DashboardGrant,
    **_op("put_dashboard_grant"),
)
async def put_dashboard_grant(
    ctx: Ctx, session: Session, dashboard_id: DashboardId, user_id: UserId
) -> DashboardGrant:
    """Grant the dashboard to a member of the project (idempotent; ``404`` for a user who
    holds no membership here: invite them first). Audited."""
    grant, member = await crud.put_grant(session, ctx, dashboard_id, user_id)
    return grant_dto(grant, member)


@router.delete(
    "/{dashboard_id}/grants/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    **_op("delete_dashboard_grant"),
)
async def delete_dashboard_grant(
    ctx: Ctx, session: Session, dashboard_id: DashboardId, user_id: UserId
) -> Response:
    """Take the dashboard away from a user (idempotent). Audited."""
    await crud.delete_grant(session, ctx, dashboard_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ================================================================================ share links


@router.get(
    "/{dashboard_id}/shares", response_model=Page[DashboardShare], **_op("list_dashboard_shares")
)
async def list_dashboard_shares(
    ctx: Ctx,
    session: Session,
    dashboard_id: DashboardId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[DashboardShare]:
    """Share links of the dashboard, newest first (never their tokens)."""
    row = await load_dashboard(session, ctx, dashboard_id)
    stmt = select(m.DashboardShare).where(m.DashboardShare.dashboard_id == row.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.DashboardShare.id < str(after))
    rows = list(
        (
            await session.execute(stmt.order_by(m.DashboardShare.id.desc()).limit(limit + 1))
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    at = now()
    return Page[DashboardShare](
        items=[share_dto(r, at) for r in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.post(
    "/{dashboard_id}/shares",
    response_model=DashboardShareCreated,
    status_code=status.HTTP_201_CREATED,
    **_op("create_dashboard_share"),
)
async def create_dashboard_share(
    ctx: Ctx, session: Session, dashboard_id: DashboardId, body: DashboardShareCreate
) -> DashboardShareCreated:
    """A share link of the published dashboard. The token is in this response once (only
    its HMAC is stored); open ``<app origin><url_fragment>``. Read-only unless
    ``allowed_inputs`` names inputs, which needs the runner role. Audited."""
    share, token = await create_share(session, ctx, dashboard_id, body)
    dto = share_dto(share)
    return DashboardShareCreated(**dto.model_dump(), token=token, url_fragment=f"/s#{token}")


@router.post(
    "/{dashboard_id}/shares/{share_id}:revoke",
    response_model=DashboardShare,
    **_op("revoke_dashboard_share"),
)
async def revoke_dashboard_share(
    ctx: Ctx, session: Session, dashboard_id: DashboardId, share_id: ShareId
) -> DashboardShare:
    """Revoke a share link: its next request answers ``410 LINK_EXPIRED``. Idempotent."""
    share = await revoke_share(session, ctx, dashboard_id, share_id)
    return share_dto(share)
