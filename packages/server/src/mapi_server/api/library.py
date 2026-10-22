"""The library: ``/v1/library`` (v0.18 D1, D2, D5, D8).

Project routes in the ``X-Mapi-Project`` project. Viewers read the project's items and the
tenant's items, check documents (``:validate``), preview chart templates and use items
(``:use``; a model recipe also needs ``spec`` create, since its result becomes a spec).
Editors create, revise and archive project items; tenant items need a tenant admin
(``tenant_library``), and so does ``:promote``. Clients reach none of these routes.

Every write is audited (``library.create``, ``library.revise``, ``library.archive``,
``library.promote``). ``:validate`` and ``charts:preview`` have their own per-user minute
limits. User-authored strings (titles, summaries, labels) are stored and returned verbatim:
the web renders them as text.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Response, status
from mapi_spec.api import Page
from mapi_spec.audit import AuditAction
from mapi_spec.library import (
    LIBRARY_ITEM_ID_RE,
    ChartPreview,
    ChartPreviewRequest,
    LibraryCreate,
    LibraryItem,
    LibraryKind,
    LibraryRevision,
    LibraryRevisionCreate,
    LibraryScope,
    LibraryUseRequest,
    LibraryUseResult,
    LibraryValidateRequest,
    LibraryValidation,
    parse_library_doc,
)
from mapi_spec.tenancy import Action, ResourceKind

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
from mapi_server.services import library as svc
from mapi_server.services.audit import write_audit
from mapi_server.services.library.store import item_cursor

__all__ = ["router"]

router = APIRouter(prefix="/v1/library", tags=["library"])

ItemId = Annotated[str, Path(pattern=LIBRARY_ITEM_ID_RE)]
RevisionNo = Annotated[int, Path(ge=1)]
IfMatch = Annotated[str | None, Header(alias="If-Match", max_length=130)]


def _op(operation_id: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id)}


def _resource(scope: str) -> ResourceKind:
    return ResourceKind.TENANT_LIBRARY if scope == "tenant" else ResourceKind.LIBRARY


def _require_for(auth: RequestAuth, item: m.LibraryItem, action: Action) -> None:
    """A tenant item also needs the action on ``tenant_library`` (a tenant admin); the route
    already checked it on ``library`` in the project."""
    if item.scope == "tenant":
        auth.require(action, ResourceKind.TENANT_LIBRARY)


def _author(auth: RequestAuth) -> str:
    return auth.principal_id or "unknown"


def _with_etag(response: Response, row: m.LibraryItem) -> LibraryItem:
    dto = svc.item_dto(row)
    response.headers["ETag"] = dto.etag
    return dto


async def _audit(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    action: AuditAction,
    item: m.LibraryItem,
    details: dict[str, Any],
) -> None:
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                action,
                _resource(item.scope),
                resource_id=item.id,
                details={"kind": item.kind, "scope": item.scope, **details},
                project_id=item.project_id or ctx.scope.project_id,
            )
        ],
        session=session,
    )


# ==================================================================================== items


@router.get("", response_model=Page[LibraryItem], **_op("list_library_items"))
async def list_library_items(
    ctx: Ctx,
    session: Session,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    kind: LibraryKind | None = None,
    scope: LibraryScope | None = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
    include_archived: bool = False,
) -> Page[LibraryItem]:
    """Library items of the project and of the tenant, most recently changed first. Archived
    items only with ``include_archived=true``; ``q`` matches the name or the title."""
    rows = await svc.list_items(
        session,
        ctx.scope,
        kind=kind,
        item_scope=scope,
        q=q,
        include_archived=include_archived,
        after=decode_cursor(cursor),
        limit=limit,
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[LibraryItem](
        items=[svc.item_dto(r) for r in rows],
        next_cursor=encode_cursor(item_cursor(rows[-1])) if more and rows else None,
    )


@router.post(
    "",
    response_model=LibraryItem,
    status_code=status.HTTP_201_CREATED,
    **_op("create_library_item"),
)
async def create_library_item(
    ctx: Ctx, auth: Auth, session: Session, body: LibraryCreate, response: Response
) -> LibraryItem:
    """Create an item and its revision 1 from a document (or YAML/JSON text). A document that
    does not compile against the current catalog is stored with ``valid: false`` and its
    issues, except a workflow template, which is refused. ``scope: tenant`` needs a tenant
    admin."""
    if body.scope == "tenant":
        auth.require(Action.CREATE, ResourceKind.TENANT_LIBRARY)
    doc, stored = svc.parse_body(body.document, body.text)
    scope = ctx.scope
    checked = await svc.check_document(ctx, session, scope, doc)
    item, _ = await svc.create_item(
        session,
        scope,
        doc,
        stored,
        checked,
        item_scope=body.scope,
        created_by=_author(auth),
        message=body.message,
        max_items=ctx.settings.library_items_per_tenant,
    )
    await _audit(
        ctx, auth, session, AuditAction.LIBRARY_ITEM_CREATED, item, {"valid": checked.valid}
    )
    return _with_etag(response, item)


@router.post(":validate", response_model=LibraryValidation, **_op("validate_library_document"))
async def validate_library_document(
    ctx: Ctx, session: Session, body: LibraryValidateRequest
) -> LibraryValidation:
    """Check a document against the current catalog (and, with ``dataset``, against the
    data: binding, shape, slots, variables). Nothing is saved; the issues point into the
    document."""
    return await svc.validate_doc(ctx, session, body.document, body.text, body.dataset)


@router.post("/charts:preview", response_model=ChartPreview, **_op("preview_library_chart"))
async def preview_library_chart(
    ctx: Ctx, session: Session, body: ChartPreviewRequest
) -> ChartPreview:
    """Draw a chart template on its source recipe's example data (JSON only, no run).
    Problems with the template come back as issues with ``plot: null``."""
    return await svc.preview_chart(ctx, session, body.document)


@router.get("/{item_id}", response_model=LibraryItem, **_op("get_library_item"))
async def get_library_item(
    ctx: Ctx, session: Session, item_id: ItemId, response: Response
) -> LibraryItem:
    """One item (``ETag``: ``"r<latest_revision>"``, for ``If-Match`` on a new revision)."""
    return _with_etag(response, await svc.load_item(session, ctx.scope, item_id))


@router.get(
    "/{item_id}/revisions",
    response_model=Page[LibraryRevision],
    **_op("list_library_revisions"),
)
async def list_library_revisions(
    ctx: Ctx,
    session: Session,
    item_id: ItemId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[LibraryRevision]:
    """The revisions of an item, newest first."""
    item = await svc.load_item(session, ctx.scope, item_id)
    rows = await svc.list_revisions(session, item, after=decode_cursor(cursor), limit=limit)
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[LibraryRevision](
        items=[svc.revision_dto(r) for r in rows],
        next_cursor=encode_cursor(rows[-1].revision) if more and rows else None,
    )


@router.get(
    "/{item_id}/revisions/{revision}",
    response_model=LibraryRevision,
    **_op("get_library_revision"),
)
async def get_library_revision(
    ctx: Ctx, session: Session, item_id: ItemId, revision: RevisionNo
) -> LibraryRevision:
    """One revision: the document, whether it compiled, its issues and the compiled output."""
    item = await svc.load_item(session, ctx.scope, item_id)
    return svc.revision_dto(await svc.load_revision(session, item, revision))


@router.post(
    "/{item_id}/revisions",
    response_model=LibraryRevision,
    status_code=status.HTTP_201_CREATED,
    **_op("create_library_revision"),
)
async def create_library_revision(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    item_id: ItemId,
    body: LibraryRevisionCreate,
    response: Response,
    if_match: IfMatch = None,
) -> LibraryRevision:
    """Add an immutable revision. ``If-Match`` with the item's ``ETag`` refuses the change
    (``412``) when someone saved in between. The kind cannot change; the document's
    metadata renames and retitles the item."""
    scope = ctx.scope
    item = await svc.load_item(session, scope, item_id, for_update=True)
    _require_for(auth, item, Action.UPDATE)
    doc, stored = svc.parse_body(body.document, body.text)
    checked = await svc.check_document(ctx, session, scope, doc)
    rev = await svc.revise_item(
        session,
        item,
        doc,
        stored,
        checked,
        if_match=if_match,
        created_by=_author(auth),
        message=body.message,
        max_revisions=ctx.settings.library_revisions_per_item,
    )
    await _audit(
        ctx,
        auth,
        session,
        AuditAction.LIBRARY_ITEM_REVISED,
        item,
        {"revision": rev.revision, "valid": rev.valid},
    )
    response.headers["ETag"] = svc.item_dto(item).etag
    return svc.revision_dto(rev)


async def _set_archived(
    ctx: Ctx, auth: Auth, session: Session, item_id: str, response: Response, archived: bool
) -> LibraryItem:
    item = await svc.load_item(session, ctx.scope, item_id, for_update=True)
    _require_for(auth, item, Action.DELETE)
    if await svc.archive_item(session, item, archived=archived):
        await _audit(
            ctx, auth, session, AuditAction.LIBRARY_ITEM_ARCHIVED, item, {"archived": archived}
        )
    return _with_etag(response, item)


@router.post("/{item_id}:archive", response_model=LibraryItem, **_op("archive_library_item"))
async def archive_library_item(
    ctx: Ctx, auth: Auth, session: Session, item_id: ItemId, response: Response
) -> LibraryItem:
    """Archive an item: lists and pickers hide it; nothing is deleted, and recipes that use
    it keep working."""
    return await _set_archived(ctx, auth, session, item_id, response, True)


@router.post("/{item_id}:unarchive", response_model=LibraryItem, **_op("unarchive_library_item"))
async def unarchive_library_item(
    ctx: Ctx, auth: Auth, session: Session, item_id: ItemId, response: Response
) -> LibraryItem:
    """Show an archived item again."""
    return await _set_archived(ctx, auth, session, item_id, response, False)


@router.post(
    "/{item_id}:promote",
    response_model=LibraryItem,
    status_code=status.HTTP_201_CREATED,
    **_op("promote_library_item"),
)
async def promote_library_item(
    ctx: Ctx, auth: Auth, session: Session, item_id: ItemId, response: Response
) -> LibraryItem:
    """Copy a project item's latest revision into a new tenant item, which every project of
    the tenant can read (tenant admins). The project item stays as it is."""
    auth.require(Action.CREATE, ResourceKind.TENANT_LIBRARY)
    scope = ctx.scope
    item = await svc.load_item(session, scope, item_id)
    latest = await svc.load_revision(session, item)
    checked = await svc.check_document(ctx, session, scope, parse_library_doc(latest.document))
    tenant_item, _ = await svc.promote_item(
        session,
        item,
        checked,
        created_by=_author(auth),
        max_items=ctx.settings.library_items_per_tenant,
    )
    await _audit(
        ctx,
        auth,
        session,
        AuditAction.LIBRARY_ITEM_PROMOTED,
        tenant_item,
        {"from": item.id, "from_revision": item.latest_revision, "valid": checked.valid},
    )
    return _with_etag(response, tenant_item)


@router.post("/{item_id}:use", response_model=LibraryUseResult, **_op("use_library_item"))
async def use_library_item(
    ctx: Ctx, auth: Auth, session: Session, item_id: ItemId, body: LibraryUseRequest
) -> LibraryUseResult:
    """Compile an item for use, with lineage labels; nothing is saved. A model recipe gives a
    spec fragment (it needs ``spec`` create), a prep preset its steps, a chart template the
    chart applied to ``model_id`` v``model_version``, a workflow template its document."""
    item = await svc.load_item(session, ctx.scope, item_id)
    if item.kind == "model_recipe":
        auth.require(Action.CREATE, ResourceKind.SPEC)
    return await svc.use_item(ctx, session, item, body)
