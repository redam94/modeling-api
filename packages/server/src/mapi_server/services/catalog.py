"""The static catalog (steps, models, plots) the control plane validates against.

Catalog JSON is produced *outside* the control plane — by ``mapi-runner describe`` at image
build time (``MAPI_CATALOG_PATH``) or by a sandboxed ``describe`` run — and cached in the
``catalogs`` table per **image digest** (``sha256`` of the image's ``uv.lock``; in a source
checkout also of the workspace sources, :func:`mapi_server.settings.checkout_image_digest`).
The server only ever parses it as JSON with :class:`mapi_spec.catalog.Catalog`; it never
imports step, model or plot code.

v0.18: every catalog is *hardened* before the control plane uses it (:func:`harden_catalog`,
run when a catalog is parsed, stored or loaded): the JSON Schemas a plugin wrote (model
settings and fit schemas, step settings, plot settings, workflow node parameters) go through
:func:`mapi_spec.schema_safety.sanitize_schema` in ``strip`` mode, so an unsafe ``pattern``,
a remote ``$ref`` or a huge schema never reaches ``jsonschema``; plugin workflow nodes that
break the plugin rules and plugin templates that do not validate against the merged node
contracts are left out. Each change is recorded as a :class:`CatalogDiagnostic`
(``GET /v1/catalog/diagnostics``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from mapi_spec.about import about_issues
from mapi_spec.catalog import (
    CATALOG_DIAGNOSTICS_MAX,
    Catalog,
    CatalogDiagnostic,
    DiagnosticCode,
    TemplateCatalogEntry,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.namespaces import check_namespace
from mapi_spec.plots import PlotExamples, PlotSpec
from mapi_spec.run import catalog_digest
from mapi_spec.schema_safety import SETTINGS_SCHEMA_MAX_BYTES, sanitize_schema
from mapi_spec.workflow import validate_workflow
from mapi_spec.workflow_nodes import (
    NODE_IMPLEMENTATIONS,
    NodeImplementation,
    plugin_contract_issues,
)
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from mapi_server.db import models as m
from mapi_server.jsonlimits import loads_limited

__all__ = [
    "CatalogService",
    "CatalogSnapshot",
    "CatalogUnavailable",
    "harden_catalog",
    "parse_catalog",
]

log = logging.getLogger(__name__)

MAX_CATALOG_BYTES = 16 * 1024 * 1024
_TEXT_MAX: Final = 500
"""The longest ``message`` / ``untrusted_text`` of a diagnostic."""


class CatalogUnavailable(MapiError):
    """No catalog is cached for the current image digest yet (503, retryable)."""

    default_code = ErrorCode.INFRA_ERROR


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    image_digest: str
    digest: str
    catalog: Catalog
    generated_at: datetime | None
    source: str
    overlay_key: str = ""
    """v0.19: empty for the base catalog of the image digest; the view key of a tenant
    catalog view (:mod:`mapi_server.services.plugins.view`), whose ``catalog`` adds the
    caller's visible uploads under the same base ``digest``. Caches keyed by the digest
    alone must add it."""

    @property
    def cache_key(self) -> str:
        """``digest`` for the base catalog, ``digest + overlay_key`` for a tenant view."""
        return self.digest if not self.overlay_key else f"{self.digest}+{self.overlay_key}"

    @property
    def diagnostics(self) -> list[CatalogDiagnostic]:
        """What the runner could not load and what :func:`harden_catalog` changed (v0.18)."""
        return self.catalog.diagnostics


# ========================================================================= hardening (v0.18)


def _clip(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= _TEXT_MAX else text[: _TEXT_MAX - 1] + "\u2026"


class _Hardener:
    """One pass of :func:`harden_catalog` over the JSON form of a catalog."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.data: dict[str, Any] = catalog.model_dump(mode="json")
        self.found: list[CatalogDiagnostic] = []
        self.changed = False

    def note(
        self,
        code: DiagnosticCode,
        group: str,
        id_: str | None,
        message: str,
        *,
        provider: str | None = None,
        untrusted: str | None = None,
    ) -> None:
        self.found.append(
            CatalogDiagnostic(
                code=code,
                group=group,
                distribution=provider,
                id=id_[:128] if id_ else None,
                message=_clip(message) or code,
                untrusted_text=_clip(untrusted),
            )
        )

    def schema(
        self,
        holder: dict[str, Any],
        key: str,
        *,
        where: str,
        group: str,
        id_: str,
        what: str,
        provider: str | None,
        max_bytes: int | None = None,
    ) -> None:
        """Strip the unsafe parts of ``holder[key]`` (``max_bytes``: drop the schema when
        it is still larger)."""
        raw = holder.get(key)
        if not isinstance(raw, Mapping) or not raw:
            return
        safe, issues = sanitize_schema(raw, mode="strip", where=where)
        too_big = (
            max_bytes is not None
            and len(json.dumps(safe, separators=(",", ":"), ensure_ascii=False).encode())
            > max_bytes
        )
        if not too_big and (not issues or safe == raw):
            # (a schema already stripped can still report its emptied too-deep subschemas;
            # nothing changes, so a second pass is a no-op)
            return
        self.changed = True
        if provider is None:
            # a built-in schema must never trip the rules (a test checks it): a bug to fix
            log.warning(
                "built-in %s of %s has unsafe parts at %s",
                what,
                id_,
                [i.pointer for i in issues[:5]],
            )
        if too_big:
            holder.pop(key, None)
            self.note(
                "schema_unsafe",
                group,
                id_,
                f"The {what} is larger than {(max_bytes or 0) // 1024} KiB after the unsafe parts "
                "were removed; it was left out.",
                provider=provider,
            )
            return
        if max_bytes is not None and not safe:
            holder.pop(key, None)  # nothing usable left: the entry has no settings form
        else:
            holder[key] = safe
        first = issues[0]
        more = f" ({len(issues)} problems)" if len(issues) > 1 else ""
        # the pointer holds property names the plugin chose: untrusted, like the value
        where_ = first.pointer or "/"
        self.note(
            "schema_unsafe",
            group,
            id_,
            f"Unsafe parts of the {what} were removed{more}. First: {first.message}",
            provider=provider,
            untrusted=f"{where_}: {first.untrusted_text}" if first.untrusted_text else where_,
        )

    def schemas(self) -> None:
        for i, model in enumerate(self.data.get("models", [])):
            manifest = model["manifest"]
            ref = f"{manifest.get('id')}@{manifest.get('version')}"
            for key, what in (("settings_schema", "settings schema"), ("fit_schema", "fit schema")):
                self.schema(manifest, key, where=f"/models/{i}/manifest/{key}",
                            group="mapi.models", id_=ref, what=what,
                            provider=model.get("provider"))  # fmt: skip
        for i, step in enumerate(self.data.get("steps", [])):
            ref = f"{step.get('id')}@{step.get('version')}"
            self.schema(step, "settings_schema", where=f"/steps/{i}/settings_schema",
                        group="mapi.steps", id_=ref, what="settings schema",
                        provider=step.get("provider"))  # fmt: skip
        for i, plot in enumerate(self.data.get("plots", [])):
            ref = f"{plot.get('id')}@{plot.get('version')}"
            self.schema(plot, "settings_schema", where=f"/plots/{i}/settings_schema",
                        group="mapi.plots", id_=ref, what="settings schema",
                        provider=plot.get("provider"),
                        max_bytes=SETTINGS_SCHEMA_MAX_BYTES)  # fmt: skip

    def nodes(self) -> dict[str, NodeImplementation]:
        """Drop the plugin node contracts the sandbox may not run; the merged contracts."""
        merged: dict[str, NodeImplementation] = dict(NODE_IMPLEMENTATIONS)
        kept: list[Any] = []
        for contract in self.catalog.workflow_nodes:
            issues = plugin_contract_issues(contract)
            if not issues:
                kept.append(contract.model_dump(mode="json"))
                merged[contract.id] = contract
                continue
            self.changed = True
            first = issues[0]
            more = f" ({len(issues)} problems)" if len(issues) > 1 else ""
            self.note(
                _node_code(first, contract),
                "mapi.workflow_nodes",
                contract.id,
                f"The workflow node is hidden: it breaks the plugin node rules{more}. "
                f"First: {first.message}",
            )
        if len(kept) != len(self.catalog.workflow_nodes):
            self.data["workflow_nodes"] = kept
        return merged

    def templates(self, nodes: Mapping[str, NodeImplementation]) -> None:
        """Drop the plugin templates that collide, use a reserved namespace, lack a complete
        About card or do not validate against the merged ``nodes``."""
        kept: list[Any] = []
        for entry in self.catalog.workflow_templates:
            problem = self.template_problem(entry, nodes)
            if problem is None:
                kept.append(entry.model_dump(mode="json"))
                continue
            self.changed = True
            code, message, untrusted = problem
            self.note(code, "mapi.workflow_templates", entry.id, message,
                      provider=entry.provider, untrusted=untrusted)  # fmt: skip
        if len(kept) != len(self.catalog.workflow_templates):
            self.data["workflow_templates"] = kept

    def template_problem(
        self, entry: TemplateCatalogEntry, nodes: Mapping[str, NodeImplementation]
    ) -> tuple[DiagnosticCode, str, str | None] | None:
        hidden = "The workflow template is hidden from the gallery"
        if entry.id in WORKFLOW_TEMPLATES:
            return "id_collision", f"{hidden}: a built-in template has the same id.", None
        namespace = entry.id.partition("_")[0]
        reason = check_namespace(entry.id, entry.provider, plugin=namespace)
        if reason is not None:
            return "namespace_reserved", f"{hidden}: {reason}", None
        missing = about_issues(entry.about)
        if missing:
            return "about_incomplete", f"{hidden}: its About card is not complete.", missing[0]
        result = validate_workflow(entry.document, catalog=self.catalog, nodes=nodes)
        if not result.valid:
            first = next((i for i in result.issues if i.is_error), None)
            return (
                "template_invalid",
                f"{hidden}: its workflow document is not valid.",
                None if first is None else f"{first.pointer or '/'}: {first.message}",
            )
        return None

    def result(self) -> Catalog:
        if not self.changed:
            return self.catalog
        room = max(CATALOG_DIAGNOSTICS_MAX - len(self.catalog.diagnostics), 0)
        found = self.found[:room]
        if len(self.found) > room:
            # the runner's rows come first; the list is capped (the changes still apply)
            log.warning("catalog: %d diagnostics not listed", len(self.found) - room)
        self.data["diagnostics"] = [
            *(d.model_dump(mode="json") for d in self.catalog.diagnostics),
            *(d.model_dump(mode="json") for d in found),
        ]
        return Catalog.model_validate(self.data)


