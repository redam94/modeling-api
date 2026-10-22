"""Library items and their immutable revisions (v0.18 D1, D2).

An item lives in one project (``scope="project"``) or in the whole tenant (``scope="tenant"``,
``project_id`` NULL). A request in a project reads the project's items and the tenant's items;
everything else answers ``404``. Every write is a new revision (``If-Match`` against the
item's ETag ``"r<latest_revision>"``); items are archived, never deleted. A tenant admin can
promote a project item: its latest revision is copied into a new tenant item.

The caps (``library_items_per_tenant``, archived items included, and
``library_revisions_per_item``) answer ``409``. Names are unique per kind in their scope
(``409``). A document that does not compile against the current catalog is stored with
``valid=false`` and its issues, except a workflow template, which is refused (a team template
must start a valid workflow). All reads and writes run in the request's tenant session
(row-level security).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from mapi_spec.api import new_id
from mapi_spec.catalog import Catalog
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.library import (
    LibraryDoc,
    LibraryItem,
    LibraryKind,
    LibraryRef,
    LibraryRevision,
    LibraryScope,
    ModelRecipeDoc,
    WorkflowTemplateDoc,
    library_doc_hash,
    library_kind,
    parse_library_doc,
)
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS
from pydantic import JsonValue
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.library.compile import Checked, RefMap, check_with, ref_key, refusal
from mapi_server.services.runs import Scope, now

__all__ = [
    "ITEM_ID_PREFIX",
    "SYSTEM_AUTHOR",
    "archive_item",
    "create_item",
    "create_item_system",
    "etag_of",
    "item_cursor",
    "item_dto",
    "list_items",
    "list_revisions",
    "load_item",
    "load_revision",
    "promote_item",
    "read_refs",
    "refuse_project_refs",
    "resolve_ref",
    "revise_item",
    "revision_dto",
]

ITEM_ID_PREFIX: Final = "lib"
SYSTEM_AUTHOR: Final = "system"
"""``created_by`` of the items the platform seeds (the sample project's library, D20)."""


# ------------------------------------------------------------------------------------- DTOs


def etag_of(row: m.LibraryItem) -> str:
    return f'"r{row.latest_revision}"'


def item_dto(row: m.LibraryItem) -> LibraryItem:
    return LibraryItem(
        id=row.id,
        kind=row.kind,
        scope=row.scope,
        project_id=row.project_id,
        name=row.name,
        title=row.title,
        summary=row.summary,
        tags=list(row.tags or []),
        latest_revision=row.latest_revision,
        archived=row.archived,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        etag=etag_of(row),
    )


def revision_dto(row: m.LibraryItemRevision) -> LibraryRevision:
    return LibraryRevision(
        item_id=row.item_id,
        revision=row.revision,
        document=row.document,
        doc_hash=row.doc_hash,
        valid=row.valid,
        issues=[Issue.model_validate(i) for i in row.issues or []],
        compiled=row.compiled,
        catalog_digest=row.catalog_digest,
        created_by=row.created_by,
        created_at=row.created_at,
        message=row.message,
    )


# ------------------------------------------------------------------------------------ reads


def _visible(scope: Scope) -> ColumnElement[bool]:
    """The items a request in ``scope`` may read: its project's and its tenant's."""
    li = m.LibraryItem
    return and_(
        li.tenant_id == scope.tenant_id,
        or_(li.project_id == scope.project_id, li.project_id.is_(None)),
    )


async def load_item(
    session: AsyncSession, scope: Scope, item_id: str, *, for_update: bool = False
) -> m.LibraryItem:
    """An item ``scope`` can read (``404`` otherwise, also for other tenants' items)."""
    stmt = select(m.LibraryItem).where(m.LibraryItem.id == item_id, _visible(scope))
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("library item", item_id)
    return row


async def load_revision(
    session: AsyncSession, item: m.LibraryItem, revision: int | None = None
) -> m.LibraryItemRevision:
    """One revision of ``item`` (its latest by default; ``404`` when there is no such one)."""
    rev = item.latest_revision if revision is None else revision
    row = await session.get(m.LibraryItemRevision, (item.id, rev))
    if row is None:
        raise NotFoundError("library revision", f"{item.id} r{rev}")
    return row


def _bad_cursor() -> MapiError:
    return MapiError(
        "the cursor is invalid", code=ErrorCode.SCHEMA_INVALID, pointer="/query/cursor"
    )


def _like(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_items(
    session: AsyncSession,
    scope: Scope,
    *,
    kind: LibraryKind | None = None,
    item_scope: LibraryScope | None = None,
    q: str | None = None,
    include_archived: bool = False,
    after: Any = None,
    limit: int,
) -> list[m.LibraryItem]:
    """The project's and the tenant's items, most recently changed first (up to ``limit + 1``
    rows: one more tells the caller there is a next page). ``q`` matches the name or the
    title (case-insensitive); archived items only with ``include_archived``. ``after`` is the
    decoded cursor: ``[updated_at ISO, id]`` of the last row of the previous page."""
    li = m.LibraryItem
    stmt = select(li).where(_visible(scope))
    if kind is not None:
        stmt = stmt.where(li.kind == kind)
    if item_scope is not None:
        stmt = stmt.where(li.scope == item_scope)
    if not include_archived:
        stmt = stmt.where(li.archived.is_(False))
    if q:
        pattern = _like(q)
        stmt = stmt.where(or_(li.name.ilike(pattern), li.title.ilike(pattern)))
    if after is not None:
        if not (
            isinstance(after, list) and len(after) == 2 and all(isinstance(a, str) for a in after)
        ):
            raise _bad_cursor()
        try:
            at = datetime.fromisoformat(after[0])
        except ValueError:
            raise _bad_cursor() from None
        stmt = stmt.where(or_(li.updated_at < at, and_(li.updated_at == at, li.id < after[1])))
    stmt = stmt.order_by(li.updated_at.desc(), li.id.desc()).limit(limit + 1)
    return list((await session.execute(stmt)).scalars())


def item_cursor(row: m.LibraryItem) -> list[str]:
    """The cursor key after ``row`` in :func:`list_items` order."""
    return [row.updated_at.isoformat(), row.id]


async def list_revisions(
    session: AsyncSession, item: m.LibraryItem, *, after: Any = None, limit: int
) -> list[m.LibraryItemRevision]:
    """The revisions of ``item``, newest first (up to ``limit + 1``; ``after`` is the last
    revision number of the previous page)."""
    rv = m.LibraryItemRevision
    stmt = select(rv).where(rv.item_id == item.id)
    if after is not None:
        if not isinstance(after, int) or isinstance(after, bool):
            raise _bad_cursor()
        stmt = stmt.where(rv.revision < after)
    return list(
        (await session.execute(stmt.order_by(rv.revision.desc()).limit(limit + 1))).scalars()
    )


async def resolve_ref(
    session: AsyncSession, scope: Scope, ref: LibraryRef
) -> tuple[LibraryKind, LibraryDoc] | None:
    """The document a :class:`~mapi_spec.library.LibraryRef` names, if ``scope`` can read the
    item (its project's or its tenant's; archived items still resolve, so an existing recipe
    keeps working) and the revision exists. ``None`` otherwise."""
    row = (
        await session.execute(
            select(m.LibraryItem).where(m.LibraryItem.id == ref.item, _visible(scope))
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    revision = row.latest_revision if ref.revision == "latest" else ref.revision
    rev = await session.get(m.LibraryItemRevision, (row.id, revision))
    if rev is None:
        return None
    try:
        return row.kind, parse_library_doc(rev.document)  # type: ignore[return-value]
    except MapiError:  # stored documents parsed when they were written; be safe anyway
        return None


async def read_refs(session: AsyncSession, scope: Scope, doc: LibraryDoc) -> RefMap:
    """Every item a model recipe references (``prep``, ``charts``), read as ``scope`` can."""
    if not isinstance(doc, ModelRecipeDoc):
        return {}
    refs = [*([doc.prep] if doc.prep is not None else []), *doc.charts]
    out: dict[Any, Any] = {}
    for ref in refs:
        key = ref_key(ref)
        if key not in out:
            out[key] = await resolve_ref(session, scope, ref)
    return out


async def refuse_project_refs(session: AsyncSession, scope: Scope, doc: LibraryDoc) -> None:
    """Refuse a model recipe about to become a tenant item (created with ``scope: tenant``,
    or promoted) that references an item of a project: every project of the tenant reads a
    tenant item, but only that one project can read what it references (``LIBRARY_INVALID``,
    one issue per reference, pointing at its ``item``)."""
    if not isinstance(doc, ModelRecipeDoc):
        return
    named = [*([("/prep/item", doc.prep)] if doc.prep is not None else [])]
    named += [(f"/charts/{i}/item", ref) for i, ref in enumerate(doc.charts)]
    issues: list[Issue] = []
    for pointer, ref in named:
        project_id = (
            await session.execute(
                select(m.LibraryItem.project_id).where(
                    m.LibraryItem.id == ref.item, _visible(scope)
                )
            )
        ).scalar_one_or_none()
        if project_id is not None:
            issues.append(
                Issue(
                    code=ErrorCode.LIBRARY_INVALID,
                    severity=Severity.ERROR,
                    pointer=pointer,
                    message=(
                        "A tenant item can only use tenant items: this one belongs to a "
                        "project. Promote it first and point to the promoted item."
                    ),
                )
            )
    if issues:
        raise refusal(issues, "the model recipe cannot be shared with the tenant")


# ----------------------------------------------------------------------------------- writes


async def _check_cap(session: AsyncSession, tenant_id: str, cap: int) -> None:
    count = (
        await session.execute(
            select(func.count())
            .select_from(m.LibraryItem)
            .where(m.LibraryItem.tenant_id == tenant_id)
        )
    ).scalar_one()
    if int(count) >= cap:
        raise Conflict(
            f"this tenant has {cap} library items, the most allowed (archived items count); "
            "reuse or revise an item instead"
        )


def _refuse_invalid_template(doc: LibraryDoc, checked: Checked) -> None:
    """Team workflow templates must be valid when saved (D8); other kinds are stored with
    ``valid=false``."""
    if isinstance(doc, WorkflowTemplateDoc) and not checked.valid:
        raise refusal(checked.issues, "the workflow template is not valid")


def _issues_json(issues: Sequence[Issue]) -> list[Any]:
    return [i.model_dump(mode="json", exclude_none=True) for i in issues]


def _name_taken(kind: str, name: str, item_scope: str) -> Conflict:
    where = "this tenant" if item_scope == "tenant" else "this project"
    return Conflict(f"{where} already has a {kind.replace('_', ' ')} named {name!r}")


async def _insert(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str | None,
    doc: LibraryDoc,
    stored: Mapping[str, JsonValue],
    checked: Checked,
    created_by: str,
    message: str | None,
) -> tuple[m.LibraryItem, m.LibraryItemRevision]:
    at = now()
    meta = doc.metadata
    item_scope = "project" if project_id is not None else "tenant"
    kind = library_kind(doc)
    item = m.LibraryItem(
        id=new_id(ITEM_ID_PREFIX),
        tenant_id=tenant_id,
        project_id=project_id,
        scope=item_scope,
        kind=kind,
        name=meta.name,
        title=meta.title,
        summary=meta.summary,
        tags=list(meta.tags),
        latest_revision=1,
        archived=False,
        created_by=created_by,
        created_at=at,
        updated_at=at,
    )
    revision = m.LibraryItemRevision(
        item_id=item.id,
        revision=1,
        tenant_id=tenant_id,
        project_id=project_id,
        document=dict(stored),
        compiled=checked.compiled,
        doc_hash=library_doc_hash(doc),
        valid=checked.valid,
        issues=_issues_json(checked.issues),
        catalog_digest=checked.catalog_digest,
        message=message,
        created_by=created_by,
        created_at=at,
    )
    try:
        async with session.begin_nested():
            session.add(item)
            await session.flush()
            session.add(revision)
            await session.flush()
    except IntegrityError:
        raise _name_taken(kind, meta.name, item_scope) from None
    return item, revision


async def create_item(
    session: AsyncSession,
    scope: Scope,
    doc: LibraryDoc,
    stored: Mapping[str, JsonValue],
    checked: Checked,
    *,
    item_scope: LibraryScope,
    created_by: str,
    message: str | None = None,
    max_items: int,
) -> tuple[m.LibraryItem, m.LibraryItemRevision]:
    """A new item with revision 1 in ``scope``'s project (or its tenant). The caller checked
    the permissions (``tenant_library`` create for a tenant item) and the document."""
    _refuse_invalid_template(doc, checked)
    await _check_cap(session, scope.tenant_id, max_items)
    return await _insert(
        session,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id if item_scope == "project" else None,
        doc=doc,
        stored=stored,
        checked=checked,
        created_by=created_by,
        message=message,
    )


def _check_if_match(item: m.LibraryItem, if_match: str | None) -> None:
    if if_match is None or if_match.strip() == "*":
        return
    tags = [t.strip().removeprefix("W/") for t in if_match.split(",")]
    if etag_of(item) not in tags:
        raise MapiError(
            f"the library item changed: it is at {etag_of(item)}; reload it and try again",
            code=ErrorCode.PRECONDITION_FAILED,
        )


async def revise_item(
    session: AsyncSession,
    item: m.LibraryItem,
    doc: LibraryDoc,
    stored: Mapping[str, JsonValue],
    checked: Checked,
    *,
    if_match: str | None,
    created_by: str,
    message: str | None = None,
    max_revisions: int,
) -> m.LibraryItemRevision:
    """Add revision ``latest + 1`` to ``item`` (locked by the caller). The document's
    ``metadata`` updates the item's name, title, summary and tags; its kind cannot change."""
    _check_if_match(item, if_match)
    if item.archived:
        raise Conflict("the library item is archived; unarchive it before you change it")
    kind, item_scope = library_kind(doc), item.scope
    if kind != item.kind:
        raise MapiError(
            f"the item is a {item.kind.replace('_', ' ')}; a revision cannot change its kind",
            code=ErrorCode.LIBRARY_INVALID,
            pointer="/kind",
        )
    _refuse_invalid_template(doc, checked)
    if item.latest_revision >= max_revisions:
        raise Conflict(
            f"the library item has {max_revisions} revisions, the most allowed; save it as a "
            "new item instead"
        )
    at = now()
    meta = doc.metadata
    revision = m.LibraryItemRevision(
        item_id=item.id,
        revision=item.latest_revision + 1,
        tenant_id=item.tenant_id,
        project_id=item.project_id,
        document=dict(stored),
        compiled=checked.compiled,
        doc_hash=library_doc_hash(doc),
        valid=checked.valid,
        issues=_issues_json(checked.issues),
        catalog_digest=checked.catalog_digest,
        message=message,
        created_by=created_by,
        created_at=at,
    )
    try:
        async with session.begin_nested():
            item.name = meta.name
            item.title = meta.title
            item.summary = meta.summary
            item.tags = list(meta.tags)
            item.latest_revision = revision.revision
            item.updated_at = at
            session.add(revision)
            await session.flush()
    except IntegrityError:
        # the savepoint rolled back and expired ``item``: report with the values read before
        raise _name_taken(kind, meta.name, item_scope) from None
    return revision


async def archive_item(session: AsyncSession, item: m.LibraryItem, *, archived: bool) -> bool:
    """Archive or unarchive ``item``; ``True`` when that changed it."""
    if item.archived is archived:
        return False
    item.archived = archived
    item.updated_at = now()
    await session.flush()
    return True


async def promote_item(
    session: AsyncSession,
    item: m.LibraryItem,
    checked: Checked,
    *,
    created_by: str,
    max_items: int,
) -> tuple[m.LibraryItem, m.LibraryItemRevision]:
    """A new tenant item whose revision 1 is ``item``'s latest document (re-checked against
    the current catalog: ``checked``). The project item stays as it is."""
    if item.scope == "tenant":
        raise Conflict("the library item is already shared with the whole tenant")
    latest = await load_revision(session, item)
    doc = parse_library_doc(latest.document)
    _refuse_invalid_template(doc, checked)
    await _check_cap(session, item.tenant_id, max_items)
    return await _insert(
        session,
        tenant_id=item.tenant_id,
        project_id=None,
        doc=doc,
        stored=latest.document,
        checked=checked,
        created_by=created_by,
        message=f"promoted from {item.id} r{item.latest_revision}",
    )


async def create_item_system(
    session: AsyncSession,
    tenant_id: str,
    project_id: str,
    doc: LibraryDoc | Mapping[str, Any],
    *,
    catalog: Catalog | None = None,
    catalog_digest: str | None = None,
    max_items: int | None = None,
) -> m.LibraryItem | None:
    """Seed a project item as the platform (``created_by="system"``): the sample project's
    library (D20). No permission check and no audit; the caller owns ``session`` (a tenant
    session of ``tenant_id``).

    Idempotent: when the project already has an item of that kind and name, that item is
    returned unchanged. With ``catalog`` the document is checked like a user's (an invalid
    workflow template is refused); without one it is stored unchecked (``catalog_digest``
    NULL, one info issue). Returns ``None`` when ``max_items`` is given and the tenant is at
    that cap (a seed never fails a sample)."""
    parsed = _as_doc(doc)
    kind = library_kind(parsed)
    li = m.LibraryItem
    existing = (
        await session.execute(
            select(li).where(
                li.tenant_id == tenant_id,
                li.project_id == project_id,
                li.kind == kind,
                li.name == parsed.metadata.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    if max_items is not None:
        try:
            await _check_cap(session, tenant_id, max_items)
        except Conflict:
            return None
    if catalog is not None:
        # a seed references no other item: no refs to read; no example (checked at use)
        issues, compiled = check_with(parsed, catalog, nodes=NODE_IMPLEMENTATIONS)
        checked = Checked(issues=issues, compiled=compiled, catalog_digest=catalog_digest)
        _refuse_invalid_template(parsed, checked)
    else:
        checked = Checked(
            issues=[
                Issue(
                    code=ErrorCode.LIBRARY_INVALID,
                    severity=Severity.INFO,
                    message="The document is checked against the catalog when it is used.",
                )
            ]
        )
    stored = parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)
    item, _ = await _insert(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        doc=parsed,
        stored=stored,
        checked=checked,
        created_by=SYSTEM_AUTHOR,
        message=None,
    )
    return item


def _as_doc(doc: LibraryDoc | Mapping[str, Any]) -> LibraryDoc:
    if isinstance(doc, Mapping):
        return parse_library_doc(doc)
    return doc
