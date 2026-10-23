"""The tenant catalog view: the base catalog plus the uploads a caller may use (v0.19, D11).

``view = base catalog (image digest) ∪ visible overlay entries``. The base is the immutable
catalog of the image digest (:class:`~mapi_server.services.catalog.CatalogService`); the
overlay entries come from the ``plugin_describes`` rows of the versions
:func:`~mapi_server.services.plugins.gate.visible_versions` lists for the caller: every
``approved`` version of the tenant, plus the caller's own ``kit_passed`` /
``awaiting_approval`` versions while the tenant allows private use. Each overlay entry
carries ``provider="upload:<plugin version id>"`` and the ``overlay_hash`` of its wheel.

Rules:

* A view never reaches the platform-wide ``catalogs`` table and never changes the base:
  specs keep pinning the base ``catalog_digest`` (the overlay facts of a run live in
  ``runs.overlays``, set from :meth:`CatalogView.pins_for`).
* One version per uploaded plugin: when the caller sees several versions of one namespace
  (an approved one and a newer private one), the newest version wins.
* Built-ins win every id collision (impossible by the namespace rule, still checked), and
  an overlay entry outside its version's namespace is dropped. The JSON Schemas, node
  contracts and template documents of overlay entries are checked again here (the describe
  result was checked by ``check_describe`` when it was stored; this is defence in depth).
* Views are cached in process by ``(tenant, user, base digest, visible describe digests)``;
  the same key gives the ``etag`` the catalog routes send (``Cache-Control: private``).

Help me choose and ``GET /v1/catalog/diagnostics`` stay on the base catalog (default d).
The control plane only reads JSON here; it never opens an upload.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from mapi_spec.about import about_issues
from mapi_spec.catalog import Catalog, TemplateCatalogEntry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.namespaces import uploaded_namespace_of
from mapi_spec.plots import PlotSpec
from mapi_spec.plugins import OverlayPin
from mapi_spec.schema_safety import sanitize_schema
from mapi_spec.tenancy import ProjectRole, parse_project_selector
from mapi_spec.workflow import validate_workflow
from mapi_spec.workflow_nodes import (
    NODE_IMPLEMENTATIONS,
    NodeImplementation,
    plugin_contract_issues,
)
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.services.catalog import CatalogService, CatalogSnapshot
from mapi_server.services.plugins.gate import VisibleVersion, visible_versions
from mapi_server.services.plugins.refs import merge_pins, uploaded_ids

__all__ = [
    "UPLOAD_PROVIDER_PREFIX",
    "CatalogView",
    "ViewCatalogs",
    "caller_tenant",
    "catalog_view",
    "clear_view_cache",
    "pins_from",
    "resolve_pins",
    "snapshot_for",
    "upload_provider",
    "view_catalogs",
    "view_owner",
]

log = logging.getLogger(__name__)

UPLOAD_PROVIDER_PREFIX: Final = "upload:"
"""``provider`` of every overlay entry: ``upload:<plugin version id>``."""
_CACHE_SIZE: Final = 128


def upload_provider(version_id: str) -> str:
    """The ``provider`` of the catalog entries of an uploaded version."""
    return f"{UPLOAD_PROVIDER_PREFIX}{version_id}"


def _etag(tenant_id: str, user_id: str | None, base: str, visible: Iterable[str]) -> str:
    h = hashlib.sha256()
    for part in (tenant_id, user_id or "-", base, *visible):
        h.update(part.encode())
        h.update(b"\x00")
    return f'"v-{h.hexdigest()[:32]}"'


@dataclass(frozen=True, slots=True)
class CatalogView:
    """The catalog one caller of one tenant sees.

    ``catalog`` is the union (``base.catalog`` when nothing is visible); ``snapshot`` is a
    :class:`CatalogSnapshot` of it under the **base** digest (``overlay_key`` tells the
    per-snapshot caches apart); ``overlays`` are the visible versions that contributed.
    """

    base: CatalogSnapshot
    catalog: Catalog
    overlays: tuple[VisibleVersion, ...]
    etag: str
    snapshot: CatalogSnapshot
    _by_namespace: Mapping[str, VisibleVersion] = field(default_factory=dict, repr=False)

    def version_of(self, id_: str) -> VisibleVersion | None:
        """The visible version that exports catalog id ``id_`` (``None``: not uploaded, or
        not visible to this caller)."""
        ns = uploaded_namespace_of(id_)
        return None if ns is None else self._by_namespace.get(ns)

    def pins_for(self, ids: Iterable[str]) -> list[OverlayPin]:
        """The overlay pins a run that uses ``ids`` needs (one per uploaded plugin, sorted).

        Ids outside an uploaded namespace need no pin. An uploaded id whose plugin this
        caller cannot use (not approved, someone else's private version, private use
        turned off, withdrawn, removed, or another tenant's) is ``PLUGIN_UNAVAILABLE``:
        the run is refused before it is queued.
        """
        pins: list[OverlayPin] = []
        for id_ in sorted(set(ids)):
            ns = uploaded_namespace_of(id_)
            if ns is None:
                continue
            vv = self._by_namespace.get(ns)
            if vv is None:
                raise MapiError(
                    f"The uploaded plugin {ns} is not available to you. It is not approved "
                    "for the workspace, it is someone else's private upload, or it was "
                    "withdrawn.",
                    code=ErrorCode.PLUGIN_UNAVAILABLE,
                )
            pins.append(OverlayPin(plugin_version_id=vv.version_id, namespace=ns, sha256=vv.sha256))
        return merge_pins(pins)

    def example(self, ref: str) -> PlotSpec | None:
        """The stored example of an uploaded recipe (``id@version`` or bare id), re-checked
        as a :class:`PlotSpec`; ``None`` when the recipe is not uploaded or has none."""
        vv = self.version_of(ref)
        if vv is None:
            return None
        bare = ref.partition("@")[0]
        raw = vv.plot_examples.get(ref) or vv.plot_examples.get(bare)
        if not isinstance(raw, Mapping):
            return None
        try:
            return PlotSpec.model_validate(raw)
        except ValidationError:
            log.warning("unreadable example of %s in plugin version %s", bare, vv.version_id)
            return None

    def service(self, base: CatalogService) -> ViewCatalogs:
        """A :class:`CatalogService` that answers with this view (for the services that
        take one, e.g. ``validate_document``)."""
        return ViewCatalogs(base, self)


class ViewCatalogs(CatalogService):
    """A :class:`CatalogService` bound to one :class:`CatalogView`: ``get``/``require``
    return the view's snapshot (union catalog, base digest) and ``example`` answers uploaded
    recipes from the view. Writes go to the base service: a view is never stored."""

    def __init__(self, base: CatalogService, view: CatalogView) -> None:
        super().__init__(base.image_digest, catalog_path=base.catalog_path)
        self._base = base
        self.view = view

    async def get(self, session: AsyncSession) -> CatalogSnapshot | None:
        return self.view.snapshot

    async def require(self, session: AsyncSession) -> CatalogSnapshot:
        return self.view.snapshot

    async def example(self, session: AsyncSession, ref: str) -> PlotSpec | None:
        if self.view.version_of(ref) is not None:
            return self.view.example(ref)
        return await self._base.example(session, ref)

    async def store(self, session: AsyncSession, catalog: Catalog, **kw: Any) -> CatalogSnapshot:
        return await self._base.store(session, catalog, **kw)

    async def load_file(
        self, session: AsyncSession, *, tenant_id: str, project_id: str
    ) -> CatalogSnapshot | None:
        return await self._base.load_file(session, tenant_id=tenant_id, project_id=project_id)

    def invalidate(self) -> None:
        self._base.invalidate()


# =================================================================================== the union


def _schema_ok(raw: object, where: str) -> bool:
    if not isinstance(raw, Mapping) or not raw:
        return True
    _, issues = sanitize_schema(raw, mode="refuse", where=where)
    return not issues


class _Union:
    """Adds the entries of visible versions to a base catalog (see the module rules)."""

    def __init__(self, base: Catalog) -> None:
        self.base = base
        self.steps = list(base.steps)
        self.models = list(base.models)
        self.plots = list(base.plots)
        self.nodes = list(base.workflow_nodes)
        self.templates = list(base.workflow_templates)
        self.refs = {
            "step": {e.ref for e in base.steps},
            "model": {e.ref for e in base.models},
            "plot": {e.ref for e in base.plots},
        }
        self.ids = {e.id for e in base.workflow_nodes} | set(NODE_IMPLEMENTATIONS)
        self.template_ids = {e.id for e in base.workflow_templates} | set(WORKFLOW_TEMPLATES)
        # templates are checked last, against the node contracts of the whole union
        self.pending: list[tuple[VisibleVersion, TemplateCatalogEntry, dict[str, Any]]] = []
        self.added = 0

    def _skip(self, vv: VisibleVersion, kind: str, id_: str, why: str) -> None:
        log.warning("catalog view: %s %s of plugin version %s left out: %s",
                    kind, id_, vv.version_id, why)  # fmt: skip

    def _mine(self, vv: VisibleVersion, kind: str, id_: str) -> bool:
        if uploaded_namespace_of(id_) != vv.namespace:
            self._skip(vv, kind, id_, f"not in the namespace {vv.namespace}")
            return False
        return True

    def _fresh(self, vv: VisibleVersion, kind: str, ref: str) -> bool:
        seen = self.refs[kind]
        if ref in seen:
            self._skip(vv, kind, ref, "the id is taken")
            return False
        seen.add(ref)
        return True

    def add(self, vv: VisibleVersion) -> None:
        ex = vv.exports
        provider = upload_provider(vv.version_id)
        stamp: dict[str, Any] = {"provider": provider, "overlay_hash": vv.sha256}
        for step in ex.steps:
            if (
                self._mine(vv, "step", step.id)
                and _schema_ok(step.settings_schema, "/settings_schema")
                and self._fresh(vv, "step", step.ref)
            ):
                self.steps.append(step.model_copy(update=stamp))
                self.added += 1
        for model in ex.models:
            man = model.manifest
            if (
                self._mine(vv, "model", model.id)
                and _schema_ok(man.settings_schema, "/settings_schema")
                and _schema_ok(man.fit_schema, "/fit_schema")
                and self._fresh(vv, "model", model.ref)
            ):
                self.models.append(model.model_copy(update=stamp))
                self.added += 1
        for plot in ex.plots:
            if (
                self._mine(vv, "plot", plot.id)
                and _schema_ok(plot.settings_schema, "/settings_schema")
                and self._fresh(vv, "plot", plot.ref)
            ):
                self.plots.append(plot.model_copy(update=stamp))
                self.added += 1
        for node in ex.workflow_nodes:
            self._node(vv, node)
        self.pending.extend((vv, t, stamp) for t in ex.workflow_templates)

    def _node(self, vv: VisibleVersion, node: NodeImplementation) -> None:
        if not self._mine(vv, "node", node.id):
            return
        if node.plugin != vv.namespace:
            self._skip(vv, "node", node.id, "its plugin is not its namespace")
            return
        if node.id in self.ids:
            self._skip(vv, "node", node.id, "the id is taken")
            return
        # the namespace check is the claimed one above (an upload may use only its own)
        problems = [i for i in plugin_contract_issues(node) if i.pointer != "/plugin"]
        if problems:
            self._skip(vv, "node", node.id, problems[0].message)
            return
        self.ids.add(node.id)
        self.nodes.append(node)
        self.added += 1

    def _templates(self, catalog: Catalog) -> list[TemplateCatalogEntry]:
        from mapi_spec.library_workflows import node_contracts

        merged = node_contracts(catalog)
        out: list[TemplateCatalogEntry] = []
        for vv, entry, stamp in self.pending:
            if not self._mine(vv, "template", entry.id):
                continue
            if entry.id in self.template_ids:
                self._skip(vv, "template", entry.id, "the id is taken")
                continue
            if about_issues(entry.about):
                self._skip(vv, "template", entry.id, "its About card is not complete")
                continue
            if not validate_workflow(entry.document, catalog=catalog, nodes=merged).valid:
                self._skip(vv, "template", entry.id, "its workflow document is not valid")
                continue
            self.template_ids.add(entry.id)
            out.append(entry.model_copy(update=stamp))
            self.added += 1
        return out

    def _catalog(self, templates: list[TemplateCatalogEntry]) -> Catalog:
        return Catalog(
            steps=self.steps,
            models=self.models,
            plots=self.plots,
            workflow_nodes=self.nodes,
            workflow_templates=templates,
            diagnostics=list(self.base.diagnostics),
        )

    def result(self) -> Catalog:
        """The union (the base catalog itself when nothing was added)."""
        if not self.added and not self.pending:
            return self.base
        catalog = self._catalog(self.templates)
        extra = self._templates(catalog)
        if not self.added:
            return self.base
        return self._catalog([*self.templates, *extra]) if extra else catalog


def _newest_per_namespace(visible: Iterable[VisibleVersion]) -> tuple[VisibleVersion, ...]:
    """One version per namespace: the newest (version ids are time-ordered ULIDs)."""
    best: dict[str, VisibleVersion] = {}
    for vv in visible:
        seen = best.get(vv.namespace)
        if seen is None or vv.version_id > seen.version_id:
            best[vv.namespace] = vv
    return tuple(best[ns] for ns in sorted(best))


def _build(base: CatalogSnapshot, overlays: tuple[VisibleVersion, ...], etag: str) -> CatalogView:
    union = _Union(base.catalog)
    for vv in overlays:
        union.add(vv)
    catalog = union.result()
    if catalog is base.catalog:
        snapshot = base
    else:
        snapshot = CatalogSnapshot(
            base.image_digest,
            base.digest,
            catalog,
            base.generated_at,
            base.source,
            overlay_key=etag,
        )
    return CatalogView(
        base=base,
        catalog=catalog,
        overlays=overlays,
        etag=etag,
        snapshot=snapshot,
        _by_namespace={vv.namespace: vv for vv in overlays},
    )


_views: OrderedDict[tuple[str, ...], CatalogView] = OrderedDict()


def clear_view_cache() -> None:
    """Forget every cached view (tests; the cache key already changes with the data)."""
    _views.clear()


async def catalog_view(
    ctx: ServerContext, session: AsyncSession, *, tenant_id: str, user_id: str | None
) -> CatalogView:
    """The catalog ``user_id`` sees in ``tenant_id`` (``None``: nobody in particular, so
    approved uploads only). ``CatalogUnavailable`` (503) while the image has no catalog.

    ``session`` reads the base catalog (any session does). The plugin rows are read in a
    session of ``tenant_id`` of their own (row-level security; inside a request the platform
    session is the gateway role, which cannot read them), for that tenant only
    (:func:`~mapi_server.services.plugins.gate.visible_versions`).
    """
    base = await ctx.catalogs.require(session)
    visible: list[VisibleVersion] = []
    if ctx.settings.plugins_enabled:
        async with ctx.db.tenant_session(tenant_id) as rows:
            visible = await visible_versions(
                rows, tenant_id=tenant_id, user_id=user_id, base_image_digest=base.image_digest
            )
    overlays = _newest_per_namespace(visible)
    digests = tuple(f"{vv.version_id}={vv.describe_digest}" for vv in overlays)
    key = (tenant_id, user_id or "-", base.digest, *digests)
    hit = _views.get(key)
    if hit is not None and hit.base is base:
        _views.move_to_end(key)
        return hit
    view = _build(base, overlays, _etag(tenant_id, user_id, base.digest, digests))
    _views[key] = view
    while len(_views) > _CACHE_SIZE:
        _views.popitem(last=False)
    return view


async def view_catalogs(
    ctx: ServerContext, session: AsyncSession, *, tenant_id: str, user_id: str | None
) -> CatalogService:
    """The :class:`CatalogService` to validate a document of ``user_id`` in ``tenant_id``
    against: the tenant view, or ``ctx.catalogs`` itself while the image has no catalog
    (its ``require`` then answers 503 as before)."""
    if await ctx.catalogs.get(session) is None:
        return ctx.catalogs
    view = await catalog_view(ctx, session, tenant_id=tenant_id, user_id=user_id)
    return view.service(ctx.catalogs)


def view_owner(ctx: ServerContext) -> tuple[str | None, str | None]:
    """``(tenant, user)`` whose view a request sees: the request's tenant and caller.
    ``(None, None)`` outside a request or on routes without a tenant (the caller then gets
    the base catalog unless the route resolves a tenant itself)."""
    auth = ctx.request
    if auth is None:
        return None, None
    tenant = auth.tenant_id or (auth.scope.tenant_id if auth.scope is not None else None)
    return tenant, auth.user_id


async def snapshot_for(
    ctx: ServerContext,
    session: AsyncSession,
    *,
    tenant_id: str | None,
    user_id: str | None,
) -> CatalogSnapshot | None:
    """The snapshot ``user_id`` sees in ``tenant_id``: the base catalog without a tenant,
    ``None`` while the image has no catalog."""
    base = await ctx.catalogs.get(session)
    if base is None or tenant_id is None:
        return base
    return (await catalog_view(ctx, session, tenant_id=tenant_id, user_id=user_id)).snapshot


def pins_from(catalogs: CatalogService, ids: Iterable[str]) -> list[OverlayPin]:
    """The pins of ``ids`` in the view behind ``catalogs`` (a :class:`ViewCatalogs` from
    :func:`view_catalogs`). With the plain base service no upload is visible, so any
    uploaded id is ``PLUGIN_UNAVAILABLE``."""
    if isinstance(catalogs, ViewCatalogs):
        return catalogs.view.pins_for(ids)
    wanted = uploaded_ids(ids)
    if wanted:
        ns = uploaded_namespace_of(sorted(wanted)[0])
        raise MapiError(
            f"The uploaded plugin {ns} is not available to you.",
            code=ErrorCode.PLUGIN_UNAVAILABLE,
        )
    return []


async def resolve_pins(
    ctx: ServerContext,
    session: AsyncSession,
    *,
    ids: Iterable[str] = (),
    inherited: Sequence[OverlayPin] = (),
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> list[OverlayPin]:
    """The overlay pins of a run: ``inherited`` (a model version's lineage) plus the pins
    of the uploaded ids among ``ids`` in the view of ``tenant_id``/``user_id`` (default:
    the run's project tenant and the request's caller). The view is built only when an
    uploaded id is named."""
    wanted = uploaded_ids(ids)
    pins: list[OverlayPin] = []
    if wanted:
        if tenant_id is None:
            tenant_id = ctx.scope.tenant_id
            user_id = ctx.request.user_id if ctx.request is not None else None
        view = await catalog_view(ctx, session, tenant_id=tenant_id, user_id=user_id)
        pins = view.pins_for(wanted)
    return merge_pins(inherited, pins)


def caller_tenant(ctx: ServerContext, project_header: str | None = None) -> str | None:
    """The tenant whose uploads a catalog request sees, for routes that are not scoped to a
    project (``GET /v1/catalog/...``, the node and template galleries).

    In order: the request's own tenant; an API token's tenant; the tenant named by the
    ``X-Mapi-Project`` header when the caller is a member of it (a client membership does
    not count); the caller's only tenant; the single tenant of a single-tenant
    deployment. ``None`` (the base catalog) otherwise, and outside requests.
    """
    auth = ctx.request
    if auth is None:
        return None
    tenant, _ = view_owner(ctx)
    if tenant is not None:
        return tenant
    if auth.token is not None:
        return auth.token.tenant_id
    members = {mm.tenant_id for mm in auth.memberships if mm.role is not ProjectRole.CLIENT}
    if project_header:
        try:
            named = parse_project_selector(project_header).tenant_id
        except MapiError:
            named = None
        if named is None and len(members) == 1:
            named = next(iter(members))
        return named if named in members else None
    if len(members) == 1:
        return next(iter(members))
    if not ctx.settings.multi_tenant:
        return ctx.settings.tenant_id
    return None