def _node_code(first: Issue, contract: NodeImplementation) -> DiagnosticCode:
    if first.pointer == "/plugin":
        return "namespace_reserved"
    if first.pointer == "/id" and contract.id in NODE_IMPLEMENTATIONS:
        return "id_collision"
    if first.code is ErrorCode.ABOUT_INCOMPLETE:
        return "about_incomplete"
    if first.code is ErrorCode.SCHEMA_UNSAFE:
        return "schema_unsafe"
    return "node_contract_invalid"


def harden_catalog(catalog: Catalog) -> Catalog:
    """The catalog the control plane may use (v0.18; pure, JSON only).

    * Every JSON Schema a plugin wrote (model ``settings_schema``/``fit_schema``, step
      ``settings_schema``, plot ``settings_schema``) is sanitized in ``strip`` mode; a plot
      settings schema still larger than 16 KiB is left out.
    * Plugin workflow nodes that break :func:`~mapi_spec.workflow_nodes.plugin_contract_issues`
      (reserved namespace, built-in id, kind, sandbox only, ports, About card, unsafe
      parameter schema) are left out.
    * Plugin workflow templates that collide with a built-in, use a namespace the provider may
      not use, lack a complete About card or do not validate against the built-in nodes plus
      the remaining plugin nodes are left out.

    Each change appends a :class:`CatalogDiagnostic` (at most 500 in all). A catalog that
    needs no change is returned as is, so the digest of a built-in catalog never moves. The
    pass is idempotent.
    """
    h = _Hardener(catalog)
    h.schemas()
    nodes = h.nodes()
    h.templates(nodes)
    return h.result()


