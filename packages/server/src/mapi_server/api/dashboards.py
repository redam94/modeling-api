"""Client dashboards: drafts, publishing, revisions, grants and share links (v0.16).

Project routes in the ``X-Mapi-Project`` project. Viewers and up read every dashboard
(``Dashboard``; a draft needs the editor role); a ``client`` lists and reads only the
published dashboards granted to them (``ClientDashboard``: the published revision, no ids
of people) and gets ``404`` for anything else. Drafts, publishing, grants and share links
need an editor; publishing, and share links with inputs, also need the runner role (the
live inputs run as the publisher). Every change is audited.

The inputs and readouts of a dashboard are in :mod:`mapi_server.api.dashboard_readouts`,
the public share-link routes in :mod:`mapi_server.api.shares`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Path, Query, Response, status
from mapi_spec.api import Page
from mapi_spec.dashboards import (
    ClientDashboard,
    Dashboard,
    DashboardCreate,
    DashboardDraftUpdate,
    DashboardGrant,
    DashboardRevision,
    DashboardShare,
    DashboardShareCreate,
    DashboardShareCreated,
    DashboardSpec,
    DashboardSummary,
    DashboardValidation,
)
from mapi_spec.tenancy import Action, ResourceKind
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
    revision_spec,
    share_dto,
    summary_dto,
)
from mapi_server.services.dashboards.shares import create_share, revoke_share
from mapi_server.services.runs import now

__all__ = ["DashboardId", "router"]

router = APIRouter(prefix="/v1/dashboards", tags=["dashboards"])

DashboardId = Annotated[str, Path(pattern=r"^dsh_[0-9a-z]{26}$")]
ShareId = Annotated[str, Path(pattern=r"^dss_[0-9a-z]{26}$")]
UserId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
IfMatch = Annotated[str | None, Header(alias="If-Match", max_length=130)]
StatusFilter = Literal["draft", "published", "archived"]


def _op(operation_id: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id)}


def _etag(response: Response, row: m.Dashboard) -> None:
    response.headers["ETag"] = row.draft_etag


async def _analyst_dto(session: Session, row: m.Dashboard, response: Response) -> Dashboard:
    _etag(response, row)
    return await dashboard_dto(session, row)


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
    granted to them; viewers do not see drafts (editors do)."""
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
            spec = revision_spec(rev)
        else:
            spec = DashboardSpec.model_validate(row.draft_spec)
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


@router.get("/{dashboard_id}", response_model=Dashboard | ClientDashboard, **_op("get_dashboard"))
async def get_dashboard(
    ctx: Ctx, auth: Auth, session: Session, dashboard_id: DashboardId, response: Response
) -> Dashboard | ClientDashboard:
    """Viewers and up: the analyst's view with the draft (``ETag`` = ``draft_etag``). A
    client: the published revision only (``ClientDashboard``)."""
    row = await load_dashboard(session, ctx, dashboard_id)
    if not caller_is_client(auth):
        return await _analyst_dto(session, row, response)
    rev = await load_revision(session, row)
    if rev is None:  # pragma: no cover - load_dashboard refuses unpublished ones
        raise NotFoundError("dashboard", dashboard_id)
    spec = revision_spec(rev)
    mode = await actuals_mode(session, ctx, rev.model_version_id) if spec.actuals.enabled else None
    return await client_dashboard(session, row, rev, effective_inputs(spec, mode))


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
    result, _ = await crud.validate_draft(
        session, ctx, DashboardSpec.model_validate(row.draft_spec)
    )
    return result


@router.post("/{dashboard_id}:publish", response_model=Dashboard, **_op("publish_dashboard"))
async def publish_dashboard(
    ctx: Ctx, session: Session, dashboard_id: DashboardId, response: Response
) -> Dashboard:
    """Publish the draft as a new revision pinned to its model version. Needs the runner
    role: the live inputs run as you. Queues the baseline readout. Audited."""
    row = await crud.publish(session, ctx, dashboard_id)
    return await _analyst_dto(session, row, response)


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