def parse_catalog(raw: bytes | str | dict[str, Any]) -> Catalog:
    """Validate catalog JSON (size-capped) and harden it (:func:`harden_catalog`); raises
    ``ValueError`` on any problem."""
    if isinstance(raw, bytes | str):
        if len(raw) > MAX_CATALOG_BYTES:
            raise ValueError(f"catalog JSON exceeds {MAX_CATALOG_BYTES} bytes")
        try:
            data = loads_limited(raw)
        except ValueError as exc:
            raise ValueError(f"catalog is not valid JSON: {exc}") from exc
    else:
        data = raw
    try:
        return harden_catalog(Catalog.model_validate(data))
    except ValidationError as exc:
        raise ValueError(f"catalog does not match the Catalog schema: {exc}") from exc


class CatalogService:
    """Caches the catalog of one image digest (immutable once stored)."""

    def __init__(self, image_digest: str, *, catalog_path: Path | None = None) -> None:
        self.image_digest = image_digest
        self.catalog_path = catalog_path
        self._cached: CatalogSnapshot | None = None
        self._examples: PlotExamples | None = None

    def _snapshot(self, row: m.Catalog) -> CatalogSnapshot:
        # stored catalogs are hardened on the way in; a row written before v0.18 is hardened
        # here (it keeps its recorded digest: specs and runs pin that one)
        cat = harden_catalog(Catalog.model_validate(row.catalog))
        return CatalogSnapshot(
            row.image_digest, row.catalog_digest, cat, row.created_at, row.source
        )

    async def get(self, session: AsyncSession) -> CatalogSnapshot | None:
        if self._cached is not None:
            return self._cached
        row = await session.get(m.Catalog, self.image_digest)
        if row is not None:
            self._cached = self._snapshot(row)
        return self._cached

    async def require(self, session: AsyncSession) -> CatalogSnapshot:
        snap = await self.get(session)
        if snap is None:
            raise CatalogUnavailable(
                f"no catalog is available for image {self.image_digest} yet; a describe run "
                "must finish (or set MAPI_CATALOG_PATH) before specs can be validated"
            )
        return snap

    async def store(
        self,
        session: AsyncSession,
        catalog: Catalog,
        *,
        source: str,
        tenant_id: str,
        project_id: str,
        describe_run_id: str | None = None,
        image_digest: str | None = None,
        plot_examples: PlotExamples | None = None,
    ) -> CatalogSnapshot:
        """Insert the catalog for an image digest (first writer wins; it is immutable).

        The catalog is hardened first (:func:`harden_catalog`): what is stored, and digested,
        is what the control plane uses. ``plot_examples`` (from the same describe run) are
        stored with it, and only with it: a catalog that is already stored keeps the
        examples it has."""
        catalog = harden_catalog(catalog)
        digest = catalog_digest(catalog)
        image = image_digest or self.image_digest
        stmt = (
            insert(m.Catalog)
            .values(
                image_digest=image,
                catalog_digest=digest,
                catalog=catalog.model_dump(mode="json"),
                source=source,
                describe_run_id=describe_run_id,
                tenant_id=tenant_id,
                project_id=project_id,
                plot_examples=(
                    None
                    if plot_examples is None
                    else plot_examples.model_dump(mode="json", exclude_none=True)
                ),
            )
            .on_conflict_do_nothing(index_elements=["image_digest"])
        )
        await session.execute(stmt)
        row = (
            await session.execute(select(m.Catalog).where(m.Catalog.image_digest == image))
        ).scalar_one()
        snap = self._snapshot(row)
        if image == self.image_digest:
            self._cached = snap
        return snap

    async def load_file(
        self, session: AsyncSession, *, tenant_id: str, project_id: str
    ) -> CatalogSnapshot | None:
        """Import ``catalog_path`` if configured and nothing is cached yet."""
        existing = await self.get(session)
        if existing is not None or self.catalog_path is None:
            return existing
        catalog = parse_catalog(self.catalog_path.read_bytes())
        log.info("imported catalog from %s for image %s", self.catalog_path, self.image_digest)
        return await self.store(
            session, catalog, source="file", tenant_id=tenant_id, project_id=project_id
        )

    async def example(self, session: AsyncSession, ref: str) -> PlotSpec | None:
        """The example render of plot recipe ``ref`` (``id@version``) for this image digest;
        ``None`` when the catalog (or the recipe) has none."""
        if self._examples is None:
            row = (
                await session.execute(
                    select(m.Catalog)
                    .options(undefer(m.Catalog.plot_examples))
                    .where(m.Catalog.image_digest == self.image_digest)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            # validated by the finalizer; an unreadable value is treated as "no examples"
            try:
                self._examples = PlotExamples.model_validate(row.plot_examples or {})
            except ValidationError:
                log.warning("unreadable plot examples for image %s", self.image_digest)
                self._examples = PlotExamples()
        return self._examples.plots.get(ref)

    def invalidate(self) -> None:
        self._cached = None
        self._examples = None
